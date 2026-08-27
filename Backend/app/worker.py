"""Durable tour-job queue: atomic claim, generation, and stale-lease recovery.

Pending jobs live in Postgres (`tour_jobs`). A polling loop claims the oldest
pending row with ``FOR UPDATE SKIP LOCKED`` so multiple API processes (or a
dedicated ``python -m app.worker``) cannot double-run the same job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid

from sqlalchemy import exc, text
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models.tour_job import TourJob, TourJobStatus
from app.tour import TourGenerationError, generate_tour

logger = logging.getLogger(__name__)

WORKER_RECOVERY_INTERVAL = 60.0
WORKER_SHUTDOWN_TIMEOUT = 10.0

CLAIM_SQL = text("""
UPDATE tour_jobs
SET status = 'generating',
    claimed_at = now(),
    claimed_by = :worker_id,
    attempts = attempts + 1
WHERE id = (
    SELECT id FROM tour_jobs
    WHERE status = 'pending'
    ORDER BY "createdAt"
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id
""")

RECOVER_SQL = text("""
UPDATE tour_jobs
SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'pending' END,
    error  = CASE WHEN attempts >= :max_attempts THEN 'Exceeded max attempts' ELSE error END,
    claimed_at = NULL,
    claimed_by = NULL
WHERE status = 'generating'
  AND claimed_at < now() - (:lease_timeout_seconds * INTERVAL '1 second')
""")


def _make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def claim_next_job(session: Session, worker_id: str) -> int | None:
    """Atomically claim the oldest pending job. Commits before returning."""
    result = session.execute(CLAIM_SQL, {"worker_id": worker_id})
    job_id = result.scalar_one_or_none()
    session.commit()
    if job_id is not None:
        logger.info("claimed tour job | id=%s worker=%s", job_id, worker_id)
    return job_id


def recover_stale_jobs(
    session: Session,
    *,
    lease_timeout_seconds: float,
    max_attempts: int,
) -> int:
    """Requeue or fail generating jobs whose lease has expired. Commits before returning."""
    result = session.execute(
        RECOVER_SQL,
        {
            "max_attempts": max_attempts,
            "lease_timeout_seconds": lease_timeout_seconds,
        },
    )
    session.commit()
    recovered = result.rowcount or 0
    if recovered:
        logger.warning("recovered %d stale tour job(s)", recovered)
    return recovered


def _claim_pending(worker_id: str) -> int | None:
    with Session(engine) as session:
        return claim_next_job(session, worker_id)


def _recover_stale() -> None:
    with Session(engine) as session:
        recover_stale_jobs(
            session,
            lease_timeout_seconds=settings.worker_lease_timeout,
            max_attempts=settings.worker_max_attempts,
        )


def _mark_failed(session: Session, job_id: int, error: str) -> None:
    try:
        job = session.get(TourJob, job_id)
        if job is None:
            return
        job.status = TourJobStatus.FAILED
        job.error = error
        session.add(job)
        session.commit()
    except exc.SQLAlchemyError:
        logger.exception(
            "failed to mark tour job as FAILED | id=%s", job_id
        )
        session.rollback()


async def run_job(job_id: int) -> None:
    """Generate a tour and persist the outcome. Never raises to the caller."""
    with Session(engine) as session:
        job = session.get(TourJob, job_id)
        if job is None:
            logger.error("tour job vanished before generation | id=%s", job_id)
            return

        topic, repo_name, installation_id = job.topic, job.repo_name, job.installation_id

        try:
            artifact = await generate_tour(
                session,
                topic=topic,
                repo_name=repo_name,
                installation_id=installation_id,
            )
        except TourGenerationError as e:
            logger.warning(
                "tour generation failed | id=%s topic=%r repo=%r: %s",
                job_id, topic, repo_name, e,
            )
            session.rollback()
            _mark_failed(session, job_id, str(e))
            return
        except Exception:
            logger.exception(
                "tour generation crashed | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            _mark_failed(session, job_id, "Internal generation error")
            return

        job = session.get(TourJob, job_id)
        if job is None:
            logger.error("tour job vanished after generation | id=%s", job_id)
            return
        job.status = TourJobStatus.COMPLETE
        job.artifact = artifact.model_dump()
        job.error = None
        session.add(job)
        try:
            session.commit()
        except exc.SQLAlchemyError:
            logger.exception(
                "tour job commit failed after successful generation | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            _mark_failed(session, job_id, "Failed to persist generated tour")
            return
        logger.info(
            "tour generation complete | id=%s topic=%r steps=%d",
            job_id, topic, len(artifact.steps),
        )


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


async def worker_loop(
    stop_event: asyncio.Event,
    *,
    worker_id: str | None = None,
    poll_interval: float | None = None,
    recovery_interval: float | None = None,
) -> None:
    """Poll for pending jobs until ``stop_event`` is set.

    Claim and recovery use short-lived sessions and commit immediately. Any
    exception from a single job is logged (and marked failed if it leaked
    past ``run_job``); the loop keeps running.
    """
    identity = worker_id or _make_worker_id()
    interval = settings.worker_poll_interval if poll_interval is None else poll_interval
    recover_every = (
        WORKER_RECOVERY_INTERVAL if recovery_interval is None else recovery_interval
    )
    last_recover = 0.0
    logger.info("tour worker started | worker=%s", identity)

    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_recover >= recover_every:
            try:
                _recover_stale()
            except Exception:
                logger.exception("stale job recovery failed")
            last_recover = now

        try:
            job_id = _claim_pending(identity)
        except Exception:
            logger.exception("claim_next_job failed")
            await _sleep_or_stop(stop_event, interval)
            continue

        if job_id is None:
            await _sleep_or_stop(stop_event, interval)
            continue

        try:
            await run_job(job_id)
        except Exception:
            logger.exception("run_job leaked an exception | id=%s", job_id)
            try:
                with Session(engine) as session:
                    _mark_failed(session, job_id, "Internal generation error")
            except Exception:
                logger.exception(
                    "failed to mark leaked tour job as FAILED | id=%s", job_id
                )


async def _run_standalone() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await worker_loop(stop_event)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_standalone())


if __name__ == "__main__":
    main()
