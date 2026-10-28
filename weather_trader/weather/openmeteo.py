"""Open-Meteo: multi-model deterministic forecasts, ensembles and archived past runs.

Free for non-commercial use without a key; set OPEN_METEO_API_KEY to use the paid
customer endpoints (recommended if you trade with real money).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..http import Http
from ..stations import Station

_KEY_RE = re.compile(r"^temperature_2m(?:_previous_day(\d+))?(?:_member(\d+))?(?:_(.+))?$")

DEFAULT_MODELS = [
    "ncep_nbm_conus",  # National Blend of Models — station-calibrated, very strong for US highs/lows
    "gfs_seamless",    # GFS + HRRR for the first two days
    "ecmwf_ifs025",
    "icon_seamless",
    "gem_seamless",
    "ukmo_seamless",
    "jma_seamless",
    "meteofrance_seamless",
]
DEFAULT_ENSEMBLES = ["gfs025", "ecmwf_ifs025", "icon_seamless", "gem_global", "ecmwf_aifs025"]


@dataclass
class Hourly:
    """Hourly series keyed by source name; times are UTC."""

    times: list[datetime]
    series: dict[str, list[float | None]] = field(default_factory=dict)

    def at(self, name: str, t: datetime) -> float | None:
        """Linear interpolation of one series at time ``t``."""
        vals = self.series.get(name)
        if not vals or not self.times:
            return None
        for i in range(len(self.times) - 1):
            t0, t1 = self.times[i], self.times[i + 1]
            if t0 <= t <= t1:
                v0, v1 = vals[i], vals[i + 1]
                if v0 is None or v1 is None:
                    return v0 if v1 is None else v1
                w = (t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
                return v0 + w * (v1 - v0)
        return None


@dataclass
class EnsembleHourly:
    times: list[datetime]
    members: dict[str, list[list[float | None]]] = field(default_factory=dict)


def _times(payload: dict) -> list[datetime]:
    return [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in payload["hourly"]["time"]]


def _iter_series(payload: dict, requested: list[str]):
    """Yield (model, lead_days, member, values) for every temperature column."""
    fallback = requested[0] if len(requested) == 1 else "default"
    for key, values in payload.get("hourly", {}).items():
        if key == "time":
            continue
        m = _KEY_RE.match(key)
        if not m:
            continue
        lead, member, model = m.groups()
        yield model or fallback, int(lead or 0), int(member or 0), values


def window_extreme(
    times: list[datetime],
    values: list[float | None],
    start: datetime,
    end: datetime,
    kind: str = "high",
    min_coverage: float = 0.75,
) -> tuple[float, datetime] | None:
    """Max (or min) of an hourly series over [start, end); None if too many gaps."""
    good = [(v, t) for t, v in zip(times, values) if start <= t < end and v is not None]
    expected = max(int((end - start).total_seconds() // 3600), 1)
    if not good or len(good) < min_coverage * expected:
        return None
    return max(good, key=lambda p: p[0]) if kind == "high" else min(good, key=lambda p: p[0])


def daily_extremes(
    times: list[datetime],
    values: list[float | None],
    windows: list[tuple[date, datetime, datetime]],
    kind: str = "high",
    min_coverage: float = 0.75,
) -> dict[date, float]:
    """Fast per-day extremes for a long, contiguous hourly series (calibration/backtests)."""
    if not times:
        return {}
    t0 = times[0]
    out: dict[date, float] = {}
    for day, start, end in windows:
        i = max(math.ceil((start - t0).total_seconds() / 3600), 0)
        j = max(math.ceil((end - t0).total_seconds() / 3600), 0)
        seg = [v for v in values[i:j] if v is not None]
        if seg and j > i and len(seg) >= min_coverage * (j - i):
            out[day] = max(seg) if kind == "high" else min(seg)
    return out


class OpenMeteo:
    def __init__(self, http: Http, api_key: str | None = None):
        self.http = http
        self.api_key = api_key

    def _url(self, host: str, path: str = "/v1/forecast") -> str:
        prefix = "customer-" if self.api_key else ""
        return f"https://{prefix}{host}{path}"

    def _params(self, station: Station, unit: str, **extra) -> dict:
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
            "timezone": "GMT",
            **extra,
        }
        if self.api_key:
            params["apikey"] = self.api_key
        return params

    def forecast(self, station: Station, models: list[str] | None = None, unit: str = "F",
                 past_days: int = 1, forecast_days: int = 3, ttl: float = 900) -> Hourly:
        models = models or DEFAULT_MODELS
        payload = self.http.get_json(
            self._url("api.open-meteo.com"),
            self._params(station, unit, models=",".join(models), past_days=past_days, forecast_days=forecast_days),
            ttl=ttl,
        )
        out = Hourly(_times(payload))
        for model, _lead, _member, values in _iter_series(payload, models):
            if any(v is not None for v in values):
                out.series[model] = values
        return out

    def ensemble(self, station: Station, models: list[str] | None = None, unit: str = "F",
                 forecast_days: int = 3, ttl: float = 1800) -> EnsembleHourly:
        models = models or DEFAULT_ENSEMBLES
        payload = self.http.get_json(
            self._url("ensemble-api.open-meteo.com", "/v1/ensemble"),
            self._params(station, unit, models=",".join(models), forecast_days=forecast_days),
            ttl=ttl,
        )
        out = EnsembleHourly(_times(payload))
        for model, _lead, _member, values in _iter_series(payload, models):
            if any(v is not None for v in values):
                out.members.setdefault(model, []).append(values)
        return out

    def previous_runs(self, station: Station, models: list[str], start: date, end: date,
                      leads: tuple[int, ...] = (0, 1, 2), unit: str = "F") -> tuple[list[datetime], dict]:
        """Archived forecasts: {(model, lead_days): hourly values} for calibration/backtests."""
        hourly = ",".join("temperature_2m" if d == 0 else f"temperature_2m_previous_day{d}" for d in leads)
        payload = self.http.get_json(
            self._url("previous-runs-api.open-meteo.com"),
            self._params(station, unit, models=",".join(models), start_date=start.isoformat(),
                         end_date=end.isoformat()) | {"hourly": hourly},
        )
        series = {}
        for model, lead, _member, values in _iter_series(payload, models):
            series[(model, lead)] = values
        return _times(payload), series
