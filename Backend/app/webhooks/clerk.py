from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import exc
from sqlmodel import select
from svix.webhooks import Webhook, WebhookVerificationError

from app.config import settings
from app.db import SessionDep
from app.models.user import User
from app.services.account_deletion import (
    AccountDeletionError,
    delete_local_account_data,
)


router = APIRouter()


@router.post("")
async def clerk_webhook_handler(request: Request, session: SessionDep) -> User | str:
    headers = request.headers
    payload = await request.body()

    try:
        wh = Webhook(settings.clerk_wh_key)
        msg = wh.verify(payload, headers)
        event = msg["type"]

        if event == "user.created":
            user_id = msg["data"]["id"]
            email = (
                msg["data"]["email_addresses"][0]["email_address"]
                if len(msg["data"]["email_addresses"]) > 0
                else ""
            )
            name = msg["data"]["first_name"] if msg["data"].get("first_name") is not None else ""
            user = User(id=user_id, email=email, name=name)
            try:
                session.add(user)
                session.commit()
                session.refresh(user)
                return user
            except exc.IntegrityError:
                session.rollback()
                existing = session.exec(select(User).where(User.id == user_id)).first()
                if existing:
                    return existing
                raise HTTPException(status_code=409, detail="Already exists")

        elif event == "user.updated":
            user_id = msg["data"]["id"]
            new_email = (
                msg["data"]["email_addresses"][0]["email_address"]
                if len(msg["data"]["email_addresses"]) > 0
                else ""
            )
            new_name = (
                msg["data"]["first_name"] if msg["data"].get("first_name") is not None else ""
            )
            try:
                statement = select(User).where(User.id == user_id)
                user = session.exec(statement).one()
                user.email = new_email
                user.name = new_name
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
            user_id = msg["data"]["id"]
            try:
                delete_local_account_data(session, user_id)
                return "user deleted"
            except AccountDeletionError:
                raise HTTPException(status_code=500, detail="Failed to delete user")
        else:
            print("Unknown event")

        return "Successfully processed user event"
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Bad request")
