"""Reproduce the headline numbers from public repository data only.

    git fetch origin data
    python scripts/reproduce_analysis.py              # reads FETCH_HEAD of the `data` branch
    python scripts/reproduce_analysis.py --data-dir path/to/data-branch-checkout
    python scripts/reproduce_analysis.py --ref <data-branch commit>   # pin to the README snapshot

Prints two views: the public collector dataset alone, and the same plus the committed
Oct 1 trade backfill (datasets/panta-trade-backfill-2026-10-01). No API key, no local DB.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from panta_radar.datastore import TABLES, read_dir  # noqa: E402
from panta_radar.reproduce import DATASETS, format_report, run_analysis  # noqa: E402


def from_git(ref: str) -> dict[str, list[str]]:
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, check=True, capture_output=True, text=True,
                                    encoding="utf-8").stdout
    git("fetch", "--quiet", "origin", "data")  # also makes older data-branch commits available for --ref
    files = [f for f in git("ls-tree", "-r", "--name-only", ref).splitlines() if f.endswith(".jsonl")]
    out = {t: [] for t in TABLES}
    for f in sorted(files):
        t = f.split("/", 1)[0]
        if t in out:
            out[t].extend(git("show", f"{ref}:{f}").splitlines())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", help="checkout of the data branch (default: git fetch origin data)")
    ap.add_argument("--ref", default="FETCH_HEAD",
                    help="data-branch commit to analyse (default: latest). README numbers are pinned to a commit.")
    a = ap.parse_args()
    store = read_dir(Path(a.data_dir)) if a.data_dir else from_git(a.ref)
    backfills = sorted(p for p in DATASETS.glob("panta-trade-backfill-*") if (p / "trades_raw.json").exists())
    print(format_report(run_analysis(store, []), "Public collector dataset only (data branch)"))
    print()
    print(format_report(run_analysis(store, backfills),
                        "Data branch + committed trade backfill(s): " + ", ".join(p.name for p in backfills)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
