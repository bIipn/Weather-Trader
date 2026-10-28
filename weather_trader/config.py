"""Settings: defaults ← config.toml ← environment variables (.env supported)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .model.forecast import ModelSettings


@dataclass
class StrategyConfig:
    # Forecast trading: bet where the model disagrees with the market. Off by default — in
    # backtests the market's own prices were better calibrated than the model (see README).
    forecast_trading: bool = False
    model_weight: float = 0.5          # trade on w·model + (1-w)·market mid, not the raw model
    # Dead-bracket sniping: sell any bid left on a bracket the observations already ruled out.
    sniper: bool = True
    sniper_min_bid: float = 0.02
    sniper_max_contracts: int = 200
    min_edge: float = 0.05             # taker: min expected profit per contract after fees ($)
    min_edge_maker: float = 0.03       # maker quotes: min expected profit per contract ($)
    execution: str = "hybrid"          # taker | maker | hybrid
    kelly_fraction: float = 0.25       # quarter Kelly
    max_stake_per_market: float = 25.0  # $ at risk in any single bracket
    max_event_loss: float = 60.0       # worst-case $ loss across all brackets of one city-day
    max_total_exposure: float = 300.0  # $ at risk across everything
    daily_loss_limit: float = 100.0    # stop opening positions after losing this much today
    min_price: float = 0.04            # never buy contracts cheaper than this...
    max_price: float = 0.96            # ...or dearer than this
    max_model_market_gap: float = 0.30  # bigger disagreement usually means bad data, not alpha
    maker_ttl_minutes: int = 20
    min_minutes_to_close: float = 15.0
    trade_days: list[int] = field(default_factory=lambda: [0, 1])  # 0 = today, 1 = tomorrow
    require_calibration: bool = False
    fee_multiplier: float = 1.0        # taker multiplier for weather series (fee schedule)
    maker_fee_multiplier: float = 0.0  # weather series charge no maker fee


@dataclass
class Settings:
    mode: str = "paper"  # paper | demo | live
    series: list[str] = field(default_factory=list)  # empty = auto-discover all daily temp series
    stations: list[str] = field(default_factory=list)  # optional allow-list, e.g. ["KNYC", "KMDW"]
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key: str = ""
    tomorrow_api_key: str = ""
    open_meteo_api_key: str = ""
    contact: str = "weather-trader"
    data_dir: str = "data"
    calibration_dir: str = "calibration"
    paper_bankroll: float = 1000.0
    interval_seconds: int = 300
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    model: ModelSettings = field(default_factory=ModelSettings)

    @property
    def user_agent(self) -> str:
        return f"weather-trader/0.1 ({self.contact})"

    @property
    def has_kalshi_keys(self) -> bool:
        return bool(self.kalshi_key_id and (self.kalshi_private_key or self.kalshi_private_key_path))


ENV_MAP = {
    "WXBOT_MODE": "mode",
    "KALSHI_API_KEY_ID": "kalshi_key_id",
    "KALSHI_PRIVATE_KEY_PATH": "kalshi_private_key_path",
    "KALSHI_PRIVATE_KEY": "kalshi_private_key",
    "TOMORROW_API_KEY": "tomorrow_api_key",
    "OPEN_METEO_API_KEY": "open_meteo_api_key",
    "WXBOT_CONTACT": "contact",
    "WXBOT_DATA_DIR": "data_dir",
}


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'").replace("\\n", "\n")
        os.environ.setdefault(key.strip(), value)


def _apply(obj, data: dict) -> None:
    names = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key in names and not isinstance(value, dict):
            setattr(obj, key, value)


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv()
    s = Settings()
    path = Path(config_path or os.environ.get("WXBOT_CONFIG", "config.toml"))
    if path.exists():
        data = tomllib.loads(path.read_text())
        _apply(s, data.get("bot", {}))
        _apply(s.strategy, data.get("strategy", {}))
        _apply(s.model, data.get("model", {}))
    for env, attr in ENV_MAP.items():
        if os.environ.get(env):
            setattr(s, attr, os.environ[env])
    s.mode = s.mode.lower()
    if s.mode not in ("paper", "demo", "live"):
        raise ValueError(f"mode must be paper, demo or live (got {s.mode!r})")
    return s
