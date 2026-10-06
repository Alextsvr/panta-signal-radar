"""Temporal integrity for locally accumulated snapshots.

Three rules this module enforces (all motivated by real Panta API behaviour, Oct 2026):

1. ROW CONSISTENCY. A "latest" or "reference" snapshot is always ONE physical stored row.
   We never use pandas ``groupby().first()/last()``, which pick the first/last non-null value
   *per column* and can silently assemble a synthetic row from different timestamps.

2. VALUATION VALIDITY. The catalog endpoint intermittently returns degraded rows for a
   market: ``yesPrice = null``, ``volumeUsdc = "0.00"``, ``totalVolumeUsdc = null``,
   ``priceSource = null`` (710 of 2,140 stored rows on Oct 1-6). Such a row is kept verbatim in
   storage (raw API state), but it is NOT a valuation: it can never be used for movement,
   volume flow or outcome. A probability of 0 or 1 is legitimate (resolved outcomes).

3. RAW STATE vs ANALYTICAL STATE. The same market can alternate
   ``resolved -> secondary_active -> resolved`` between runs (84 of 91 markets flapped).
   ``raw_phase``/``raw_status`` always report what the API said last. ``analytical_state`` is
   sticky: once a confirmed outcome row (``priceSource == "resolved_outcome"``) has been seen,
   the market stays ``resolved`` for analytics; later transient live-looking rows are counted
   in ``state_flaps`` instead of creating live signals.

Movement is computed live-to-live only: both rows must be valid, non-outcome valuations in the
SAME price regime (``primary`` bonding-curve price vs ``secondary`` last-trade price), so a
resolution (0/1) or a regime switch is never reported as a market move.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

LIVE_PHASES = ("primary", "secondary")
OUTCOME_SOURCE = "resolved_outcome"


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce")


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index, dtype="object")


def valuation_mask(df: pd.DataFrame) -> pd.Series:
    """True where the row carries a usable valuation.

    Requires a YES price in [0, 1]. When the frame has ``total_volume_usdc`` (every row collected
    since Oct 1), it must be present too: the degraded API rows null it together with the price.
    ``api_price_source`` is not required, because rows stored before that column existed lack it.
    """
    yes = pd.to_numeric(_col(df, "yes_price"), errors="coerce")
    ok = yes.notna() & (yes >= 0) & (yes <= 1)
    if "total_volume_usdc" in df.columns:
        ok &= pd.to_numeric(df["total_volume_usdc"], errors="coerce").notna()
    return ok.fillna(False).astype(bool)


def is_valid_valuation(row: dict) -> bool:
    return bool(valuation_mask(pd.DataFrame([row])).iloc[0])


def outcome_mask(df: pd.DataFrame) -> pd.Series:
    """Rows that report a resolved OUTCOME (not a tradable price)."""
    src = _col(df, "api_price_source")
    yes = pd.to_numeric(_col(df, "yes_price"), errors="coerce")
    by_source = src == OUTCOME_SOURCE
    # legacy rows without priceSource: phase resolved + 0/1 price
    legacy = src.isna() & (_col(df, "phase") == "resolved") & yes.isin([0.0, 1.0])
    return (valuation_mask(df) & (by_source | legacy)).fillna(False).astype(bool)


def live_mask(df: pd.DataFrame) -> pd.Series:
    """Valid, tradable (non-outcome) valuations of a live phase."""
    return (valuation_mask(df) & ~outcome_mask(df) & _col(df, "phase").isin(LIVE_PHASES)).astype(bool)


def latest_rows(df: pd.DataFrame, mask: pd.Series | None = None) -> pd.DataFrame:
    """One complete physical row per market: the most recent one (optionally among `mask` rows)."""
    d = df if mask is None else df[mask]
    if d.empty:
        return d.iloc[0:0].copy()
    return (d.sort_values(["market_id", "snapshot_ts"], kind="mergesort")
             .drop_duplicates("market_id", keep="last").reset_index(drop=True))


def prepare_snapshots(snaps: pd.DataFrame) -> pd.DataFrame:
    s = snaps.copy()
    s["snapshot_ts"] = _ts(s["snapshot_ts"])
    s = s.dropna(subset=["snapshot_ts"])
    s["valid_valuation"] = valuation_mask(s)
    s["is_outcome"] = outcome_mask(s)
    s["is_live"] = live_mask(s)
    return s


def market_states(snaps: pd.DataFrame, markets: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per market: raw API state of the latest physical row + a stable analytical state.

    analytical_state:
      * ``resolved``  - a confirmed outcome row has been observed (sticky)
      * ``closed``    - the catalog flags the market resolved but no outcome row was seen yet
      * ``primary`` / ``secondary`` - live, from the latest physical row's phase
    """
    cols = ["market_id", "raw_phase", "raw_status", "raw_snapshot_ts", "latest_row_valid", "analytical_state",
            "outcome", "outcome_value", "resolved_at", "state_flaps", "outcome_conflict", "invalid_rows"]
    if snaps is None or snaps.empty:
        return pd.DataFrame(columns=cols)
    s = snaps if "is_outcome" in snaps.columns else prepare_snapshots(snaps)
    s = s.sort_values(["market_id", "snapshot_ts"], kind="mergesort")
    flagged = {}
    if markets is not None and not markets.empty and "resolved" in markets.columns:
        flagged = {m: bool(v) for m, v in zip(markets["market_id"], markets["resolved"]) if not pd.isna(v)}
    out = []
    for mid, g in s.groupby("market_id", sort=False):
        last = g.iloc[-1]
        oc = g[g["is_outcome"]]
        vals = sorted(set(pd.to_numeric(oc["yes_price"]).round(6)))
        if len(oc):
            first_res = oc["snapshot_ts"].iloc[0]
            later = g[g["snapshot_ts"] > first_res]
            flaps = int(((~later["is_outcome"]) & (later["is_outcome"].shift(1, fill_value=True))).sum())
            latest_outcome = float(oc["yes_price"].iloc[-1])
            state, outcome = "resolved", ("YES" if latest_outcome >= 0.5 else "NO")
        else:
            first_res, flaps, latest_outcome, outcome = pd.NaT, 0, math.nan, None
            if flagged.get(mid) or last.get("phase") == "resolved":
                state = "closed"
            else:
                state = last.get("phase") if last.get("phase") in LIVE_PHASES else (last.get("phase") or "unknown")
        out.append({"market_id": mid, "raw_phase": last.get("phase"), "raw_status": last.get("status"),
                    "raw_snapshot_ts": last["snapshot_ts"], "latest_row_valid": bool(last["valid_valuation"]),
                    "analytical_state": state, "outcome": outcome, "outcome_value": latest_outcome,
                    "resolved_at": first_res, "state_flaps": flaps, "outcome_conflict": len(vals) > 1,
                    "invalid_rows": int((~g["valid_valuation"]).sum())})
    return pd.DataFrame(out, columns=cols)


