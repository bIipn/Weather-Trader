"""Tomorrow.io forecast — the same provider behind the Tomorrow.io weather connector.

Optional: set TOMORROW_API_KEY to add Tomorrow.io's model to the blend.  The free tier is
rate limited (≈25 calls/hour), so responses are cached for an hour per station.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..http import Http
from ..stations import Station
from .openmeteo import Hourly

URL = "https://api.tomorrow.io/v4/weather/forecast"
SOURCE = "tomorrow_io"


def parse_hourly(payload: dict) -> Hourly:
    rows = (payload.get("timelines") or {}).get("hourly") or []
    times, values = [], []
    for row in rows:
        times.append(datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(timezone.utc))
        values.append((row.get("values") or {}).get("temperature"))
    return Hourly(times, {SOURCE: values} if times else {})


class TomorrowIO:
    def __init__(self, http: Http, api_key: str, ttl: float = 3600):
        self.http = http
        self.api_key = api_key
        self.ttl = ttl

    def hourly(self, station: Station, unit: str = "F") -> Hourly:
        payload = self.http.get_json(URL, {
            "location": f"{station.lat},{station.lon}",
            "timesteps": "1h",
            "units": "imperial" if unit == "F" else "metric",
            "apikey": self.api_key,
        }, ttl=self.ttl)
        return parse_hourly(payload)
