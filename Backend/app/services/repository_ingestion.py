from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from uuid import uuid4

from github import Auth, GithubException, GithubIntegration
import requests
from requests.exceptions import RequestException
from sqlalchemy import exc, text
from sqlmodel import Session

from app.config import settings
from app.models.code import CodeChunkEmbedding, CodeChunkModel
from app.services.embeddings import (
    EMBED_DIMENSIONS,
    EMBED_MODEL,
    EmbeddingError,
    build_embedding_text,
    embed_all,
)
from app.services.jobs import normalize_repository_name
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

_CLEANUP_STAGED_SQL = text("""
    DELETE FROM code_chunks AS c
    WHERE c.repo_name = :repo_name
      AND c.installation_id = :installation_id
      AND NOT EXISTS (
          SELECT 1
          FROM repo_index_state AS s
          WHERE s.repo_name = c.repo_name
            AND s.installation_id = c.installation_id
            AND s.active_generation = c.generation
      )
""")

_PUBLISH_GENERATION_SQL = text("""
    INSERT INTO repo_index_state (
        installation_id,
        repo_name,
        active_generation
    )
    VALUES (:installation_id, :repo_name, :generation)
    ON CONFLICT (installation_id, repo_name)
    DO UPDATE SET active_generation = EXCLUDED.active_generation
""")

_DELETE_OLD_GENERATIONS_SQL = text("""
    DELETE FROM code_chunks
    WHERE repo_name = :repo_name
      AND installation_id = :installation_id
      AND generation <> :generation
""")


class RepositoryIngestionError(RuntimeError):
    """Base error raised by repository ingestion."""


class TransientRepositoryIngestionError(RepositoryIngestionError):
    """An upstream or database failure that is safe to retry."""


class PermanentRepositoryIngestionError(RepositoryIngestionError):
    """Invalid input or deterministic failure that should not be retried."""


class IngestionCancelledError(RepositoryIngestionError):
    """Ingestion stopped because its job is no longer valid or owned."""


@dataclass
class _RepositoryWalkStats:
    files_seen: int
    files_parsed: int
    dirs_walked: int


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
        extracted_bytes = 0
        entry_count = 0
        for member in archive:
            entry_count += 1
            if entry_count > settings.ingest_max_archive_entries:
                raise PermanentRepositoryIngestionError(
                    "Repository archive contains too many entries"
                )

            extracted_bytes += max(member.size, 0)
            if extracted_bytes > settings.ingest_max_extracted_bytes:
                raise PermanentRepositoryIngestionError(
                    "Repository archive expands beyond the allowed size"
                )

            archive.extract(member, destination, filter="data")

    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1:
        raise tarfile.ReadError("GitHub tarball does not have one root directory")

    repo_root = roots[0]
    _, separator, commit_sha = repo_root.name.rpartition("-")
    if not separator or not commit_sha:
        raise tarfile.ReadError("GitHub tarball root does not contain a commit SHA")
    return repo_root, commit_sha


def _iter_file_chunks(
    root: Path,
    stats: _RepositoryWalkStats | None = None,
) -> Iterator[list[CodeChunk]]:
    """Parse supported source files lazily, yielding one file at a time."""
    walk_stats = stats or _RepositoryWalkStats(0, 0, 0)
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) != root:
            walk_stats.dirs_walked += 1
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

            walk_stats.files_seen += 1
            try:
                source_bytes = full_path.read_bytes()
            except OSError:
                continue
            if not source_bytes:
                continue

            relative_path = full_path.relative_to(root).as_posix()
            chunks = parse_file(relative_path, source_bytes)
            walk_stats.files_parsed += 1
            yield chunks


def _prepare_repository(
    repo_name: str,
    installation_id: int,
    temp_path: Path,
) -> tuple[Path, str]:
    """Synchronously verify access, download, and extract one snapshot."""
    app_auth = Auth.AppAuth(
        app_id=settings.gh_app_id,
        private_key=settings.gh_app_private_key,
    )
    integration = GithubIntegration(auth=app_auth)
    installation = integration.get_app_installation(installation_id)
    normalized_repo_name = normalize_repository_name(repo_name)
    repo_selected = next(
        (
            repo
            for repo in installation.get_repos()
            if normalize_repository_name(repo.full_name) == normalized_repo_name
        ),
        None,
    )
    if repo_selected is None:
        raise PermanentRepositoryIngestionError("Repository not found")

    token = integration.get_access_token(installation_id).token
    archive_path = temp_path / "repo.tar.gz"
    _download_tarball(repo_selected.full_name, token, archive_path)
    return _extract_tarball(archive_path, temp_path)


