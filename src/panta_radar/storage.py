"""Local SQLite store: market catalog, per-run snapshots, and a de-duplicated trade tape.

Every refresh writes one row per market into `snapshots` — that is how we build
real history (probability / volume / activity over time) that the API does not serve.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    markets_seen INTEGER,
    markets_enriched INTEGER,
    api_calls INTEGER,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    title TEXT, description TEXT, category TEXT, phase TEXT, status TEXT,
    market_type TEXT, region TEXT, resolved INTEGER,
    start_time TEXT, end_time TEXT, resolution_time TEXT,
    image TEXT, created_by_partner INTEGER,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id INTEGER NOT NULL,
    snapshot_ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    phase TEXT, status TEXT,
    yes_price REAL, no_price REAL, price_source TEXT,
    volume_usdc REAL, total_volume_usdc REAL,
    enriched INTEGER NOT NULL DEFAULT 0,
    trades_sampled INTEGER, trades_1h INTEGER, trades_24h INTEGER,
    shares_24h REAL, fees_24h REAL, wallets_24h INTEGER,
    last_trade_at TEXT, tape_capped INTEGER,
    PRIMARY KEY (run_id, market_id)
);
CREATE INDEX IF NOT EXISTS ix_snap_market_ts ON snapshots(market_id, snapshot_ts);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL, wallet TEXT, is_primary INTEGER,
    yes_amount REAL, no_amount REAL, shares REAL, fee_paid REAL, usdc_amount REAL,
    side TEXT, kind TEXT, block_time TEXT, signature TEXT,
    first_seen_run INTEGER
);
CREATE INDEX IF NOT EXISTS ix_trades_market_time ON trades(market_id, block_time);
"""

MARKET_COLS = ["market_id", "title", "description", "category", "phase", "status", "market_type",
               "region", "resolved", "start_time", "end_time", "resolution_time", "image",
               "created_by_partner"]
SNAP_COLS = ["phase", "status", "yes_price", "no_price", "price_source", "volume_usdc",
             "total_volume_usdc", "trades_sampled", "trades_1h", "trades_24h", "shares_24h",
             "fees_24h", "wallets_24h", "last_trade_at", "tape_capped"]
TRADE_COLS = ["trade_id", "market_id", "wallet", "is_primary", "yes_amount", "no_amount", "shares",
              "fee_paid", "usdc_amount", "side", "kind", "block_time", "signature"]


def iso(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, bool):
        return int(v)
    return v


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def connect(db_path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def start_run(con, started_at: datetime) -> int:
    cur = con.execute("INSERT INTO runs(started_at) VALUES (?)", (iso(started_at),))
    return int(cur.lastrowid)


def finish_run(con, run_id: int, markets_seen: int, enriched: int, api_calls: int, errors: list[str]):
    con.execute("UPDATE runs SET finished_at=?, markets_seen=?, markets_enriched=?, api_calls=?, errors=? "
                "WHERE run_id=?", (iso(utcnow()), markets_seen, enriched, api_calls,
                                   json.dumps(errors[:50]) if errors else None, run_id))


def upsert_market(con, m: dict, seen_at: datetime):
    vals = [iso(m.get(c)) for c in MARKET_COLS]
    con.execute(
        f"INSERT INTO markets({','.join(MARKET_COLS)},first_seen_at,last_seen_at) "
        f"VALUES ({','.join('?' * len(MARKET_COLS))},?,?) "
        f"ON CONFLICT(market_id) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in MARKET_COLS[1:]) + ",last_seen_at=excluded.last_seen_at",
        vals + [iso(seen_at), iso(seen_at)])


def insert_snapshot(con, run_id: int, ts: datetime, m: dict, activity: dict | None):
    row = {**m, **(activity or {})}
    vals = [iso(row.get(c)) for c in SNAP_COLS]
    con.execute(
        f"INSERT OR REPLACE INTO snapshots(run_id,snapshot_ts,market_id,enriched,{','.join(SNAP_COLS)}) "
        f"VALUES (?,?,?,?,{','.join('?' * len(SNAP_COLS))})",
        [run_id, iso(ts), m["market_id"], 1 if activity is not None else 0] + vals)


def insert_trades(con, run_id: int, trades: list[dict]) -> int:
    before = con.total_changes
    con.executemany(
        f"INSERT OR IGNORE INTO trades({','.join(TRADE_COLS)},first_seen_run) "
        f"VALUES ({','.join('?' * len(TRADE_COLS))},?)",
        [[iso(t.get(c)) for c in TRADE_COLS] + [run_id] for t in trades])
    return con.total_changes - before


# ------------------------------------------------------------------ readers
def _df(con, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def load_runs(db_path=DB_PATH) -> pd.DataFrame:
    with connect(db_path) as con:
        return _df(con, "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY run_id")


def load_markets(db_path=DB_PATH) -> pd.DataFrame:
    with connect(db_path) as con:
        return _df(con, "SELECT * FROM markets")


def load_snapshots(db_path=DB_PATH, since_iso: str | None = None) -> pd.DataFrame:
    with connect(db_path) as con:
        if since_iso:
            return _df(con, "SELECT * FROM snapshots WHERE snapshot_ts >= ? ORDER BY snapshot_ts", (since_iso,))
        return _df(con, "SELECT * FROM snapshots ORDER BY snapshot_ts")


def load_trades(market_id: str, db_path=DB_PATH, limit: int = 500) -> pd.DataFrame:
    with connect(db_path) as con:
        return _df(con, "SELECT * FROM trades WHERE market_id=? ORDER BY block_time DESC LIMIT ?",
                   (market_id, limit))
