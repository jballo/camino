import asyncio
import contextlib
import logging
import threading
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import exc

from app.models.job import JobStatus, JobType
from app.rate_limit import RateLimitDecision
from app.brief import BriefNeedsRefreshError
from app.models.tour import TourArtifact, TourStep
from app.services.staleness import ChangedFile, HeadComparison
from app.services.github_app import INVALID_CONNECTION_MESSAGE
from app.services.repository_ingestion import (
    IngestionCancelledError,
    PermanentRepositoryIngestionError,
    SponsorInstallationInvalidError,
    TransientRepositoryIngestionError,
)
from app.tour import TourGenerationCancelledError, TourGenerationError
from app.worker import (
    JobAuthorizationRevokedError,
    _describe_cause,
    _run_standalone,
    _ensure_ingestion_owned,
    _fail_rejected_sponsor_waiters,
    _requeue_or_fail,
    _stage_owned_ingestion_completion,
    release_shared_ingest,
    run_job,
    worker_loop,
)

WORKER_ID = "test-host:1:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


async def test_standalone_worker_verifies_embedding_schema():
    connection = MagicMock()
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = connection
    loop = asyncio.get_running_loop()

    with (
        patch("app.worker.engine", mock_engine),
        patch("app.worker.verify_embedding_schema") as verify_schema,
        patch("app.worker.worker_loop", new=AsyncMock()) as loop_worker,
        patch.object(loop, "add_signal_handler"),
    ):
        await _run_standalone()

    verify_schema.assert_called_once_with(connection)
    loop_worker.assert_awaited_once()


def _artifact() -> TourArtifact:
    return TourArtifact(
        title="Auth tour",
        topic="authentication flow",
        repo_name="org/repo",
        steps=[
            TourStep(
                title="Validate JWT",
                explanation="Checks the token.",
                file_path="auth.py",
                start_line=1,
                end_line=10,
                snippet="def validate(): ...",
            )
        ],
    )


def _job(**overrides) -> MagicMock:
    job = MagicMock()
    job.id = 1
    job.topic = "authentication flow"
    job.repo_name = "org/repo"
    job.issue_repo = None
    job.ref = "main"
    job.installation_id = 12345
    job.userId = "user_1"
    job.job_type = JobType.TOUR
    job.status = JobStatus.RUNNING
    job.claimed_by = WORKER_ID
    job.attempts = 1
    job.artifact = None
    job.error = None
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _shared_ingest_job(**overrides) -> MagicMock:
    return _job(
        job_type=JobType.REPOSITORY_INGEST,
        topic=None,
        userId=None,
        installation_id=None,
        **overrides,
    )


def _patch_sponsors(*installation_ids: int):
    return patch(
        "app.worker._sponsor_installations",
        return_value=list(installation_ids),
    )


def _patch_session(session: MagicMock):
    ctx = MagicMock()
    ctx.__enter__.return_value = session
    ctx.__exit__.return_value = False
    return patch("app.worker.Session", return_value=ctx)


async def test_run_job_success_persists_artifact():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    artifact = _artifact()
    persist = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=artifact),
    ):
        await run_job(1, WORKER_ID)

    persist.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        status=JobStatus.COMPLETE,
        artifact=artifact.model_dump(),
        error=None,
        claimed_at=None,
        claimed_by=None,
    )


async def test_run_job_does_not_call_github_after_authorization_is_revoked():
    job = _job(
        job_type=JobType.ISSUE_BRIEF,
        issue_repo="org/repo",
        issue_number=44,
        topic="Revoked request",
    )
    session = MagicMock()
    session.get.return_value = job

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch(
            "app.worker._ensure_job_authorized",
            side_effect=JobAuthorizationRevokedError("revoked"),
        ),
        patch("app.worker.fetch_issue_thread") as fetch_issue,
        patch("app.worker._update_owned_job") as persist,
        patch("app.worker._mark_failed") as mark_failed,
    ):
        await run_job(1, WORKER_ID)

    fetch_issue.assert_not_called()
    persist.assert_not_called()
    mark_failed.assert_not_called()
    session.rollback.assert_called()


def _refresh_brief_job():
    return _job(
        job_type=JobType.ISSUE_BRIEF,
        issue_repo="org/repo",
        issue_number=44,
        refresh_cycles=0,
        userId="user_1",
        topic="Fix refresh races",
    )


