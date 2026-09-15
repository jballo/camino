import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exc
from sqlmodel import select

from app.agent import answer_question
from app.db import SessionDep
from app.models.code import RepoIndexState
from app.rate_limit import AGENT_ASK_RATE_LIMIT
from app.security import get_authenticated_user_id
from app.services.embeddings import EmbeddingError
from app.services.jobs import normalize_repository_name
from app.services.repo_access import (
    RepoAccessDenied,
    RepoAccessUnavailable,
    authorize_index_read,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    repoName: str
    ref: str | None = None


class SourceResponse(BaseModel):
    chunk_id: int
    repo_name: str
    file_path: str
    symbol_name: str
    symbol_type: str
    language: str
    start_line: int
    end_line: int
    score: float


class AskResponse(BaseModel):
    answer: str
    sources: list[SourceResponse]


@router.post("/ask", dependencies=[Depends(AGENT_ASK_RATE_LIMIT)])
async def ask_agent(
    payload: AskBody,
    session: SessionDep,
    auth_user_id: str = Depends(get_authenticated_user_id),
) -> AskResponse:
    try:
        statement = select(RepoIndexState).where(
            RepoIndexState.repo_name == normalize_repository_name(payload.repoName)
        )
        if payload.ref is not None:
            statement = statement.where(RepoIndexState.ref == payload.ref)
        states = session.exec(statement).all()
    except exc.SQLAlchemyError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")
    if not states:
        raise HTTPException(status_code=404, detail="Repository index not found")
    if payload.ref is None and len(states) != 1:
        raise HTTPException(
            status_code=409,
            detail="Repository has multiple indexed refs; specify ref",
        )
    index_state = states[0]
    try:
        authorize_index_read(session, auth_user_id, index_state)
    except RepoAccessDenied:
        raise HTTPException(status_code=404, detail="Repository index not found")
    except RepoAccessUnavailable:
        raise HTTPException(status_code=502, detail="Github access check failed")

    try:
        result = await answer_question(
            session,
            question=payload.question,
            repo_name=payload.repoName,
            ref=index_state.ref,
        )
    except EmbeddingError as e:
        logger.error(
            "agent embedding failure | question=%r repo=%r: %s",
            payload.question, payload.repoName, e,
        )
        raise HTTPException(status_code=502, detail="Embedding service unavailable")
    except exc.SQLAlchemyError as e:
        session.rollback()
        logger.error(
            "agent database failure | question=%r repo=%r: %s",
            payload.question, payload.repoName, e,
        )
        raise HTTPException(status_code=502, detail="Database service error")
    except Exception:
        logger.exception(
            "agent unexpected failure | question=%r repo=%r",
            payload.question, payload.repoName,
        )
        raise HTTPException(status_code=500, detail="Internal agent error")

    return AskResponse(
        answer=result.answer,
        sources=[
            SourceResponse(
                chunk_id=s.chunk_id,
                repo_name=s.repo_name,
                file_path=s.file_path,
                symbol_name=s.symbol_name,
                symbol_type=s.symbol_type,
                language=s.language,
                start_line=s.start_line,
                end_line=s.end_line,
                score=s.score,
            )
            for s in result.sources
        ],
    )
