"""Execution: a paper broker that simulates fills, and a Kalshi broker (demo or live)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .kalshi import KalshiClient, fnum
from .ledger import Ledger
from .markets import WeatherMarket
from .strategy import Order, Position


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def start_of_day_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class PaperBroker:
    """Fills taker orders at the quoted ask; resting quotes fill when the market trades through them."""

    mode = "paper"

    def __init__(self, ledger: Ledger, bankroll: float, maker_ttl_minutes: int = 20):
        self.ledger = ledger
        self.bankroll = bankroll
        self.ttl = maker_ttl_minutes

    def balance(self) -> float:
        return self.ledger.paper_cash(self.bankroll)

    def positions(self) -> dict[str, Position]:
        return self.ledger.paper_positions()

    def resting(self) -> list:
        return self.ledger.orders(self.mode, "resting")

    def exposure(self) -> float:
        held = sum(p.cost for p in self.positions().values())
        resting = sum(r["price"] * (r["count"] - r["filled"]) for r in self.resting())
        return held + resting

    def loss_today(self) -> float:
        return max(0.0, -self.ledger.realized_since(self.mode, start_of_day_utc().isoformat(timespec="seconds")))

    def submit(self, order: Order) -> dict:
        if order.style == "take":
            filled = self._fill(order.ticker, order.event_ticker, order.side, order.count, order.price, order.fee)
            status = "filled" if filled == order.count else ("partial" if filled else "rejected")
            self.ledger.log_order(self.mode, order, status, filled)
            return {"status": status, "filled": filled}
        expires = (datetime.now(timezone.utc) + timedelta(minutes=self.ttl)).isoformat(timespec="seconds")
        self.ledger.log_order(self.mode, order, "resting", 0.0, expires=expires)
        return {"status": "resting", "filled": 0}

    def cancel_resting(self) -> int:
        rows = self.resting()
        for r in rows:
            self.ledger.update_order(r["id"], "canceled")
        return len(rows)

    def sync(self, markets: dict[str, WeatherMarket], results: dict[str, str]) -> None:
        now = datetime.now(timezone.utc)
        for r in self.resting():
            m = markets.get(r["ticker"])
            if r["expires"] and datetime.fromisoformat(r["expires"]) < now:
                self.ledger.update_order(r["id"], "expired")
            elif m is not None:
                ask = m.yes_ask if r["side"] == "yes" else m.no_ask
                if ask is not None and ask <= r["price"]:
                    filled = self._fill(r["ticker"], r["event_ticker"], r["side"], int(r["count"]), r["price"], r["fee"])
                    self.ledger.update_order(r["id"], "filled" if filled else "rejected", filled)
        self.settle(results)

    def settle(self, results: dict[str, str]) -> None:
        cash = self.balance()
        for pos in self.positions().values():
            result = results.get(pos.ticker)
            if result not in ("yes", "no"):
                continue
            win = (result == "yes") == (pos.contracts > 0)
            payout = abs(pos.contracts) if win else 0.0
            pnl = payout - pos.cost
            cash += payout
            self.ledger.set_paper_cash(cash, pnl)
            self.ledger.log_pnl(self.mode, pos.ticker, f"settle:{result}", pos.contracts, payout, pnl)
            self.ledger.save_paper_position(Position(pos.ticker, pos.event_ticker, 0, 0))

    def _fill(self, ticker: str, event_ticker: str, side: str, count: int, price: float, fee: float) -> int:
        cash = self.balance()
        unit_fee = fee / count if count else 0.0
        count = min(count, int(cash // (price + unit_fee))) if price + unit_fee > 0 else count
        if count <= 0:
            return 0
        fee_total = unit_fee * count
        pos = self.positions().get(ticker) or Position(ticker, event_ticker, 0.0, 0.0)
        direction = 1 if side == "yes" else -1
        realized = 0.0
        if pos.contracts == 0 or (pos.contracts > 0) == (direction > 0):
            pos.contracts += direction * count
            pos.cost += count * price + fee_total
            cash -= count * price + fee_total
        else:
            # Buying the opposite side closes pairs: each YES+NO pair is worth exactly $1.
            pairs = min(abs(pos.contracts), count)
            avg = pos.cost / abs(pos.contracts)
            proceeds = pairs * (1.0 - price) - unit_fee * pairs
            realized = proceeds - pairs * avg
            cash += proceeds
            pos.cost -= pairs * avg
            pos.contracts += direction * pairs
            rest = count - pairs
            if rest:  # flipped through zero: the remainder opens a new position
                pos.contracts = direction * rest
                pos.cost = rest * (price + unit_fee)
                cash -= pos.cost
        self.ledger.set_paper_cash(cash, realized)
        if realized:
            self.ledger.log_pnl(self.mode, ticker, "close", count, 0.0, realized)
        self.ledger.save_paper_position(pos)
        return count


class KalshiBroker:
    """Sends real orders to Kalshi (the demo exchange when mode == 'demo')."""

    def __init__(self, client: KalshiClient, ledger: Ledger, mode: str, maker_ttl_minutes: int = 20):
        if client.signer is None:
            raise ValueError("Kalshi API keys are required for demo/live trading")
        self.client = client
        self.ledger = ledger
        self.mode = mode
        self.ttl = maker_ttl_minutes

    def balance(self) -> float:
        return self.client.balance()

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self.client.positions():
            contracts = fnum(p.get("position_fp")) or 0.0
            if contracts:
                out[p["ticker"]] = Position(p["ticker"], event_of(p["ticker"]), contracts,
                                            fnum(p.get("market_exposure_dollars")) or 0.0)
        return out

    def resting(self) -> list[dict]:
        return self.client.orders("resting")

    def exposure(self) -> float:
        held = sum(p.cost for p in self.positions().values())
        resting = 0.0
        for o in self.resting():
            remaining = fnum(o.get("remaining_count_fp")) or 0.0
            price = fnum(o.get("yes_price_dollars") if o.get("outcome_side") == "yes" else o.get("no_price_dollars"))
            resting += remaining * (price or 0.0)
        return held + resting

    def loss_today(self) -> float:
        since = int(start_of_day_utc().timestamp())
        pnl = 0.0
        for s in self.client.settlements(min_ts=since):
            revenue = (s.get("revenue") or 0) / 100.0
            cost = (fnum(s.get("yes_total_cost_dollars")) or 0) + (fnum(s.get("no_total_cost_dollars")) or 0)
            pnl += revenue - cost - (fnum(s.get("fee_cost")) or 0)
        return max(0.0, -pnl)

    def submit(self, order: Order) -> dict:
        if order.style == "take":
            resp = self.client.create_order(order.ticker, order.book_side, order.count, order.yes_price,
                                            "immediate_or_cancel")
            expires = None
        else:
            exp = datetime.now(timezone.utc) + timedelta(minutes=self.ttl)
            resp = self.client.create_order(order.ticker, order.book_side, order.count, order.yes_price,
                                            "good_till_canceled", post_only=True, expiration_ts=int(exp.timestamp()))
            expires = exp.isoformat(timespec="seconds")
        filled = fnum(resp.get("fill_count")) or 0.0
        remaining = fnum(resp.get("remaining_count")) or 0.0
        if order.style == "take":
            status = "filled" if filled >= order.count else ("partial" if filled else "unfilled")
        else:
            status = "filled" if filled >= order.count else "resting"
        self.ledger.log_order(self.mode, order, status, filled, resp.get("order_id"), expires,
                              note=f"remaining={remaining:g}")
        return {"status": status, "filled": filled, "order_id": resp.get("order_id")}

    def cancel_resting(self, tickers: set[str] | None = None) -> int:
        n = 0
        for o in self.resting():
            if tickers is None or o.get("ticker") in tickers:
                self.client.cancel_order(o["order_id"], o.get("ticker"))
                n += 1
        return n

    def sync(self, markets: dict[str, WeatherMarket], results: dict[str, str]) -> None:
        """The exchange fills, expires and settles orders itself."""
