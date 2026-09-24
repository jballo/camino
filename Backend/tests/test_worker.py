import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import exc

from app.models.job import JobStatus, JobType
from app.brief import BriefNeedsRefreshError
from app.models.tour import TourArtifact, TourStep
from app.services.staleness import ChangedFile, HeadComparison
from app.services.repository_ingestion import (
    IngestionCancelledError,
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
)
from app.tour import TourGenerationCancelledError, TourGenerationError
from app.worker import (
    _run_standalone,
    _ensure_ingestion_owned,
    _requeue_or_fail,
    _stage_owned_ingestion_completion,
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
    job.job_type = JobType.TOUR
    job.status = JobStatus.RUNNING
    job.claimed_by = WORKER_ID
    job.attempts = 1
    job.artifact = None
    job.error = None
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


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


async def test_issue_brief_parks_behind_refresh_without_spending_retry():
    job = _job(
        job_type=JobType.ISSUE_BRIEF,
        issue_repo="org/repo",
        issue_number=44,
        refresh_cycles=0,
        userId="user_1",
        topic="Fix refresh races",
    )
    session = MagicMock()
    session.get.return_value = job
    dependency = MagicMock(id=17)
    park = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker.fetch_issue_thread", return_value=MagicMock(branch_instruction=None)),
        patch("app.worker.resolve_target_branch", return_value=MagicMock(branch="main", default_branch="main")),
        patch("app.worker.resolve_fork_status", return_value=MagicMock()),
        patch("app.worker.generate_brief", new_callable=AsyncMock, side_effect=BriefNeedsRefreshError("stale")),
        patch("app.worker.enqueue_job", return_value=(dependency, True)) as enqueue,
        patch("app.worker.park_job", park),
        patch("app.worker._update_owned_job") as persist,
    ):
        await run_job(1, WORKER_ID)

    assert enqueue.call_args.kwargs["job_type"] == JobType.REPOSITORY_INGEST
    park.assert_called_once_with(
        session, 1, WORKER_ID, blocked_by_job_id=17
    )
    persist.assert_not_called()


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
    job = _job(job_type=JobType.REPOSITORY_INGEST, topic=None)
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
    job = _job(job_type=JobType.REPOSITORY_INGEST, topic=None, attempts=2)
    session = MagicMock()
    session.get.return_value = job
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
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
    job = _job(job_type=JobType.REPOSITORY_INGEST, topic=None)
    session = MagicMock()
    session.get.return_value = job
    mark_failed = MagicMock(return_value=True)

    with (
        _patch_session(session),
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


async def test_run_job_discards_cancelled_ingestion_without_updating_job():
    job = _job(job_type=JobType.REPOSITORY_INGEST, topic=None)
    session = MagicMock()
    session.get.return_value = job
    persist = MagicMock(return_value=True)
    mark_failed = MagicMock(return_value=True)
    requeue = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch("app.worker._mark_failed", mark_failed),
        patch("app.worker._requeue_or_fail", requeue),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            side_effect=IngestionCancelledError("lease lost"),
        ),
    ):
        await run_job(1, WORKER_ID)

    persist.assert_not_called()
    mark_failed.assert_not_called()
    requeue.assert_not_called()
    session.rollback.assert_called()


def test_ingestion_ownership_guard_locks_owned_job_and_checks_installation():
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = 1

    _ensure_ingestion_owned(
        session,
        job_id=1,
        worker_id=WORKER_ID,
        installation_id=12345,
        lease_lost=threading.Event(),
    )

    installation_sql = " ".join(
        str(session.execute.call_args_list[0].args[0]).split()
    )
    job_sql = " ".join(str(session.execute.call_args_list[1].args[0]).split())
    assert "j.status = 'running'" in job_sql
    assert "j.claimed_by = :worker_id" in job_sql
    assert "FOR SHARE OF j" in job_sql
    assert "FROM githubconnections" in installation_sql
    assert '"installationId" = :installation_id' in installation_sql
    assert "FOR SHARE" in installation_sql


def test_ingestion_ownership_guard_rejects_missing_or_reclaimed_job():
    session = MagicMock()
    existing_installation = MagicMock()
    existing_installation.scalar_one_or_none.return_value = 1
    missing_job = MagicMock()
    missing_job.scalar_one_or_none.return_value = None
    session.execute.side_effect = [existing_installation, missing_job]

    with pytest.raises(IngestionCancelledError, match="no longer active"):
        _ensure_ingestion_owned(
            session,
            job_id=1,
            worker_id=WORKER_ID,
            installation_id=12345,
            lease_lost=threading.Event(),
        )

    assert session.execute.call_count == 2


def test_ingestion_ownership_guard_rejects_missing_installation():
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = None

    with pytest.raises(
        IngestionCancelledError,
        match="installation is no longer active",
    ):
        _ensure_ingestion_owned(
            session,
            job_id=1,
            worker_id=WORKER_ID,
            installation_id=12345,
            lease_lost=threading.Event(),
        )

    assert session.execute.call_count == 1


def test_ingestion_ownership_guard_rejects_known_lease_loss_without_query():
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
