from typing import Annotated
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlalchemy import exc
from contextlib import asynccontextmanager
from svix.webhooks import Webhook, WebhookVerificationError
import pprint


class Settings(BaseSettings):
    database_url: str
    clerk_wh_key: str
    model_config = SettingsConfigDict(env_file=".env")

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    email: str
    name: str


    def __repr__(self):
        return "<User(id='%s', email='%s', name='%s')>" % ( self.id, self.email, self.name)

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
        

@app.post("/webhook/")
async def webhook_handler(request: Request, response: Response, session: SessionDep) -> User | str:
    headers = request.headers
    payload = await request.body()

    try:
        wh = Webhook(settings.clerk_wh_key)
        msg = wh.verify(payload, headers)
        event = msg["type"]
        
        pp = pprint.PrettyPrinter(indent=3, width=50)
        pp.pprint(msg)

        if event == "user.created":
            print("user created")
            userId = msg["data"]["id"]
            email = msg["data"]["email_addresses"][0]["email_address"] if len(msg["data"]["email_addresses"]) > 0 else "randomeEmail@email.com"
            name = msg["data"]["first_name"]
            user = User(id=userId, email=email, name=name)
            try:
                session.add(user)
                session.commit()
                session.refresh(user)
                return user
            except exc.IntegrityError:
                session.rollback()
                raise HTTPException(status_code=409, detail="Already exists")
            
        elif event == "user.updated":
            print("user updated")
            userId = msg["data"]["id"]
            newEmail = msg["data"]["email_addresses"][0]["email_address"] if len(msg["data"]["email_addresses"]) > 0 else "randomeEmail@email.com"
            newName = msg["data"]["first_name"]
            try:
                statement = select(User).where(User.id == userId)
                results = session.exec(statement)
                user = results.one()
                user.email = newEmail
                user.name = newName
                session.add(user)
                session.commit()
                session.refresh(user)
                return user
            except exc.IntegrityError:
                session.rollback()
                raise HTTPException(status_code=500, detail="Failed to update user")

        elif event == "user.deleted":
            print("user deleted")
            userId = msg["data"]["id"]
            try:
                statement = select(User).where(User.id == userId)
                results = session.exec(statement)
                user = results.one()
                print("deleting user: ", user)
                session.delete(user)
                session.commit()
                return "user deleted"
            except exc.IntegrityError:
                session.rollback()
                raise HTTPException(status_code=500, detail="Failed to delete user")
        else:
            print("Unknown event")

        
        return "Succesfully processed user event"
    except WebhookVerificationError as err:
        raise HTTPException(status_code=400, details="Bad request")