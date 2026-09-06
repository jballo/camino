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
from app.services.parser import MAX_FILE_BYTES
from app.services.repository_ingestion import (
    PermanentRepositoryIngestionError,
    TransientRepositoryIngestionError,
    _extract_tarball,
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
            "app.services.repository_ingestion.GithubIntegration",
            return_value=integration,
        ),
        integration,
    )


def _repository_installation(repo_name: str = "org/repo"):
    installation = MagicMock()
    installation.get_repos.return_value = [MagicMock(full_name=repo_name)]
    return installation


async def test_ingestion_commits_atomic_replace_and_returns_counts():
    session = MagicMock()
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
            repo_name="org/repo",
            installation_id=123,
        )

    assert result == {"chunks_inserted": 1, "embeddings_created": 1}
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/example.py"]
    integration.get_access_token.assert_called_once_with(123)
    request_kwargs = get.call_args.kwargs
    assert request_kwargs["stream"] is True
    assert request_kwargs["allow_redirects"] is True
    assert request_kwargs["headers"]["Authorization"] == "Bearer installation-token"
    assert response.closed is True
    session.commit.assert_called_once_with()
    session.rollback.assert_not_called()


async def test_download_extract_and_parse_run_outside_the_event_loop_thread():
    session = MagicMock()
    installation = _repository_installation()
    integration = MagicMock()
    integration.get_access_token.return_value.token = "installation-token"
    walk_threads: list[int] = []

    def get_installation(_installation_id: int):
        walk_threads.append(threading.get_ident())
        return installation

    integration.get_app_installation.side_effect = get_installation
    event_loop_thread = threading.get_ident()

    with (
        patch(
            "app.services.repository_ingestion.GithubIntegration",
            return_value=integration,
        ),
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(_tarball({})),
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
        )

    assert result == {"chunks_inserted": 1, "embeddings_created": 1}
    chunk_models = session.add_all.call_args_list[0].args[0]
    assert [chunk.file_path for chunk in chunk_models] == ["src/keep.py"]


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
            )

    session.rollback.assert_called_once_with()


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (503, TransientRepositoryIngestionError),
        (404, PermanentRepositoryIngestionError),
    ],
)
async def test_download_http_status_is_classified(status_code, expected_error):
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(b"", status_code=status_code),
        ),
        pytest.raises(expected_error, match=f"status {status_code}"),
    ):
        await ingest_repository(
            session,
            repo_name="org/repo",
            installation_id=123,
        )

    session.rollback.assert_called_once_with()


async def test_missing_repository_is_permanent():
    session = MagicMock()
    installation = MagicMock()
    installation.get_repos.return_value = []
    github_patch, integration = _github(installation)

    with github_patch:
        with pytest.raises(
            PermanentRepositoryIngestionError,
            match="Repository not found",
        ):
            await ingest_repository(
                session,
                repo_name="org/missing",
                installation_id=123,
            )

    integration.get_access_token.assert_not_called()
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
        )

    session.rollback.assert_called_once_with()


async def test_embedding_failure_is_transient():
    session = MagicMock()
    github_patch, _ = _github(_repository_installation())

    with (
        github_patch,
        patch(
            "app.services.repository_ingestion.requests.get",
            return_value=_StreamingResponse(_tarball({})),
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
            )

    session.rollback.assert_called_once_with()
