import datetime as dt
from re import split
from time import timezone
from typing import Annotated, Optional
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlalchemy import exc, Column, DateTime, func
from contextlib import asynccontextmanager
from svix.webhooks import Webhook, WebhookVerificationError
from pprint import pprint
from github import AccessToken, BadCredentialsException, Github, GithubException, Auth, GithubIntegration, RateLimitExceededException, Installation


class Settings(BaseSettings):
    database_url: str
    clerk_wh_key: str
    gh_app_id: int
    gh_app_client_id: str
    gh_app_secret: str
    gh_app_private_key: str
    backend_api_key: str
    encryption_key: str
    model_config = SettingsConfigDict(env_file=".env")

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    email: str
    name: Optional[str] = None


    def __repr__(self):
        return "<User(id='%s', email='%s', name='%s')>" % ( self.id, self.email, self.name)

class GithubConnections(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    userId: str = Field(unique=True)
    githubUsername: str
    installationId: int
    accessToken: str
    refreshToken: str
    tokenExpiresAt: dt.datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    refreshTokenExpiresAt: dt.datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    createdAt: dt.datetime = Field(
        sa_column=Column[dt.datetime](
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )
    )
    updatedAt: dt.datetime = Field(
        sa_column=Column[dt.datetime](
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )


class _GithubConnectBody(BaseModel):
    code: str
    userId: str
    installationId: int

class _RepoIngestBody(BaseModel):
    repoName: str
    userId: str

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

        print("Event: ", event)
        if event == "user.created":
            userId = msg["data"]["id"]
            email = msg["data"]["email_addresses"][0]["email_address"] if len(msg["data"]["email_addresses"]) > 0 else ""
            name = msg["data"]["first_name"] if msg["data"].get("first_name") != None else ""
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
            userId = msg["data"]["id"]
            newEmail = msg["data"]["email_addresses"][0]["email_address"] if len(msg["data"]["email_addresses"]) > 0 else ""
            newName = msg["data"]["first_name"] if msg["data"].get("first_name") != None else ""
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
            except exc.NoResultFound:
                session.rollback()
                raise HTTPException(status_code=404, detail="User not found")
            except exc.IntegrityError:
                session.rollback()
                raise HTTPException(status_code=500, detail="Failed to update user")

        elif event == "user.deleted":
            userId = msg["data"]["id"]
            try:
                statement = select(User).where(User.id == userId)
                results = session.exec(statement)
                user = results.one()
                session.delete(user)
                session.commit()
                return "user deleted"
            except exc.NoResultFound:
                session.rollback()
                raise HTTPException(status_code=404, detail="User not found")
            except exc.IntegrityError:
                session.rollback()
                raise HTTPException(status_code=500, detail="Failed to delete user")
        else:
            print("Unknown event")

        
        return "Succesfully processed user event"
    except WebhookVerificationError as err:
        raise HTTPException(status_code=400, detail="Bad request")


@app.post("/api/github/connect")
async def add_github_connection(payload: _GithubConnectBody, request: Request, session: SessionDep) -> str:
    headers = request.headers
    authorization = headers.get("authorization")

    
    if authorization is None or authorization != f"Bearer {settings.backend_api_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        g = Github()
        app = g.get_oauth_application(settings.gh_app_client_id, settings.gh_app_secret)
        accessToken: AccessToken = app.get_access_token(payload.code)
        g = Github(auth=Auth.AppUserAuth(client_id=settings.gh_app_client_id, client_secret=settings.gh_app_secret,token=accessToken.token))
        user = g.get_user()
        username = user.name if user.name else ""
        access_token: str = accessToken.token
        expires_in: int | None = accessToken.expires_in
        refresh_token: str | None = accessToken.refresh_token
        refresh_expires_in: int | None = accessToken.refresh_expires_in
        created_at: dt.datetime = accessToken.created
    except BadCredentialsException:
        raise HTTPException(status_code=400, detail="Invalid Github code")
    except RateLimitExceededException:
        raise HTTPException(status_code=429, detail="GitHub rate limit exceeded")
    except GithubException as e:
        if e.status in (400, 401, 403):
            raise HTTPException(status_code=400, detail="Invalid Github code")
        raise HTTPException(status_code=502, detail="Github error")

    if expires_in is None or refresh_token is None or refresh_expires_in is None:
        raise HTTPException(status_code=502, detail="Github returned a non expiring token. Expected an expiring user to server token")

    

    try:
        installation_id = payload.installationId
        token_expires_at = created_at + dt.timedelta(seconds=expires_in)
        refresh_token_expires_at = created_at + dt.timedelta(seconds=refresh_expires_in)

        connection = GithubConnections(
            userId=payload.userId,
            githubUsername=username,
            installationId=installation_id,
            accessToken=access_token,
            refreshToken=accessToken.refresh_token,
            tokenExpiresAt=token_expires_at,
            refreshTokenExpiresAt=refresh_token_expires_at

        )
        session.add(connection)
        session.commit()
        session.refresh(connection)
        return "Sucessfully added github connection"
    except exc.IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Already connected")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/api/repositories/{userId}")
async def list_respositories(userId: str, request: Request, session: SessionDep) -> list[str]:
    headers = request.headers
    authorization = headers.get("authorization")
    if authorization is None or authorization != f"Bearer {settings.backend_api_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        statement = select(GithubConnections).where(GithubConnections.userId == userId)
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Gtibhub connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(app_id=settings.gh_app_id, private_key=settings.gh_app_private_key)
        gi = GithubIntegration(auth=app_auth)

        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()
        parsedRepos = []
        for repo in repos:
            parsedRepos.append(repo.full_name)
        print("parsedRepos: ", parsedRepos)
        return parsedRepos
        
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")



@app.post("/api/repositories/ingest")
async def process_repository(payload: _RepoIngestBody, request: Request, session: SessionDep):

    headers = request.headers
    authorization = headers.get("authorization")
    if authorization is None or authorization != f"Bearer {settings.backend_api_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")


    try:
        statement = select(GithubConnections).where(GithubConnections.userId == payload.userId)
        result = session.exec(statement)
        gh_connection = result.one()
    except exc.NoResultFound:
        raise HTTPException(status_code=404, detail="Gtibhub connection not found for user")
    except exc.OperationalError:
        session.rollback()
        raise HTTPException(status_code=500, detail="Database error")

    try:
        app_auth = Auth.AppAuth(app_id=settings.gh_app_id, private_key=settings.gh_app_private_key)
        gi = GithubIntegration(auth=app_auth)

        installation = gi.get_app_installation(gh_connection.installationId)
        repos = installation.get_repos()
        repoSelected = None
        for repo in repos:
            if repo.full_name == payload.repoName:
                repoSelected = repo
        
        contents = repoSelected.get_contents("")
        while contents:
            file_content = contents.pop(0)
            if file_content.type == "dir":
                contents.extend(repo.get_contents(file_content.path))
            else:
                print(file_content.decoded_content)
                content = file_content.decoded_content
                content.split

        return []
        
    except GithubException:
        raise HTTPException(status_code=500, detail="Github error")

    
