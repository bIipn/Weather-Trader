"""Fit station calibrations from archived model runs vs. official climate reports."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from .config import Settings
from .http import Http, HttpError
from .model.calibration import StationCalibration, fit_calibration
from .stations import Station, climate_day_window
from .weather.iem import IEM
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


def calibrate_station(http: Http, settings: Settings, station: Station, kind: str, days: int = 365,
                      unit: str = "F", end: date | None = None) -> StationCalibration:
    end = end or (date.today() - timedelta(days=2))
    start = end - timedelta(days=days - 1)
    actuals = official_extremes(http, station, start, end, kind)
    om = OpenMeteo(http, settings.open_meteo_api_key or None)
    forecasts = archived_extremes(om, station, settings.model.models or DEFAULT_MODELS, start, end, kind, unit)
    return fit_calibration(station.code, kind, actuals, forecasts, unit)
