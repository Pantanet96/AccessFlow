"""Run Alembic migrations programmatically (called on app startup)."""
from pathlib import Path

from alembic import command
from alembic.config import Config

_ROOT = Path(__file__).resolve().parent.parent


def run_migrations() -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(cfg, "head")