def _run_refresh(session, *, created, decision=None, consume_error=None, generate=None):
    """Run a brief whose first draft needs a refresh ingest.

    ``created`` says whether the refresh starts a new shared ingest or joins
    an active one; ``decision`` is what the ingest bucket answers.
    """
    dependency = MagicMock(id=17)
    consume = MagicMock(return_value=decision, side_effect=consume_error)
    mocks = SimpleNamespace(
        enqueue=MagicMock(return_value=(dependency, dependency, created)),
        consume=consume,
        park=MagicMock(return_value=True),
        persist=MagicMock(return_value=True),
        requeue=MagicMock(),
        generate=generate
        or AsyncMock(side_effect=BriefNeedsRefreshError("stale")),
    )
    return dependency, mocks, (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.fetch_issue_thread", return_value=MagicMock(branch_instruction=None)),
        patch("app.worker.resolve_target_branch", return_value=MagicMock(branch="main", default_branch="main")),
        patch("app.worker.resolve_fork_status", return_value=MagicMock()),
        patch("app.worker.generate_brief", mocks.generate),
        patch("app.worker.enqueue_shared_ingest", mocks.enqueue),
        patch("app.rate_limit.consume_fixed_window", consume),
        patch("app.worker.park_job", mocks.park),
        patch("app.worker._update_owned_job", mocks.persist),
        patch("app.worker._requeue_or_fail", mocks.requeue),
    )


async def _run_refresh_job(patches):
    with contextlib.ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        await run_job(1, WORKER_ID)


async def test_issue_brief_parks_behind_refresh_without_spending_retry():
    session = MagicMock()
    session.get.return_value = _refresh_brief_job()
    _, mocks, patches = _run_refresh(
        session,
        created=True,
        decision=RateLimitDecision(allowed=True, retry_after=60),
    )

    await _run_refresh_job(patches)

    assert mocks.enqueue.call_args.kwargs["waiting_row"] is False
    assert mocks.enqueue.call_args.kwargs["commit"] is False
    mocks.park.assert_called_once_with(
        session, 1, WORKER_ID, blocked_by_job_id=17
    )
    mocks.persist.assert_not_called()


async def test_refresh_that_starts_an_ingest_charges_the_requester_once():
    session = MagicMock()
    session.get.return_value = _refresh_brief_job()
    _, mocks, patches = _run_refresh(
        session,
        created=True,
        decision=RateLimitDecision(allowed=True, retry_after=60),
    )

    await _run_refresh_job(patches)

    mocks.consume.assert_called_once()
    assert mocks.consume.call_args.kwargs["bucket"] == "repository_ingest"
    assert mocks.consume.call_args.kwargs["user_id"] == "user_1"
    mocks.park.assert_called_once()


async def test_refresh_that_joins_an_active_ingest_is_free():
    session = MagicMock()
    session.get.return_value = _refresh_brief_job()
    _, mocks, patches = _run_refresh(session, created=False)

    await _run_refresh_job(patches)

    mocks.consume.assert_not_called()
    mocks.park.assert_called_once_with(
        session, 1, WORKER_ID, blocked_by_job_id=17
    )


async def test_refresh_with_an_empty_ingest_bucket_drafts_a_stale_brief():
    session = MagicMock()
    session.get.return_value = _refresh_brief_job()
    artifact = MagicMock()
    artifact.model_dump.return_value = {"summary": "stale but disclosed"}
    generate = AsyncMock(side_effect=[BriefNeedsRefreshError("stale"), artifact])
    _, mocks, patches = _run_refresh(
        session,
        created=True,
        decision=RateLimitDecision(allowed=False, retry_after=60),
        generate=generate,
    )

    await _run_refresh_job(patches)

    # The uncommitted refresh ingest is rolled back, not parked on.
    mocks.park.assert_not_called()
    session.rollback.assert_called()
    assert [call.kwargs["allow_stale"] for call in generate.await_args_list] == [
        False,
        True,
    ]
    mocks.persist.assert_called_once()
    assert mocks.persist.call_args.kwargs["status"] == JobStatus.COMPLETE
    assert mocks.persist.call_args.kwargs["artifact"] == {
        "summary": "stale but disclosed"
    }


