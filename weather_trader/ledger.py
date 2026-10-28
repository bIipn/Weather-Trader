"""SQLite journal: every forecast and order, plus the paper-trading account."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .strategy import Order, Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    ts TEXT, event_ticker TEXT, station TEXT, day TEXT, kind TEXT, phase TEXT,
    mu REAL, sigma REAL, lead_hours REAL, probs TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, ts TEXT, mode TEXT, ticker TEXT, event_ticker TEXT, side TEXT,
    style TEXT, price REAL, count REAL, filled REAL, fee REAL, prob REAL, edge REAL,
    status TEXT, expires TEXT, exchange_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS paper_positions (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, contracts REAL, cost REAL);
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL, realized REAL);
CREATE TABLE IF NOT EXISTS pnl_events (
    ts TEXT, mode TEXT, ticker TEXT, kind TEXT, contracts REAL, amount REAL, pnl REAL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- journal ------------------------------------------------------------------------
    def log_forecast(self, event_ticker: str, fc, probs: dict[str, float]) -> None:
        self.db.execute(
            "INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), event_ticker, fc.station.code, fc.day.isoformat(), fc.kind, fc.phase,
             fc.mu, fc.sigma, fc.lead_hours, json.dumps(probs)),
        )
        self.db.commit()

    def log_order(self, mode: str, order: Order, status: str, filled: float = 0.0,
                  exchange_id: str | None = None, expires: str | None = None, note: str = "") -> str:
        oid = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, utcnow(), mode, order.ticker, order.event_ticker, order.side, order.style, order.price,
             order.count, filled, order.fee, order.prob, order.edge, status, expires, exchange_id, note),
        )
        self.db.commit()
        return oid

    def update_order(self, oid: str, status: str, filled: float | None = None) -> None:
        if filled is None:
            self.db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        else:
            self.db.execute("UPDATE orders SET status=?, filled=? WHERE id=?", (status, filled, oid))
        self.db.commit()

    def orders(self, mode: str, status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if status:
            q = "SELECT * FROM orders WHERE mode=? AND status=? ORDER BY ts DESC LIMIT ?"
            return list(self.db.execute(q, (mode, status, limit)))
        return list(self.db.execute("SELECT * FROM orders WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit)))

    def log_pnl(self, mode: str, ticker: str, kind: str, contracts: float, amount: float, pnl: float) -> None:
        self.db.execute("INSERT INTO pnl_events VALUES (?,?,?,?,?,?,?)",
                        (utcnow(), mode, ticker, kind, contracts, amount, pnl))
        self.db.commit()

    def realized_since(self, mode: str, since_iso: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM pnl_events WHERE mode=? AND ts>=?",
                              (mode, since_iso)).fetchone()
        return float(row[0])

    # -- paper account -------------------------------------------------------------------
    def paper_cash(self, bankroll: float) -> float:
        row = self.db.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
        if row is None:
            self.db.execute("INSERT INTO paper_account VALUES (1, ?, 0)", (bankroll,))
            self.db.commit()
            return bankroll
        return float(row[0])

    def set_paper_cash(self, cash: float, realized_delta: float = 0.0) -> None:
        self.db.execute("UPDATE paper_account SET cash=?, realized=realized+? WHERE id=1", (cash, realized_delta))
        self.db.commit()

    def paper_realized(self) -> float:
        row = self.db.execute("SELECT realized FROM paper_account WHERE id=1").fetchone()
        return float(row[0]) if row else 0.0

    def paper_positions(self) -> dict[str, Position]:
        rows = self.db.execute("SELECT * FROM paper_positions")
        return {r["ticker"]: Position(r["ticker"], r["event_ticker"], r["contracts"], r["cost"]) for r in rows}

    def save_paper_position(self, pos: Position) -> None:
        if abs(pos.contracts) < 1e-9:
            self.db.execute("DELETE FROM paper_positions WHERE ticker=?", (pos.ticker,))
        else:
            self.db.execute("INSERT OR REPLACE INTO paper_positions VALUES (?,?,?,?)",
                            (pos.ticker, pos.event_ticker, pos.contracts, pos.cost))
        self.db.commit()
