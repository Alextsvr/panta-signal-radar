"""Public read-only deployment: rebuild/refresh the local SQLite from repository-accessible data.

    GitHub Actions (+ PANTA_API_KEY secret) -> public `data` branch (JSONL)
        -> this module -> ephemeral local SQLite -> Streamlit dashboard

The public app never calls the Panta API. Data sources, in order:
  1. PANTA_RADAR_DATA_DIR      a local checkout/copy of the data branch (tests, offline runs)
  2. git fetch origin data     when the app runs from a git clone with an `origin` remote
  3. HTTPS from GitHub         tree listing via api.github.com + files via raw.githubusercontent.com
Committed verbatim captures in datasets/panta-trade-backfill-* are always imported too.
Every import is idempotent (runs keyed by started_at, trades by trade_id).
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from .config import PROJECT_ROOT
from .datastore import TABLES, import_lines, read_dir
from .reproduce import DATASETS, backfill_trades
from .storage import connect, insert_trades

DEFAULT_REPO = "Alextsvr/panta-signal-radar"
DATA_BRANCH = "data"
SYNC_INTERVAL_S = 600  # at most one sync attempt per 10 minutes per app process

Lines = dict[str, list[str]]


def _empty() -> Lines:
    return {t: [] for t in TABLES}


def from_git(root: Path = PROJECT_ROOT, timeout: int = 60) -> Lines:
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True,
                                    encoding="utf-8", timeout=timeout).stdout
    git("fetch", "--quiet", "origin", DATA_BRANCH)
    out = _empty()
    for f in sorted(git("ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines()):
        t = f.split("/", 1)[0]
        if f.endswith(".jsonl") and t in out:
            out[t].extend(git("show", f"FETCH_HEAD:{f}").splitlines())
    return out


def from_https(repo: str = DEFAULT_REPO, timeout: int = 30) -> Lines:
    tree = requests.get(f"https://api.github.com/repos/{repo}/git/trees/{DATA_BRANCH}?recursive=1", timeout=timeout)
    tree.raise_for_status()
    out = _empty()
    for item in sorted(tree.json().get("tree", []), key=lambda i: i["path"]):
        p, t = item["path"], item["path"].split("/", 1)[0]
        if item.get("type") == "blob" and p.endswith(".jsonl") and t in out:
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/{DATA_BRANCH}/{p}", timeout=timeout)
            r.raise_for_status()
            out[t].extend(r.text.splitlines())
    return out


def default_source() -> Lines:
    local = os.getenv("PANTA_RADAR_DATA_DIR")
    if local:
        return read_dir(Path(local))
    errors = []
    if (PROJECT_ROOT / ".git").exists():
        try:
            return from_git()
        except (subprocess.SubprocessError, OSError) as e:
            errors.append(f"git: {type(e).__name__}")
    try:
        return from_https(os.getenv("PANTA_RADAR_DATA_REPO", DEFAULT_REPO))
    except requests.RequestException as e:
        errors.append(f"https: {type(e).__name__}")
    raise RuntimeError("could not read the public data branch (" + "; ".join(errors) + ")")


@dataclass
class SyncResult:
    ok: bool
    db_exists: bool
    added: dict = field(default_factory=dict)
    backfill_new: int = 0
    error: str | None = None
    at: float = field(default_factory=time.time)

    @property
    def fatal(self) -> bool:
        """Only fatal when nothing can be served."""
        return not self.ok and not self.db_exists

    @property
    def changed(self) -> bool:
        return bool(self.backfill_new or any(self.added.values()))


def sync_public_data(db_path: Path, source: Callable[[], Lines] = default_source,
                     datasets_dir: Path = DATASETS) -> SyncResult:
    """Fetch the data branch and import missing runs/snapshots/trades/markets + committed backfills.
    Never raises: failures are reported in the result so the app can keep serving an existing DB."""
    db_path = Path(db_path)
    existed = db_path.exists()
    try:
        lines = source()
        added = import_lines(db_path, lines)
        new = 0
        for d in sorted(Path(datasets_dir).glob("panta-trade-backfill-*")):
            if (d / "trades_raw.json").exists():
                with connect(db_path) as con:
                    new += insert_trades(con, None, backfill_trades(d))
        return SyncResult(ok=True, db_exists=db_path.exists(), added=added, backfill_new=new)
    except Exception as e:  # noqa: BLE001 - any failure must stay non-fatal for an existing DB
        return SyncResult(ok=False, db_exists=existed and db_path.exists(), error=f"{type(e).__name__}: {e}"[:300])


class SyncThrottle:
    """Runs a sync at most once per `interval_s` (success or failure), not on every Streamlit rerun."""

    def __init__(self, interval_s: float = SYNC_INTERVAL_S, clock: Callable[[], float] = time.monotonic):
        self.interval_s, self.clock = interval_s, clock
        self.last_attempt: float | None = None
        self.last_result: SyncResult | None = None

    def due(self) -> bool:
        if self.last_attempt is None:
            return True
        # nothing to serve yet -> retry after a minute instead of waiting a full interval
        wait = 60 if (self.last_result is not None and self.last_result.fatal) else self.interval_s
        return self.clock() - self.last_attempt >= wait

    def maybe_sync(self, fn: Callable[[], SyncResult], force: bool = False) -> SyncResult | None:
        if force or self.due():
            self.last_attempt = self.clock()
            self.last_result = fn()
        return self.last_result
