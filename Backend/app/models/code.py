import datetime as dt
from typing import Any
from pgvector import Vector
from pgvector.sqlalchemy import HALFVEC
from sqlmodel import Column, Field, Session, SQLModel
from sqlalchemy import DateTime, Index, Integer, UniqueConstraint, ForeignKey, insert
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.services.parser import CodeChunk


class EmbeddingColumnType(HALFVEC):
    """halfvec in the schema, but sends embeddings at fp32 precision.

    HALFVEC itself rounds values to half precision before sending them, which
    would silently degrade a column converted to vector(1536) for the fp32
    experiment (eval/README.md). Postgres does the same rounding when it
    stores into a halfvec column, so halfvec writes of fp32 embeddings (what
    the OpenAI client returns) are unchanged.

    Write batches with insert_embeddings, not session.add_all: see there.
    """

    cache_ok = True

    def bind_processor(self, dialect):
        def process(value):
            return Vector._to_db(value, self.dim)
        return process

    def literal_processor(self, dialect):
        string_literal_processor = self._string._cached_literal_processor(dialect)

        def process(value):
            return string_literal_processor(Vector._to_db(value, self.dim))
        return process


# Matches EMBED_DIMENSIONS in app/services/embeddings.py. The column type is
# fixed by the migrations; VECTOR_TYPE is only checked against it at startup.
EMBEDDING_COLUMN_TYPE = EmbeddingColumnType(1536)


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
        Index("ix_chunks_repo_generation", "repo_name", "ref", "generation"),
        Index("ix_chunks_search", "search_vector", postgresql_using="gin"),
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


def insert_embeddings(session: Session, rows: list[dict[str, Any]]) -> None:
    """Insert CodeChunkEmbedding rows in one batch, at the column's precision.

    session.add_all would batch them in the ordered INSERT ... SELECT form,
    which casts each value to HALFVEC(1536) and so rounds it to half
    precision even when the column is vector(1536). A plain bulk insert
    sends the values uncast.
    """
    if rows:
        session.exec(insert(CodeChunkEmbedding), params=rows)
