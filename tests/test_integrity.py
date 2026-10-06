"""Regression tests for the Oct 1-6 data-integrity audit (row consistency, transient API rows,
resolution vs movement, state flapping, volume glitches, trade de-duplication, causal replay)."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from panta_radar.datastore import import_lines
from panta_radar.replay import MM_ROLE, build_replay, prepare_trades, wallet_profiles
from panta_radar.signals import build_radar, crowd_activity
from panta_radar.snapshots import is_valid_valuation, latest_rows, market_states, prepare_snapshots
from panta_radar.storage import load_all_trades

T0 = datetime(2026, 10, 3, 0, tzinfo=timezone.utc)
MK = pd.DataFrame([{"market_id": "M", "title": "t", "resolved": 0}])


def row(h, phase, status, yes, vol, total, src, val, mid="M", enriched=1):
    return {"run_id": int(h * 10), "snapshot_ts": (T0 + timedelta(hours=h)).isoformat(), "market_id": mid,
            "phase": phase, "status": status, "yes_price": yes, "volume_usdc": vol, "total_volume_usdc": total,
            "api_price_source": src, "valuation_status": val, "enriched": enriched}


def live(h, yes, vol, phase="primary", mid="M"):
    src = "primary_last" if phase == "primary" else "secondary_last_trade"
    return row(h, phase, phase, yes, vol, vol + 5, src, "complete" if phase == "primary" else "indicative", mid)


def broken(h, phase="primary", status="open", mid="M"):
    # verbatim shape of the degraded catalog rows: null price, "0.00" volume, null total/source
    return row(h, phase, status, None, 0.0, None, None, None, mid)


def resolved(h, yes, vol, mid="M"):
    return row(h, "resolved", "resolved", yes, vol, vol + 5, "resolved_outcome", "complete", mid, enriched=0)


def radar(rows, hours_after_last=1, mk=MK):
    snaps = pd.DataFrame(rows)
    now = pd.to_datetime(snaps["snapshot_ts"]).max() + timedelta(hours=hours_after_last)
    return build_radar(mk, snaps, now=now.to_pydatetime()).set_index("market_id")


# 1 ---------------------------------------------------------------- row consistency
def test_complementary_nans_are_never_merged_into_a_synthetic_snapshot():
    a = live(0, 0.51, 11.0)
    a["volume_usdc"] = None                                  # A: price, no volume
    b = row(1, "primary", "open", None, 20.0, None, None, None)  # B: volume, no price
    snaps = prepare_snapshots(pd.DataFrame([a, b]))
    latest = latest_rows(snaps).iloc[0]
    assert latest["snapshot_ts"] == pd.Timestamp(b["snapshot_ts"])
    assert pd.isna(latest["yes_price"]) and latest["volume_usdc"] == 20.0      # B, verbatim
    r = radar([a, b]).loc["M"]
    assert r["yes_price"] == 0.51 and r["valuation_ts"] == pd.Timestamp(a["snapshot_ts"])
    assert pd.isna(r["volume_usdc"])                         # NOT B's 20.0 glued onto A's price
    assert r["latest_row_valid"] == False  # noqa: E712


# 2 ---------------------------------------------------------------- transient missing valuation
def test_valid_invalid_valid_creates_no_fake_move():
    r = radar([live(0, 0.50, 23.0), broken(1), live(2, 0.50, 23.0)]).loc["M"]
    assert r["delta_pp"] == 0 and r["volume_delta"] == 0
    r = radar([live(0, 0.50, 23.0), broken(1)]).loc["M"]
    assert pd.isna(r["delta_pp"]) and pd.isna(r["volume_delta"])   # no reference -> no move at all
    assert r["yes_price"] == 0.50 and not r["latest_row_valid"]
    assert not is_valid_valuation(broken(1)) and is_valid_valuation(resolved(1, 0.0, 34.0))  # 0 is legit


# 3 ---------------------------------------------------------------- resolution is not movement
FRANCE = [  # France vs Belgium, real stored rows (Oct 3-6): live ~50%, then resolved NO
    live(1.97, 0.5048063, 23.0), live(7.95, 0.505631025, 23.5), live(17.5, 0.498110615, 29.0), broken(20.2),
    live(23.1, 0.501445429, 31.0), broken(32.85), live(38.67, 0.502275672, 31.5), broken(42.4),
    live(45.8, 0.505583014, 33.5), broken(48.5), live(54.25, 0.506406474, 34.0, "secondary"),
    live(63.15, 0.506406474, 34.0, "secondary"), resolved(70.0, 0.0, 34.0), resolved(74.3, 0.0, 34.0)]


def test_live_to_resolved_is_not_a_50pp_mover():
    r = radar([live(0, 0.50, 10.0), resolved(5, 0.0, 10.0)]).loc["M"]
    assert pd.isna(r["delta_pp"]) and r["signal"] == "RESOLVED" and r["outcome"] == "NO"
    assert r["yes_price"] == 0.0 and pd.isna(r["attention_score"])


def test_france_belgium_real_sequence():
    before = radar(FRANCE[:9], hours_after_last=0.5).loc["M"]     # Oct 4 21:47, still primary
    # 24h window: reference = Oct 3 23:07 (50.14%, 31.0 USDC) -> latest Oct 4 21:47 (50.56%, 33.5 USDC)
    assert abs(before["delta_pp"]) < 1 and before["volume_delta"] == pytest.approx(2.5)
    after = radar(FRANCE).loc["M"]
    assert after["analytical_state"] == "resolved" and after["outcome"] == "NO"
    assert pd.isna(after["delta_pp"]) and "moved" not in after["reason"]


# 4 ---------------------------------------------------------------- state flapping
def test_primary_resolved_secondary_active_resolved_stays_resolved():
    rows = [live(0, 0.50, 10.0), resolved(3, 1.0, 12.0), broken(6, "secondary", "secondary_active"),
            resolved(9, 1.0, 12.0)]
    st = market_states(prepare_snapshots(pd.DataFrame(rows))).iloc[0]
    assert st["analytical_state"] == "resolved" and st["outcome"] == "YES" and st["state_flaps"] == 1
    # latest RAW row live-looking (even with a valid price): still resolved, no live signal
    rows2 = rows[:3] + [live(9, 0.55, 15.0, "secondary")]
    r = radar(rows2).loc["M"]
    assert r["raw_phase"] == "secondary" and r["analytical_state"] == "resolved" and not r["is_open"]
    assert pd.isna(r["delta_pp"]) and pd.isna(r["attention_score"])
    rep = build_replay(radar(rows2).reset_index(), pd.DataFrame())
    assert rep.iloc[0]["outcome"] == "YES"


# 5 ---------------------------------------------------------------- volume glitch
def test_volume_glitch_100_missing_105_is_plus_5():
    r = radar([live(0, 0.5, 100.0), broken(1), live(2, 0.5, 105.0)]).loc["M"]
    assert r["volume_delta"] == pytest.approx(5.0)
    r = radar([live(0, 0.5, 388.0), live(1, 0.5, 380.0)]).loc["M"]          # valid but lower
    assert pd.isna(r["volume_delta"]) and r["volume_regression"] and "regression" in r["reason"]


# 6 ---------------------------------------------------------------- trade de-duplication
def trade(tid, wallet, mid, yes, no, ts, usdc=None):
    return {"trade_id": tid, "market_id": mid, "wallet": wallet, "is_primary": 1, "yes_amount": yes,
            "no_amount": no, "shares": yes + no, "fee_paid": 0.0, "usdc_amount": usdc, "side": None, "kind": "buy",
            "block_time": ts.isoformat(), "signature": tid}


def test_same_trade_in_ten_runs_counts_once(tmp_path):
    t = trade("sig:1", "alice", "M", 10, 0, T0, 5.0)
    lines = {"trades": [json.dumps(t)] * 10}
    db = tmp_path / "x.db"
    import_lines(db, lines)
    assert len(load_all_trades(db)) == 1
    dup = pd.DataFrame([t] * 10)
    assert len(prepare_trades(dup)) == 1
    assert wallet_profiles(prepare_trades(dup)).iloc[0]["trades"] == 1
    assert crowd_activity(dup, T0 + timedelta(hours=1)).iloc[0]["trades_24h"] == 1


# 7-10 ------------------------------------------------------------ causal replay
END = T0 + timedelta(days=2)


def target_radar(end=END):
    return pd.DataFrame([{"market_id": "TGT", "analytical_state": "resolved", "phase": "resolved",
                          "outcome": "YES", "end_time": end, "volume_usdc": 10.0, "is_open": False}])


def seeds(wallet, n_markets, start):
    out = []
    for i in range(n_markets):
        ts = start + timedelta(hours=i)
        out += [trade(f"{wallet}-s{i}a", wallet, f"S{i}", 100, 0, ts), trade(f"{wallet}-s{i}b", wallet, f"S{i}", 0, 100,
                                                                              ts + timedelta(seconds=2))]
    return out


def test_trade_after_close_cannot_influence_replay():
    tr = pd.DataFrame([trade("a", "alice", "TGT", 20, 0, END - timedelta(hours=1), 10.0),
                       trade("b", "bob", "TGT", 0, 900, END + timedelta(hours=1), 450.0)])
    r = build_replay(target_radar(), tr).iloc[0]
    assert r["directional_trades"] == 1 and r["yes_lean"] == 1.0 and bool(r["flow_correct"]) is True


def test_future_market_making_does_not_leak_into_replay():
    w_target = [trade("w1", "W", "TGT", 0, 50, END - timedelta(hours=5), 25.0),
                trade("w2", "W", "TGT", 0, 50, END - timedelta(hours=4), 25.0)]
    crowd = [trade("c1", "alice", "TGT", 10, 0, END - timedelta(hours=3), 5.0)]
    future = seeds("W", 6, END + timedelta(days=1))           # W only looks like a market maker LATER
    tr = pd.DataFrame(w_target + crowd + future)
    glob = wallet_profiles(prepare_trades(tr)).set_index("wallet")
    assert glob.loc["W", "role"] == MM_ROLE                 # the global profile would exclude W ...
    r = build_replay(target_radar(), tr).iloc[0]
    assert r["mm_wallets_asof"] == 0 and r["mm_trades"] == 0  # ... but as of close it was not known
    assert r["directional_trades"] == 3 and r["flow_call"] == "NO" and bool(r["flow_correct"]) is False


def test_known_market_maker_before_close_is_excluded():
    w_target = [trade("w1", "W", "TGT", 0, 50, END - timedelta(hours=5), 25.0)]
    crowd = [trade("c1", "alice", "TGT", 10, 0, END - timedelta(hours=3), 5.0)]
    past = seeds("W", 6, END - timedelta(days=1))
    r = build_replay(target_radar(), pd.DataFrame(w_target + crowd + past)).iloc[0]
    assert r["mm_wallets_asof"] == 1 and r["mm_trades"] == 1
    assert r["directional_trades"] == 1 and r["flow_call"] == "YES" and bool(r["flow_correct"]) is True


def test_live_view_uses_current_global_profile():
    w_open = [trade("w1", "W", "OPEN", 0, 50, END + timedelta(days=2), 25.0)]
    tr = pd.DataFrame(w_open + seeds("W", 6, END + timedelta(days=1)))
    act = crowd_activity(tr, END + timedelta(days=2, hours=1)).set_index("market_id").loc["OPEN"]
    assert act["trades_24h"] == 0 and act["mm_prints_24h"] == 1   # W excluded from the live crowd