async def test_refresh_retries_the_brief_when_the_limiter_is_unavailable():
    session = MagicMock()
    session.get.return_value = _refresh_brief_job()
    _, mocks, patches = _run_refresh(
        session,
        created=True,
        consume_error=HTTPException(
            status_code=503, detail="Rate limit service unavailable"
        ),
    )

    await _run_refresh_job(patches)

    mocks.park.assert_not_called()
    mocks.persist.assert_not_called()
    mocks.requeue.assert_called_once()
    assert mocks.requeue.call_args.kwargs["error"] == "Rate limit service unavailable"


async def test_legacy_issue_brief_without_issue_repo_fails_without_fetching():
    job = _job(
        job_type=JobType.ISSUE_BRIEF,
        issue_repo=None,
        issue_number=44,
        userId="user_1",
        topic="Legacy issue",
    )
    session = MagicMock()
    session.get.return_value = job

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.fetch_issue_thread") as fetch_issue,
        patch("app.worker._mark_failed") as mark_failed,
    ):
        await run_job(1, WORKER_ID)

    fetch_issue.assert_not_called()
    mark_failed.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        "Issue brief job is missing its issue repository",
    )


async def test_issue_brief_fetches_issue_from_fork_and_indexes_upstream():
    job = _job(
        job_type=JobType.ISSUE_BRIEF,
        issue_repo="contributor/repo",
        issue_number=44,
        refresh_cycles=0,
        userId="user_1",
        topic="Fork-only issue",
    )
    session = MagicMock()
    session.get.return_value = job
    issue = MagicMock(branch_instruction=None)
    artifact = MagicMock()
    artifact.model_dump.return_value = {"summary": "done"}
    persist = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.fetch_issue_thread", return_value=issue) as fetch_issue,
        patch(
            "app.worker.resolve_target_branch",
            return_value=MagicMock(branch="main", default_branch="main"),
        ) as resolve_branch,
        patch("app.worker.resolve_fork_status", return_value=MagicMock()) as resolve_fork,
        patch(
            "app.worker.generate_brief", new_callable=AsyncMock, return_value=artifact
        ),
        patch("app.worker._update_owned_job", persist),
    ):
        await run_job(1, WORKER_ID)

    fetch_issue.assert_called_once_with("contributor/repo", 44, 12345)
    resolve_branch.assert_called_once_with("org/repo", 12345)
    resolve_fork.assert_called_once_with("contributor/repo", 12345, "main")


async def test_run_job_stamps_tour_freshness():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    session.exec.return_value.one_or_none.return_value = SimpleNamespace(
        indexed_sha="abc123456789"
    )
    artifact = _artifact()
    persist = MagicMock(return_value=True)
    comparison = HeadComparison(
        head_sha="def987654321",
        commits_behind=3,
        changed_files=(
            ChangedFile("auth.py", "modified"),
            ChangedFile("unrelated.py", "added"),
        ),
        measurable=True,
    )

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker.compare_to_head", return_value=comparison) as compare,
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=artifact),
    ):
        await run_job(1, WORKER_ID)

    compare.assert_called_once_with("org/repo", 12345, "abc123456789", "main")
    persisted = persist.call_args.kwargs["artifact"]
    assert persisted["freshness"]["indexed_sha"] == "abc123456789"
    assert persisted["freshness"]["head_sha"] == "def987654321"
    assert persisted["freshness"]["commits_behind"] == 3
    assert persisted["freshness"]["changed_cited_files"] == ["auth.py"]
    assert isinstance(persisted["freshness"]["checked_at"], str)


async def test_run_job_tolerates_freshness_compare_failure():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    session.exec.return_value.one_or_none.return_value = SimpleNamespace(
        indexed_sha="abc123"
    )
    persist = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker.compare_to_head", side_effect=RuntimeError("GitHub down")),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=_artifact()),
    ):
        await run_job(1, WORKER_ID)

    assert persist.call_args.kwargs["status"] == JobStatus.COMPLETE
    assert persist.call_args.kwargs["artifact"]["freshness"] is None


