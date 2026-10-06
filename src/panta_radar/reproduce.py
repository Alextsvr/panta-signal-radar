"""Headline analysis rebuilt ONLY from repository-accessible data:

  * the public `data` branch (JSONL written by the GitHub Actions collector), and
  * static, verbatim API captures committed under `datasets/` (e.g. the Oct 1 trade backfill).

No local database is read. Everything is de-duplicated by `trade_id` before analysis.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from .datastore import import_lines
from .normalize import normalize_trade
from .replay import MM_ROLE, build_replay, prepare_trades, replay_summary, wallet_profiles
from .signals import build_radar
from .snapshots import coverage_stats
from .storage import connect, insert_trades, load_all_trades, load_markets, load_runs, load_snapshots

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "datasets"


def backfill_trades(dataset_dir: Path) -> list[dict]:
    """Normalize a verbatim `{marketId: [trade rows]}` capture with the production normalizer."""
    raw = json.loads((Path(dataset_dir) / "trades_raw.json").read_text(encoding="utf-8"))
    out = []
    for mid, rows in raw.items():
        out += [t for t in (normalize_trade(r, mid) for r in rows) if t]
    return out


def run_analysis(datastore: dict[str, list[str]], backfill_dirs: list[Path] | None = None) -> dict:
    backfill_dirs = backfill_dirs or []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "repro.db"
        import_lines(db, datastore)
        public_rows = len([l for l in datastore.get("trades", []) if l.strip()])
        public_unique = len({json.loads(l)["trade_id"] for l in datastore.get("trades", []) if l.strip()})
        bf = [t for d in backfill_dirs for t in backfill_trades(d)]
        with connect(db) as con:
            insert_trades(con, None, bf)
        markets, snaps, runs, trades = load_markets(db), load_snapshots(db), load_runs(db), load_all_trades(db)
    now = pd.to_datetime(snaps["snapshot_ts"], utc=True).max() if not snaps.empty else None
    radar = build_radar(markets, snaps, now=now) if not snaps.empty else pd.DataFrame()
    prepared = prepare_trades(trades)
    prof = wallet_profiles(prepared)
    top = prof.iloc[0] if len(prof) else None
    rep = build_replay(radar, trades) if not radar.empty else pd.DataFrame()
    sm = replay_summary(rep)
    cov = coverage_stats(runs)
    states = radar["analytical_state"].value_counts().to_dict() if not radar.empty else {}
    return {
        "public_trade_rows": public_rows, "public_unique_trades": public_unique,
        "backfill_unique_trades": len({t["trade_id"] for t in bf}),
        "unique_trades": int(len(prepared)), "wallets": int(len(prof)),
        "mm_wallets": int((prof["role"] == MM_ROLE).sum()) if len(prof) else 0,
        "dominant_wallet": None if top is None else {
            "wallet": top["wallet"], "share_of_tape": float(top["share_of_tape"]), "markets": int(top["markets"]),
            "trades": int(top["trades"]), "seed_ratio": float(top["seed_ratio"]), "role": top["role"]},
        "seed_pair_prints": int(prepared["is_seed"].sum()) if len(prepared) else 0,
        "markets": int(len(radar)), "analytical_states": states,
        "flapping_markets": int((radar["state_flaps"] > 0).sum()) if not radar.empty else 0,
        "invalid_valuation_rows": int(radar["invalid_rows"].sum()) if not radar.empty else 0,
        "replay": sm, "coverage": cov,
    }


def format_report(r: dict, title: str) -> str:
    sm, cov, top = r["replay"], r["coverage"], r["dominant_wallet"]
    pct = lambda a, b: f"{a}/{b} ({a / b:.0%})" if b else f"{a}/{b}"
    lines = [f"== {title}",
             f"Trade log rows (public data branch): {r['public_trade_rows']}  -> unique: {r['public_unique_trades']}",
             f"Unique trades from datasets/ backfill: {r['backfill_unique_trades']}",
             f"Unique trades analysed (by trade_id): {r['unique_trades']}",
             f"Wallets: {r['wallets']}",
             f"Wallets with market-making behaviour (current/global profile): {r['mm_wallets']}"]
    if top:
        lines.append(f"Dominant wallet {top['wallet'][:6]}..{top['wallet'][-4:]}: {top['share_of_tape']:.0%} of shares, "
                     f"{top['markets']} markets, {top['trades']} prints, {top['seed_ratio']:.0%} seed pairs, role={top['role']}")
    lines += [f"Seed-pair prints: {r['seed_pair_prints']}",
              f"Markets: {r['markets']}  analytical states: {r['analytical_states']}  "
              f"flapping: {r['flapping_markets']}  invalid valuation rows: {r['invalid_valuation_rows']}",
              f"Resolved markets tested: {sm['with_outcome']}",
              f"Naive replay (seed pairs removed only): {pct(sm['naive_correct'], sm['naive_with_flow'])}",
              f"Crowd-only replay (as-of market-making wallets removed): {pct(sm['flow_correct'], sm['with_flow'])}",
              f"Baseline 'always {sm['baseline_side']}' on the crowd-called markets: {pct(sm['baseline_correct'], sm['with_flow'])}"]
    if cov["runs"]:
        lines.append(f"Collection runs: {cov['runs']}  {cov['first']:%Y-%m-%d %H:%M} -> {cov['last']:%Y-%m-%d %H:%M} UTC  "
                     f"median gap {cov['median_gap_h']:.2f}h  min {cov['min_gap_h']:.2f}h  max {cov['max_gap_h']:.2f}h")
    return "\n".join(lines)
