"""The committed Oct 1 backfill must keep reproducing the dominant-wallet finding without any local DB."""
import hashlib
import json
from datetime import datetime, timezone

from panta_radar.datastore import export_db
from panta_radar.normalize import normalize_market, normalize_trade
from panta_radar.reproduce import DATASETS, backfill_trades, run_analysis
from panta_radar.storage import connect, finish_run, insert_trades, start_run, upsert_market

BF = DATASETS / "panta-trade-backfill-2026-10-01"


def test_backfill_dataset_matches_manifest():
    m = json.loads((BF / "manifest.json").read_text())
    assert hashlib.sha256((BF / "trades_raw.json").read_bytes()).hexdigest() == m["sha256"]
    trades = backfill_trades(BF)
    assert len(trades) == m["rows"] == len({t["trade_id"] for t in trades}) == 452


def test_dominant_wallet_reproduces_from_committed_data_only():
    r = run_analysis({}, [BF])
    top = r["dominant_wallet"]
    assert r["unique_trades"] == 452 and r["wallets"] == 76 and r["seed_pair_prints"] == 128
    assert top["wallet"].startswith("Bji2jp") and top["markets"] == 68 and round(top["share_of_tape"], 2) == 0.81
    assert top["role"] == "market-making behaviour"


def test_export_skips_trades_already_in_datastore(tmp_path):
    out = tmp_path / "store"
    for i in range(3):  # three CI runs that all see the same trade
        db = tmp_path / f"r{i}.db"
        ts = datetime(2026, 10, 1, 10 + i, tzinfo=timezone.utc)
        with connect(db) as con:
            rid = start_run(con, ts)
            upsert_market(con, normalize_market({"marketId": "A"}), ts)
            insert_trades(con, rid, [normalize_trade({"id": 1, "signature": "s", "yesAmount": 1e6, "blockTime": 1}, "A")])
            finish_run(con, rid, 1, 0, 1, [])
        export_db(db, out)
    lines = [l for f in (out / "trades").glob("*.jsonl") for l in f.read_text().splitlines() if l.strip()]
    assert len(lines) == 1


class FakeClient:
    """Offline stand-in for PantaClient: one open and one resolved market, each with one trade."""
    calls = 0

    class settings:
        db_path = None

    def get_markets(self, max_pages=40):
        return [{"marketId": "OPEN", "phase": "primary", "resolved": False, "yesPrice": "0.5", "volumeUsdc": "1"},
                {"marketId": "DONE", "phase": "resolved", "resolved": True, "yesPrice": "1", "volumeUsdc": "9"}]

    def get_market(self, mid):
        return {"marketId": mid, "phase": "primary", "yesPrice": "0.5", "totalVolumeUsdc": "6"}

    def get_market_trades(self, mid, limit=200):
        return [{"id": f"{mid}-1", "signature": f"sig-{mid}", "marketId": mid, "wallet": "w", "yesAmount": 1e6,
                 "noAmount": 0.0, "blockTime": 1790000000, "shares": "1.0"}]


def test_ci_collector_stores_tapes_of_resolved_markets_too(tmp_path):
    from panta_radar.collect import collect_snapshot
    from panta_radar.storage import load_all_trades
    for all_tapes, expected in ((False, {"OPEN"}), (True, {"OPEN", "DONE"})):
        db = tmp_path / f"c{all_tapes}.db"
        collect_snapshot(FakeClient(), db_path=db, save_raw=False, all_tapes=all_tapes)
        assert set(load_all_trades(db)["market_id"]) == expected
