from fastapi import APIRouter, Depends, HTTPException
from github import Auth, GithubException, GithubIntegration
from pydantic import BaseModel, Field
from sqlalchemy import exc, text
from sqlmodel import delete, select

import logging
import os

logger = logging.getLogger(__name__)

from app.services.embeddings import (
    EMBED_DIMENSIONS,
    EMBED_MODEL,
    EmbeddingError,
    build_embedding_text,
    embed_all,
)
from app.models.code import CodeChunkEmbedding, CodeChunkModel
from app.config import settings
from app.db import SessionDep
from app.models.github_connection import GithubConnections
from app.security import verify_api_key
from app.services.parser import LANGUAGES, MAX_FILE_BYTES, SKIP_DIRS, parse_file

from app.services.search import hybrid_search


router = APIRouter()

class SearchBody(BaseModel):
    query: str
    repoName: str
    limit: int = Field(default=10, ge=1, le=100)


class SearchResultResponse(BaseModel):
    chunk_id: int
    repo_name: str
    file_path: str
    symbol_name: str
    symbol_type: str
    language: str
    start_line: int
    end_line: int
    source_code: str
    signature: str
    docstring: str | None
    score: float



class RepoIngestBody(BaseModel):
    repoName: str
    userId: str


@router.get("/{userId}", dependencies=[Depends(verify_api_key)])
async def list_repositories(userId: str, session: SessionDep) -> list[str]:
    try:
        statement = select(GithubConnections).where(GithubConnections.userId == userId)
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        gi = GithubIntegration(auth=app_auth)
        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()
        return [repo.full_name for repo in repos]
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.post("/ingest", dependencies=[Depends(verify_api_key)])
async def process_repository(payload: RepoIngestBody, session: SessionDep):
    try:
        statement = select(GithubConnections).where(
            GithubConnections.userId == payload.userId
        )
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Github connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(
            app_id=settings.gh_app_id, private_key=settings.gh_app_private_key
        )
        gi = GithubIntegration(auth=app_auth)
        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()

        repo_selected = None
        for repo in repos:
            if repo.full_name == payload.repoName:
                repo_selected = repo
                break

        if repo_selected is None:
            raise HTTPException(status_code=404, detail="Repo not found")

        contents = repo_selected.get_contents("")
        all_chunks = []
        while contents:
            file_content = contents.pop(0)

            path_parts = file_content.path.replace("\\", "/").split("/")
            if any(part in SKIP_DIRS for part in path_parts):
                continue

            if file_content.type == "dir":
                contents.extend(repo_selected.get_contents(file_content.path))
                continue

            if file_content.type != "file":
                continue

            ext = os.path.splitext(file_content.path)[1]
            if ext not in LANGUAGES:
                continue

            if file_content.size and file_content.size > MAX_FILE_BYTES:
                continue

            try:
                source_bytes = file_content.decoded_content
            except AssertionError:
                continue

            if not source_bytes:
                continue

            chunks = parse_file(file_content.path, source_bytes)
            all_chunks.extend(chunks)

        try:
            texts = [build_embedding_text(c) for c in all_chunks]
            vectors = await embed_all(texts)
        except EmbeddingError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Failed to generate embeddings: {str(e)}"
            )

        try:
            session.exec(
                delete(CodeChunkModel).where(CodeChunkModel.repo_name == payload.repoName)
            )

            chunk_models = [
                CodeChunkModel.from_parsed(c, repo_name=payload.repoName)
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

            session.exec(text("""
                UPDATE code_chunks
                SET search_vector = 
                    setweight(to_tsvector('simple', coalesce(symbol_name, '')), 'A') ||
                    setweight(to_tsvector('simple', replace(replace(file_path, '/', ' '), '.', ' ')), 'B') ||
                    setweight(to_tsvector('english', coalesce(docstring, '')), 'C')
                WHERE repo_name = :repo_name AND search_vector IS NULL
            """).bindparams(repo_name=payload.repoName))

            session.commit()
            return {
                "chunks_inserted": len(chunk_models),
                "embeddings_created": len(embedding_models)    
            }
        except exc.IntegrityError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Database integrity error during ingestion: {str(e)}",
            )
        except exc.SQLAlchemyError:
            session.rollback()
            raise HTTPException(status_code=500, detail="Database error")
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")


@router.post("/search", dependencies=[Depends(verify_api_key)])
async def search_repository(
    payload: SearchBody, session: SessionDep
) -> list[SearchResultResponse]:
    try:
        results = await hybrid_search(
            session, payload.query, payload.repoName, limit=payload.limit
        )
    except EmbeddingError as e:
        logger.error(
            "Embedding service failed during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Embedding service unavailable")
    except exc.SQLAlchemyError as e:
        session.rollback()
        logger.error(
            "Database error during search: %s | query=%r repo=%r",
            e, payload.query, payload.repoName,
        )
        raise HTTPException(status_code=502, detail="Database service error")
    except Exception as e:
        logger.exception(
            "Unexpected error during search | query=%r repo=%r",
            payload.query, payload.repoName,
        )
        raise HTTPException(status_code=500, detail="Internal search error")

    if not results:
        return []
    return [
        SearchResultResponse(
            chunk_id=r.chunk_id,
            repo_name=r.repo_name,
            file_path=r.file_path,
            symbol_name=r.symbol_name,
            symbol_type=r.symbol_type,
            language=r.language,
            start_line=r.start_line,
            end_line=r.end_line,
            source_code=r.source_code,
            signature=r.signature,
            docstring=r.docstring,
            score=r.score,
        )
        for r in results
    ]