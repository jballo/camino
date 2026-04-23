from typing import Annotated
from fastapi import FastAPI, Depends
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Field, Session, SQLModel, create_engine

class Settings(BaseSettings):
    database_url: str
    model_config = SettingsConfigDict(env_file=".env")


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(default=None, primary_key=True)
    email: str = Field(default=None)

settings = Settings()
engine = create_engine(settings.database_url)

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI()


@app.post("/users")
def create_user(user: User, session: SessionDep) -> User:
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
