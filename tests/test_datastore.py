from datetime import datetime, timezone

from panta_radar.datastore import TABLES, export_db, import_lines
from panta_radar.normalize import normalize_market, normalize_trade
from panta_radar.storage import (connect, finish_run, insert_snapshot, insert_trades, load_snapshots, start_run,
                                 upsert_market)


def make_run_db(path, ts, price):
    with connect(path) as con:
        rid = start_run(con, ts)
        m = normalize_market({"marketId": "A", "title": "t", "yesPrice": str(price), "phase": "primary"})
        upsert_market(con, m, ts)
        insert_snapshot(con, rid, ts, m, {"trades_24h": 1})
        insert_trades(con, rid, [normalize_trade({"id": 1, "signature": "s", "yesAmount": 1e6, "blockTime": 1}, "A")])
        finish_run(con, rid, 1, 1, 3, [])


def read(out):
    return {t: [l for f in sorted((out / t).glob("*.jsonl")) for l in f.read_text().splitlines()]
            for t in TABLES if (out / t).exists()}


def test_roundtrip_two_ci_runs_into_local_db(tmp_path):
    out = tmp_path / "store"
    for i, (h, p) in enumerate([(10, 0.4), (11, 0.6)]):
        db = tmp_path / f"run{i}.db"           # each CI run uses a fresh DB with run_id = 1
        make_run_db(db, datetime(2026, 10, 1, h, tzinfo=timezone.utc), p)
        export_db(db, out)
    local = tmp_path / "local.db"
    added = import_lines(local, read(out))
    assert added["runs"] == 2 and added["snapshots"] == 2 and added["trades"] == 1
    s = load_snapshots(local)
    assert sorted(s["yes_price"]) == [0.4, 0.6] and s["run_id"].nunique() == 2
    again = import_lines(local, read(out))     # idempotent
    assert again.get("runs", 0) == 0 and again.get("snapshots", 0) == 0
    with connect(local) as con:
        assert con.execute("SELECT title FROM markets").fetchone()[0] == "t"
