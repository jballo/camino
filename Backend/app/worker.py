"""Durable database job queue with atomic claims and stale-lease recovery.

Pending jobs live in Postgres (`jobs`). A polling loop claims the oldest
pending row with ``FOR UPDATE SKIP LOCKED`` so multiple API processes (or a
dedicated ``python -m app.worker``) cannot double-run the same job.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import socket
import threading
import time
import uuid

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from requests.exceptions import RequestException
from sqlalchemy import exc, text, update
from sqlmodel import Session, select

from app.brief import (
    BriefGenerationCancelledError,
    BriefGenerationError,
    BriefNeedsRefreshError,
    generate_brief,
)
from app.config import settings
from app.db import engine
from app.models.job import Job, JobStatus, JobType
from app.models.code import RepoIndexState
from app.models.tour import TourArtifact, TourFreshness
from app.services.fork_status import resolve_fork_status
from app.services.issue_thread import IssueThreadError, fetch_issue_thread
from app.services.jobs import enqueue_job, repository_ingest_dedupe_key
from app.services.repository_ingestion import (
    IngestionCancelledError,
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
    ingest_repository,
)
from app.services.staleness import compare_to_head
from app.services.target_branch import TargetBranchResolution, resolve_target_branch
from app.tour import (
    TourGenerationCancelledError,
    TourGenerationError,
    generate_tour,
)

logger = logging.getLogger(__name__)

WORKER_RECOVERY_INTERVAL = 60.0
WORKER_SHUTDOWN_TIMEOUT = 10.0

CLAIM_SQL = text("""
UPDATE jobs AS candidate
SET status = 'running',
    claimed_at = now(),
    claimed_by = :worker_id,
    attempts = attempts + 1,
    error = NULL
WHERE candidate.id = (
    SELECT j.id FROM jobs AS j
    WHERE j.status = 'pending'
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS dependency
          WHERE dependency.id = j.blocked_by_job_id
            AND dependency.status IN ('pending', 'running')
      )
    ORDER BY j."createdAt"
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id
""")

RECOVER_SQL = text("""
UPDATE jobs
SET status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'pending' END,
    error  = CASE WHEN attempts >= :max_attempts THEN 'Exceeded max attempts' ELSE error END,
    claimed_at = NULL,
    claimed_by = NULL
WHERE status = 'running'
  AND (
      claimed_at IS NULL
      OR claimed_at < now() - (:lease_timeout_seconds * INTERVAL '1 second')
  )
""")

RENEW_SQL = text("""
UPDATE jobs
SET claimed_at = now()
WHERE id = :job_id
  AND status = 'running'
  AND claimed_by = :worker_id
""")

ENSURE_INGESTION_OWNED_SQL = text("""
SELECT 1
FROM jobs AS j
WHERE j.id = :job_id
  AND j.status = 'running'
  AND j.claimed_by = :worker_id
