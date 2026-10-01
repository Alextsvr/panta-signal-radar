"""Append-only JSONL dataset of Panta snapshots — the bridge between the 24/7 GitHub Actions
collector and any local SQLite.

Layout (on the `data` branch):
    runs/YYYY-MM-DD.jsonl  snapshots/YYYY-MM-DD.jsonl  trades/YYYY-MM-DD.jsonl  markets/YYYY-MM-DD.jsonl
Text files diff well in git, are human-readable, and form an open dataset of Panta market history.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .storage import MARKET_COLS, SNAP_COLS, TRADE_COLS, connect

TABLES = ("runs", "snapshots", "trades", "markets")


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    cur = con.execute(f"SELECT * FROM {table}")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def export_db(db_path: Path, out_dir: Path) -> dict[str, int]:
    """Append every row of a (single-run) SQLite DB to dated JSONL files."""
    counts = {}
    with connect(db_path) as con:
        runs = _rows(con, "runs")
        day = (runs[-1]["started_at"] if runs else "unknown")[:10]
        for table in TABLES:
            rows = _rows(con, table)
            counts[table] = len(rows)
            if not rows:
                continue
            p = Path(out_dir) / table / f"{day}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return counts


def import_lines(db_path, files: dict[str, Iterable[str]]) -> dict[str, int]:
    """Idempotent import. `files` maps table name -> iterable of JSONL lines.
    Runs are matched by started_at; their snapshots are re-keyed to the local run_id."""
    data = {t: [json.loads(l) for l in files.get(t, []) if l.strip()] for t in TABLES}
    added = defaultdict(int)
    with connect(db_path) as con:
        # CI run_ids restart at 1 in every throwaway DB -> key runs by started_at (= snapshot_ts)
        ts_to_run: dict[str, int | None] = {}
        known = {r[0]: r[1] for r in con.execute("SELECT started_at, run_id FROM runs")}
        for r in data["runs"]:
            if r["started_at"] in known:
                ts_to_run[r["started_at"]] = None  # already imported
                continue
            cur = con.execute("INSERT INTO runs(started_at, finished_at, markets_seen, markets_enriched, api_calls, "
                              "errors) VALUES (?,?,?,?,?,?)",
                              (r["started_at"], r.get("finished_at"), r.get("markets_seen"),
                               r.get("markets_enriched"), r.get("api_calls"), r.get("errors")))
            ts_to_run[r["started_at"]] = cur.lastrowid
            known[r["started_at"]] = cur.lastrowid
            added["runs"] += 1
        cols = ["snapshot_ts", "market_id", "enriched"] + SNAP_COLS
        for s in data["snapshots"]:
            rid = ts_to_run.get(s["snapshot_ts"])
            if rid is None:
                continue
            con.execute(f"INSERT OR IGNORE INTO snapshots(run_id,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))})",
                        [rid] + [s.get(c) for c in cols])
            added["snapshots"] += 1
        for t in data["trades"]:
            con.execute(f"INSERT OR IGNORE INTO trades({','.join(TRADE_COLS)}) VALUES ({','.join('?' * len(TRADE_COLS))})",
                        [t.get(c) for c in TRADE_COLS])
            added["trades"] += con.execute("SELECT changes()").fetchone()[0]
        for m in data["markets"]:
            con.execute(
                f"INSERT INTO markets({','.join(MARKET_COLS)},first_seen_at,last_seen_at) "
                f"VALUES ({','.join('?' * len(MARKET_COLS))},?,?) ON CONFLICT(market_id) DO UPDATE SET "
                + ",".join(f"{c}=COALESCE(excluded.{c}, markets.{c})" for c in MARKET_COLS[1:])
                + ",first_seen_at=MIN(markets.first_seen_at, excluded.first_seen_at)"
                + ",last_seen_at=MAX(markets.last_seen_at, excluded.last_seen_at)",
                [m.get(c) for c in MARKET_COLS] + [m.get("first_seen_at"), m.get("last_seen_at")])
            added["markets"] += 1
    return dict(added)
