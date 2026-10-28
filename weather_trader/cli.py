"""wxbot — command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Settings, load_settings
from .stations import STATIONS
from .ui import DIM, GREEN, MUTED, RED, SKY, money, render_orders, render_positions, render_scan

console = Console()
MAIN_SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHLAX", "KXHIGHDEN", "KXHIGHPHIL"]


def _engine(settings: Settings):
    from .engine import Engine
    return Engine(settings)


def cmd_scan(args, s: Settings) -> int:
    if args.station:
        s.stations = [x.upper() for x in args.station]
    s.mode = "paper"
    eng = _engine(s)
    views = eng.scan(days=args.days)
    if args.json:
        print(json.dumps([_view_json(v) for v in views], indent=2, default=str))
    else:
        render_scan(console, views, "scan", s.paper_bankroll)
    return 0


def _view_json(v) -> dict:
    fc = v.forecast
    return {
        "event": v.event.event_ticker, "station": v.event.station.code, "day": v.event.day.isoformat(),
        "kind": v.event.kind, "error": v.error or None,
        "mu": None if not fc else round(fc.mu, 2), "sigma": None if not fc else round(fc.sigma, 2),
        "lead_hours": None if not fc else round(fc.lead_hours, 1), "phase": None if not fc else fc.phase,
        "tradeable": bool(fc and fc.tradeable), "notes": [] if not fc else fc.notes,
        "markets": [{
            "ticker": r.market.ticker, "bracket": r.market.bracket.label(v.event.unit),
            "bid": r.market.yes_bid, "ask": r.market.yes_ask, "model": round(r.prob, 4),
            "signal": None if not r.best else {"side": r.best.side, "price": r.best.price, "count": r.best.count,
                                               "style": r.best.style, "edge": round(r.best.edge, 4)},
            "skip": r.skip or None,
        } for r in v.rows],
    }


def cmd_run(args, s: Settings) -> int:
    if args.mode:
        s.mode = args.mode
    if args.interval:
        s.interval_seconds = args.interval
    if s.mode == "live" and not (args.confirm_live or os.environ.get("WXBOT_CONFIRM_LIVE") == "yes"):
        console.print(Text("Live mode trades real money. Re-run with --confirm-live (or WXBOT_CONFIRM_LIVE=yes).",
                           style=f"bold {RED}"))
        return 2
    if s.mode in ("demo", "live") and not s.has_kalshi_keys:
        console.print(Text("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (see README) first.", style=RED))
        return 2
    eng = _engine(s)
    while True:
        started = time.monotonic()
        try:
            res = eng.cycle()
            render_scan(console, res.views, s.mode, res.balance, res.exposure, res.loss_today)
            render_orders(console, res.fills)
            for err in res.errors:
                console.print(Text(f"! {err}", style=RED))
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # keep the loop alive through transient outages
            logging.getLogger("wxbot").exception("cycle failed")
            console.print(Text(f"cycle failed: {exc}", style=RED))
        if args.once:
            return 0
        wait = max(s.interval_seconds - (time.monotonic() - started), 5)
        console.print(Text(f"next cycle in {wait:.0f}s · Ctrl-C to stop", style=DIM))
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            return 0


def cmd_status(args, s: Settings) -> int:
    if args.mode:
        s.mode = args.mode
    eng = _engine(s)
    marks = {}
    try:
        for ev in eng.discovery.open_events():
            for m in ev.markets:
                if m.mid is not None:
                    marks[m.ticker] = m.mid
    except Exception as exc:
        console.print(Text(f"(could not load market prices: {exc})", style=MUTED))
    console.print(Text.assemble(("Mode ", MUTED), (s.mode.upper(), f"bold {SKY}"), ("   balance ", MUTED),
                                (money(eng.broker.balance()), "bold"), ("   at risk ", MUTED),
                                (money(eng.broker.exposure()), "bold")))
    render_positions(console, eng.broker.positions(), marks)
    rows = eng.ledger.orders(s.mode, limit=15)
    if rows:
        t = Table(title=Text("Recent orders", style="bold"), title_justify="left", header_style=MUTED, box=None)
        for col in ("Time (UTC)", "Market", "Side", "Price", "Qty", "Filled", "Style", "Status"):
            t.add_column(col)
        for r in rows:
            t.add_row(r["ts"][5:16].replace("T", " "), r["ticker"], r["side"].upper(), f"{r['price'] * 100:.0f}¢",
                      f"{r['count']:g}", f"{r['filled']:g}", r["style"], r["status"])
        console.print(t)
    return 0


def cmd_calibrate(args, s: Settings) -> int:
    from .calibrate import calibrate_station
    from .http import Http
    from .model.calibration import CalibrationStore

    http = Http(s.user_agent)
    codes = [c.upper() for c in (args.station or [])]
    if not codes:
        eng = _engine(s)
        codes = sorted({ev.station.code for ev in eng.discovery.open_events()})
    kinds = ["high", "low"] if args.kind == "both" else [args.kind]
    store = CalibrationStore(s.calibration_dir)
    t = Table(title=Text("Calibration", style="bold"), title_justify="left", header_style=MUTED)
    for col in ("Station", "Kind", "Days", "σ same-day", "σ day-ahead", "σ 2-day", "Best model", "Its bias"):
        t.add_column(col, justify="left" if col in ("Station", "Kind", "Best model") else "right")
    for code in codes:
        st = STATIONS.get(code)
        if not st:
            console.print(Text(f"unknown station {code}", style=RED))
            continue
        for kind in kinds:
            with console.status(f"calibrating {code} {kind}…"):
                try:
                    cal = calibrate_station(http, s, st, kind, args.days)
                except Exception as exc:
                    console.print(Text(f"{code} {kind}: {exc}", style=RED))
                    continue
            if not cal.fitted:
                console.print(Text(f"{code} {kind}: not enough data", style=RED))
                continue
            store.save(cal)
            best = min(cal.rmse, key=lambda m: cal.rmse[m].get(1, 99), default="—")
            t.add_row(code, kind, str(cal.n_days), *(f"{cal.sigma.get(k, float('nan')):.2f}" for k in (0, 1, 2)),
                      best, f"{cal.bias.get(best, {}).get(1, 0):+.2f}")
    console.print(t)
    return 0


def cmd_backtest(args, s: Settings) -> int:
    from .backtest import run_backtest, samples_json
    from .http import Http

    if args.strategy == "sniper":
        return _backtest_sniper(args, s)
    with Console(stderr=True).status("replaying markets…"):
        res = run_backtest(Http(s.user_agent), s, args.series, args.days,
                           nowcast_hours=() if args.no_nowcast else (11, 14, 17, 20))
    summary = res.summary()
    if args.json:
        print(json.dumps({"summary": summary, "errors": res.errors, "samples": samples_json(res)}, default=str))
        return 0
    t = Table(title=Text(f"Backtest  {summary['window']}", style="bold"), title_justify="left", header_style=MUTED)
    for col in ("Decision", "Brackets", "Brier model", "Brier market", "Trades", "Win %", "Staked", "P&L", "ROI"):
        t.add_column(col, justify="left" if col == "Decision" else "right")
    for mode, st in summary.items():
        if mode == "window":
            continue
        label = "day before, 10 PM" if mode == "dayahead" else f"same day, {int(mode[-2:])}:00"
        better = st["brier_model"] is not None and st["brier_market"] is not None and st["brier_model"] < st["brier_market"]
        t.add_row(label, str(st["quoted"]), Text(f"{st['brier_model']}", style=GREEN if better else RED),
                  f"{st['brier_market']}", str(st["trades"]),
                  f"{st['win_rate']:.0%}" if st["win_rate"] is not None else "—", money(st["cost"]),
                  Text(money(st["pnl"]), style=GREEN if st["pnl"] >= 0 else RED),
                  f"{st['roi']:+.1%}" if st["roi"] is not None else "—")
    console.print(t)
    console.print(Text("Brier score: lower is better. Model in green when it beat the market's own prices.",
                       style=DIM))
    for err in res.errors:
        console.print(Text(f"! {err}", style=MUTED))
    return 0


def _backtest_sniper(args, s: Settings) -> int:
    from .http import Http
    from .sniper import run_sniper_backtest, snipes_json, summarize_snipes

    with Console(stderr=True).status("replaying observations…"):
        samples, errors = run_sniper_backtest(Http(s.user_agent), s, args.series, args.days)
    summary = summarize_snipes(samples)
    if args.json:
        print(json.dumps({"summary": summary, "errors": errors, "samples": snipes_json(samples)}, default=str))
        return 0
    t = Table(title=Text("Dead-bracket sniping", style="bold"), title_justify="left", header_style=MUTED)
    for col in ("Reaction time", "Stale bids found", "Profit per contract (sum)"):
        t.add_column(col, justify="left" if col == "Reaction time" else "right")
    for key, val in summary.items():
        if key.startswith("after_"):
            t.add_row(key.replace("after_", "").replace("min", " min after the report"), str(val["opportunities"]),
                      money(val["profit_per_contract_sum"]))
    console.print(t)
    console.print(Text(f"{summary['dead_brackets']} brackets killed by observations; "
                       f"{summary['violations']} of them settled YES anyway.", style=MUTED))
    for err in errors:
        console.print(Text(f"! {err}", style=MUTED))
    return 0


def cmd_markets(args, s: Settings) -> int:
    eng = _engine(s)
    events = eng.discovery.open_events()
    t = Table(title=Text("Open daily temperature markets", style="bold"), title_justify="left", header_style=MUTED)
    for col in ("Series", "Event", "Station", "Matched by", "Kind", "Day", "Brackets", "Closes (UTC)"):
        t.add_column(col)
    for ev in events:
        t.add_row(ev.series_ticker, ev.event_ticker, f"{ev.station.code} {ev.station.name}", ev.station_source,
                  ev.kind, ev.day.isoformat(), str(len(ev.markets)),
                  ev.close_time.strftime("%m-%d %H:%M") if ev.close_time else "—")
    console.print(t)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxbot", description="Weather Trader — Kalshi daily temperature bot")
    parser.add_argument("--config", help="path to config.toml (default ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"wxbot {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="model vs market for every open temperature market (never trades)")
    p.add_argument("--days", type=int, nargs="*", help="climate days: 0 = today, 1 = tomorrow (default 0 1)")
    p.add_argument("--station", nargs="*", help="limit to stations, e.g. KNYC KMDW")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("run", help="trade: paper (default), demo or live")
    p.add_argument("--mode", choices=["paper", "demo", "live"])
    p.add_argument("--once", action="store_true", help="run a single cycle and exit (for cron)")
    p.add_argument("--interval", type=int, help="seconds between cycles (default 300)")
    p.add_argument("--confirm-live", action="store_true", help="required to trade real money")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="balance, positions and recent orders")
    p.add_argument("--mode", choices=["paper", "demo", "live"])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("calibrate", help="fit per-station calibration from a year of archived forecasts")
    p.add_argument("--station", nargs="*", help="stations (default: all with open markets)")
    p.add_argument("--kind", choices=["high", "low", "both"], default="high")
    p.add_argument("--days", type=int, default=365)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("backtest", help="replay the strategy against historical Kalshi prices")
    p.add_argument("--series", nargs="*", default=MAIN_SERIES)
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--strategy", choices=["forecast", "sniper"], default="forecast",
                   help="forecast = model vs market; sniper = sell brackets observations have ruled out")
    p.add_argument("--no-nowcast", action="store_true", help="skip the same-day (observation) replay")
    p.add_argument("--json", action="store_true", help="summary plus every replayed bracket")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("markets", help="list open temperature markets and their settlement stations")
    p.set_defaults(func=cmd_markets)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings(args.config)
    except ValueError as exc:
        console.print(Text(str(exc), style=RED))
        return 2
    return args.func(args, settings)


if __name__ == "__main__":
    sys.exit(main())
