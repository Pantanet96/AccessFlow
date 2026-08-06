import os
import tempfile

# Point the app at a throwaway SQLite file before any app import triggers
# get_settings(). Keeps tests off the container's /data path.
_tmp = tempfile.mkdtemp(prefix="pum-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
# Short dev secret above would trip the weak-key guard (Fix #1); allow it in tests.
os.environ.setdefault("ALLOW_INSECURE_SECRET", "true")
# Don't spin up the scheduler / telegram bot during tests.
os.environ.setdefault("ENABLE_SCHEDULER", "false")
os.environ.setdefault("ENABLE_BOT", "false")
# Deterministic credentials (env vars take precedence over any local .env).
os.environ.setdefault("SUPERADMIN_USERNAME", "admin")
# Explicit non-default password so the seed doesn't auto-generate one (Fix #2).
os.environ.setdefault("SUPERADMIN_PASSWORD", "test-admin-pw")

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client():
    # Context manager runs lifespan -> migrate + seed against the temp DB.
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def login_as():
    """Return a helper that attaches a session cookie for a given user id."""
    from app.auth.session import COOKIE_NAME, make_token

    def _login(client, user_id: int):
        client.cookies.set(COOKIE_NAME, make_token(user_id))

    return _login


@pytest.fixture
def db_session():
    from sqlmodel import Session, SQLModel

    from app.db import engine
    from app.migrate import run_migrations
    from app.seed import seed_all

    run_migrations()
    with Session(engine) as s:
        # Isolate tests: wipe all tables (FK-safe order) then re-seed.
        for table in reversed(SQLModel.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()
        # This wipe bypasses settings_store.delete_value, so its cache never
        # sees the write; force runtime_config to re-read from the fresh DB.
        from app.services import settings_store

        settings_store.invalidate()
        seed_all(s)
        # Test convenience: standard paid plans (no longer seeded in production).
        from sqlmodel import select as _select

        from app.models import Plan

        for slug, name, months, cents in [
            ("bronze", "Bronze", 1, 500),
            ("silver", "Silver", 6, 2500),
            ("gold", "Gold", 12, 5000),
        ]:
            if s.exec(_select(Plan).where(Plan.slug == slug)).first() is None:
                s.add(
                    Plan(
                        slug=slug, name=name, duration_months=months,
                        price_cents=cents, is_paid=True,
                    )
                )
        s.commit()
        yield s
