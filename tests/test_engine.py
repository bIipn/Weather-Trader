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
    eng = engine(fake_http, tmp_path, execution="taker", max_event_loss=30.0)
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
