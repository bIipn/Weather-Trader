"""Iowa Environmental Mesonet archive of NWS daily climate reports (CLI) — the official
daily highs/lows used to calibrate the model."""

from __future__ import annotations

from datetime import date

from ..http import Http

URL = "https://mesonet.agron.iastate.edu/json/cli.py"


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
