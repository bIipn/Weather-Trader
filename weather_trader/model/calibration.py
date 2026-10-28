"""Per-station calibration: model biases, blend weights and forecast error by lead time.

``wxbot calibrate`` fits these from a year of archived forecasts (Open-Meteo previous
runs) against official climate-report highs/lows.  Until a station is calibrated the
conservative defaults below are used.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_WEIGHTS: dict[str, float] = {
    "ncep_nbm_conus": 2.0,
    "gfs_seamless": 1.0,
    "ecmwf_ifs025": 1.5,
    "icon_seamless": 1.0,
    "gem_seamless": 0.8,
    "ukmo_seamless": 1.0,
    "jma_seamless": 0.6,
    "meteofrance_seamless": 0.8,
    "tomorrow_io": 1.0,
    "ens:ncep_gefs025": 0.8,
    "ens:ecmwf_ifs025_ensemble": 1.2,
    "ens:icon_seamless_eps": 0.8,
    "ens:gem_global_ensemble": 0.6,
    "ens:ecmwf_aifs025_ensemble": 1.0,
}

# Ensembles have no public archive, so they borrow the bias of their parent model.
ENSEMBLE_BASE = {
    "ncep_gefs025": "gfs_seamless",
    "ecmwf_ifs025_ensemble": "ecmwf_ifs025",
    "ecmwf_aifs025_ensemble": "ecmwf_ifs025",
    "icon_seamless_eps": "icon_seamless",
    "gem_global_ensemble": "gem_seamless",
}

# Hours from forecast issue to the daily peak for archived lead-day buckets
# (lead 0 = latest run, lead 1 = run ~24h earlier, ...).
LEAD_HOURS = {0: 3.0, 1: 27.0, 2: 51.0, 3: 75.0}
DEFAULT_SIGMA_F = {0: 1.5, 1: 2.3, 2: 2.9, 3: 3.4}
SIGMA_FLOOR_F = 0.7
SIGMA_GROWTH_F_PER_HOUR = 0.012
DEFAULT_BIAS_F = {"high": 0.5, "low": -0.5}
# Backtests showed the official high beats the hourly reports far more often than once in
# three days; these priors are replaced per station by `wxbot calibrate`.
DEFAULT_PEAK_GAP = {0: 0.52, 1: 0.38, 2: 0.08, 3: 0.02}
DEFAULT_PEAK_GAP_CONFIRMED = {0: 0.85, 1: 0.12, 2: 0.03}


def _interp(points: list[tuple[float, float]], x: float, slope_after: float = 0.0) -> float:
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1] + slope_after * (x - pts[-1][0])


def base_model(name: str) -> str:
    if name.startswith("ens:"):
        return ENSEMBLE_BASE.get(name[4:], name[4:])
    return name


@dataclass
class StationCalibration:
    station: str
    kind: str
    unit: str = "F"
    bias: dict[str, dict[int, float]] = field(default_factory=dict)
    rmse: dict[str, dict[int, float]] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    sigma: dict[int, float] = field(default_factory=dict)
    # How far beyond the hourly reports the official extreme lands, in reading steps:
    # from hourly readings alone, and once 6-hour max/min groups have confirmed them.
    peak_gap: dict[int, float] = field(default_factory=dict)
    peak_gap_confirmed: dict[int, float] = field(default_factory=dict)
    gap_violations: int = 0  # days the official value was *inside* an observed reading
    n_days: int = 0
    updated: str = ""

    @property
    def fitted(self) -> bool:
        return bool(self.sigma)

    @property
    def scale(self) -> float:
        return 1.0 if self.unit == "F" else 5.0 / 9.0

    def model_bias(self, name: str, lead_hours: float) -> float:
        table = self.bias.get(base_model(name))
        if not table:
            return DEFAULT_BIAS_F[self.kind] * self.scale
        return _interp([(LEAD_HOURS[int(k)], v) for k, v in table.items()], lead_hours)

    def weight(self, name: str) -> float:
        return self.weights.get(name, DEFAULT_WEIGHTS.get(name, 0.8))

    def sigma_at(self, lead_hours: float) -> float:
        table = self.sigma or {k: v * self.scale for k, v in DEFAULT_SIGMA_F.items()}
        pts = [(0.0, SIGMA_FLOOR_F * self.scale)] + [(LEAD_HOURS[int(k)], v) for k, v in table.items()]
        return _interp(pts, max(lead_hours, 0.0), SIGMA_GROWTH_F_PER_HOUR * self.scale)

    def observed_scenarios(self, extreme: float, confirmed: bool, step: float = 1.0
                           ) -> tuple[tuple[float, float], ...]:
        """Where the official extreme may land given the reported one (beyond it, never inside)."""
        gaps = (self.peak_gap_confirmed or DEFAULT_PEAK_GAP_CONFIRMED) if confirmed else \
            (self.peak_gap or DEFAULT_PEAK_GAP)
        sign = 1.0 if self.kind == "high" else -1.0
        total = sum(gaps.values())
        return tuple((extreme + sign * int(k) * step, p / total) for k, p in sorted(gaps.items(), key=lambda x: int(x[0])))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "StationCalibration":
        return cls(
            station=d["station"], kind=d["kind"], unit=d.get("unit", "F"),
            bias={m: {int(k): v for k, v in t.items()} for m, t in d.get("bias", {}).items()},
            rmse={m: {int(k): v for k, v in t.items()} for m, t in d.get("rmse", {}).items()},
            weights=d.get("weights", {}),
            sigma={int(k): v for k, v in d.get("sigma", {}).items()},
            peak_gap={int(k): v for k, v in d.get("peak_gap", {}).items()},
            peak_gap_confirmed={int(k): v for k, v in d.get("peak_gap_confirmed", {}).items()},
            gap_violations=d.get("gap_violations", 0),
            n_days=d.get("n_days", 0), updated=d.get("updated", ""),
        )


class CalibrationStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._cache: dict[tuple[str, str], StationCalibration] = {}

    def path(self, station: str, kind: str) -> Path:
        return self.directory / f"{station}_{kind}.json"

    def get(self, station: str, kind: str, unit: str = "F") -> StationCalibration:
        key = (station, kind)
        if key not in self._cache:
            p = self.path(station, kind)
            if p.exists():
                self._cache[key] = StationCalibration.from_dict(json.loads(p.read_text()))
            else:
                self._cache[key] = StationCalibration(station, kind, unit)
        return self._cache[key]

    def save(self, cal: StationCalibration) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        p = self.path(cal.station, cal.kind)
        p.write_text(cal.to_json() + "\n")
        self._cache[(cal.station, cal.kind)] = cal
        return p


def fit_calibration(
    station: str,
    kind: str,
    actuals: dict[date, float],
    forecasts: dict[tuple[str, int], dict[date, float]],
    unit: str = "F",
    halflife_days: float = 60.0,
    min_days: int = 20,
) -> StationCalibration:
    """Fit biases (recency-weighted), inverse-MSE blend weights and blended error spread."""
    scale = 1.0 if unit == "F" else 5.0 / 9.0
    cap = 15.0 * scale
    ref = max(actuals) if actuals else date.today()

    def wt(d: date) -> float:
        return 0.5 ** (max((ref - d).days, 0) / halflife_days)

    cal = StationCalibration(station, kind, unit)
    errors: dict[tuple[str, int], dict[date, float]] = {}
    for (model, lead), series in forecasts.items():
        errs = {d: max(-cap, min(cap, actuals[d] - f)) for d, f in series.items()
                if f is not None and actuals.get(d) is not None}
        if len(errs) < min_days:
            continue
        W = sum(wt(d) for d in errs)
        b = sum(wt(d) * e for d, e in errs.items()) / W
        r = math.sqrt(sum(wt(d) * (e - b) ** 2 for d, e in errs.items()) / W)
        cal.bias.setdefault(model, {})[lead] = round(b, 3)
        cal.rmse.setdefault(model, {})[lead] = round(r, 3)
        errors[(model, lead)] = errs

    # Weights from day-ahead skill, shrunk halfway toward the priors.
    skill = {m: (t.get(1) or t.get(0)) for m, t in cal.rmse.items()}
    skill = {m: s for m, s in skill.items() if s}
    if skill:
        inv = {m: 1.0 / s**2 for m, s in skill.items()}
        prior_mean = sum(DEFAULT_WEIGHTS.get(m, 1.0) for m in inv) / len(inv)
        norm = prior_mean * len(inv) / sum(inv.values())
        cal.weights = {m: round(0.5 * DEFAULT_WEIGHTS.get(m, 1.0) + 0.5 * v * norm, 3) for m, v in inv.items()}

    # Error spread of the blended, bias-corrected forecast, per lead.
    leads = sorted({lead for _, lead in errors})
    days_seen: set[date] = set()
    for lead in leads:
        models = [m for (m, ld) in errors if ld == lead]
        num = den = 0.0
        for d in sorted({d for m in models for d in errors[(m, lead)]}):
            avail = [m for m in models if d in forecasts[(m, lead)] and forecasts[(m, lead)][d] is not None]
            if len(avail) < 2:
                continue
            ws = sum(cal.weight(m) for m in avail)
            blend = sum(cal.weight(m) * (forecasts[(m, lead)][d] + cal.bias[m][lead]) for m in avail) / ws
            e = max(-cap, min(cap, actuals[d] - blend))
            num += wt(d) * e * e
            den += wt(d)
            days_seen.add(d)
        if den > 0:
            cal.sigma[lead] = round(max(math.sqrt(num / den) * 1.05, 0.8 * scale), 3)
    cal.n_days = len(days_seen)
    cal.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return cal
