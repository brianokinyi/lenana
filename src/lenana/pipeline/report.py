"""Tracker price snapshots and deduplicated daily Telegram reports."""

from __future__ import annotations

from sqlmodel import Session

from ..config import Config
from ..tracker import queue_daily_report, refresh_current_prices
from .runner import StageResult


def tracker_prices(session: Session, cfg: Config) -> StageResult:
    return StageResult(stage="tracker_prices", processed=refresh_current_prices(session, cfg))


def daily_report(session: Session, cfg: Config) -> StageResult:
    return StageResult(stage="daily_report", processed=queue_daily_report(session, cfg))
