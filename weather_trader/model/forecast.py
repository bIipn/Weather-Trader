"""Turn raw weather data into a calibrated distribution for one station-day.

Pipeline: per-model daily extremes over the climate-day window → per-model bias
correction → skill-weighted blend (mu) → spread from calibrated lead-time error,
ensemble spread and model disagreement (sigma) → fat-tailed distribution.  Once the day
is under way, observations take over: the observed extreme becomes a floor (a ceiling
for lows) and only the rest of the day is forecast, anchored on the current error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..stations import Station, climate_day_window
from ..weather.nws import NWS, Nowcast, build_nowcast
from ..weather.openmeteo import EnsembleHourly, Hourly, OpenMeteo, window_extreme
from ..weather.tomorrow import TomorrowIO
from .calibration import CalibrationStore, StationCalibration
from .distribution import OutcomeDistribution, ScaleMixture


@dataclass
class Source:
    name: str
    raw: float
    adjusted: float
    weight: float
    kind: str  # model | ensemble | tomorrow
    spread: float | None = None
    members: int | None = None


@dataclass
class Forecast:
    station: Station
    day: date
    kind: str
    unit: str
    phase: str  # pre | live | final
    mu: float
    sigma: float
    lead_hours: float
    dist: OutcomeDistribution
    sources: list[Source] = field(default_factory=list)
    nowcast: Nowcast | None = None
    tradeable: bool = True
    notes: list[str] = field(default_factory=list)
    calibrated: bool = False

    @property
    def n_sources(self) -> int:
        return len(self.sources)


@dataclass
class ModelSettings:
    models: list[str] | None = None
    ensembles: list[str] | None = None
    use_ensembles: bool = True
    tail_weight: float = 0.15
    tail_scale: float = 2.5
    ensemble_spread_factor: float = 1.15
    disagreement_factor: float = 0.9
    nowcast_anomaly_hours: float = 4.0
    max_obs_age_minutes: float = 100.0
    min_sources: int = 3


def _weighted_mean_sd(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    W = sum(w for _, w in pairs)
    mu = sum(v * w for v, w in pairs) / W
    var = sum(w * (v - mu) ** 2 for v, w in pairs) / W
    return mu, math.sqrt(var)


class Forecaster:
    def __init__(
        self,
        openmeteo: OpenMeteo,
        nws: NWS | None,
        calibration: CalibrationStore,
        settings: ModelSettings | None = None,
        tomorrow: TomorrowIO | None = None,
    ):
        self.om = openmeteo
        self.nws = nws
        self.cal = calibration
        self.cfg = settings or ModelSettings()
        self.tomorrow = tomorrow

    # -- data ----------------------------------------------------------------------
    def _load(self, station: Station, unit: str, notes: list[str]):
        hourly = self.om.forecast(station, self.cfg.models, unit)
        ens = None
        if self.cfg.use_ensembles:
            try:
                ens = self.om.ensemble(station, self.cfg.ensembles, unit)
            except Exception as exc:  # ensembles are a bonus; never fatal
                notes.append(f"ensembles unavailable ({type(exc).__name__})")
        tio = None
        if self.tomorrow:
            try:
                tio = self.tomorrow.hourly(station, unit)
            except Exception as exc:
                notes.append(f"tomorrow.io unavailable ({type(exc).__name__})")
        return hourly, ens, tio

    # -- public ----------------------------------------------------------------------
    def forecast(self, station: Station, day: date, kind: str = "high", unit: str = "F",
                 now: datetime | None = None, observations=None) -> Forecast:
        now = now or datetime.now(timezone.utc)
        notes: list[str] = []
        start, end = climate_day_window(station, day)
        cal = self.cal.get(station.code, kind, unit)
        hourly, ens, tio = self._load(station, unit, notes)
        series = dict(hourly.series)
        if tio and tio.series:
            series.update(_align(tio, hourly.times))

        peak = _peak_time(hourly, start, end, kind)
        lead_h = max((peak - now).total_seconds() / 3600.0, 0.0)
        sources = self._sources(series, hourly.times, ens, start, end, kind, cal, lead_h)

        phase = "pre" if now < start else ("final" if now >= end else "live")
        if len(sources) < self.cfg.min_sources:
            notes.append(f"only {len(sources)} forecast sources")

        if not sources and phase == "pre":
            dummy = OutcomeDistribution(ScaleMixture(0.0, 10.0), kind)
            return Forecast(station, day, kind, unit, phase, float("nan"), float("nan"), lead_h, dummy,
                            tradeable=False, notes=notes + ["no forecast data"])

        mu = sigma = float("nan")
        if sources:
            mu, sigma = self._blend(sources, cal, lead_h)

        if phase == "pre":
            dist = OutcomeDistribution(self._mixture(mu, sigma), kind)
            fc = Forecast(station, day, kind, unit, phase, mu, sigma, lead_h, dist, sources, notes=notes)
        else:
            fc = self.intraday(station, day, kind, unit, now, start, end, hourly, series, cal,
                                sources, mu, notes, observations)
        fc.calibrated = cal.fitted
        if fc.tradeable and len(fc.sources) < self.cfg.min_sources and fc.phase == "pre":
            fc.tradeable = False
        return fc

    # -- internals -------------------------------------------------------------------
    def _mixture(self, mu: float, sigma: float) -> ScaleMixture:
        return ScaleMixture(mu, sigma, self.cfg.tail_weight, self.cfg.tail_scale)

    def _sources(self, series, times, ens: EnsembleHourly | None, start, end, kind,
                 cal: StationCalibration, lead_h: float) -> list[Source]:
        out: list[Source] = []
        for name, values in series.items():
            ext = window_extreme(times, values, start, end, kind)
            if ext is None:
                continue
            raw = ext[0]
            kind_ = "tomorrow" if name == "tomorrow_io" else "model"
            out.append(Source(name, raw, raw + cal.model_bias(name, lead_h), cal.weight(name), kind_))
        if ens:
            for model, members in ens.members.items():
                vals = [e[0] for m in members if (e := window_extreme(ens.times, m, start, end, kind))]
                if len(vals) < 5:
                    continue
                name = f"ens:{model}"
                mean = sum(vals) / len(vals)
                sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
                out.append(Source(name, mean, mean + cal.model_bias(name, lead_h), cal.weight(name),
                                  "ensemble", sd, len(vals)))
        return out

    def _blend(self, sources: list[Source], cal: StationCalibration, lead_h: float) -> tuple[float, float]:
        mu, disagreement = _weighted_mean_sd([(s.adjusted, s.weight) for s in sources])
        ens = [s for s in sources if s.kind == "ensemble" and s.spread is not None]
        ens_sd = math.sqrt(sum(s.spread**2 for s in ens) / len(ens)) if ens else 0.0
        sigma = max(
            cal.sigma_at(lead_h),
            self.cfg.ensemble_spread_factor * ens_sd,
            self.cfg.disagreement_factor * disagreement,
        )
        return mu, sigma

    def intraday(self, station, day, kind, unit, now, start, end, hourly: Hourly, series, cal,
                  sources, mu_full, notes, observations) -> Forecast:
        step = 1.0 if unit == "F" else 0.1
        try:
            obs = observations if observations is not None else self.nws.observations(
                station.code, start - timedelta(hours=1))
            nc = build_nowcast(obs, start, end, kind, unit)
        except Exception as exc:
            notes.append(f"observations unavailable ({type(exc).__name__})")
            nc = None

        tradeable = True
        if nc is None or nc.extreme is None:
            notes.append("no observations yet for this climate day")
            tradeable = False
        else:
            age = (now - nc.last_time).total_seconds() / 60 if nc.last_time else None
            if age is None or age > self.cfg.max_obs_age_minutes:
                notes.append(f"observations stale ({age:.0f} min)" if age else "observations stale")
                tradeable = False

        # Observed extreme as a floor (ceiling for lows). Until the 24-hour group arrives the
        # official value can still land beyond it — by how much is calibrated per station.
        observed: tuple[tuple[float, float], ...] = ()
        if nc and nc.extreme is not None:
            if nc.final:
                observed = ((nc.extreme, 1.0),)
            else:
                observed = cal.observed_scenarios(nc.extreme, nc.exact, step)

        # Remaining part of the day, anchored on how wrong the models are right now.
        rest = None
        rest_start = max(now, start)
        mu_rest = sigma_rest = float("nan")
        lead_rest = 0.0
        combined = Hourly(hourly.times, series)
        if rest_start < end - timedelta(minutes=20):
            pairs = []
            peak_times = []
            for s in sources:
                if s.kind == "ensemble":
                    continue
                values = series.get(s.name)
                ext = window_extreme(hourly.times, values, rest_start, end, kind, min_coverage=0.6) if values else None
                if ext is None:
                    continue
                value, t_ext = ext
                hours_out = max((t_ext - now).total_seconds() / 3600.0, 0.0)
                bias = cal.model_bias(s.name, hours_out)
                if nc and nc.last_temp is not None and nc.last_time is not None:
                    model_now = combined.at(s.name, nc.last_time)
                    if model_now is not None:
                        anomaly = nc.last_temp - model_now
                        a = math.exp(-max((t_ext - nc.last_time).total_seconds() / 3600.0, 0.0)
                                     / self.cfg.nowcast_anomaly_hours)
                        bias = a * anomaly + (1 - a) * bias
                pairs.append((value + bias, s.weight))
                peak_times.append(t_ext)
            if pairs:
                mu_rest, disagreement = _weighted_mean_sd(pairs)
                t_peak = sorted(peak_times)[len(peak_times) // 2]
                lead_rest = max((t_peak - now).total_seconds() / 3600.0, 0.0)
                sigma_rest = max(cal.sigma_at(lead_rest), self.cfg.disagreement_factor * disagreement)
                rest = self._mixture(mu_rest, sigma_rest)
        if rest is None and not observed:
            tradeable = False
            notes.append("nothing left to forecast and no observations")

        phase = "final" if now >= end else "live"
        dist = OutcomeDistribution(rest, kind, observed, step)
        mu = dist.mean() if (rest or observed) else mu_full
        sigma = sigma_rest if rest else 0.0
        return Forecast(station, day, kind, unit, phase, mu, sigma, lead_rest, dist, sources, nc, tradeable, notes)


def _align(src: Hourly, times: list[datetime]) -> dict[str, list[float | None]]:
    """Resample another provider's hourly series onto the Open-Meteo time axis."""
    out = {}
    for name in src.series:
        out[name] = [src.at(name, t) for t in times]
    return out


def _peak_time(hourly: Hourly, start: datetime, end: datetime, kind: str) -> datetime:
    """When the day's extreme is expected (median of models); climatology as fallback."""
    times = []
    for values in hourly.series.values():
        ext = window_extreme(hourly.times, values, start, end, kind)
        if ext:
            times.append(ext[1])
    if times:
        return sorted(times)[len(times) // 2]
    return start + timedelta(hours=15 if kind == "high" else 6)
