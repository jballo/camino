import datetime as dt
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.authorization_revocation import (
    AuthorizationRevocationError,
    REVOCATION_ERROR,
    deactivate_user_connections,
    revoke_user_authorization,
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
    *,
    active: bool = True,
) -> GithubConnections:
    return GithubConnections(
        userId=user_id,
        githubUsername=f"github-{user_id}",
        githubUserId=github_user_id,
        installationId=installation_id,
        active=active,
    )


def _job(
    user_id: str,
    installation_id: int,
    *,
    status: str,
    job_type: str = JobType.TOUR,
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


def test_deletes_connections_and_cancels_only_revoked_users_active_jobs():
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _connection("revoked", 501, 101),
                _connection("teammate", 777, 101),
                _job("revoked", 101, status=JobStatus.PENDING),
                _job("revoked", 101, status=JobStatus.RUNNING),
                _job("revoked", 101, status=JobStatus.COMPLETE),
                _job("teammate", 101, status=JobStatus.RUNNING),
            ]
        )
        session.commit()

        revoke_user_authorization(session, 501)

        connection = session.exec(
            select(GithubConnections).where(GithubConnections.userId == "revoked")
        ).one_or_none()
        assert connection is None
        revoked_jobs = session.exec(
            select(Job).where(Job.userId == "revoked").order_by(Job.id)
        ).all()
        assert [job.status for job in revoked_jobs] == [
            JobStatus.CANCELLED,
            JobStatus.CANCELLED,
            JobStatus.COMPLETE,
        ]
        assert all(
            job.error == REVOCATION_ERROR
            and job.claimed_at is None
            and job.claimed_by is None
            for job in revoked_jobs[:2]
        )
        teammate_job = session.exec(
            select(Job).where(Job.userId == "teammate")
        ).one()
        assert teammate_job.status == JobStatus.RUNNING
        assert teammate_job.claimed_by == "worker"

    engine.dispose()


def test_transfers_pending_shared_ingest_to_oldest_authorized_dependent_owner():
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _connection("revoked", 501, 101),
                _connection("oldest", 601, 101),
                _connection("newer", 602, 101),
            ]
        )
        ingest = _job(
            "revoked",
            101,
            status=JobStatus.PENDING,
            job_type=JobType.REPOSITORY_INGEST,
        )
        session.add(ingest)
        session.flush()
        session.add_all(
            [
                _job(
                    "oldest",
                    101,
                    status=JobStatus.PENDING,
                    blocked_by_job_id=ingest.id,
                    created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                ),
                _job(
                    "newer",
                    101,
                    status=JobStatus.PENDING,
                    blocked_by_job_id=ingest.id,
                    created_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
                ),
            ]
        )
        session.commit()
        ingest_id = ingest.id

        revoke_user_authorization(session, 501)

        transferred = session.get(Job, ingest_id)
        assert transferred is not None
        assert transferred.userId == "oldest"
        assert transferred.installation_id == 101
        assert transferred.status == JobStatus.PENDING
        assert transferred.error is None

    engine.dispose()


def test_running_shared_ingest_is_replaced_and_dependents_are_repointed():
    engine = _engine()
    with Session(engine) as session:
        session.add_all(
            [
                _connection("revoked", 501, 101),
                _connection("teammate", 601, 101),
            ]
        )
        ingest = _job(
            "revoked",
            101,
            status=JobStatus.RUNNING,
            job_type=JobType.REPOSITORY_INGEST,
        )
        session.add(ingest)
        session.flush()
        dependent = _job(
            "teammate",
            101,
            status=JobStatus.PENDING,
            blocked_by_job_id=ingest.id,
        )
        session.add(dependent)
        session.commit()
        ingest_id = ingest.id
        dependent_id = dependent.id

        revoke_user_authorization(session, 501)

        old_ingest = session.get(Job, ingest_id)
        assert old_ingest is not None
        assert old_ingest.status == JobStatus.CANCELLED
        replacement = session.exec(
            select(Job).where(
                Job.job_type == JobType.REPOSITORY_INGEST,
                Job.status == JobStatus.PENDING,
            )
        ).one()
        assert replacement.userId == "teammate"
        assert replacement.id != old_ingest.id
        repointed = session.get(Job, dependent_id)
        assert repointed is not None
        assert repointed.blocked_by_job_id == replacement.id

    engine.dispose()


def test_replay_is_a_no_op_for_already_cancelled_jobs():
    engine = _engine()
    with Session(engine) as session:
        session.add(_connection("revoked", 501, 101))
        session.add(_job("revoked", 101, status=JobStatus.PENDING))
        session.commit()

        revoke_user_authorization(session, 501)
        revoke_user_authorization(session, 501)

        jobs = session.exec(select(Job)).all()
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.CANCELLED
        assert jobs[0].error == REVOCATION_ERROR

    engine.dispose()


def test_rolls_back_connection_and_job_changes_when_commit_fails():
    engine = _engine()
    with Session(engine) as session:
        session.add(_connection("revoked", 501, 101))
        session.add(_job("revoked", 101, status=JobStatus.PENDING))
        session.commit()

        @event.listens_for(session, "before_commit", once=True)
        def _fail_commit(_session):
            raise SQLAlchemyError("database unavailable")

        with pytest.raises(AuthorizationRevocationError):
            revoke_user_authorization(session, 501)

    with Session(engine) as verification:
        connection = verification.exec(select(GithubConnections)).one()
        job = verification.exec(select(Job)).one()
        assert connection.active is True
        assert job.status == JobStatus.PENDING
        assert job.error is None

    engine.dispose()


def test_backward_compatible_alias_delegates_to_revocation():
    session = MagicMock()
    session.exec.return_value.scalars.return_value.all.return_value = []

    deactivate_user_connections(session, 501)

    statement = str(session.exec.call_args.args[0])
    assert "DELETE FROM githubconnections" in statement
    assert 'githubconnections."githubUserId"' in statement
    assert 'RETURNING githubconnections."userId"' in statement
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()
