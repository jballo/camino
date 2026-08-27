import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import exc

from app.models.tour import TourArtifact, TourStep
from app.models.tour_job import TourJobStatus
from app.tour import TourGenerationError
from app.worker import run_job, worker_loop


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
    job.status = TourJobStatus.GENERATING
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

    with (
        _patch_session(session),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=artifact),
    ):
        await run_job(1)

    assert job.status == TourJobStatus.COMPLETE
    assert job.artifact == artifact.model_dump()
    assert job.error is None
    session.commit.assert_called()


async def test_run_job_tour_generation_error_marks_failed():
    job = _job()
    session = MagicMock()
    session.get.return_value = job

    with (
        _patch_session(session),
        patch(
            "app.worker.generate_tour",
            new_callable=AsyncMock,
            side_effect=TourGenerationError("no grounded steps"),
        ),
    ):
        await run_job(1)

    assert job.status == TourJobStatus.FAILED
    assert job.error == "no grounded steps"
    session.commit.assert_called()


async def test_run_job_unexpected_exception_does_not_propagate():
    job = _job()
    session = MagicMock()
    session.get.return_value = job

    with (
        _patch_session(session),
        patch(
            "app.worker.generate_tour",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        await run_job(1)

    assert job.status == TourJobStatus.FAILED
    assert job.error == "Internal generation error"


async def test_run_job_commit_failure_falls_back_to_failed():
    job = _job()
    session = MagicMock()
    session.get.return_value = job
    session.commit.side_effect = [exc.SQLAlchemyError("persist failed"), None]

    with (
        _patch_session(session),
        patch("app.worker.generate_tour", new_callable=AsyncMock, return_value=_artifact()),
    ):
        await run_job(1)

    assert job.status == TourJobStatus.FAILED
    assert job.error == "Failed to persist generated tour"


async def test_run_job_missing_row_returns_without_crashing():
    session = MagicMock()
    session.get.return_value = None
    generate = AsyncMock()

    with (
        _patch_session(session),
        patch("app.worker.generate_tour", generate),
    ):
        await run_job(99)

    generate.assert_not_called()


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
