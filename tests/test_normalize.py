from datetime import datetime, timezone

from panta_radar.normalize import (normalize_market, normalize_markets, normalize_trade,
                                   summarize_trades, ts_to_dt)

# Shape copied from the real sandbox response (GET /markets/ with a pk_test_ key)
REAL_ROW = {
    "marketId": "TestMarket1111111111111111111111111111111", "category": "crypto",
    "title": "Sandbox test market", "description": "Fixture market for pk_test_ keys. Not on mainnet.",
    "images": [], "phase": "primary", "marketType": "standard",
    "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-12-31T23:59:59Z",
    "resolutionTime": "2027-01-01T00:00:00Z", "region": "Global", "resolved": False,
    "status": "primary", "volumeUsdc": "0.00", "campaignId": None, "createdByPartner": False,
    "yesPrice": "0.50", "noPrice": "0.50", "primaryYesPrice": "0.50", "primaryNoPrice": "0.50",
    "secondaryYesPrice": None, "secondaryNoPrice": None,
}


def test_real_row_normalizes():
    m = normalize_market(REAL_ROW)
    assert m["market_id"].startswith("TestMarket")
    assert m["yes_price"] == 0.5 and m["price_source"] == "yesPrice"
    assert m["volume_usdc"] == 0.0
    assert m["start_time"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert m["image"] is None and m["resolved"] is False


def test_timestamps_iso_unix_ms():
    assert ts_to_dt("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ts_to_dt(1767225600) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ts_to_dt(1767225600000) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ts_to_dt(None) is None and ts_to_dt("garbage") is None and ts_to_dt(0) is None


def test_missing_fields_are_none_not_invented():
    m = normalize_market({"marketId": "X"})
    assert m["yes_price"] is None and m["volume_usdc"] is None and m["category"] is None
    assert m["start_time"] is None and m["title"] is None


def test_price_fallbacks_and_bounds():
    m = normalize_market({"marketId": "A", "phase": "secondary", "yesPrice": None,
                          "primaryYesPrice": "0.4", "secondaryYesPrice": "0.7"})
    assert m["yes_price"] == 0.7 and m["price_source"] == "secondaryYesPrice"
    assert normalize_market({"marketId": "B", "yesPrice": "1.7"})["yes_price"] is None


def test_volume_base_units_fallback():
    assert normalize_market({"marketId": "A", "volumeUsdcBase": "8600000"})["volume_usdc"] == 8.6


def test_malformed_and_duplicates():
    assert normalize_market(None) is None and normalize_market({"title": "no id"}) is None
    out = normalize_markets([{"marketId": "A", "title": "old"}, {"marketId": "A", "title": "new"}, "junk"])
    assert len(out) == 1 and out[0]["title"] == "new"


def test_trades_summary():
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    raw = [{"id": i, "signature": f"s{i}", "wallet": f"w{i % 2}", "yesAmount": "10", "noAmount": "0",
            "feePaid": "0.05", "blockTime": int(now.timestamp()) - i * 1800} for i in range(6)]
    trades = [normalize_trade(r, "M") for r in raw]
    s = summarize_trades(trades, now, limit_used=200)
    assert s["trades_24h"] == 6 and s["trades_1h"] == 3  # 0, 30, 60 minutes ago
    assert s["wallets_24h"] == 2 and s["tape_capped"] is False
    assert trades[0]["side"] == "yes" and trades[0]["market_id"] == "M"
