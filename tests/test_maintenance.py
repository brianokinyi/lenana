from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from lenana.config import Config
from lenana.models import KV, Outbox
from lenana.pipeline import maintenance, orchestrate
from lenana.pipeline.runner import StageResult


def test_daily_tasks_follow_adelaide_midnight_and_persist(
    session: Session,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.settings.display_tz = "Australia/Adelaide"
    now = datetime(2026, 10, 10, 13, 29, tzinfo=UTC)  # 11:59PM Adelaide
    monkeypatch.setattr(maintenance, "utcnow", lambda: now)
    backups: list[int] = []
    monkeypatch.setattr(
        maintenance, "backup", lambda cfg: backups.append(1) or cfg.settings.data_dir
    )
    monkeypatch.setattr(maintenance, "prune_backups", lambda cfg: 0)
    assert maintenance.daily_backup(session, config).processed == 1
    assert maintenance.daily_backup(session, config).skipped == 1
    session.expire_all()
    assert maintenance.daily_backup(session, config).skipped == 1
    now = datetime(2026, 10, 10, 13, 30, tzinfo=UTC)  # next Adelaide day
    assert maintenance.daily_backup(session, config).processed == 1
    assert len(backups) == 2


def test_failed_backup_retries_but_completed_health_findings_do_not(
    session: Session,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_cfg: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "backup", fail)
    with pytest.raises(OSError):
        maintenance.daily_backup(session, config)
    assert session.get(KV, "maintenance:daily_backup") is None
    monkeypatch.setattr(maintenance, "backup", lambda cfg: cfg.settings.data_dir)
    monkeypatch.setattr(maintenance, "prune_backups", lambda cfg: 0)
    assert maintenance.daily_backup(session, config).processed == 1
    monkeypatch.setattr(maintenance, "health", lambda *args: StageResult(stage="health", failed=2))
    assert maintenance.daily_health(session, config).failed == 2
    assert maintenance.daily_health(session, config).skipped == 1


def test_weekly_scorecard_uses_iso_year_and_adelaide_week_boundary(
    session: Session,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.settings.display_tz = "Australia/Adelaide"
    now = datetime(2027, 1, 3, 13, 29, tzinfo=UTC)  # Sunday 11:59PM, ISO week 2026-W53
    monkeypatch.setattr(maintenance, "utcnow", lambda: now)
    assert maintenance.weekly_scorecard(session, config).processed == 1
    assert maintenance.weekly_scorecard(session, config).skipped == 1
    now = datetime(2027, 1, 3, 13, 30, tzinfo=UTC)  # Monday, ISO week 2027-W01
    assert maintenance.weekly_scorecard(session, config).processed == 1
    assert {r.dedupe_key for r in session.exec(select(Outbox)).all()} == {
        "scorecard:2026-W53",
        "scorecard:2027-W01",
    }


def test_pipeline_still_flushes_outbox_after_maintenance_failure(
    session: Session,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lenana.notify.outbox import SendSummary

    def fail(_session: Session, _cfg: Config) -> StageResult:
        raise OSError("backup failed")

    monkeypatch.setitem(orchestrate.STAGES, "daily_backup", fail)
    monkeypatch.setattr(orchestrate, "send_pending", lambda *args: SendSummary(sent=1, claimed=1))
    report = orchestrate.run_pipeline(session, config, only=["daily_backup", "weekly_scorecard"])
    assert report.errors
    assert report.outbox["sent"] == 1
    assert report.stages[0]["stage"] == "weekly_scorecard"
