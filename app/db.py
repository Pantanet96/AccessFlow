"""SQLite engine + session, WAL + foreign keys enabled."""
from collections.abc import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from app.config import get_settings


def _sqlite_url(path: str) -> str:
    return f"sqlite:///{path}"


def make_engine(path: str | None = None) -> Engine:
    settings = get_settings()
    url = _sqlite_url(path or settings.database_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Wait up to 30s for a lock instead of failing immediately with
        # "database is locked": the daily scan holds a session across slow
        # Plex/SMTP/Telegram calls, which can otherwise collide with web writes.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


engine = make_engine()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
