from io import BytesIO
from pathlib import Path
import tarfile
import tempfile
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from requests.exceptions import Timeout

from app.config import settings
from app.services.embeddings import EmbeddingError
from app.services.parser import CodeChunk, MAX_FILE_BYTES
from app.services.repository_ingestion import (
    IngestionCancelledError,
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
    _extract_tarball,
    _persist_wave,
    ingest_repository,
)


class _StreamingResponse:
    def __init__(self, body: bytes, status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def json(self):
        return {"full_name": "org/repo", "private": False}

    def close(self):
        self.closed = True


def _tarball(
    files: dict[str, bytes],
    *,
    root: str = "org-repo-deadbeef",
) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)
        for relative_path, content in files.items():
            info = tarfile.TarInfo(f"{root}/{relative_path}")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
    return buffer.getvalue()


def _github(installation):
    integration = MagicMock()
    integration.get_app_installation.return_value = installation
    integration.get_access_token.return_value.token = "installation-token"
    return (
        patch(
            "app.services.repository_ingestion.github_integration",
            return_value=integration,
        ),
        integration,
    )


def _repository_installation(repo_name: str = "org/repo"):
    installation = MagicMock()
    installation.get_repos.return_value = [MagicMock(full_name=repo_name)]
    return installation


def _chunk(file_path: str, *, source_code: str = "def example():\n    pass"):
    return CodeChunk(
        file_path=file_path,
        symbol_name="example",
        symbol_type="function",
        language="py",
        start_line=1,
        end_line=2,
        source_code=source_code,
        signature="def example()",
        docstring=None,
        parent_class=None,
    )


async def test_ingestion_stages_publishes_and_returns_counts():
    session = MagicMock()
    ensure_owned = MagicMock()

    def assert_finalized_before_publish_commit(*_args):
        assert session.commit.call_count == 3

    finalize_publication = MagicMock(
        side_effect=assert_finalized_before_publish_commit
    )
    github_patch, integration = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/example.py": (
                    b"def greet(name: str) -> str:\n"
                    b'    return f"Hello, {name}"\n'
                )
            }
        )
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ) as get,
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
    ):
        result = await ingest_repository(
            session,
            repo_name="Org/Repo",
            installation_id=123,
            ref="feature/CaseSensitive",
            ensure_owned=ensure_owned,
            finalize_publication=finalize_publication,
        )

    assert result == {
        "chunks_inserted": 1,
        "embeddings_created": 1,
        "files_skipped": 0,
    }
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/example.py"]
    assert [chunk.repo_name for chunk in chunk_models] == ["org/repo"]
    integration.get_access_token.assert_called_once_with(123)
    assert get.call_args.args[0] == (
        "https://api.github.com/repos/org/repo/tarball/feature%2FCaseSensitive"
    )
    assert get.call_count == 2
    request_kwargs = get.call_args.kwargs
    assert request_kwargs["stream"] is True
    assert request_kwargs["allow_redirects"] is True
    assert request_kwargs["headers"]["Authorization"] == "Bearer installation-token"
    assert response.closed is True
    assert chunk_models[0].generation
    # Cleanup, one wave, search-vector population, then atomic publish.
    assert session.commit.call_count == 4
    assert ensure_owned.call_count == session.commit.call_count
    ensure_owned.assert_called_with(session)
    executed_sql = [" ".join(str(call.args[0]).split()) for call in session.execute.call_args_list]
    assert executed_sql[0].startswith("DELETE FROM code_chunks AS c")
    assert "INSERT INTO repo_index_state" in executed_sql[-2]
    publish_params = session.execute.call_args_list[-2].args[1]
    assert publish_params["commit_sha"] == "deadbeef"
    assert publish_params["ref"] == "feature/CaseSensitive"
    assert publish_params["visibility"] == "public"
    assert executed_sql[-1].startswith("DELETE FROM code_chunks")
    finalize_publication.assert_called_once_with(session, result)
    session.rollback.assert_not_called()


async def test_failed_job_finalization_rolls_back_publication():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    finalize_publication = MagicMock(
        side_effect=IngestionCancelledError("job was reclaimed")
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(_tarball({})),
        ),
        pytest.raises(IngestionCancelledError, match="job was reclaimed"),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
            finalize_publication=finalize_publication,
        )

    # Cleanup and search vectors committed; publication did not.
    assert session.commit.call_count == 3
    finalize_publication.assert_called_once_with(
        session,
        {
            "chunks_inserted": 0,
            "embeddings_created": 0,
            "files_skipped": 0,
        },
    )
    session.rollback.assert_called_once_with()


