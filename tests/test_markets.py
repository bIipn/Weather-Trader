from datetime import date

from conftest import load

from weather_trader.markets import Bracket, events_from_markets, is_daily_temperature_series, parse_event_date
from weather_trader.stations import resolve_station


def events():
    return events_from_markets(load("kalshi_markets_kxhighny.json")["markets"], "KXHIGHNY")


def test_events_are_grouped_and_typed():
    evs = events()
    assert [e.event_ticker for e in evs] == ["KXHIGHNY-26OCT28", "KXHIGHNY-26OCT29"]
    ev = evs[1]
    assert (ev.station.code, ev.station_source, ev.kind, ev.unit) == ("KNYC", "rules", "high", "F")
    assert ev.day == date(2026, 10, 29)
    assert [m.bracket.label() for m in ev.markets] == ["≤66°", "67–68°", "69–70°", "71–72°", "73–74°", "≥75°"]


def test_bracket_semantics_follow_the_rules_text():
    less = Bracket("less", None, 67)  # "less than 67°" → 66 or below
    assert less.bounds() == (None, 66) and less.contains(66) and not less.contains(67)
    greater = Bracket("greater", 74, None)  # "greater than 74°" → 75 or above
    assert greater.bounds() == (75, None) and greater.contains(75) and not greater.contains(74)
    between = Bracket("between", 67, 68)
    assert between.contains(67) and between.contains(68) and not between.contains(69)


def test_quotes_treat_empty_sides_as_missing():
    by = {m.ticker: m for e in events() for m in e.markets}
    tail = by["KXHIGHNY-26OCT29-T74"]
    assert tail.yes_bid is None and tail.yes_ask == 0.01
    assert tail.no_bid == 0.99 and tail.no_ask is None
    mid = by["KXHIGHNY-26OCT29-B67.5"]
    assert (mid.yes_bid, mid.yes_ask, mid.no_bid, mid.no_ask) == (0.56, 0.57, 0.43, 0.44)
    assert abs(mid.mid - 0.565) < 1e-9


def test_event_dates():
    assert parse_event_date("KXHIGHNY-26OCT29") == date(2026, 10, 29)
    assert parse_event_date("KXHIGHTDAL-27JAN03") == date(2027, 1, 3)
    assert parse_event_date("KXHIGHNY-26FOO29") is None


def test_series_filter_keeps_daily_temperature_series():
    picks = {s["ticker"] for s in load("kalshi_series.json")["series"] if is_daily_temperature_series(s)}
    assert {"KXHIGHNY", "KXHIGHCHI", "KXLOWTNYC", "KXHIGHTDAL"} <= picks


def test_station_resolution_order():
    rules = "If the maximum temperature recorded at New York City (CLINYC) for Oct 29, 2026, is ..."
    assert resolve_station("KXHIGHNY", rules)[0].code == "KNYC"
    st, how = resolve_station("KXHIGHCHI", "")
    assert (st.code, how) == ("KMDW", "fallback")
    st, how = resolve_station("SOMETHING", "", "Miami")
    assert (st.code, how) == ("KMIA", "city")
    assert resolve_station("SOMETHING", "", None) == (None, "unknown")
