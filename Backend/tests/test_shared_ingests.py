from unittest.mock import patch

from sqlalchemy import create_engine, delete, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services import shared_ingests
from app.services.shared_ingests import enqueue_shared_ingest, release_user_jobs


PRIMARY_KEY = "repository_ingest:org/repo:main"
ERROR = "removed"


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


def _connect(session: Session, *user_ids: str) -> None:
    for index, user_id in enumerate(user_ids):
        session.add(
            GithubConnections(
                userId=user_id,
                githubUsername=f"github-{user_id}",
                githubUserId=1000 + index,
                installationId=_installation(user_id),
            )
        )
    session.commit()


def _installation(user_id: str) -> int:
    return {"a": 101, "b": 202, "c": 303, "d": 404}[user_id]


def _request(session: Session, user_id: str, **kwargs):
    return enqueue_shared_ingest(
        session,
        user_id=user_id,
        installation_id=_installation(user_id),
        repo_name="Org/Repo",
        ref="main",
        **kwargs,
    )


def _remove(session: Session, user_id: str, *, dispose: str = "cancel") -> None:
    session.exec(delete(GithubConnections).where(GithubConnections.userId == user_id))
    release_user_jobs(session, {user_id}, error=ERROR, dispose=dispose)
    session.commit()


def _active_primary(session: Session) -> Job | None:
    return session.exec(
        select(Job).where(
            Job.dedupe_key == PRIMARY_KEY,
            Job.status.in_(JobStatus.ACTIVE),
        )
    ).one_or_none()


def test_first_request_creates_primary_owned_by_requester():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")

        requester_job, primary, created = _request(session, "a")

        assert created is True
        assert requester_job is primary
        assert primary.userId == "a"
        assert primary.dedupe_key == PRIMARY_KEY
        assert primary.blocked_by_job_id is None
    engine.dispose()


def test_non_owner_request_creates_waiting_row_blocked_on_primary():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")

        waiting, joined, created = _request(session, "b")

        assert created is True
        assert joined.id == primary.id
        assert waiting.id != primary.id
        assert waiting.userId == "b"
        assert waiting.installation_id == 202
        assert waiting.job_type == JobType.REPOSITORY_INGEST
        assert waiting.dedupe_key == f"{PRIMARY_KEY}:user:b"
        assert waiting.blocked_by_job_id == primary.id
        assert waiting.status == JobStatus.PENDING
    engine.dispose()


def test_repeat_request_by_same_user_returns_same_waiting_row():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")
        waiting, _, _ = _request(session, "b")

        repeat, joined, created = _request(session, "b")

        assert created is False
        assert repeat.id == waiting.id
        assert joined.id == primary.id
        assert len(session.exec(select(Job)).all()) == 2
    engine.dispose()


def test_owner_repeat_request_returns_primary():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")
        _, primary, _ = _request(session, "a")

        repeat, joined, created = _request(session, "a")

        assert created is False
        assert repeat.id == primary.id
        assert joined.id == primary.id
        assert len(session.exec(select(Job)).all()) == 1
    engine.dispose()


def test_request_without_waiting_row_returns_primary_only():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")

        requester_job, joined, created = _request(session, "b", waiting_row=False)

        assert created is False
        assert requester_job.id == primary.id
        assert joined.id == primary.id
        assert len(session.exec(select(Job)).all()) == 1
    engine.dispose()


def test_primary_cancelled_between_lookup_and_lock_makes_requester_owner():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")
        primary_id = primary.id
        real_lock = shared_ingests._lock_job

        def cancel_then_lock(lock_session, job_id):
            lock_session.exec(
                update(Job)
                .where(Job.id == job_id)
                .values(status=JobStatus.CANCELLED)
            )
            return real_lock(lock_session, job_id)

        with patch.object(shared_ingests, "_lock_job", side_effect=cancel_then_lock):
            requester_job, new_primary, created = _request(session, "b")

        assert created is True
        assert requester_job is new_primary
        assert new_primary.id != primary_id
        assert new_primary.userId == "b"
        assert new_primary.blocked_by_job_id is None
        assert session.get(Job, primary_id).status == JobStatus.CANCELLED
    engine.dispose()


def test_release_cancel_mode_hands_over_then_cancels_remaining_jobs():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")
        waiting, _, _ = _request(session, "b")
        tour = Job(
            userId="a",
            installation_id=101,
            repo_name="org/repo",
            ref="main",
            job_type=JobType.TOUR,
            status=JobStatus.RUNNING,
            claimed_by="worker",
        )
        session.add(tour)
        session.commit()
        primary_id, waiting_id, tour_id = primary.id, waiting.id, tour.id

        _remove(session, "a")

        primary = session.get(Job, primary_id)
        assert primary.userId == "b"
        assert primary.status == JobStatus.PENDING
        tour = session.get(Job, tour_id)
        assert tour.status == JobStatus.CANCELLED
        assert tour.error == ERROR
        assert tour.claimed_by is None
        assert session.get(Job, waiting_id).status == JobStatus.PENDING
    engine.dispose()


def test_release_delete_mode_hands_over_then_deletes_remaining_rows():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, primary, _ = _request(session, "a")
        primary.status = JobStatus.RUNNING
        primary.claimed_by = "worker"
        session.add(primary)
        session.add(
            Job(
                userId="a",
                installation_id=101,
                repo_name="org/repo",
                ref="main",
                job_type=JobType.TOUR,
                status=JobStatus.COMPLETE,
            )
        )
        session.commit()
        waiting, _, _ = _request(session, "b")
        waiting_id = waiting.id

        _remove(session, "a", dispose="delete")

        assert session.exec(select(Job).where(Job.userId == "a")).all() == []
        replacement = _active_primary(session)
        assert replacement.userId == "b"
        assert session.get(Job, waiting_id).blocked_by_job_id == replacement.id
    engine.dispose()


def test_ingest_follows_chain_of_waiting_users_until_none_remain():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b", "c", "d")
        _, primary, _ = _request(session, "a")
        primary_id = primary.id
        _request(session, "b")
        _request(session, "c")

        _remove(session, "a")
        assert _active_primary(session).id == primary_id
        assert _active_primary(session).userId == "b"

        _remove(session, "b")
        assert _active_primary(session).id == primary_id
        assert _active_primary(session).userId == "c"
        assert _active_primary(session).installation_id == 303

        _remove(session, "c")
        assert _active_primary(session) is None
        assert session.get(Job, primary_id).status == JobStatus.CANCELLED
        assert session.get(Job, primary_id).error == ERROR
        assert (
            session.exec(
                select(Job).where(Job.status.in_(JobStatus.ACTIVE))
            ).all()
            == []
        )

        requester_job, fresh, created = _request(session, "d")
        assert created is True
        assert requester_job is fresh
        assert fresh.id != primary_id
        assert fresh.userId == "d"
    engine.dispose()
