"""CoinGecko client for the demo (free) tier.

Tier constraints that shape every decision here, verified against the API docs:

* **Auto-granularity on `market_chart/range`**: 5-minutely within 1 day,
  **hourly for 2-90 day ranges**, daily beyond 90. The `interval` parameter is
  paid-only, so granularity is chosen by picking the range, not by asking.
* **`/coins/{id}/ohlc`** is available on demo and returns true per-candle
  high/low (4-hour candles for 3-30 days). This is what makes "was the target
  touched" honest: hourly closes would miss an intraday wick.
  Its `days` parameter is a now-anchored enum, which is why scoring stores the
  window high/low rather than recomputing later.
* **Rate limits** (~30 calls/min, 10k/month) make caching mandatory, not an
  optimisation. Historical price points never change, so `price_cache` is a
  permanent cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, func, select

from ..config import Config
from ..httpc import build_client, retry_http
from ..logging import get_logger
from ..models import Coin, PriceCache, utcnow
from ..utils import floor_hour

#: httpx accepts only primitives as query values.
QueryParams = dict[str, str | int | float | bool | None]

log = get_logger("prices")

API_BASE = "https://api.coingecko.com/api/v3"
#: Demo keys use this header; a Pro key would use x-cg-pro-api-key instead.
DEMO_KEY_HEADER = "x-cg-demo-api-key"

#: /coins/markets caps per_page at 250, so a top-1000 list is 4 calls.
MARKETS_PAGE_SIZE = 250


class CoinGeckoError(RuntimeError):
    pass


@dataclass
class PricePoint:
    at: datetime
    price_usd: float


class CoinGecko:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        headers = {"accept": "application/json"}
        if cfg.settings.coingecko_api_key:
            headers[DEMO_KEY_HEADER] = cfg.settings.coingecko_api_key
        self._headers = headers
        self.calls_made = 0

    @retry_http()
    def _get(self, path: str, params: QueryParams | None = None) -> object:
        self.calls_made += 1
        # Not proxied: CoinGecko does not block us, and a residential proxy
        # would only add latency and burn metered bandwidth.
        with build_client(self.cfg, base_url=API_BASE, headers=self._headers) as client:
            response = client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    # ---------------- coin list ----------------

    def refresh_coin_list(self, session: Session) -> int:
        """Cache the top-N coins by market cap.

        Symbol collisions are rampant (dozens of tokens claim "BTC"), and
        resolving them needs market caps, so the market list is the thing we
        cache rather than the plain `/coins/list`.
        """
        wanted = self.cfg.app.enrichment.coin_list_size
        stored = 0
        for page in range(1, (wanted // MARKETS_PAGE_SIZE) + 1):
            payload = self._get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": MARKETS_PAGE_SIZE,
                    "page": page,
                    "sparkline": "false",
                },
            )
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                coin_id = item.get("id")
                if not coin_id:
                    continue
                existing = session.get(Coin, coin_id)
                if existing is None:
                    existing = Coin(coingecko_id=coin_id, symbol="", name="")
                existing.symbol = (item.get("symbol") or "").upper()
                existing.name = item.get("name") or ""
                existing.market_cap = item.get("market_cap")
                existing.market_cap_rank = item.get("market_cap_rank")
                existing.refreshed_at = utcnow()
                session.add(existing)
                stored += 1
            session.commit()

        log.info("coin list refreshed", extra={"coins": stored, "api_calls": self.calls_made})
        return stored

    def coin_list_is_stale(self, session: Session) -> bool:
        newest = session.exec(select(func.max(col(Coin.refreshed_at)))).first()
        if newest is None:
            return True
        age = utcnow() - newest
        return age > timedelta(hours=self.cfg.app.enrichment.coin_list_refresh_hours)

    # ---------------- prices ----------------

    def price_at(self, session: Session, coingecko_id: str, when: datetime) -> float | None:
        """Price at a point in time, from cache when possible.

        A narrow range around `when` keeps us inside the hourly-granularity
        band. Every point returned is cached, so neighbouring lookups (a second
        call in the same video) cost nothing.
        """
        hour = floor_hour(when)
        cached = session.exec(
            select(PriceCache)
            .where(PriceCache.coingecko_id == coingecko_id)
            .where(PriceCache.ts_hour == hour)
        ).first()
        if cached is not None:
            return cached.price_usd

        # ±6 hours: wide enough to survive a sparse series, narrow enough to
        # stay well inside the 2-90 day hourly band.
        points = self.market_chart_range(
            coingecko_id, when - timedelta(hours=6), when + timedelta(hours=6)
        )
        if not points:
            return None

        self._cache_points(session, coingecko_id, points)
        nearest = min(points, key=lambda p: abs((p.at - when).total_seconds()))
        # Reject a "nearest" point that is a day away: that means the series
        # has a gap here and the number would be misleading.
        if abs((nearest.at - when).total_seconds()) > 86400:
            log.warning(
                "no price near requested time",
                extra={"coin": coingecko_id, "requested": when.isoformat()},
            )
            return None
        return nearest.price_usd

    def market_chart_range(
        self, coingecko_id: str, start: datetime, end: datetime
    ) -> list[PricePoint]:
        payload = self._get(
            f"/coins/{coingecko_id}/market_chart/range",
            {
                "vs_currency": "usd",
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        if not isinstance(payload, dict):
            return []
        points: list[PricePoint] = []
        for entry in payload.get("prices") or []:
            try:
                ts_ms, price = entry[0], entry[1]
            except (IndexError, TypeError):
                continue
            points.append(
                PricePoint(
                    at=datetime.fromtimestamp(ts_ms / 1000, tz=utcnow().tzinfo),
                    price_usd=float(price),
                )
            )
        return points

    def ohlc_window(
        self, coingecko_id: str, start: datetime, end: datetime
    ) -> tuple[float, float] | None:
        """(high, low) across [start, end] from OHLC candles.

        Uses true per-candle highs and lows, so a target touched intraday is
        detected even when the price closed away from it. `days` must be one of
        the API's allowed values, so it is snapped up to the nearest bucket and
        the candles are then trimmed client-side.
        """
        span_days = max((utcnow() - start).total_seconds() / 86400, 1.0)
        buckets = self.cfg.app.scoring.ohlc_days_buckets
        days = next((b for b in buckets if b >= span_days), buckets[-1])

        payload = self._get(f"/coins/{coingecko_id}/ohlc", {"vs_currency": "usd", "days": days})
        if not isinstance(payload, list) or not payload:
            return None

        highs: list[float] = []
        lows: list[float] = []
        for candle in payload:
            try:
                ts_ms, _open, high, low, _close = candle[:5]
            except (IndexError, TypeError, ValueError):
                continue
            at = datetime.fromtimestamp(ts_ms / 1000, tz=utcnow().tzinfo)
            if start <= at <= end:
                highs.append(float(high))
                lows.append(float(low))

        if not highs or not lows:
            return None
        return max(highs), min(lows)

    def _cache_points(self, session: Session, coingecko_id: str, points: list[PricePoint]) -> None:
        """Store one row per hour, ignoring duplicates."""
        from sqlalchemy.dialects.sqlite import insert

        rows = {}
        for point in points:
            rows[floor_hour(point.at)] = point.price_usd
        if not rows:
            return
        statement = insert(PriceCache).values(
            [
                {
                    "coingecko_id": coingecko_id,
                    "ts_hour": hour,
                    "price_usd": price,
                    "source": "market_chart_range",
                    "fetched_at": utcnow(),
                }
                for hour, price in rows.items()
            ]
        )
        # ON CONFLICT DO NOTHING: concurrent runs can race on the same hour,
        # and the cached value is identical either way.
        session.exec(statement.on_conflict_do_nothing(index_elements=["coingecko_id", "ts_hour"]))
        session.commit()


def resolve_symbol(session: Session, symbol: str) -> tuple[str | None, str]:
    """Map a ticker to a CoinGecko id, resolving collisions by market cap.

    Returns `(coingecko_id, status)` where status is one of `mapped`,
    `ambiguous` or `unmapped`. "Ambiguous" still returns the best candidate so
    enrichment can proceed, but the call is flagged for review.
    """
    normalised = symbol.strip().upper().lstrip("$")
    candidates = list(
        session.exec(
            select(Coin).where(Coin.symbol == normalised).order_by(col(Coin.market_cap).desc())
        ).all()
    )
    if not candidates:
        return None, "unmapped"
    if len(candidates) == 1:
        return candidates[0].coingecko_id, "mapped"

    best, second = candidates[0], candidates[1]
    best_cap = best.market_cap or 0
    second_cap = second.market_cap or 0
    # A clear leader (>5x the runner-up) is a confident match. Otherwise the
    # ticker genuinely is ambiguous and a human should look.
    if best_cap > 0 and best_cap >= second_cap * 5:
        return best.coingecko_id, "mapped"
    return best.coingecko_id, "ambiguous"


__all__ = [
    "API_BASE",
    "DEMO_KEY_HEADER",
    "CoinGecko",
    "CoinGeckoError",
    "PricePoint",
    "resolve_symbol",
]
