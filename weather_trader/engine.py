"""One trading cycle: discover markets → forecast → price → risk → execute → journal."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .broker import KalshiBroker, PaperBroker
from .config import Settings
from .http import Http
from .kalshi import DEMO_URL, PROD_URL, KalshiClient, KalshiSigner
from .ledger import Ledger
from .markets import MarketDiscovery, WeatherEvent
from .model import CalibrationStore, Forecaster
from .sniper import is_dead, snipe_orders
from .stations import climate_today
from .strategy import EventView, Order, evaluate_market, select_orders
from .weather import NWS, OpenMeteo, TomorrowIO

log = logging.getLogger("wxbot")


@dataclass
class CycleResult:
    views: list[EventView]
    orders: list[Order] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    balance: float = 0.0
    exposure: float = 0.0
    loss_today: float = 0.0
    canceled: int = 0
    errors: list[str] = field(default_factory=list)


def build_signer(s: Settings) -> KalshiSigner | None:
    if not s.has_kalshi_keys:
        return None
    if s.kalshi_private_key:
        return KalshiSigner(s.kalshi_key_id, s.kalshi_private_key)
    return KalshiSigner.from_file(s.kalshi_key_id, s.kalshi_private_key_path)


class Engine:
    def __init__(self, settings: Settings, http: Http | None = None, market_client: KalshiClient | None = None,
                 broker=None, forecaster: Forecaster | None = None, ledger: Ledger | None = None):
        self.s = settings
        self.http = http or Http(settings.user_agent)
        signer = build_signer(settings) if settings.mode in ("demo", "live") else None
        # Market data always comes from production (demo prices are not the real market).
        self.markets = market_client or KalshiClient(PROD_URL, None, self.http)
        self.ledger = ledger or Ledger(Path(settings.data_dir) / f"wxbot_{settings.mode}.sqlite")
        if broker is not None:
            self.broker = broker
        elif settings.mode == "paper":
            self.broker = PaperBroker(self.ledger, settings.paper_bankroll, settings.strategy.maker_ttl_minutes)
        else:
            url = DEMO_URL if settings.mode == "demo" else PROD_URL
            self.broker = KalshiBroker(KalshiClient(url, signer, self.http), self.ledger, settings.mode,
                                       settings.strategy.maker_ttl_minutes)
        tomorrow = TomorrowIO(self.http, settings.tomorrow_api_key) if settings.tomorrow_api_key else None
        self.forecaster = forecaster or Forecaster(
            OpenMeteo(self.http, settings.open_meteo_api_key or None), NWS(self.http),
            CalibrationStore(settings.calibration_dir), settings.model, tomorrow)
        self.discovery = MarketDiscovery(self.markets, settings.series or None)

    # -- scanning ------------------------------------------------------------------------
    def _wanted(self, ev: WeatherEvent, now: datetime, days: list[int] | None) -> bool:
        if self.s.stations and ev.station.code not in self.s.stations:
            return False
        offset = (ev.day - climate_today(ev.station, now)).days
        if offset not in (days if days is not None else self.s.strategy.trade_days):
            return False
        if ev.close_time and ev.close_time - now < timedelta(minutes=self.s.strategy.min_minutes_to_close):
            return False
        return True

    def scan(self, now: datetime | None = None, days: list[int] | None = None,
             bankroll: float | None = None, events: list[WeatherEvent] | None = None) -> list[EventView]:
        now = now or datetime.now(timezone.utc)
        bankroll = bankroll if bankroll is not None else self.s.paper_bankroll
        events = events if events is not None else self.discovery.open_events(now)
        views = []
        for ev in events:
            if not self._wanted(ev, now, days):
                continue
            try:
                fc = self.forecaster.forecast(ev.station, ev.day, ev.kind, ev.unit, now=now)
            except Exception as exc:  # one bad station must not stop the others
                log.warning("forecast failed for %s: %s", ev.event_ticker, exc)
                views.append(EventView(ev, None, error=f"{type(exc).__name__}: {exc}"[:200]))
                continue
            view = EventView(ev, fc)
            cfg = self.s.strategy
            extreme = fc.nowcast.extreme if fc.nowcast and ev.station_source in ("rules", "fallback") else None
            snipes = {o.ticker: o for o in snipe_orders(ev, extreme, cfg, cfg.sniper_min_bid,
                                                        cfg.sniper_max_contracts)} if extreme is not None else {}
            for m in ev.markets:
                p_model = fc.dist.prob(m.bracket)
                p_trade = p_model if m.mid is None else cfg.model_weight * p_model + (1 - cfg.model_weight) * m.mid
                row = evaluate_market(m, p_trade, cfg, bankroll)
                row.prob = p_model
                if extreme is not None and is_dead(m, extreme, ev.kind):
                    row.best = snipes.get(m.ticker) if cfg.sniper else None
                    row.skip = "ruled out by observations"
                elif not cfg.forecast_trading:
                    row.best = None
                elif not fc.tradeable:
                    row.best, row.skip = None, "; ".join(fc.notes) or "not tradeable"
                elif cfg.require_calibration and not fc.calibrated:
                    row.best, row.skip = None, "station not calibrated"
                elif ev.station_source not in ("rules", "fallback"):
                    row.best, row.skip = None, "station inferred from city name"
                view.rows.append(row)
            views.append(view)
        return views

    # -- trading -------------------------------------------------------------------------
    def cycle(self, now: datetime | None = None, trade: bool = True) -> CycleResult:
        now = now or datetime.now(timezone.utc)
        res = CycleResult(views=[])
        events = self.discovery.open_events(now)
        try:
            res.balance = self.broker.balance()
            res.exposure = self.broker.exposure()
            held = self.broker.positions()
            res.loss_today = self.broker.loss_today()
        except Exception as exc:
            res.errors.append(f"broker unavailable: {exc}")
            return res

        open_markets = {m.ticker: m for ev in events for m in ev.markets}
        results = self._results([t for t in held if t not in open_markets])
        self.broker.sync(open_markets, results)

        bankroll = res.balance + res.exposure
        res.views = self.scan(now, bankroll=bankroll, events=events)
        for v in res.views:
            if v.forecast:
                self.ledger.log_forecast(v.event.event_ticker, v.forecast,
                                         {r.market.ticker: round(r.prob, 4) for r in v.rows})
        if not trade:
            return res

        res.canceled = self.broker.cancel_resting()
        res.exposure = self.broker.exposure()
        res.orders = select_orders(res.views, self.broker.positions(), self.s.strategy, res.exposure, res.loss_today)
        # Snipes are settled already (the bracket cannot win), so they skip the per-event
        # loss checks but still respect the overall exposure cap.
        budget = self.s.strategy.max_total_exposure - res.exposure - sum(o.cost for o in res.orders)
        for v in res.views:
            for row in v.rows:
                o = row.best
                if o is not None and o.note.startswith("dead:") and o.cost <= budget:
                    res.orders.append(o)
                    budget -= o.cost
        for order in res.orders:
            try:
                res.fills.append({"order": order, **self.broker.submit(order)})
            except Exception as exc:
                res.errors.append(f"{order.ticker}: {exc}")
                log.error("order failed %s: %s", order.ticker, exc)
        return res

    def _results(self, tickers: list[str]) -> dict[str, str]:
        if not tickers:
            return {}
        out = {}
        for i in range(0, len(tickers), 50):
            for m in self.markets.markets(tickers=",".join(tickers[i:i + 50])):
                if m.get("result") in ("yes", "no"):
                    out[m["ticker"]] = m["result"]
        return out
