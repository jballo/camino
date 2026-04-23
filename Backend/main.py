from typing import Annotated
from fastapi import FastAPI, Depends
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Field, Session, SQLModel, create_engine, table
from dotenv import load_dotenv
import os

load_dotenv('.env')
db_url = os.getenv('DATABASE_URL')
print("db_url", db_url)

class Settings(BaseSettings):
    database_url: str
    model_config = SettingsConfigDict(env_file=".env")


class Users(SQLModel, table=True):
    id: str = Field(default=None, primary_key=True)
    email: str = Field(default=None)

connect_args = {"check_same_thread": False}
engine = create_engine(db_url)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    print("url: ", Settings.database_url)

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]



app = FastAPI()


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}


@app.post("/users")
def create_user(user: Users, session: SessionDep) -> Users:
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
