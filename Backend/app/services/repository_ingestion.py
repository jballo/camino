from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import random
import time

from github import Auth, GithubException, GithubIntegration
from requests.exceptions import RequestException
from sqlalchemy import exc, text
from sqlmodel import Session, delete

from app.config import settings
from app.models.code import CodeChunkEmbedding, CodeChunkModel
from app.services.embeddings import (
    EMBED_DIMENSIONS,
    EMBED_MODEL,
    EmbeddingError,
    build_embedding_text,
    embed_all,
)
from app.services.parser import (
    LANGUAGES,
    MAX_FILE_BYTES,
    SKIP_DIRS,
    CodeChunk,
    parse_file,
)
from app.services.search_index import populate_search_vector_sql

logger = logging.getLogger(__name__)

_RETRYABLE_GH_STATUS = {408, 429, 500, 502, 503, 504}


class RepositoryIngestionError(RuntimeError):
    """Base error raised by repository ingestion."""


class TransientRepositoryIngestionError(RepositoryIngestionError):
    """An upstream or database failure that is safe to retry."""


class PermanentRepositoryIngestionError(RepositoryIngestionError):
    """Invalid input or deterministic failure that should not be retried."""


@dataclass(frozen=True)
class _RepositoryWalk:
    chunks: list[CodeChunk]
    files_seen: int
    files_parsed: int
    dirs_walked: int


def _gh_with_retry(fn, what: str, attempts: int = 4, base_delay: float = 0.5):
    """Run one blocking GitHub request with bounded transient retries."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except GithubException as error:
            status = getattr(error, "status", None)
            if status not in _RETRYABLE_GH_STATUS or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            logger.warning(
                "github transient error, retrying | what=%s status=%s "
                "attempt=%d/%d sleep=%.2fs",
                what,
                status,
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)


def _walk_repository(
    *,
    repo_name: str,
    installation_id: int,
) -> _RepositoryWalk:
    """Synchronously fetch and parse a repository.

    PyGithub performs blocking HTTP and lazy pagination, and decoded_content can
    issue another request. The async caller runs this entire function in one
    thread so none of those operations block its event loop.
    """
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    installation = GithubIntegration(auth=app_auth).get_app_installation(
        installation_id
    )
    repo_selected = next(
        (repo for repo in installation.get_repos() if repo.full_name == repo_name),
        None,
    )
    if repo_selected is None:
        raise PermanentRepositoryIngestionError("Repository not found")

    contents = _gh_with_retry(
        lambda: repo_selected.get_contents(""),
        "get_contents:root",
    )
    chunks: list[CodeChunk] = []
    files_seen = 0
    files_parsed = 0
    dirs_walked = 0

    while contents:
        file_content = contents.pop(0)
        path_parts = file_content.path.replace("\\", "/").split("/")
        if any(part in SKIP_DIRS for part in path_parts):
            continue

        if file_content.type == "dir":
            dirs_walked += 1
            contents.extend(
                _gh_with_retry(
                    lambda path=file_content.path: repo_selected.get_contents(path),
                    f"get_contents:{file_content.path}",
                )
            )
            continue
        if file_content.type != "file":
            continue

        extension = os.path.splitext(file_content.path)[1]
        if extension not in LANGUAGES:
            continue
        if file_content.size and file_content.size > MAX_FILE_BYTES:
            continue

        files_seen += 1
        try:
            source_bytes = file_content.decoded_content
        except AssertionError:
            continue
        if not source_bytes:
            continue

        chunks.extend(parse_file(file_content.path, source_bytes))
        files_parsed += 1

    return _RepositoryWalk(
        chunks=chunks,
        files_seen=files_seen,
        files_parsed=files_parsed,
        dirs_walked=dirs_walked,
    )


async def ingest_repository(
    session: Session,
    *,
    repo_name: str,
    installation_id: int,
) -> dict[str, int]:
    """Replace one repository's index atomically and return ingestion counts."""
    phase = "init"
    files_seen = 0
    files_parsed = 0
    dirs_walked = 0
    started = time.monotonic()

    try:
        phase = "github_auth"
        logger.info(
            "ingest start | repo=%r installation=%s",
            repo_name,
            installation_id,
        )

        phase = "walk"
        walk = await asyncio.to_thread(
            _walk_repository,
            repo_name=repo_name,
            installation_id=installation_id,
        )
        all_chunks = walk.chunks
        files_seen = walk.files_seen
        files_parsed = walk.files_parsed
        dirs_walked = walk.dirs_walked

        logger.info(
            "ingest walk complete | repo=%r files_seen=%d files_parsed=%d "
            "dirs_walked=%d chunks=%d elapsed=%.2fs",
            repo_name,
            files_seen,
            files_parsed,
            dirs_walked,
            len(all_chunks),
            time.monotonic() - started,
        )

        phase = "embed"
        vectors = await embed_all(
            [build_embedding_text(chunk) for chunk in all_chunks]
        )

        phase = "persist"
        session.exec(
            delete(CodeChunkModel).where(
                CodeChunkModel.repo_name == repo_name,
                CodeChunkModel.installation_id == installation_id,
            )
        )
        chunk_models = [
            CodeChunkModel.from_parsed(
                chunk,
                repo_name=repo_name,
                installation_id=installation_id,
            )
            for chunk in all_chunks
        ]
        session.add_all(chunk_models)
        session.flush()
        embedding_models = [
            CodeChunkEmbedding(
                chunk_id=chunk.id,
                model_name=EMBED_MODEL,
                dimension=EMBED_DIMENSIONS,
                embedding=vector,
            )
            for chunk, vector in zip(chunk_models, vectors, strict=True)
        ]
        session.add_all(embedding_models)
        session.exec(
            text(populate_search_vector_sql(only_null=True)).bindparams(
                repo_name=repo_name,
                installation_id=installation_id,
            )
        )
        session.commit()

        result = {
            "chunks_inserted": len(chunk_models),
            "embeddings_created": len(embedding_models),
        }
        logger.info(
            "ingest complete | repo=%r chunks=%d embeddings=%d elapsed=%.2fs",
            repo_name,
            result["chunks_inserted"],
            result["embeddings_created"],
            time.monotonic() - started,
        )
        return result
    except RepositoryIngestionError:
        session.rollback()
        raise
    except (RequestException, EmbeddingError, exc.OperationalError) as error:
        session.rollback()
        logger.warning(
            "transient ingest failure | phase=%s repo=%r error=%s",
            phase,
            repo_name,
            error,
        )
        raise TransientRepositoryIngestionError(str(error)) from error
    except GithubException as error:
        session.rollback()
        status = getattr(error, "status", None)
        message = f"GitHub request failed with status {status}"
        if status in _RETRYABLE_GH_STATUS:
            raise TransientRepositoryIngestionError(message) from error
        raise PermanentRepositoryIngestionError(message) from error
    except exc.IntegrityError as error:
        session.rollback()
        raise PermanentRepositoryIngestionError(
            "Database integrity error during ingestion"
        ) from error
    except exc.SQLAlchemyError as error:
        session.rollback()
        raise TransientRepositoryIngestionError(
            "Database error during ingestion"
        ) from error
    except Exception as error:
        session.rollback()
        logger.exception(
            "unexpected ingest failure | phase=%s repo=%r files_seen=%d "
            "elapsed=%.2fs",
            phase,
            repo_name,
            files_seen,
            time.monotonic() - started,
        )
        raise PermanentRepositoryIngestionError(
            "Internal ingestion error"
        ) from error
