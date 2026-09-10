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
from app.models.code import CodeChunkEmbedding, CodeChunkModel, RepoIndexState
from app.models.job import Job
from app.models.rate_limit import RateLimit
from app.worker import WORKER_SHUTDOWN_TIMEOUT, worker_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    SQLModel.metadata.create_all(engine)

    with engine.connect() as conn:
        # create_all() cannot express these: the composite, partial, HNSW, and
        # GIN indexes, and the view that exposes only live index generations.
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chunks_repo_generation
            ON code_chunks (installation_id, repo_name, generation)
        """))
        conn.execute(text("""
            CREATE OR REPLACE VIEW live_code_chunks AS
            SELECT c.*
            FROM code_chunks c
            JOIN repo_index_state s
              ON s.installation_id = c.installation_id
             AND s.repo_name = c.repo_name
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
app.include_router(clerk.router, prefix="/webhooks/clerk", tags=["webhooks"])
app.include_router(github_webhook.router, prefix="/webhooks/github", tags=["webhooks"])
