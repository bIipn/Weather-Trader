"""Discover Kalshi daily temperature markets and parse them into typed brackets."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .kalshi import KalshiClient, fnum
from .stations import Station, resolve_station

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")


@dataclass(frozen=True)
class Bracket:
    """The set of official readings that pay YES.  Bounds are compared on the reading itself."""

    kind: str  # between | greater | less | greater_or_equal | less_or_equal
    floor: float | None = None
    cap: float | None = None

    def contains(self, y: float) -> bool:
        if self.kind == "between":
            return self.floor <= y <= self.cap
        if self.kind == "greater":
            return y > self.floor
        if self.kind == "greater_or_equal":
            return y >= self.floor
        if self.kind == "less":
            return y < self.cap
        if self.kind == "less_or_equal":
            return y <= self.cap
        raise ValueError(f"unsupported strike type {self.kind}")

    def bounds(self, step: float = 1.0) -> tuple[float | None, float | None]:
        """Inclusive [lo, hi] on the reading grid (None = open), e.g. 'less 67' → (None, 66)."""
        if self.kind == "between":
            return self.floor, self.cap
        if self.kind == "greater":
            return self.floor + step, None
        if self.kind == "greater_or_equal":
            return self.floor, None
        if self.kind == "less":
            return None, self.cap - step
        if self.kind == "less_or_equal":
            return None, self.cap
        raise ValueError(f"unsupported strike type {self.kind}")

    @property
    def sort_key(self) -> float:
        lo, hi = self.bounds()
        return lo if lo is not None else (hi - 1000 if hi is not None else 0)

    def label(self, unit: str = "F") -> str:
        lo, hi = self.bounds()
        fmt = lambda v: f"{v:g}°"  # noqa: E731
        if lo is None:
            return f"≤{fmt(hi)}"
        if hi is None:
            return f"≥{fmt(lo)}"
        return fmt(lo) if lo == hi else f"{lo:g}–{fmt(hi)}"


@dataclass
class WeatherMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    bracket: Bracket
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float
    yes_ask_size: float
    last_price: float | None
    volume: float
    open_interest: float
    close_time: datetime | None
    status: str
    result: str = ""

    @property
    def no_bid(self) -> float | None:
        return None if self.yes_ask is None else round(1 - self.yes_ask, 4)

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


@dataclass
class WeatherEvent:
    event_ticker: str
    series_ticker: str
    station: Station
    station_source: str
    day: date
    kind: str  # "high" | "low"
    unit: str  # "F" | "C"
    markets: list[WeatherMarket] = field(default_factory=list)

    @property
    def close_time(self) -> datetime | None:
        times = [m.close_time for m in self.markets if m.close_time]
        return min(times) if times else None

    @property
    def step(self) -> float:
        return 1.0

    def implied_mean(self) -> float | None:
        """Rough market-implied expectation from bracket mids (for display only)."""
        num = den = 0.0
        for m in self.markets:
            if m.mid is None:
                continue
            lo, hi = m.bracket.bounds()
            center = (lo + hi) / 2 if lo is not None and hi is not None else (lo + 1 if lo is not None else hi - 1)
            num += m.mid * center
            den += m.mid
        return num / den if den > 0.5 else None


def parse_event_date(event_ticker: str) -> date | None:
    m = _EVENT_DATE_RE.search(event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))


def parse_kind(text: str) -> str | None:
    t = (text or "").lower()
    if "maximum" in t or "highest" in t or "high temp" in t:
        return "high"
    if "minimum" in t or "lowest" in t or "low temp" in t:
        return "low"
    return None


def parse_unit(text: str) -> str:
    t = (text or "").lower()
    if "celsius" in t or "°c" in t:
        return "C"
    return "F"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def market_from_api(m: dict, series_ticker: str) -> WeatherMarket | None:
    stype = m.get("strike_type")
    if stype not in ("between", "greater", "less", "greater_or_equal", "less_or_equal"):
        return None
    bracket = Bracket(stype, fnum(m.get("floor_strike")), fnum(m.get("cap_strike")))
    return WeatherMarket(
        ticker=m["ticker"],
        event_ticker=m.get("event_ticker", ""),
        series_ticker=series_ticker,
        bracket=bracket,
        yes_bid=_price(m.get("yes_bid_dollars")),
        yes_ask=_price(m.get("yes_ask_dollars")),
        yes_bid_size=fnum(m.get("yes_bid_size_fp")) or 0.0,
        yes_ask_size=fnum(m.get("yes_ask_size_fp")) or 0.0,
        last_price=fnum(m.get("last_price_dollars")),
        volume=fnum(m.get("volume_fp")) or 0.0,
        open_interest=fnum(m.get("open_interest_fp")) or 0.0,
        close_time=_parse_time(m.get("close_time")),
        status=m.get("status", ""),
        result=m.get("result") or "",
    )


def _price(value) -> float | None:
    """Kalshi reports an empty side as 0 (bid) or 1 (ask); treat those as 'no quote'."""
    p = fnum(value)
    if p is None or p <= 0 or p >= 1:
        return None
    return p


def events_from_markets(raw_markets: list[dict], series_ticker: str, series_title: str = "") -> list[WeatherEvent]:
    by_event: dict[str, WeatherEvent] = {}
    for raw in raw_markets:
        rules = raw.get("rules_primary") or ""
        market = market_from_api(raw, series_ticker)
        if market is None:
            continue
        ev = by_event.get(market.event_ticker)
        if ev is None:
            day = parse_event_date(market.event_ticker)
            kind = parse_kind(rules) or parse_kind(series_title) or parse_kind(raw.get("title", ""))
            city = (raw.get("custom_strike") or {}).get("City")
            station, how = resolve_station(series_ticker, rules, city)
            if day is None or kind is None or station is None:
                continue
            ev = WeatherEvent(market.event_ticker, series_ticker, station, how, day, kind, parse_unit(rules))
            by_event[market.event_ticker] = ev
        ev.markets.append(market)
    for ev in by_event.values():
        ev.markets.sort(key=lambda mk: mk.bracket.sort_key)
    return sorted(by_event.values(), key=lambda e: (e.day, e.series_ticker))


def is_daily_temperature_series(s: dict) -> bool:
    if s.get("frequency") != "daily":
        return False
    t = s.get("ticker", "")
    title = (s.get("title") or "").lower()
    if not (t.startswith("KXHIGH") or t.startswith("KXLOW")):
        return False
    return "temp" in title or "highest" in title or "lowest" in title or "max" in title


class MarketDiscovery:
    def __init__(self, client: KalshiClient, series: list[str] | None = None, series_ttl: float = 6 * 3600):
        self.client = client
        self.explicit_series = series
        self.series_ttl = series_ttl
        self._series_cache: tuple[float, list[dict]] | None = None

    def weather_series(self) -> list[dict]:
        if self.explicit_series:
            return [{"ticker": t, "title": ""} for t in self.explicit_series]
        now = time.monotonic()
        if self._series_cache and self._series_cache[0] > now:
            return self._series_cache[1]
        series = [s for s in self.client.series_list("Climate and Weather") if is_daily_temperature_series(s)]
        self._series_cache = (now + self.series_ttl, series)
        return series

    def open_events(self, now: datetime | None = None) -> list[WeatherEvent]:
        events: list[WeatherEvent] = []
        for s in self.weather_series():
            raw = self.client.markets(series_ticker=s["ticker"], status="open")
            if raw:
                events.extend(events_from_markets(raw, s["ticker"], s.get("title", "")))
        now = now or datetime.now(timezone.utc)
        return [e for e in events if e.close_time is None or e.close_time > now]
