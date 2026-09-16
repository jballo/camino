"""Ingest a local code repository into Postgres using the production pipeline.

This mirrors ``app.services.repository_ingestion.ingest_repository`` (same parser,
embedding text, and RRF-ready ``search_vector`` population) but reads files from
the local filesystem instead of the GitHub API. It exists so the retrieval eval
can exercise the *real* ``hybrid_search`` path against a known codebase (FastAPI)
without needing a GitHub App installation.

Usage:
    uv run python -m eval.ingest_local --path eval/.data/fastapi \
        --repo tiangolo/fastapi --ref 0.115.6
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

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
from app.services.jobs import normalize_repository_name
from app.services.parser import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, parse_file
from app.services.search_index import (
    populate_search_vector_sql,
    rebuild_search_vector,
)

# Live eval generation still needs a placeholder installation id for GitHub-bound
# interfaces. The shared code index itself is keyed only by repository and ref.
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


def _git_commit_sha(root: Path) -> str | None:
    """Return the checked-out commit for provenance when ``root`` is a Git repo."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    commit_sha = result.stdout.strip()
    return commit_sha or None


async def ingest(
    root: Path,
    repo_name: str,
    ref: str = FIXTURE_REPO_VERSION,
    visibility: str = "public",
) -> dict:
    started = time.monotonic()
    engine = create_engine(settings.database_url)
    repo_name = normalize_repository_name(repo_name)
    generation = uuid4().hex
    commit_sha = _git_commit_sha(root)

    all_chunks = []
    files_parsed = 0
    for full in _iter_source_files(root):
        # store paths relative to the repo root so they look like GitHub paths
        rel_path = full.relative_to(root).as_posix()
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
                CodeChunkModel.ref == ref,
            )
        )

        chunk_models = [
            CodeChunkModel.from_parsed(
                c,
                repo_name=repo_name,
                ref=ref,
                generation=generation,
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
            for chunk, vector in zip(chunk_models, vectors, strict=True)
        ]
        session.add_all(embedding_models)

        session.exec(
            text(populate_search_vector_sql(only_null=True)).bindparams(
                repo_name=repo_name,
                ref=ref,
                generation=generation,
            )
        )
        session.exec(
            text("""
                INSERT INTO repo_index_state (
                    repo_name,
                    ref,
                    visibility,
                    active_generation,
                    indexed_sha,
                    indexed_at
                )
                VALUES (
                    :repo_name,
                    :ref,
                    :visibility,
                    :generation,
                    :commit_sha,
                    now()
                )
                ON CONFLICT (repo_name, ref)
                DO UPDATE SET
                    visibility = EXCLUDED.visibility,
                    active_generation = EXCLUDED.active_generation,
                    indexed_sha = EXCLUDED.indexed_sha,
                    indexed_at = EXCLUDED.indexed_at
            """).bindparams(
                repo_name=repo_name,
                ref=ref,
                visibility=visibility,
                generation=generation,
                commit_sha=commit_sha,
            )
        )

        session.commit()
        inserted = len(chunk_models)

    print(
        f"ingested: chunks={inserted} embeddings={len(embedding_models)} "
        f"repo={repo_name!r} ref={ref!r} commit_sha={commit_sha or 'unknown'} "
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
        "--ref",
        default=FIXTURE_REPO_VERSION,
        help="Git ref represented by the local checkout.",
    )
    parser.add_argument(
        "--visibility",
        choices=("public", "private"),
        default="public",
        help="Repository visibility recorded in the index state.",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Do not auto-clone the pinned fixture if the path is missing.",
    )
    parser.add_argument(
        "--rebuild-fts",
        action="store_true",
        help="Recompute search_vector for already-ingested chunks and exit "
        "(no parsing, no embedding). Use after changing the FTS tokenization.",
    )
    args = parser.parse_args()

    if args.rebuild_fts:
        engine = create_engine(settings.database_url)
        with Session(engine) as session:
            rebuild_search_vector(session, args.repo, args.ref)
        print(
            f"rebuilt search_vector: repo={args.repo!r} "
            f"ref={args.ref!r}"
        )
        return

    root = Path(args.path)
    if not args.no_clone:
        ensure_fixture(root, FIXTURE_REPO_URL, FIXTURE_REPO_VERSION)

    root = root.resolve()
    if not root.exists():
        raise SystemExit(
            f"path does not exist: {root} "
            "(pass without --no-clone to auto-fetch the fixture)"
        )

    asyncio.run(ingest(root, args.repo, args.ref, args.visibility))


if __name__ == "__main__":
    main()
