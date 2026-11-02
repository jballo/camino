from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import engine
from app.db_schema import verify_schema

from app.api import agent, briefs, github, journeys, repositories
from app.webhooks import clerk, github as github_webhook
from app.worker import WORKER_SHUTDOWN_TIMEOUT, worker_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrations own the schema (python -m app.migrate, run before deploys);
    # startup only checks it.
    with engine.connect() as conn:
        verify_schema(conn)

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

@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Liveness probe for the Compose healthcheck and Caddy.

    Unauthenticated and free of database and external calls: it answers as
    long as the process serves HTTP. Startup already fails if the database or
    schema revision is unusable, so there is nothing further to check here.
    """
    return {"status": "ok"}


app.include_router(github.router, prefix="/api/v1/github", tags=["github"])
app.include_router(repositories.router, prefix="/api/v1/repositories", tags=["repositories"])
app.include_router(agent.router, prefix="/api/v1/agent", tags=["agent"])
app.include_router(journeys.router, prefix="/api/v1/journeys", tags=["journeys"])
app.include_router(briefs.router, prefix="/api/v1/briefs", tags=["briefs"])
app.include_router(clerk.router, prefix="/webhooks/clerk", tags=["webhooks"])
app.include_router(github_webhook.router, prefix="/webhooks/github", tags=["webhooks"])
