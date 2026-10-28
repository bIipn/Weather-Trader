import math

from weather_trader.markets import Bracket
from weather_trader.model.distribution import OutcomeDistribution, ScaleMixture


def test_mixture_has_the_requested_total_spread():
    mix = ScaleMixture(0.0, 2.0)
    xs = [i * 0.01 for i in range(-4000, 4001)]
    var = sum(((a + b) / 2) ** 2 * (mix.cdf(b) - mix.cdf(a)) for a, b in zip(xs, xs[1:]))
    assert math.isclose(math.sqrt(var), 2.0, rel_tol=0.01)


def test_mixture_has_fatter_tails_than_normal():
    mix = ScaleMixture(0.0, 1.0)
    normal_tail = 0.5 * math.erfc(4 / math.sqrt(2))
    assert 1 - mix.cdf(4.0) > 5 * normal_tail


def test_bracket_partition_sums_to_one():
    dist = OutcomeDistribution(ScaleMixture(68.1, 2.2))
    brackets = [Bracket("less", None, 67), Bracket("between", 67, 68), Bracket("between", 69, 70),
                Bracket("between", 71, 72), Bracket("between", 73, 74), Bracket("greater", 74, None)]
    assert math.isclose(sum(dist.prob(b) for b in brackets), 1.0, abs_tol=1e-9)
    assert math.isclose(dist.mean(), 68.1, abs_tol=0.05)


def test_observed_high_is_a_floor():
    # 61° already observed, the rest of the day is forecast much cooler.
    dist = OutcomeDistribution(ScaleMixture(55.0, 1.0), "high", ((61.0, 1.0),))
    assert dist.prob_between(None, 60) == 0.0
    assert math.isclose(dist.pmf(61), 1.0, abs_tol=1e-3)


def test_unconfirmed_peak_can_still_tick_up_one_degree():
    dist = OutcomeDistribution(ScaleMixture(55.0, 1.0), "high", ((61.0, 0.7), (62.0, 0.3)))
    assert math.isclose(dist.prob(Bracket("between", 60, 61)), 0.7, abs_tol=1e-3)
    assert math.isclose(dist.prob(Bracket("between", 62, 63)), 0.3, abs_tol=1e-3)


def test_warm_afternoon_still_ahead_dominates_the_floor():
    dist = OutcomeDistribution(ScaleMixture(68.0, 1.0), "high", ((61.0, 1.0),))
    assert dist.prob(Bracket("between", 67, 68)) > 0.5
    assert dist.prob_between(None, 61) < 0.01


def test_observed_low_is_a_ceiling():
    dist = OutcomeDistribution(ScaleMixture(45.0, 1.0), "low", ((40.0, 1.0),))
    assert math.isclose(dist.pmf(40), 1.0, abs_tol=1e-3)
    colder = OutcomeDistribution(ScaleMixture(37.0, 1.0), "low", ((40.0, 1.0),))
    assert colder.prob_between(None, 38) > 0.6
    assert colder.prob_between(41, None) == 0.0
