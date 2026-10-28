<div align="center">

# ⛅ Weather Trader

**A research-grade trading bot for Kalshi's daily temperature markets.**

Blends 13 weather models with live airport observations into a probability for every
bracket, compares it with the market, and trades only where the edge survives fees.
Ships with a backtester that replays real Kalshi prices, so you can see whether that
edge actually exists before you risk anything.

[![CI](https://github.com/bIipn/Weather-Trader/actions/workflows/ci.yml/badge.svg)](https://github.com/bIipn/Weather-Trader/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![mode](https://img.shields.io/badge/default%20mode-paper-7dd3fc)

</div>

---

> **Is it profitable today? No.** In a 45-day replay against real Kalshi prices
> (Sep 12 – Oct 26 2026, 7 cities, 9,450 bracket decisions), the market's own prices were
> better calibrated than the model at every decision time, and no forecast-based rule
> made money after fees. The one structural edge found, selling brackets that observations
> have already ruled out, disappears within about 2 minutes. That is faster than free
> public data feeds deliver the observation. Details are under [Results](#results), and
> [Paths to a real edge](#paths-to-a-real-edge) covers what could change that. The bot
> therefore ships in **paper mode**, with forecast trading **off**.

## Why Kalshi

| | Kalshi | Polymarket |
|---|---|---|
| Daily high/low temperature markets | ✅ 20+ US cities, highs and lows, every day | a handful of cities |
| Legal for US residents | ✅ CFTC-regulated, USD | ❌ international site geoblocks the US |
| Official API + **free demo exchange** | ✅ | crypto wallet + on-chain signing |
| Fee on resting (maker) orders in weather | **$0** | $0 |
| Settlement | official climate report via The Weather Company | Weather Underground page |

## How it works

```
Kalshi markets ──► station, day and brackets parsed from each contract's rules
                                   │
13 forecast sources: NBM, GFS/HRRR, ECMWF, ICON, GEM, UKMO, JMA, Météo-France,
5 ensembles (GEFS, ECMWF ENS, AIFS, ICON-EPS, GEM), Tomorrow.io (optional)
          ──► daily max/min over the climate day (midnight-to-midnight local *standard* time)
                                   │
per-station calibration (a year of archived forecasts vs official climate reports):
bias by model and lead time · skill weights · error spread · peak-gap distribution
                                   │
NWS observations (METAR + 6h/24h groups) ──► same-day nowcast: what has been observed is a
                                              floor, only the rest of the day is forecast
                                   │
fat-tailed probability for every bracket ──► edge after Kalshi fees ──► Kelly sizing + caps
                                   │
                    paper simulator  ·  Kalshi demo  ·  Kalshi live
```

Things that matter in these markets, all handled:

- **Settlement**: since Oct 22 2026 Kalshi settles on The Weather Company's relay of the
  station's climate report (`CLINYC` = Central Park). The station is read from the contract rules.
- **Climate day**: midnight to midnight *local standard time*. In summer, NYC's day runs 1 AM–1 AM EDT.
- **Peaks between reports**: the official high came in above every hourly report on
  **61% of days** (median over 20 stations). Each station's calibration stores how often, and by how much.
- **Station biases**: models run warm at Central Park (GFS +1.0°F, NBM +0.4°F on the day-ahead high).
  Calibration fits this per station, per model and per lead time.
- **Fat tails**: errors are a Normal mixture, so 2¢ "impossible" brackets are not priced at zero.
- **Fees**: taker `ceil(0.07·C·P·(1−P))`, maker $0 on weather series. Resting quotes are only
  placed when they become the best bid.

## Results

Replayed with `wxbot backtest`. Calibration was fitted only on the year before the window, so
nothing leaks from the future. Decisions used the bid/ask Kalshi actually showed at that minute
or hour.

| Strategy | Bets | Result after fees | Model vs market accuracy (Brier, lower is better) |
|---|---:|---:|---|
| Model vs market, 10 PM the night before | 695 | **−16.5%** | 0.135 vs **0.111** |
| Same-day nowcast at 11 AM | 436 | **−7.7%** | 0.160 vs **0.124** |
| Same-day nowcast at 2 PM | 194 | **−14.7%** | 0.167 vs **0.108** |
| Blend 10–70% model into the market price | — | none profitable | every blend was worse than the market alone |
| Always buy the favorite bracket the night before | 314 | +6.5% | not statistically significant (~1.2σ) |
| Buy every bracket when they sum to < $1 | 0 | never happens | asks add up to $1.067 on average |
| **Dead-bracket sniping** (sell brackets a report has ruled out) | 647 dead brackets | **0 settled YES** | see below |

**Dead-bracket sniping.** Once a station reports 64°F, "63° or below" can no longer win. In the
replay, 62 such brackets still had a bid when the report came in.

| Time after the report | Bids still standing | Average bid |
|---|---:|---:|
| 1 minute | 42 | 12¢ |
| 2 minutes | 21 | 7¢ |
| 3 minutes | 2 | 2¢ |

The free feeds (aviationweather.gov, api.weather.gov) receive a METAR about 3 minutes after the
observation, so faster traders get there first. The bot still runs the sniper because it is close
to riskless. It waits for a 1°F margin, since the official value can very rarely land a degree
inside a report. Don't expect it to fire often on free data.

## Paths to a real edge

1. **A faster observation feed.** 5-minute ASOS data from a low-latency provider (for example
   Synoptic Data's push API) would put the sniper inside the 1–2 minute window where stale bids live.
   Add it as an observation source; the sniper logic and the replay are already built.
2. **Liquidity provision.** Maker fees are $0 and spreads are 2–6¢ on mid-priced brackets. Quoting
   around the market mid can't be replayed honestly from candles, so paper-trade it first.
3. **Better day-ahead information**, such as NWS point forecasts or NBM station text guidance.
   Re-run `wxbot backtest` after adding a source. The model has to beat Brier 0.111 before
   forecast trading deserves real money.

## Quick start

```bash
git clone https://github.com/bIipn/Weather-Trader && cd Weather-Trader
python -m venv .venv && source .venv/bin/activate
pip install -e .

wxbot scan              # model vs market for every open temperature market (never trades)
wxbot run --once        # one paper-trading cycle
wxbot run               # paper trade every 5 minutes (Ctrl-C to stop)
wxbot status            # balance, positions, recent orders
wxbot backtest          # replay the last 45 days against real Kalshi prices
wxbot backtest --strategy sniper
```

`scan` shows one panel per city-day:

```
╭─  New York City (Central Park)  ·  KNYC  ·  High  ·  Wed Oct 28  ─────────────────────────────╮
│  Bracket    Bid   Ask   Market   Model                  Edge   Signal                          │
│  ≤59°         —    1¢        —     <1%                     —   ruled out by observations       │
│  60–61°     65¢   66¢      66%     68%   ████████▏        +3                                   │
│  62–63°     33¢   37¢      35%     31%   ███▊             -4                                   │
│ model 61.3°F ± 0.9   lead 1h   6 sources   market ≈ 61.2°F                                     │
│ observed max so far 61°F (unconfirmed — official often lands 1° beyond)   now 61°F at 18:51    │
╰────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## Going live (demo first)

1. **API keys**: get demo keys from [demo.kalshi.co](https://demo.kalshi.co). For production keys,
   go to kalshi.com → Account & security → API keys.
2. `cp .env.example .env`, then set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
3. **Demo**: `wxbot run --mode demo`. Real orders go to the demo exchange with fake money;
   prices still come from the live market.
4. **Live**: `wxbot run --mode live --confirm-live`. Start with small caps in `config.toml`.

> Run the bot on its own Kalshi account or subaccount. Its exposure checks count every
> position on the account.

## Commands

| Command | What it does |
|---|---|
| `wxbot scan [--station KNYC] [--days 0 1] [--json]` | Price every open bracket. Never trades. |
| `wxbot run [--mode paper\|demo\|live] [--once]` | Trading loop, or a single cycle for cron. |
| `wxbot status` | Balance, positions with marks, recent orders. |
| `wxbot calibrate [--station …] [--kind high\|low\|both]` | Re-fit station calibration from a year of data. |
| `wxbot backtest [--strategy forecast\|sniper] [--days 45] [--json]` | Replay against real Kalshi prices. |
| `wxbot markets` | Open temperature markets and the settlement station of each. |

## Configuration

`cp config.example.toml config.toml`. Every setting is documented there. The important ones:

| Setting | Default | Meaning |
|---|---|---|
| `forecast_trading` | `false` | Trade model-vs-market disagreements (backtests say: don't, yet) |
| `model_weight` | `0.5` | Trade on `w·model + (1−w)·market`, not the raw model |
| `sniper` | `true` | Sell bids on brackets observations have ruled out |
| `min_edge` | `0.05` | Taker trades need ≥ 5¢ expected profit per contract after fees |
| `kelly_fraction` | `0.25` | Quarter-Kelly sizing |
| `max_stake_per_market` / `max_event_loss` / `max_total_exposure` | `$25` / `$60` / `$300` | Risk caps |
| `daily_loss_limit` | `$100` | Stop opening positions for the day |

## Running it 24/7

- **Your machine or a small VPS**: `wxbot run` loops forever, or run `wxbot run --once` from cron.
- **GitHub Actions**: `.github/workflows/bot.yml` runs one cycle per invocation. Add the
  `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY` secrets and uncomment its schedule.
- **Research**: `.github/workflows/research.yml` recalibrates 20 stations, scans live markets and
  re-runs both backtests every Monday, then publishes the output to the `research-results` branch.

## About the connectors

- **Tomorrow.io weather connector**: a standalone bot can't call a chat connector, so the bot uses
  the same provider's REST API. Set `TOMORROW_API_KEY` and Tomorrow.io joins the blend.
- **TextToQuant connector**: it backtests crypto, stock and FX strategies on price candles. It has
  no prediction-market or weather data, and the free plan is crypto-only. It can't evaluate this
  strategy, so the bot has its own backtester that replays Kalshi's real price history.

## Caveats

- Nothing here is financial advice. Prediction markets can and do take money from bots.
- Open-Meteo's free API is for non-commercial use. Set `OPEN_METEO_API_KEY` if you trade real money.
- Kalshi changes rules and APIs (V2 orders in 2026; TWC settlement from Oct 22 2026). The bot reads
  the station and strike type from each contract, but check the rules if something looks off.

## Project layout

```
weather_trader/
  kalshi/        API client + request signing (RSA-PSS / Ed25519)
  weather/       Open-Meteo, NWS observations, Tomorrow.io, IEM climate + METAR archives
  model/         distribution, forecaster (blend + nowcast), calibration
  markets.py     market discovery and bracket parsing
  strategy.py    fees, edge, Kelly sizing, risk limits
  sniper.py      dead-bracket detection and its 1-minute replay
  broker.py      paper simulator + Kalshi broker
  engine.py      one trading cycle
  backtest.py    walk-forward replay on real Kalshi prices
  ui.py · cli.py terminal dashboard and commands
calibration/     fitted per-station parameters (20 stations × high/low)
tests/           55 tests, built on real captured API payloads
```
