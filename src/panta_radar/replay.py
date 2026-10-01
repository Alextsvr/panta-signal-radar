"""Trade-tape analytics: liquidity-seed detection, trade-implied prices, and the
Resolved Replay ("did the money see the outcome coming?").

Everything here is derived from real Panta trade rows. Observed live (2026-10-01):
  * yesAmount / noAmount are share quantities in 1e6 base units
  * amountUsdc is present on user trades; seeding trades have none
  * market creators / market makers buy YES and NO in near-equal size seconds apart
    -> that is liquidity provision, not a directional bet, and is excluded from flow
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

SEED_WINDOW_S = 10      # opposite-side buy by the same wallet within 10 s ...
SEED_SIZE_TOL = 0.02    # ... and within 2 % share size => liquidity seed


def prepare_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Typed copy with `is_seed`, `implied_yes` (trade-implied YES price) and `direction`."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["market_id", "wallet", "block_time", "side", "shares", "usdc_amount",
                                     "is_seed", "implied_yes", "direction"])
    t = trades.copy()
    t["block_time"] = pd.to_datetime(t["block_time"], utc=True, errors="coerce")
    for c in ("yes_amount", "no_amount", "shares", "usdc_amount"):
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t["direction"] = np.where(t["yes_amount"].fillna(0) > t["no_amount"].fillna(0), "yes",
                              np.where(t["no_amount"].fillna(0) > 0, "no", None))
    t["is_seed"] = False
    t = t.sort_values(["market_id", "wallet", "block_time"]).reset_index(drop=True)
    for _, g in t.groupby(["market_id", "wallet"], sort=False):
        idx = g.index.to_list()
        for a_pos, a in enumerate(idx):
            for b in idx[a_pos + 1:]:
                dt = (t.at[b, "block_time"] - t.at[a, "block_time"]).total_seconds()
                if dt > SEED_WINDOW_S:
                    break
                if t.at[a, "direction"] == t.at[b, "direction"]:
                    continue
                sa, sb = t.at[a, "shares"], t.at[b, "shares"]
                if sa and sb and abs(sa - sb) / max(sa, sb) <= SEED_SIZE_TOL:
                    t.at[a, "is_seed"] = True
                    t.at[b, "is_seed"] = True
    price = t["usdc_amount"] / t["shares"].where(t["shares"] > 0)
    price = price.where((price > 0) & (price < 1))
    t["implied_yes"] = np.where(t["direction"] == "yes", price,
                                np.where(t["direction"] == "no", 1 - price, np.nan))
    return t.sort_values(["market_id", "block_time"]).reset_index(drop=True)


def outcome_from_price(yes_price) -> str | None:
    """Resolved rows carry yesPrice "1"/"0" with priceSource=resolved_outcome."""
    try:
        p = float(yes_price)
    except (TypeError, ValueError):
        return None
    if math.isnan(p):
        return None
    return "YES" if p >= 0.999 else "NO" if p <= 0.001 else None


def replay_market(trades: pd.DataFrame, outcome: str | None, end_time=None) -> dict:
    """Directional flow before close vs the actual outcome for one market."""
    t = trades
    if end_time is not None and not pd.isna(end_time):
        t = t[t["block_time"] <= pd.Timestamp(end_time)]
    d = t[~t["is_seed"] & t["direction"].notna()]
    use_usdc = len(d) > 0 and d["usdc_amount"].notna().mean() >= 0.5
    w = d["usdc_amount"] if use_usdc else d["shares"]
    yes_w = float(w[d["direction"] == "yes"].fillna(0).sum())
    no_w = float(w[d["direction"] == "no"].fillna(0).sum())
    lean = yes_w / (yes_w + no_w) if (yes_w + no_w) > 0 else math.nan
    flow_call = None if math.isnan(lean) or abs(lean - 0.5) < 0.05 else ("YES" if lean > 0.5 else "NO")
    priced = d.dropna(subset=["implied_yes"])
    last_px = float(priced["implied_yes"].iloc[-1]) if len(priced) else math.nan
    price_call = None if math.isnan(last_px) or abs(last_px - 0.5) < 0.02 else ("YES" if last_px > 0.5 else "NO")
    return {
        "trades_total": int(len(t)), "seed_trades": int(t["is_seed"].sum()),
        "directional_trades": int(len(d)), "wallets": int(d["wallet"].nunique()),
        "flow_basis": "USDC" if use_usdc else "shares", "yes_flow": yes_w, "no_flow": no_w,
        "yes_lean": lean, "flow_call": flow_call,
        "flow_correct": None if (flow_call is None or outcome is None) else flow_call == outcome,
        "last_implied_yes": last_px, "price_call": price_call,
        "price_correct": None if (price_call is None or outcome is None) else price_call == outcome,
        "first_trade": d["block_time"].min() if len(d) else pd.NaT,
        "last_trade": d["block_time"].max() if len(d) else pd.NaT,
    }


def explain_replay(r: dict, outcome: str | None) -> str:
    if r["directional_trades"] == 0:
        return ("Only liquidity-seeding trades — no directional bets before close"
                if r["seed_trades"] else "No trades on the tape")
    parts = [f"{r['directional_trades']} directional trades from {r['wallets']} wallets"]
    if not math.isnan(r["yes_lean"]):
        parts.append(f"{r['yes_lean'] * 100:.0f}% of {r['flow_basis']} flow went to YES")
    if not math.isnan(r["last_implied_yes"]):
        parts.append(f"last trade implied YES {r['last_implied_yes'] * 100:.0f}%")
    if outcome:
        verdict = {True: "flow called it ✓", False: "flow was wrong ✗", None: "flow was split"}[r["flow_correct"]]
        parts.append(f"resolved {outcome} — {verdict}")
    return " · ".join(parts)


def build_replay(radar: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """One row per resolved market (from the radar frame) with its replay."""
    if radar is None or radar.empty:
        return pd.DataFrame()
    res = radar[(radar["phase"] == "resolved") | (radar["resolved"].fillna(False).astype(bool)
                                                   & ~radar["is_open"])].copy()
    if res.empty:
        return pd.DataFrame()
    t = prepare_trades(trades)
    rows = []
    for r in res.to_dict("records"):
        outcome = outcome_from_price(r.get("yes_price")) if r.get("phase") == "resolved" else None
        mt = t[t["market_id"] == r["market_id"]]
        rp = replay_market(mt, outcome, r.get("end_time"))
        rows.append({"market_id": r["market_id"], "outcome": outcome, **rp,
                     "replay": explain_replay(rp, outcome)})
    out = res.merge(pd.DataFrame(rows), on="market_id", how="left")
    return out.sort_values(["directional_trades", "volume_usdc"], ascending=False).reset_index(drop=True)


def replay_summary(rep: pd.DataFrame) -> dict:
    if rep is None or rep.empty:
        return {"resolved": 0, "with_outcome": 0, "with_flow": 0, "flow_correct": 0, "baseline_correct": 0,
                "baseline_side": None, "outcomes": {}, "price_calls": 0, "price_correct": 0}
    called = rep[rep["flow_correct"].notna()]
    pc = rep[rep["price_correct"].notna()]
    # honest baseline: always guessing the more common outcome on the same markets
    base_n = int(called["outcome"].value_counts().max()) if len(called) else 0
    base_side = called["outcome"].value_counts().idxmax() if len(called) else None
    return {"resolved": int(len(rep)), "with_outcome": int(rep["outcome"].notna().sum()),
            "with_flow": int(len(called)), "flow_correct": int(called["flow_correct"].astype(bool).sum()),
            "baseline_correct": base_n, "baseline_side": base_side,
            "outcomes": rep["outcome"].value_counts().to_dict(),
            "price_calls": int(len(pc)), "price_correct": int(pc["price_correct"].astype(bool).sum())}