async def test_download_extract_and_parse_run_outside_the_event_loop_thread():
    session = MagicMock()
    integration = MagicMock()
    integration.get_access_token.return_value.token = "installation-token"
    walk_threads: list[int] = []

    response = _StreamingResponse(_tarball({}))

    def get_repository(*_args, **_kwargs):
        walk_threads.append(threading.get_ident())
        return response

    event_loop_thread = threading.get_ident()

    with (
        patch(
            "app.services.repository_ingestion.github_integration",
            return_value=integration,
        ),
        patch(
            "app.services.repository_ingestion.requests.get",
            side_effect=get_repository,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert walk_threads
    assert walk_threads[0] != event_loop_thread


async def test_walk_filters_skipped_unsupported_and_oversized_files():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/keep.py": b"def keep():\n    return True\n",
                "node_modules/skip.py": b"def skip():\n    return False\n",
                "README.md": b"# Not source code\n",
                "src/oversized.py": b"x" * (MAX_FILE_BYTES + 1),
            }
        )
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result == {
        "chunks_inserted": 1,
        "embeddings_created": 1,
        "files_skipped": 0,
    }
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]


async def test_nul_file_is_skipped_and_rest_ingested(caplog):
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/keep.py": b"def keep():\n    return True\n",
                "src/evil.py": b"def evil():\n    return '\x00'\n",
            }
        )
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ) as embed,
        caplog.at_level("WARNING", logger="app.services.repository_ingestion"),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result == {
        "chunks_inserted": 1,
        "embeddings_created": 1,
        "files_skipped": 1,
    }
    embed.assert_awaited_once()
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]
    assert "src/evil.py" in caplog.text
    assert "binary (NUL bytes)" in caplog.text


async def test_invalid_utf8_file_is_skipped(caplog):
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/keep.py": b"def keep():\n    return True\n",
                "src/evil.py": b"def evil():\n    return '\xff'\n",
            }
        )
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
        caplog.at_level("WARNING", logger="app.services.repository_ingestion"),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result["files_skipped"] == 1
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]
    assert "src/evil.py" in caplog.text
    assert "invalid UTF-8" in caplog.text


async def test_vendored_and_nested_repo_dirs_are_pruned():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                ".repos/x/src/a.py": b"def nested():\n    return 1\n",
                "vendor/b.py": b"def vendored():\n    return 2\n",
                "src/keep.py": b"def keep():\n    return 3\n",
            }
        )
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result == {
        "chunks_inserted": 1,
        "embeddings_created": 1,
        "files_skipped": 0,
    }
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]


async def test_persist_wave_drops_nul_chunks_before_embedding(caplog):
    session = MagicMock()
    chunks = [
        _chunk("src/keep.py"),
        _chunk("src/evil.py", source_code="def evil():\n    return '\x00'"),
    ]

    with (
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ) as embed,
        caplog.at_level("WARNING", logger="app.services.repository_ingestion"),
    ):
        persisted = await _persist_wave(
            session,
            chunks,
            repo_name="org/repo",
            ref="main",
            generation="generation-1",
        )

    assert persisted == 1
    assert len(embed.await_args.args[0]) == 1
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]
    assert "src/evil.py" in caplog.text
    assert "field=source_code" in caplog.text


async def test_persist_wave_all_chunks_dropped_returns_zero(caplog):
    session = MagicMock()

    with (
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
        ) as embed,
        caplog.at_level("WARNING", logger="app.services.repository_ingestion"),
    ):
        persisted = await _persist_wave(
            session,
            [_chunk("src/evil.py\x00")],
            repo_name="org/repo",
            ref="main",
            generation="generation-1",
        )

    assert persisted == 0
    embed.assert_not_awaited()
    session.add_all.assert_not_called()
    session.commit.assert_not_called()
    assert "src/evil.py" in caplog.text
    assert "field=file_path" in caplog.text


async def test_flush_valueerror_is_permanent_with_file_context():
    session = MagicMock()
    session.flush.side_effect = ValueError(
        "A string literal cannot contain NUL (0x00) characters"
    )

    with (
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="src/example.py",
        ),
    ):
        await _persist_wave(
            session,
            [_chunk("src/example.py")],
            repo_name="org/repo",
            ref="main",
            generation="generation-1",
        )

    session.commit.assert_not_called()


async def test_download_failure_is_transient():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            side_effect=Timeout("timed out"),
        ),
    ):
        with pytest.raises(
            TransientRepositoryIngestionError,
            match="GitHub tarball download failed",
        ):
            await ingest_repository(
                session,
                repo_name="org/repo",
                installation_id=123,
                ref="main",
            )

    session.rollback.assert_called_once_with()


