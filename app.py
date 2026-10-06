"""Panta Signal Radar — Streamlit dashboard.   Run:  streamlit run app.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Streamlit re-executes app.py on every rerun but keeps imported modules cached;
# reload our own package so code changes under src/ take effect on a browser refresh.
import importlib  # noqa: E402

import panta_radar  # noqa: E402

for _name in ("config", "normalize", "api", "storage", "snapshots", "signals", "replay", "datastore", "collect",
              "reproduce", "publicsync"):
    _mod = sys.modules.get(f"panta_radar.{_name}")
    if _mod is not None:
        importlib.reload(_mod)

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.collect import collect_snapshot  # noqa: E402
from panta_radar.config import load_settings  # noqa: E402
from panta_radar.publicsync import SyncThrottle, sync_public_data  # noqa: E402
from panta_radar.normalize import display_title  # noqa: E402
from panta_radar.replay import MM_ROLE, build_replay, prepare_trades, replay_summary, tag_crowd, wallet_profiles  # noqa: E402
from panta_radar.snapshots import coverage_stats  # noqa: E402
from panta_radar.signals import build_radar  # noqa: E402
from panta_radar.storage import load_all_trades, load_markets, load_runs, load_snapshots, load_trades  # noqa: E402

SERIES = "#2a78d6"
st.set_page_config(page_title="Panta Signal Radar", page_icon="📡", layout="wide")

st.markdown("""
<style>
.block-container {padding-top: 2rem;}
.radar-sub {color: #6b6b66; margin-top: -0.6rem; font-size: 1.05rem;}
.radar-msg {border-left: 3px solid #2a78d6; padding: .4rem .8rem; margin: .6rem 0 1rem 0; font-size: .95rem;}
.powered {font-size: .85rem; color: #6b6b66;}
.powered b {color: inherit;}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ data
@st.cache_data(ttl=30, show_spinner=False)
def load_all(db: str, db_mtime: float):
    return load_markets(db), load_snapshots(db), load_runs(db), load_all_trades(db)


def _secrets_to_env() -> None:
    """Streamlit Community Cloud exposes root-level secrets as env vars; copy the one flag we use
    explicitly in case it is only available through st.secrets."""
    import os
    if "PANTA_RADAR_PUBLIC_MODE" in os.environ:
        return
    try:
        v = st.secrets.get("PANTA_RADAR_PUBLIC_MODE")
    except Exception:  # noqa: BLE001 - no secrets file locally
        v = None
    if v is not None:
        os.environ["PANTA_RADAR_PUBLIC_MODE"] = str(v)


_secrets_to_env()
settings = load_settings()
DB = settings.db_path
PUBLIC = settings.public_mode


@st.cache_resource(show_spinner=False)
def sync_throttle() -> SyncThrottle:
    """One throttle per server process: the public data branch is fetched at most every 10 min."""
    return SyncThrottle()


def db_mtime() -> float:
    return DB.stat().st_mtime if DB.exists() else 0.0

# ------------------------------------------------------------------ header
h1, h2 = st.columns([4, 1])
with h1:
    st.title("📡 Panta Signal Radar")
    st.markdown('<div class="radar-sub">Real-time intelligence for prediction markets.</div>', unsafe_allow_html=True)
with h2:
    st.markdown('<div class="powered" style="text-align:right;padding-top:2.2rem">Powered by <b>Panta</b></div>',
                unsafe_allow_html=True)
st.markdown('<div class="radar-msg">Panta answers <i>“What markets exist?”</i> — '
            'Signal Radar answers <b>“What changed, what matters, and where should I look first?”</b></div>',
            unsafe_allow_html=True)

# ------------------------------------------------------------------ public bootstrap / sync
sync = None
if PUBLIC:
    # Rebuilds the ephemeral SQLite from the public `data` branch + committed backfills.
    # No Panta API call, no API key; throttled so reruns do not trigger git/HTTP fetches.
    with st.spinner("Synchronizing public data…"):
        sync = sync_throttle().maybe_sync(lambda: sync_public_data(DB))
    if sync is not None and sync.fatal:
        st.error("Public data could not be loaded and no local copy exists yet. "
                 f"Please reload in a minute. ({sync.error})")
        st.stop()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.subheader("Data")
    if PUBLIC:
        st.info("Public read-only mode · data synchronized from the GitHub collector.")
        st.caption("Source: GitHub collector (`data` branch) + committed trade backfill · no visitor triggers a "
                   "Panta API call.")
        if sync is not None and not sync.ok:
            st.warning("Latest sync failed; showing the last synchronized data.", icon="⚠️")
    else:
        st.caption(f"API key: `{settings.masked_key}` · DB: `{DB.name}`")
        max_enrich = st.slider("Markets to enrich per refresh", 5, 60, 40, 5,
                               help="Open markets get a detail call (spot price) and a trade-tape call. "
                                    "Panta read limit ≈120 requests/min.")
        if st.button("🔄 Refresh from Panta", type="primary", width="stretch",
                     disabled=not settings.api_key):
            bar = st.progress(0.0, text="Starting…")
            try:
                res = collect_snapshot(PantaClient(settings), max_enrich=max_enrich,
                                       progress=lambda f, m: bar.progress(min(f, 1.0), text=m[:60]))
                st.success(f"Snapshot #{res['run_id']}: {res['markets_seen']} markets, "
                           f"{res['markets_enriched']} enriched, {res['api_calls']} API calls")
                for e in res["errors"][:3]:
                    st.warning(e)
                st.cache_data.clear()
            except PantaAPIError as e:
                st.error(f"Panta API error: {e}")
        if not settings.api_key:
            st.error("PANTA_API_KEY missing in .env")
        if (settings.api_key or "").startswith("pk_test_"):
            st.info("Test key detected: Panta serves **sandbox fixtures** for `pk_test_` keys. "
                    "Use a `pk_live_` key for the real market catalog.")

markets, snaps, runs, all_trades = load_all(str(DB), db_mtime())
if PUBLIC:
    with st.sidebar:
        last = pd.to_datetime(snaps["snapshot_ts"], utc=True).max() if not snaps.empty else None
        st.caption(f"Last stored snapshot: {last:%Y-%m-%d %H:%M} UTC" if last is not None else
                   "Last stored snapshot: —")
        st.caption("Scheduled every 15 min; actual GitHub Actions cadence is best-effort.")
if snaps.empty:
    st.info("No snapshots yet. " + ("The public collector has not published data yet." if PUBLIC else
            "Click **Refresh from Panta** in the sidebar (or run `python scripts/fetch_snapshot.py`)."))
    st.stop()

now = datetime.now(timezone.utc)
radar = build_radar(markets, snaps, now=now, trades=all_trades)
prepared = tag_crowd(prepare_trades(all_trades)) if not all_trades.empty else pd.DataFrame()
profiles = wallet_profiles(prepared) if not prepared.empty else pd.DataFrame()
radar["market"] = [display_title(r) for r in radar.to_dict("records")]
radar["ends_in"] = [("ended" if h <= 0 else f"in {h:.0f}h" if h < 48 else f"in {h / 24:.0f}d") if pd.notna(h) else "—"
                    for h in radar["hours_left"]]
radar["d_prob"] = [f"{x:+.1f} pp" if pd.notna(x) else "—" for x in radar["delta_pp"]]
radar["d_vol"] = [f"{x:+,.2f}" if pd.notna(x) else "—" for x in radar["volume_delta"]]
last_run = runs.iloc[-1] if not runs.empty else None

# ------------------------------------------------------------------ filters
f0, f1, f2, f3, f4 = st.columns([1.3, 2, 2, 3, 1.6])
open_only = f0.toggle("Open only", value=True, help="Hide resolved / closed markets")
cats = sorted(radar["category"].dropna().unique().tolist())
phases = sorted(radar["phase"].dropna().unique().tolist())
sel_cat = f1.multiselect("Category", cats)
sel_phase = f2.multiselect("Phase", phases)
query = f3.text_input("Search", placeholder="market title…")
min_trades = f4.number_input("Min trades (24h)", 0, 10_000, 0)

view = radar.copy()
if open_only:
    view = view[view["is_open"]]
if sel_cat:
    view = view[view["category"].isin(sel_cat)]
if sel_phase:
    view = view[view["phase"].isin(sel_phase)]
if query:
    view = view[view["market"].str.contains(query, case=False, regex=False)
                | view["market_id"].str.contains(query, case=False, regex=False)]
if min_trades:
    view = view[view["trades_24h"].fillna(0) >= min_trades]

history_ready = bool(radar["has_history"].any())
if not history_ready:
    st.warning("⏳ **Historical signal data is accumulating.** Movement and volume-flow signals need at least "
               "two snapshots — refresh again later (or run `fetch_snapshot.py --loop 15`).")


def table(df: pd.DataFrame, cols: list[str], height: int | None = None):
    show = df.copy()
    show["probability"] = show["yes_price"] * 100
    cfg = {
        "market": st.column_config.TextColumn("Market", width="large"),
        "ends_in": "Ends",
        "d_prob": "Δ Prob.", "d_vol": "Δ Volume",
        "category": "Category", "phase": "Phase",
        "probability": st.column_config.NumberColumn("YES prob.", format="%.1f%%"),
        "delta_pp": st.column_config.NumberColumn("Δ Prob.", format="%+.1f pp"),
        "trades_24h": st.column_config.NumberColumn("Crowd trades 24h", format="%d"),
        "tape_trades_24h": st.column_config.NumberColumn("All prints 24h", format="%d",
                                                         help="Including seed pairs and prints by wallets with market-making behaviour"),
        "trades_1h": st.column_config.NumberColumn("Trades 1h", format="%d"),
        "accel_x": st.column_config.NumberColumn("Pace ×", format="%.1fx"),
        "volume_usdc": st.column_config.NumberColumn("Volume (USDC)", format="%.2f"),
        "volume_delta": st.column_config.NumberColumn("Δ Volume", format="%+.2f"),
        "signal": "Signal",
        "attention_score": st.column_config.ProgressColumn("Attention", min_value=0, max_value=100, format="%d"),
        "reason": st.column_config.TextColumn("Why", width="large"),
        "start_time": st.column_config.DatetimeColumn("Opened (UTC)", format="YYYY-MM-DD HH:mm"),
        "end_time": st.column_config.DatetimeColumn("Ends (UTC)", format="YYYY-MM-DD HH:mm"),
    }
    kw = {"height": height} if height else {}
    st.dataframe(show[cols], column_config=cfg, hide_index=True, width="stretch", **kw)


tabs = st.tabs(["Overview", "Movers", "Activity", "New Markets", "Resolved Replay", "Who's Trading", "Market Detail"])

# ------------------------------------------------------------------ Overview
with tabs[0]:
    open_n = int(radar["is_open"].sum())
    sig_n = int((pd.to_numeric(radar["attention_score"], errors="coerce").fillna(0) >= 20).sum())
    k = st.columns(5)
    k[0].metric("Total markets", len(radar))
    k[1].metric("Open markets", open_n)
    k[2].metric("Enriched last run", int(last_run["markets_enriched"]) if last_run is not None else 0)
    k[3].metric("Signals (score ≥ 20)", sig_n)
    lr = pd.to_datetime(last_run["finished_at"], utc=True) if last_run is not None else None
    k[4].metric("Last refresh", f"{(now - lr).total_seconds() / 60:.0f} min ago" if lr is not None else "—",
                help=f"{lr:%Y-%m-%d %H:%M:%S} UTC · {len(runs)} snapshots stored" if lr is not None else None)
    if not profiles.empty:
        mmw = profiles[profiles["role"] == MM_ROLE]
        if len(mmw):
            st.info(f"**Tape integrity:** {mmw['share_of_tape'].sum():.0%} of all shares on the Panta trade tape come "
                    f"from {len(mmw)} wallet{'s' if len(mmw) > 1 else ''} exhibiting market-making behaviour "
                    f"(liquidity seeding, not conviction). Signal Radar counts **crowd trades only** — "
                    f"see *Who's Trading*.")
    cov = coverage_stats(runs)
    if cov["runs"] >= 2:
        st.caption(f"Data coverage: {cov['runs']} snapshots, {cov['first']:%Y-%m-%d %H:%M} → {cov['last']:%Y-%m-%d %H:%M} "
                   f"UTC · median gap {cov['median_gap_h']:.1f}h · largest gap {cov['max_gap_h']:.1f}h. "
                   "Collection is scheduled every 15 min via GitHub Actions; actual cadence is best-effort and "
                   "may be delayed, so movements are measured between the snapshots that exist.")
    st.subheader("Top signals")
    top = view[view["is_open"]].head(15)
    if top.empty:
        st.caption("No open markets match the filters.")
    else:
        table(top, ["market", "probability", "d_prob", "trades_24h", "volume_usdc", "ends_in", "signal",
                    "attention_score", "reason"])

# ------------------------------------------------------------------ Movers
with tabs[1]:
    mv = view[view["delta_pp"].notna()].copy()
    if mv.empty:
        st.info("Historical signal data is accumulating — movers appear after the second snapshot "
                "of a market with a spot price.")
    else:
        mv = mv.reindex(mv["delta_pp"].abs().sort_values(ascending=False).index)
        table(mv, ["market", "probability", "d_prob", "d_vol", "signal", "attention_score", "reason"])

# ------------------------------------------------------------------ Activity
with tabs[2]:
    act = view[view["trades_24h"].notna()].sort_values(["trades_24h", "volume_usdc"], ascending=False)
    if act.empty:
        st.info("No trade-tape data yet for the filtered markets.")
    else:
        if act["trades_24h"].fillna(0).sum() == 0:
            st.caption("Trade tapes fetched — no trades in the last 24h for these markets.")
        table(act, ["market", "trades_24h", "tape_trades_24h", "trades_1h", "accel_x", "volume_usdc", "d_vol",
                    "signal", "attention_score", "reason"])

# ------------------------------------------------------------------ New
with tabs[3]:
    nw = view[view["start_time"].notna()].sort_values("start_time", ascending=False)
    st.caption("Sorted by Panta `startTime`. Markets that opened in the last 72h are flagged NEW.")
    if nw.empty:
        st.info("No markets with a start time.")
    else:
        table(nw.head(30), ["market", "category", "phase", "start_time", "end_time", "probability",
                            "volume_usdc", "signal"])

# ------------------------------------------------------------------ Resolved Replay
with tabs[4]:
    st.caption("Causal replay: for each market, wallet roles and seed pairs are computed only from trades at or "
               "before that market's close — no future information leaks into the result.")
    st.markdown("**Did the money see it coming?** For every resolved market we replay the real Panta trade tape "
                "up to the close, drop liquidity-seeding trades (same wallet buying YES and NO in equal size within "
                "seconds), and compare where the directional flow went with the actual outcome.")
    rep = build_replay(radar, all_trades)
    if rep.empty:
        st.info("No resolved markets in the local database yet.")
    else:
        min_bets = st.slider("Minimum directional trades per market", 1, 20, 1,
                             help="Thin markets are noisy — raise this to look only at markets with real flow.")
        rep = rep[(rep["directional_trades"] >= min_bets) | (min_bets <= 1)]
        sm = replay_summary(rep)
        if sm["naive_with_flow"] and sm["with_flow"]:
            n1, n2, n3 = st.columns(3)
            n1.metric("Naive tape reading", f"{sm['naive_correct']}/{sm['naive_with_flow']} correct",
                      f"{sm['naive_correct'] / sm['naive_with_flow']:.0%}", delta_color="off",
                      help="All prints except seed pairs — what a typical dashboard counts")
            n2.metric("Crowd only (as-of market-making wallets removed)", f"{sm['flow_correct']}/{sm['with_flow']} correct",
                      f"{sm['flow_correct'] / sm['with_flow']:.0%}", delta_color="off")
            n3.metric(f"Baseline: always {sm['baseline_side']}", f"{sm['baseline_correct']}/{sm['with_flow']} correct",
                      f"{sm['baseline_correct'] / sm['with_flow']:.0%}", delta_color="off")
            # Display-only conclusion built from replay_summary: plain differences, no significance label
            crowd_pct = sm["flow_correct"] / sm["with_flow"] * 100
            base_pct = sm["baseline_correct"] / sm["with_flow"] * 100
            gap = sm["flow_correct"] - sm["baseline_correct"]
            gap_txt = f"{gap:+d} market{'s' if abs(gap) != 1 else ''} ({crowd_pct - base_pct:+.1f} pp)"
            with st.container(border=True):
                st.markdown(f"**Crowd-only flow: {sm['flow_correct']}/{sm['with_flow']} ({crowd_pct:.1f}%) vs "
                            f"always-{sm['baseline_side']} baseline: {sm['baseline_correct']}/{sm['with_flow']} "
                            f"({base_pct:.1f}%) — {gap_txt}.** Small sample; no significance test.")
        k = st.columns(4)
        k[0].metric("Resolved / closed markets", sm["resolved"])
        k[1].metric("With known outcome", sm["with_outcome"])
        k[2].metric("Flow leaned one way", sm["with_flow"],
                    help="Markets with directional trades whose YES share of flow was outside 45-55%")
        k[3].metric("Flow matched outcome", f"{sm['flow_correct']} / {sm['with_flow']}" if sm["with_flow"] else "—",
                    f"{sm['flow_correct'] / sm['with_flow'] * 100:.0f}%" if sm["with_flow"] else None,
                    delta_color="off")
        if sm["with_flow"]:
            st.caption(
                f"Baseline for honesty: always guessing **{sm['baseline_side']}** on the same {sm['with_flow']} "
                f"markets would be right {sm['baseline_correct']} times "
                f"({sm['baseline_correct'] / sm['with_flow'] * 100:.0f}%). Outcomes overall: "
                + ", ".join(f"{k} {v}" for k, v in sm["outcomes"].items())
                + ". Flow is weighted by USDC where the tape has it, otherwise by shares (shares overweight the "
                  "cheaper side). Small sample — read as evidence, not proof.")
        if all_trades.empty:
            st.warning("No trade tapes stored yet — run `backfill.bat` (or `scripts/backfill_trades.py`).")
        show = rep.copy()
        show["lean"] = show["yes_lean"] * 100
        # text column so missing trade-implied prices read "—" instead of "None"
        show["last_px"] = [f"{x * 100:.0f}%" if pd.notna(x) else "—" for x in show["last_implied_yes"]]
        show["verdict"] = show["flow_correct"].map({True: "✓ called it", False: "✗ wrong"}).fillna("—")
        mm = show["mm_trades"].fillna(0).astype(int) if "mm_trades" in show.columns else 0
        show["mm_prints"] = mm
        # short scan-friendly note; the full narrative stays available in the expander below
        show["note"] = np.where(show["directional_trades"].fillna(0) == 0,
                                np.where((show["seed_trades"].fillna(0) + mm) > 0, "seeding / market-making only",
                                         "no trades"), "")
        st.dataframe(show[["market", "outcome", "verdict", "lean", "last_px", "directional_trades", "wallets",
                           "seed_trades", "mm_prints", "note", "category", "volume_usdc"]],
                     hide_index=True, width="stretch",
                     column_config={
                         "market": st.column_config.TextColumn("Market", width="large"),
                         "category": "Category", "outcome": "Outcome",
                         "verdict": "Flow vs outcome",
                         "lean": st.column_config.ProgressColumn("Crowd flow → YES", min_value=0, max_value=100,
                                                                 format="%.0f%%"),
                         "last_px": st.column_config.TextColumn(
                             "Last trade YES", help="Trade-implied YES price of the last crowd trade with a USDC amount"),
                         "directional_trades": st.column_config.NumberColumn("Crowd bets", format="%d"),
                         "wallets": st.column_config.NumberColumn("Wallets", format="%d"),
                         "seed_trades": st.column_config.NumberColumn("Seed prints", format="%d"),
                         "mm_prints": st.column_config.NumberColumn(
                             "MM prints", format="%d",
                             help="Prints by wallets that showed market-making behaviour as of the market's close"),
                         "note": "Note",
                         "volume_usdc": st.column_config.NumberColumn("Volume (USDC)", format="%.2f"),
                     })
        with st.expander("Full replay narrative per market"):
            st.dataframe(show[["market", "replay"]], hide_index=True, width="stretch",
                         column_config={"market": st.column_config.TextColumn("Market", width="medium"),
                                        "replay": st.column_config.TextColumn("Replay", width="large")})

# ------------------------------------------------------------------ Who's trading
with tabs[5]:
    st.markdown("**Who is actually on the tape?** Every wallet seen in Panta trade tapes, with an explainable role. "
                "A wallet is labelled **market-making behaviour** if it seeds YES+NO pairs across many markets or holds a dominant "
                "share of all shares traded. Its prints are excluded from every crowd signal.")
    if profiles.empty:
        st.info("No trade tapes stored yet — run `backfill.bat`.")
    else:
        mm_n = int((profiles["role"] == MM_ROLE).sum())
        k = st.columns(4)
        k[0].metric("Wallets on the tape", len(profiles))
        k[1].metric("Market-making behaviour", mm_n)
        k[2].metric("Their share of tape", f"{profiles.loc[profiles['role'] == MM_ROLE, 'share_of_tape'].sum():.0%}")
        k[3].metric("Seed-pair prints", int(prepared["is_seed"].sum()) if not prepared.empty else 0)
        pv = profiles.copy()
        pv["share_pct"] = pv["share_of_tape"] * 100
        pv["wallet_short"] = pv["wallet"].str[:6] + ".." + pv["wallet"].str[-4:]
        cols = ["wallet_short", "role", "trades", "markets", "share_pct", "seed_ratio", "no_ratio", "why", "wallet"]

        def _emphasize(row):  # display only: highlight wallets labelled market-making behaviour
            style = "background-color: rgba(235, 161, 0, 0.16); font-weight: 600" if row["role"] == MM_ROLE else ""
            return [style] * len(row)
        if mm_n:
            top = pv[pv["role"] == MM_ROLE].iloc[0]
            st.markdown(f"**Dominant wallet:** `{top['wallet']}` — {top['share_of_tape']:.0%} of all shares, "
                        f"{int(top['markets'])} markets, {top['seed_ratio']:.0%} of its prints are seed pairs "
                        "(highlighted below).")
        st.dataframe(pv[cols].style.apply(_emphasize, axis=1),
                     hide_index=True, width="stretch",
                     column_config={"wallet_short": "Wallet",
                                    "role": st.column_config.TextColumn("Role", width="medium"),
                                    "trades": st.column_config.NumberColumn("Prints", format="%d"),
                                    "markets": st.column_config.NumberColumn("Markets", format="%d"),
                                    "share_pct": st.column_config.ProgressColumn("Share of tape", min_value=0,
                                                                                 max_value=100, format="%.1f%%"),
                                    "seed_ratio": st.column_config.NumberColumn("Seed pairs", format="percent"),
                                    "no_ratio": st.column_config.NumberColumn("NO side", format="percent"),
                                    "why": st.column_config.TextColumn("Why", width="large"),
                                    "wallet": st.column_config.TextColumn("Full address")})

# ------------------------------------------------------------------ Detail
with tabs[6]:
    opts = view if not view.empty else radar
    labels = {r.market_id: f"{r.market}  ·  {r.category or '—'}" for r in opts.itertuples()}
    mid = st.selectbox("Market", list(labels), format_func=labels.get)
    r = radar[radar["market_id"] == mid].iloc[0]
    hc1, hc2 = st.columns([1, 6])
    if isinstance(r.get("image"), str) and r["image"].startswith("http"):
        hc1.image(r["image"], width=110)
    hc2.markdown(f"### {r['market']}")
    hc2.caption(f"`{mid}` · {r.get('category') or '—'} · phase **{r.get('phase') or '—'}** · "
               f"{r.get('market_type') or ''} · {r.get('region') or ''} · ends {r['ends_in']}"
               + (f" · price source `{r.get('live_price_source')}` ({r.get('live_valuation_status')})"
                  if pd.notna(r.get("live_price_source")) else ""))
    notes = []
    if r.get("raw_status") is not None and r.get("raw_phase") != r.get("phase"):
        notes.append(f"latest raw API state is `{r.get('raw_phase')}/{r.get('raw_status')}`; analytical state stays "
                     f"**{r.get('phase')}** (a confirmed outcome was already observed)")
    if r.get("latest_row_valid") is False and pd.notna(r.get("valuation_ts")):
        notes.append(f"latest API row had no valuation (null price / zero volume); showing the last valid "
                     f"valuation from {pd.Timestamp(r['valuation_ts']):%Y-%m-%d %H:%M} UTC")
    if (r.get("state_flaps") or 0) > 0:
        notes.append(f"state flipped back from resolved {int(r['state_flaps'])}× in raw data")
    if notes:
        st.caption("Data quality: " + "; ".join(notes) + ".")
    c = st.columns(5)
    c[0].metric("YES probability", f"{r['yes_price'] * 100:.1f}%" if pd.notna(r["yes_price"]) else "N/A",
                f"{r['delta_pp']:+.1f} pp" if pd.notna(r["delta_pp"]) else None)
    c[1].metric("Volume (USDC)", f"{r['volume_usdc']:,.2f}" if pd.notna(r["volume_usdc"]) else "N/A",
                f"{r['volume_delta']:+,.2f}" if pd.notna(r["volume_delta"]) else None)
    c[2].metric("Trades 24h", int(r["trades_24h"]) if pd.notna(r["trades_24h"]) else "N/A")
    c[3].metric("Attention", int(r["attention_score"]) if pd.notna(r["attention_score"]) else "—")
    c[4].metric("Signal", r["signal"])
    st.markdown(f"**Why:** {r['reason']}")
    if r.get("description"):
        with st.expander("Market description"):
            st.write(r["description"])

    hist = snaps[snaps["market_id"] == mid].copy()
    hist["snapshot_ts"] = pd.to_datetime(hist["snapshot_ts"], utc=True)
    hp = hist.dropna(subset=["yes_price"])
    st.markdown("**YES probability — locally accumulated snapshots**")
    if len(hp) < 2:
        st.caption(f"{len(hp)} snapshot(s) with a price so far — the chart appears after the second one.")
    else:
        fig = go.Figure(go.Scatter(x=hp["snapshot_ts"], y=hp["yes_price"] * 100, mode="lines+markers",
                                   line=dict(color=SERIES, width=2), marker=dict(size=8, color=SERIES),
                                   hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>YES %{y:.1f}%<extra></extra>"))
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="YES %",
                          yaxis=dict(range=[0, 100], gridcolor="rgba(128,128,128,.15)"),
                          xaxis=dict(gridcolor="rgba(128,128,128,.15)"))
        st.plotly_chart(fig, width="stretch")

    tt = prepare_trades(all_trades[all_trades["market_id"] == mid]) if not all_trades.empty else pd.DataFrame()
    px = tt.dropna(subset=["implied_yes"]) if not tt.empty else tt
    px = px[~px["is_seed"]] if not px.empty else px
    st.markdown("**Trade-implied YES price — reconstructed from the trade tape** (USDC paid ÷ shares)")
    if px.empty:
        st.caption("No priced directional trades for this market.")
    else:
        fig2 = go.Figure(go.Scatter(
            x=px["block_time"], y=px["implied_yes"] * 100, mode="markers+lines",
            line=dict(color=SERIES, width=1, dash="dot"), marker=dict(size=9, color=SERIES),
            customdata=np.stack([px["direction"].str.upper(), px["usdc_amount"], px["shares"]], axis=-1),
            hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>YES %{y:.1f}%<br>bought %{customdata[0]} · "
                          "%{customdata[1]:.2f} USDC for %{customdata[2]:.2f} shares<extra></extra>"))
        if pd.notna(r.get("end_time")):
            fig2.add_vline(x=r["end_time"], line=dict(color="rgba(128,128,128,.6)", dash="dash"))
        fig2.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="YES %",
                           yaxis=dict(range=[0, 100], gridcolor="rgba(128,128,128,.15)"),
                           xaxis=dict(gridcolor="rgba(128,128,128,.15)"))
        st.plotly_chart(fig2, width="stretch")

    st.markdown("**Recent trades (Panta trade tape, stored locally)**")
    tr = load_trades(mid)
    if tr.empty:
        st.caption("No trades returned by Panta for this market yet.")
    else:
        tr = prepare_trades(tr).sort_values("block_time", ascending=False)
        tr["implied_yes"] = tr["implied_yes"] * 100
        st.dataframe(tr[["block_time", "direction", "shares", "usdc_amount", "implied_yes", "is_seed", "wallet"]],
                     hide_index=True, width="stretch",
                     column_config={"block_time": st.column_config.DatetimeColumn("Time (UTC)",
                                                                                   format="YYYY-MM-DD HH:mm:ss"),
                                    "direction": "Side", "shares": st.column_config.NumberColumn("Shares", format="%.2f"),
                                    "usdc_amount": st.column_config.NumberColumn("USDC", format="%.2f"),
                                    "implied_yes": st.column_config.NumberColumn("Implied YES", format="%.1f%%"),
                                    "is_seed": st.column_config.CheckboxColumn("Liquidity seed")})

st.divider()
st.markdown('<div class="powered">Powered by <b>Panta</b> · data from the Panta Markets API · '
            'deterministic signals, no paid AI · <a href="https://docs.panta.market/">docs.panta.market</a></div>',
            unsafe_allow_html=True)
