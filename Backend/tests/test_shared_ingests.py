from sqlalchemy import create_engine, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.models.github_connection import GithubConnections
from app.models.job import Job, JobStatus, JobType
from app.services.shared_ingests import (
    MISSING_SHARED_INGEST_ERROR,
    enqueue_shared_ingest,
    release_user_jobs,
    waiting_row_outcome,
)


SHARED_KEY = "repository_ingest:org/repo:main"
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


def _active_shared(session: Session) -> Job | None:
    return session.exec(
        select(Job).where(
            Job.dedupe_key == SHARED_KEY,
            Job.status.in_(JobStatus.ACTIVE),
        )
    ).one_or_none()


def _finish(session: Session, job: Job, status: str, **values) -> None:
    job.status = status
    for name, value in values.items():
        setattr(job, name, value)
    session.add(job)
    session.commit()


def test_first_request_creates_ownerless_shared_ingest_and_waiting_row():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")

        waiting, shared, created = _request(session, "a")

        assert created is True
        assert shared.userId is None
        assert shared.installation_id is None
        assert shared.dedupe_key == SHARED_KEY
        assert shared.blocked_by_job_id is None
        assert waiting.id != shared.id
        assert waiting.userId == "a"
        assert waiting.installation_id == 101
        assert waiting.dedupe_key == f"{SHARED_KEY}:user:a"
        assert waiting.blocked_by_job_id == shared.id
        assert waiting.status == JobStatus.PENDING
    engine.dispose()


def test_second_user_request_creates_only_their_waiting_row():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, shared, _ = _request(session, "a")

        waiting, joined, created = _request(session, "b")

        assert created is True
        assert joined.id == shared.id
        assert waiting.userId == "b"
        assert waiting.installation_id == 202
        assert waiting.job_type == JobType.REPOSITORY_INGEST
        assert waiting.dedupe_key == f"{SHARED_KEY}:user:b"
        assert waiting.blocked_by_job_id == shared.id
        assert len(session.exec(select(Job)).all()) == 3
    engine.dispose()


def test_repeat_request_returns_same_waiting_row_while_shared_ingest_is_active():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")
        waiting, shared, _ = _request(session, "a")
        _finish(session, shared, JobStatus.RUNNING, claimed_by="worker")

        repeat, joined, created = _request(session, "a")

        assert created is False
        assert repeat.id == waiting.id
        assert joined.id == shared.id
        assert len(session.exec(select(Job)).all()) == 2
    engine.dispose()


def test_request_without_waiting_row_returns_shared_ingest_only():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, shared, _ = _request(session, "a")

        requester_job, joined, created = _request(session, "b", waiting_row=False)

        assert created is False
        assert requester_job.id == shared.id
        assert joined.id == shared.id
        assert len(session.exec(select(Job)).all()) == 2
    engine.dispose()


def test_request_without_waiting_row_creates_shared_ingest_nobody_waits_on():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")

        requester_job, shared, created = _request(session, "a", waiting_row=False)

        assert created is True
        assert requester_job is shared
        assert shared.userId is None
        assert session.exec(select(Job)).all() == [shared]
    engine.dispose()


def test_repeat_request_after_shared_ingest_completed_starts_fresh():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")
        waiting, shared, _ = _request(session, "a")
        old_waiting_id, old_shared_id = waiting.id, shared.id
        _finish(session, shared, JobStatus.COMPLETE, artifact={"chunks_inserted": 3})

        fresh_waiting, fresh_shared, created = _request(session, "a")

        settled = session.get(Job, old_waiting_id)
        assert settled.status == JobStatus.COMPLETE
        assert settled.artifact == {"chunks_inserted": 3}
        assert created is True
        assert fresh_shared.id != old_shared_id
        assert fresh_shared.userId is None
        assert fresh_waiting.id != old_waiting_id
        assert fresh_waiting.blocked_by_job_id == fresh_shared.id
    engine.dispose()


def test_repeat_request_after_shared_ingest_failed_starts_fresh():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a")
        waiting, shared, _ = _request(session, "a")
        old_waiting_id, old_shared_id = waiting.id, shared.id
        _finish(session, shared, JobStatus.FAILED, error="Repository not found")

        fresh_waiting, fresh_shared, created = _request(session, "a")

        settled = session.get(Job, old_waiting_id)
        assert settled.status == JobStatus.FAILED
        assert settled.error == "ingest failed: Repository not found"
        assert created is True
        assert fresh_shared.id != old_shared_id
        assert fresh_waiting.blocked_by_job_id == fresh_shared.id
    engine.dispose()


def test_waiting_row_outcome():
    assert waiting_row_outcome(None) == (
        JobStatus.FAILED,
        None,
        MISSING_SHARED_INGEST_ERROR,
    )
    for status in JobStatus.ACTIVE:
        assert waiting_row_outcome(Job(repo_name="r", status=status)) is None
    assert waiting_row_outcome(
        Job(repo_name="r", status=JobStatus.COMPLETE, artifact={"n": 1})
    ) == (JobStatus.COMPLETE, {"n": 1}, None)
    assert waiting_row_outcome(
        Job(repo_name="r", status=JobStatus.FAILED, error="boom")
    ) == (JobStatus.FAILED, None, "ingest failed: boom")
    assert waiting_row_outcome(
        Job(repo_name="r", status=JobStatus.CANCELLED)
    ) == (JobStatus.FAILED, None, "ingest failed: cancelled")


def test_release_cancel_mode_cancels_users_jobs_and_leaves_shared_ingest():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        waiting_a, shared, _ = _request(session, "a")
        waiting_b, _, _ = _request(session, "b")
        _finish(session, shared, JobStatus.RUNNING, claimed_by="worker", attempts=1)
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
        ids = (waiting_a.id, waiting_b.id, shared.id, tour.id)

        _remove(session, "a")

        waiting_a, waiting_b, shared, tour = (session.get(Job, i) for i in ids)
        for job in (waiting_a, tour):
            assert job.status == JobStatus.CANCELLED
            assert job.error == ERROR
            assert job.claimed_by is None
        assert waiting_b.status == JobStatus.PENDING
        assert waiting_b.error is None
        assert shared.status == JobStatus.RUNNING
        assert shared.claimed_by == "worker"
        assert shared.userId is None
    engine.dispose()


def test_release_delete_mode_deletes_users_rows_and_leaves_shared_ingest():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b")
        _, shared, _ = _request(session, "a")
        waiting_b, _, _ = _request(session, "b")
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
        shared_id, waiting_b_id = shared.id, waiting_b.id

        _remove(session, "a", dispose="delete")

        assert session.exec(select(Job).where(Job.userId == "a")).all() == []
        assert _active_shared(session).id == shared_id
        assert session.get(Job, waiting_b_id).status == JobStatus.PENDING
    engine.dispose()


def test_shared_ingest_stays_pending_after_every_requester_leaves():
    engine = _engine()
    with Session(engine) as session:
        _connect(session, "a", "b", "c")
        _, shared, _ = _request(session, "a")
        shared_id = shared.id
        _request(session, "b")

        _remove(session, "a")
        _remove(session, "b")

        idle = _active_shared(session)
        assert idle.id == shared_id
        assert idle.status == JobStatus.PENDING

        waiting, joined, created = _request(session, "c")
        assert created is True
        assert joined.id == shared_id
        assert waiting.blocked_by_job_id == shared_id
    engine.dispose()
