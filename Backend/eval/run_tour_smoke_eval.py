"""Tour smoke eval — generate real tours and structurally validate them.

Exercises the live tour pipeline (``app.tour.generate_tour``: Plan -> Retrieve ->
Draft) against the pinned FastAPI fixture, then runs each generated artifact
through the same ``validate_tour`` checks used by the structural eval (schema,
path exists, lines in bounds, snippet matches). Because snippets are extracted
deterministically from retrieved chunks, a healthy pipeline should validate
cleanly — this catches regressions where that grounding breaks.

Prerequisites:
    1. Postgres running with the fixture ingested (``eval.ingest_local``)
    2. ``OPENAI_API_KEY`` set for the pipeline LLM

Usage:
    uv run python -m eval.ingest_local
    uv run python -m eval.run_tour_smoke_eval
    uv run python -m eval.run_tour_smoke_eval --topic "request lifecycle" --json
    uv run python -m eval.run_tour_smoke_eval --strict
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

from app.config import settings
from app.models.code import CodeChunkModel
from app.tour import generate_tour
from eval.ingest_local import (
    DEFAULT_FIXTURE_PATH,
    EVAL_INSTALLATION_ID,
    FIXTURE_REPO_URL,
    FIXTURE_REPO_VERSION,
    ensure_fixture,
)
from eval.structural.validate import validate_tour

GOLDEN_DATASET = Path(__file__).parent / "golden_dataset.json"

DEFAULT_TOPICS = [
    "how a request flows through the framework",
    "dependency injection",
    "how routes are registered",
]


STRUCTURAL_FIXTURES_DIR = Path(__file__).parent / "structural" / "fixtures"
STRUCTURAL_MANIFEST = STRUCTURAL_FIXTURES_DIR / "manifest.json"


@dataclass
class TopicRun:
    topic: str
    title: str
    step_count: int
    valid: bool
    failed_checks: list[str]
    issues: list[dict]
    error: str | None
    elapsed_s: float
    artifact: dict | None = None


def _load_repo_name() -> str:
    try:
        data = json.loads(GOLDEN_DATASET.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read golden dataset at {GOLDEN_DATASET}: {exc}") from exc
    repo_name = data.get("repo_name")
    if not isinstance(repo_name, str):
        raise SystemExit(f"golden dataset must define a string repo_name: {GOLDEN_DATASET}")
    return repo_name


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


async def _run_topic(
    session: Session,
    *,
    topic: str,
    repo_name: str,
    installation_id: int,
    repo_root: Path,
    model: str | None,
    search_limit: int,
) -> TopicRun:
    started = time.monotonic()
    try:
        artifact = await generate_tour(
            session,
            topic=topic,
            repo_name=repo_name,
            installation_id=installation_id,
            model=model,
            search_limit=search_limit,
        )
    except Exception as exc:
        # Isolate per-topic failures (LLM error, empty retrieval, DB error) so one
        # bad topic doesn't abort the whole run. Roll back the shared session so a
        # DB error doesn't poison the next topic.
        session.rollback()
        return TopicRun(
            topic=topic,
            title="",
            step_count=0,
            valid=False,
            failed_checks=["harness_error"],
            issues=[{"kind": "harness_error", "step_index": None, "message": str(exc)}],
            error=str(exc),
            elapsed_s=round(time.monotonic() - started, 2),
        )

    artifact_dict = artifact.model_dump()
    result = validate_tour(artifact_dict, repo_root)
    return TopicRun(
        topic=topic,
        title=artifact.title,
        step_count=len(artifact.steps),
        valid=result.passed,
        failed_checks=sorted(c.value for c in result.failed_checks),
        issues=[
            {"kind": i.kind.value, "step_index": i.step_index, "message": i.message}
            for i in result.issues
        ],
        error=None,
        elapsed_s=round(time.monotonic() - started, 2),
        artifact=artifact_dict,
    )


def _aggregate(runs: list[TopicRun]) -> dict:
    n = len(runs)
    harness_errors = sum(1 for r in runs if "harness_error" in r.failed_checks)
    generated = [r for r in runs if "harness_error" not in r.failed_checks]
    valid = sum(1 for r in generated if r.valid)
    return {
        "topics": n,
        "harness_errors": harness_errors,
        "generated": len(generated),
        "valid_tours": valid,
        "validity_rate": valid / len(generated) if generated else 0.0,
        "total_steps": sum(r.step_count for r in generated),
    }


async def _run_all(
    *,
    repo_root: Path,
    repo_name: str,
    installation_id: int,
    topics: list[str],
    model: str | None,
    search_limit: int,
) -> list[TopicRun]:
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        count = _chunk_count(session, repo_name, installation_id)
        if count == 0:
            raise SystemExit(
                "No indexed chunks found for the fixture repo. "
                "Run: uv run python -m eval.ingest_local"
            )
        print(f"indexed chunks: {count}")

        runs: list[TopicRun] = []
        for topic in topics:
            print(f"generating tour: {topic!r}...", flush=True)
            runs.append(
                await _run_topic(
                    session,
                    topic=topic,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    repo_root=repo_root,
                    model=model,
                    search_limit=search_limit,
                )
            )
        return runs


def _print_report(repo_root: Path, repo_name: str, runs: list[TopicRun], aggregate: dict) -> None:
    print(f"\nTour smoke eval | repo={repo_name} | repo_root={repo_root}")
    print("=" * 90)
    print(f"{'steps':>6}{'valid':>7}{'t':>7}  topic")
    print("-" * 90)
    for run in runs:
        valid = "Y" if run.valid else "N"
        topic = run.topic if len(run.topic) <= 55 else run.topic[:52] + "..."
        print(f"{run.step_count:>6}{valid:>7}{run.elapsed_s:>7.1f}  {topic}")
    print("=" * 90)
    print("AGGREGATE")
    print(f"  topics          : {aggregate['topics']}")
    print(f"  harness errors  : {aggregate['harness_errors']}")
    print(f"  generated       : {aggregate['generated']}")
    print(f"  valid tours     : {aggregate['valid_tours']}")
    print(f"  validity_rate   : {aggregate['validity_rate']:.3f} (of generated)")
    print(f"  total steps     : {aggregate['total_steps']}")
    print()

    flagged = [r for r in runs if r.issues]
    if flagged:
        print(f"ISSUES ({len(flagged)} topics)")
        for run in flagged:
            print(f"  [{run.topic}]")
            for issue in run.issues:
                idx = issue["step_index"]
                label = f"step {idx}" if idx is not None else "tour"
                print(f"        {issue['kind']} ({label}): {issue['message']}")
        print()


def _save_structural_fixture(name: str, run: TopicRun, repo_version: str) -> Path:
    """Persist a real generated artifact as a structural fixture + manifest entry.

    Locks the pipeline's output shape into the (no-LLM) structural eval: the saved
    JSON must keep validating against the pinned fixture repo, so a regression that
    breaks grounding turns into a failing structural fixture.
    """
    if run.artifact is None:
        raise SystemExit(f"cannot save fixture {name!r}: topic produced no artifact")
    if not run.valid:
        raise SystemExit(
            f"refusing to save fixture {name!r}: generated tour did not validate "
            f"({', '.join(run.failed_checks) or 'unknown'})"
        )

    fixture_path = STRUCTURAL_FIXTURES_DIR / f"{name}.json"
    fixture_path.write_text(json.dumps(run.artifact, indent=2) + "\n")

    manifest = json.loads(STRUCTURAL_MANIFEST.read_text())
    manifest["repo_version"] = repo_version
    entry = {"id": name, "file": f"{name}.json", "expect": "pass"}
    fixtures = [f for f in manifest.get("fixtures", []) if f.get("id") != name]
    fixtures.append(entry)
    manifest["fixtures"] = fixtures
    STRUCTURAL_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")

    return fixture_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(DEFAULT_FIXTURE_PATH))
    parser.add_argument("--no-clone", action="store_true")
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="Topic to generate a tour for (repeatable; default: a small set)",
    )
    parser.add_argument("--model", default=None, help="OpenAI chat model (default: settings.agent_model)")
    parser.add_argument("--search-limit", type=int, default=6, help="Candidates retrieved per step")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", default=None, help="write the full report JSON to this path")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless every topic generated a structurally valid tour",
    )
    parser.add_argument(
        "--save-fixture",
        default=None,
        metavar="NAME",
        help=(
            "save the first valid generated tour as eval/structural/fixtures/"
            "NAME.json and register it in the manifest (locks output shape)"
        ),
    )
    args = parser.parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required for tour smoke eval")

    repo_root = Path(args.repo_root)
    if not args.no_clone:
        ensure_fixture(repo_root, FIXTURE_REPO_URL, FIXTURE_REPO_VERSION)
    repo_root = repo_root.resolve()
    if not repo_root.exists():
        raise SystemExit(
            f"repo root does not exist: {repo_root} (omit --no-clone to auto-fetch the fixture)"
        )

    repo_name = _load_repo_name()
    topics = args.topics or DEFAULT_TOPICS

    runs = asyncio.run(
        _run_all(
            repo_root=repo_root,
            repo_name=repo_name,
            installation_id=EVAL_INSTALLATION_ID,
            topics=topics,
            model=args.model,
            search_limit=args.search_limit,
        )
    )
    aggregate = _aggregate(runs)
    output = {
        "repo_name": repo_name,
        "repo_root": str(repo_root),
        "repo_version": FIXTURE_REPO_VERSION,
        "installation_id": EVAL_INSTALLATION_ID,
        "model": args.model or settings.agent_model,
        "search_limit": args.search_limit,
        "aggregate": aggregate,
        # Drop the full artifact from the report — it's captured via --save-fixture.
        "topics": [
            {k: v for k, v in asdict(run).items() if k != "artifact"} for run in runs
        ],
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        _print_report(repo_root, repo_name, runs, aggregate)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {out_path}", file=sys.stderr if args.json else sys.stdout)

    if args.save_fixture:
        valid_run = next((r for r in runs if r.valid and r.artifact), None)
        if valid_run is None:
            raise SystemExit("no valid tour to save as a fixture")
        fixture_path = _save_structural_fixture(
            args.save_fixture, valid_run, FIXTURE_REPO_VERSION
        )
        print(
            f"saved fixture {fixture_path} (topic={valid_run.topic!r})",
            file=sys.stderr if args.json else sys.stdout,
        )

    if args.strict and aggregate["valid_tours"] != aggregate["topics"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
