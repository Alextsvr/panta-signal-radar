# Panta Signal Radar

**Honest signals for prediction markets.**
Most dashboards read Panta's trade tape as if every print were a bet. In the Oct 1 2026 capture of every
market's tape, **one dominant wallet exhibiting market-making behaviour holds ~81% of all shares** (YES+NO seed
pairs and one-sided liquidity across 68 of 87 markets). Signal Radar separates that behaviour from the crowd,
scores markets on crowd trades only, and shows every claim next to an honest baseline. All headline numbers are
reproducible from this repository: `python scripts/reproduce_analysis.py`.

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

## Data integrity rules
Real Panta API behaviour (observed Oct 1-6 2026) that the analytics explicitly guard against:
- **Transient degraded rows.** The catalog sometimes returns a market with `yesPrice = null`, `volumeUsdc = "0.00"`,
  `totalVolumeUsdc = null`, `priceSource = null` (710 of 2,140 stored rows), then valid values again. Such rows are
  stored verbatim but are never a valuation: they cannot create probability moves, volume flow or outcomes.
- **Row-consistent selection.** "Latest" and "reference" snapshots are always one physical stored row
  (`snapshots.latest_rows`); fields from different timestamps are never combined.
- **Live-to-live movement only.** Δ probability compares two valid, non-outcome valuations in the same price regime
  (`primary` bonding-curve price vs `secondary` last-trade price). A resolution to 0/1 is an *outcome*, not a ±50pp
  move; a primary→secondary switch is not counted as movement.
- **Monotonic volume.** Cumulative volume never falls; a lower newer value is flagged as a data issue, never negative flow.
- **Raw vs analytical state.** 84 of 91 markets flipped `resolved → secondary_active → resolved` between runs. The raw
  API state is kept as returned; the analytical state is sticky once a confirmed outcome row
  (`priceSource = resolved_outcome`) has been seen, so flaps cannot reopen a market or create live signals.
- **One row per trade.** The append-only trade log repeats a trade in every run that saw it (452 rows = 48 trades on
  Oct 1-6). All analytics de-duplicate by `trade_id` first; new collector runs only append unseen trades.

## Who's trading — market-making behaviour vs crowd
Each wallet on the tape gets an explainable *behaviour* label (Panta has not confirmed who owns any wallet). A wallet
shows **market-making behaviour** when ≥30% of its prints are YES+NO seed pairs across ≥5 markets, or when it holds
≥25% of all shares across ≥10 markets.
- **Live radar** uses the current/global wallet profile (all trades known now) to filter today's signals.
- **Resolved Replay** uses an **as-of-cutoff** profile: for each market, roles and seed pairs are computed only from
  trades at or before that market's close, so no future behaviour leaks into a historical verdict.

## Resolved Replay — "did the money see it coming?"
For every resolved market Signal Radar replays the real Panta trade tape up to the close and compares the
directional order flow with the actual outcome.
- **Liquidity seeds are removed**: the same wallet buying YES and NO in near-equal size within seconds is
  liquidity provision, not a bet (128 of 452 trades in the Oct 1 capture).
- **Trade-implied price** = USDC paid ÷ shares received (when the tape carries `amountUsdc`).
- **Flow lean** = share of directional flow that went to YES (USDC-weighted where available, else shares).
- **Honest baseline**: every result is shown next to "always guess the more common outcome".

Reproducible result (data branch commit `432a14a` = snapshots through 2026-10-06 02:18 UTC, plus the committed
Oct 1 backfill; 470 unique trades, 88 resolved markets, causal as-of classification):
`python scripts/reproduce_analysis.py --ref 432a14a0c6a88fb90a70594ade9ad83b7852345f`

| Reading | Correct | |
|---|---|---|
| Naive tape (seed pairs removed only) | 40 / 64 | 62.5% (baseline "always NO" on the same markets: 40 / 64) |
| **Crowd only** (as-of market-making wallets removed) | **29 / 47** | **61.7%** |
| Baseline "always NO" on the crowd-called markets | 28 / 47 | 59.6% |

