"""Terminal dashboard built on rich."""

from __future__ import annotations

from datetime import datetime, timezone

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .strategy import EventView, Order, Position

SKY = "#7dd3fc"
GREEN = "#4ade80"
RED = "#f87171"
ROSE = "#f472b6"
AMBER = "#fbbf24"
MUTED = "#94a3b8"
DIM = "#64748b"
TEXT = "#e2e8f0"
_BLOCKS = " ▏▎▍▌▋▊▉"

MODE_STYLE = {"paper": f"bold black on {SKY}", "demo": f"bold black on {AMBER}", "live": f"bold white on {RED}"}


def cents(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100):d}¢"


def pct(p: float | None) -> str:
    if p is None:
        return "—"
    if p < 0.005:
        return "<1%"
    if p > 0.995:
        return ">99%"
    return f"{p * 100:.0f}%"


def money(x: float) -> str:
    sign = "−" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def bar(p: float, width: int = 12, color: str = SKY) -> Text:
    full = max(0.0, min(1.0, p)) * width
    n = int(full)
    s = "█" * n
    if n < width:
        s += _BLOCKS[int((full - n) * 8)]
    return Text(s.ljust(width), style=color)


def header(mode: str, balance: float, exposure: float, loss_today: float, n_events: int,
           n_signals: int, now: datetime | None = None) -> Panel:
    now = now or datetime.now(timezone.utc)
    title = Text.assemble(("⛅  WEATHER TRADER", f"bold {SKY}"), ("   Kalshi daily temperature markets", MUTED))
    badge = Text(f" {mode.upper()} ", style=MODE_STYLE.get(mode, "bold"))
    sep = ("  ·  ", DIM)
    stats = Text.assemble(
        (f"{now:%a %b %d %H:%M} UTC", MUTED), sep,
        ("balance ", MUTED), (money(balance), "bold"), sep,
        ("at risk ", MUTED), (money(exposure), "bold"), sep,
        ("lost today ", MUTED), (money(loss_today), f"bold {RED}" if loss_today > 0 else "bold"), sep,
        (f"{n_events} events  ", MUTED), (f"{n_signals} signals", f"bold {GREEN}" if n_signals else MUTED),
    )
    top = Table.grid(expand=True)
    top.add_column()
    top.add_column(justify="right")
    top.add_row(title, badge)
    return Panel(Group(top, stats), border_style=DIM, box=box.ROUNDED, padding=(0, 1))


def _signal(order: Order | None, skip: str) -> Text:
    if order is None:
        return Text(skip[:40], style=DIM) if skip else Text("")
    color = GREEN if order.side == "yes" else ROSE
    verb = "BUY YES" if order.side == "yes" else "BUY NO "
    how = "take" if order.style == "take" else "rest"
    return Text.assemble((f"{verb} ", f"bold {color}"), (f"{cents(order.price):>4} ×{order.count:<4}", "bold"),
                         (f" {how}", DIM))


def event_panel(view: EventView) -> Panel:
    ev = view.event
    title = Text.assemble(
        (f" {ev.station.name}", f"bold {TEXT}"), ("  ·  ", DIM), (ev.station.code, SKY), ("  ·  ", DIM),
        ("High" if ev.kind == "high" else "Low", f"bold {TEXT}"), ("  ·  ", DIM),
        (f"{ev.day:%a %b %d} ", f"bold {TEXT}"),
    )
    return Panel(event_table(view), title=title, title_align="left", border_style=DIM, box=box.ROUNDED,
                 padding=(0, 1))


