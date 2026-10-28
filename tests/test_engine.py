import math
from datetime import datetime, timezone

from weather_trader.config import Settings
from weather_trader.engine import Engine
from weather_trader.strategy import worst_case_loss

NOW = datetime(2026, 10, 28, 20, 5, tzinfo=timezone.utc)  # when the fixtures were captured


def engine(fake_http, tmp_path, **strategy):
    s = Settings(data_dir=str(tmp_path), calibration_dir=str(tmp_path / "cal"))
    for k, v in strategy.items():
        setattr(s.strategy, k, v)
    return Engine(s, http=fake_http)


def test_scan_prices_every_bracket(fake_http, tmp_path):
    views = engine(fake_http, tmp_path).scan(now=NOW)
    assert [v.event.event_ticker for v in views] == ["KXHIGHNY-26OCT28", "KXHIGHNY-26OCT29"]
    for v in views:
        assert math.isclose(sum(r.prob for r in v.rows), 1.0, abs_tol=1e-6)

    today, tomorrow = views
    assert today.forecast.phase == "live" and today.forecast.nowcast.extreme == 61.0
    assert today.forecast.tradeable
    # The observed 61° makes anything colder impossible.
    assert all(r.prob < 1e-9 for r in today.rows if r.market.bracket.bounds()[1] == 59)
    assert tomorrow.forecast.phase == "pre" and tomorrow.forecast.n_sources == 6
    assert 66 < tomorrow.forecast.mu < 70


def test_stale_observations_block_trading(fake_http, tmp_path):
    later = datetime(2026, 10, 28, 23, 30, tzinfo=timezone.utc)  # last fixture obs is 18:51Z
    today = engine(fake_http, tmp_path).scan(now=later)[0]
    assert not today.forecast.tradeable
    assert all(r.best is None for r in today.rows)


def test_paper_cycle_journals_orders(fake_http, tmp_path):
    eng = engine(fake_http, tmp_path, execution="taker", max_event_loss=30.0, forecast_trading=True)
    res = eng.cycle(now=NOW)
    assert not res.errors
    assert res.orders, "fixture day-ahead prices disagree with the model, so the paper bot should trade"
    rows = eng.ledger.orders("paper")
    assert len(rows) == len(res.orders)
    assert eng.broker.balance() < 1000.0
    held = eng.broker.positions()
    for v in res.views:
        mine = [p for p in held.values() if p.event_ticker == v.event.event_ticker]
        assert worst_case_loss(v.event, mine, []) <= 30.0 + 1e-6


def test_forecast_trading_is_off_by_default(fake_http, tmp_path):
    views = engine(fake_http, tmp_path).scan(now=NOW)
    assert all(r.best is None or r.best.note.startswith("dead:") for v in views for r in v.rows)


def test_sniper_sells_brackets_the_observations_ruled_out(fake_http, tmp_path):
    eng = engine(fake_http, tmp_path)
    events = eng.discovery.open_events(NOW)
    today = events[0]
    cold = next(m for m in today.markets if m.bracket.bounds() == (None, 59))  # 61° already observed
    cold.yes_bid, cold.yes_bid_size = 0.05, 40
    row = next(r for r in eng.scan(now=NOW, events=events)[0].rows if r.market is cold)
    assert row.skip == "ruled out by observations"
    assert (row.best.side, row.best.price, row.best.count) == ("no", 0.95, 40)
    res = eng.cycle(now=NOW)  # fresh discovery: the real fixture has no bid on dead brackets
    assert not any(o.note.startswith("dead:") for o in res.orders)
