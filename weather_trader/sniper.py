"""Dead-bracket sniping: sell brackets that new observations have already ruled out.

The official daily high can never be below any temperature the station actually reported
during the climate day (and the low never above).  So the moment a report shows 64°F,
every "63° or below" bracket is worth exactly zero.  If anyone is still bidding for it in
the minutes after the report, selling YES (buying NO) into that bid is close to riskless
money.  The edge is pure speed: other bots do the same thing, so it only pays if quotes
lag the observation feed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .config import Settings, StrategyConfig
from .http import Http, HttpError
from .kalshi import PROD_URL, KalshiClient, fnum
from .markets import WeatherEvent, WeatherMarket
from .stations import climate_day_window
from .strategy import Order, taker_fee
from .weather.iem import IEM
from .weather.nws import Observation, despike, official_reading

log = logging.getLogger("wxbot")


def observed_extreme(obs: list[Observation], start: datetime, end: datetime, kind: str = "high",
                     unit: str = "F") -> float | None:
    """The most extreme official-grid reading reported inside [start, end)."""
    inside = despike([o for o in obs if o.temp_c is not None and start <= o.time < end])
    vals = [official_reading(o.temp_c, unit) for o in inside]
    if not vals:
        return None
    return max(vals) if kind == "high" else min(vals)


def is_dead(market: WeatherMarket, extreme: float, kind: str, margin: float = 1.0) -> bool:
    """True when the observed extreme already rules this bracket out.

    A year of reports showed the official value landing a degree *inside* an hourly
    reading on 0-3 days per station (rounding, quality control), so by default a bracket
    only counts as dead once the reading clears it by more than `margin`.
    """
    lo, hi = market.bracket.bounds()
    if kind == "high":
        return hi is not None and hi < extreme - margin
    return lo is not None and lo > extreme + margin


def snipe_orders(event: WeatherEvent, extreme: float, cfg: StrategyConfig, min_bid: float = 0.02,
                 max_contracts: int = 200) -> list[Order]:
    """Sell YES (buy NO) into any bid left on a bracket the observations have killed."""
    orders = []
    for m in event.markets:
        if m.yes_bid is None or m.yes_bid < min_bid or not is_dead(m, extreme, event.kind):
            continue
        no_price = round(1 - m.yes_bid, 2)
        count = int(min(m.yes_bid_size, max_contracts))
        if count < 1:
            continue
        fee = taker_fee(no_price, count, cfg.fee_multiplier)
        edge = 1 - no_price - fee / count
        if edge > 0:
            orders.append(Order(m.ticker, m.event_ticker, "no", no_price, count, "take", 1.0, edge, fee,
                                note=f"dead: observed {extreme:g}"))
    return orders


@dataclass
class SnipeSample:
    series: str
    station: str
    day: str
    ticker: str
    bracket: str
    obs_time: str
    reading: float
    outcome: int
    bids: list[tuple[int, float | None]] = field(default_factory=list)  # (minutes after report, yes bid)


def run_sniper_backtest(http: Http, settings: Settings, series: list[str], days: int = 30,
                        end: date | None = None, minutes: int = 20) -> tuple[list[SnipeSample], list[str]]:
    """For every bracket killed by a report, record the YES bid minute by minute afterwards."""
    from .backtest import _settled_events  # shared helper

    client = KalshiClient(PROD_URL, None, http)
    iem = IEM(http)
    end = end or (date.today() - timedelta(days=2))
    start = end - timedelta(days=days - 1)
    samples: list[SnipeSample] = []
    errors: list[str] = []
    for s in series:
        try:
            events = _settled_events(client, s, start, end)
        except HttpError as exc:
            errors.append(f"{s}: {exc}")
            continue
        if not events:
            continue
        station, kind, unit = events[0].station, events[0].kind, events[0].unit
        w0, _ = climate_day_window(station, start)
        _, w1 = climate_day_window(station, end)
        try:
            obs_all = iem.metars(station.code, w0 - timedelta(hours=1), w1 + timedelta(hours=1))
        except HttpError as exc:
            errors.append(f"{s}: METAR archive: {exc}")
            continue
        for ev in events:
            ws, we = climate_day_window(station, ev.day)
            dead: set[str] = set()
            running = None
            for o in despike([o for o in obs_all if ws <= o.time < we and o.temp_c is not None]):
                r = official_reading(o.temp_c, unit)
                if running is not None and ((kind == "high" and r <= running) or (kind == "low" and r >= running)):
                    continue
                running = r
                newly = [m for m in ev.markets if m.ticker not in dead and is_dead(m, running, kind, margin=0.0)]
                if not newly:
                    continue
                dead.update(m.ticker for m in newly)
                t0 = int(o.time.timestamp())
                try:
                    candles = client.event_candlesticks(s, ev.event_ticker, t0 - 120, t0 + minutes * 60, 1)
                except HttpError:
                    candles = {}
                for m in newly:
                    bids = []
                    for c in candles.get(m.ticker, []):
                        offset = (c.get("end_period_ts", 0) - t0) // 60
                        bid = fnum((c.get("yes_bid") or {}).get("close_dollars"))
                        bids.append((int(offset), bid if bid and 0 < bid < 1 else None))
                    samples.append(SnipeSample(s, station.code, ev.day.isoformat(), m.ticker,
                                               m.bracket.label(ev.unit), o.time.isoformat(), r,
                                               int(m.result == "yes"), sorted(bids)))
        log.info("%s: %d dead-bracket moments", s, sum(1 for x in samples if x.series == s))
    return samples, errors


def summarize_snipes(samples: list[SnipeSample], latencies: tuple[int, ...] = (2, 3, 5, 10),
                     min_bid: float = 0.02) -> dict:
    """How much was left on the table N minutes after each report, per contract."""
    out = {"dead_brackets": len(samples), "violations": sum(x.outcome for x in samples)}
    for lag in latencies:
        hits, value = 0, 0.0
        for x in samples:
            bid = next((b for off, b in x.bids if off >= lag), None)  # the quote standing at that minute
            if bid is not None and bid >= min_bid:
                hits += 1
                no_price = 1 - bid
                value += bid - taker_fee(no_price, 1)
        out[f"after_{lag}min"] = {"opportunities": hits, "profit_per_contract_sum": round(value, 2)}
    return out


def snipes_json(samples: list[SnipeSample]) -> list[dict]:
    return [asdict(x) for x in samples]
