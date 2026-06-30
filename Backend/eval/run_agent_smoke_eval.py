"""Agent smoke eval — run the ReAct agent on a few questions and structurally check citations.

Exercises the live agent path (LLM + hybrid_search + answer text) then parses
free-text citations from the final answer and validates path existence and line
bounds against the pinned FastAPI fixture repo.

Prerequisites:
    1. Postgres running with the fixture ingested (``eval.ingest_local``)
    2. ``OPENAI_API_KEY`` set for the agent LLM

Usage:
    uv run python -m eval.ingest_local
    uv run python -m eval.run_agent_smoke_eval
    uv run python -m eval.run_agent_smoke_eval --question q02 --json
    uv run python -m eval.run_agent_smoke_eval --strict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import func
from sqlmodel import Session, create_engine, select

from app.agent.runner import answer_question
from app.config import settings
from app.models.code import CodeChunkModel
from eval.ingest_local import (
    DEFAULT_FIXTURE_PATH,
    EVAL_INSTALLATION_ID,
    FIXTURE_REPO_URL,
    FIXTURE_REPO_VERSION,
    ensure_fixture,
)
from eval.structural.citations import CitationRef, parse_citations, validate_citations

SMOKE_MANIFEST = Path(__file__).parent / "structural" / "smoke_questions.json"
GOLDEN_DATASET = Path(__file__).parent / "golden_dataset.json"


@dataclass
class QuestionRun:
    id: str
    question: str
    answer_preview: str
    citations: list[dict]
    citation_count: int
    citations_valid: bool
    failed_checks: list[str]
    issues: list[dict]
    source_count: int
    elapsed_s: float


def _load_smoke_ids() -> tuple[str, list[str]]:
    data = json.loads(SMOKE_MANIFEST.read_text())
    return data.get("repo_version", FIXTURE_REPO_VERSION), [
        item["id"] for item in data["questions"]
    ]


def _resolve_questions(wanted_ids: list[str] | None) -> tuple[list[dict], str, dict]:
    golden = json.loads(GOLDEN_DATASET.read_text())
    by_id = {q["id"]: q for q in golden["questions"]}
    smoke_version, smoke_ids = _load_smoke_ids()
    ids = wanted_ids or smoke_ids
    missing = [qid for qid in ids if qid not in by_id]
    if missing:
        raise SystemExit(f"unknown question id(s): {', '.join(sorted(missing))}")
    return [by_id[qid] for qid in ids], smoke_version, golden


def _chunk_count(session: Session, repo_name: str, installation_id: int) -> int:
    statement = (
        select(func.count())
        .select_from(CodeChunkModel)
        .where(
            CodeChunkModel.repo_name == repo_name,
            CodeChunkModel.installation_id == installation_id,
        )
    )
    return session.exec(statement).one()


def _citation_dicts(citations: list[CitationRef]) -> list[dict]:
    return [
        {
            "raw": c.raw,
            "file_path": c.file_path,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "symbol": c.symbol,
        }
        for c in citations
    ]


def _issue_dicts(result) -> list[dict]:
    return [
        {
            "kind": issue.kind.value,
            "citation_index": issue.step_index,
            "message": issue.message,
        }
        for issue in result.issues
    ]


async def _run_question(
    session: Session,
    *,
    question: dict,
    repo_name: str,
    installation_id: int,
    repo_root: Path,
    model: str | None,
    search_limit: int,
) -> QuestionRun:
    started = time.monotonic()
    try:
        agent_result = await answer_question(
            session,
            question=question["question"],
            repo_name=repo_name,
            installation_id=installation_id,
            model=model,
            search_limit=search_limit,
        )
    except Exception as exc:
        # Isolate per-question failures (API timeout, rate limit, DB error) so a
        # single flaky LLM call doesn't abort the whole run and discard the rest.
        # The Session is shared across questions, so roll back any half-finished
        # transaction to keep a DB error from poisoning the next question.
        session.rollback()
        return QuestionRun(
            id=question["id"],
            question=question["question"],
            answer_preview=f"[error: {exc}]",
            citations=[],
            citation_count=0,
            citations_valid=False,
            failed_checks=["harness_error"],
            issues=[{"kind": "harness_error", "citation_index": None, "message": str(exc)}],
            source_count=0,
            elapsed_s=round(time.monotonic() - started, 2),
        )
    citations = parse_citations(agent_result.answer)
    validation = validate_citations(citations, repo_root)
    preview = agent_result.answer.replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."

    return QuestionRun(
        id=question["id"],
        question=question["question"],
        answer_preview=preview,
        citations=_citation_dicts(citations),
        citation_count=len(citations),
        citations_valid=validation.passed,
        failed_checks=sorted(c.value for c in validation.failed_checks),
        issues=_issue_dicts(validation),
        source_count=len(agent_result.sources),
        elapsed_s=round(time.monotonic() - started, 2),
    )


def _aggregate(runs: list[QuestionRun]) -> dict:
    n = len(runs)
    with_citations = sum(1 for r in runs if r.citation_count > 0)
    valid = sum(1 for r in runs if r.citations_valid)
    total_citations = sum(r.citation_count for r in runs)
    return {
        "questions": n,
        "questions_with_citations": with_citations,
        "questions_all_citations_valid": valid,
        "citation_validity_rate": valid / n if n else 0.0,
        "total_citations": total_citations,
        "avg_citations_per_question": total_citations / n if n else 0.0,
    }


def _print_report(
    repo_root: Path,
    repo_version: str,
    repo_name: str,
    runs: list[QuestionRun],
    aggregate: dict,
) -> None:
    print(
        f"\nAgent smoke eval | repo={repo_name} | repo_root={repo_root} "
        f"| version={repo_version}"
    )
    print("=" * 90)
    print(
        f"{'id':<5}{'cite':>5}{'valid':>6}{'src':>4}{'t':>6}  question"
    )
    print("-" * 90)
    for run in runs:
        valid = "Y" if run.citations_valid else "N"
        q = run.question if len(run.question) <= 52 else run.question[:49] + "..."
        print(
            f"{run.id:<5}{run.citation_count:>5}{valid:>6}"
            f"{run.source_count:>4}{run.elapsed_s:>6.1f}  {q}"
        )
    print("=" * 90)
    print("AGGREGATE")
    print(f"  questions                 : {aggregate['questions']}")
    print(f"  with citations            : {aggregate['questions_with_citations']}")
    print(f"  all citations valid       : {aggregate['questions_all_citations_valid']}")
    print(f"  citation_validity_rate    : {aggregate['citation_validity_rate']:.3f}")
    print(f"  total citations           : {aggregate['total_citations']}")
    print()


def _print_issues(runs: list[QuestionRun]) -> None:
    flagged = [r for r in runs if r.issues]
    if not flagged:
        return
    print(f"CITATION ISSUES ({len(flagged)} questions)")
    for run in flagged:
        print(f"  [{run.id}] {run.question}")
        for issue in run.issues:
            idx = issue["citation_index"]
            label = f"citation {idx}" if idx is not None else "citation"
            print(f"        {issue['kind']} ({label}): {issue['message']}")
        if run.citations:
            print(f"        parsed: {run.citations}")
    print()


async def _run_all(
    *,
    repo_root: Path,
    repo_name: str,
    installation_id: int,
    questions: list[dict],
    model: str | None,
    search_limit: int,
) -> list[QuestionRun]:
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        count = _chunk_count(session, repo_name, installation_id)
        if count == 0:
            raise SystemExit(
                "No indexed chunks found for the fixture repo. "
                "Run: uv run python -m eval.ingest_local"
            )
        print(f"indexed chunks: {count}")

        runs: list[QuestionRun] = []
        for question in questions:
            print(f"running {question['id']}...", flush=True)
            runs.append(
                await _run_question(
                    session,
                    question=question,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    repo_root=repo_root,
                    model=model,
                    search_limit=search_limit,
                )
            )
        return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Local checkout used for citation path/line checks",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Do not auto-clone the pinned fixture if the path is missing",
    )
    parser.add_argument(
        "--question",
        action="append",
        dest="questions",
        help="Run only these golden question ids (repeatable; default: smoke set)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OpenAI chat model (default: settings.agent_model)",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=8,
        help="Chunks retrieved per hybrid_search tool call",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--out",
        default=None,
        help="write the full report JSON to this path",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 1 unless every question produced at least one citation and "
            "all parsed citations are structurally valid"
        ),
    )
    args = parser.parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required for agent smoke eval")

    repo_root = Path(args.repo_root)
    if not args.no_clone:
        ensure_fixture(repo_root, FIXTURE_REPO_URL, FIXTURE_REPO_VERSION)
    repo_root = repo_root.resolve()
    if not repo_root.exists():
        raise SystemExit(
            f"repo root does not exist: {repo_root} "
            "(omit --no-clone to auto-fetch the fixture)"
        )

    questions, repo_version, golden = _resolve_questions(args.questions)
    repo_name = golden["repo_name"]
    installation_id = golden.get("installation_id", EVAL_INSTALLATION_ID)

    runs = asyncio.run(
        _run_all(
            repo_root=repo_root,
            repo_name=repo_name,
            installation_id=installation_id,
            questions=questions,
            model=args.model,
            search_limit=args.search_limit,
        )
    )
    aggregate = _aggregate(runs)
    output = {
        "repo_name": repo_name,
        "repo_root": str(repo_root),
        "repo_version": repo_version,
        "installation_id": installation_id,
        "model": args.model or settings.agent_model,
        "search_limit": args.search_limit,
        "aggregate": aggregate,
        "questions": [asdict(run) for run in runs],
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        _print_report(repo_root, repo_version, repo_name, runs, aggregate)
        _print_issues(runs)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {out_path}", file=sys.stderr if args.json else sys.stdout)

    if args.strict and (
        aggregate["questions_all_citations_valid"] != aggregate["questions"]
        or aggregate["questions_with_citations"] != aggregate["questions"]
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
