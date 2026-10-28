"""Thin Kalshi Trade API v2 client (market data + portfolio + V2 orders)."""

from __future__ import annotations

import uuid
from typing import Any

from ..http import Http
from .auth import KalshiSigner

PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiAuthError(RuntimeError):
    pass


def fnum(value: Any) -> float | None:
    """Parse Kalshi fixed-point strings ("0.5600", "10.00") and plain numbers."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class KalshiClient:
    def __init__(self, base_url: str = PROD_URL, signer: KalshiSigner | None = None, http: Http | None = None):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.http = http or Http("weather-trader")

    @property
    def is_demo(self) -> bool:
        return "demo" in self.base_url

    # -- plumbing -----------------------------------------------------------------
    def _call(self, method: str, path: str, params: dict | None = None, body: Any = None, auth: bool = False):
        url = self.base_url + path
        if auth and not self.signer:
            raise KalshiAuthError("This call needs KALSHI_API_KEY_ID and a private key.")
        headers = self.signer.headers(method, url) if self.signer else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.http.request(method, url, params=params, json_body=body, headers=headers)

    def _paginate(self, path: str, key: str, params: dict, auth: bool = False, max_pages: int = 50) -> list:
        out: list = []
        cursor = None
        for _ in range(max_pages):
            p = {k: v for k, v in params.items() if v is not None}
            if cursor:
                p["cursor"] = cursor
            data = self._call("GET", path, p, auth=auth)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    # -- public market data -------------------------------------------------------
    def exchange_status(self) -> dict:
        return self._call("GET", "/exchange/status")

    def series_list(self, category: str | None = None) -> list[dict]:
        data = self._call("GET", "/series", {"category": category} if category else None)
        return data.get("series") or []

    def series(self, ticker: str) -> dict:
        return self._call("GET", f"/series/{ticker}").get("series") or {}

    def markets(self, series_ticker: str | None = None, event_ticker: str | None = None,
                status: str | None = None, tickers: str | None = None,
                min_settled_ts: int | None = None, max_settled_ts: int | None = None) -> list[dict]:
        return self._paginate("/markets", "markets", {
            "series_ticker": series_ticker, "event_ticker": event_ticker,
            "status": status, "tickers": tickers, "limit": 1000,
            "min_settled_ts": min_settled_ts, "max_settled_ts": max_settled_ts,
        })

    def historical_markets(self, series_ticker: str | None = None, event_ticker: str | None = None) -> list[dict]:
        return self._paginate("/historical/markets", "markets", {
            "series_ticker": series_ticker, "event_ticker": event_ticker, "limit": 1000,
        })

    def market(self, ticker: str) -> dict:
        return self._call("GET", f"/markets/{ticker}").get("market") or {}

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._call("GET", f"/markets/{ticker}/orderbook", {"depth": depth}).get("orderbook_fp") or {}

    def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                     period: int = 60, historical: bool = False) -> list[dict]:
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period}
        if historical:
            path = f"/historical/markets/{ticker}/candlesticks"
        else:
            path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        return self._call("GET", path, params).get("candlesticks") or []

    def event_candlesticks(self, series_ticker: str, event_ticker: str, start_ts: int, end_ts: int,
                           period: int = 60) -> dict[str, list[dict]]:
        """Candlesticks for every market of an event in one call: {market_ticker: candles}."""
        data = self._call("GET", f"/series/{series_ticker}/events/{event_ticker}/candlesticks",
                          {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period})
        return dict(zip(data.get("market_tickers") or [], data.get("market_candlesticks") or []))

    def historical_cutoff(self) -> dict:
        return self._call("GET", "/historical/cutoff")

    # -- portfolio (authenticated) --------------------------------------------------
    def balance(self) -> float:
        data = self._call("GET", "/portfolio/balance", auth=True)
        dollars = fnum(data.get("balance_dollars"))
        return dollars if dollars is not None else (data.get("balance") or 0) / 100.0

    def positions(self) -> list[dict]:
        return self._paginate("/portfolio/positions", "market_positions",
                              {"limit": 1000, "count_filter": "position"}, auth=True)

    def orders(self, status: str = "resting") -> list[dict]:
        return self._paginate("/portfolio/orders", "orders", {"status": status, "limit": 1000}, auth=True)

    def fills(self, min_ts: int | None = None) -> list[dict]:
        return self._paginate("/portfolio/fills", "fills", {"min_ts": min_ts, "limit": 1000}, auth=True)

    def settlements(self, min_ts: int | None = None) -> list[dict]:
        return self._paginate("/portfolio/settlements", "settlements", {"min_ts": min_ts, "limit": 1000}, auth=True)

    def create_order(
        self,
        ticker: str,
        book_side: str,
        count: float,
        price: float,
        time_in_force: str = "immediate_or_cancel",
        post_only: bool = False,
        expiration_ts: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """V2 order. ``book_side`` is the YES book: 'bid' buys YES, 'ask' sells YES (= buys NO at 1-price)."""
        if book_side not in ("bid", "ask"):
            raise ValueError("book_side must be 'bid' or 'ask'")
        if not 0 < price < 1:
            raise ValueError(f"price {price} outside (0, 1)")
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{price:.2f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if post_only:
            body["post_only"] = True
        if expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        return self._call("POST", "/portfolio/events/orders", body=body, auth=True)

    def cancel_order(self, order_id: str, ticker: str | None = None) -> dict:
        params = {"market_ticker": ticker} if ticker else None
        return self._call("DELETE", f"/portfolio/events/orders/{order_id}", params, auth=True)
