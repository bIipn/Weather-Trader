"""Walk-forward backtest against real Kalshi prices.

For every settled city-day in the window we rebuild the day-ahead forecast exactly as it
could have been known the evening before (archived model runs, calibration fitted only on
data *before* the window), read the actual Kalshi bid/ask at the decision time from the
candlestick history, apply the live strategy (taker orders only — resting fills can't be
replayed honestly from candles) and settle against the real market result.

Ensembles have no public archive, so the backtest uses deterministic models only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone

from .calibrate import archived_extremes, calibrate_station
from .config import Settings
from .http import Http, HttpError
from .kalshi import PROD_URL, KalshiClient, fnum
from .markets import WeatherEvent, events_from_markets
from .model.distribution import OutcomeDistribution, ScaleMixture
from .stations import climate_day_window
from .strategy import evaluate_market
from .weather.openmeteo import DEFAULT_MODELS, OpenMeteo

log = logging.getLogger("wxbot")


@dataclass
class Trade:
    day: date
    station: str
    ticker: str
    side: str
    price: float
    count: int
    prob: float
    fee: float
    won: bool

    @property
    def cost(self) -> float:
        return self.count * self.price + self.fee

    @property
    def pnl(self) -> float:
        return (self.count if self.won else 0) - self.cost


@dataclass
class SeriesStats:
    series: str
    station: str
    events: int = 0
    markets: int = 0
    brier_model: float = 0.0
    brier_market: float = 0.0
    trades: list[Trade] = field(default_factory=list)
    calibrated_days: int = 0

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def cost(self) -> float:
        return sum(t.cost for t in self.trades)


@dataclass
class BacktestResult:
    start: date
    end: date
    stats: list[SeriesStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def trades(self) -> list[Trade]:
        return [t for s in self.stats for t in s.trades]

    def summary(self) -> dict:
        trades = self.trades
        markets = sum(s.markets for s in self.stats)
        cost = sum(t.cost for t in trades)
        pnl = sum(t.pnl for t in trades)
        return {
            "window": f"{self.start} → {self.end}",
            "events": sum(s.events for s in self.stats),
            "brackets_scored": markets,
            "brier_model": round(sum(s.brier_model for s in self.stats) / markets, 4) if markets else None,
            "brier_market": round(sum(s.brier_market for s in self.stats) / markets, 4) if markets else None,
            "trades": len(trades),
            "win_rate": round(sum(t.won for t in trades) / len(trades), 3) if trades else None,
            "cost": round(cost, 2),
            "pnl": round(pnl, 2),
            "roi": round(pnl / cost, 4) if cost else None,
        }


def _close(candle: dict, side: str) -> float | None:
    block = candle.get(side) or {}
    return fnum(block.get("close_dollars", block.get("close")))


def _quote_at(candles: list[dict], ts: int) -> tuple[float | None, float | None]:
    best = None
    for c in candles:
        if c.get("end_period_ts", 0) <= ts and (best is None or c["end_period_ts"] > best["end_period_ts"]):
            best = c
    if not best:
        return None, None
    bid, ask = _close(best, "yes_bid"), _close(best, "yes_ask")
    bid = bid if bid and 0 < bid < 1 else None
    ask = ask if ask and 0 < ask < 1 else None
    return bid, ask


def _settled_events(client: KalshiClient, series: str, start: date, end: date) -> list[WeatherEvent]:
    lo = int(datetime.combine(start, time(0), timezone.utc).timestamp())
    hi = int(datetime.combine(end + timedelta(days=9), time(0), timezone.utc).timestamp())
    raw = client.markets(series_ticker=series, status="settled", min_settled_ts=lo, max_settled_ts=hi)
    try:
        raw += client.historical_markets(series_ticker=series)
    except HttpError:
        pass
    seen, uniq = set(), []
    for m in raw:
        if m["ticker"] not in seen and (m.get("result") in ("yes", "no")):
            seen.add(m["ticker"])
            uniq.append(m)
    return [e for e in events_from_markets(uniq, series) if start <= e.day <= end]


def run_backtest(http: Http, settings: Settings, series: list[str], days: int = 45, decision_hour: int = 22,
                 bankroll: float = 1000.0, calibration_days: int = 365, end: date | None = None) -> BacktestResult:
    client = KalshiClient(PROD_URL, None, http)
    om = OpenMeteo(http, settings.open_meteo_api_key or None)
    models = settings.model.models or DEFAULT_MODELS
    cfg = replace(settings.strategy, execution="taker")
    end = end or (date.today() - timedelta(days=2))
    start = end - timedelta(days=days - 1)
    result = BacktestResult(start, end)
    for s in series:
        try:
            events = _settled_events(client, s, start, end)
        except HttpError as exc:
            result.errors.append(f"{s}: {exc}")
            continue
        if not events:
            result.errors.append(f"{s}: no settled events in window")
            continue
        station, kind, unit = events[0].station, events[0].kind, events[0].unit
        stats = SeriesStats(s, station.code)
        try:
            cal = calibrate_station(http, settings, station, kind, calibration_days, unit, end=start - timedelta(days=1))
            fc = archived_extremes(om, station, models, start, end, kind, unit, leads=(1,))
        except HttpError as exc:
            result.errors.append(f"{s}: weather data: {exc}")
            continue
        stats.calibrated_days = cal.n_days
        for ev in events:
            ws, we = climate_day_window(station, ev.day)
            decision = ws - timedelta(hours=24 - decision_hour)  # decision_hour local std time, day before
            peak = ws + timedelta(hours=15 if kind == "high" else 6)
            lead_h = (peak - decision).total_seconds() / 3600
            pairs = [(fc[(m, 1)][ev.day] + cal.model_bias(m, lead_h), cal.weight(m))
                     for m in models if ev.day in fc.get((m, 1), {})]
            if len(pairs) < 3:
                continue
            W = sum(w for _, w in pairs)
            mu = sum(v * w for v, w in pairs) / W
            spread = math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / W)
            sigma = max(cal.sigma_at(lead_h), settings.model.disagreement_factor * spread)
            dist = OutcomeDistribution(ScaleMixture(mu, sigma, settings.model.tail_weight,
                                                    settings.model.tail_scale), kind)
            ts = int(decision.timestamp())
            try:
                candles = client.event_candlesticks(s, ev.event_ticker, ts - 3 * 3600, ts, 60)
            except HttpError:
                candles = {}
            stats.events += 1
            for m in ev.markets:
                series_c = candles.get(m.ticker)
                if series_c is None:
                    try:
                        series_c = client.candlesticks(s, m.ticker, ts - 3 * 3600, ts, 60, historical=True)
                    except HttpError:
                        series_c = []
                bid, ask = _quote_at(series_c, ts)
                won_yes = m.result == "yes"
                p = dist.prob(m.bracket)
                if bid is not None and ask is not None:
                    mid = (bid + ask) / 2
                    stats.markets += 1
                    stats.brier_model += (p - won_yes) ** 2
                    stats.brier_market += (mid - won_yes) ** 2
                quote = replace(m, yes_bid=bid, yes_ask=ask, yes_bid_size=1e6, yes_ask_size=1e6)
                view = evaluate_market(quote, p, cfg, bankroll)
                o = view.best
                if o is not None:
                    stats.trades.append(Trade(ev.day, station.code, m.ticker, o.side, o.price, o.count, o.prob,
                                              o.fee, won_yes if o.side == "yes" else not won_yes))
        result.stats.append(stats)
        log.info("%s: %d events, %d trades, pnl %.2f", s, stats.events, len(stats.trades), stats.pnl)
    return result
