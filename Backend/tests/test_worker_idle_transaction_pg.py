"""Generation must not hold a transaction open across awaits (#120).

A brief or tour awaits the LLM, embeddings and GitHub between its reads. If the
worker's session kept a transaction open across those awaits, its table locks
would block schema changes (ALTER TABLE, CREATE OR REPLACE VIEW) until the job
ended. These tests run the real graphs and search SQL with the slow calls
stubbed, and at every stub check from a separate connection that the worker
has nothing idle in a transaction and every table it reads can be locked
exclusively.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, select

from app.brief.schemas import BriefDraft, BriefReadingDraft, BriefSynthesis
from app.config import settings
from app.db import engine_options
from app.models.job import Job, JobStatus, JobType
from app.services.fork_status import ForkStatus
from app.services.staleness import HeadComparison
from app.services.target_branch import TargetBranchResolution
from app.tour.schemas import DraftedStep, PlannedStep, TourPlan
from app.worker import claim_next_job, run_job
from tests.test_brief import issue as _issue
from tests.test_repository_ingestion_pg import (  # noqa: F401  (fixtures)
    _ingest,
    _vectors,
    engine,
    pg_engine,
)
from tests.test_worker_claim_pg import WORKER_A, _insert_job, _reload

WORKER_APPLICATION = "camino-worker-120"
READ_TABLES = ("jobs", "code_chunks", "code_chunk_embeddings", "repo_index_state")
INDEXED_SHA = "deadbeef"


class _LockProbe:
    """Checks, from its own connection, that the worker blocks no DDL."""

    def __init__(self, url):
        self._engine = create_engine(url, poolclass=NullPool)
        self.checkpoints: list[str] = []
        self.problems: list[str] = []

    def __call__(self, checkpoint: str) -> None:
        self.checkpoints.append(checkpoint)
        with self._engine.connect() as conn:
            idle = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = :application "
                    "AND state = 'idle in transaction'"
                ),
                {"application": WORKER_APPLICATION},
            ).scalar_one()
            conn.rollback()
            if idle:
                self.problems.append(
                    f"{checkpoint}: {idle} worker connection(s) idle in transaction"
                )
            for table in READ_TABLES:
                # ALTER TABLE takes this lock; the transaction is rolled back
                # before the worker resumes, so the probe never changes schema.
                try:
                    conn.execute(text("SET LOCAL lock_timeout = '1s'"))
                    conn.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
                except DBAPIError as error:
                    self.problems.append(
                        f"{checkpoint}: cannot lock {table} "
                        f"({type(error.orig).__name__})"
                    )
                finally:
                    conn.rollback()

    def dispose(self) -> None:
        self._engine.dispose()


class _Structured:
    def __init__(self, probe, checkpoint, value):
        self._probe = probe
        self._checkpoint = checkpoint
        self._value = value

    async def ainvoke(self, _messages):
        self._probe(self._checkpoint)
        return self._value


class _FakeLLM:
    def __init__(self, probe, outputs):
        self._probe = probe
        self._outputs = outputs

    def with_structured_output(self, model):
        checkpoint, value = self._outputs[model]
        return _Structured(self._probe, checkpoint, value)


def _brief_llm(probe):
    return _FakeLLM(
        probe,
        {
            BriefSynthesis: (
                "brief synthesis",
                BriefSynthesis(
                    scope_summary="Change what a() returns.",
                    # Two queries: retrieval gathers both searches on one session.
                    retrieval_queries=["def a", "def b"],
                    issue_keywords=["a"],
                    confidence="high",
                ),
            ),
            BriefDraft: (
                "brief drafting",
                BriefDraft(summary="Edit a() and its test."),
            ),
            BriefReadingDraft: (
                "brief reading step",
                BriefReadingDraft(
                    chunk_id=0,
                    title="Read a",
                    explanation="Returns one.",
                    start_line=1,
                    end_line=2,
                ),
            ),
        },
    )


def _tour_llm(probe):
    return _FakeLLM(
        probe,
        {
            TourPlan: (
                "tour planning",
                TourPlan(
                    title="How a works",
                    steps=[PlannedStep(step_intent="find a", search_query="def a")],
                ),
            ),
            DraftedStep: (
                "tour drafting",
                DraftedStep(
                    chunk_id=0,
                    title="a",
                    explanation="Returns one.",
                    start_line=1,
                    end_line=2,
                ),
            ),
        },
    )


@pytest.fixture
def indexed(engine):  # noqa: F811
    with Session(engine) as session:
        session.execute(
            text("TRUNCATE jobs, githubconnections RESTART IDENTITY CASCADE")
        )
        session.commit()
    return engine


@pytest.fixture
def worker_engine(indexed):
    """The worker's engine, tagged so the probe can find its connections."""
    options = engine_options(settings)
    options["connect_args"]["application_name"] = WORKER_APPLICATION
    worker = create_engine(indexed.url, **options)
    yield worker
    worker.dispose()


@pytest.fixture
def probe(indexed):
    lock_probe = _LockProbe(indexed.url)
    yield lock_probe
    lock_probe.dispose()


async def _index_repository(engine) -> None:
    with Session(engine) as session:
        await _ingest(session, embed=_vectors)


def _claim(engine, **overrides) -> int:
    with Session(engine) as session:
        job_id = _insert_job(session, **overrides).id
        if overrides.get("job_type") == JobType.ISSUE_BRIEF:
            session.execute(
                text(
                    "UPDATE jobs SET issue_repo = 'org/repo', issue_number = 44 "
                    "WHERE id = :job_id"
                ),
                {"job_id": job_id},
            )
            session.commit()
        assert claim_next_job(session, WORKER_A) == job_id
    return job_id


