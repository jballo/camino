from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel
from sqlalchemy import text

from app.config import settings
from app.db import engine

from app.api import agent, github, journeys, repositories
from app.webhooks import clerk, github as github_webhook
from app.models.code import CodeChunkModel, CodeChunkEmbedding
from app.models.job import Job
from app.models.rate_limit import RateLimit
from app.worker import WORKER_SHUTDOWN_TIMEOUT, worker_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Preserve queued tours when upgrading from the tour-specific queue.
        conn.execute(text("""
            DO $$
            BEGIN
                IF to_regclass('public.jobs') IS NULL
                   AND to_regclass('public.tour_jobs') IS NOT NULL THEN
                    ALTER TABLE tour_jobs RENAME TO jobs;
                END IF;
            END $$;
        """))
        conn.commit()
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # create_all() does not evolve existing tables. Keep this nullable for
        # legacy connections because a numeric GitHub user ID cannot be derived
        # reliably from the data already stored; reconnecting fills it in.
        conn.execute(text("""
            ALTER TABLE githubconnections
            ADD COLUMN IF NOT EXISTS "githubUserId" INTEGER
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS "ix_githubconnections_githubUserId"
            ON githubconnections ("githubUserId")
        """))
        conn.execute(text("""
            ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ
        """))
        conn.execute(text("""
            ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS claimed_by TEXT
        """))
        conn.execute(text("""
            ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
        """))
        conn.execute(text("""
            ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'tour'
        """))
        conn.execute(text("""
            ALTER TABLE jobs
            ADD COLUMN IF NOT EXISTS dedupe_key TEXT
        """))
        conn.execute(text("ALTER TABLE jobs ALTER COLUMN topic DROP NOT NULL"))
        conn.execute(text("""
            UPDATE jobs SET status = 'running' WHERE status = 'generating'
        """))
        conn.execute(text("""
            WITH ranked AS (
                SELECT id,
                       concat(
                           'tour:', "userId", ':', installation_id, ':',
                           repo_name, ':', topic
                       ) AS key,
                       row_number() OVER (
                           PARTITION BY "userId", installation_id, repo_name, topic
                           ORDER BY "createdAt", id
                       ) AS position
                FROM jobs
                WHERE job_type = 'tour'
                  AND status IN ('pending', 'running')
                  AND dedupe_key IS NULL
            )
            UPDATE jobs
            SET dedupe_key = ranked.key
            FROM ranked
            WHERE jobs.id = ranked.id AND ranked.position = 1
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_jobs_job_type ON jobs (job_type)
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
app.include_router(clerk.router, prefix="/webhooks/clerk", tags=["webhooks"])
app.include_router(github_webhook.router, prefix="/webhooks/github", tags=["webhooks"])
