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


MM_MIN_MARKETS = 5        # a liquidity wallet works across many markets ...
MM_MIN_SEED_RATIO = 0.30  # ... and a large share of its prints are YES+NO seed pairs
MM_MIN_SHARE = 0.25       # or it alone holds >25% of all shares on the tape (with >=10 markets)


def wallet_profiles(t: pd.DataFrame) -> pd.DataFrame:
    """Per-wallet footprint on the tape and an explainable role: market maker vs crowd."""
    if t is None or t.empty:
        return pd.DataFrame(columns=["wallet", "trades", "markets", "shares", "share_of_tape", "seed_ratio",
                                     "usdc_ratio", "no_ratio", "role", "why"])
    g = t.groupby("wallet")
    w = pd.DataFrame({
        "trades": g.size(), "markets": g["market_id"].nunique(), "shares": g["shares"].sum(),
        "seed_ratio": g["is_seed"].mean(), "usdc_ratio": g["usdc_amount"].apply(lambda x: x.notna().mean()),
        "no_ratio": g["direction"].apply(lambda x: (x == "no").mean()),
    })
    w["share_of_tape"] = w["shares"] / w["shares"].sum() if w["shares"].sum() else 0.0
    roles, whys = [], []
    for r in w.itertuples():
        why = []
        if r.markets >= MM_MIN_MARKETS and r.seed_ratio >= MM_MIN_SEED_RATIO:
            why.append(f"{r.seed_ratio:.0%} of its {r.trades} prints are YES+NO seed pairs across {r.markets} markets")
        if r.share_of_tape >= MM_MIN_SHARE and r.markets >= 10:
            why.append(f"holds {r.share_of_tape:.0%} of all shares on the tape")
        roles.append("market maker" if why else "crowd")
        whys.append("; ".join(why))
    w["role"], w["why"] = roles, whys
    return w.reset_index().sort_values("shares", ascending=False).reset_index(drop=True)


def tag_crowd(t: pd.DataFrame, profiles: pd.DataFrame | None = None) -> pd.DataFrame:
    """Adds `is_mm` (wallet classified as market maker) and `is_crowd` (directional, not seed, not MM)."""
    t = t.copy()
    if t.empty:
        t["is_mm"] = pd.Series(dtype=bool)
        t["is_crowd"] = pd.Series(dtype=bool)
        return t
    profiles = wallet_profiles(t) if profiles is None else profiles
    mm = set(profiles.loc[profiles["role"] == "market maker", "wallet"])
    t["is_mm"] = t["wallet"].isin(mm)
    t["is_crowd"] = ~t["is_mm"] & ~t["is_seed"] & t["direction"].notna()
    return t


def outcome_from_price(yes_price) -> str | None:
    """Resolved rows carry yesPrice "1"/"0" with priceSource=resolved_outcome."""
    try:
        p = float(yes_price)
    except (TypeError, ValueError):
        return None
    if math.isnan(p):
        return None
    return "YES" if p >= 0.999 else "NO" if p <= 0.001 else None


def replay_market(trades: pd.DataFrame, outcome: str | None, end_time=None, crowd_only: bool = False) -> dict:
    """Directional flow before close vs the actual outcome for one market.
    crowd_only=False reproduces a naive tape reading (only seed pairs removed);
    crowd_only=True also drops every trade by wallets classified as market makers."""
    t = trades
    if end_time is not None and not pd.isna(end_time):
        t = t[t["block_time"] <= pd.Timestamp(end_time)]
    d = t[~t["is_seed"] & t["direction"].notna()]
    if crowd_only and "is_mm" in d.columns:
        d = d[~d["is_mm"]]
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
        "mm_trades": int(t["is_mm"].sum()) if "is_mm" in t.columns else 0,
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
        return ("Only market-maker / seeding prints — no crowd bets before close"
                if (r["seed_trades"] or r.get("mm_trades")) else "No trades on the tape")
    parts = [f"{r['directional_trades']} crowd trades from {r['wallets']} wallets"]
    if r.get("mm_trades"):
        parts.append(f"{r['mm_trades']} market-maker prints excluded")
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
    t = tag_crowd(prepare_trades(trades))
    rows = []
    for r in res.to_dict("records"):
        outcome = outcome_from_price(r.get("yes_price")) if r.get("phase") == "resolved" else None
        mt = t[t["market_id"] == r["market_id"]]
        naive = replay_market(mt, outcome, r.get("end_time"), crowd_only=False)
        rp = replay_market(mt, outcome, r.get("end_time"), crowd_only=True)
        rows.append({"market_id": r["market_id"], "outcome": outcome, **rp,
                     "naive_lean": naive["yes_lean"], "naive_correct": naive["flow_correct"],
                     "naive_trades": naive["directional_trades"],
                     "replay": explain_replay(rp, outcome)})
    out = res.merge(pd.DataFrame(rows), on="market_id", how="left")
    return out.sort_values(["directional_trades", "volume_usdc"], ascending=False).reset_index(drop=True)


def replay_summary(rep: pd.DataFrame) -> dict:
    if rep is None or rep.empty:
        return {"resolved": 0, "with_outcome": 0, "with_flow": 0, "flow_correct": 0, "baseline_correct": 0,
                "baseline_side": None, "outcomes": {}, "price_calls": 0, "price_correct": 0,
                "naive_with_flow": 0, "naive_correct": 0, "naive_baseline": 0}
    naive = rep[rep["naive_correct"].notna()] if "naive_correct" in rep.columns else rep.iloc[0:0]
    called = rep[rep["flow_correct"].notna()]
    pc = rep[rep["price_correct"].notna()]
    # honest baseline: always guessing the more common outcome on the same markets
    base_n = int(called["outcome"].value_counts().max()) if len(called) else 0
    base_side = called["outcome"].value_counts().idxmax() if len(called) else None
    return {"resolved": int(len(rep)), "with_outcome": int(rep["outcome"].notna().sum()),
            "with_flow": int(len(called)), "flow_correct": int(called["flow_correct"].astype(bool).sum()),
            "baseline_correct": base_n, "baseline_side": base_side,
            "outcomes": rep["outcome"].value_counts().to_dict(),
            "price_calls": int(len(pc)), "price_correct": int(pc["price_correct"].astype(bool).sum()),
            "naive_with_flow": int(len(naive)), "naive_correct": int(naive["naive_correct"].astype(bool).sum()),
            "naive_baseline": int(naive["outcome"].value_counts().max()) if len(naive) else 0}