async def test_run_job_tour_generation_error_marks_failed():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch(
            "app.worker.generate_tour",
            new_callable=AsyncMock,
            side_effect=TourGenerationError("no grounded steps"),
        ),
    ):
        await run_job(1, WORKER_ID)

    mark_failed.assert_called_once_with(
        session, 1, WORKER_ID, "no grounded steps"
    )


async def test_run_job_fails_legacy_row_without_ref_permanently():
    job = _job(ref=None)
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker.generate_tour", new_callable=AsyncMock) as generate,
    ):
        await run_job(1, WORKER_ID)

    generate.assert_not_awaited()
    mark_failed.assert_called_once_with(
        session, 1, WORKER_ID, "Legacy job is missing its repository ref"
    )


async def test_run_job_unexpected_exception_does_not_propagate():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch(
            "app.worker.generate_tour",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        await run_job(1, WORKER_ID)

    mark_failed.assert_called_once_with(
        session, 1, WORKER_ID, "Internal tour error"
    )


async def test_run_job_commit_failure_falls_back_to_failed():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch(
            "app.worker._update_owned_job",
            side_effect=exc.SQLAlchemyError("persist failed"),
        ),
        patch("app.worker._requeue_or_fail", requeue),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=_artifact()),
    ):
        await run_job(1, WORKER_ID)

    requeue.assert_called_once()
    assert requeue.call_args.args == (session, 1, WORKER_ID)
    assert requeue.call_args.kwargs["attempts"] == 1
    assert requeue.call_args.kwargs["error"].startswith(
        "Failed to persist job result:"
    )


async def test_run_job_missing_row_returns_without_crashing():
    session = MagicMock()
    session.get.return_value = None
    generate = AsyncMock()

    with (
        _patch_session(session),
        patch("app.worker.generate_tour", generate),
    ):
        await run_job(99, WORKER_ID)

    generate.assert_not_called()


async def test_run_job_renews_lease_during_generation():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    renew = MagicMock(return_value=True)

    async def _slow_generation(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return _artifact()

    with (
        _patch_session(session),
        patch("app.worker.settings.worker_lease_timeout", 0.03),
        patch("app.worker._renew_job_lease", renew),
        patch("app.worker._update_owned_job", return_value=True),
        patch("app.worker.generate_tour", side_effect=_slow_generation),
    ):
        await run_job(1, WORKER_ID)

    # One renewal establishes the lease; later calls are heartbeats.
    assert renew.call_count >= 2


async def test_run_job_heartbeat_interval_is_based_only_on_lease_timeout():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    heartbeat = MagicMock()

    with (
        _patch_session(session),
        patch("app.worker.settings.worker_lease_timeout", 600),
        patch("app.worker.settings.worker_poll_interval", 0.01),
        patch("app.worker.threading.Thread", return_value=heartbeat) as thread,
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", return_value=True),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=_artifact()),
    ):
        await run_job(1, WORKER_ID)

    assert thread.call_args.kwargs["kwargs"]["interval"] == 200
    heartbeat.start.assert_called_once_with()
    heartbeat.join.assert_called_once_with()


async def test_run_job_discards_result_after_lease_is_lost():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    persist = MagicMock(return_value=True)

    async def _slow_generation(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return _artifact()

    with (
        _patch_session(session),
        patch("app.worker.settings.worker_lease_timeout", 0.03),
        patch("app.worker._renew_job_lease", side_effect=[True, False]),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker.generate_tour", side_effect=_slow_generation),
    ):
        await run_job(1, WORKER_ID)

    persist.assert_not_called()


async def test_run_job_propagates_lease_loss_to_running_tour():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    persist = MagicMock(return_value=True)
    mark_failed = MagicMock(return_value=True)
    requeue = MagicMock(return_value=True)

    async def _cancel_when_lease_is_lost(*_args, cancel_event, **_kwargs):
        while not cancel_event.is_set():
            await asyncio.sleep(0.001)
        raise TourGenerationCancelledError("Tour generation was cancelled")

    with (
        _patch_session(session),
        patch("app.worker.settings.worker_lease_timeout", 0.03),
        patch("app.worker._renew_job_lease", side_effect=[True, False]),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker._requeue_or_fail", requeue),
        patch("app.worker.generate_tour", side_effect=_cancel_when_lease_is_lost),
    ):
        await asyncio.wait_for(run_job(1, WORKER_ID), timeout=1)

    persist.assert_not_called()
    mark_failed.assert_not_called()
    requeue.assert_not_called()
    session.rollback.assert_called()


async def test_run_job_dispatches_repository_ingestion():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    result = {"chunks_inserted": 12, "embeddings_created": 12}
    persist = MagicMock(return_value=True)
    stage_completion = MagicMock()

    async def ingest_and_finalize(
        _session,
        *,
        finalize_publication,
        **_kwargs,
    ):
        finalize_publication(_session, result)
        return result

    with (
        _patch_session(session),
        _patch_sponsors(12345, 67890),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch(
            "app.worker._stage_owned_ingestion_completion",
            stage_completion,
        ),
        patch(
            "app.worker.ingest_repository",
            side_effect=ingest_and_finalize,
        ) as ingest,
        patch("app.worker.generate_tour", new_callable=AsyncMock) as generate,
    ):
        await run_job(1, WORKER_ID)

    ingest.assert_awaited_once_with(
        session,
        repo_name="org/repo",
        installation_id=12345,
        ref="main",
        ensure_owned=ANY,
        finalize_publication=ANY,
    )
    generate.assert_not_awaited()
    stage_completion.assert_called_once_with(
        session,
        job_id=1,
        worker_id=WORKER_ID,
        artifact=result,
    )
    persist.assert_not_called()


def test_ingestion_completion_is_staged_without_a_separate_commit():
    session = MagicMock()
    session.execute.return_value.rowcount = 1
    artifact = {"chunks_inserted": 12, "embeddings_created": 12}

    _stage_owned_ingestion_completion(
        session,
        job_id=1,
        worker_id=WORKER_ID,
        artifact=artifact,
    )

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "jobs.id = :id_1" in compiled
    assert "jobs.status = :status_1" in compiled
    assert "jobs.claimed_by = :claimed_by_1" in compiled
    session.commit.assert_not_called()


async def test_run_job_requeues_transient_ingestion_failure():
    job = _shared_ingest_job(attempts=2)
    session = MagicMock()
    session.get.return_value = job
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(12345),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._requeue_or_fail", requeue),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=TransientRepositoryIngestionError("GitHub unavailable"),
        ),
    ):
        await run_job(1, WORKER_ID)

    requeue.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        attempts=2,
        error="GitHub unavailable",
    )