def _comparison(probe, checkpoint, comparison):
    def compare(*_args, **_kwargs):
        probe(checkpoint)
        return comparison

    return compare


async def _embed(probe, texts):
    probe("embedding")
    return _vectors(texts)


def _assert_pool_reset(worker_engine) -> None:
    """Connections the read session used go back transactional.

    Postgres reports read committed even on an autocommit connection, so
    check that a rollback really undoes work.
    """
    assert worker_engine.pool.checkedout() == 0
    connections = [
        worker_engine.connect() for _ in range(worker_engine.pool.checkedin())
    ]
    try:
        for connection in connections:
            assert connection.connection.driver_connection.autocommit is False
            connection.execute(text("CREATE TEMP TABLE reset_probe (id int)"))
            connection.rollback()
            leftover = connection.execute(
                text("SELECT to_regclass('pg_temp.reset_probe')")
            ).scalar_one()
            assert leftover is None
            isolation = connection.execute(
                text("SHOW transaction_isolation")
            ).scalar_one()
            assert isolation == "read committed"
    finally:
        for connection in connections:
            connection.close()


async def _run(worker_engine, job_id, probe, llm_patch, *extra_patches) -> None:
    async def embed(texts):
        return await _embed(probe, texts)

    with ExitStack() as stack:
        for active in (
            patch("app.worker.engine", worker_engine),
            patch("app.services.search.embed_batch", side_effect=embed),
            llm_patch,
            *extra_patches,
        ):
            stack.enter_context(active)
        await run_job(job_id, WORKER_A)


def _brief_patches(probe, *, comparison: HeadComparison):
    resolution = TargetBranchResolution(
        branch="main",
        source="default_branch",
        evidence="Default branch",
        evidence_path=None,
        default_branch="main",
        checked_at=dt.datetime.now(dt.UTC),
    )
    fork = ForkStatus("org/repo", "org/repo", None, False, "main", 0, True)

    def fetch_issue(*_args, **_kwargs):
        probe("issue thread")
        return _issue()

    def target_branch(*_args, **_kwargs):
        probe("target branch")
        return resolution

    def fork_status(*_args, **_kwargs):
        probe("fork status")
        return fork

    return (
        patch("app.worker.fetch_issue_thread", side_effect=fetch_issue),
        patch("app.worker.resolve_target_branch", side_effect=target_branch),
        patch("app.worker.resolve_fork_status", side_effect=fork_status),
        patch(
            "app.brief.graph.compare_to_head",
            side_effect=_comparison(probe, "brief staleness", comparison),
        ),
    )


async def test_tour_generation_holds_no_transaction(indexed, worker_engine, probe):
    await _index_repository(indexed)
    job_id = _claim(indexed, job_type=JobType.TOUR, topic="how a works")

    await _run(
        worker_engine,
        job_id,
        probe,
        patch("app.tour.runner.ChatOpenAI", return_value=_tour_llm(probe)),
        patch(
            "app.worker.compare_to_head",
            side_effect=_comparison(
                probe, "tour freshness", HeadComparison(INDEXED_SHA, 0, (), True)
            ),
        ),
    )

    row = _reload(indexed, job_id)
    assert row.status == JobStatus.COMPLETE, row.error
    assert row.artifact["freshness"]["indexed_sha"] == INDEXED_SHA
    assert {"tour planning", "embedding", "tour drafting", "tour freshness"} <= set(
        probe.checkpoints
    )
    assert probe.problems == []
    _assert_pool_reset(worker_engine)


async def test_brief_generation_holds_no_transaction(indexed, worker_engine, probe):
    await _index_repository(indexed)
    job_id = _claim(indexed, job_type=JobType.ISSUE_BRIEF, topic="Issue 44")

    await _run(
        worker_engine,
        job_id,
        probe,
        patch("app.brief.runner.ChatOpenAI", return_value=_brief_llm(probe)),
        *_brief_patches(probe, comparison=HeadComparison(INDEXED_SHA, 0, (), True)),
    )

    row = _reload(indexed, job_id)
    assert row.status == JobStatus.COMPLETE, row.error
    assert {
        "issue thread",
        "target branch",
        "fork status",
        "brief synthesis",
        "embedding",
        "brief staleness",
        "brief drafting",
        "brief reading step",
    } <= set(probe.checkpoints)
    assert probe.problems == []
    _assert_pool_reset(worker_engine)


async def test_stale_brief_fallback_holds_no_transaction(
    indexed, worker_engine, probe
):
    """Out of refresh budget, the brief is drafted again with allow_stale."""
    await _index_repository(indexed)
    job_id = _claim(indexed, job_type=JobType.ISSUE_BRIEF, topic="Issue 44")

    await _run(
        worker_engine,
        job_id,
        probe,
        patch("app.brief.runner.ChatOpenAI", return_value=_brief_llm(probe)),
        patch("app.worker._charge_refresh_ingest", return_value=False),
        *_brief_patches(probe, comparison=HeadComparison(None, None, (), False)),
    )

    row = _reload(indexed, job_id)
    assert row.status == JobStatus.COMPLETE, row.error
    assert row.artifact["honesty_note"]
    # Both drafts (the refused one and the stale fallback) were checked.
    assert probe.checkpoints.count("brief staleness") == 2
    assert probe.problems == []
    with Session(indexed) as session:
        # The refused refresh's shared ingest was rolled back, not left behind.
        assert session.exec(
            select(Job).where(Job.job_type == JobType.REPOSITORY_INGEST)
        ).all() == []
    _assert_pool_reset(worker_engine)
