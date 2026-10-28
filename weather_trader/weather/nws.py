"""National Weather Service observations and the intraday "nowcast" of the daily extreme.

Hourly METARs can miss the true peak between reports, but the remarks section carries
6-hour max/min groups (``1snTTT`` / ``2snTTT``) and, in the midnight report, 24-hour
groups (``4snTTTsnTTT``) measured from continuous data.  When those groups confirm the
running extreme we know it exactly; otherwise the official value may still come in one
degree beyond what the hourly reports show.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..http import Http

BASE = "https://api.weather.gov"
_T_RE = re.compile(r"^T([01])(\d{3})(?:[01]\d{3})?$")
_MAX6_RE = re.compile(r"^1([01])(\d{3})$")
_MIN6_RE = re.compile(r"^2([01])(\d{3})$")
_EXT24_RE = re.compile(r"^4([01])(\d{3})([01])(\d{3})$")


def _tenths(sign: str, digits: str) -> float:
    v = int(digits) / 10.0
    return -v if sign == "1" else v


def parse_remarks(raw: str) -> dict:
    """Pull precise temperature groups out of a METAR's remarks section (°C)."""
    out: dict[str, float] = {}
    if not raw or " RMK " not in raw:
        return out
    for tok in raw.split(" RMK ", 1)[1].split():
        if m := _T_RE.match(tok):
            out["temp_c"] = _tenths(m.group(1), m.group(2))
        elif m := _MAX6_RE.match(tok):
            out["max6_c"] = _tenths(m.group(1), m.group(2))
        elif m := _MIN6_RE.match(tok):
            out["min6_c"] = _tenths(m.group(1), m.group(2))
        elif m := _EXT24_RE.match(tok):
            out["max24_c"] = _tenths(m.group(1), m.group(2))
            out["min24_c"] = _tenths(m.group(3), m.group(4))
    return out


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def official_reading(c: float, unit: str = "F") -> float:
    """ASOS stations measure whole °F; METAR °C tenths convert back exactly when rounded."""
    if unit == "F":
        return float(math.floor(c_to_f(c) + 0.5))
    return round(c, 1)


@dataclass
class Observation:
    time: datetime
    temp_c: float | None
    max6_c: float | None = None
    min6_c: float | None = None
    max24_c: float | None = None
    min24_c: float | None = None
    raw: str = ""


def parse_observations(payload: dict) -> list[Observation]:
    obs = []
    for feat in payload.get("features", []):
        p = feat.get("properties", {})
        raw = p.get("rawMessage") or ""
        groups = parse_remarks(raw)
        temp = groups.get("temp_c")
        if temp is None:
            temp = (p.get("temperature") or {}).get("value")
        obs.append(Observation(
            time=datetime.fromisoformat(p["timestamp"]).astimezone(timezone.utc),
            temp_c=temp,
            max6_c=groups.get("max6_c"),
            min6_c=groups.get("min6_c"),
            max24_c=groups.get("max24_c"),
            min24_c=groups.get("min24_c"),
            raw=raw,
        ))
    return sorted(obs, key=lambda o: o.time)


def despike(obs: list[Observation], jump_c: float = 5.5) -> list[Observation]:
    """Drop isolated sensor glitches: a reading that jumps > ~10°F away from both neighbours.

    A single bad report would otherwise become a false floor for the day — and make the
    sniper sell a bracket that is still alive.
    """
    out = []
    for i, o in enumerate(obs):
        prev = obs[i - 1].temp_c if i > 0 else None
        nxt = obs[i + 1].temp_c if i + 1 < len(obs) else None
        far = [abs(o.temp_c - n) > jump_c for n in (prev, nxt) if n is not None]
        if far and all(far):
            continue
        out.append(o)
    return out


@dataclass
class Nowcast:
    kind: str
    extreme: float | None  # official-grid reading so far (max for highs, min for lows)
    extreme_time: datetime | None
    exact: bool  # confirmed by 6h/24h extreme groups
    final: bool  # 24h group published: the day is fully observed
    last_time: datetime | None
    last_temp: float | None
    n_obs: int

    @property
    def stale_minutes(self) -> float | None:
        if not self.last_time:
            return None
        return (datetime.now(timezone.utc) - self.last_time).total_seconds() / 60


def build_nowcast(obs: list[Observation], start: datetime, end: datetime, kind: str = "high",
                  unit: str = "F") -> Nowcast:
    hi = kind == "high"
    better = (lambda a, b: a > b) if hi else (lambda a, b: a < b)
    inside = [o for o in obs if start <= o.time < end + timedelta(minutes=10)]
    tol = timedelta(minutes=15)

    # 6-hour groups whose whole period lies inside the climate day, and the 24h group.
    group_ext = None
    covered: list[tuple[datetime, datetime]] = []
    final = False
    for o in inside:
        g6 = o.max6_c if hi else o.min6_c
        if g6 is not None and o.time - timedelta(hours=6) >= start - tol:
            r = official_reading(g6, unit)
            covered.append((o.time - timedelta(hours=6), o.time))
            if group_ext is None or better(r, group_ext):
                group_ext = r
        g24 = o.max24_c if hi else o.min24_c
        if g24 is not None and abs((o.time - end).total_seconds()) <= 75 * 60:
            final = True
            group_ext = official_reading(g24, unit)

    def is_covered(t: datetime) -> bool:
        return any(a < t <= b + tol for a, b in covered)

    extreme = extreme_time = uncovered = None
    last_time = last_temp = None
    for o in despike([o for o in inside if o.temp_c is not None and o.time < end]):
        r = official_reading(o.temp_c, unit)
        last_time, last_temp = o.time, (c_to_f(o.temp_c) if unit == "F" else o.temp_c)
        if extreme is None or better(r, extreme):
            extreme, extreme_time = r, o.time
        if not is_covered(o.time) and (uncovered is None or better(r, uncovered)):
            uncovered = r

    if final:
        extreme, exact = group_ext, True
    elif group_ext is not None and (extreme is None or not better(extreme, group_ext)):
        # Groups confirm the running extreme; it is exact unless an un-covered hourly
        # reading ties it (the true peak between reports could then be one degree beyond).
        extreme = group_ext
        exact = uncovered is None or better(extreme, uncovered)
    else:
        exact = False
    return Nowcast(kind, extreme, extreme_time, exact, final, last_time, last_temp, len(inside))


class NWS:
    def __init__(self, http: Http):
        self.http = http

    def observations(self, station_code: str, start: datetime, end: datetime | None = None,
                     ttl: float = 120) -> list[Observation]:
        params = {"start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "limit": 500}
        if end:
            params["end"] = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = self.http.get_json(f"{BASE}/stations/{station_code}/observations", params,
                                     headers={"Accept": "application/geo+json"}, ttl=ttl)
        return parse_observations(payload)
