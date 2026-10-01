# Panta Signal Radar

**Honest signals for prediction markets.**
Most dashboards read Panta's trade tape as if every print were a bet. On the live tape, **one market-maker wallet
holds ~81% of all shares** (YES+NO seed pairs and one-sided liquidity across 68 of 87 markets). Signal Radar
separates the market maker from the crowd, scores markets on crowd behaviour only, and shows every claim next to
an honest baseline.

> Powered by **Panta** · built for the Colosseum Crypto World's Fair and the Panta API Sidetrack · MIT licensed.

## What it is
An attention / intelligence layer on top of the [Panta Markets API](https://docs.panta.market/).
It continuously snapshots Panta prediction markets, detects probability moves, volume flow and
trading-activity surges, ranks every open market with an explainable **Attention Score (0–100)**,
and tells you *why* in plain English — e.g. *"YES probability moved +12.4pp (40% → 52%) over 6h ·
Volume +200.00 USDC (+200%) · 37 trades in the last 24h from 21 wallets"*.

## Problem
A prediction-market catalog is a flat list. To find what is actually happening a user has to open
markets one by one and remember yesterday's prices. Repricing, sudden activity and closing-soon
markets are invisible unless you are already watching them.

## Solution
```
raw Panta market data → normalized state → temporal snapshots → change detection
                     → signal scoring → ranked attention → human-readable explanation
```
- **Snapshots**: every refresh stores each market's state in local SQLite, building the history the API does not serve.
- **Change detection**: Δ probability (pp), Δ volume, trade pace vs 24h average.
- **Ranking**: deterministic, bounded components → one Attention Score.
- **Explanation**: every score ships with the facts that produced it. No paid AI, no black box.

## How the Panta API is used
| Endpoint | Used for |
|---|---|
| `GET /markets/` (cursor pagination) | full catalog every refresh |
| `GET /markets/{id}/` | spot YES/NO prices (implied probability) for open markets |
| `GET /markets/{id}/trades/` | trade tape → trades 1h/24h, unique wallets, last trade; stored de-duplicated |
| `GET /categories/` | category list (inspection) |

Auth: `X-Api-Key` header, key read from `.env` (never logged; shown masked as `pk_live_abcd...wxyz`).
Rate-limit friendly: ≥0.55 s between calls (Panta read limit ≈120/min), retries with `Retry-After` on 429/5xx.
Read-only by design — no wallet, no signing, no trading. Details and verified schemas: [`docs/api_notes.md`](docs/api_notes.md).

## Architecture
```
app.py                      Streamlit product UI (Overview · Movers · Activity · New Markets · Market Detail)
src/panta_radar/
  config.py                 .env loading, key masking, paths
  api.py                    read-only Panta client (timeouts, retries, safe errors)
  normalize.py              raw JSON → typed records (missing → None, never invented)
  collect.py                one refresh: catalog → enrich open markets → snapshot
  storage.py                SQLite: runs, markets, snapshots, trades
  signals.py                deterministic signal engine + explanations
  replay.py                 seed detection, trade-implied prices, Resolved Replay
scripts/inspect_api.py      first-contact schema inspection
scripts/fetch_snapshot.py   one snapshot or --loop N minutes
scripts/backfill_trades.py  trade tapes + detail rows for every market (feeds Resolved Replay)
tests/                      normalization, deltas, scoring, missing values, first snapshot, storage
```

## Signal methodology
Each open market gets five components, each bounded to [0, 1] on a fixed, documented scale:

| Component | Weight | Full marks at | Needs |
|---|---|---|---|
| Move — \|Δ YES probability\| over the 24h window | 0.35 | 10 pp | ≥2 snapshots with price |
| Activity — trades in last 24h (log scale) | 0.25 | 10 trades | trade tape |
| Flow — Δ cumulative volume in window (log scale) | 0.20 | +100 USDC | ≥2 snapshots |
| Pace — last-hour trades vs 24h average | 0.10 | 4× | ≥3 trades/24h |
| Timing — opened <72h ago or ends <48h | 0.10 | flag | catalog times |

Scales are calibrated on the live catalog (Oct 2026: open-market volume 0–390 USDC, a few trades per market per day) and live in `ScoreConfig`.
`Attention = round(100 × Σ weight × component)`. Missing data contributes **0** — scores are never
re-inflated when history is short, so a first-run score is honestly low. Labels: **TRENDING** (move and
activity/flow both ≥0.3), otherwise the strongest contributor — **MOVER, ACTIVE, SURGE, NEW, CLOSING SOON**, or **QUIET**.
On the very first snapshot the UI says *"Historical signal data is accumulating."*

## Who's trading — market maker vs crowd
Each wallet on the tape gets an explainable role. A wallet is a **market maker** when ≥30% of its prints are
YES+NO seed pairs across ≥5 markets, or when it holds ≥25% of all shares across ≥10 markets. Its prints are
excluded from crowd activity, crowd flow and the Attention Score.

## Resolved Replay — "did the money see it coming?"
For every resolved market Signal Radar replays the real Panta trade tape up to the close and compares the
directional order flow with the actual outcome.
- **Liquidity seeds are removed**: the same wallet buying YES and NO in near-equal size within seconds is
  market making, not a bet (128 of 452 trades on 2026-10-01).
- **Trade-implied price** = USDC paid ÷ shares received (when the tape carries `amountUsdc`).
- **Flow lean** = share of directional flow that went to YES (USDC-weighted where available, else shares).
- **Honest baseline**: every result is shown next to "always guess the more common outcome".

First run on the live catalog (80 resolved markets, 452 trades, 2026-10-01):

| Reading | Correct | |
|---|---|---|
| Naive tape (seed pairs removed only) | 38 / 61 | 62% — and 12/16 (75%) on markets with ≥5 trades |
| **Crowd only** (market maker removed) | **27 / 44** | **61%** |
| Baseline "always NO" on the same markets | 27 / 44 | 61% |

The apparent edge of the naive reading came from the market maker's systematic NO buying, not from the crowd.
Honest conclusion today: Panta's crowd flow does not yet beat the base rate — Signal Radar says so instead of
selling a fake win rate, and keeps measuring as the dataset grows.

## Screenshots
_To be added from a live run._

## 24/7 open dataset
A GitHub Actions workflow (`.github/workflows/collect.yml`) takes a read-only snapshot every 15 minutes and appends
it as JSONL to the `data` branch — an open, growing history of Panta prices, volumes and trades that the API itself
does not provide. `python scripts/sync_data.py` imports it into the local SQLite.

## Run locally (Windows)
Double-click `setup_and_inspect.bat` once (creates `.venv`, installs deps, inspects the API), then `run_radar.bat`
(takes a snapshot, runs tests, opens the dashboard). Manually:
```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then put your key in .env
.venv\Scripts\python scripts\inspect_api.py
.venv\Scripts\python scripts\fetch_snapshot.py            # or: --loop 15
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m streamlit run app.py
```

## Configuration
| Variable | Default | |
|---|---|---|
| `PANTA_API_KEY` | — | required, `pk_live_…` for real markets |
| `PANTA_API_BASE_URL` | `https://live-api.panta.market/api/v1` | |
| `PANTA_RADAR_DB` | `data/radar.db` (`radar_sandbox.db` for `pk_test_`) | SQLite path |

## Limitations
- `pk_test_` keys return a single sandbox fixture market (verified); real signals need a `pk_live_` key. Sandbox data goes to a separate `radar_sandbox.db`.
- Most live catalog rows have an empty `title` (80 of 87 on 2026-10-01); the UI falls back to `[untitled <category>] abcd…wxyz`.
- Movement/flow signals need ≥2 snapshots; history starts on first run (no backfill — the API exposes none).
- Trade tape is capped at 200 rows per call → 24h counts on very busy markets are lower bounds (shown as "≥").
- No liquidity / order-book depth in the API, so it is not scored.
- Enrichment is capped per refresh (default 40 open markets by volume) to respect rate limits.

## Roadmap
Watchlists & alerts · category-level trends · shareable signal cards · scheduled collection ·
cross-market relationships · traction instrumentation.

## Powered by Panta
Market data from the [Panta Markets API](https://docs.panta.market/). This project is not affiliated with Panta.
