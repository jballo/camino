from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import SQLModel

from app.db import engine

from app.api import github, repositories, users
from app.webhooks import clerk, github as github_webhook



@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(github.router, prefix="/api/v1/github", tags=["github"])
app.include_router(repositories.router, prefix="/api/v1/repositories", tags=["repositories"])
app.include_router(clerk.router, prefix="/webhooks/clerk", tags=["webhooks"])
app.include_router(github_webhook.router, prefix="/webhooks/github", tags=["webhooks"])
