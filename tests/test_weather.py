from datetime import date, datetime, timezone

from conftest import load

from weather_trader.calibrate import day_windows
from weather_trader.stations import STATIONS, climate_day_window
from weather_trader.weather import NWS, OpenMeteo, build_nowcast, window_extreme
from weather_trader.weather.iem import parse_cli
from weather_trader.weather.nws import Observation, despike, official_reading, parse_remarks
from weather_trader.weather.openmeteo import daily_extremes
from weather_trader.weather.tomorrow import parse_hourly

NYC = STATIONS["KNYC"]
UTC = timezone.utc


def test_climate_day_uses_local_standard_time_even_in_dst():
    start, end = climate_day_window(NYC, date(2026, 7, 4))
    assert start == datetime(2026, 7, 4, 5, tzinfo=UTC)  # 1 AM EDT = midnight EST
    assert (end - start).total_seconds() == 86400
    la_start, _ = climate_day_window(STATIONS["KLAX"], date(2026, 1, 10))
    assert la_start == datetime(2026, 1, 10, 8, tzinfo=UTC)


def test_deterministic_models_daily_high(fake_http):
    h = OpenMeteo(fake_http).forecast(NYC, ["gfs_seamless", "ncep_nbm_conus", "ecmwf_ifs025"])
    start, end = climate_day_window(NYC, date(2026, 10, 29))
    highs = {m: window_extreme(h.times, h.series[m], start, end)[0] for m in h.series}
    assert highs == {"gfs_seamless": 67.1, "ncep_nbm_conus": 69.2, "ecmwf_ifs025": 66.7}


def test_ensemble_members_are_grouped_by_model(fake_http):
    ens = OpenMeteo(fake_http).ensemble(NYC, ["gfs025", "ecmwf_ifs025", "icon_seamless"])
    counts = {m: len(v) for m, v in ens.members.items()}
    assert counts == {"ncep_gefs025": 31, "ecmwf_ifs025_ensemble": 51, "icon_seamless_eps": 40}


def test_previous_runs_give_day_ahead_forecasts(fake_http):
    times, series = OpenMeteo(fake_http).previous_runs(
        NYC, ["gfs_seamless", "ecmwf_ifs025"], date(2026, 10, 20), date(2026, 10, 21))
    assert set(series) == {(m, d) for m in ("gfs_seamless", "ecmwf_ifs025") for d in (0, 1, 2)}
    windows = day_windows(NYC, date(2026, 10, 20), date(2026, 10, 20))
    assert daily_extremes(times, series[("gfs_seamless", 1)], windows)[date(2026, 10, 20)] == 67.8
    assert daily_extremes(times, series[("ecmwf_ifs025", 1)], windows)[date(2026, 10, 20)] == 65.7


def test_metar_remark_groups():
    g = parse_remarks("KNYC 281751Z AUTO 00000KT 10SM CLR 15/04 A3044 RMK AO2 SLP299 T01500039 10150 20078 58022 $")
    assert g == {"temp_c": 15.0, "max6_c": 15.0, "min6_c": 7.8}
    g = parse_remarks("KNYC 280451Z AUTO 04003KT 10SM CLR 08/05 A3045 RMK AO2 SLP302 T00780050 401610078 $")
    assert g["max24_c"] == 16.1 and g["min24_c"] == 7.8
    assert parse_remarks("KXYZ 010151Z RMK AO2 T10221033")["temp_c"] == -2.2


def test_official_reading_round_trips_asos_fahrenheit():
    assert official_reading(16.1) == 61.0
    assert official_reading(15.0) == 59.0
    assert official_reading(-2.2) == 28.0


def test_nowcast_running_max_is_unconfirmed_between_synoptic_reports(fake_http):
    obs = NWS(fake_http).observations("KNYC", datetime(2026, 10, 27, tzinfo=UTC))
    start, end = climate_day_window(NYC, date(2026, 10, 28))
    nc = build_nowcast(obs, start, end, "high")
    assert nc.extreme == 61.0 and not nc.exact and not nc.final
    assert nc.last_time == datetime(2026, 10, 28, 18, 51, tzinfo=UTC)


def test_nowcast_24h_group_matches_the_official_climate_report(fake_http):
    obs = NWS(fake_http).observations("KNYC", datetime(2026, 10, 27, tzinfo=UTC))
    start, end = climate_day_window(NYC, date(2026, 10, 27))
    cli = parse_cli(load("iem_cli_knyc_2026.json"))[date(2026, 10, 27)]
    high = build_nowcast(obs, start, end, "high")
    low = build_nowcast(obs, start, end, "low")
    assert high.final and high.extreme == cli["high"] == 61
    assert low.final and low.extreme == cli["low"] == 46


def test_tomorrow_io_hourly_parsing():
    h = parse_hourly({"timelines": {"hourly": [{"time": "2026-10-29T18:00:00Z", "values": {"temperature": 67.1}}]}})
    assert h.series == {"tomorrow_io": [67.1]}
    assert h.times[0] == datetime(2026, 10, 29, 18, tzinfo=UTC)


def test_despike_drops_isolated_sensor_glitches():
    t = datetime(2026, 10, 28, 12, 51, tzinfo=UTC)
    temps = [15.0, 15.6, 37.2, 16.1, 16.7]  # one 99°F glitch in a 60°F afternoon
    obs = [Observation(t.replace(hour=12 + i), c) for i, c in enumerate(temps)]
    assert [o.temp_c for o in despike(obs)] == [15.0, 15.6, 16.1, 16.7]
    start, end = climate_day_window(NYC, date(2026, 10, 28))
    assert build_nowcast(obs, start, end, "high").extreme == 62.0
