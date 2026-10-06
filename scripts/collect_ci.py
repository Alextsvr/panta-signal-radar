"""Collector for GitHub Actions: one snapshot into a throwaway SQLite, appended to the JSONL datastore.

Usage (CI):  PANTA_API_KEY=... python scripts/collect_ci.py --out datastore
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.collect import collect_snapshot  # noqa: E402
from panta_radar.config import load_settings  # noqa: E402
from panta_radar.datastore import export_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="datastore")
    ap.add_argument("--max-enrich", type=int, default=60)
    a = ap.parse_args()
    s = load_settings()
    if not s.api_key:
        print("PANTA_API_KEY missing")
        return 1
    if s.api_key.startswith("pk_test_"):
        print("refusing to publish sandbox fixtures: use a pk_live_ key")
        return 1
    print(f"key {s.masked_key} -> {s.base_url}")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "run.db"
        try:
            r = collect_snapshot(PantaClient(s), max_enrich=a.max_enrich, db_path=db, save_raw=False,
                                 all_tapes=True)
        except PantaAPIError as e:
            print(f"API ERROR: {e}")
            return 2
        counts = export_db(db, Path(a.out))
    print(f"snapshot {r['snapshot_ts']:%Y-%m-%d %H:%M}Z markets={r['markets_seen']} open={r['open_markets']} "
          f"api_calls={r['api_calls']} errors={len(r['errors'])} exported={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
