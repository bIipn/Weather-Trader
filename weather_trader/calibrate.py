"""Fit station calibrations from archived model runs vs. official climate reports."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta

from .config import Settings
from .http import Http, HttpError
from .model.calibration import StationCalibration, fit_calibration
from .stations import Station, climate_day_window
from .weather.iem import IEM
from .weather.nws import Observation, build_nowcast, official_reading
from .weather.openmeteo import DEFAULT_MODELS, OpenMeteo, daily_extremes

log = logging.getLogger("wxbot")


def day_windows(station: Station, start: date, end: date):
    d, out = start, []
    while d <= end:
        ws, we = climate_day_window(station, d)
        out.append((d, ws, we))
        d += timedelta(days=1)
    return out


def archived_extremes(om: OpenMeteo, station: Station, models: list[str], start: date, end: date,
                      kind: str, unit: str = "F", leads: tuple[int, ...] = (0, 1, 2)
                      ) -> dict[tuple[str, int], dict[date, float]]:
    """{(model, lead_days): {day: forecast daily extreme}} from Open-Meteo's previous-runs archive."""
    windows = day_windows(station, start, end)
    out: dict[tuple[str, int], dict[date, float]] = {}
    for model in models:  # one request per model so an unsupported model can't sink the rest
        try:
            times, series = om.previous_runs(station, [model], start - timedelta(days=1),
                                             end + timedelta(days=1), leads, unit)
        except HttpError as exc:
            log.warning("previous runs unavailable for %s/%s: %s", station.code, model, exc.status)
            continue
        for (_, lead), values in series.items():
            ext = daily_extremes(times, values, windows, kind)
            if ext:
                out[(model, lead)] = ext
    return out


def official_extremes(http: Http, station: Station, start: date, end: date, kind: str) -> dict[date, float]:
    reports = IEM(http).climate_reports(station.code, list(range(start.year, end.year + 1)))
    return {d: r[kind] for d, r in reports.items() if start <= d <= end and r.get(kind) is not None}


def fit_peak_gaps(obs: list[Observation], actuals: dict[date, float], station: Station, start: date, end: date,
                  kind: str, unit: str = "F") -> tuple[dict[int, float], dict[int, float], int]:
    """Distribution of (official extreme − most extreme hourly reading), in reading steps.

    Returned twice: from hourly readings alone, and on days where 6-hour max/min groups had
    already confirmed the reading (what the intraday nowcast sees late in the day).  Also
    counts days where the official value fell *inside* a reported reading — those would
    break the dead-bracket sniper, so they must be zero for it to be safe.
    """
    step = 1.0 if unit == "F" else 0.1
    sign = 1.0 if kind == "high" else -1.0
    plain: Counter = Counter()
    confirmed: Counter = Counter()
    violations = 0
    pad = timedelta(minutes=80)
    for d, ws, we in day_windows(station, start, end):
        actual = actuals.get(d)
        if actual is None:
            continue
        # Strip the 24-hour groups: they *are* the answer, the nowcast must do without them.
        day_obs = [replace(o, max24_c=None, min24_c=None) for o in obs if ws - pad <= o.time < we + pad]
        readings = [official_reading(o.temp_c, unit) for o in day_obs if o.temp_c is not None and ws <= o.time < we]
        if len(readings) < 18:
            continue
        ext = max(readings) if kind == "high" else min(readings)
        gap = round(sign * (actual - ext) / step)
        if gap < 0:
            violations += 1
            continue
        plain[min(gap, 3)] += 1
        nc = build_nowcast(day_obs, ws, we, kind, unit)
        if nc.exact and nc.extreme is not None:
            cgap = round(sign * (actual - nc.extreme) / step)
            if cgap >= 0:
                confirmed[min(cgap, 3)] += 1

    def smooth(c: Counter) -> dict[int, float]:
        n = sum(c.values())
        if n < 30:
            return {}
        return {k: round((c[k] + 0.5) / (n + 2.0), 4) for k in range(4)}

    return smooth(plain), smooth(confirmed), violations


def calibrate_station(http: Http, settings: Settings, station: Station, kind: str, days: int = 365,
                      unit: str = "F", end: date | None = None) -> StationCalibration:
    end = end or (date.today() - timedelta(days=2))
    start = end - timedelta(days=days - 1)
    actuals = official_extremes(http, station, start, end, kind)
    om = OpenMeteo(http, settings.open_meteo_api_key or None)
    forecasts = archived_extremes(om, station, settings.model.models or DEFAULT_MODELS, start, end, kind, unit)
    cal = fit_calibration(station.code, kind, actuals, forecasts, unit)
    try:
        w0, _ = climate_day_window(station, start)
        _, w1 = climate_day_window(station, end)
        obs = IEM(http).metars(station.code, w0 - timedelta(hours=2), w1 + timedelta(hours=2))
        cal.peak_gap, cal.peak_gap_confirmed, cal.gap_violations = fit_peak_gaps(
            obs, actuals, station, start, end, kind, unit)
    except HttpError as exc:
        log.warning("METAR archive unavailable for %s: %s", station.code, exc.status)
    return cal
