import math

import pandas as pd

from panta_radar.normalize import normalize_trade, to_shares
from panta_radar.replay import build_replay, outcome_from_price, prepare_trades, replay_market


def test_live_share_units():
    # live tape: "19677335" shares for 10 USDC -> 19.68 shares, price ~0.508
    assert to_shares("19677335") == 19.677335
    assert to_shares(19677335) == 19.677335
    assert to_shares("10.00") == 10.0
    t = normalize_trade({"id": 1, "signature": "s", "yesAmount": "19677335", "noAmount": "0",
                         "amountUsdc": "10.0", "side": "YES", "kind": "buy", "blockTime": 1790000000}, "M")
    assert t["shares"] == 19.677335 and t["side"] == "yes"


def tape(rows):
    return pd.DataFrame([{"market_id": "M", "trade_id": str(i), "wallet": w, "yes_amount": y, "no_amount": n,
                          "shares": y + n, "usdc_amount": u, "block_time": pd.Timestamp(ts, unit="s", tz="UTC")}
                         for i, (w, y, n, u, ts) in enumerate(rows)])


def test_seed_pairs_detected_and_excluded():
    t = prepare_trades(tape([
        ("maker", 116.59, 0, None, 100), ("maker", 0, 116.55, None, 101),   # seed pair
        ("alice", 19.68, 0, 10.0, 200),                                      # directional YES
        ("bob", 0, 10.49, 5.0, 300),                                         # directional NO
    ]))
    assert t["is_seed"].tolist() == [True, True, False, False]
    assert round(t.loc[2, "implied_yes"], 3) == 0.508
    assert round(t.loc[3, "implied_yes"], 3) == round(1 - 5.0 / 10.49, 3)
    r = replay_market(t, "YES")
    assert r["directional_trades"] == 2 and r["seed_trades"] == 2 and r["flow_basis"] == "USDC"
    assert round(r["yes_lean"], 3) == round(10 / 15, 3) and r["flow_call"] == "YES" and r["flow_correct"] is True


def test_trades_after_close_ignored_and_split_flow():
    t = prepare_trades(tape([("a", 10, 0, 5.0, 100), ("b", 0, 10, 5.0, 200), ("c", 50, 0, 25.0, 999)]))
    r = replay_market(t, "NO", end_time=pd.Timestamp(500, unit="s", tz="UTC"))
    assert r["directional_trades"] == 2 and r["flow_call"] is None and r["flow_correct"] is None


def test_outcome_and_empty():
    assert outcome_from_price("1") == "YES" and outcome_from_price("0") == "NO"
    assert outcome_from_price("0.5") is None and outcome_from_price(None) is None
    r = replay_market(prepare_trades(pd.DataFrame()), "YES")
    assert r["directional_trades"] == 0 and math.isnan(r["yes_lean"])
    assert build_replay(pd.DataFrame(), pd.DataFrame()).empty
