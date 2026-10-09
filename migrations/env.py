"""Alembic environment.

The database URL and the target metadata both come from the application, so
there is exactly one definition of each and `alembic.ini` stays secret-free.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from yt_crypto_signals import models  # noqa: F401  (registers tables on the metadata)
from yt_crypto_signals.config import get_config
from yt_crypto_signals.db import build_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _db_path() -> str:
    return str(get_config().settings.db_path)


def run_migrations_offline() -> None:
    context.configure(
        url=f"sqlite:///{_db_path()}",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(get_config().settings.db_path)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, which is what makes future migrations possible.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
