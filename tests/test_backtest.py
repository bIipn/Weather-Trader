"""End-to-end backtest on a small synthetic world (no network)."""

import json
import math
from datetime import date, datetime, timedelta, timezone

from conftest import FakeResponse

from weather_trader.backtest import run_backtest
from weather_trader.config import Settings
from weather_trader.http import Http
from weather_trader.stations import STATIONS

NYC = STATIONS["KNYC"]
UTC = timezone.utc
END = date(2026, 10, 20)
MODEL_BIAS = {"gfs_seamless": 1.0, "ecmwf_ifs025": -0.5, "icon_seamless": 0.0}


def true_high(d: date) -> float:
    return 62 + 10 * math.sin(2 * math.pi * d.timetuple().tm_yday / 365)


def temp_at(t: datetime) -> float:
    """Diurnal cycle peaking at 3 PM local standard time."""
    local = t + NYC.std_offset
    d = local.date()
    return true_high(d) - 8 + 8 * math.cos(2 * math.pi * (local.hour + local.minute / 60 - 15) / 24)


class SyntheticSession:
    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        p = dict(params or {}) if not isinstance(params, list) else dict(params)
        if url.endswith("/markets"):
            return FakeResponse({"markets": self._markets() if p.get("status") == "settled" else [], "cursor": ""})
        if url.endswith("/historical/markets"):
            return FakeResponse({"markets": [], "cursor": ""})
        if "/events/" in url and url.endswith("/candlesticks"):
            tickers = [m["ticker"] for m in self._markets()]
            candles = [{"end_period_ts": ts, "yes_bid": {"close_dollars": "0.2000"}, "yes_ask": {"close_dollars": "0.2400"}}
                       for ts in range(int(p["start_ts"]), int(p["end_ts"]) + 1, 3600)]
            return FakeResponse({"market_tickers": tickers, "market_candlesticks": [candles] * len(tickers)})
        if "previous-runs" in url:
            return FakeResponse(self._previous_runs(p))
        if "cli.py" in url:
            return FakeResponse(self._cli(int(p["year"])))
        if "asos.py" in url:
            return self._metars(params)
        return FakeResponse({"message": url}, status=404)

    def _markets(self):
        h = round(true_high(END))
        rules = "If the maximum temperature recorded at New York City (CLINYC) for Oct 20, 2026, is ..."
        def mk(suffix, stype, floor, cap, yes):
            m = {"ticker": f"KXHIGHNY-26OCT20-{suffix}", "event_ticker": "KXHIGHNY-26OCT20", "strike_type": stype,
                 "rules_primary": rules, "result": "yes" if yes else "no", "status": "finalized",
                 "close_time": "2026-10-21T05:00:00Z"}
            if floor is not None:
                m["floor_strike"] = floor
            if cap is not None:
                m["cap_strike"] = cap
            return m
        return [mk("T1", "less", None, h - 2, False), mk("B1", "between", h - 2, h - 1, False),
                mk("B2", "between", h, h + 1, True), mk("B3", "between", h + 2, h + 3, False),
                mk("T2", "greater", h + 3, None, False)]

    def _previous_runs(self, p):
        model = p["models"]
        start = datetime.fromisoformat(p["start_date"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(p["end_date"]).replace(tzinfo=UTC) + timedelta(days=1)
        times, t = [], start
        while t < end:
            times.append(t)
            t += timedelta(hours=1)
        hourly = {"time": [x.strftime("%Y-%m-%dT%H:%M") for x in times]}
        for var in p["hourly"].split(","):
            hourly[f"{var}_{model}"] = [round(temp_at(x) + MODEL_BIAS.get(model, 0.0), 1) for x in times]
        return {"hourly": hourly}

    def _cli(self, year):
        d, rows = date(year, 1, 1), []
        while d.year == year:
            rows.append({"station": "KNYC", "valid": d.isoformat(), "high": round(true_high(d)), "low": 40})
            d += timedelta(days=1)
        return {"results": rows}

    def _metars(self, params):
        p = dict(params)
        t = datetime.strptime(p["sts"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC).replace(minute=51)
        end = datetime.strptime(p["ets"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
        lines = ["station,valid,tmpf,metar"]
        while t <= end:
            c = (temp_at(t) - 32) * 5 / 9
            tg = f"T{'1' if c < 0 else '0'}{abs(round(c * 10)):03d}0050"
            lines.append(f"NYC,{t:%Y-%m-%d %H:%M},{temp_at(t):.1f},KNYC {t:%d%H%M}Z AUTO RMK AO2 {tg}")
            t += timedelta(hours=1)
        resp = FakeResponse({})
        resp.text = "\n".join(lines)
        return resp


def test_backtest_replays_both_decision_points(tmp_path):
    s = Settings(calibration_dir=str(tmp_path))
    s.model.models = list(MODEL_BIAS)
    http = Http("test", session=SyntheticSession(), sleep=lambda x: None)
    res = run_backtest(http, s, ["KXHIGHNY"], days=1, calibration_days=120, end=END)
    assert not res.errors, res.errors
    modes = {x.mode for x in res.samples}
    assert "dayahead" in modes and {"nowcast14", "nowcast17", "nowcast20"} <= modes
    summary = res.summary()
    # Calibration learns the model biases, so on this clean synthetic world the model
    # beats a market that prices every bracket at 22¢.
    assert summary["dayahead"]["brier_model"] < summary["dayahead"]["brier_market"]
    assert summary["dayahead"]["trades"] >= 1
    late = [x for x in res.samples if x.mode == "nowcast20"]
    winner = next(x for x in late if x.outcome == 1)
    assert winner.p_model > 0.9  # by 8 PM the high is in the books
    json.dumps([x.__dict__ for x in res.samples])