FOR SHARE OF j
""")

LOCK_INSTALLATION_CONNECTION_SQL = text("""
SELECT 1
FROM githubconnections
WHERE "installationId" = :installation_id
ORDER BY id
LIMIT 1
FOR SHARE
""")


def _make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def claim_next_job(session: Session, worker_id: str) -> int | None:
    """Atomically claim the oldest unblocked job. Commits before returning."""
    while True:
        result = session.execute(CLAIM_SQL, {"worker_id": worker_id})
        job_id = result.scalar_one_or_none()
        if job_id is None:
            session.commit()
            return None
        job = session.get(Job, job_id)
        dependency = (
            session.get(Job, job.blocked_by_job_id)
            if job is not None and job.blocked_by_job_id is not None
            else None
        )
        if dependency is not None and dependency.status in (
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            reason = dependency.error or dependency.status
            job.status = JobStatus.FAILED
            job.error = f"ingest failed: {reason}"
            job.claimed_at = None
            job.claimed_by = None
            session.add(job)
            session.commit()
            logger.warning(
                "failed dependent job | id=%s dependency=%s", job_id, dependency.id
            )
            continue
        session.commit()
        logger.info("claimed job | id=%s worker=%s", job_id, worker_id)
        return job_id


def park_job(
    session: Session,
    job_id: int,
    worker_id: str,
    *,
    blocked_by_job_id: int,
) -> bool:
    """Park owned work behind a dependency without consuming a retry attempt."""
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.RUNNING,
            Job.claimed_by == worker_id,
        )
        .values(
            status=JobStatus.PENDING,
            blocked_by_job_id=blocked_by_job_id,
            claimed_at=None,
            claimed_by=None,
            attempts=Job.attempts - 1,
            refresh_cycles=Job.refresh_cycles + 1,
            error=None,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True


def recover_stale_jobs(
    session: Session,
    *,
    lease_timeout_seconds: float,
    max_attempts: int,
) -> int:
    """Requeue or fail running jobs whose lease has expired. Commits before returning."""
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
        logger.warning("recovered %d stale job(s)", recovered)
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


def _ensure_ingestion_owned(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    installation_id: int,
    lease_lost: threading.Event,
) -> None:
    """Lock and verify the job claim before an ingestion transaction commits."""
    if lease_lost.is_set():
        raise IngestionCancelledError("Ingestion job lease was lost")

    installation_exists = session.execute(
        LOCK_INSTALLATION_CONNECTION_SQL,
        {"installation_id": installation_id},
    ).scalar_one_or_none()
    if installation_exists is None:
        raise IngestionCancelledError(
            "GitHub installation is no longer active"
        )

    owns_job = session.execute(
        ENSURE_INGESTION_OWNED_SQL,
        {
            "job_id": job_id,
            "worker_id": worker_id,
        },
    ).scalar_one_or_none()
    if owns_job is None:
        raise IngestionCancelledError("Ingestion job is no longer active")


def _stage_owned_ingestion_completion(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    artifact: dict[str, int],
) -> None:
    """Stage job completion without committing the publication transaction."""
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.RUNNING,
            Job.claimed_by == worker_id,
        )
        .values(
            status=JobStatus.COMPLETE,
            artifact=artifact,
            error=None,
            claimed_at=None,
            claimed_by=None,
        )
    )
    if result.rowcount != 1:
        raise IngestionCancelledError("Ingestion job is no longer active")


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
                "job lease renewal failed | id=%s worker=%s",
                job_id,
                worker_id,
            )
            continue
        if not renewed:
            lease_lost.set()
            logger.warning(
                "job lease ownership lost | id=%s worker=%s",
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
    """Update a running job only while ``worker_id`` owns its lease."""
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.RUNNING,
            Job.claimed_by == worker_id,
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
            status=JobStatus.FAILED,
            error=error,
            claimed_at=None,
            claimed_by=None,
        )
    except exc.SQLAlchemyError:
        logger.exception(
            "failed to mark job as FAILED | id=%s", job_id
        )
        session.rollback()
        return False
    if not updated:
        logger.warning(
            "discarded job failure after lease ownership changed | id=%s worker=%s",
            job_id,
            worker_id,
        )
    return updated


def _requeue_or_fail(
    session: Session,
    job_id: int,
    worker_id: str,
    *,
    attempts: int,
    error: str,
) -> bool:
    """Requeue a transient failure unless this claim exhausted the retry budget."""
    exhausted = attempts >= settings.worker_max_attempts
    try:
        updated = _update_owned_job(
            session,
            job_id,
            worker_id,
            status=JobStatus.FAILED if exhausted else JobStatus.PENDING,
            error=(
                f"Exceeded max attempts: {error}"
                if exhausted
                else error
            ),
            claimed_at=None,
            claimed_by=None,
        )
    except exc.SQLAlchemyError:
        logger.exception("failed to requeue transient job | id=%s", job_id)
        session.rollback()
        return False

    if updated:
        logger.warning(
            "%s transient job | id=%s attempt=%d/%d error=%s",
            "failed" if exhausted else "requeued",
            job_id,
            attempts,
            settings.worker_max_attempts,
            error,
        )
    else:
        logger.warning(
            "discarded transient job outcome after lease ownership changed "
            "| id=%s worker=%s",
            job_id,
            worker_id,
        )
    return updated


_TRANSIENT_JOB_ERRORS = (
    RequestException,
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    exc.OperationalError,
    TransientRepositoryIngestionError,
)


async def _stamp_tour_freshness(
    session: Session,
    artifact: TourArtifact,
    *,
    repo_name: str,
    installation_id: int,
    ref: str,
) -> None:
    """Attach a best-effort generation-time freshness disclosure."""
    try:
        state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == repo_name,
                RepoIndexState.ref == ref,
            )
        ).one_or_none()
    except exc.SQLAlchemyError:
        session.rollback()
        artifact.freshness = None
        logger.exception(
            "tour freshness check failed | repo=%r reason=index_state_read",
            repo_name,
        )
        return

    try:
        indexed_sha = state.indexed_sha if state is not None else None
        if not isinstance(indexed_sha, str) or not indexed_sha:
            logger.info(
                "tour freshness unknown | repo=%r reason=missing_indexed_sha",
                repo_name,
            )
            return

        comparison = await asyncio.to_thread(
            compare_to_head,
            repo_name,
            installation_id,
            indexed_sha,
            ref,
        )
        cited_paths = {step.file_path.lstrip("./") for step in artifact.steps}
        changed_cited_files = sorted(
            {
                changed.path
                for changed in comparison.changed_files
                if changed.path.lstrip("./") in cited_paths
            }
        )
        artifact.freshness = TourFreshness(
            indexed_sha=indexed_sha,
            head_sha=comparison.head_sha,
            commits_behind=comparison.commits_behind,
            measurable=comparison.measurable,
            changed_cited_files=changed_cited_files,
            checked_at=dt.datetime.now(dt.UTC),
        )
    except Exception:
        # Freshness is disclosure metadata, never a reason to retry an otherwise
        # valid and expensive tour generation.
        artifact.freshness = None
        logger.exception("tour freshness check failed | repo=%r", repo_name)


async def run_job(job_id: int, worker_id: str) -> None:
    """Dispatch one claimed job and persist its outcome. Never raises."""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            logger.error("job vanished before execution | id=%s", job_id)
            return

        if job.status != JobStatus.RUNNING or job.claimed_by != worker_id:
            logger.warning(
                "skipping job not owned by worker | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return

        job_type = job.job_type
        topic = job.topic
        repo_name = job.repo_name
        installation_id = job.installation_id
        ref = job.ref
        attempts = job.attempts
        issue_repo = job.issue_repo
        issue_number = job.issue_number
        refresh_cycles = job.refresh_cycles

        try:
            owns_lease = _renew_job_lease(job_id, worker_id)
        except Exception:
            logger.exception(
                "could not establish job lease heartbeat | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return
        if not owns_lease:
            logger.warning(
                "job lease changed before execution | id=%s worker=%s",
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
            name=f"job-{job_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()

        result: dict | None = None
        transient_error: str | None = None
        permanent_error: str | None = None
        job_cancelled = False
        ingestion_completed = False
        job_parked = False
        try:
            if ref is None:
                raise PermanentRepositoryIngestionError(
                    "Legacy job is missing its repository ref"
                )
            if job_type == JobType.TOUR:
                if topic is None:
                    raise TourGenerationError("Tour job is missing its topic")
                artifact = await generate_tour(
                    session,
                    topic=topic,
                    repo_name=repo_name,
                    ref=ref,
                    cancel_event=lease_lost,
                )
                await _stamp_tour_freshness(
                    session,
                    artifact,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    ref=ref,
                )
                result = artifact.model_dump(mode="json")
            elif job_type == JobType.ISSUE_BRIEF:
                if issue_repo is None:
                    raise BriefGenerationError(
                        "Issue brief job is missing its issue repository"
                    )
                if topic is None or issue_number is None:
                    raise BriefGenerationError(
                        "Issue brief job is missing its issue metadata"
                    )
                issue = await asyncio.to_thread(
                    fetch_issue_thread,
                    issue_repo,
                    issue_number,
                    installation_id,
                )
                resolved = await asyncio.to_thread(
                    resolve_target_branch,
                    repo_name,
                    installation_id,
                )
                if (
                    issue.branch_instruction is not None
                    and issue.branch_instruction.branch == ref
                ):
                    target_resolution = TargetBranchResolution(
                        branch=ref,
                        source="issue_thread",
                        evidence=issue.branch_instruction.evidence,
                        evidence_path=None,
                        default_branch=resolved.default_branch,
                        checked_at=dt.datetime.now(dt.UTC),
                    )
                elif resolved.branch == ref:
                    target_resolution = resolved
                else:
                    target_resolution = TargetBranchResolution(
                        branch=ref,
                        source="user_override",
                        evidence="Target branch selected by the requester",
                        evidence_path=None,
                        default_branch=resolved.default_branch,
                        checked_at=dt.datetime.now(dt.UTC),
                    )
                fork_status = await asyncio.to_thread(
                    resolve_fork_status,
                    issue_repo,
                    installation_id,
                    ref,
                )
                try:
                    artifact = await generate_brief(
                        session,
                        issue=issue,
                        repo_name=repo_name,
                        ref=ref,
                        installation_id=installation_id,
                        target_resolution=target_resolution,
                        fork_status=fork_status,
                        allow_stale=refresh_cycles >= 2,
                        cancel_event=lease_lost,
                    )
                    result = artifact.model_dump(mode="json")
                except BriefNeedsRefreshError:
                    session.rollback()
                    ingest_job, _ = enqueue_job(
                        session,
                        user_id=job.userId,
                        installation_id=installation_id,
                        repo_name=repo_name,
                        ref=ref,
                        job_type=JobType.REPOSITORY_INGEST,
                        dedupe_key=repository_ingest_dedupe_key(
                            repo_name=repo_name, ref=ref
                        ),
                    )
                    job_parked = park_job(
                        session,
                        job_id,
                        worker_id,
                        blocked_by_job_id=ingest_job.id,
                    )
            elif job_type == JobType.REPOSITORY_INGEST:
                def ensure_ingestion_owned(guard_session: Session) -> None:
                    _ensure_ingestion_owned(
                        guard_session,
                        job_id=job_id,
                        worker_id=worker_id,
                        installation_id=installation_id,
                        lease_lost=lease_lost,
                    )

                def finalize_ingestion_publication(
                    publication_session: Session,
                    artifact: dict[str, int],
                ) -> None:
                    _stage_owned_ingestion_completion(
                        publication_session,
                        job_id=job_id,
                        worker_id=worker_id,
                        artifact=artifact,
                    )

                result = await ingest_repository(
                    session,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    ref=ref,
                    ensure_owned=ensure_ingestion_owned,
                    finalize_publication=finalize_ingestion_publication,
                )
                ingestion_completed = True
            else:
                permanent_error = f"Unsupported job type: {job_type}"
        except (
            BriefGenerationCancelledError,
            IngestionCancelledError,
            TourGenerationCancelledError,
        ) as error:
            logger.warning(
                "job cancelled | id=%s type=%s repo=%r: %s",
                job_id,
                job_type,
                repo_name,
                error,
            )
            session.rollback()
            job_cancelled = True
        except (
            BriefGenerationError,
            IssueThreadError,
            TourGenerationError,
            PermanentRepositoryIngestionError,
        ) as error:
            logger.warning(
                "job failed permanently | id=%s type=%s repo=%r: %s",
                job_id,
                job_type,
                repo_name,
                error,
            )
            session.rollback()
            permanent_error = str(error)
        except _TRANSIENT_JOB_ERRORS as error:
            logger.warning(
                "job failed transiently | id=%s type=%s repo=%r: %s",
                job_id,
                job_type,
                repo_name,
                error,
            )
            session.rollback()
            transient_error = str(error) or type(error).__name__
        except Exception as error:
            logger.exception(
                "job crashed | id=%s type=%s repo=%r",
                job_id,
                job_type,
                repo_name,
            )
            session.rollback()
            permanent_error = f"Internal {job_type} error"
        finally:
            heartbeat_stop.set()
            heartbeat.join()

        if ingestion_completed:
            logger.info(
                "job complete | id=%s type=%s repo=%r",
                job_id,
                job_type,
                repo_name,
            )
            return

        if job_parked:
            logger.info(
                "job parked for repository refresh | id=%s repo=%r", job_id, repo_name
            )
            return

        if job_cancelled or lease_lost.is_set():
            session.rollback()
            logger.warning(
                "discarded job outcome after cancellation or lease ownership "
                "change | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return

        if transient_error is not None:
            _requeue_or_fail(
                session,
                job_id,
                worker_id,
                attempts=attempts,
                error=transient_error,
            )
            return
        if permanent_error is not None:
            _mark_failed(session, job_id, worker_id, permanent_error)
            return
        if result is None:
            _mark_failed(session, job_id, worker_id, "Job produced no result")
            return

        try:
            persisted = _update_owned_job(
                session,
                job_id,
                worker_id,
                status=JobStatus.COMPLETE,
                artifact=result,
                error=None,
                claimed_at=None,
                claimed_by=None,
            )
        except exc.SQLAlchemyError as error:
            logger.exception(
                "job result commit failed | id=%s type=%s repo=%r",
                job_id,
                job_type,
                repo_name,
            )
            session.rollback()
            _requeue_or_fail(
                session,
                job_id,
                worker_id,
                attempts=attempts,
                error=f"Failed to persist job result: {error}",
            )
            return
        if not persisted:
            logger.warning(
                "discarded job result after lease ownership changed | id=%s worker=%s",
                job_id,
                worker_id,
            )
            return
        logger.info(
            "job complete | id=%s type=%s repo=%r",
            job_id,
            job_type,
            repo_name,
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
    logger.info("job worker started | worker=%s", identity)

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
                        "Internal job error",
                    )
            except Exception:
                logger.exception(
                    "failed to mark leaked job as FAILED | id=%s", job_id
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
