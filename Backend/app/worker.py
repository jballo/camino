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
import threading
import time
import uuid

from sqlalchemy import exc, text, update
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

RENEW_SQL = text("""
UPDATE tour_jobs
SET claimed_at = now()
WHERE id = :job_id
  AND status = 'generating'
  AND claimed_by = :worker_id
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


def _renew_job_lease(job_id: int, worker_id: str) -> bool:
    """Refresh a claim if this worker still owns it."""
    with Session(engine) as session:
        result = session.execute(
            RENEW_SQL,
            {"job_id": job_id, "worker_id": worker_id},
        )
        session.commit()
        return result.rowcount == 1


def _heartbeat_job_lease(
    job_id: int,
    worker_id: str,
    *,
    interval: float,
    stop_event: threading.Event,
    lease_lost: threading.Event,
) -> None:
    """Renew a job lease until generation finishes or ownership is lost."""
    while not stop_event.wait(interval):
        try:
            renewed = _renew_job_lease(job_id, worker_id)
        except Exception:
            # A transient database failure should not kill generation. Recovery
            # cannot safely duplicate the final write because outcome updates
            # below also verify ownership.
            logger.exception(
                "tour job lease renewal failed | id=%s worker=%s",
                job_id,
                worker_id,
            )
            continue
        if not renewed:
            lease_lost.set()
            logger.warning(
                "tour job lease ownership lost | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return


def _update_owned_job(
    session: Session,
    job_id: int,
    worker_id: str,
    **values: object,
) -> bool:
    """Update a generating job only while ``worker_id`` owns its lease."""
    result = session.execute(
        update(TourJob)
        .where(
            TourJob.id == job_id,
            TourJob.status == TourJobStatus.GENERATING,
            TourJob.claimed_by == worker_id,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True


def _mark_failed(
    session: Session,
    job_id: int,
    worker_id: str,
    error: str,
) -> bool:
    try:
        updated = _update_owned_job(
            session,
            job_id,
            worker_id,
            status=TourJobStatus.FAILED,
            error=error,
        )
    except exc.SQLAlchemyError:
        logger.exception(
            "failed to mark tour job as FAILED | id=%s", job_id
        )
        session.rollback()
        return False
    if not updated:
        logger.warning(
            "discarded tour job failure after lease ownership changed | id=%s worker=%s",
            job_id,
            worker_id,
        )
    return updated


async def run_job(job_id: int, worker_id: str) -> None:
    """Generate a tour and persist the outcome. Never raises to the caller."""
    with Session(engine) as session:
        job = session.get(TourJob, job_id)
        if job is None:
            logger.error("tour job vanished before generation | id=%s", job_id)
            return

        if (
            job.status != TourJobStatus.GENERATING
            or job.claimed_by != worker_id
        ):
            logger.warning(
                "skipping tour job not owned by worker | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return

        topic, repo_name, installation_id = job.topic, job.repo_name, job.installation_id

        try:
            owns_lease = _renew_job_lease(job_id, worker_id)
        except Exception:
            logger.exception(
                "could not establish tour job lease heartbeat | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return
        if not owns_lease:
            logger.warning(
                "tour job lease changed before generation | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return

        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_job_lease,
            kwargs={
                "job_id": job_id,
                "worker_id": worker_id,
                "interval": settings.worker_lease_timeout / 3,
                "stop_event": heartbeat_stop,
                "lease_lost": lease_lost,
            },
            name=f"tour-job-{job_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()

        generation_error: str | None = None
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
            generation_error = str(e)
        except Exception:
            logger.exception(
                "tour generation crashed | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            generation_error = "Internal generation error"
        finally:
            heartbeat_stop.set()
            heartbeat.join()

        if generation_error is not None:
            _mark_failed(session, job_id, worker_id, generation_error)
            return

        if lease_lost.is_set():
            session.rollback()
            logger.warning(
                "discarded generated tour after lease ownership changed | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return

        try:
            persisted = _update_owned_job(
                session,
                job_id,
                worker_id,
                status=TourJobStatus.COMPLETE,
                artifact=artifact.model_dump(),
                error=None,
            )
        except exc.SQLAlchemyError:
            logger.exception(
                "tour job commit failed after successful generation | id=%s topic=%r repo=%r",
                job_id, topic, repo_name,
            )
            session.rollback()
            _mark_failed(
                session,
                job_id,
                worker_id,
                "Failed to persist generated tour",
            )
            return
        if not persisted:
            logger.warning(
                "discarded generated tour after lease ownership changed | id=%s worker=%s",
                job_id,
                worker_id,
            )
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
            await run_job(job_id, identity)
        except Exception:
            logger.exception("run_job leaked an exception | id=%s", job_id)
            try:
                with Session(engine) as session:
                    _mark_failed(
                        session,
                        job_id,
                        identity,
                        "Internal generation error",
                    )
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
