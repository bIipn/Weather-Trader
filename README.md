<div align="center">

# ⛅ Weather Trader

**A calibrated weather-forecast bot for Kalshi's daily temperature markets.**

Blends ~13 weather models with live airport observations into a probability for every
bracket, compares it with the market, and trades only when the edge clears fees.

[![CI](https://github.com/bIipn/Weather-Trader/actions/workflows/ci.yml/badge.svg)](https://github.com/bIipn/Weather-Trader/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![mode](https://img.shields.io/badge/default%20mode-paper-7dd3fc)

</div>

---

## Why Kalshi

| | Kalshi | Polymarket |
|---|---|---|
| Daily high/low temperature markets | ✅ 20+ US cities and ~20 international ones, every day | ✅ a handful of cities |
| Legal for US residents | ✅ CFTC-regulated, USD | ❌ international exchange geoblocks the US |
| Official trading API + **free demo exchange** | ✅ | ⚠️ crypto wallet + on-chain signing |
| Fees on resting (maker) orders in weather | **$0** | $0 |
| Settlement source | Official climate report via The Weather Company | Weather Underground page |

Kalshi is the easiest place to run this end to end: you can test against the demo exchange,
fund it in dollars, and settle on official climate data.

## How it works

```
Kalshi markets ──► which station? which day? which brackets?   (parsed from the contract rules)
                                   │
Open-Meteo models (NBM, GFS/HRRR, ECMWF, ICON, GEM, UKMO, JMA, Météo-France)
Open-Meteo ensembles (GEFS, ECMWF ENS, AIFS, ICON-EPS, GEM)  ──►  daily max/min over the
Tomorrow.io (optional)                                           *climate day* (local standard time)
                                   │
              per-station calibration: bias by model & lead time, skill weights, error spread
                                   │
NWS observations (METAR + 6h/24h groups) ──►  intraday nowcast: the observed extreme becomes a
                                              floor; only the rest of the day is forecast
                                   │
                 fat-tailed probability for every bracket  ──►  edge after Kalshi fees
                                   │
          quarter-Kelly sizing · per-bracket, per-city-day & total caps · daily loss stop
                                   │
                     paper simulator  ·  Kalshi demo  ·  Kalshi live
```

The details that matter in these markets, all handled:

- **Settlement**: since Oct 22 2026 Kalshi settles on The Weather Company's relay of the station's official
  climate report (e.g. `CLINYC` = Central Park). The bot reads the station from each contract's rules.
- **Climate day**: the day runs from midnight to midnight *local standard time*. In summer the NYC
  day is 1 AM to 1 AM EDT, so a warm 12:30 AM can be the day's high.
- **Between-report peaks**: hourly reports miss the true peak. Precise 6-hour and 24-hour max/min groups
  in the METAR remarks confirm it; until then the bot keeps a chance that the official value ends up 1° higher.
- **Fat tails**: errors are modelled as a Normal mixture, so 2¢ "impossible" brackets are priced correctly.
- **Fees**: taker fee `ceil(0.07·C·P·(1−P))`, maker fee $0 on weather series. Resting quotes are only placed
  when they become the best bid. A bid behind the queue only fills when someone knows more.

## Quick start

```bash
git clone https://github.com/bIipn/Weather-Trader && cd Weather-Trader
python -m venv .venv && source .venv/bin/activate
pip install -e .

wxbot scan                 # model vs. market for every open temperature market (never trades)
wxbot run --once           # one paper-trading cycle
wxbot run                  # paper trade every 5 minutes (Ctrl-C to stop)
wxbot status               # balance, positions, recent orders
```

`scan` shows one panel per city-day:

```
╭─  New York City (Central Park)  ·  KNYC  ·  High  ·  Thu Oct 29  ──────────────────────────────────╮
│  Bracket    Bid   Ask   Market   Model                  Edge   Signal                               │
│  ≤66°       32¢   33¢      32%     19%   ██▎             -13   BUY NO   68¢ ×22   take              │
│  67–68°     56¢   57¢      56%     39%   ████▋           -17   BUY NO   44¢ ×54   take              │
│  69–70°     11¢   12¢      12%     31%   ███▋            +20   BUY YES  12¢ ×22   take              │
│  71–72°      1¢    2¢       2%      8%   ▉                +6                                        │
│ model 68.1°F ± 2.2   lead 24h   6 sources   market ≈ 67.0°F                                         │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## Going live (demo first)

1. **Create API keys.** Use [demo.kalshi.co](https://demo.kalshi.co) for the demo exchange and
   kalshi.com → Account & security → API keys for production. Save the private key file.
2. `cp .env.example .env` and fill in `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
3. **Demo:** `wxbot run --mode demo`. This places real orders on Kalshi's demo exchange with fake money.
   Prices still come from the real market.
4. **Live:** `wxbot run --mode live --confirm-live`. Start with small caps in `config.toml`.

> Use a dedicated Kalshi subaccount or account for the bot. Its exposure checks count every
> position on the account.

## Commands

| Command | What it does |
|---|---|
| `wxbot scan [--station KNYC] [--days 0 1] [--json]` | Price every open bracket; show signals. Never trades. |
| `wxbot run [--mode paper\|demo\|live] [--once]` | Trading loop (or a single cycle for cron). |
| `wxbot status` | Balance, positions with marks, recent orders. |
| `wxbot calibrate [--station …] [--kind high\|low\|both]` | Fit per-station bias, weights and error spread from a year of archived forecasts vs. official climate reports. |
| `wxbot backtest [--series …] [--days 45]` | Replay the strategy against real historical Kalshi prices. |
| `wxbot markets` | List open temperature markets and the settlement station of each. |

## Configuration

`cp config.example.toml config.toml`. Every knob is documented there. The most important ones:

| Setting | Default | Meaning |
|---|---|---|
| `min_edge` | `0.05` | Taker trades need ≥ 5¢ expected profit per contract **after fees** |
| `kelly_fraction` | `0.25` | Quarter-Kelly position sizing |
| `max_stake_per_market` | `$25` | Cap per bracket |
| `max_event_loss` | `$60` | Worst-case loss across all brackets of one city-day |
| `max_total_exposure` | `$300` | Cap across everything |
| `daily_loss_limit` | `$100` | Stop opening positions after this much is lost in a day |
| `require_calibration` | `false` | Only trade stations that have a fitted calibration file |

## Running it 24/7

- **Your machine or a $5 VPS:** `wxbot run` (loops every `interval_seconds`), or cron `wxbot run --once`.
- **GitHub Actions:** `.github/workflows/bot.yml` runs one cycle per invocation. Add the
  `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY` secrets, then uncomment its `schedule:` to run it every 15
  minutes. Actions are free on public repos.
- **Research:** `.github/workflows/research.yml` runs a live scan, recalibrates 20 stations and backtests
  weekly.

## About the connectors

- **Tomorrow.io weather connector:** a standalone bot can't call a chat connector, so the bot uses the same
  provider's REST API instead. Set `TOMORROW_API_KEY` and Tomorrow.io joins the model blend.
- **TextToQuant connector:** it backtests crypto, stock and FX strategies on price candles. It has no
  prediction-market or weather data, and on the free plan it is crypto-only. It can't evaluate this
  strategy, so the bot has its own backtester that replays real Kalshi order-book history.

## Honest caveats

- No bot is guaranteed to make money. These markets have experienced traders and other bots in them.
  The edge comes from calibration, fast observations and discipline. Check it with `wxbot backtest`
  and paper trading before risking money.
- When the model and the market disagree by a lot, the model is often the one that's wrong. The bot
  skips gaps over 30 points and every disagreement is visible on the board.
- Open-Meteo's free API is for non-commercial use. Set `OPEN_METEO_API_KEY` for real-money trading.
- Kalshi changes rules and APIs (V2 orders arrived in 2026; TWC settlement on Oct 22 2026). If
  something looks off, check the market rules on Kalshi first.

## Project layout

```
weather_trader/
  kalshi/        API client + request signing
  weather/       Open-Meteo, NWS observations, Tomorrow.io, climate-report archive
  model/         distribution, forecaster (blend + nowcast), calibration
  markets.py     market discovery & bracket parsing
  strategy.py    fees, edge, Kelly sizing, risk limits
  broker.py      paper simulator + Kalshi broker
  engine.py      one trading cycle
  backtest.py    walk-forward replay on real Kalshi prices
  ui.py · cli.py terminal dashboard and commands
calibration/     fitted per-station parameters
tests/           unit tests on real captured API payloads
```