async def test_run_job_fails_permanent_ingestion_failure():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(12345),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=PermanentRepositoryIngestionError("Repository not found"),
        ),
    ):
        await run_job(1, WORKER_ID)

    mark_failed.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        "Repository not found",
    )



def _raise_wrapped(wrapper: Exception, cause: Exception):
    """Return an async side effect that raises ``wrapper`` from ``cause``."""

    async def side_effect(*_args, **_kwargs):
        raise wrapper from cause

    return side_effect


def _job_failure_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "app.worker"
        and record.getMessage().startswith("job failed")
    ]


_INTEGRITY_CAUSE = (
    'duplicate key value violates unique constraint "uq_chunk_identity_gen"\n'
    "DETAIL:  Key (file_path, symbol_name, start_line)="
    "(docs/jquery.min.js, n, 2) already exists."
)


def test_describe_cause_is_empty_without_a_chain():
    assert _describe_cause(ValueError("plain")) == ""


def test_describe_cause_reports_innermost_exception_on_one_line():
    root = RuntimeError("line one\n  line two")
    try:
        try:
            raise root
        except RuntimeError as inner:
            raise KeyError("middle") from inner
    except KeyError as middle:
        error = PermanentRepositoryIngestionError("outer")
        error.__cause__ = middle

    assert _describe_cause(error) == " | cause=RuntimeError: line one line two"



def test_describe_cause_follows_implicit_context():
    # Raised inside an except block without "from": only __context__ is set.
    try:
        try:
            raise RuntimeError("root")
        except RuntimeError:
            raise PermanentRepositoryIngestionError("outer")
    except PermanentRepositoryIngestionError as error:
        assert error.__cause__ is None
        assert error.__context__ is not None
        assert _describe_cause(error) == " | cause=RuntimeError: root"