@pytest.mark.parametrize(
    ("status_code", "expected_error", "message"),
    [
        (503, TransientRepositoryIngestionError, "status 503"),
        (404, PermanentRepositoryIngestionError, "Repository not found"),
    ],
)
async def test_download_http_status_is_classified(
    status_code, expected_error, message
):
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(b"", status_code=status_code),
        ),
        pytest.raises(expected_error, match=message),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    session.rollback.assert_called_once_with()


async def test_missing_repository_is_permanent():
    session = MagicMock()
    github_patch, integration = _github(MagicMock())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(b"", status_code=404),
        ),
    ):
        with pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository not found",
        ):
            await ingest_repository(
                session,
                repo_name="org/missing",
                installation_id=123,
                ref="main",
            )

    integration.get_access_token.assert_called_once_with(123)
    session.rollback.assert_called_once_with()


async def test_tarball_over_cap_is_permanent_and_cleans_up_temp_directory():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    created_directories: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    class TrackingTemporaryDirectory:
        def __init__(self):
            self._temporary_directory = real_temporary_directory()

        def __enter__(self):
            path = self._temporary_directory.__enter__()
            created_directories.append(Path(path))
            return path

        def __exit__(self, *args):
            return self._temporary_directory.__exit__(*args)

    with (
        github_patch,
        patch.object(settings, "ingest_max_tarball_bytes", 10),
        patch(
            "app.services.repository_ingestion.tempfile.TemporaryDirectory",
            TrackingTemporaryDirectory,
        ),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(b"more than ten bytes"),
        ),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository too large to ingest",
        ),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert len(created_directories) == 1
    assert not created_directories[0].exists()
    session.rollback.assert_called_once_with()


def test_tarball_path_traversal_is_rejected(tmp_path):
    archive_path = tmp_path / "repo.tar.gz"
    extract_path = tmp_path / "extract"
    extract_path.mkdir()
    with tarfile.open(archive_path, mode="w:gz") as archive:
        info = tarfile.TarInfo("../outside.py")
        content = b"def escaped():\n    pass\n"
        info.size = len(content)
        archive.addfile(info, BytesIO(content))

    with pytest.raises(tarfile.OutsideDestinationError):
        _extract_tarball(archive_path, extract_path)

    assert not (tmp_path / "outside.py").exists()


def test_tarball_expanded_size_is_bounded(tmp_path):
    archive_path = tmp_path / "repo.tar.gz"
    archive_path.write_bytes(_tarball({"src/large.py": b"x" * 11}))
    extract_path = tmp_path / "extract"
    extract_path.mkdir()

    with (
        patch.object(settings, "ingest_max_extracted_bytes", 10),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="expands beyond the allowed size",
        ),
    ):
        _extract_tarball(archive_path, extract_path)


def test_tarball_entry_count_is_bounded(tmp_path):
    archive_path = tmp_path / "repo.tar.gz"
    archive_path.write_bytes(
        _tarball({"src/a.py": b"a", "src/b.py": b"b"})
    )
    extract_path = tmp_path / "extract"
    extract_path.mkdir()

    with (
        patch.object(settings, "ingest_max_archive_entries", 2),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="too many entries",
        ),
    ):
        _extract_tarball(archive_path, extract_path)


async def test_corrupt_archive_is_transient():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(b"not a tar archive"),
        ),
        pytest.raises(TransientRepositoryIngestionError),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    session.rollback.assert_called_once_with()


async def test_embedding_failure_is_transient():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(
                _tarball({"src/example.py": b"def example():\n    return True\n"})
            ),
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            side_effect=EmbeddingError("OpenAI unavailable"),
        ),
    ):
        with pytest.raises(TransientRepositoryIngestionError):
            await ingest_repository(
                session,
                repo_name="org/repo",
                installation_id=123,
                ref="main",
            )

    session.rollback.assert_called_once_with()


async def test_ingestion_commits_multiple_bounded_waves():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/a.py": b"def a():\n    return 1\n",
                "src/b.py": b"def b():\n    return 2\n",
                "src/c.py": b"def c():\n    return 3\n",
            }
        )
    )

    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 2),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            side_effect=[[[0.1], [0.2]], [[0.3]]],
        ) as embed,
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result == {
        "chunks_inserted": 3,
        "embeddings_created": 3,
        "files_skipped": 0,
    }
    assert [len(call.args[0]) for call in embed.await_args_list] == [2, 1]
    # Cleanup + two wave commits + search vectors + swap.
    assert session.commit.call_count == 5


