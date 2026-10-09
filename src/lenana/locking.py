"""Single-instance enforcement via an fcntl advisory lock.

A manual `lenana run` must not overlap a timer-driven run: they would both try to
claim the same `discovered` rows and double-charge the extraction step. The
lock is held for the life of the process and released by the OS even if the
process is killed, which a PID file could not guarantee.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .logging import get_logger

log = get_logger("lock")


class AlreadyRunning(RuntimeError):
    """Raised when another process holds the lock."""


@contextmanager
def exclusive_lock(path: Path, wait: bool = False) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the duration of the block.

    With `wait=False` (the default, and what the timers use) a concurrent run
    exits immediately rather than queueing — the next timer tick is only
    minutes away, so waiting would just stack up processes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise AlreadyRunning(
                    f"another lenana process holds {path}; skipping this run"
                ) from exc
            raise
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        log.debug("acquired lock", extra={"lock_path": str(path), "pid": os.getpid()})
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = ["AlreadyRunning", "exclusive_lock"]
