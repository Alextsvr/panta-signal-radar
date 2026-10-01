from datetime import datetime, timedelta, timezone

import pandas as pd

from panta_radar.signals import build_radar, score_row

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def mk_markets(**over):
    base = {"market_id": "M1", "title": "Will X happen?", "category": "crypto", "phase": "primary",
            "status": "primary", "resolved": 0, "start_time": "2026-01-01T00:00:00+00:00",
            "end_time": "2026-12-31T23:59:59+00:00"}
    base.update(over)
    return pd.DataFrame([base])


def snap(ts, price, vol, **kw):
    row = {"run_id": 1, "snapshot_ts": ts.isoformat(), "market_id": "M1", "phase": "primary",
           "status": "primary", "yes_price": price, "volume_usdc": vol, "trades_24h": None,
           "trades_1h": None, "wallets_24h": None, "tape_capped": None}
    row.update(kw)
    return row


def test_first_snapshot_has_no_fake_history():
    r = build_radar(mk_markets(), pd.DataFrame([snap(NOW, 0.5, 0.0)]), now=NOW).iloc[0]
    assert pd.isna(r["delta_pp"]) and pd.isna(r["volume_delta"])
    assert not r["has_history"]
    assert "accumulating" in r["reason"]
    assert r["attention_score"] == 0 and r["signal"] == "QUIET"


def test_delta_calculation():
    s = pd.DataFrame([snap(NOW - timedelta(hours=6), 0.40, 100.0),
                      snap(NOW - timedelta(hours=3), 0.45, 150.0, run_id=2),
                      snap(NOW, 0.524, 300.0, run_id=3)])
    r = build_radar(mk_markets(), s, now=NOW).iloc[0]
    assert round(r["delta_pp"], 1) == 12.4          # vs oldest snapshot in the window
    assert r["volume_delta"] == 200.0 and r["volume_growth"] == 2.0
    assert r["observed_h"] == 6.0
    assert "+12.4pp" in r["reason"] and "+200.00 USDC" in r["reason"]


def test_window_excludes_old_reference():
    s = pd.DataFrame([snap(NOW - timedelta(hours=30), 0.10, 0.0), snap(NOW, 0.60, 0.0, run_id=2)])
    r = build_radar(mk_markets(), s, now=NOW).iloc[0]
    assert pd.isna(r["delta_pp"])  # only reference is outside the 24h window


def test_zero_previous_volume():
    s = pd.DataFrame([snap(NOW - timedelta(hours=1), 0.5, 0.0), snap(NOW, 0.5, 50.0, run_id=2)])
    r = build_radar(mk_markets(), s, now=NOW).iloc[0]
    assert r["volume_delta"] == 50.0 and pd.isna(r["volume_growth"])
    assert "from zero" in r["reason"]


def test_score_bounds_and_explanation():
    row = {"phase": "primary", "resolved": False, "yes_price": 0.9, "ref_yes_price": 0.2,
           "snapshot_ts": NOW, "ref_ts": NOW - timedelta(hours=2), "volume_usdc": 1e7,
           "ref_volume_usdc": 0.0, "trades_24h": 5000, "trades_1h": 4000, "wallets_24h": 300,
           "tape_capped": True, "start_time": NOW - timedelta(hours=1), "end_time": NOW + timedelta(hours=5)}
    out = score_row(row, NOW)
    assert out["attention_score"] == 100
    assert out["signal"] == "TRENDING"
    assert "≥5000 trades" in out["reason"]


def test_missing_values_do_not_crash_and_resolved_unscored():
    out = score_row({"phase": "resolved", "resolved": True}, NOW)
    assert out["attention_score"] is None and out["signal"] == "RESOLVED"
    out = score_row({"phase": "primary"}, NOW)
    assert out["attention_score"] == 0


def test_activity_only_not_inflated():
    row = {"phase": "primary", "resolved": False, "trades_24h": 50, "trades_1h": 2}
    out = score_row(row, NOW)
    assert out["attention_score"] == 25  # activity weight only; missing history contributes 0


def test_empty_inputs():
    assert build_radar(pd.DataFrame(), pd.DataFrame(), now=NOW).empty


def test_closing_soon_and_last_trade_reason():
    row = {"phase": "secondary", "resolved": False, "trades_24h": 0, "trades_1h": 0,
           "end_time": NOW + timedelta(hours=7.5), "last_trade_at": NOW - timedelta(days=5),
           "valuation_status": "indicative"}
    out = score_row(row, NOW)
    assert out["signal"] == "CLOSING SOON" and out["attention_score"] == 10
    assert "ends in 7.5h" in out["reason"] and "Last trade 5d ago" in out["reason"]
    assert "indicative" in out["reason"]