def movement_frame(snaps: pd.DataFrame, window_h: float) -> pd.DataFrame:
    """Live-to-live movement per market from two physical, valid, same-regime rows.

    latest   = most recent live valuation row
    reference = oldest live valuation row in [latest - window_h, latest) with the same phase
    Cumulative volume must not decrease; a lower newer value is flagged, never used as flow.
    """
    cols = ["market_id", "live_ts", "live_yes_price", "live_volume_usdc", "live_regime", "live_price_source",
            "live_valuation_status", "ref_ts", "ref_yes_price", "ref_volume_usdc", "n_snapshots",
            "n_live_snapshots", "volume_regression"]
    if snaps is None or snaps.empty:
        return pd.DataFrame(columns=cols)
    s = snaps if "is_live" in snaps.columns else prepare_snapshots(snaps)
    counts = s.groupby("market_id").size().rename("n_snapshots")
    live = s[s["is_live"]]
    latest = latest_rows(live)
    rows = []
    for r in latest.itertuples(index=False):
        g = live[(live["market_id"] == r.market_id) & (live["phase"] == r.phase)
                 & (live["snapshot_ts"] < r.snapshot_ts)
                 & (live["snapshot_ts"] >= r.snapshot_ts - pd.Timedelta(hours=window_h))]
        ref = g.sort_values("snapshot_ts", kind="mergesort").iloc[0] if len(g) else None
        lv, rv = getattr(r, "volume_usdc", math.nan), (ref["volume_usdc"] if ref is not None else math.nan)
        regression = bool(ref is not None and not pd.isna(lv) and not pd.isna(rv) and lv < rv - 1e-9)
        rows.append({"market_id": r.market_id, "live_ts": r.snapshot_ts, "live_yes_price": r.yes_price,
                     "live_volume_usdc": lv, "live_regime": r.phase,
                     "live_price_source": getattr(r, "api_price_source", None),
                     "live_valuation_status": getattr(r, "valuation_status", None),
                     "ref_ts": ref["snapshot_ts"] if ref is not None else pd.NaT,
                     "ref_yes_price": ref["yes_price"] if ref is not None else math.nan,
                     "ref_volume_usdc": rv, "n_live_snapshots": int((live["market_id"] == r.market_id).sum()),
                     "volume_regression": regression})
    mv = pd.DataFrame(rows, columns=[c for c in cols if c != "n_snapshots"])
    out = counts.reset_index().merge(mv, on="market_id", how="left")
    out["n_live_snapshots"] = out["n_live_snapshots"].fillna(0).astype(int)
    out["volume_regression"] = out["volume_regression"].eq(True)
    return out[cols]


def coverage_stats(runs: pd.DataFrame) -> dict:
    """Honest collection-cadence metadata (scheduled != executed)."""
    if runs is None or runs.empty:
        return {"runs": 0, "first": None, "last": None, "median_gap_h": math.nan, "max_gap_h": math.nan,
                "min_gap_h": math.nan}
    t = _ts(runs["started_at"]).dropna().sort_values()
    gaps = t.diff().dropna().dt.total_seconds() / 3600
    return {"runs": int(len(t)), "first": t.iloc[0], "last": t.iloc[-1],
            "median_gap_h": float(gaps.median()) if len(gaps) else math.nan,
            "max_gap_h": float(gaps.max()) if len(gaps) else math.nan,
            "min_gap_h": float(gaps.min()) if len(gaps) else math.nan}
