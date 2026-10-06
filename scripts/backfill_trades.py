"""Backfill trade tapes (and detail rows) for every market in the catalog — read-only GETs.

Usage:  python scripts/backfill_trades.py            # all markets (~2 calls each)
        python scripts/backfill_trades.py --no-detail

Feeds the Resolved Replay: closed markets have a known outcome and a full pre-close tape.
Raw JSON goes to data/raw/ (git-ignored).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.config import RAW_DIR, load_settings  # noqa: E402
from panta_radar.normalize import display_title, normalize_market, normalize_markets, normalize_trade  # noqa: E402
from panta_radar.storage import connect, insert_trades, upsert_market  # noqa: E402


def export_dataset(out: Path, raw_trades: dict, ts: datetime) -> None:
    """Verbatim `{marketId: [rows]}` + manifest with sha256, so others can verify the capture."""
    import hashlib
    out.mkdir(parents=True, exist_ok=True)
    body = json.dumps(raw_trades, ensure_ascii=False).encode("utf-8")
    (out / "trades_raw.json").write_bytes(body)
    rows = [t for v in raw_trades.values() for t in v]
    bts = [t.get("blockTime") for t in rows if t.get("blockTime")]
    iso = lambda x: datetime.fromtimestamp(x, timezone.utc).isoformat()
    manifest = {"name": out.name, "source": "GET /api/v1/markets/{marketId}/trades/?limit=200",
                "fetched_at_utc": ts.isoformat(), "markets": len(raw_trades),
                "markets_with_trades": sum(1 for v in raw_trades.values() if v), "rows": len(rows),
                "unique_trade_ids": len({t.get("id") for t in rows}),
                "block_time_min": iso(min(bts)) if bts else None, "block_time_max": iso(max(bts)) if bts else None,
                "max_rows_per_market": max((len(v) for v in raw_trades.values()), default=0),
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"exported {len(rows)} rows to {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-detail", action="store_true", help="skip GET /markets/{id}/")
    ap.add_argument("--export", metavar="DIR", help="also write a verbatim, committable dataset "
                    "(trades_raw.json + manifest.json) to DIR, e.g. datasets/panta-trade-backfill-YYYY-MM-DD")
    a = ap.parse_args()
    s = load_settings()
    print(f"key {s.masked_key} -> {s.base_url}  db={s.db_path.name}")
    c = PantaClient(s, min_interval_s=0.6)
    ts = datetime.now(timezone.utc)
    try:
        markets = normalize_markets(c.get_markets(max_pages=40))
    except PantaAPIError as e:
        print("API ERROR:", e)
        return 2
    print(f"catalog: {len(markets)} markets {dict(Counter(m['phase'] for m in markets))}")
    raw_trades, raw_details, errors, new_total = {}, {}, [], 0
    with connect(s.db_path) as con:
        for i, m in enumerate(markets, 1):
            mid = m["market_id"]
            try:
                if not a.no_detail:
                    d = c.get_market(mid)
                    raw_details[mid] = d
                    nd = normalize_market(d)
                    if nd:
                        m = {**m, **{k: v for k, v in nd.items() if v is not None}}
                tr = c.get_market_trades(mid, limit=200)
                raw_trades[mid] = tr
                rows = [t for t in (normalize_trade(r, mid) for r in tr) if t]
                new = insert_trades(con, None, rows)
                new_total += new
                print(f"  [{i:3d}/{len(markets)}] {m['phase'] or '-':9s} trades={len(rows):3d} new={new:3d}  "
                      f"{display_title(m)[:60]}")
            except PantaAPIError as e:
                errors.append(str(e))
                print(f"  [{i:3d}/{len(markets)}] ERROR {e}")
            upsert_market(con, m, ts)
            con.commit()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    (RAW_DIR / f"{stamp}_backfill_trades.json").write_text(json.dumps(raw_trades), encoding="utf-8")
    if raw_details:
        (RAW_DIR / f"{stamp}_backfill_details.json").write_text(json.dumps(raw_details), encoding="utf-8")
    if a.export:
        out = Path(a.export) if a.export != "auto" else \
            Path(__file__).resolve().parents[1] / "datasets" / f"panta-trade-backfill-{ts:%Y-%m-%d}"
        export_dataset(out, raw_trades, ts)
    print(f"done: api_calls={c.calls} new_trades={new_total} errors={len(errors)}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
