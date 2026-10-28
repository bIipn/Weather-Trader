from datetime import date, datetime, timezone

from test_backtest import END, SyntheticSession

from weather_trader.config import Settings, StrategyConfig
from weather_trader.http import Http
from weather_trader.markets import Bracket, WeatherEvent, WeatherMarket
from weather_trader.sniper import is_dead, observed_extreme, run_sniper_backtest, snipe_orders, summarize_snipes
from weather_trader.stations import STATIONS
from weather_trader.weather.nws import Observation

UTC = timezone.utc


def m(ticker, bracket, bid, ask, bid_size=50.0):
    return WeatherMarket(ticker, "EV", "KXHIGHNY", bracket, bid, ask, bid_size, 50.0, None, 0, 0, None, "active")


def test_dead_bracket_rules():
    low = m("A", Bracket("less", None, 63), 0.05, 0.08)       # 62 or below
    mid = m("B", Bracket("between", 63, 64), 0.40, 0.45)
    top = m("C", Bracket("greater", 66, None), 0.10, 0.12)    # 67 or above
    assert is_dead(low, 64, "high") and not is_dead(mid, 64, "high") and not is_dead(top, 64, "high")
    # For lows a reading of 40 rules out everything that needs the minimum to be above 40.
    assert is_dead(m("D", Bracket("between", 41, 42), 0.1, 0.2), 40, "low")
    assert not is_dead(m("E", Bracket("between", 39, 40), 0.1, 0.2), 40, "low")


def test_snipe_sells_only_dead_brackets_with_a_bid():
    ev = WeatherEvent("EV", "KXHIGHNY", STATIONS["KNYC"], "rules", date(2026, 10, 28), "high", "F")
    ev.markets = [m("A", Bracket("less", None, 63), 0.05, 0.08, bid_size=30),
                  m("B", Bracket("between", 63, 64), 0.40, 0.45),
                  m("Z", Bracket("less", None, 61), None, 0.01)]
    orders = snipe_orders(ev, 64.0, StrategyConfig())
    assert [(o.ticker, o.side, o.price, o.count) for o in orders] == [("A", "no", 0.95, 30)]
    assert orders[0].book_side == "ask" and orders[0].yes_price == 0.05


def test_observed_extreme_uses_official_grid():
    t = datetime(2026, 10, 28, 18, 51, tzinfo=UTC)
    obs = [Observation(t, 16.1), Observation(t.replace(hour=19), 17.8)]
    start, end = datetime(2026, 10, 28, 5, tzinfo=UTC), datetime(2026, 10, 29, 5, tzinfo=UTC)
    assert observed_extreme(obs, start, end, "high") == 64.0
    assert observed_extreme(obs, start, end, "low") == 61.0


def test_sniper_backtest_runs_end_to_end():
    http = Http("test", session=SyntheticSession(), sleep=lambda x: None)
    samples, errors = run_sniper_backtest(http, Settings(), ["KXHIGHNY"], days=1, end=END)
    assert not errors
    assert samples, "the two brackets below the synthetic high must die during the day"
    assert all(x.outcome == 0 for x in samples)  # a dead bracket can never settle YES
    summary = summarize_snipes(samples)
    assert summary["violations"] == 0 and summary["after_2min"]["opportunities"] > 0
