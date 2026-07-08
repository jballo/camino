"""Tour judge eval — score generated tours with an LLM-as-judge.

The structural eval proves a tour is *grounded* (real files, matching snippets);
this harness scores the qualities structure can't see — whether each step's prose
is **faithful** to its snippet and **relevant** to the topic, and whether the tour
is **complete** and well **ordered** (see ``eval.judge``).

Two sources of tours:

- **live** (default): generate a fresh tour per topic through the real pipeline
  (``app.tour.generate_tour``), then judge it. Needs Postgres with the fixture
  ingested (``eval.ingest_local``) and ``OPENAI_API_KEY``.
- **fixture** (``--from-fixture NAME``): judge an already-saved artifact from
  ``eval/structural/fixtures/`` (e.g. one captured via
  ``run_tour_smoke_eval --save-fixture``). No DB or generation — just the judge.

Scores are 1-5 per dimension; ``--min-score`` (default 3.5) is the pass bar and
``--strict`` exits non-zero if any judged tour falls below it.

Usage:
    uv run python -m eval.ingest_local
    uv run python -m eval.run_tour_judge_eval
    uv run python -m eval.run_tour_judge_eval --topic "dependency injection" --json
    uv run python -m eval.run_tour_judge_eval --from-fixture valid_minimal
    uv run python -m eval.run_tour_judge_eval --strict --min-score 3.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from langchain_openai import ChatOpenAI
from sqlalchemy import func
from sqlmodel import Session, create_engine, select

from app.config import settings
from app.models.code import CodeChunkModel
from app.models.tour import TourArtifact
from app.tour import generate_tour
from eval.ingest_local import EVAL_INSTALLATION_ID
from eval.judge import DimensionScores, judge_tour, summarize

GOLDEN_DATASET = Path(__file__).parent / "golden_dataset.json"
STRUCTURAL_FIXTURES_DIR = Path(__file__).parent / "structural" / "fixtures"

DEFAULT_TOPICS = [
    "how a request flows through the framework",
    "dependency injection",
    "how routes are registered",
]

DEFAULT_MIN_SCORE = 3.5


@dataclass
class JudgeRun:
    topic: str
    title: str
    step_count: int
    faithfulness: float | None
    relevance: float | None
    completeness: int | None
    ordering: int | None
    overall: float | None
    passed: bool
    step_scores: list[dict]
    summary: str | None
    error: str | None
    elapsed_s: float


def _load_repo_name() -> str:
    try:
        data = json.loads(GOLDEN_DATASET.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read golden dataset at {GOLDEN_DATASET}: {exc}") from exc
    repo_name = data.get("repo_name")
    if not isinstance(repo_name, str):
        raise SystemExit(f"golden dataset must define a string repo_name: {GOLDEN_DATASET}")
    return repo_name


def _load_fixture_artifact(name: str) -> TourArtifact:
    path = STRUCTURAL_FIXTURES_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise SystemExit(f"could not read fixture {name!r} at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in fixture {name!r} at {path}: {exc}") from exc
    try:
        return TourArtifact.model_validate(data)
    except Exception as exc:  # pydantic ValidationError et al.
        raise SystemExit(f"fixture {name!r} is not a valid TourArtifact: {exc}") from exc


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


def _harness_error_run(topic: str, message: str, started: float) -> JudgeRun:
    return JudgeRun(
        topic=topic,
        title="",
        step_count=0,
        faithfulness=None,
        relevance=None,
        completeness=None,
        ordering=None,
        overall=None,
        passed=False,
        step_scores=[],
        summary=None,
        error=message,
        elapsed_s=round(time.monotonic() - started, 2),
    )


def _run_from_scores(
    artifact: TourArtifact,
    scores: DimensionScores,
    summary: str | None,
    step_scores: list[dict],
    min_score: float,
    started: float,
) -> JudgeRun:
    passed = scores.overall is not None and scores.overall >= min_score
    return JudgeRun(
        topic=artifact.topic,
        title=artifact.title,
        step_count=len(artifact.steps),
        faithfulness=scores.faithfulness,
        relevance=scores.relevance,
        completeness=scores.completeness,
        ordering=scores.ordering,
        overall=scores.overall,
        passed=passed,
        step_scores=step_scores,
        summary=summary,
        error=None,
        elapsed_s=round(time.monotonic() - started, 2),
    )


async def _judge_artifact(
    llm: ChatOpenAI, artifact: TourArtifact, *, min_score: float, started: float
) -> JudgeRun:
    judgment = await judge_tour(llm, artifact)
    scores = summarize(judgment, len(artifact.steps))
    step_scores = [s.model_dump() for s in judgment.steps]
    return _run_from_scores(artifact, scores, judgment.summary, step_scores, min_score, started)


async def _run_live_topic(
    session: Session,
    judge_llm: ChatOpenAI,
    *,
    topic: str,
    repo_name: str,
    installation_id: int,
    model: str | None,
    search_limit: int,
    min_score: float,
) -> JudgeRun:
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
        # Isolate per-topic generation failures (LLM error, empty retrieval, DB
        # error) so one bad topic doesn't abort the run; roll back so a DB error
        # doesn't poison the next topic's session.
        session.rollback()
        return _harness_error_run(topic, f"generation failed: {exc}", started)

    try:
        return await _judge_artifact(judge_llm, artifact, min_score=min_score, started=started)
    except Exception as exc:
        return _harness_error_run(topic, f"judge failed: {exc}", started)


async def _run_fixture(
    judge_llm: ChatOpenAI, artifact: TourArtifact, *, min_score: float
) -> JudgeRun:
    started = time.monotonic()
    try:
        return await _judge_artifact(judge_llm, artifact, min_score=min_score, started=started)
    except Exception as exc:
        return _harness_error_run(artifact.topic, f"judge failed: {exc}", started)


def _aggregate(runs: list[JudgeRun]) -> dict:
    n = len(runs)
    harness_errors = sum(1 for r in runs if r.error is not None)
    judged = [r for r in runs if r.error is None]

    def _avg(attr: str) -> float | None:
        values = [getattr(r, attr) for r in judged if getattr(r, attr) is not None]
        return sum(values) / len(values) if values else None

    return {
        "topics": n,
        "harness_errors": harness_errors,
        "judged": len(judged),
        "passed": sum(1 for r in judged if r.passed),
        "avg_faithfulness": _avg("faithfulness"),
        "avg_relevance": _avg("relevance"),
        "avg_completeness": _avg("completeness"),
        "avg_ordering": _avg("ordering"),
        "avg_overall": _avg("overall"),
    }


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "—"


def _print_report(runs: list[JudgeRun], aggregate: dict, min_score: float) -> None:
    print(f"\nTour judge eval | min_score={min_score}")
    print("=" * 96)
    print(
        f"{'faith':>6}{'rel':>6}{'compl':>6}{'order':>6}{'all':>6}{'ok':>4}{'t':>7}  topic"
    )
    print("-" * 96)
    for run in runs:
        if run.error is not None:
            topic = run.topic if len(run.topic) <= 48 else run.topic[:45] + "..."
            print(f"{'—':>6}{'—':>6}{'—':>6}{'—':>6}{'—':>6}{'ERR':>4}{run.elapsed_s:>7.1f}  {topic}")
            continue
        ok = "Y" if run.passed else "N"
        topic = run.topic if len(run.topic) <= 48 else run.topic[:45] + "..."
        print(
            f"{_fmt(run.faithfulness):>6}{_fmt(run.relevance):>6}"
            f"{_fmt(float(run.completeness) if run.completeness is not None else None):>6}"
            f"{_fmt(float(run.ordering) if run.ordering is not None else None):>6}"
            f"{_fmt(run.overall):>6}{ok:>4}{run.elapsed_s:>7.1f}  {topic}"
        )
    print("=" * 96)
    print("AGGREGATE")
    print(f"  topics          : {aggregate['topics']}")
    print(f"  harness errors  : {aggregate['harness_errors']}")
    print(f"  judged          : {aggregate['judged']}")
    print(f"  passed          : {aggregate['passed']} (overall >= {min_score})")
    print(f"  avg faithfulness: {_fmt(aggregate['avg_faithfulness'])}")
    print(f"  avg relevance   : {_fmt(aggregate['avg_relevance'])}")
    print(f"  avg completeness: {_fmt(aggregate['avg_completeness'])}")
    print(f"  avg ordering    : {_fmt(aggregate['avg_ordering'])}")
    print(f"  avg overall     : {_fmt(aggregate['avg_overall'])}")
    print()

    flagged = [r for r in runs if r.error is not None or not r.passed]
    if flagged:
        print(f"FLAGGED ({len(flagged)} topics)")
        for run in flagged:
            if run.error is not None:
                print(f"  [{run.topic}] {run.error}")
                continue
            print(f"  [{run.topic}] overall={_fmt(run.overall)} — {run.summary or 'no summary'}")
            for score in run.step_scores:
                if score.get("faithfulness", 5) <= 3 or score.get("relevance", 5) <= 3:
                    print(
                        f"        step {score.get('step_index')}: "
                        f"faith={score.get('faithfulness')} rel={score.get('relevance')} "
                        f"{score.get('notes') or ''}"
                    )
        print()


async def _run_live(
    *,
    repo_name: str,
    installation_id: int,
    topics: list[str],
    model: str | None,
    judge_llm: ChatOpenAI,
    search_limit: int,
    min_score: float,
) -> list[JudgeRun]:
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        count = _chunk_count(session, repo_name, installation_id)
        if count == 0:
            raise SystemExit(
                "No indexed chunks found for the fixture repo. "
                "Run: uv run python -m eval.ingest_local"
            )
        print(f"indexed chunks: {count}")

        runs: list[JudgeRun] = []
        for topic in topics:
            print(f"generating + judging: {topic!r}...", flush=True)
            runs.append(
                await _run_live_topic(
                    session,
                    judge_llm,
                    topic=topic,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    model=model,
                    search_limit=search_limit,
                    min_score=min_score,
                )
            )
        return runs


async def _run_fixtures(
    *, names: list[str], judge_llm: ChatOpenAI, min_score: float
) -> list[JudgeRun]:
    runs: list[JudgeRun] = []
    for name in names:
        artifact = _load_fixture_artifact(name)
        print(f"judging fixture {name!r} (topic={artifact.topic!r})...", flush=True)
        runs.append(await _run_fixture(judge_llm, artifact, min_score=min_score))
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-fixture",
        action="append",
        dest="fixtures",
        metavar="NAME",
        help=(
            "judge a saved artifact from eval/structural/fixtures/NAME.json "
            "instead of generating live (repeatable; no DB needed)"
        ),
    )
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="topic to generate + judge (repeatable; default: a small set)",
    )
    parser.add_argument(
        "--model", default=None, help="generator chat model (default: settings.agent_model)"
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="judge chat model (default: settings.agent_model); use a stronger model to reduce self-preference bias",
    )
    parser.add_argument("--search-limit", type=int, default=6, help="candidates retrieved per step")
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="overall score a tour must reach to pass (1-5 scale)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--out", default=None, help="write the full report JSON to this path")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless every judged tour scored >= --min-score",
    )
    args = parser.parse_args()

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required for the tour judge eval")

    judge_model = args.judge_model or settings.agent_model
    judge_llm = ChatOpenAI(
        model=judge_model,
        api_key=settings.openai_api_key,
        temperature=0,
    )

    if args.fixtures:
        runs = asyncio.run(
            _run_fixtures(names=args.fixtures, judge_llm=judge_llm, min_score=args.min_score)
        )
        source = {"mode": "fixture", "fixtures": args.fixtures}
    else:
        repo_name = _load_repo_name()
        topics = args.topics or DEFAULT_TOPICS
        runs = asyncio.run(
            _run_live(
                repo_name=repo_name,
                installation_id=EVAL_INSTALLATION_ID,
                topics=topics,
                model=args.model,
                judge_llm=judge_llm,
                search_limit=args.search_limit,
                min_score=args.min_score,
            )
        )
        source = {"mode": "live", "repo_name": repo_name, "generator_model": args.model or settings.agent_model}

    aggregate = _aggregate(runs)
    output = {
        **source,
        "judge_model": judge_model,
        "min_score": args.min_score,
        "aggregate": aggregate,
        "topics": [asdict(run) for run in runs],
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        _print_report(runs, aggregate, args.min_score)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {out_path}", file=sys.stderr if args.json else sys.stdout)

    if args.strict:
        judged = [r for r in runs if r.error is None]
        if not judged or any(not r.passed for r in judged) or aggregate["harness_errors"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
