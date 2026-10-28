"""Iowa Environmental Mesonet archives: NWS daily climate reports (the official highs and
lows used for calibration) and historical METARs (to replay the intraday nowcast)."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from ..http import Http
from .nws import Observation, parse_remarks

URL = "https://mesonet.agron.iastate.edu/json/cli.py"
ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def parse_asos_csv(text: str) -> list[Observation]:
    """Rows of station,valid,tmpf,metar → observations (°C, with remark groups)."""
    obs = []
    rows = csv.DictReader(io.StringIO("\n".join(line for line in text.splitlines() if line and not line.startswith("#"))))
    for row in rows:
        try:
            t = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        raw = row.get("metar") or ""
        groups = parse_remarks(raw if " RMK " in raw else raw.replace("RMK", " RMK ", 1))
        temp = groups.get("temp_c")
        if temp is None:
            try:
                temp = (float(row.get("tmpf")) - 32.0) * 5.0 / 9.0
            except (TypeError, ValueError):
                temp = None
        obs.append(Observation(t, temp, groups.get("max6_c"), groups.get("min6_c"),
                               groups.get("max24_c"), groups.get("min24_c"), raw))
    return sorted(obs, key=lambda o: o.time)


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_cli(payload: dict) -> dict[date, dict[str, int | None]]:
    out = {}
    for row in payload.get("results", []):
        try:
            d = date.fromisoformat(row["valid"])
        except (KeyError, ValueError):
            continue
        out[d] = {"high": _int(row.get("high")), "low": _int(row.get("low"))}
    return out


class IEM:
    def __init__(self, http: Http):
        self.http = http

    def climate_reports(self, station_code: str, years: list[int]) -> dict[date, dict[str, int | None]]:
        out: dict[date, dict[str, int | None]] = {}
        for year in years:
            out.update(parse_cli(self.http.get_json(URL, {"station": station_code, "year": year}, ttl=3600)))
        return out

    def metars(self, station_code: str, start: datetime, end: datetime) -> list[Observation]:
        """Historical METARs (routine + specials) between two UTC times."""
        ident = station_code[1:] if len(station_code) == 4 and station_code.startswith("K") else station_code
        params = [
            ("station", ident), ("data", "tmpf"), ("data", "metar"), ("tz", "Etc/UTC"),
            ("format", "onlycomma"), ("latlon", "no"), ("elev", "no"), ("missing", "M"), ("trace", "T"),
            ("direct", "no"), ("report_type", "3"), ("report_type", "4"),
            ("sts", start.strftime("%Y-%m-%dT%H:%MZ")), ("ets", end.strftime("%Y-%m-%dT%H:%MZ")),
        ]
        return parse_asos_csv(self.http.get_text(ASOS_URL, params))
