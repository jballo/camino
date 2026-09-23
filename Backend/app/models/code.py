import datetime as dt
from typing import Any
from pgvector.sqlalchemy import HALFVEC, Vector
from sqlmodel import Column, Field, SQLModel
from sqlalchemy import DateTime, Integer, UniqueConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.config import settings
from app.services.embeddings import EMBED_DIMENSIONS
from app.services.parser import CodeChunk


EMBEDDING_COLUMN_TYPE = (
    HALFVEC(EMBED_DIMENSIONS)
    if settings.vector_type == "halfvec"
    else Vector(EMBED_DIMENSIONS)
)


class CodeChunkModel(SQLModel, table=True):
    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint(
            "repo_name",
            "ref",
            "generation",
            "file_path",
            "symbol_name",
            "start_line",
            name="uq_chunk_identity_gen",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    repo_name: str = Field(index=True)
    ref: str = Field(index=True)
    generation: str = Field(index=False)

    file_path: str
    symbol_name: str
    symbol_type: str          # "function" | "class" | "method"
    language: str
    start_line: int
    end_line: int
    source_code: str
    signature: str
    docstring: str | None = None
    parent_class: str | None = None

    search_vector: Any = Field(
        default=None,
        sa_column=Column(TSVECTOR)
    )

    @classmethod
    def from_parsed(
        cls,
        chunk: CodeChunk,
        *,
        repo_name: str,
        ref: str,
        generation: str,
    ) -> "CodeChunkModel":
        return cls(
            repo_name=repo_name,
            ref=ref,
            generation=generation,
            file_path=chunk.file_path,
            symbol_name=chunk.symbol_name,
            symbol_type=chunk.symbol_type,
            language=chunk.language,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            source_code=chunk.source_code,
            signature=chunk.signature,
            docstring=chunk.docstring,
            parent_class=chunk.parent_class,
        )


class RepoIndexState(SQLModel, table=True):
    __tablename__ = "repo_index_state"
    __table_args__ = (
        UniqueConstraint(
            "repo_name",
            "ref",
            name="uq_repo_index_state",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    repo_name: str
    ref: str
    visibility: str
    active_generation: str
    indexed_sha: str | None = None
    indexed_at: dt.datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class CodeChunkEmbedding(SQLModel, table=True):
    __tablename__ = "code_chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model_name", name="uq_chunk_model"),
    )

    id: int | None = Field(default=None, primary_key=True)

    chunk_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("code_chunks.id", ondelete="CASCADE"), index=True)
    )
    model_name: str = Field(index=True)
    dimension: int
    embedding: Any = Field(
        default=None,
        sa_column=Column(EMBEDDING_COLUMN_TYPE),
    )
