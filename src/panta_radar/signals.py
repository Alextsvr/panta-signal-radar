"""Deterministic, explainable signal engine.

Input : market catalog + locally accumulated snapshots (SQLite) + trade-tape summaries.
Output: one row per market with deltas, component scores, an Attention Score (0-100),
        a signal label and human-readable reasons.

Design rules
  * only real fields; missing -> NaN, never imputed
  * components are bounded to [0, 1] with fixed, documented scales (no fake precision)
  * a missing component contributes 0 — scores are NOT re-inflated when history is short
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

OPEN_PHASES = ("primary", "secondary")


@dataclass(frozen=True)
class ScoreConfig:
    window_h: float = 24.0          # look-back window for deltas
    # Scales calibrated on the live catalog (2026-10-01): open-market volume 0-390 USDC,
    # trade tapes of 0-12 rows, a handful of trades per market per day.
    move_full_pp: float = 10.0      # |Δprob| of 10pp => move component = 1
    trades_full: int = 10           # 10 trades / 24h => activity component = 1 (log scale)
    flow_full_usdc: float = 100.0   # +100 USDC volume in window => flow component = 1 (log scale)
    accel_full_x: float = 4.0       # last-hour pace 4x the 24h average => accel component = 1
    new_h: float = 72.0             # started within 72h => "new"
    closing_h: float = 48.0         # ends within 48h => "closing soon"
    min_trades_for_accel: int = 3
    w_move: float = 0.35
    w_activity: float = 0.25
    w_flow: float = 0.20
    w_accel: float = 0.10
    w_timing: float = 0.10


DEFAULT = ScoreConfig()


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce")


def _num(x) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return math.nan
    return f


def latest_and_reference(snaps: pd.DataFrame, window_h: float) -> pd.DataFrame:
    """For each market: latest snapshot + the oldest snapshot inside the window before it."""
    cols = ["market_id", "snapshot_ts", "yes_price", "volume_usdc"]
    if snaps is None or snaps.empty:
        return pd.DataFrame(columns=["market_id", "ref_ts", "ref_yes_price", "ref_volume_usdc", "n_snapshots"])
    s = snaps.copy()
    s["snapshot_ts"] = _ts(s["snapshot_ts"])
    s = s.dropna(subset=["snapshot_ts"]).sort_values("snapshot_ts")
    latest_ts = s.groupby("market_id")["snapshot_ts"].transform("max")
    in_window = (s["snapshot_ts"] < latest_ts) & (s["snapshot_ts"] >= latest_ts - pd.Timedelta(hours=window_h))
    ref = (s[in_window].groupby("market_id", as_index=False).first()[cols]
           .rename(columns={"snapshot_ts": "ref_ts", "yes_price": "ref_yes_price",
                            "volume_usdc": "ref_volume_usdc"}))
    n = s.groupby("market_id").size().rename("n_snapshots").reset_index()
    return n.merge(ref, on="market_id", how="left")


def _clip01(x: float) -> float:
    return 0.0 if (x is None or math.isnan(x)) else max(0.0, min(1.0, x))


def _log_scale(x: float, full: float) -> float:
    if x is None or math.isnan(x) or x <= 0:
        return 0.0
    return _clip01(math.log1p(x) / math.log1p(full))


def fmt_pp(x: float) -> str:
    return f"{x:+.1f}pp"


def fmt_dur(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.0f}h" if h >= 10 else f"{h:.1f}h"
    return f"{h / 24:.0f}d"


def score_row(r: dict, now: datetime, cfg: ScoreConfig = DEFAULT) -> dict:
    """Pure function: one market row -> components, score, label, reasons."""
    reasons: list[str] = []
    is_open = r.get("phase") in OPEN_PHASES and not bool(r.get("resolved"))

    # --- movement (needs ≥2 snapshots with prices)
    dpp = math.nan
    yes, ref_yes = _num(r.get("yes_price")), _num(r.get("ref_yes_price"))
    obs_h = math.nan
    ref_ts = r.get("ref_ts")
    snap_ts = r.get("snapshot_ts")
    if ref_ts is not None and snap_ts is not None and not pd.isna(ref_ts) and not pd.isna(snap_ts):
        obs_h = (pd.Timestamp(snap_ts) - pd.Timestamp(ref_ts)).total_seconds() / 3600
    if not math.isnan(yes) and not math.isnan(ref_yes):
        dpp = (yes - ref_yes) * 100
    c_move = _clip01(abs(dpp) / cfg.move_full_pp) if not math.isnan(dpp) else 0.0
    if not math.isnan(dpp) and abs(dpp) >= 0.5:
        reasons.append(f"YES probability moved {fmt_pp(dpp)} "
                       f"({ref_yes * 100:.0f}% → {yes * 100:.0f}%) over {fmt_dur(obs_h)}")

    # --- volume flow (needs ≥2 snapshots)
    vol, ref_vol = _num(r.get("volume_usdc")), _num(r.get("ref_volume_usdc"))
    dvol = vol - ref_vol if not (math.isnan(vol) or math.isnan(ref_vol)) else math.nan
    vol_growth = (dvol / ref_vol) if (not math.isnan(dvol) and ref_vol > 0) else math.nan
    c_flow = _log_scale(dvol, cfg.flow_full_usdc)
    if not math.isnan(dvol) and dvol > 0:
        g = f" ({vol_growth * 100:+.0f}%)" if not math.isnan(vol_growth) else " (from zero)"
        reasons.append(f"Volume +{dvol:,.2f} USDC{g} over {fmt_dur(obs_h)}")

    # --- activity from the real trade tape
    t24, t1 = _num(r.get("trades_24h")), _num(r.get("trades_1h"))
    capped = bool(r.get("tape_capped")) if not pd.isna(r.get("tape_capped")) else False
    c_act = _log_scale(t24, cfg.trades_full)
    if not math.isnan(t24) and t24 > 0:
        w = _num(r.get("wallets_24h"))
        who = f" from {int(w)} wallets" if not math.isnan(w) and w > 0 else ""
        reasons.append(f"{'≥' if capped else ''}{int(t24)} crowd trades in the last 24h{who}")
        ys = _num(r.get("crowd_yes_share_24h"))
        if not math.isnan(ys):
            reasons.append(f"{ys:.0%} of crowd shares bought YES")

    accel = math.nan
    if not math.isnan(t24) and t24 >= cfg.min_trades_for_accel and not math.isnan(t1):
        accel = t1 / (t24 / 24.0)
    c_accel = _clip01((accel - 1) / (cfg.accel_full_x - 1)) if not math.isnan(accel) else 0.0
    if not math.isnan(accel) and accel >= 1.5:
        reasons.append(f"Last-hour trading pace is {accel:.1f}x the 24h average")

    # --- timing (real catalog timestamps)
    start, end = r.get("start_time"), r.get("end_time")
    age_h = (now - pd.Timestamp(start)).total_seconds() / 3600 if start is not None and not pd.isna(start) else math.nan
    left_h = (pd.Timestamp(end) - now).total_seconds() / 3600 if end is not None and not pd.isna(end) else math.nan
    is_new = not math.isnan(age_h) and 0 <= age_h <= cfg.new_h
    closing = is_open and not math.isnan(left_h) and 0 < left_h <= cfg.closing_h
    c_timing = 1.0 if (is_new or closing) else 0.0
    if is_new:
        reasons.append(f"New market: opened {fmt_dur(age_h)} ago")
    if closing:
        reasons.append(f"Closing soon: ends in {fmt_dur(left_h)}")
    mm24 = _num(r.get("mm_prints_24h"))
    if is_open and not math.isnan(mm24) and mm24 > 0:
        reasons.append(f"{int(mm24)} market-maker prints ignored")
    if is_open and (math.isnan(t24) or t24 == 0):
        lt = r.get("last_trade_at")
        if lt is not None and not pd.isna(lt):
            reasons.append(f"Last trade {fmt_dur((now - pd.Timestamp(lt)).total_seconds() / 3600)} ago")
    if is_open and r.get("valuation_status") == "indicative":
        reasons.append("price is indicative (last secondary trade)")

    comps = {"c_move": c_move, "c_activity": c_act, "c_flow": c_flow, "c_accel": c_accel, "c_timing": c_timing}
    score = (cfg.w_move * c_move + cfg.w_activity * c_act + cfg.w_flow * c_flow
             + cfg.w_accel * c_accel + cfg.w_timing * c_timing) * 100
    score = int(round(score)) if is_open else None

    # label = strongest weighted contribution
    contrib = {"MOVER": cfg.w_move * c_move, "ACTIVE": cfg.w_activity * c_act + cfg.w_flow * c_flow,
               "SURGE": cfg.w_accel * c_accel,
               "CLOSING SOON" if closing else "NEW": cfg.w_timing * c_timing}
    if not is_open:
        label = "RESOLVED" if r.get("phase") == "resolved" or r.get("resolved") else (r.get("phase") or "—").upper()
    elif c_move >= 0.3 and (c_act >= 0.3 or c_flow >= 0.3):
        label = "TRENDING"
    elif max(contrib.values()) <= 0:
        label = "QUIET"
    else:
        label = max(contrib, key=contrib.get)

    has_history = not math.isnan(obs_h)
    if not has_history and is_open and not any("moved" in x for x in reasons):
        reasons.append("history accumulating")
    if not reasons:
        reasons.append("No movement or trading detected yet" if has_history
                       else "Historical signal data is accumulating (first snapshot)")
    return {**comps, "delta_pp": dpp, "volume_delta": dvol, "volume_growth": vol_growth,
            "observed_h": obs_h, "accel_x": accel, "age_h": age_h, "hours_left": left_h,
            "is_open": is_open, "is_new": is_new, "closing_soon": closing, "has_history": has_history,
            "attention_score": score, "signal": label, "reason": " · ".join(reasons)}


def crowd_activity(trades: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Per-market activity counted on CROWD trades only (no seed pairs, no market-maker wallets)."""
    from .replay import prepare_trades, tag_crowd  # local import keeps signals importable standalone
    cols = ["market_id", "trades_24h", "trades_1h", "wallets_24h", "last_trade_at", "crowd_yes_share_24h",
            "tape_trades_24h", "mm_prints_24h"]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=cols)
    t = tag_crowd(prepare_trades(trades))
    age_h = (pd.Timestamp(now) - t["block_time"]).dt.total_seconds() / 3600
    t24 = t[(age_h >= 0) & (age_h <= 24)]
    out = []
    for mid, g in t.groupby("market_id"):
        g24 = t24[t24["market_id"] == mid]
        c24 = g24[g24["is_crowd"]]
        c = g[g["is_crowd"]]
        sh = c24.groupby("direction")["shares"].sum()
        tot = sh.sum()
        out.append({"market_id": mid, "trades_24h": len(c24),
                    "trades_1h": int((age_h.loc[c24.index] <= 1).sum()),
                    "wallets_24h": c24["wallet"].nunique(),
                    "last_trade_at": c["block_time"].max() if len(c) else pd.NaT,
                    "crowd_yes_share_24h": float(sh.get("yes", 0) / tot) if tot else math.nan,
                    "tape_trades_24h": len(g24), "mm_prints_24h": int((g24["is_mm"] | g24["is_seed"]).sum())})
    return pd.DataFrame(out, columns=cols)


