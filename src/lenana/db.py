"""SQLite engine and session management.

SQLite is a good fit here precisely because of how the application runs: every
stage is a short-lived oneshot process holding an exclusive file lock, so there
is never real write contention. WAL mode and a generous `busy_timeout` cover
the one case that remains — the nightly backup reading while a timer-driven run
writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from .config import Config, get_config

#: Waited out by SQLite itself when another connection holds a write lock.
BUSY_TIMEOUT_MS = 10_000

_engine: Engine | None = None


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Per-connection PRAGMAs.

    These must be set on every new connection, not once at startup, which is
    why this hangs off the connect event rather than living in `init_engine`.
    """
    cur = dbapi_connection.cursor()
    try:
        # Readers never block the writer; survives power loss with synchronous=NORMAL.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA synchronous=NORMAL")
        # Off by default in SQLite; our schema depends on them.
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()


def build_engine(db_path: Path, echo: bool = False) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        # Short-lived processes: a pool would just hold file handles open.
        pool_pre_ping=True,
        connect_args={"timeout": BUSY_TIMEOUT_MS / 1000},
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def get_engine(cfg: Config | None = None) -> Engine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        cfg = cfg or get_config()
        _engine = build_engine(cfg.settings.db_path)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests and by `--db` overrides."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope(cfg: Config | None = None) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on any exception.

    Stages wrap each small batch in one of these, so an unhandled error leaves
    the batch's rows in their original input state to be retried next run —
    nothing is half-processed.
    """
    session = Session(get_engine(cfg))
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create tables directly from the models.

    Alembic owns the real schema; this exists for tests and for bootstrapping a
    throwaway database. Production uses `lenana db upgrade`.
    """
    from . import models  # noqa: F401  (import registers the tables)

    SQLModel.metadata.create_all(engine or get_engine())


def backup_to(dest: Path, cfg: Config | None = None) -> Path:
    """Consistent online backup via SQLite's native backup API.

    Copying the file would risk a torn read against a concurrent writer; the
    backup API takes care of that for us.
    """
    import sqlite3

    cfg = cfg or get_config()
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{cfg.settings.db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def table_counts() -> dict[str, int]:
    """Row counts per table, for the `stats` command."""
    from sqlalchemy import func, select

    from . import models

    counts: dict[str, int] = {}
    with session_scope() as session:
        for name in models.__all__:
            obj = getattr(models, name)
            is_table = (
                isinstance(obj, type)
                and issubclass(obj, SQLModel)
                and obj.__dict__.get("__tablename__") is not None
            )
            if is_table:
                total = session.scalar(select(func.count()).select_from(obj))
                counts[str(obj.__tablename__)] = int(total or 0)
    return counts


__all__ = [
    "backup_to",
    "build_engine",
    "create_all",
    "get_engine",
    "reset_engine",
    "session_scope",
    "table_counts",
]
