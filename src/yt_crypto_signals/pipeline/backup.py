"""Nightly SQLite backup with retention.

Uses SQLite's native backup API rather than copying the file: a plain copy
taken while a timer-driven run is writing can produce a torn, unusable
database.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from ..config import Config
from ..db import backup_to
from ..logging import get_logger
from ..models import utcnow

log = get_logger("backup")


def backup(cfg: Config) -> Path:
    directory = cfg.settings.data_dir / "backups"
    target = directory / f"ycs-{utcnow():%Y%m%d-%H%M%S}.db"
    path = backup_to(target, cfg)
    log.info("backup written", extra={"path": str(path), "bytes": path.stat().st_size})
    return path


def prune_backups(cfg: Config) -> int:
    """Delete backups older than the retention window."""
    directory = cfg.settings.data_dir / "backups"
    if not directory.exists():
        return 0
    cutoff = utcnow() - timedelta(days=cfg.app.health.backup_retention_days)
    removed = 0
    for path in directory.glob("ycs-*.db"):
        try:
            stamp = path.stem.removeprefix("ycs-")
            from datetime import datetime

            taken = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=cutoff.tzinfo)
        except ValueError:
            # Not one of ours; leave it alone rather than deleting blind.
            continue
        if taken < cutoff:
            path.unlink()
            removed += 1
    if removed:
        log.info("old backups pruned", extra={"removed": removed})
    return removed


__all__ = ["backup", "prune_backups"]
