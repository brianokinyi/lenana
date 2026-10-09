"""Health stage: notice when the system is quietly broken.

The failure mode worth guarding against is silence — a blocked proxy or an
exhausted quota produces no alerts, which looks exactly like a slow news day.

Health alerts are deduplicated by day and condition, so a persistent problem
produces one message per day rather than one every 30 minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlmodel import Session, col, func, select

from ..config import Config
from ..enums import FailureKind, OutboxKind, OutboxState, VideoState
from ..httpc import build_client
from ..logging import get_logger
from ..models import ApiUsage, Outbox, StageRun, Video, utcnow
from ..notify.format import render_health_alert
from ..notify.outbox import enqueue_committed
from .runner import StageResult

log = get_logger("pipeline.health")


@dataclass
class HealthReport:
    problems: list[str] = field(default_factory=list)
    checks: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_health(session: Session, cfg: Config) -> HealthReport:
    report = HealthReport()
    hc = cfg.app.health
    now = utcnow()

    # --- transcripts failing for a sustained period ---
    cutoff = now - timedelta(hours=hc.transcript_failure_alert_hours)
    stuck = session.exec(
        select(func.count())
        .select_from(Video)
        .where(Video.state == VideoState.TRANSCRIPT_FAILED)
        .where(col(Video.updated_at) <= cutoff)
    ).one()
    report.checks["transcripts_failing_over_window"] = stuck
    if stuck:
        report.problems.append(
            f"{stuck} video(s) have had transcript failures for more than "
            f"{hc.transcript_failure_alert_hours}h"
        )

    # --- videos given up on entirely ---
    permanent = session.exec(
        select(func.count()).select_from(Video).where(Video.state == VideoState.PERMANENT_FAILED)
    ).one()
    report.checks["permanent_failed"] = permanent
    if permanent:
        report.problems.append(f"{permanent} video(s) exhausted the retry ladder")

    # --- a proxy that looks dead: everything blocked, nothing succeeding ---
    blocked = session.exec(
        select(func.count())
        .select_from(Video)
        .where(Video.last_error_kind == FailureKind.BLOCKED.value)
        .where(col(Video.updated_at) >= now - timedelta(hours=2))
    ).one()
    report.checks["blocked_last_2h"] = blocked
    if blocked >= 3:
        report.problems.append(
            f"{blocked} transcript fetches were blocked in the last 2h — the proxy may be "
            f"dead or this IP is rate-limited (PROXY_PROVIDER={cfg.settings.proxy_provider})"
        )

    # --- repeatedly failing stages ---
    recent_runs = session.exec(
        select(StageRun.stage, func.count())
        .where(col(StageRun.started_at) >= now - timedelta(hours=6))
        .where(col(StageRun.ok).is_(False))
        .group_by(col(StageRun.stage))
    ).all()
    failing = {str(stage): int(count) for stage, count in recent_runs if count >= 3}
    report.checks["failing_stages"] = failing
    for stage, count in failing.items():
        report.problems.append(f"stage '{stage}' failed {count} times in the last 6h")

    # --- YouTube quota ---
    today = f"{now:%Y-%m-%d}"
    used = session.exec(
        select(func.coalesce(func.sum(ApiUsage.units), 0))
        .where(ApiUsage.day == today)
        .where(ApiUsage.service == "youtube")
    ).one()
    pct = (int(used) / hc.youtube_quota_daily_units) * 100
    report.checks["youtube_quota_pct"] = round(pct, 1)
    if pct >= hc.youtube_quota_warn_pct:
        report.problems.append(
            f"YouTube API quota at {pct:.0f}% of {hc.youtube_quota_daily_units} units today"
        )

    # --- outbox backlog ---
    stale_outbox = session.exec(
        select(func.count())
        .select_from(Outbox)
        .where(Outbox.state == OutboxState.PENDING)
        .where(col(Outbox.created_at) <= now - timedelta(hours=1))
    ).one()
    failed_outbox = session.exec(
        select(func.count()).select_from(Outbox).where(Outbox.state == OutboxState.FAILED)
    ).one()
    report.checks["outbox_pending_over_1h"] = stale_outbox
    report.checks["outbox_failed"] = failed_outbox
    if stale_outbox:
        report.problems.append(f"{stale_outbox} alert(s) have been pending for over an hour")
    if failed_outbox:
        report.problems.append(f"{failed_outbox} alert(s) permanently failed to send")

    return report


def ping_healthcheck(cfg: Config, suffix: str = "") -> bool:
    """Ping an external watchdog (healthchecks.io style).

    This is what catches the case the health stage structurally cannot: the
    whole host being down, in which case nothing local runs to notice.
    """
    url = cfg.settings.healthcheck_url
    if not url:
        return False
    try:
        with build_client(cfg, use_proxy=False) as client:
            client.get(url + suffix)
        return True
    except httpx.HTTPError as exc:
        # Never fatal: a watchdog outage must not fail the pipeline run.
        log.warning("healthcheck ping failed", extra={"error": str(exc)})
        return False


def health(session: Session, cfg: Config, notify: bool = True) -> StageResult:
    """Run the checks and queue one alert per day per problem set."""
    result = StageResult(stage="health")
    report = check_health(session, cfg)
    result.notes.update(report.checks)
    result.notes["problems"] = report.problems

    if report.ok:
        log.info("health ok", extra=report.checks)
        ping_healthcheck(cfg)
        return result

    log.warning("health problems detected", extra={"problems": report.problems})
    result.failed = len(report.problems)

    if notify:
        # Dedupe on the day plus the problem set, so a new problem appearing
        # later the same day still gets through.
        import hashlib

        digest = hashlib.sha256("|".join(sorted(report.problems)).encode()).hexdigest()[:12]
        enqueue_committed(
            session,
            dedupe_key=f"health:{utcnow():%Y-%m-%d}:{digest}",
            kind=OutboxKind.HEALTH,
            body=render_health_alert("lenana health warning", report.problems),
            cfg=cfg,
        )
    return result


__all__ = ["HealthReport", "check_health", "health", "ping_healthcheck"]
