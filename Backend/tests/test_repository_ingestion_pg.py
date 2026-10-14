"""Failed-generation cleanup against a real Postgres.

The cleanup's guard is a ``NOT EXISTS`` subquery against ``repo_index_state``,
so it only means something when the publish statements really took effect.
GitHub and embeddings are patched out; everything else runs as in the worker.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, select

from app.config import settings
from app.models.code import CodeChunkEmbedding, CodeChunkModel, RepoIndexState
from app.services.embeddings import EMBED_DIMENSIONS, EmbeddingError
from app.services.repository_ingestion import (
    TransientRepositoryIngestionError,
    ingest_repository,
)
from tests.test_repository_ingestion import (
    _StreamingResponse,
    _github,
    _repository_installation,
    _tarball,
)
from tests.test_worker_claim_pg import pg_engine  # noqa: F401  (fixture)


REPO = "org/repo"
FILES = {
    "src/a.py": b"def a():\n    return 1\n",
    "src/b.py": b"def b():\n    return 2\n",
}


@pytest.fixture
def engine(pg_engine):  # noqa: F811
    try:
        with pg_engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except (OperationalError, ProgrammingError) as error:
        pytest.skip(f"pgvector not available ({error})")
    CodeChunkModel.__table__.create(pg_engine, checkfirst=True)
    CodeChunkEmbedding.__table__.create(pg_engine, checkfirst=True)
    with Session(pg_engine) as session:
        session.execute(
            text(
                "TRUNCATE code_chunks, code_chunk_embeddings, repo_index_state "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
    return pg_engine


def _vectors(texts):
    return [[0.1] * EMBED_DIMENSIONS for _ in texts]


async def _ingest(session: Session, *, embed, finalize_publication=None):
    github_patch, _ = _github(_repository_installation())
    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 1),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(_tarball(FILES)),
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            side_effect=embed,
        ),
    ):
        return await ingest_repository(
            session,
            repo_name=REPO,
            installation_id=123,
            ref="main",
            finalize_publication=finalize_publication,
        )


def _index(engine) -> tuple[str | None, dict[str, int]]:
    """Return the active generation and the chunk count per generation."""
    with Session(engine) as session:
        state = session.exec(
            select(RepoIndexState).where(
                RepoIndexState.repo_name == REPO,
                RepoIndexState.ref == "main",
            )
        ).one_or_none()
        rows = session.execute(
            text(
                "SELECT generation, count(*) FROM code_chunks "
                "WHERE repo_name = :repo_name AND ref = 'main' "
                "GROUP BY generation"
            ),
            {"repo_name": REPO},
        ).all()
    active = state.active_generation if state is not None else None
    return active, {generation: count for generation, count in rows}


async def test_lost_publish_acknowledgment_keeps_the_active_generation(engine):
    session = Session(engine)
    real_commit = session.commit
    publish_pending = False

    def finalize_publication(_session, _result):
        nonlocal publish_pending
        publish_pending = True

    def commit_then_lose_acknowledgment():
        # Postgres commits the publish, then the connection drops before
        # the client hears back.
        nonlocal publish_pending
        real_commit()
        if publish_pending:
            publish_pending = False
            raise OperationalError(
                "COMMIT", {}, Exception("server closed the connection")
            )

    session.commit = commit_then_lose_acknowledgment
    try:
        with pytest.raises(TransientRepositoryIngestionError):
            await _ingest(
                session,
                embed=_vectors,
                finalize_publication=finalize_publication,
            )
    finally:
        session.close()

    active, chunks = _index(engine)
    assert active is not None
    assert chunks == {active: 2}


async def test_failed_generation_is_cleaned_while_the_active_one_stays(engine):
    with Session(engine) as session:
        await _ingest(session, embed=_vectors)
    published, _ = _index(engine)

    with Session(engine) as session:
        with pytest.raises(
            TransientRepositoryIngestionError, match="wave 2 failed"
        ):
            await _ingest(
                session,
                embed=[_vectors(["a"]), EmbeddingError("wave 2 failed")],
            )

    active, chunks = _index(engine)
    assert active == published
    assert chunks == {published: 2}