async def test_single_file_chunks_are_split_into_bounded_waves():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    source = b"\n".join(
        f"def function_{index}():\n    return {index}\n".encode()
        for index in range(5)
    )
    wave_sizes: list[int] = []

    async def embed_wave(texts: list[str]) -> list[list[float]]:
        wave_sizes.append(len(texts))
        return [[0.1] for _ in texts]

    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 2),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(_tarball({"src/generated.py": source})),
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new=embed_wave,
        ),
    ):
        result = await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert result == {
        "chunks_inserted": 5,
        "embeddings_created": 5,
        "files_skipped": 0,
    }
    assert wave_sizes == [2, 2, 1]


async def test_permanent_failure_cleans_staged_generation():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/a.py": b"def a():\n    return 1\n",
                "src/b.py": b"def b():\n    return 2\n",
            }
        )
    )

    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 1),
        patch.object(settings, "ingest_max_chunks", 1),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.1]],
        ) as embed,
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository exceeds the maximum indexable size",
        ),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    embed.assert_awaited_once()
    # Initial cleanup, wave 1, and failed-generation cleanup committed.
    assert session.commit.call_count == 3
    executed_sql = [" ".join(str(call.args[0]).split()) for call in session.execute.call_args_list]
    assert not any("INSERT INTO repo_index_state" in sql for sql in executed_sql)
    failed_cleanup_call = session.execute.call_args_list[-1]
    assert "generation = :generation" in str(failed_cleanup_call.args[0])
    assert failed_cleanup_call.args[1]["generation"]
    assert failed_cleanup_call.args[1]["repo_name"] == "org/repo"
    assert failed_cleanup_call.args[1]["ref"] == "main"
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert failed_cleanup_call.args[1]["generation"] == chunk_models[0].generation
    session.rollback.assert_called_once_with()


async def test_transient_failure_cleans_staged_generation():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/a.py": b"def a():\n    return 1\n",
                "src/b.py": b"def b():\n    return 2\n",
            }
        )
    )

    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 1),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            side_effect=[[[0.1]], EmbeddingError("wave 2 failed")],
        ) as embed,
        pytest.raises(TransientRepositoryIngestionError, match="wave 2 failed"),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert embed.await_count == 2
    assert session.commit.call_count == 3
    executed_sql = [" ".join(str(call.args[0]).split()) for call in session.execute.call_args_list]
    assert not any("INSERT INTO repo_index_state" in sql for sql in executed_sql)
    failed_cleanup_call = session.execute.call_args_list[-1]
    assert "generation = :generation" in str(failed_cleanup_call.args[0])
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert failed_cleanup_call.args[1]["generation"] == chunk_models[0].generation
    session.rollback.assert_called_once_with()


async def test_cleanup_failure_does_not_mask_original_error():
    session = MagicMock()
    session.execute.side_effect = [
        MagicMock(),
        RuntimeError("cleanup database unavailable"),
    ]
    github_patch, _ = _github(_repository_installation())
    response = _StreamingResponse(
        _tarball(
            {
                "src/a.py": b"def a():\n    return 1\n",
                "src/b.py": b"def b():\n    return 2\n",
            }
        )
    )

    with (
        github_patch,
        patch.object(settings, "ingest_wave_chunks", 1),
        patch.object(settings, "ingest_max_chunks", 1),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=response,
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.1]],
        ),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository exceeds the maximum indexable size",
        ),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    assert session.rollback.call_count == 2
    assert session.commit.call_count == 2


async def test_internal_error_message_includes_phase():
    session = MagicMock()

    with (
        patch(
            "app.services.repository_ingestion._prepare_repository",
            side_effect=RuntimeError("unexpected"),
        ),
        pytest.raises(
            PermanentRepositoryIngestionError,
            match=r"Internal ingestion error \(phase=github_auth\)",
        ),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
        )

    failed_cleanup_call = session.execute.call_args_list[-1]
    assert "generation = :generation" in str(failed_cleanup_call.args[0])
    assert session.commit.call_count == 2


async def test_ownership_loss_before_wave_commit_prevents_publication():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())
    ensure_owned = MagicMock(
        side_effect=[None, IngestionCancelledError("lease lost")]
    )

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(
                _tarball({"src/example.py": b"def example():\n    return True\n"})
            ),
        ),
        patch(
            "app.services.repository_ingestion.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.1]],
        ),
        pytest.raises(IngestionCancelledError, match="lease lost"),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
            ref="main",
            ensure_owned=ensure_owned,
        )

    assert ensure_owned.call_count == 2
    assert session.commit.call_count == 2
    executed_sql = [
        " ".join(str(call.args[0]).split())
        for call in session.execute.call_args_list
    ]
    assert not any("INSERT INTO repo_index_state" in sql for sql in executed_sql)
    session.rollback.assert_called_once_with()
