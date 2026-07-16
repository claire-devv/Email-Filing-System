from collections.abc import Generator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        # WAL mode: readers and writers don't block each other — essential with
        # multiple uvicorn workers writing from background tasks simultaneously.
        # synchronous=NORMAL is safe with WAL and much faster than the default FULL.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        # Under a burst (many emails committing across 2 workers x several threads),
        # writers are serialized. Wait up to 30s for the write lock before raising
        # "database is locked" — WAL writes are fast, so this comfortably absorbs bursts.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.db import models  # noqa: F401

    sqlite_path = settings.sqlite_path
    if sqlite_path:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _seed_dashboard_users()


def _seed_dashboard_users() -> None:
    from app.core.security import hash_password
    from app.db.models import DashboardUser

    db = SessionLocal()
    try:
        if db.execute(select(DashboardUser)).first():
            return
        email = settings.dashboard_auth_email
        password = settings.dashboard_auth_password
        if email and password:
            db.add(
                DashboardUser(
                    email=email.strip().lower(),
                    password_hash=hash_password(password),
                    active=True,
                    is_admin=True,
                )
            )
            db.commit()
    finally:
        db.close()
