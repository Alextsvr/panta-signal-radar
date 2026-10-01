"""One refresh = list all markets -> enrich open ones (detail prices + trade tape) -> snapshot to SQLite."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from .api import PantaAPIError, PantaClient
from .config import RAW_DIR
from .normalize import display_title, is_open, normalize_market, normalize_markets, normalize_trade, summarize_trades
from .storage import connect, finish_run, insert_snapshot, insert_trades, start_run, upsert_market, utcnow

TRADES_LIMIT = 200  # documented max


def _save_raw(name: str, ts: datetime, payload) -> None:
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{ts.strftime('%Y%m%dT%H%M%SZ')}_{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # raw dumps are a debugging aid, never fatal


def collect_snapshot(client: PantaClient | None = None, max_enrich: int = 40, db_path=None,
                     progress: Callable[[float, str], None] | None = None, save_raw: bool = True) -> dict:
    """Returns a run summary dict. Raises PantaAPIError only if the market list itself fails."""
    client = client or PantaClient()
    db_path = db_path or client.settings.db_path
    say = progress or (lambda frac, msg: None)
    ts = utcnow()
    errors: list[str] = []

    say(0.02, "Fetching market catalog...")
    raw_markets = client.get_markets(max_pages=40)
    if save_raw:
        _save_raw("markets", ts, raw_markets)
    markets = normalize_markets(raw_markets)

    # Enrich open markets first by catalog volume (list rows carry no prices).
    open_ms = sorted([m for m in markets if is_open(m)], key=lambda m: m.get("volume_usdc") or 0, reverse=True)
    to_enrich = {m["market_id"] for m in open_ms[:max_enrich]}

    enriched = 0
    raw_details: dict[str, dict] = {}
    with connect(db_path) as con:
        run_id = start_run(con, ts)
        for i, m in enumerate(markets):
            activity = None
            if m["market_id"] in to_enrich:
                say(0.05 + 0.9 * enriched / max(1, len(to_enrich)), f"Enriching {display_title(m)}")
                try:
                    raw_d = client.get_market(m["market_id"])
                    raw_details[m["market_id"]] = raw_d
                    detail = normalize_market(raw_d)
                    if detail:
                        m = {**m, **{k: v for k, v in detail.items() if v is not None}}
                except PantaAPIError as e:
                    errors.append(str(e))
                try:
                    raw_tr = client.get_market_trades(m["market_id"], limit=TRADES_LIMIT)
                    trades = [t for t in (normalize_trade(r, m["market_id"]) for r in raw_tr) if t]
                    insert_trades(con, run_id, trades)
                    activity = summarize_trades(trades, ts, TRADES_LIMIT)
                except PantaAPIError as e:
                    errors.append(str(e))
                enriched += 1
            upsert_market(con, m, ts)
            insert_snapshot(con, run_id, ts, m, activity)
        finish_run(con, run_id, len(markets), enriched, client.calls, errors)
    if save_raw and raw_details:
        _save_raw("details", ts, raw_details)
    say(1.0, "Done")
    return {"run_id": run_id, "snapshot_ts": ts, "markets_seen": len(markets), "open_markets": len(open_ms),
            "markets_enriched": enriched, "api_calls": client.calls, "errors": errors, "db_path": str(db_path)}
