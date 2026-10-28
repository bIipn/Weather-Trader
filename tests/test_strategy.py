from datetime import date

from weather_trader.config import StrategyConfig
from weather_trader.markets import Bracket, WeatherEvent, WeatherMarket
from weather_trader.stations import STATIONS
from weather_trader.strategy import (EventView, MarketView, Order, Position, evaluate_market, maker_fee,
                                     select_orders, taker_fee, worst_case_loss)


def market(ticker, bracket, bid, ask, bid_size=100.0, ask_size=100.0):
    return WeatherMarket(ticker, "EV", "KXHIGHNY", bracket, bid, ask, bid_size, ask_size, None, 0, 0, None, "active")


def test_fees_match_the_published_table():
    assert taker_fee(0.50, 1) == 0.02
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.10, 100) == 0.63
    assert taker_fee(0.01, 100) == 0.07
    assert maker_fee(0.50, 100) == 0.0  # weather series: no maker fee
    assert maker_fee(0.50, 100, multiplier=1.0) == 0.44


def test_takes_yes_when_model_beats_the_ask():
    cfg = StrategyConfig(execution="hybrid")
    view = evaluate_market(market("B69", Bracket("between", 69, 70), 0.11, 0.12, ask_size=22), 0.31, cfg, 1000)
    o = view.best
    assert (o.side, o.style, o.price, o.count) == ("yes", "take", 0.12, 22)
    assert o.book_side == "bid" and o.yes_price == 0.12


def test_buying_no_is_an_ask_on_the_yes_book():
    cfg = StrategyConfig(execution="taker")
    view = evaluate_market(market("B67", Bracket("between", 67, 68), 0.56, 0.57), 0.39, cfg, 1000)
    o = view.best
    assert (o.side, o.price) == ("no", 0.44)
    assert o.book_side == "ask" and o.yes_price == 0.56
    assert o.edge > 0.10


def test_no_trade_when_market_is_fair():
    cfg = StrategyConfig(execution="hybrid")
    assert evaluate_market(market("X", Bracket("between", 60, 61), 0.65, 0.66), 0.655, cfg, 1000).best is None


def test_never_rests_behind_the_queue_on_tails():
    # Nobody bids YES; the best NO bid is already 99¢ — a 96¢ NO bid would only fill on bad news.
    cfg = StrategyConfig(execution="hybrid")
    view = evaluate_market(market("T74", Bracket("greater", 74, None), None, 0.01, 0, 1007), 0.0005, cfg, 1000)
    assert view.best is None


def test_rests_a_bid_that_improves_the_book():
    cfg = StrategyConfig(execution="maker", min_edge_maker=0.03)
    view = evaluate_market(market("B62", Bracket("between", 62, 63), 0.33, 0.37), 0.314, cfg, 1000)
    o = view.best
    assert (o.side, o.style, o.price) == ("no", "make", 0.64)  # NO bid was 63¢, NO ask 67¢


def test_huge_disagreement_is_treated_as_bad_data():
    cfg = StrategyConfig(max_model_market_gap=0.30)
    view = evaluate_market(market("X", Bracket("between", 60, 61), 0.29, 0.31), 0.90, cfg, 1000)
    assert view.best is None and "gap" in view.skip


def _event():
    ev = WeatherEvent("EV", "KXHIGHNY", STATIONS["KNYC"], "rules", date(2026, 10, 29), "high", "F")
    ev.markets = [market("LO", Bracket("less", None, 67), 0.32, 0.33),
                  market("MID", Bracket("between", 67, 68), 0.56, 0.57),
                  market("HI", Bracket("between", 69, 70), 0.11, 0.12),
                  market("TOP", Bracket("greater", 70, None), 0.01, 0.02)]
    return ev


def test_worst_case_loss_spans_every_outcome():
    ev = _event()
    yes_hi = Order("HI", "EV", "yes", 0.12, 10, "take", 0.31, 0.18, 0.0)
    no_mid = Order("MID", "EV", "no", 0.44, 10, "take", 0.61, 0.15, 0.0)
    # Both lose only if the high lands in 67–68: -1.20 (YES HI) + (-4.40) (NO MID).
    assert abs(worst_case_loss(ev, [], [yes_hi, no_mid]) - 5.60) < 1e-9
    held = [Position("HI", "EV", 10, 1.20)]
    assert abs(worst_case_loss(ev, held, []) - 1.20) < 1e-9


def test_select_orders_respects_event_and_daily_limits():
    from weather_trader.model.distribution import OutcomeDistribution, ScaleMixture
    from weather_trader.model.forecast import Forecast

    ev = _event()
    fc = Forecast(ev.station, ev.day, "high", "F", "pre", 69.0, 2.0, 24, OutcomeDistribution(ScaleMixture(69, 2)))
    big = Order("MID", "EV", "no", 0.44, 200, "take", 0.61, 0.15, 0.0)
    view = EventView(ev, fc, [MarketView(ev.markets[1], 0.39, big)])
    cfg = StrategyConfig(max_event_loss=20.0, max_total_exposure=1000)
    picked = select_orders([view], {}, cfg, exposure=0.0, loss_today=0.0)
    assert len(picked) == 1 and picked[0].count * 0.44 <= 20.0 + 1e-9
    assert select_orders([view], {}, StrategyConfig(daily_loss_limit=50), 0.0, loss_today=60.0) == []