Honest conclusion: neither reading beats the base rate in a meaningful way (one market of difference on 47).
A fresh capture of every tape on 2026-10-06 08:22 UTC returned all 452 Oct 1 trades unchanged plus 21 newer ones;
with it the numbers become naive 41/65, crowd 30/48, baseline 29/48 — the same conclusion. Numbers computed on the
latest data branch will keep moving as the collector adds markets.
Signal Radar reports that instead of selling a win rate, and keeps measuring as the dataset grows. An earlier
local-only figure (12/16 on markets with ≥5 trades) is not part of the reproducible set and is no longer claimed.

## Reproduce the numbers
```bash
git fetch origin data
python scripts/reproduce_analysis.py        # or: --data-dir <checkout of the data branch>
```
Inputs, both public: the `data` branch (collector JSONL) and `datasets/panta-trade-backfill-2026-10-01/` — the
verbatim responses of `GET /markets/{id}/trades/?limit=200` for all 87 catalog markets captured on 2026-10-01
(sha256 in `manifest.json`; no tape hit the 200-row limit). That capture is the only source of pre-Oct-1 trades:
the collector originally fetched tapes for open markets only. Since this release it fetches every market's tape
on each run (`collect_ci.py`, `all_tapes=True`), and `python scripts/backfill_trades.py --export auto` writes a new
verifiable capture. No API key or local database is needed to reproduce.

## Screenshots
_To be added from a live run._

## 24/7 open dataset
A GitHub Actions workflow (`.github/workflows/collect.yml`) requests a read-only snapshot every 15 minutes and appends
it as JSONL to the `data` branch — an open, growing history of Panta prices, volumes and trades that the API itself
does not provide. **Scheduled workflows are best-effort: GitHub may delay or skip them.** Observed Oct 1-6: 24
successful runs, median gap 4.7 h (min 2.7 h, max 8.9 h), no run with errors. The dashboard shows the actual
coverage (runs, median and largest gap), and movements are measured only between snapshots that exist.
`python scripts/sync_data.py` imports the branch into the local SQLite.

## Streamlit Community Cloud (public read-only deployment)
```
GitHub Actions + PANTA_API_KEY (repo secret)  ->  public `data` branch (JSONL)
        ->  Streamlit Community Cloud app  ->  ephemeral SQLite rebuilt from the data branch
```
GitHub Actions owns all Panta API collection. The public app only consumes the public `data` branch plus the
committed `datasets/panta-trade-backfill-*`; it never calls the Panta API and never holds a key.

| Setting | Value |
|---|---|
| Repository | `Alextsvr/panta-signal-radar` |
| Branch | `main` |
| Main file | `app.py` |
| Python | 3.12 (Advanced settings) |
| Secrets | `PANTA_RADAR_PUBLIC_MODE = "1"` |

**Do NOT put `PANTA_API_KEY` into Streamlit Community Cloud.** In public mode the key is ignored even if present,
the "Refresh from Panta" control is removed and `PantaClient` refuses to start.

How public mode works (`src/panta_radar/publicsync.py`):
- On boot (no `data/radar.db` on the ephemeral filesystem) the app imports the `data` branch and the committed
  backfill into a new SQLite — a reboot recovers automatically.
- Re-sync at most every 10 minutes per server process (`git fetch origin data`, falling back to HTTPS from
  GitHub), importing only missing runs and trades; reruns by visitors never fetch.
- If a sync fails, the last synchronized DB keeps being served with a warning; if no DB exists yet, the app shows
  an error instead of crashing and retries after a minute.
- Local check: `PANTA_RADAR_PUBLIC_MODE=1 streamlit run app.py` (optional `PANTA_RADAR_DB=<tmp path>` to keep your
  local DB untouched, `PANTA_RADAR_DATA_DIR=<data-branch checkout>` to work offline).

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
| `PANTA_RADAR_PUBLIC_MODE` | off | `1` = read-only public deployment (no API key, no API calls) |
| `PANTA_RADAR_DATA_DIR` | — | public mode: read the data branch from a local directory instead of GitHub |

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
