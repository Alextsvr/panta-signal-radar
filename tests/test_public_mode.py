"""Public read-only deployment: no API key, no Panta calls, DB rebuilt from repository data."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from panta_radar.api import PantaAPIError, PantaClient
from panta_radar.config import Settings, is_public_mode, load_settings
from panta_radar.datastore import export_db, read_dir
from panta_radar.normalize import normalize_market
from panta_radar.publicsync import SyncThrottle, sync_public_data
from panta_radar.replay import build_replay
from panta_radar.reproduce import DATASETS
from panta_radar.signals import build_radar
from panta_radar.storage import (connect, finish_run, insert_snapshot, load_all_trades, load_markets,
                                 load_snapshots, start_run, upsert_market)

ROOT = Path(__file__).resolve().parents[1]
BF = json.loads((DATASETS / "panta-trade-backfill-2026-10-01" / "trades_raw.json").read_text())


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True), (" on ", True),
                                            ("0", False), ("false", False), ("", False), (None, False)])
def test_public_mode_parsing(value, expected):
    env = {} if value is None else {"PANTA_RADAR_PUBLIC_MODE": value}
    assert is_public_mode(env) is expected


def test_public_mode_needs_no_key_and_ignores_one(monkeypatch):
    monkeypatch.setenv("PANTA_RADAR_PUBLIC_MODE", "1")
    monkeypatch.setenv("PANTA_API_KEY", "pk_live_should_never_be_used_0000000000000000")
    s = load_settings()
    assert s.public_mode and s.api_key is None


def test_public_mode_cannot_build_an_api_client():
    with pytest.raises(PantaAPIError) as e:
        PantaClient(Settings(api_key="pk_live_x" * 3, base_url="https://example.invalid", public_mode=True))
    assert e.value.code == "PUBLIC_MODE"


def make_data_branch(tmp: Path) -> Path:
    """A tiny but real-shaped data branch: two collector runs on the 3 busiest backfilled markets,
    first live, then resolved (outcome rows), exported with the production exporter."""
    busiest = sorted(BF, key=lambda m: len(BF[m]), reverse=True)[:3]
    out = tmp / "data-branch"
    for i, (h, resolved) in enumerate([(0, False), (6, True)]):
        db = tmp / f"ci{i}.db"
        ts = datetime(2026, 10, 1, 10, tzinfo=timezone.utc) + timedelta(hours=h)
        with connect(db) as con:
            rid = start_run(con, ts)
            for j, mid in enumerate(busiest):
                end = max(t["blockTime"] for t in BF[mid]) + 3600
                raw = {"marketId": mid, "title": f"market {j}", "category": "test", "endTime": end,
                       "phase": "resolved" if resolved else "secondary", "resolved": resolved,
                       "yesPrice": str(j % 2) if resolved else "0.5", "volumeUsdc": "10", "totalVolumeUsdc": "15",
                       "priceSource": "resolved_outcome" if resolved else "secondary_last_trade"}
                m = normalize_market(raw)
                upsert_market(con, m, ts)
                insert_snapshot(con, rid, ts, m, None)
            finish_run(con, rid, 3, 0, 4, [])
        export_db(db, out)
    return out


def test_fresh_db_bootstraps_from_public_data(tmp_path):
    branch = make_data_branch(tmp_path)
    db = tmp_path / "fresh" / "radar.db"
    assert not db.exists()
    r = sync_public_data(db, source=lambda: read_dir(branch))
    assert r.ok and not r.fatal and db.exists()
    assert r.added["runs"] == 2 and r.backfill_new == 452
    snaps, trades = load_snapshots(db), load_all_trades(db)
    assert len(snaps) == 6 and len(trades) == 452
    radar = build_radar(load_markets(db), snaps, trades=trades)
    rep = build_replay(radar, trades)
    assert len(rep) == 3 and rep["outcome"].notna().all() and rep["trades_total"].sum() > 0


def test_sync_is_idempotent(tmp_path):
    branch = make_data_branch(tmp_path)
    db = tmp_path / "radar.db"
    sync_public_data(db, source=lambda: read_dir(branch))
    counts = lambda: [sqlite3.connect(db).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("runs", "snapshots", "trades", "markets")]
    before = counts()
    again = sync_public_data(db, source=lambda: read_dir(branch))
    assert again.ok and not again.changed and again.added.get("runs", 0) == 0 and again.backfill_new == 0
    assert counts() == before


def test_sync_failure_with_existing_db_is_nonfatal(tmp_path):
    branch = make_data_branch(tmp_path)
    db = tmp_path / "radar.db"
    sync_public_data(db, source=lambda: read_dir(branch))

    def offline():
        raise RuntimeError("network down")
    r = sync_public_data(db, source=offline)
    assert not r.ok and not r.fatal and "network down" in r.error
    assert len(load_snapshots(db)) == 6                      # still serving the old copy
    r2 = sync_public_data(tmp_path / "missing.db", source=offline)
    assert r2.fatal                                          # nothing to serve -> fatal, but no exception


def test_throttle_limits_sync_frequency():
    now = [0.0]
    th = SyncThrottle(interval_s=600, clock=lambda: now[0])
    calls = []
    ok = lambda: calls.append(1) or type("R", (), {"fatal": False})()
    th.maybe_sync(ok); now[0] = 300; th.maybe_sync(ok)
    assert len(calls) == 1
    now[0] = 601; th.maybe_sync(ok)
    assert len(calls) == 2
    bad = lambda: calls.append(1) or type("R", (), {"fatal": True})()
    th.maybe_sync(bad, force=True); now[0] = 665; th.maybe_sync(bad)
    assert len(calls) == 4                                   # fatal -> retried after 60 s


def test_public_app_bootstraps_without_key_or_api_calls(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    branch = make_data_branch(tmp_path)
    db = tmp_path / "app" / "radar.db"
    monkeypatch.setenv("PANTA_RADAR_PUBLIC_MODE", "1")
    monkeypatch.setenv("PANTA_RADAR_DB", str(db))
    monkeypatch.setenv("PANTA_RADAR_DATA_DIR", str(branch))
    monkeypatch.delenv("PANTA_API_KEY", raising=False)

    def no_http(*a, **k):
        raise AssertionError("the public app must not make HTTP calls")
    monkeypatch.setattr(requests.sessions.Session, "request", no_http)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert db.exists()
    assert any("Public read-only mode" in i.value for i in at.sidebar.info)
    assert not [b for b in at.button if "Refresh from Panta" in b.label]
    assert not any("PANTA_API_KEY" in e.value for e in at.error)
    assert not any("API key" in c.value for c in at.sidebar.caption)
    assert any(m.label == "Resolved / closed markets" and m.value == "3" for m in at.metric)


def test_local_mode_keeps_refresh_control(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("PANTA_RADAR_PUBLIC_MODE", raising=False)
    monkeypatch.setenv("PANTA_RADAR_DB", str(tmp_path / "local.db"))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert [b for b in at.button if "Refresh from Panta" in b.label]
