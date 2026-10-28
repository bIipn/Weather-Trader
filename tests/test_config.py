import pytest

from weather_trader.config import load_settings


def test_toml_and_env_layering(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[bot]\nmode = "demo"\nstations = ["KNYC"]\n\n[strategy]\nmin_edge = 0.08\n\n[model]\nnowcast_p_up = 0.25\n')
    (tmp_path / ".env").write_text("KALSHI_API_KEY_ID=abc-123\nTOMORROW_API_KEY='tio'\n")
    for var in ("WXBOT_MODE", "KALSHI_API_KEY_ID", "TOMORROW_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    assert s.mode == "demo" and s.stations == ["KNYC"]
    assert s.strategy.min_edge == 0.08 and s.model.nowcast_p_up == 0.25
    assert s.kalshi_key_id == "abc-123" and s.tomorrow_api_key == "tio"
    monkeypatch.setenv("WXBOT_MODE", "PAPER")
    assert load_settings().mode == "paper"


def test_rejects_unknown_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WXBOT_MODE", "yolo")
    with pytest.raises(ValueError):
        load_settings()
