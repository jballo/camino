from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel
from sqlalchemy import text

from app.config import settings
from app.db import engine

from app.api import agent, briefs, github, journeys, repositories
from app.webhooks import clerk, github as github_webhook
from app.models.code import CodeChunkEmbedding, CodeChunkModel, RepoIndexState
from app.models.job import Job
from app.models.rate_limit import RateLimit
from app.models.repo_follow import UserRepoFollow
from app.worker import WORKER_SHUTDOWN_TIMEOUT, worker_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # The repository index is a disposable cache. Step 3 changes its
        # identity from (installation, repo) to (repo, ref), so an old-shape
        # cache is dropped once and rebuilt instead of migrated in place.
        old_shape = conn.execute(text("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'code_chunks'
                  AND column_name = 'installation_id'
            )
        """)).scalar_one()
        if old_shape:
            conn.execute(text("DROP VIEW IF EXISTS live_code_chunks"))
            conn.execute(text("DROP TABLE IF EXISTS code_chunk_embeddings"))
            conn.execute(text("DROP TABLE IF EXISTS code_chunks"))
            conn.execute(text("DROP TABLE IF EXISTS repo_index_state CASCADE"))
        conn.commit()
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # create_all() cannot express these: the composite, partial, HNSW, and
        # GIN indexes, and the view that exposes only live index generations.
        # It also cannot add columns to tables created by older releases.
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ref VARCHAR"))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS issue_repo VARCHAR"
        ))
        # The repository that owns an issue was not stored before issue_repo was
        # introduced.  repo_name identifies the code repository and may be the
        # issue's upstream, so active legacy briefs cannot be resumed safely.
        conn.execute(text("""
            UPDATE jobs
            SET status = 'failed',
                error = 'Legacy issue brief is missing its issue repository; recreate it',
                claimed_at = NULL,
                claimed_by = NULL
            WHERE job_type = 'issue_brief'
              AND issue_repo IS NULL
              AND status IN ('pending', 'running')
        """))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS issue_number INTEGER"
        ))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS blocked_by_job_id INTEGER"
        ))
        conn.execute(text(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS refresh_cycles INTEGER "
            "NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_jobs_issue_number ON jobs (issue_number)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_jobs_blocked_by_job_id "
            "ON jobs (blocked_by_job_id)"
        ))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chunks_repo_generation
            ON code_chunks (repo_name, ref, generation)
        """))
        conn.execute(text("""
            CREATE OR REPLACE VIEW live_code_chunks AS
            SELECT c.*
            FROM code_chunks c
            JOIN repo_index_state s
              ON s.repo_name = c.repo_name
             AND s.ref = c.ref
             AND s.active_generation = c.generation
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_jobs_pending
            ON jobs ("createdAt") WHERE status = 'pending'
        """))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_dedupe
            ON jobs (dedupe_key)
            WHERE status IN ('pending', 'running') AND dedupe_key IS NOT NULL
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw
            ON code_chunk_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chunks_search
            ON code_chunks USING gin (search_vector)
        """))
        conn.commit()

    stop_event = asyncio.Event()
    worker_task = None
    if settings.run_worker:
        worker_task = asyncio.create_task(
            worker_loop(stop_event), name="job-worker"
        )
    app.state.worker_stop_event = stop_event
    app.state.worker_task = worker_task

    yield

    if worker_task is not None:
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=WORKER_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            logger.warning("job worker did not stop in time; cancelling")
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

app.include_router(github.router, prefix="/api/v1/github", tags=["github"])
app.include_router(repositories.router, prefix="/api/v1/repositories", tags=["repositories"])
app.include_router(agent.router, prefix="/api/v1/agent", tags=["agent"])
app.include_router(journeys.router, prefix="/api/v1/journeys", tags=["journeys"])
app.include_router(briefs.router, prefix="/api/v1/briefs", tags=["briefs"])
app.include_router(clerk.router, prefix="/webhooks/clerk", tags=["webhooks"])
app.include_router(github_webhook.router, prefix="/webhooks/github", tags=["webhooks"])
