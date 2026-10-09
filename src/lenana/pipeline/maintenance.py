"""Daily and weekly work driven by the single pipeline timer, in the display timezone."""

from __future__ import annotations

from collections.abc import Callable

from sqlmodel import Session

from ..config import Config
from ..enums import OutboxKind
from ..models import KV, utcnow
from ..notify.outbox import enqueue
from .backup import backup, prune_backups
from .health import health
from .runner import StageResult
from .scorecard import build_scorecard, render_scorecard


def _once(session: Session, name: str, period: str, task: Callable[[], StageResult]) -> StageResult:
    """Persist completion only after work succeeds; exceptions retry on the next run."""
    key = f"maintenance:{name}"
    previous = session.get(KV, key)
    if previous is not None and previous.value == period:
        return StageResult(stage=name, skipped=1)
    result = task()
    result.stage = name
    session.merge(KV(key=key, value=period))
    session.commit()
    return result


def daily_backup(session: Session, cfg: Config) -> StageResult:
    day = utcnow().astimezone(cfg.settings.tz).date().isoformat()

    def task() -> StageResult:
        path = backup(cfg)
        removed = prune_backups(cfg)
        return StageResult(
            stage="daily_backup", processed=1, notes={"backup": str(path), "pruned": removed}
        )

    return _once(session, "daily_backup", day, task)


def daily_health(session: Session, cfg: Config) -> StageResult:
    day = utcnow().astimezone(cfg.settings.tz).date().isoformat()
    # A completed check reporting problems is still completed; its warning is queued.
    return _once(session, "daily_health", day, lambda: health(session, cfg))


def weekly_scorecard(session: Session, cfg: Config) -> StageResult:
    local = utcnow().astimezone(cfg.settings.tz)
    year, week, _ = local.isocalendar()
    period = f"{year}-W{week:02d}"

    def task() -> StageResult:
        rows = enqueue(
            session,
            f"scorecard:{period}",
            OutboxKind.SCORECARD,
            render_scorecard(build_scorecard(session, cfg)),
            cfg,
        )
        return StageResult(stage="weekly_scorecard", processed=len(rows))

    return _once(session, "weekly_scorecard", period, task)
