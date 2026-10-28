"""Walk-forward backtest against real Kalshi prices.

Two decision points are replayed for every settled city-day:

* **day-ahead** — 10 PM local standard time the evening before, using the archived model
  runs that were available then (Open-Meteo previous runs, 1-day lead);
* **same-day nowcast** — several hours on the day itself, using the METARs observed up to
  that moment (IEM archive) plus the same day-old model runs for the rest of the day.

Calibration is fitted only on the year *before* the window, so nothing leaks from the
future.  Each bracket at each decision time becomes a `Sample` (model probability, the
real bid/ask from Kalshi's candlestick history, the outcome), and the live strategy is
applied to it with taker orders — resting fills can't be replayed honestly from candles.
Ensembles have no public archive, so the replay uses deterministic models only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from .calibrate import calibrate_station, day_windows
from .config import Settings
from .http import Http, HttpError
from .kalshi import PROD_URL, KalshiClient, fnum
from .markets import WeatherEvent, events_from_markets
from .model.calibration import LEAD_HOURS, CalibrationStore
from .model.distribution import OutcomeDistribution, ScaleMixture
from .model.forecast import Forecaster, Source
from .stations import climate_day_window
from .strategy import evaluate_market
from .weather.iem import IEM
from .weather.openmeteo import DEFAULT_MODELS, Hourly, OpenMeteo, daily_extremes

log = logging.getLogger("wxbot")
DAY_AHEAD_HOUR = 22  # local standard time, the evening before
NOWCAST_HOURS = (11, 14, 17, 20)  # local standard time, on the day


@dataclass
class Sample:
    mode: str
    series: str
    station: str
    day: str
    ticker: str
    bracket: str
    p_model: float
    bid: float | None
    ask: float | None
    outcome: int
    mid_later: float | None = None
    traded: str | None = None  # "yes"/"no" when the strategy took a position
    price: float | None = None
    count: int = 0
    fee: float = 0.0
    pnl: float = 0.0

    @property
    def mid(self) -> float | None:
        return None if self.bid is None or self.ask is None else (self.bid + self.ask) / 2


@dataclass
class BacktestResult:
    start: date
    end: date
    samples: list[Sample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        out = {"window": f"{self.start} → {self.end}"}
        for mode in sorted({s.mode for s in self.samples}):
            ss = [s for s in self.samples if s.mode == mode]
            quoted = [s for s in ss if s.mid is not None]
            trades = [s for s in ss if s.traded]
            cost = sum(s.count * s.price + s.fee for s in trades)
            pnl = sum(s.pnl for s in trades)
            out[mode] = {
                "brackets": len(ss),
                "quoted": len(quoted),
                "brier_model": _brier(quoted, lambda s: s.p_model),
                "brier_market": _brier(quoted, lambda s: s.mid),
                "trades": len(trades),
                "win_rate": round(sum(s.pnl > 0 for s in trades) / len(trades), 3) if trades else None,
                "cost": round(cost, 2),
                "pnl": round(pnl, 2),
                "roi": round(pnl / cost, 4) if cost else None,
            }
        return out


def _brier(samples: list[Sample], prob) -> float | None:
    if not samples:
        return None
    return round(sum((prob(s) - s.outcome) ** 2 for s in samples) / len(samples), 4)


def _close(candle: dict, side: str) -> float | None:
    block = candle.get(side) or {}
    return fnum(block.get("close_dollars", block.get("close")))


def _quote_at(candles: list[dict], ts: int) -> tuple[float | None, float | None]:
    best = None
    for c in candles:
        if c.get("end_period_ts", 0) <= ts and (best is None or c["end_period_ts"] > best["end_period_ts"]):
            best = c
    if not best or ts - best["end_period_ts"] > 3 * 3600:
        return None, None
    bid, ask = _close(best, "yes_bid"), _close(best, "yes_ask")
    return (bid if bid and 0 < bid < 1 else None), (ask if ask and 0 < ask < 1 else None)


def _settled_events(client: KalshiClient, series: str, start: date, end: date) -> list[WeatherEvent]:
    lo = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    hi = int((datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=9)).timestamp())
    raw = client.markets(series_ticker=series, status="settled", min_settled_ts=lo, max_settled_ts=hi)
    try:
        raw += client.historical_markets(series_ticker=series)
    except HttpError:
        pass
    seen, uniq = set(), []
    for m in raw:
        if m["ticker"] not in seen and m.get("result") in ("yes", "no"):
            seen.add(m["ticker"])
            uniq.append(m)
    return [e for e in events_from_markets(uniq, series) if start <= e.day <= end]


def _candles(client: KalshiClient, series: str, ev: WeatherEvent, start_ts: int, end_ts: int) -> dict[str, list]:
    try:
        out = client.event_candlesticks(series, ev.event_ticker, start_ts, end_ts, 60)
    except HttpError:
        out = {}
    for m in ev.markets:
        if m.ticker not in out:
            try:
                out[m.ticker] = client.candlesticks(series, m.ticker, start_ts, end_ts, 60, historical=True)
            except HttpError:
                out[m.ticker] = []
    return out


def _trade(sample: Sample, market, cfg, bankroll: float) -> None:
    quote = replace(market, yes_bid=sample.bid, yes_ask=sample.ask, yes_bid_size=1e6, yes_ask_size=1e6)
    order = evaluate_market(quote, sample.p_model, cfg, bankroll).best
    if order is None:
        return
    won = sample.outcome == 1 if order.side == "yes" else sample.outcome == 0
    sample.traded, sample.price, sample.count, sample.fee = order.side, order.price, order.count, order.fee
    sample.pnl = (order.count if won else 0) - order.cost


def run_backtest(http: Http, settings: Settings, series: list[str], days: int = 45, bankroll: float = 1000.0,
                 calibration_days: int = 365, end: date | None = None,
                 nowcast_hours: tuple[int, ...] = NOWCAST_HOURS) -> BacktestResult:
    client = KalshiClient(PROD_URL, None, http)
    om = OpenMeteo(http, settings.open_meteo_api_key or None)
    iem = IEM(http)
    models = settings.model.models or DEFAULT_MODELS
    cfg = replace(settings.strategy, execution="taker")
    forecaster = Forecaster(om, None, CalibrationStore(settings.calibration_dir), settings.model)
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
        try:
            cal = calibrate_station(http, settings, station, kind, calibration_days, unit, end=start - timedelta(days=1))
            times, series_lead1 = _lead1_series(om, station, models, start, end, unit)
        except HttpError as exc:
            result.errors.append(f"{s}: weather data: {exc}")
            continue
        daily = {m: daily_extremes(times, v, day_windows(station, start, end), kind) for m, v in series_lead1.items()}
        hourly = Hourly(times, series_lead1)
        obs_all = []
        if nowcast_hours:
            try:
                w0, _ = climate_day_window(station, start)
                _, w1 = climate_day_window(station, end)
                obs_all = iem.metars(station.code, w0 - timedelta(hours=2), w1 + timedelta(hours=2))
            except HttpError as exc:
                result.errors.append(f"{s}: METAR archive: {exc}")

        for ev in events:
            ws, we = climate_day_window(station, ev.day)
            day_ahead = ws - timedelta(hours=24 - DAY_AHEAD_HOUR)
            later = ws + timedelta(hours=9)
            candles = _candles(client, s, ev, int(day_ahead.timestamp()) - 3 * 3600, int(we.timestamp()))
            sources = [Source(m, daily[m][ev.day], daily[m][ev.day] + cal.model_bias(m, LEAD_HOURS[1]), cal.weight(m),
                              "model") for m in models if ev.day in daily.get(m, {})]
            if len(sources) < 3:
                continue
            W = sum(x.weight for x in sources)
            mu = sum(x.adjusted * x.weight for x in sources) / W
            spread = math.sqrt(sum(x.weight * (x.adjusted - mu) ** 2 for x in sources) / W)
            sigma = max(cal.sigma_at(LEAD_HOURS[1]), settings.model.disagreement_factor * spread)
            plans = [("dayahead", day_ahead, OutcomeDistribution(
                ScaleMixture(mu, sigma, settings.model.tail_weight, settings.model.tail_scale), kind))]
            for h in nowcast_hours if obs_all else ():
                now = ws + timedelta(hours=h)
                seen = [o for o in obs_all if o.time <= now]
                fc = forecaster.intraday(station, ev.day, kind, unit, now, ws, we, hourly, series_lead1, cal,
                                         sources, mu, [], seen)
                if fc.tradeable:
                    plans.append((f"nowcast{h:02d}", now, fc.dist))
            for mode, when, dist in plans:
                ts = int(when.timestamp())
                for m in ev.markets:
                    bid, ask = _quote_at(candles.get(m.ticker, []), ts)
                    sample = Sample(mode, s, station.code, ev.day.isoformat(), m.ticker, m.bracket.label(ev.unit),
                                    round(dist.prob(m.bracket), 5), bid, ask, int(m.result == "yes"))
                    if mode == "dayahead":
                        lb, la = _quote_at(candles.get(m.ticker, []), int(later.timestamp()))
                        sample.mid_later = None if lb is None or la is None else (lb + la) / 2
                    if bid is not None or ask is not None:
                        _trade(sample, m, cfg, bankroll)
                    result.samples.append(sample)
        log.info("%s: %d events replayed", s, len(events))
    return result


def _lead1_series(om: OpenMeteo, station, models, start: date, end: date, unit: str):
    """Day-old model runs for the whole window, one request per model."""
    times: list[datetime] = []
    out: dict[str, list] = {}
    for model in models:
        try:
            t, series = om.previous_runs(station, [model], start - timedelta(days=1), end + timedelta(days=1),
                                         (1,), unit)
        except HttpError as exc:
            log.warning("previous runs unavailable for %s/%s: %s", station.code, model, exc.status)
            continue
        values = series.get((model, 1))
        if values and any(v is not None for v in values):
            times = times or t
            if len(t) == len(times):
                out[model] = values
    return times, out


def samples_json(result: BacktestResult) -> list[dict]:
    return [asdict(s) for s in result.samples]
