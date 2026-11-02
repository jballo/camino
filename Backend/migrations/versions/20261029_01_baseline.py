"""Full schema baseline: every table, index and view the application uses.

Until this revision the API created and altered the schema at every startup.
This revision creates the same schema on an empty database, frozen as it was
on 2026-10-29: later model changes need their own revision, never an edit
here. Databases created by the old API startup are adopted with
``python -m app.migrate adopt`` instead of running this revision (see
Backend/README.md, "Migrations").

Revision ID: 20261029_01
Revises: 20260928_01
Create Date: 2026-10-29
"""

from alembic import op
from pgvector.sqlalchemy import HALFVEC
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261029_01"
down_revision = "20260928_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "githubconnections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("userId", sa.String(), nullable=False),
        sa.Column("githubUsername", sa.String(), nullable=False),
        sa.Column("githubUserId", sa.Integer(), nullable=False),
        sa.Column("installationId", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updatedAt",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("userId"),
    )
    op.create_index(
        "ix_githubconnections_githubUserId", "githubconnections", ["githubUserId"]
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("userId", sa.String(), nullable=True),
        sa.Column("installation_id", sa.Integer(), nullable=True),
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=True),
        sa.Column("issue_repo", sa.String(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("blocked_by_job_id", sa.Integer(), nullable=True),
        sa.Column("refresh_cycles", sa.Integer(), nullable=False),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updatedAt",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_blocked_by_job_id", "jobs", ["blocked_by_job_id"])
    op.create_index("ix_jobs_issue_number", "jobs", ["issue_number"])
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])
    op.create_index("ix_jobs_ref", "jobs", ["ref"])
    op.create_index("ix_jobs_repo_name", "jobs", ["repo_name"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_userId", "jobs", ["userId"])
    op.create_index(
        "ix_jobs_pending",
        "jobs",
        ["createdAt"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ux_jobs_active_dedupe",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND dedupe_key IS NOT NULL"
        ),
    )

    op.create_table(
        "rate_limits",
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("bucket", "user_id"),
    )

    op.create_table(
        "user_repo_follows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("userId", sa.String(), nullable=False),
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("userId", "repo_name", name="uq_user_repo_follow"),
    )
    op.create_index(
        "ix_user_repo_follows_repo_name", "user_repo_follows", ["repo_name"]
    )
    op.create_index("ix_user_repo_follows_userId", "user_repo_follows", ["userId"])

    op.create_table(
        "repo_index_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("active_generation", sa.String(), nullable=False),
        sa.Column("indexed_sha", sa.String(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_name", "ref", name="uq_repo_index_state"),
    )

    op.create_table(
        "code_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("generation", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("symbol_name", sa.String(), nullable=False),
        sa.Column("symbol_type", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.String(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("docstring", sa.String(), nullable=True),
        sa.Column("parent_class", sa.String(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repo_name",
            "ref",
            "generation",
            "file_path",
            "symbol_name",
            "start_line",
            name="uq_chunk_identity_gen",
        ),
    )
    op.create_index("ix_code_chunks_ref", "code_chunks", ["ref"])
    op.create_index("ix_code_chunks_repo_name", "code_chunks", ["repo_name"])
    op.create_index(
        "ix_chunks_repo_generation", "code_chunks", ["repo_name", "ref", "generation"]
    )
    op.create_index(
        "ix_chunks_search", "code_chunks", ["search_vector"], postgresql_using="gin"
    )

    op.create_table(
        "code_chunk_embeddings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("embedding", HALFVEC(1536), nullable=True),
        sa.ForeignKeyConstraint(["chunk_id"], ["code_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "model_name", name="uq_chunk_model"),
    )
    op.create_index(
        "ix_code_chunk_embeddings_chunk_id", "code_chunk_embeddings", ["chunk_id"]
    )
    op.create_index(
        "ix_code_chunk_embeddings_model_name", "code_chunk_embeddings", ["model_name"]
    )
    # Without an ANN index, exact search reads every embedding; PLAIN keeps
    # them inline instead of in TOAST. Model metadata cannot express storage.
    op.execute(
        "ALTER TABLE code_chunk_embeddings ALTER COLUMN embedding SET STORAGE PLAIN"
    )

    # Search reads only the active generation of each repository index.
    op.execute("""
        CREATE VIEW live_code_chunks AS
        SELECT c.*
        FROM code_chunks c
        JOIN repo_index_state s
          ON s.repo_name = c.repo_name
         AND s.ref = c.ref
         AND s.active_generation = c.generation
    """)


def downgrade() -> None:
    op.execute("DROP VIEW live_code_chunks")
    for table in (
        "code_chunk_embeddings",
        "code_chunks",
        "repo_index_state",
        "user_repo_follows",
        "rate_limits",
        "jobs",
        "githubconnections",
        "users",
    ):
        op.drop_table(table)
