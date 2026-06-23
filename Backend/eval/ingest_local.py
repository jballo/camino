"""Ingest a local code repository into Postgres using the production pipeline.

This mirrors ``app.api.repositories.process_repository`` exactly (same parser,
same embedding text, same RRF-ready ``search_vector`` population) but reads files
from the local filesystem instead of the GitHub API. It exists so the retrieval
eval can exercise the *real* ``hybrid_search`` path against a known codebase
(FastAPI) without needing a GitHub App installation.

Usage:
    uv run python -m eval.ingest_local --path eval/.data/fastapi \
        --repo tiangolo/fastapi
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import time
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, create_engine, delete

from app.config import settings
from app.models.code import CodeChunkEmbedding, CodeChunkModel
from app.services.embeddings import (
    EMBED_DIMENSIONS,
    EMBED_MODEL,
    build_embedding_text,
    embed_all,
)
from app.services.parser import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, parse_file

# A sentinel installation id reserved for eval fixtures so it never collides
# with real GitHub installation ids.
EVAL_INSTALLATION_ID = 999_999_999

# The eval fixture is checked out (not committed) so it can be reproduced from a
# fresh clone. Pinned to the version the golden dataset was hand-labeled against;
# keep this in sync with ``repo_version`` in golden_dataset.json.
FIXTURE_REPO_URL = "https://github.com/fastapi/fastapi.git"
FIXTURE_REPO_VERSION = "0.115.6"
DEFAULT_FIXTURE_PATH = Path(__file__).parent / ".data" / "fastapi"


def ensure_fixture(path: Path, url: str, version: str) -> None:
    """Shallow-clone the pinned fixture repo if it isn't already present.

    The fixture source lives under ``eval/.data/`` which is gitignored, so a
    fresh checkout of this project won't have it. Cloning on demand keeps the
    eval reproducible without committing a third-party repo.
    """
    if path.exists() and any(path.iterdir()):
        print(f"fixture present: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"cloning fixture {url}@{version} -> {path}")
    subprocess.run(
        [
            "git", "clone", "--depth", "1", "--branch", version,
            url, str(path),
        ],
        check=True,
    )


def _iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skipped dirs in place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1]
            if ext not in LANGUAGES:
                continue
            full = Path(dirpath) / name
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield full


async def ingest(
    root: Path,
    repo_name: str,
    installation_id: int = EVAL_INSTALLATION_ID,
) -> dict:
    started = time.monotonic()
    engine = create_engine(settings.database_url)

    all_chunks = []
    files_parsed = 0
    for full in _iter_source_files(root):
        # store paths relative to the repo root so they look like GitHub paths
        rel_path = str(full.relative_to(root))
        try:
            source_bytes = full.read_bytes()
        except OSError:
            continue
        if not source_bytes:
            continue
        chunks = parse_file(rel_path, source_bytes)
        all_chunks.extend(chunks)
        files_parsed += 1

    print(
        f"parsed: files={files_parsed} chunks={len(all_chunks)} "
        f"elapsed={time.monotonic() - started:.1f}s"
    )

    texts = [build_embedding_text(c) for c in all_chunks]
    vectors = await embed_all(texts)
    print(f"embedded: vectors={len(vectors)} elapsed={time.monotonic() - started:.1f}s")

    with Session(engine) as session:
        session.exec(
            delete(CodeChunkModel).where(
                CodeChunkModel.repo_name == repo_name,
                CodeChunkModel.installation_id == installation_id,
            )
        )

        chunk_models = [
            CodeChunkModel.from_parsed(
                c, repo_name=repo_name, installation_id=installation_id
            )
            for c in all_chunks
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
            for chunk, vector in zip(chunk_models, vectors)
        ]
        session.add_all(embedding_models)

        session.exec(
            text(
                """
                UPDATE code_chunks
                SET search_vector =
                    setweight(to_tsvector('simple', coalesce(symbol_name, '')), 'A') ||
                    setweight(to_tsvector('simple', replace(replace(file_path, '/', ' '), '.', ' ')), 'B') ||
                    setweight(to_tsvector('english', coalesce(docstring, '')), 'C')
                WHERE repo_name = :repo_name
                  AND installation_id = :installation_id
                  AND search_vector IS NULL
                """
            ).bindparams(repo_name=repo_name, installation_id=installation_id)
        )

        session.commit()
        inserted = len(chunk_models)

    print(
        f"ingested: chunks={inserted} embeddings={len(embedding_models)} "
        f"repo={repo_name!r} installation_id={installation_id} "
        f"elapsed={time.monotonic() - started:.1f}s"
    )
    return {"chunks": inserted, "embeddings": len(embedding_models)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Path to the local repo root to ingest.",
    )
    parser.add_argument(
        "--repo",
        default="tiangolo/fastapi",
        help="Logical repo_name to store chunks under.",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=EVAL_INSTALLATION_ID,
        help="Installation id to store chunks under.",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Do not auto-clone the pinned fixture if the path is missing.",
    )
    args = parser.parse_args()

    root = Path(args.path)
    if not args.no_clone:
        ensure_fixture(root, FIXTURE_REPO_URL, FIXTURE_REPO_VERSION)

    root = root.resolve()
    if not root.exists():
        raise SystemExit(
            f"path does not exist: {root} "
            "(pass without --no-clone to auto-fetch the fixture)"
        )

    asyncio.run(ingest(root, args.repo, args.installation_id))


if __name__ == "__main__":
    main()