def test_describe_cause_stops_on_a_cyclic_chain():
    first = RuntimeError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first
    error = PermanentRepositoryIngestionError("outer")
    error.__cause__ = first
    result: list[str] = []

    # Run in a daemon thread so a regression fails the test instead of
    # hanging the suite.
    thread = threading.Thread(
        target=lambda: result.append(_describe_cause(error)), daemon=True
    )
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive(), "_describe_cause did not terminate"
    assert result == [" | cause=ValueError: second"]

def test_describe_cause_honours_from_none():
    try:
        try:
            raise RuntimeError("hidden")
        except RuntimeError:
            raise PermanentRepositoryIngestionError("outer") from None
    except PermanentRepositoryIngestionError as error:
        assert _describe_cause(error) == ""


def test_describe_cause_truncates_long_messages():
    error = PermanentRepositoryIngestionError("outer")
    error.__cause__ = RuntimeError("x" * 1000)

    described = _describe_cause(error)

    assert described == " | cause=RuntimeError: " + "x" * 300


async def test_run_job_logs_cause_of_permanent_failure(caplog):
    caplog.set_level(logging.WARNING, logger="app.worker")
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(12345),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=_raise_wrapped(
                PermanentRepositoryIngestionError(
                    "Database integrity error during ingestion"
                ),
                RuntimeError(_INTEGRITY_CAUSE),
            ),
        ),
    ):
        await run_job(1, WORKER_ID)

    [record] = _job_failure_records(caplog)
    message = record.getMessage()
    assert message.startswith("job failed permanently")
    assert "\n" not in message
    assert "cause=RuntimeError:" in message
    assert '"uq_chunk_identity_gen"' in message
    assert "(docs/jquery.min.js, n, 2) already exists." in message
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], PermanentRepositoryIngestionError)
    mark_failed.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        "Database integrity error during ingestion",
    )


async def test_run_job_logs_cause_of_transient_failure_without_traceback(caplog):
    caplog.set_level(logging.WARNING, logger="app.worker")
    job = _shared_ingest_job(attempts=2)
    session = MagicMock()
    session.get.return_value = job
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(12345),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._requeue_or_fail", requeue),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=_raise_wrapped(
                TransientRepositoryIngestionError(
                    "GitHub request failed with status 502"
                ),
                ConnectionError("502 Bad Gateway"),
            ),
        ),
    ):
        await run_job(1, WORKER_ID)

    [record] = _job_failure_records(caplog)
    message = record.getMessage()
    assert message.startswith("job failed transiently")
    assert message.endswith(
        "GitHub request failed with status 502"
        " | cause=ConnectionError: 502 Bad Gateway"
    )
    assert record.exc_info is None
    requeue.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        attempts=2,
        error="GitHub request failed with status 502",
    )

async def test_run_job_tries_next_sponsor_when_installation_is_invalid():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    result = {"chunks_inserted": 12, "embeddings_created": 12}
    mark_failed = MagicMock(return_value=True)
    guarded_installations = []

    async def ingest(_session, *, installation_id, ensure_owned, **_kwargs):
        with patch("app.worker._ensure_ingestion_owned") as guard:
            ensure_owned(_session)
        guarded_installations.append(guard.call_args.kwargs["installation_id"])
        if installation_id == 111:
            raise SponsorInstallationInvalidError(INVALID_CONNECTION_MESSAGE)
        return result

    with (
        _patch_session(session),
        _patch_sponsors(111, 222),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker._fail_rejected_sponsor_waiters") as fail_waiters,
        patch("app.worker.ingest_repository", side_effect=ingest) as ingest_mock,
    ):
        await run_job(1, WORKER_ID)

    assert [
        call.kwargs["installation_id"] for call in ingest_mock.await_args_list
    ] == [111, 222]
    assert guarded_installations == [111, 222]
    # 111's waiters share the result 222 sponsored.
    fail_waiters.assert_not_called()
    mark_failed.assert_not_called()


