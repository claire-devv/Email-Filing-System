from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import DashboardUser
from app.db.session import get_db

router = APIRouter(prefix="/users", tags=["users"])


class UserCreateRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)


class UserUpdateRequest(BaseModel):
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


def _user_payload(user: DashboardUser) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "active": user.active,
        "is_admin": user.is_admin,
        "created_at": user.created_at,
    }


@router.get("")
def list_users(db: Session = Depends(get_db)) -> list[dict]:
    users = db.execute(select(DashboardUser).order_by(DashboardUser.created_at)).scalars().all()
    return [_user_payload(u) for u in users]


@router.post("")
def create_user(payload: UserCreateRequest, db: Session = Depends(get_db)) -> dict:
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email is required.")
    existing = db.execute(select(DashboardUser).where(DashboardUser.email == email)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="That email is already in use.")
    user = DashboardUser(
        email=email,
        password_hash=hash_password(payload.password),
        active=True,
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_payload(user)


@router.patch("/{user_id}")
def update_user(user_id: int, payload: UserUpdateRequest, db: Session = Depends(get_db)) -> dict:
    user = db.get(DashboardUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="The admin account cannot be modified here.")

    if payload.active is False:
        _ensure_not_last_active(db, user)
        user.active = False
    elif payload.active is True:
        user.active = True

    if payload.password:
        user.password_hash = hash_password(payload.password)

    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_payload(user)


@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)) -> dict:
    user = db.get(DashboardUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="The admin account cannot be deleted.")
    _ensure_not_last_active(db, user)
    db.delete(user)
    db.commit()
    return {"status": "deleted", "id": user_id}


def _ensure_not_last_active(db: Session, user: DashboardUser) -> None:
    if not user.active:
        return
    active_count = db.execute(
        select(DashboardUser).where(DashboardUser.active.is_(True))
    ).scalars().all()
    if len(active_count) <= 1:
        raise HTTPException(status_code=400, detail="At least one active user is required.")
