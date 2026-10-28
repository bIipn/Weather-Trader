from weather_trader.broker import PaperBroker
from weather_trader.ledger import Ledger
from weather_trader.markets import Bracket, WeatherMarket
from weather_trader.strategy import Order


def broker(tmp_path, cash=100.0):
    return PaperBroker(Ledger(tmp_path / "paper.sqlite"), cash)


def test_taker_fill_then_settlement(tmp_path):
    b = broker(tmp_path)
    r = b.submit(Order("T1", "E1", "yes", 0.40, 10, "take", 0.6, 0.18, 0.17))
    assert r == {"status": "filled", "filled": 10}
    assert abs(b.balance() - 95.83) < 1e-9
    pos = b.positions()["T1"]
    assert pos.contracts == 10 and abs(pos.cost - 4.17) < 1e-9
    b.settle({"T1": "yes"})
    assert abs(b.balance() - 105.83) < 1e-9
    assert b.positions() == {}
    assert abs(b.ledger.paper_realized() - 5.83) < 1e-9


def test_buying_the_other_side_closes_pairs(tmp_path):
    b = broker(tmp_path)
    b.submit(Order("T1", "E1", "yes", 0.40, 10, "take", 0.6, 0.1, 0.0))
    b.submit(Order("T1", "E1", "no", 0.50, 4, "take", 0.6, 0.1, 0.0))
    pos = b.positions()["T1"]
    assert pos.contracts == 6 and abs(pos.cost - 2.40) < 1e-9
    assert abs(b.balance() - 98.0) < 1e-9  # 100 - 4.00 + 4 × (1 - 0.50)
    assert abs(b.ledger.paper_realized() - 0.40) < 1e-9


def test_flipping_through_zero(tmp_path):
    b = broker(tmp_path)
    b.submit(Order("T1", "E1", "yes", 0.40, 2, "take", 0.6, 0.1, 0.0))
    b.submit(Order("T1", "E1", "no", 0.50, 5, "take", 0.6, 0.1, 0.0))
    pos = b.positions()["T1"]
    assert pos.contracts == -3 and abs(pos.cost - 1.50) < 1e-9


def test_resting_quote_fills_when_market_trades_through(tmp_path):
    b = broker(tmp_path)
    assert b.submit(Order("T1", "E1", "no", 0.64, 5, "make", 0.69, 0.05, 0.0))["status"] == "resting"
    m = WeatherMarket("T1", "E1", "S", Bracket("between", 62, 63), 0.36, 0.40, 10, 10, None, 0, 0, None, "active")
    b.sync({"T1": m}, {})  # NO ask = 1 - 0.36 = 0.64 → our 64¢ bid is hit
    assert b.positions()["T1"].contracts == -5
    assert b.resting() == []


def test_cannot_spend_more_than_cash(tmp_path):
    b = broker(tmp_path, cash=5.0)
    r = b.submit(Order("T1", "E1", "yes", 0.50, 100, "take", 0.6, 0.1, 0.0))
    assert r["filled"] == 10
