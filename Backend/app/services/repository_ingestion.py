from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tarfile
import tempfile
import time

from github import Auth, GithubException, GithubIntegration
import requests
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
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = (10, 120)


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
    commit_sha: str


def _download_tarball(repo_name: str, token: str, destination: Path) -> None:
    response = requests.get(
        f"https://api.github.com/repos/{repo_name}/tarball",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        stream=True,
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    try:
        if response.status_code >= 400:
            message = f"GitHub request failed with status {response.status_code}"
            if (
                response.status_code in _RETRYABLE_GH_STATUS
                or 500 <= response.status_code < 600
            ):
                raise TransientRepositoryIngestionError(message)
            raise PermanentRepositoryIngestionError(message)

        downloaded = 0
        with destination.open("wb") as archive_file:
            for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > settings.ingest_max_tarball_bytes:
                    raise PermanentRepositoryIngestionError(
                        "Repository too large to ingest"
                    )
                archive_file.write(chunk)
    finally:
        response.close()


def _extract_tarball(archive_path: Path, destination: Path) -> tuple[Path, str]:
    with tarfile.open(archive_path) as archive:
        archive.extractall(destination, filter="data")

    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1:
        raise tarfile.ReadError("GitHub tarball does not have one root directory")

    repo_root = roots[0]
    _, separator, commit_sha = repo_root.name.rpartition("-")
    if not separator or not commit_sha:
        raise tarfile.ReadError("GitHub tarball root does not contain a commit SHA")
    return repo_root, commit_sha


def _parse_repository(root: Path, commit_sha: str) -> _RepositoryWalk:
    chunks: list[CodeChunk] = []
    files_seen = 0
    files_parsed = 0
    dirs_walked = 0

    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) != root:
            dirs_walked += 1
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            extension = os.path.splitext(name)[1]
            if extension not in LANGUAGES:
                continue

            full_path = Path(dirpath) / name
            try:
                if full_path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue

            files_seen += 1
            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue
            if not source_bytes:
                continue

            relative_path = full_path.relative_to(root).as_posix()
            chunks.extend(parse_file(relative_path, source_bytes))
            files_parsed += 1

    return _RepositoryWalk(
        chunks=chunks,
        files_seen=files_seen,
        files_parsed=files_parsed,
        dirs_walked=dirs_walked,
        commit_sha=commit_sha,
    )


def _walk_repository(
    *,
    repo_name: str,
    installation_id: int,
) -> _RepositoryWalk:
    """Synchronously verify access, download, extract, and parse a repository."""
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    integration = GithubIntegration(auth=app_auth)
    installation = integration.get_app_installation(installation_id)
    repo_selected = next(
        (repo for repo in installation.get_repos() if repo.full_name == repo_name),
        None,
    )
    if repo_selected is None:
        raise PermanentRepositoryIngestionError("Repository not found")

    token = integration.get_access_token(installation_id).token
    with tempfile.TemporaryDirectory() as temp_directory:
        temp_path = Path(temp_directory)
        archive_path = temp_path / "repo.tar.gz"
        _download_tarball(repo_name, token, archive_path)
        repo_root, commit_sha = _extract_tarball(archive_path, temp_path)
        return _parse_repository(repo_root, commit_sha)


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
            "dirs_walked=%d chunks=%d commit_sha=%s elapsed=%.2fs",
            repo_name,
            files_seen,
            files_parsed,
            dirs_walked,
            len(all_chunks),
            walk.commit_sha,
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
    except RequestException as error:
        session.rollback()
        logger.warning(
            "transient ingest failure | phase=%s repo=%r error_type=%s",
            phase,
            repo_name,
            type(error).__name__,
        )
        raise TransientRepositoryIngestionError(
            "GitHub tarball download failed"
        ) from error
    except (tarfile.TarError, EmbeddingError, exc.OperationalError) as error:
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
