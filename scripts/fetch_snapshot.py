"""Collect one snapshot from the real Panta API into data/radar.db.

Usage:
  python scripts/fetch_snapshot.py                 # one snapshot
  python scripts/fetch_snapshot.py --loop 15       # every 15 minutes (Ctrl+C to stop)
  python scripts/fetch_snapshot.py --max-enrich 60 # enrich more open markets (2 calls each)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.collect import collect_snapshot  # noqa: E402
from panta_radar.config import load_settings  # noqa: E402


def once(max_enrich: int) -> int:
    s = load_settings()
    print(f"key {s.masked_key} -> {s.base_url}  db={s.db_path.name}")
    try:
        r = collect_snapshot(PantaClient(s), max_enrich=max_enrich,
                             progress=lambda f, m: print(f"  [{f:4.0%}] {m}"))
    except PantaAPIError as e:
        print(f"API ERROR: {e}")
        return 2
    print(f"run #{r['run_id']} @ {r['snapshot_ts']:%Y-%m-%d %H:%M:%S}Z  markets={r['markets_seen']} "
          f"open={r['open_markets']} enriched={r['markets_enriched']} api_calls={r['api_calls']} "
          f"errors={len(r['errors'])}  -> {r['db_path']}")
    for e in r["errors"][:5]:
        print("  !", e)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, default=0, help="repeat every N minutes")
    ap.add_argument("--max-enrich", type=int, default=40)
    a = ap.parse_args()
    if not a.loop:
        return once(a.max_enrich)
    while True:
        once(a.max_enrich)
        print(f"sleeping {a.loop} min...")
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    raise SystemExit(main())
