from datetime import datetime, timezone

from panta_radar.normalize import normalize_market, normalize_trade
from panta_radar.storage import (connect, finish_run, insert_snapshot, insert_trades, load_snapshots,
                                 load_trades, start_run, upsert_market)


def test_snapshot_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
    m = normalize_market({"marketId": "A", "title": "t", "yesPrice": "0.3", "volumeUsdc": "1.5"})
    t = normalize_trade({"id": 1, "signature": "sig", "yesAmount": "1", "blockTime": 1767225600}, "A")
    with connect(db) as con:
        rid = start_run(con, ts)
        upsert_market(con, m, ts)
        insert_snapshot(con, rid, ts, m, {"trades_24h": 1})
        assert insert_trades(con, rid, [t]) == 1
        assert insert_trades(con, rid, [t]) == 0  # de-duplicated
        finish_run(con, rid, 1, 1, 3, [])
    s = load_snapshots(db)
    assert len(s) == 1 and s.iloc[0]["yes_price"] == 0.3 and s.iloc[0]["enriched"] == 1
    assert len(load_trades("A", db)) == 1
