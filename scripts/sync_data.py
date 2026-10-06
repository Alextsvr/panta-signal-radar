"""Pull the 24/7 dataset collected by GitHub Actions (branch `data`) into the local SQLite.

Usage:  python scripts/sync_data.py            # uses the git remote `origin`
Idempotent: runs already imported are skipped.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from panta_radar.config import load_settings  # noqa: E402
from panta_radar.datastore import TABLES, import_lines  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def main() -> int:
    s = load_settings()
    try:
        git("fetch", "--quiet", "origin", "data")
    except subprocess.CalledProcessError as e:
        print("git fetch failed:", (e.stderr or "").strip()[:300])
        return 1
    files = [f for f in git("ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines() if f.endswith(".jsonl")]
    by_table = {t: [] for t in TABLES}
    for f in sorted(files):
        table = f.split("/", 1)[0]
        if table in by_table:
            by_table[table].extend(git("show", f"FETCH_HEAD:{f}").splitlines())
    added = import_lines(s.db_path, by_table)
    print(f"{len(files)} files -> {s.db_path.name}: added {added}")
    # committed verbatim trade captures (datasets/panta-trade-backfill-*), idempotent by trade_id
    from panta_radar.reproduce import DATASETS, backfill_trades
    from panta_radar.storage import connect, insert_trades
    for d in sorted(DATASETS.glob("panta-trade-backfill-*")):
        if (d / "trades_raw.json").exists():
            with connect(s.db_path) as con:
                new = insert_trades(con, None, backfill_trades(d))
            print(f"{d.name}: {new} new trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
