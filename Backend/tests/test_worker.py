import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import exc

from app.models.job import JobStatus, JobType
from app.models.tour import TourArtifact, TourStep
from app.services.repository_ingestion import (
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
)
from app.tour import TourGenerationError
from app.worker import _requeue_or_fail, run_job, worker_loop

WORKER_ID = "test-host:1:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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


async def test_run_job_dispatches_repository_ingestion():
    job = _job(job_type=JobType.REPOSITORY_INGEST, topic=None)
    session = MagicMock()
    session.get.return_value = job
    result = {"chunks_inserted": 12, "embeddings_created": 12}
    persist = MagicMock(return_value=True)

    with (
        _patch_session(session),
        patch("app.worker._renew_job_lease", return_value=True),
        patch("app.worker._update_owned_job", persist),
        patch(
            "app.worker.ingest_repository",
            new_callable=AsyncMock,
            return_value=result,
        ) as ingest,
        patch("app.worker.generate_tour", new_callable=AsyncMock) as generate,
    ):
        await run_job(1, WORKER_ID)

    ingest.assert_awaited_once_with(
        session,
        repo_name="org/repo",
        installation_id=12345,
    )
    generate.assert_not_awaited()
    persist.assert_called_once_with(
        session,
        1,
        WORKER_ID,
        status=JobStatus.COMPLETE,
        artifact=result,
        error=None,
        claimed_at=None,
        claimed_by=None,
    )


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
