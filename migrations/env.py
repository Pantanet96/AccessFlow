"""Alembic environment. Uses the app's SQLite path and SQLModel metadata."""
from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers tables on SQLModel.metadata)
from app.config import get_settings
from app.db import make_engine

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: run_migrations() runs in-process at startup
    # (app.main) AFTER app.seed is already imported, so the default (True) would
    # silently disable the 'pum.seed' logger — swallowing the one-time generated
    # SuperAdmin password and locking the operator out. Also preserves uvicorn's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    settings = get_settings()
    context.configure(
        url=f"sqlite:///{settings.database_path}",
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode for ALTER
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