async def test_run_job_tries_sponsor_who_joined_after_the_others_were_rejected():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    result = {"chunks_inserted": 12, "embeddings_created": 12}
    mark_failed = MagicMock(return_value=True)
    # 111 is rejected; 222 joins while 111 is being tried.
    sponsor_reads = iter([[111], [111, 222]])

    async def ingest(_session, *, installation_id, **_kwargs):
        if installation_id == 111:
            raise SponsorInstallationInvalidError(INVALID_CONNECTION_MESSAGE)
        return result

    with (
        _patch_session(session),
        patch(
            "app.worker._sponsor_installations",
            side_effect=lambda *_args: next(sponsor_reads),
        ),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker._fail_rejected_sponsor_waiters"),
        patch("app.worker.ingest_repository", side_effect=ingest) as ingest_mock,
    ):
        await run_job(1, WORKER_ID)

    assert [
        call.kwargs["installation_id"] for call in ingest_mock.await_args_list
    ] == [111, 222]
    mark_failed.assert_not_called()


async def test_run_job_releases_shared_ingest_when_every_sponsor_is_rejected():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)
    release = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(111, 222),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker.release_shared_ingest", release),
        patch("app.worker._fail_rejected_sponsor_waiters") as fail_waiters,
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=SponsorInstallationInvalidError(INVALID_CONNECTION_MESSAGE),
        ) as ingest,
    ):
        await run_job(1, WORKER_ID)

    assert ingest.await_count == 2
    assert [call.kwargs for call in fail_waiters.call_args_list] == [
        {"job_id": 1, "installation_id": 111, "error": INVALID_CONNECTION_MESSAGE},
        {"job_id": 1, "installation_id": 222, "error": INVALID_CONNECTION_MESSAGE},
    ]
    # The shared ingest never fails over an installation; it goes back to
    # pending so a later requester can still sponsor it.
    mark_failed.assert_not_called()
    release.assert_called_once_with(session, 1, WORKER_ID)


def test_fail_rejected_sponsor_waiters_targets_only_that_installation():
    session = MagicMock()

    _fail_rejected_sponsor_waiters(
        session,
        job_id=1,
        installation_id=111,
        error=INVALID_CONNECTION_MESSAGE,
    )

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "jobs.blocked_by_job_id = :blocked_by_job_id_1" in compiled
    assert "jobs.installation_id = :installation_id_1" in compiled
    assert "jobs.status IN" in compiled
    params = statement.compile().params
    assert params["blocked_by_job_id_1"] == 1
    assert params["installation_id_1"] == 111
    assert params["status"] == JobStatus.FAILED
    assert params["error"] == INVALID_CONNECTION_MESSAGE
    session.commit.assert_called_once()


async def test_run_job_releases_shared_ingest_nobody_eligible_is_waiting_on():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    release = MagicMock(return_value=True)
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.release_shared_ingest", release),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker.ingest_repository", new_callable=AsyncMock) as ingest,
    ):
        await run_job(1, WORKER_ID)

    ingest.assert_not_awaited()
    release.assert_called_once_with(session, 1, WORKER_ID)
    mark_failed.assert_not_called()


async def test_run_job_releases_cancelled_ingestion_without_other_updates():
    job = _shared_ingest_job()
    session = MagicMock()
    session.get.return_value = job
    release = MagicMock(return_value=True)
    mark_failed = MagicMock(return_value=True)
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
        _patch_sponsors(12345),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.release_shared_ingest", release),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker._requeue_or_fail", requeue),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=IngestionCancelledError("nobody waiting"),
        ),
    ):
        await run_job(1, WORKER_ID)

    release.assert_called_once_with(session, 1, WORKER_ID)
    mark_failed.assert_not_called()
    requeue.assert_not_called()
    session.rollback.assert_called()


def test_release_shared_ingest_refunds_attempt_while_claim_is_held():
    session = MagicMock()
    session.execute.return_value.rowcount = 1

    assert release_shared_ingest(session, 1, WORKER_ID) is True

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile())
    assert "jobs.status = :status_1" in compiled
    assert "jobs.claimed_by = :claimed_by_1" in compiled
    assert "attempts=(jobs.attempts - :attempts_1)" in compiled
    assert statement.compile().params["status"] == JobStatus.PENDING
    session.commit.assert_called_once()


