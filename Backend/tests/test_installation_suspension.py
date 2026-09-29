import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
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


def _job(
    user_id: str,
    installation_id: int,
    *,
    status: str,
    job_type: str = JobType.ISSUE_BRIEF,
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
        dedupe_key=(
            "repository_ingest:org/repo:main"
            if job_type == JobType.REPOSITORY_INGEST
            else None
        ),
        status=status,
        blocked_by_job_id=blocked_by_job_id,
        claimed_at=(timestamp if status == JobStatus.RUNNING else None),
        claimed_by="worker" if status == JobStatus.RUNNING else None,
        createdAt=timestamp,
        updatedAt=timestamp,
    )


def _seed_shared_ingest(
    session: Session,
    *,
    ingest_status: str,
    other_installation_id: int,
) -> tuple[int, int, int]:
    """Seed a suspended-owner ingest with one brief per user blocked on it."""
    session.add_all(
        [
            _connection("suspended", 501, 101),
            _connection("teammate", 601, other_installation_id),
        ]
    )
    ingest = _job(
        "suspended",
        101,
        status=ingest_status,
        job_type=JobType.REPOSITORY_INGEST,
    )
    session.add(ingest)
    session.flush()
    own_brief = _job(
        "suspended",
        101,
        status=JobStatus.PENDING,
        blocked_by_job_id=ingest.id,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    teammate_brief = _job(
        "teammate",
        other_installation_id,
        status=JobStatus.PENDING,
        blocked_by_job_id=ingest.id,
        created_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
    )
    session.add_all([own_brief, teammate_brief])
    session.commit()
    return ingest.id, own_brief.id, teammate_brief.id


def _claimable(session: Session, job: Job) -> bool:
    """Mirror the worker claim predicate for SQLite-backed tests."""
    connection = session.exec(
        select(GithubConnections).where(
            GithubConnections.userId == job.userId,
            GithubConnections.installationId == job.installation_id,
            GithubConnections.active.is_(True),
        )
    ).one_or_none()
    dependency = (
        session.get(Job, job.blocked_by_job_id)
        if job.blocked_by_job_id is not None
        else None
    )
    return (
        job.status == JobStatus.PENDING
        and connection is not None
        and (dependency is None or dependency.status not in JobStatus.ACTIVE)
    )


def test_suspension_transfers_pending_shared_ingest_to_authorized_dependent():
    engine = _engine()
    with Session(engine) as session:
        ingest_id, own_brief_id, teammate_brief_id = _seed_shared_ingest(
            session,
            ingest_status=JobStatus.PENDING,
            other_installation_id=202,
        )

        set_installation_active(session, 101, active=False)

        ingest = session.get(Job, ingest_id)
        assert ingest.userId == "teammate"
        assert ingest.installation_id == 202
        assert ingest.status == JobStatus.PENDING
        assert _claimable(session, ingest)

        own_brief = session.get(Job, own_brief_id)
        assert own_brief.status == JobStatus.PENDING
        assert own_brief.blocked_by_job_id == ingest_id
        teammate_brief = session.get(Job, teammate_brief_id)
        assert teammate_brief.blocked_by_job_id == ingest_id

    engine.dispose()


def test_suspension_replaces_running_ingest_and_repoints_all_dependents():
    engine = _engine()
    with Session(engine) as session:
        ingest_id, own_brief_id, teammate_brief_id = _seed_shared_ingest(
            session,
            ingest_status=JobStatus.RUNNING,
            other_installation_id=202,
        )

        set_installation_active(session, 101, active=False)

        old_ingest = session.get(Job, ingest_id)
        assert old_ingest.status == JobStatus.CANCELLED
        assert old_ingest.error == SUSPENSION_ERROR
        assert old_ingest.claimed_at is None
        assert old_ingest.claimed_by is None

        replacement = session.exec(
            select(Job).where(
                Job.job_type == JobType.REPOSITORY_INGEST,
                Job.status == JobStatus.PENDING,
            )
        ).one()
        assert replacement.userId == "teammate"
        assert replacement.installation_id == 202

        own_brief = session.get(Job, own_brief_id)
        teammate_brief = session.get(Job, teammate_brief_id)
        assert own_brief.status == JobStatus.PENDING
        assert own_brief.blocked_by_job_id == replacement.id
        assert teammate_brief.blocked_by_job_id == replacement.id

    engine.dispose()


def test_suspension_without_other_dependents_leaves_jobs_frozen_until_unsuspend():
    engine = _engine()
    with Session(engine) as session:
        session.add(_connection("suspended", 501, 101))
        ingest = _job(
            "suspended",
            101,
            status=JobStatus.PENDING,
            job_type=JobType.REPOSITORY_INGEST,
        )
        session.add(ingest)
        session.flush()
        brief = _job(
            "suspended",
            101,
            status=JobStatus.PENDING,
            blocked_by_job_id=ingest.id,
        )
        session.add(brief)
        session.commit()
        ingest_id, brief_id = ingest.id, brief.id

        set_installation_active(session, 101, active=False)

        ingest = session.get(Job, ingest_id)
        brief = session.get(Job, brief_id)
        assert ingest.userId == "suspended"
        assert ingest.status == JobStatus.PENDING
        assert ingest.error is None
        assert brief.status == JobStatus.PENDING
        assert brief.blocked_by_job_id == ingest_id
        assert not _claimable(session, ingest)

        set_installation_active(session, 101, active=True)

        assert _claimable(session, session.get(Job, ingest_id))

    engine.dispose()


def test_co_suspended_users_are_not_chosen_as_new_ingest_owner():
    engine = _engine()
    with Session(engine) as session:
        ingest_id, own_brief_id, teammate_brief_id = _seed_shared_ingest(
            session,
            ingest_status=JobStatus.RUNNING,
            other_installation_id=101,
        )

        set_installation_active(session, 101, active=False)

        ingest = session.get(Job, ingest_id)
        assert ingest.userId == "suspended"
        assert ingest.status == JobStatus.RUNNING
        assert ingest.error is None
        assert len(session.exec(select(Job)).all()) == 3
        for brief_id in (own_brief_id, teammate_brief_id):
            brief = session.get(Job, brief_id)
            assert brief.status == JobStatus.PENDING
            assert brief.blocked_by_job_id == ingest_id

    engine.dispose()


def test_unsuspend_does_not_move_jobs():
    engine = _engine()
    with Session(engine) as session:
        ingest_id, _, _ = _seed_shared_ingest(
            session,
            ingest_status=JobStatus.PENDING,
            other_installation_id=202,
        )

        set_installation_active(session, 101, active=True)

        ingest = session.get(Job, ingest_id)
        assert ingest.userId == "suspended"
        assert ingest.installation_id == 101

    engine.dispose()