def build_radar(markets: pd.DataFrame, snaps: pd.DataFrame, now: datetime | None = None,
                cfg: ScoreConfig = DEFAULT, trades: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join catalog + latest snapshot + reference snapshot, score every market, rank.
    With `trades`, activity is recomputed on crowd trades only (market makers and seeds excluded)."""
    now = now or datetime.now(timezone.utc)
    if markets is None or markets.empty or snaps is None or snaps.empty:
        return pd.DataFrame()
    s = snaps.copy()
    s["snapshot_ts"] = _ts(s["snapshot_ts"])
    latest = s.sort_values("snapshot_ts").groupby("market_id", as_index=False).last()
    m = markets.drop(columns=[c for c in ("phase", "status") if c in markets.columns])
    df = latest.merge(m, on="market_id", how="left").merge(
        latest_and_reference(snaps, cfg.window_h), on="market_id", how="left")
    if trades is not None and not trades.empty:
        act = crowd_activity(trades, now)
        df = df.drop(columns=[c for c in act.columns if c != "market_id" and c in df.columns]) \
               .merge(act, on="market_id", how="left")
        for c in ("trades_24h", "trades_1h", "wallets_24h", "tape_trades_24h", "mm_prints_24h"):
            df[c] = df[c].fillna(0)
        df["tape_capped"] = False
    for c in ("start_time", "end_time", "resolution_time", "ref_ts", "last_trade_at", "first_seen_at"):
        if c in df.columns:
            df[c] = _ts(df[c])
    scored = pd.DataFrame([score_row(r, now, cfg) for r in df.to_dict("records")], index=df.index)
    out = pd.concat([df, scored], axis=1)
    out["rank_key"] = out["attention_score"].fillna(-1)
    return out.sort_values(["rank_key", "volume_usdc"], ascending=[False, False]).drop(columns="rank_key") \
              .reset_index(drop=True)
