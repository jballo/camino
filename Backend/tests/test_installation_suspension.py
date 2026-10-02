import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
import pytest
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.installation_state import (
    SUSPENSION_ERROR,
    set_installation_active,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    GithubConnections.__table__.create(engine)
    Job.__table__.create(engine)
    return engine


def _connection(
    user_id: str,
    github_user_id: int,
    installation_id: int,
) -> GithubConnections:
    return GithubConnections(
        userId=user_id,
        githubUsername=f"github-{user_id}",
        githubUserId=github_user_id,
        installationId=installation_id,
    )


SHARED_KEY = "repository_ingest:org/repo:main"


def _job(
    user_id: str | None,
    installation_id: int | None,
    *,
    status: str,
    job_type: str = JobType.ISSUE_BRIEF,
    dedupe_key: str | None = None,
    blocked_by_job_id: int | None = None,
    created_at: dt.datetime | None = None,
) -> Job:
    timestamp = created_at or dt.datetime.now(dt.timezone.utc)
    return Job(
        userId=user_id,
        installation_id=installation_id,
        repo_name="org/repo",
        ref="main",
        job_type=job_type,
        dedupe_key=dedupe_key,
        status=status,
        blocked_by_job_id=blocked_by_job_id,
        claimed_at=(timestamp if status == JobStatus.RUNNING else None),
        claimed_by="worker" if status == JobStatus.RUNNING else None,
        createdAt=timestamp,
        updatedAt=timestamp,
    )


def _waiting_jobs(
    user_id: str,
    installation_id: int,
    shared_id: int,
) -> list[Job]:
    """One user's waiting row and brief on the shared ingest."""
    return [
        _job(
            user_id,
            installation_id,
            status=JobStatus.PENDING,
            job_type=JobType.REPOSITORY_INGEST,
            dedupe_key=f"{SHARED_KEY}:user:{user_id}",
            blocked_by_job_id=shared_id,
        ),
        _job(
            user_id,
            installation_id,
            status=JobStatus.PENDING,
            blocked_by_job_id=shared_id,
        ),
    ]


def _seed_shared_ingest(
    session: Session,
    *,
    shared_status: str,
    teammate_installation_id: int | None,
) -> tuple[int, list[int], list[int]]:
    """Seed a shared ingest with the suspended user and a teammate waiting."""
    session.add(_connection("suspended", 501, 101))
    if teammate_installation_id is not None:
        session.add(_connection("teammate", 601, teammate_installation_id))
    shared = _job(
        None,
        None,
        status=shared_status,
        job_type=JobType.REPOSITORY_INGEST,
        dedupe_key=SHARED_KEY,
    )
    session.add(shared)
    session.flush()
    own = _waiting_jobs("suspended", 101, shared.id)
    teammate = (
        _waiting_jobs("teammate", teammate_installation_id, shared.id)
        if teammate_installation_id is not None
        else []
    )
    session.add_all(own + teammate)
    session.commit()
    return shared.id, [job.id for job in own], [job.id for job in teammate]


def _claimable(session: Session, job: Job) -> bool:
    """Mirror the worker claim predicate for SQLite-backed tests."""

    def eligible(candidate: Job) -> bool:
        return session.exec(
            select(GithubConnections).where(
                GithubConnections.userId == candidate.userId,
                GithubConnections.installationId == candidate.installation_id,
                GithubConnections.active.is_(True),
            )
        ).one_or_none() is not None

    if job.userId is None:
        waiting = session.exec(
            select(Job).where(
                Job.blocked_by_job_id == job.id,
                Job.status.in_(JobStatus.ACTIVE),
            )
        ).all()
        runnable = any(eligible(candidate) for candidate in waiting)
    else:
        runnable = eligible(job)
    dependency = (
        session.get(Job, job.blocked_by_job_id)
        if job.blocked_by_job_id is not None
        else None
    )
    return (
        job.status == JobStatus.PENDING
        and runnable
        and (dependency is None or dependency.status not in JobStatus.ACTIVE)
    )


@pytest.mark.parametrize("shared_status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_suspension_cancels_users_jobs_and_leaves_shared_ingest(shared_status):
    engine = _engine()
    with Session(engine) as session:
        shared_id, own_ids, teammate_ids = _seed_shared_ingest(
            session,
            shared_status=shared_status,
            teammate_installation_id=202,
        )

        set_installation_active(session, 101, active=False)

        shared = session.get(Job, shared_id)
        assert shared.status == shared_status
        assert shared.userId is None
        assert shared.installation_id is None
        assert shared.error is None
        if shared_status == JobStatus.RUNNING:
            assert shared.claimed_by == "worker"
        for job_id in own_ids:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.CANCELLED
            assert job.error == SUSPENSION_ERROR
        for job_id in teammate_ids:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.PENDING
            assert job.error is None
            assert job.blocked_by_job_id == shared_id
        if shared_status == JobStatus.PENDING:
            assert _claimable(session, shared)

    engine.dispose()


def test_suspension_of_only_requester_leaves_shared_ingest_idle_for_good():
    engine = _engine()
    with Session(engine) as session:
        shared_id, own_ids, _ = _seed_shared_ingest(
            session,
            shared_status=JobStatus.PENDING,
            teammate_installation_id=None,
        )

        set_installation_active(session, 101, active=False)

        shared = session.get(Job, shared_id)
        assert shared.status == JobStatus.PENDING
        assert not _claimable(session, shared)
        for job_id in own_ids:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.CANCELLED
            assert job.error == SUSPENSION_ERROR

        set_installation_active(session, 101, active=True)

        assert not _claimable(session, session.get(Job, shared_id))
        for job_id in own_ids:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.CANCELLED
            assert not _claimable(session, job)

    engine.dispose()


def test_co_suspended_users_leave_shared_ingest_idle():
    engine = _engine()
    with Session(engine) as session:
        shared_id, own_ids, teammate_ids = _seed_shared_ingest(
            session,
            shared_status=JobStatus.RUNNING,
            teammate_installation_id=101,
        )

        set_installation_active(session, 101, active=False)

        for job_id in own_ids + teammate_ids:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.CANCELLED
            assert job.error == SUSPENSION_ERROR
        shared = session.get(Job, shared_id)
        assert shared.status == JobStatus.RUNNING
        assert shared.error is None

    engine.dispose()


def test_suspension_replay_is_a_no_op():
    engine = _engine()
    with Session(engine) as session:
        shared_id, own_ids, teammate_ids = _seed_shared_ingest(
            session,
            shared_status=JobStatus.PENDING,
            teammate_installation_id=202,
        )
        set_installation_active(session, 101, active=False)
        before = {
            job.id: (job.status, job.error, job.updatedAt)
            for job in session.exec(select(Job)).all()
        }

        set_installation_active(session, 101, active=False)

        after = {
            job.id: (job.status, job.error, job.updatedAt)
            for job in session.exec(select(Job)).all()
        }
        assert after == before

    engine.dispose()


def test_unsuspend_does_not_move_jobs():
    engine = _engine()
    with Session(engine) as session:
        shared_id, own_ids, _ = _seed_shared_ingest(
            session,
            shared_status=JobStatus.PENDING,
            teammate_installation_id=202,
        )

        set_installation_active(session, 101, active=True)

        shared = session.get(Job, shared_id)
        assert shared.userId is None
        assert shared.installation_id is None
        for job_id in own_ids:
            job = session.get(Job, job_id)
            assert job.userId == "suspended"
            assert job.installation_id == 101
            assert job.status == JobStatus.PENDING

    engine.dispose()
