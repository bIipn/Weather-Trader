"""From probabilities to orders: fee-aware edge, fractional Kelly sizing and risk caps."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import StrategyConfig
from .markets import WeatherEvent, WeatherMarket
from .model.forecast import Forecast


def _round_up_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100.0


def taker_fee(price: float, count: float = 1, multiplier: float = 1.0) -> float:
    """Kalshi trading fee for an order that takes liquidity: roundup(0.07·M·C·P·(1-P))."""
    return _round_up_cents(0.07 * multiplier * count * price * (1 - price))


def maker_fee(price: float, count: float = 1, multiplier: float = 0.0) -> float:
    """Resting-order fee: roundup(0.0175·M·C·P·(1-P)); M is 0 for weather series."""
    return _round_up_cents(0.0175 * multiplier * count * price * (1 - price)) if multiplier else 0.0


@dataclass
class Order:
    ticker: str
    event_ticker: str
    side: str  # contract bought: "yes" | "no"
    price: float  # price of that contract in dollars
    count: int
    style: str  # "take" | "make"
    prob: float  # model probability that the bought contract pays out
    edge: float  # expected profit per contract after fees
    fee: float  # total fee for the order (estimate)
    note: str = ""

    @property
    def cost(self) -> float:
        return self.count * self.price + self.fee

    @property
    def book_side(self) -> str:
        """Kalshi V2 books everything on the YES side: buy YES = bid, buy NO = ask (sell YES)."""
        return "bid" if self.side == "yes" else "ask"

    @property
    def yes_price(self) -> float:
        return self.price if self.side == "yes" else round(1 - self.price, 4)

    @property
    def expected_profit(self) -> float:
        return self.count * self.prob - self.cost


@dataclass
class Position:
    ticker: str
    event_ticker: str
    contracts: float  # +YES / -NO
    cost: float  # dollars paid for the open contracts

    @property
    def side(self) -> str:
        return "yes" if self.contracts >= 0 else "no"


@dataclass
class MarketView:
    market: WeatherMarket
    prob: float  # model P(YES)
    best: Order | None = None
    skip: str = ""


@dataclass
class EventView:
    event: WeatherEvent
    forecast: Forecast | None
    rows: list[MarketView] = field(default_factory=list)
    error: str = ""


def _kelly_count(prob: float, unit_cost: float, bankroll: float, cfg: StrategyConfig) -> int:
    if unit_cost <= 0 or unit_cost >= 1 or prob <= unit_cost:
        return 0
    f = (prob - unit_cost) / (1 - unit_cost)
    stake = min(cfg.kelly_fraction * f * bankroll, cfg.max_stake_per_market)
    return int(stake // unit_cost)


def _round_down_cent(x: float) -> float:
    return math.floor(round(x * 100, 6)) / 100.0


def evaluate_market(m: WeatherMarket, p_yes: float, cfg: StrategyConfig, bankroll: float) -> MarketView:
    """Best single order (if any) for one bracket given the model probability."""
    view = MarketView(m, p_yes)
    if m.mid is not None and abs(p_yes - m.mid) > cfg.max_model_market_gap:
        view.skip = f"model/market gap {abs(p_yes - m.mid):.0%}"
        return view

    takes: list[Order] = []
    makes: list[Order] = []
    for side, prob, ask, ask_size, bid in (
        ("yes", p_yes, m.yes_ask, m.yes_ask_size, m.yes_bid),
        ("no", 1 - p_yes, m.no_ask, m.yes_bid_size, m.no_bid),
    ):
        # Take liquidity at the ask when the edge clears fees by a margin.
        if cfg.execution in ("taker", "hybrid") and ask is not None and cfg.min_price <= ask <= cfg.max_price:
            unit_fee = 0.07 * cfg.fee_multiplier * ask * (1 - ask)
            if prob - ask - unit_fee >= cfg.min_edge:
                n = min(_kelly_count(prob, ask + unit_fee, bankroll, cfg), int(ask_size))
                if n >= 1:
                    fee = taker_fee(ask, n, cfg.fee_multiplier)
                    edge = prob - ask - fee / n
                    if edge >= cfg.min_edge:
                        takes.append(Order(m.ticker, m.event_ticker, side, ask, n, "take", prob, edge, fee))
        # Otherwise rest a bid that becomes the new best bid, below fair value minus a margin.
        # A bid behind the queue only fills when someone knows more than we do, so skip it.
        if cfg.execution in ("maker", "hybrid"):
            target = _round_down_cent(prob - cfg.min_edge_maker)
            price = min(target, round((bid or 0.0) + 0.01, 2))
            if ask is not None and price >= ask:
                price = round(ask - 0.01, 2)
            if price > (bid or 0.0) and cfg.min_price <= price <= cfg.max_price:
                n = _kelly_count(prob, price, bankroll, cfg)
                if n >= 1:
                    fee = maker_fee(price, n, cfg.maker_fee_multiplier)
                    edge = prob - price - fee / n
                    if edge >= cfg.min_edge_maker:
                        makes.append(Order(m.ticker, m.event_ticker, side, price, n, "make", prob, edge, fee))
    # A certain fill beats a hoped-for one: take whenever taking clears the bar.
    pool = takes or makes
    if pool:
        view.best = max(pool, key=lambda o: o.expected_profit)
    return view


def outcome_grid(event: WeatherEvent) -> list[float]:
    bounds = [b for m in event.markets for b in m.bracket.bounds() if b is not None]
    if not bounds:
        return []
    lo, hi = int(min(bounds)) - 1, int(max(bounds)) + 1
    return [float(y) for y in range(lo, hi + 1)]


def event_pnl(event: WeatherEvent, positions: list[Position], orders: list[Order], y: float) -> float:
    by_ticker = {m.ticker: m for m in event.markets}
    pnl = 0.0
    for p in positions:
        m = by_ticker.get(p.ticker)
        if m is None:
            continue
        yes = m.bracket.contains(y)
        payout = abs(p.contracts) * (1.0 if (yes if p.contracts > 0 else not yes) else 0.0)
        pnl += payout - p.cost
    for o in orders:
        m = by_ticker[o.ticker]
        yes = m.bracket.contains(y)
        win = yes if o.side == "yes" else not yes
        pnl += (o.count if win else 0) - o.cost
    return pnl


def worst_case_loss(event: WeatherEvent, positions: list[Position], orders: list[Order]) -> float:
    grid = outcome_grid(event)
    if not grid:
        return sum(o.cost for o in orders) + sum(p.cost for p in positions)
    return max(0.0, -min(event_pnl(event, positions, orders, y) for y in grid))


def select_orders(
    views: list[EventView],
    positions: dict[str, Position],
    cfg: StrategyConfig,
    exposure: float,
    loss_today: float,
) -> list[Order]:
    """Apply portfolio limits and return the orders to send this cycle."""
    if loss_today >= cfg.daily_loss_limit:
        return []
    chosen: list[Order] = []
    budget = max(cfg.max_total_exposure - exposure, 0.0)
    for ev in views:
        if not ev.forecast or not ev.forecast.tradeable:
            continue
        ev_positions = [p for p in positions.values() if p.event_ticker == ev.event.event_ticker]
        picked: list[Order] = []
        forecast_rows = (r for r in ev.rows if r.best and not r.best.note.startswith("dead:"))
        for row in sorted(forecast_rows, key=lambda r: -r.best.edge):
            order = row.best
            held = positions.get(order.ticker)
            if held and held.side == order.side:
                room = cfg.max_stake_per_market - held.cost
                order = resize(order, min(order.count, int(max(room, 0.0) // order.price)), cfg)
            while order.count >= 1:
                if order.cost <= budget and worst_case_loss(ev.event, ev_positions, picked + [order]) <= cfg.max_event_loss:
                    break
                order = resize(order, order.count - max(1, order.count // 4), cfg)
            if order.count >= 1:
                picked.append(order)
                budget -= order.cost
        chosen.extend(picked)
    return chosen


def resize(order: Order, n: int, cfg: StrategyConfig) -> Order:
    n = max(int(n), 0)
    if order.style == "take":
        fee = taker_fee(order.price, n, cfg.fee_multiplier) if n else 0.0
    else:
        fee = maker_fee(order.price, n, cfg.maker_fee_multiplier) if n else 0.0
    edge = order.prob - order.price - (fee / n if n else 0.0)
    return Order(order.ticker, order.event_ticker, order.side, order.price, n, order.style, order.prob,
                 edge, fee, order.note)
