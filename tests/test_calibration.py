import math
import random
from datetime import date, timedelta

from weather_trader.model.calibration import CalibrationStore, StationCalibration, fit_calibration


def test_fit_recovers_bias_weights_and_spread():
    rng = random.Random(7)
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(300)]
    truth = {d: 60 + 15 * math.sin(i / 58.0) for i, d in enumerate(days)}
    actuals = {d: round(t) for d, t in truth.items()}
    good = {d: t - 1.0 + rng.gauss(0, 1.5) for d, t in truth.items()}  # runs 1° cold
    poor = {d: t + 0.5 + rng.gauss(0, 2.5) for d, t in truth.items()}  # runs warm, noisier
    cal = fit_calibration("KTST", "high", actuals, {("good", 1): good, ("poor", 1): poor})
    assert abs(cal.bias["good"][1] - 1.0) < 0.4
    assert abs(cal.bias["poor"][1] + 0.5) < 0.6
    assert cal.weight("good") > cal.weight("poor")
    assert 1.0 < cal.sigma[1] < 2.2
    assert cal.n_days == 300


def test_store_round_trip_and_defaults(tmp_path):
    store = CalibrationStore(tmp_path)
    fresh = store.get("KNYC", "high")
    assert not fresh.fitted
    assert math.isclose(fresh.sigma_at(27.0), 2.3)
    assert math.isclose(fresh.model_bias("gfs_seamless", 27.0), 0.5)
    cal = StationCalibration("KNYC", "high", bias={"gfs_seamless": {0: 0.4, 1: 0.9}}, sigma={0: 1.2, 1: 2.0},
                             weights={"gfs_seamless": 1.3})
    store.save(cal)
    loaded = CalibrationStore(tmp_path).get("KNYC", "high")
    assert loaded.bias == {"gfs_seamless": {0: 0.4, 1: 0.9}} and loaded.sigma == {0: 1.2, 1: 2.0}
    assert math.isclose(loaded.model_bias("ens:ncep_gefs025", 27.0), 0.9)  # ensembles borrow the parent bias
    assert math.isclose(loaded.sigma_at(15.0), 1.6)


def test_sigma_grows_with_lead_time():
    cal = StationCalibration("KNYC", "high")
    values = [cal.sigma_at(h) for h in (0, 3, 15, 27, 51, 75, 120)]
    assert values == sorted(values)
    assert math.isclose(values[0], 0.7)
