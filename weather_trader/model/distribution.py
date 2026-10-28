"""Probability distribution of the official daily reading.

The reading is an integer on the station's grid (whole °F for US markets).  Before the
day starts it is just the rounded forecast extreme.  During the day it is

    high:  Y = max(F, round(R))        low:  Y = min(F, round(R))

where F is the extreme already observed (with a small chance the true peak between
hourly reports was one step beyond what the reports show) and R is the continuous
extreme over the rest of the climate day.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_SQRT2 = math.sqrt(2.0)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


@dataclass(frozen=True)
class ScaleMixture:
    """Fat-tailed error model: (1-w)·N(mu, s) + w·N(mu, k·s), with total sd = ``sigma``.

    Forecast busts (a front arriving early, clouds that never clear) make real errors far
    more heavy-tailed than a Normal — pricing tail brackets off a Normal is how weather
    bots blow up selling 2¢ "impossible" outcomes.
    """

    mu: float
    sigma: float
    tail_weight: float = 0.15
    tail_scale: float = 2.5

    @property
    def core_sigma(self) -> float:
        w, k = self.tail_weight, self.tail_scale
        return self.sigma / math.sqrt((1 - w) + w * k * k)

    def cdf(self, x: float) -> float:
        s = max(self.core_sigma, 1e-6)
        z = (x - self.mu) / s
        w = self.tail_weight
        return (1 - w) * _phi(z) + w * _phi(z / self.tail_scale)


@dataclass(frozen=True)
class OutcomeDistribution:
    rest: ScaleMixture | None
    kind: str = "high"
    observed: tuple[tuple[float, float], ...] = ()  # (value, weight) scenarios, weights sum to 1
    step: float = 1.0

    def cdf(self, y: float) -> float:
        """P(Y <= y) for y on the reading grid."""
        half = self.step / 2.0
        if self.kind == "high":
            p_rest = self.rest.cdf(y + half) if self.rest else 1.0
            if not self.observed:
                return p_rest
            return sum(w for v, w in self.observed if v <= y + 1e-9) * p_rest
        # low: P(Y <= y) = 1 - P(Y >= y + step) = 1 - P(F >= y+step) · P(R >= y + half)
        s_rest = (1.0 - self.rest.cdf(y + half)) if self.rest else 1.0
        if not self.observed:
            return 1.0 - s_rest
        return 1.0 - sum(w for v, w in self.observed if v >= y + self.step - 1e-9) * s_rest

    def prob_between(self, lo: float | None, hi: float | None) -> float:
        upper = self.cdf(hi) if hi is not None else 1.0
        lower = self.cdf(lo - self.step) if lo is not None else 0.0
        return min(1.0, max(0.0, upper - lower))

    def prob(self, bracket) -> float:
        return self.prob_between(*bracket.bounds(self.step))

    def pmf(self, y: float) -> float:
        return max(0.0, self.cdf(y) - self.cdf(y - self.step))

    def support(self, eps: float = 5e-4) -> tuple[float, float]:
        center = self.center()
        lo = hi = round(center / self.step) * self.step
        for _ in range(400):
            if self.cdf(lo - self.step) <= eps:
                break
            lo -= self.step
        for _ in range(400):
            if 1.0 - self.cdf(hi) <= eps:
                break
            hi += self.step
        return lo, hi

    def center(self) -> float:
        if self.rest is None:
            return self.observed[0][0] if self.observed else 0.0
        if self.observed:
            v = self.observed[0][0]
            return max(v, self.rest.mu) if self.kind == "high" else min(v, self.rest.mu)
        return self.rest.mu

    def grid(self, eps: float = 5e-4) -> list[tuple[float, float]]:
        lo, hi = self.support(eps)
        out = []
        y = lo
        while y <= hi + 1e-9:
            out.append((y, self.pmf(y)))
            y += self.step
        return out

    def mean(self) -> float:
        return sum(y * p for y, p in self.grid(1e-6))

    def quantile(self, q: float) -> float:
        lo, hi = self.support(1e-6)
        y = lo
        while y < hi and self.cdf(y) < q:
            y += self.step
        return y