def event_table(view: EventView) -> Table:
    ev, fc = view.event, view.forecast
    unit = "°F" if ev.unit == "F" else "°C"
    caption = Text()
    if fc is None:
        caption.append(f"forecast failed: {view.error}", style=RED)
    else:
        if fc.mu == fc.mu:
            caption.append("model ", style=MUTED)
            caption.append(f"{fc.mu:.1f}{unit} ± {fc.sigma:.1f}", style=f"bold {SKY}")
        caption.append(f"   lead {fc.lead_hours:.0f}h   {fc.n_sources} sources", style=MUTED)
        implied = ev.implied_mean()
        if implied is not None:
            caption.append("   market ≈ ", style=MUTED)
            caption.append(f"{implied:.1f}{unit}", style="bold")
        if not fc.calibrated:
            caption.append("   uncalibrated", style=AMBER)
        nc = fc.nowcast
        if nc and nc.extreme is not None:
            word = "max" if ev.kind == "high" else "min"
            caption.append(f"\nobserved {word} so far ", style=MUTED)
            caption.append(f"{nc.extreme:g}{unit}", style=f"bold {AMBER}")
            caption.append(" confirmed" if nc.exact else " (unconfirmed — could still tick 1°)", style=MUTED)
            if nc.last_time and nc.last_temp is not None:
                caption.append(f"   now {nc.last_temp:.0f}{unit} at {nc.last_time:%H:%M} UTC", style=MUTED)
        if fc.notes:
            caption.append("\n" + " · ".join(fc.notes), style=AMBER)

    t = Table(caption=caption, caption_justify="left", box=box.SIMPLE_HEAD, header_style=MUTED, expand=True,
              pad_edge=False)
    t.add_column("Bracket", style="bold", no_wrap=True)
    t.add_column("Bid", justify="right")
    t.add_column("Ask", justify="right")
    t.add_column("Market", justify="right")
    t.add_column("Model", justify="right", style=f"bold {SKY}")
    t.add_column("", no_wrap=True)
    t.add_column("Edge", justify="right")
    t.add_column("Signal", no_wrap=True)
    for row in view.rows:
        m = row.market
        diff = None if m.mid is None else row.prob - m.mid
        edge = Text("—", style=DIM) if diff is None else Text(
            f"{diff * 100:+.0f}", style=GREEN if diff > 0.02 else (RED if diff < -0.02 else MUTED))
        t.add_row(m.bracket.label(ev.unit), cents(m.yes_bid), cents(m.yes_ask), pct(m.mid), pct(row.prob),
                  bar(row.prob), edge, _signal(row.best, row.skip))
    return t


def render_scan(console: Console, views: list[EventView], mode: str, balance: float, exposure: float = 0.0,
                loss_today: float = 0.0) -> None:
    signals = sum(1 for v in views for r in v.rows if r.best)
    console.print(header(mode, balance, exposure, loss_today, len(views), signals))
    if not views:
        console.print(Text("No open daily temperature markets matched your filters.", style=MUTED))
    for v in views:
        console.print(event_panel(v))
    console.print(Text("Edge = model − market midpoint, in percentage points.  Signals already net out Kalshi fees.",
                       style=DIM))


def render_orders(console: Console, fills: list[dict]) -> None:
    if not fills:
        console.print(Text("No orders this cycle.", style=MUTED))
        return
    t = Table(title=Text("Orders", style="bold"), box=box.SIMPLE_HEAD, header_style=MUTED, title_justify="left")
    for col in ("Market", "Side", "Price", "Qty", "Style", "Model", "Edge/ct", "Status"):
        t.add_column(col, justify="right" if col in ("Price", "Qty", "Model", "Edge/ct") else "left")
    for f in fills:
        o: Order = f["order"]
        color = GREEN if o.side == "yes" else ROSE
        t.add_row(o.ticker, Text(o.side.upper(), style=f"bold {color}"), cents(o.price), str(o.count), o.style,
                  pct(o.prob), f"{o.edge * 100:.1f}¢", f.get("status", ""))
    console.print(t)


def render_positions(console: Console, positions: dict[str, Position], marks: dict[str, float]) -> None:
    if not positions:
        console.print(Text("No open positions.", style=MUTED))
        return
    t = Table(title=Text("Positions", style="bold"), box=box.SIMPLE_HEAD, header_style=MUTED, title_justify="left")
    for col in ("Market", "Side", "Contracts", "Cost", "Mark", "Unrealized"):
        t.add_column(col, justify="left" if col in ("Market", "Side") else "right")
    for p in sorted(positions.values(), key=lambda p: p.ticker):
        mid = marks.get(p.ticker)
        value = None if mid is None else abs(p.contracts) * (mid if p.contracts > 0 else 1 - mid)
        pnl = None if value is None else value - p.cost
        color = GREEN if p.contracts > 0 else ROSE
        t.add_row(p.ticker, Text(p.side.upper(), style=f"bold {color}"), f"{abs(p.contracts):g}", money(p.cost),
                  "—" if mid is None else cents(mid),
                  Text("—" if pnl is None else money(pnl), style=GREEN if (pnl or 0) >= 0 else RED))
    console.print(t)
