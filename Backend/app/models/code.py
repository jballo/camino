from typing import Any
from pgvector.sqlalchemy import Vector
from sqlmodel import Column, Field, SQLModel
from sqlalchemy import Integer, UniqueConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.services.parser import CodeChunk
from app.services.embeddings import EMBED_DIMENSIONS

class CodeChunkModel(SQLModel, table=True):
    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint("repo_name", "file_path", "symbol_name", "start_line",
                        name="uq_chunk_identity"),
    )

    id: int | None = Field(default=None, primary_key=True)

    installation_id: int = Field(index=True)
    repo_name: str = Field(index=True)

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
    def from_parsed(cls, chunk: CodeChunk, *, repo_name: str, installation_id: int) -> "CodeChunkModel":
        return cls(
            installation_id=installation_id,
            repo_name=repo_name,
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
        sa_column=Column(Vector(EMBED_DIMENSIONS))
    )