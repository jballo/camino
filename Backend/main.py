from typing import Annotated
from fastapi import FastAPI, Depends, HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Field, Session, SQLModel, create_engine
from sqlalchemy import exc
from contextlib import asynccontextmanager

class Settings(BaseSettings):
    database_url: str
    model_config = SettingsConfigDict(env_file=".env")


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    email: str

settings = Settings()
engine = create_engine(settings.database_url)

def get_session():
    with Session(engine) as session:
        yield session

@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield

SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI(lifespan=lifespan)


@app.post("/users")
def create_user(user: User, session: SessionDep) -> User:
    try:
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    except exc.IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Already exists")
        

