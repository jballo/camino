from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel
from sqlalchemy import text

from app.config import settings
from app.db import engine

from app.api import agent, github, journeys, repositories
from app.webhooks import clerk, github as github_webhook
from app.models.code import CodeChunkModel, CodeChunkEmbedding
from app.models.rate_limit import RateLimit
from app.models.tour_job import TourJob



@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
            CREATE INDEX IF NOT EXISTS ix_embeddings_hnsw
            ON code_chunk_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chunks_search
            ON code_chunks USING gin (search_vector)
        """))
        conn.commit()
    yield


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