async def _persist_wave(
    session: Session,
    chunks: list[CodeChunk],
    *,
    repo_name: str,
    installation_id: int,
    generation: str,
    ensure_owned: Callable[[Session], None] | None = None,
) -> int:
    """Embed and commit one bounded wave, returning its row count."""
    vectors = await embed_all([build_embedding_text(chunk) for chunk in chunks])
    if ensure_owned is not None:
        ensure_owned(session)
    chunk_models = [
        CodeChunkModel.from_parsed(
            chunk,
            repo_name=repo_name,
            installation_id=installation_id,
            generation=generation,
        )
        for chunk in chunks
    ]
    session.add_all(chunk_models)
    session.flush()
    session.add_all(
        [
            CodeChunkEmbedding(
                chunk_id=chunk.id,
                model_name=EMBED_MODEL,
                dimension=EMBED_DIMENSIONS,
                embedding=vector,
            )
            for chunk, vector in zip(chunk_models, vectors, strict=True)
        ]
    )
    session.commit()
    return len(chunk_models)


async def ingest_repository(
    session: Session,
    *,
    repo_name: str,
    installation_id: int,
    ensure_owned: Callable[[Session], None] | None = None,
    finalize_publication: (
        Callable[[Session, dict[str, int]], None] | None
    ) = None,
) -> dict[str, int]:
    """Stage an index, then atomically publish it and finalize its owner."""
    repo_name = normalize_repository_name(repo_name)
    phase = "init"
    stats = _RepositoryWalkStats(0, 0, 0)
    generation = uuid4().hex
    chunks_inserted = 0
    embeddings_created = 0
    wave_number = 0
    started = time.monotonic()

    try:
        logger.info(
            "ingest start | repo=%r installation=%s generation=%s",
            repo_name,
            installation_id,
            generation,
        )

        phase = "cleanup"
        if ensure_owned is not None:
            ensure_owned(session)
        session.execute(
            _CLEANUP_STAGED_SQL,
            {
                "repo_name": repo_name,
                "installation_id": installation_id,
            },
        )
        session.commit()

        with tempfile.TemporaryDirectory() as temp_directory:
            phase = "github_auth"
            repo_root, commit_sha = await asyncio.to_thread(
                _prepare_repository,
                repo_name,
                installation_id,
                Path(temp_directory),
            )

            phase = "walk"
            file_chunks_iter = _iter_file_chunks(repo_root, stats)
            wave: list[CodeChunk] = []
            while True:
                file_chunks = await asyncio.to_thread(
                    next,
                    file_chunks_iter,
                    None,
                )
                if file_chunks is None:
                    break
                if not file_chunks:
                    continue

                chunks_inserted += len(file_chunks)
                if chunks_inserted > settings.ingest_max_chunks:
                    raise PermanentRepositoryIngestionError(
                        "Repository exceeds the maximum indexable size"
                    )
                for chunk in file_chunks:
                    wave.append(chunk)
                    if len(wave) < settings.ingest_wave_chunks:
                        continue

                    phase = "wave"
                    wave_number += 1
                    persisted = await _persist_wave(
                        session,
                        wave,
                        repo_name=repo_name,
                        installation_id=installation_id,
                        generation=generation,
                        ensure_owned=ensure_owned,
                    )
                    embeddings_created += persisted
                    logger.info(
                        "ingest wave complete | repo=%r generation=%s wave=%d "
                        "wave_chunks=%d cumulative_chunks=%d elapsed=%.2fs",
                        repo_name,
                        generation,
                        wave_number,
                        persisted,
                        chunks_inserted,
                        time.monotonic() - started,
                    )
                    wave = []
                    phase = "walk"

            if wave:
                phase = "wave"
                wave_number += 1
                persisted = await _persist_wave(
                    session,
                    wave,
                    repo_name=repo_name,
                    installation_id=installation_id,
                    generation=generation,
                    ensure_owned=ensure_owned,
                )
                embeddings_created += persisted
                logger.info(
                    "ingest wave complete | repo=%r generation=%s wave=%d "
                    "wave_chunks=%d cumulative_chunks=%d elapsed=%.2fs",
                    repo_name,
                    generation,
                    wave_number,
                    persisted,
                    chunks_inserted,
                    time.monotonic() - started,
                )

        logger.info(
            "ingest walk complete | repo=%r files_seen=%d files_parsed=%d "
            "dirs_walked=%d chunks=%d commit_sha=%s elapsed=%.2fs",
            repo_name,
            stats.files_seen,
            stats.files_parsed,
            stats.dirs_walked,
            chunks_inserted,
            commit_sha,
            time.monotonic() - started,
        )

        phase = "search_vector"
        if ensure_owned is not None:
            ensure_owned(session)
        session.execute(
            text(populate_search_vector_sql(only_null=True)).bindparams(
                repo_name=repo_name,
                installation_id=installation_id,
                generation=generation,
            )
        )
        session.commit()

        result = {
            "chunks_inserted": chunks_inserted,
            "embeddings_created": embeddings_created,
        }
        phase = "swap"
        if ensure_owned is not None:
            ensure_owned(session)
        publish_params = {
            "repo_name": repo_name,
            "installation_id": installation_id,
            "generation": generation,
        }
        session.execute(_PUBLISH_GENERATION_SQL, publish_params)
        session.execute(_DELETE_OLD_GENERATIONS_SQL, publish_params)
        if finalize_publication is not None:
            finalize_publication(session, result)
        session.commit()

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
            stats.files_seen,
            time.monotonic() - started,
        )
        raise PermanentRepositoryIngestionError(
            "Internal ingestion error"
        ) from error
