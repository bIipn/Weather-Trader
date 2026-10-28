import json
from pathlib import Path

import pytest

from weather_trader.http import Http

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)
        self.headers = {}

    def json(self):
        return self._data


class FakeSession:
    """Serves the captured real-world payloads in tests/fixtures by URL."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((method, url, params, json))
        if url.endswith("/series"):
            return FakeResponse(load("kalshi_series.json"))
        if url.endswith("/markets"):
            if params.get("series_ticker") == "KXHIGHNY" or params.get("tickers"):
                return FakeResponse(load("kalshi_markets_kxhighny.json"))
            return FakeResponse({"markets": [], "cursor": ""})
        if "ensemble-api" in url:
            return FakeResponse(load("openmeteo_ensemble_knyc.json"))
        if "previous-runs" in url:
            return FakeResponse(load("openmeteo_previous_runs_knyc.json"))
        if "open-meteo" in url:
            return FakeResponse(load("openmeteo_forecast_knyc.json"))
        if "weather.gov" in url:
            return FakeResponse(load("nws_observations_knyc.json"))
        if "iastate" in url:
            return FakeResponse(load("iem_cli_knyc_2026.json"))
        return FakeResponse({"message": f"no fixture for {url}"}, status=404)


@pytest.fixture
def fake_http():
    return Http("test", session=FakeSession(), sleep=lambda s: None)
