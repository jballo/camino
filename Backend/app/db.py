from typing import Annotated
from sqlmodel import create_engine, Session
from fastapi import Depends
from app.config import settings

engine = create_engine(settings.database_url)

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]