def test_ingestion_guard_locks_waiting_connections_and_checks_claim():
    session = MagicMock()
    session.execute.return_value.first.return_value = (1,)
    session.execute.return_value.scalar_one_or_none.return_value = 1

    _ensure_ingestion_owned(
        session,
        job_id=1,
        worker_id=WORKER_ID,
        installation_id=12345,
        lease_lost=threading.Event(),
    )

    sponsor_call, job_call = session.execute.call_args_list
    sponsor_sql = " ".join(str(sponsor_call.args[0]).split())
    job_sql = " ".join(str(job_call.args[0]).split())
    assert sponsor_call.args[1] == {"job_id": 1, "installation_id": 12345}
    assert "w.blocked_by_job_id = :job_id" in sponsor_sql
    assert "w.status IN ('pending', 'running')" in sponsor_sql
    assert 'c."installationId" = :installation_id' in sponsor_sql
    assert "c.active IS TRUE" in sponsor_sql
    assert "FOR SHARE OF c" in sponsor_sql
    assert "LIMIT" not in sponsor_sql
    assert "j.status = 'running'" in job_sql
    assert "j.claimed_by = :worker_id" in job_sql
    assert "FOR SHARE OF j" in job_sql


def test_ingestion_guard_rejects_missing_or_reclaimed_job():
    session = MagicMock()
    sponsored = MagicMock()
    sponsored.first.return_value = (1,)
    missing_job = MagicMock()
    missing_job.scalar_one_or_none.return_value = None
    session.execute.side_effect = [sponsored, missing_job]

    with pytest.raises(IngestionCancelledError, match="no longer active"):
        _ensure_ingestion_owned(
            session,
            job_id=1,
            worker_id=WORKER_ID,
            installation_id=12345,
            lease_lost=threading.Event(),
        )

    assert session.execute.call_count == 2


def test_ingestion_guard_rejects_when_nobody_eligible_waits_on_installation():
    session = MagicMock()
    session.execute.return_value.first.return_value = None

    with pytest.raises(IngestionCancelledError, match="Nobody eligible"):
        _ensure_ingestion_owned(
            session,
            job_id=1,
            worker_id=WORKER_ID,
            installation_id=12345,
            lease_lost=threading.Event(),
        )

    assert session.execute.call_count == 1


def test_ingestion_guard_rejects_known_lease_loss_without_query():
    session = MagicMock()
    lease_lost = threading.Event()
    lease_lost.set()

    with pytest.raises(IngestionCancelledError, match="lease was lost"):
        _ensure_ingestion_owned(
            session,
            job_id=1,
            worker_id=WORKER_ID,
            installation_id=12345,
            lease_lost=lease_lost,
        )

    session.execute.assert_not_called()


def test_retryable_failure_requeues_before_attempt_limit():
    session = MagicMock()
    with (
        patch("app.worker.settings.worker_max_attempts", 3),
        patch("app.worker._update_owned_job", return_value=True) as update,
    ):
        assert _requeue_or_fail(
            session,
            1,
            WORKER_ID,
            attempts=2,
            error="temporary outage",
        )

    update.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        status=JobStatus.PENDING,
        error="temporary outage",
        claimed_at=None,
        claimed_by=None,
    )


def test_retryable_failure_fails_at_attempt_limit():
    session = MagicMock()
    with (
        patch("app.worker.settings.worker_max_attempts", 3),
        patch("app.worker._update_owned_job", return_value=True) as update,
    ):
        assert _requeue_or_fail(
            session,
            1,
            WORKER_ID,
            attempts=3,
            error="temporary outage",
        )

    update.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        status=JobStatus.FAILED,
        error="Exceeded max attempts: temporary outage",
        claimed_at=None,
        claimed_by=None,
    )


async def test_worker_loop_sleeps_when_idle_and_stops_promptly():
    stop = asyncio.Event()
    claims: list[None] = []

    def _claim(_worker_id: str):
        claims.append(None)
        return None

    async def _should_not_run(_job_id: int):
        raise AssertionError("run_job should not be called when the queue is empty")

    with (
        patch("app.worker._claim_pending", side_effect=_claim),
        patch("app.worker._recover_stale"),
        patch("app.worker.run_job", side_effect=_should_not_run),
    ):
        task = asyncio.create_task(
            worker_loop(stop, poll_interval=0.05, recovery_interval=999)
        )
        await asyncio.sleep(0.16)
        assert len(claims) >= 2
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert task.done()
    assert task.exception() is None
