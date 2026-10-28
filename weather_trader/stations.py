"""Settlement stations and the "climate day" each Kalshi temperature market covers.

Kalshi daily temperature markets settle on the official daily climate report for one
station (e.g. ``CLINYC`` = Central Park).  The climate day always runs midnight to
midnight *local standard time*, even during daylight saving time — so in summer the
NYC day is 1:00 AM to 12:59 AM EDT.  Getting this window right matters a lot for the
overnight-high and morning-low edge cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import cached_property
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Station:
    code: str  # ICAO, e.g. "KNYC"
    name: str
    lat: float
    lon: float
    tz: str
    aliases: tuple[str, ...] = field(default=())

    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @cached_property
    def std_offset(self) -> timedelta:
        """UTC offset of local *standard* time (DST always adds, so take the smaller)."""
        jan = datetime(2025, 1, 15, 12, tzinfo=self.zone).utcoffset()
        jul = datetime(2025, 7, 15, 12, tzinfo=self.zone).utcoffset()
        return min(jan, jul)

    @property
    def short(self) -> str:
        return self.code[1:] if len(self.code) == 4 and self.code[0] == "K" else self.code


def climate_day_window(station: Station, day: date) -> tuple[datetime, datetime]:
    """UTC [start, end) of the climate day ``day`` at ``station``."""
    start_local = datetime.combine(day, time(0), tzinfo=timezone(station.std_offset))
    start = start_local.astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def climate_today(station: Station, now: datetime) -> date:
    """The climate day currently in progress at ``station``."""
    return now.astimezone(timezone(station.std_offset)).date()


def _s(code, name, lat, lon, tz, *aliases):
    return Station(code, name, lat, lon, tz, tuple(aliases))


_ET, _CT, _MT, _PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"

# Official climate stations used by Kalshi / The Weather Company (weather.com/kalshi).
STATIONS: dict[str, Station] = {
    s.code: s
    for s in [
        _s("KNYC", "New York City (Central Park)", 40.7789, -73.9692, _ET, "New York City", "New York", "NYC"),
        _s("KMDW", "Chicago (Midway)", 41.7860, -87.7524, _CT, "Chicago"),
        _s("KORD", "Chicago (O'Hare)", 41.9602, -87.9316, _CT),
        _s("KMIA", "Miami", 25.7881, -80.3169, _ET, "Miami"),
        _s("KAUS", "Austin (Bergstrom)", 30.1831, -97.6799, _CT, "Austin"),
        _s("KLAX", "Los Angeles (LAX)", 33.9382, -118.3866, _PT, "Los Angeles", "LA"),
        _s("KDEN", "Denver", 39.8466, -104.6562, _MT, "Denver"),
        _s("KPHL", "Philadelphia", 39.8733, -75.2268, _ET, "Philadelphia"),
        _s("KDFW", "Dallas/Fort Worth", 32.8998, -97.0190, _CT, "Dallas"),
        _s("KHOU", "Houston (Hobby)", 29.6375, -95.2825, _CT),
        _s("KIAH", "Houston (Bush)", 29.9844, -95.3608, _CT),
        _s("KBOS", "Boston", 42.3606, -71.0097, _ET, "Boston"),
        _s("KSFO", "San Francisco", 37.6197, -122.3647, _PT, "San Francisco"),
        _s("KSEA", "Seattle", 47.4447, -122.3144, _PT, "Seattle"),
        _s("KLAS", "Las Vegas", 36.0719, -115.1634, _PT, "Las Vegas"),
        _s("KPHX", "Phoenix", 33.4278, -112.0037, "America/Phoenix", "Phoenix"),
        _s("KOKC", "Oklahoma City", 35.3889, -97.6006, _CT, "Oklahoma City"),
        _s("KMSY", "New Orleans", 29.9928, -90.2508, _CT, "New Orleans"),
        _s("KSAT", "San Antonio", 29.5443, -98.4839, _CT, "San Antonio"),
        _s("KATL", "Atlanta", 33.6301, -84.4418, _ET, "Atlanta"),
        _s("KDCA", "Washington, DC", 38.8483, -77.0342, _ET, "Washington", "Washington DC", "DC"),
        _s("KMSP", "Minneapolis", 44.8831, -93.2289, _CT, "Minneapolis"),
        _s("KEWR", "Newark", 40.6827, -74.1693, _ET, "Newark"),
        _s("KTTN", "Trenton", 40.2767, -74.8156, _ET, "Trenton"),
        _s("KSAN", "San Diego", 32.7336, -117.1831, _PT, "San Diego"),
        _s("KABQ", "Albuquerque", 35.0419, -106.6156, _MT),
        _s("KCVG", "Cincinnati / Covington", 39.0446, -84.6721, _ET),
        _s("KCLL", "College Station", 30.5886, -96.3638, _CT),
        _s("KCMH", "Columbus", 39.9950, -82.8772, _ET),
        _s("KDTW", "Detroit", 42.2314, -83.3308, "America/Detroit"),
        _s("KGNV", "Gainesville", 29.6900, -82.2717, _ET),
        _s("KIND", "Indianapolis", 39.7173, -86.2944, "America/Indiana/Indianapolis"),
        _s("KJAX", "Jacksonville", 30.4941, -81.6879, _ET),
        _s("KLEX", "Lexington", 38.0365, -84.6059, _ET),
        _s("KSDF", "Louisville", 38.1811, -85.7392, "America/Kentucky/Louisville"),
        _s("KMSN", "Madison", 43.1406, -89.3453, _CT),
        _s("KMKE", "Milwaukee", 42.9550, -87.9044, _CT),
        _s("KAPC", "Napa", 38.2103, -122.2847, _PT),
        _s("KONT", "Ontario, CA", 34.0560, -117.6012, _PT),
        _s("KPNS", "Pensacola", 30.4734, -87.1866, _CT),
        _s("KPIT", "Pittsburgh", 40.4846, -80.2144, _ET),
        _s("KPVD", "Providence", 41.7225, -71.4325, _ET),
        _s("KSJC", "San Jose", 37.3594, -121.9244, _PT),
        _s("KSGF", "Springfield, MO", 37.2353, -93.4006, _CT),
        _s("KSTL", "St. Louis", 38.7525, -90.3734, _CT),
        _s("KSPG", "St. Petersburg", 27.7651, -82.6270, _ET),
        _s("KTPA", "Tampa", 27.9619, -82.5403, _ET),
    ]
}

# Fallback for the original Kalshi series, used only when a market's rules text does not
# name its climate report station explicitly.
SERIES_STATION_FALLBACK: dict[str, str] = {
    "KXHIGHNY": "KNYC", "KXLOWTNYC": "KNYC", "KXLOWNY": "KNYC", "KXLOWNYC": "KNYC",
    "KXHIGHCHI": "KMDW", "KXLOWTCHI": "KMDW", "KXLOWCHI": "KMDW",
    "KXHIGHMIA": "KMIA", "KXLOWTMIA": "KMIA", "KXLOWMIA": "KMIA",
    "KXHIGHAUS": "KAUS", "KXLOWTAUS": "KAUS", "KXLOWAUS": "KAUS",
    "KXHIGHLAX": "KLAX", "KXLOWTLAX": "KLAX", "KXLOWLAX": "KLAX",
    "KXHIGHDEN": "KDEN", "KXLOWTDEN": "KDEN", "KXLOWDEN": "KDEN",
    "KXHIGHPHIL": "KPHL", "KXLOWTPHIL": "KPHL", "KXLOWPHIL": "KPHL",
}

_CLI_RE = re.compile(r"\(CLI([A-Z]{3})\)")
_ICAO_RE = re.compile(r"\((K[A-Z]{3})\)")


def station_from_rules(text: str) -> Station | None:
    """Find the settlement station named in a market's rules, e.g. '(CLINYC)' → KNYC."""
    if not text:
        return None
    m = _CLI_RE.search(text)
    if m and f"K{m.group(1)}" in STATIONS:
        return STATIONS[f"K{m.group(1)}"]
    m = _ICAO_RE.search(text)
    if m and m.group(1) in STATIONS:
        return STATIONS[m.group(1)]
    return None


def station_from_city(city: str | None) -> Station | None:
    if not city:
        return None
    needle = city.strip().lower()
    for st in STATIONS.values():
        if needle in (a.lower() for a in st.aliases):
            return st
    return None


def resolve_station(series_ticker: str, rules: str, city: str | None = None) -> tuple[Station | None, str]:
    """Return (station, how) where ``how`` is 'rules', 'city' or 'fallback'."""
    st = station_from_rules(rules)
    if st:
        return st, "rules"
    st = station_from_city(city)
    if st:
        return st, "city"
    code = SERIES_STATION_FALLBACK.get(series_ticker)
    if code:
        return STATIONS[code], "fallback"
    return None, "unknown"
