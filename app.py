"""Panta Signal Radar — Streamlit dashboard.   Run:  streamlit run app.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.collect import collect_snapshot  # noqa: E402
from panta_radar.config import load_settings  # noqa: E402
from panta_radar.normalize import display_title  # noqa: E402
from panta_radar.signals import build_radar  # noqa: E402
from panta_radar.storage import load_markets, load_runs, load_snapshots, load_trades  # noqa: E402

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
    return load_markets(db), load_snapshots(db), load_runs(db)


settings = load_settings()
DB = settings.db_path


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

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.subheader("Data")
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

markets, snaps, runs = load_all(str(DB), db_mtime())
if snaps.empty:
    st.info("No snapshots yet. Click **Refresh from Panta** in the sidebar "
            "(or run `python scripts/fetch_snapshot.py`).")
    st.stop()

now = datetime.now(timezone.utc)
radar = build_radar(markets, snaps, now=now)
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
        "trades_24h": st.column_config.NumberColumn("Trades 24h", format="%d"),
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


tabs = st.tabs(["Overview", "Movers", "Activity", "New Markets", "Market Detail"])

# ------------------------------------------------------------------ Overview
with tabs[0]:
    open_n = int(radar["is_open"].sum())
    sig_n = int((radar["attention_score"].fillna(0) >= 20).sum())
    k = st.columns(5)
    k[0].metric("Total markets", len(radar))
    k[1].metric("Open markets", open_n)
    k[2].metric("Enriched last run", int(last_run["markets_enriched"]) if last_run is not None else 0)
    k[3].metric("Signals (score ≥ 20)", sig_n)
    lr = pd.to_datetime(last_run["finished_at"], utc=True) if last_run is not None else None
    k[4].metric("Last refresh", f"{(now - lr).total_seconds() / 60:.0f} min ago" if lr is not None else "—",
                help=f"{lr:%Y-%m-%d %H:%M:%S} UTC · {len(runs)} snapshots stored" if lr is not None else None)
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
        table(act, ["market", "trades_24h", "trades_1h", "accel_x", "volume_usdc", "d_vol",
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

# ------------------------------------------------------------------ Detail
with tabs[4]:
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
               + (f" · price source `{r.get('api_price_source')}` ({r.get('valuation_status')})"
                  if pd.notna(r.get("api_price_source")) else ""))
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

    st.markdown("**Recent trades (Panta trade tape, stored locally)**")
    tr = load_trades(mid)
    if tr.empty:
        st.caption("No trades returned by Panta for this market yet.")
    else:
        tr["block_time"] = pd.to_datetime(tr["block_time"], utc=True)
        st.dataframe(tr[["block_time", "side", "yes_amount", "no_amount", "fee_paid", "is_primary", "wallet"]],
                     hide_index=True, width="stretch",
                     column_config={"block_time": st.column_config.DatetimeColumn("Time (UTC)",
                                                                                   format="YYYY-MM-DD HH:mm:ss")})

st.divider()
st.markdown('<div class="powered">Powered by <b>Panta</b> · data from the Panta Markets API · '
            'deterministic signals, no paid AI · <a href="https://docs.panta.market/">docs.panta.market</a></div>',
            unsafe_allow_html=True)
