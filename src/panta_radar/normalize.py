"""Turn raw Panta JSON into flat, typed records. Missing fields -> None (never invented)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

OPEN_PHASES = {"primary", "secondary"}


def to_float(v: Any) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def usdc(human: Any, base: Any = None) -> float | None:
    """Prefer the human decimal string ("8.60"); fall back to 1e6 base units."""
    h = to_float(human)
    if h is not None:
        return h
    b = to_float(base)
    return b / 1_000_000 if b is not None else None


def ts_to_dt(v: Any) -> datetime | None:
    """Panta timestamps are Unix seconds (UTC). Tolerates ms and ISO strings."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            v = to_float(v)
            if v is None:
                return None
    f = to_float(v)
    if f is None or f <= 0:
        return None
    if f > 1e12:  # milliseconds
        f /= 1000
    try:
        return datetime.fromtimestamp(f, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def pick_yes_price(raw: dict) -> tuple[float | None, str | None]:
    """Implied YES probability (0..1) and which field it came from."""
    phase = raw.get("phase")
    order = ["yesPrice"]
    order += (["secondaryYesPrice", "primaryYesPrice"] if phase == "secondary"
              else ["primaryYesPrice", "secondaryYesPrice"])
    for k in order:
        p = to_float(raw.get(k))
        if p is not None and 0 <= p <= 1:
            return p, k
    return None, None


def normalize_market(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    mid = raw.get("marketId")
    if not mid:
        return None
    yes, src = pick_yes_price(raw)
    no = to_float(raw.get("noPrice"))
    images = raw.get("images") if isinstance(raw.get("images"), list) else []
    return {
        "market_id": str(mid),
        "title": (raw.get("title") or "").strip() or None,
        "description": raw.get("description") or None,
        "category": raw.get("category") or None,
        "phase": raw.get("phase") or None,
        "status": raw.get("status") or None,
        "market_type": raw.get("marketType") or None,
        "region": raw.get("region") or None,
        "resolved": raw.get("resolved") if isinstance(raw.get("resolved"), bool) else None,
        "start_time": ts_to_dt(raw.get("startTime")),
        "end_time": ts_to_dt(raw.get("endTime")),
        "resolution_time": ts_to_dt(raw.get("resolutionTime")),
        "volume_usdc": usdc(raw.get("volumeUsdc"), raw.get("volumeUsdcBase")),
        "total_volume_usdc": usdc(raw.get("totalVolumeUsdc"), raw.get("totalVolumeUsdcBase")),
        "yes_price": yes,
        "no_price": no,
        "price_source": src,                       # which field we read the probability from
        "api_price_source": raw.get("priceSource") or None,       # live: primary_last / secondary_last_trade / resolved_outcome
        "valuation_status": raw.get("valuationStatus") or None,   # live: complete / indicative
        "created_by_partner": raw.get("createdByPartner") if isinstance(raw.get("createdByPartner"), bool) else None,
        "image": images[0] if images else None,
    }


def display_title(m: dict) -> str:
    """Live catalog rows often have an empty title (80/87 on 2026-10-01) -> readable fallback."""
    def text(v) -> str:  # pandas turns SQL NULL into NaN (a float) -> treat any non-str as missing
        return v.strip() if isinstance(v, str) else ""

    t = text(m.get("title"))
    if t:
        return t
    mid = text(m.get("market_id")) or "?"
    return f"[untitled {text(m.get('category')) or 'market'}] {mid[:4]}..{mid[-4:]}"


def normalize_markets(raws: Iterable[dict]) -> list[dict]:
    """Normalize and de-duplicate by market_id (last occurrence wins)."""
    out: dict[str, dict] = {}
    for r in raws or []:
        m = normalize_market(r)
        if m:
            out[m["market_id"]] = m
    return list(out.values())


def is_open(m: dict) -> bool:
    return m.get("phase") in OPEN_PHASES and not m.get("resolved")


def to_shares(v: Any) -> float | None:
    """Share amounts on the live trade tape are 1e6 base units ("19677335" = 19.68 shares),
    although the docs describe human decimals ("10.00"). Decimal strings are taken as-is."""
    f = to_float(v)
    if f is None:
        return None
    if isinstance(v, str) and "." in v:
        return f
    return f / 1_000_000


def normalize_trade(raw: dict, market_id: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    yes, no = to_shares(raw.get("yesAmount")), to_shares(raw.get("noAmount"))
    side = raw.get("side")
    if not side and (yes or no):
        side = "yes" if (yes or 0) >= (no or 0) else "no"
    trade_id = raw.get("signature") or raw.get("id")
    bt = ts_to_dt(raw.get("blockTime"))
    if not trade_id:
        return None
    return {
        "trade_id": f"{trade_id}:{raw.get('id', '')}",
        "market_id": str(raw.get("marketId") or market_id or ""),
        "wallet": raw.get("wallet"),
        "is_primary": raw.get("isPrimary") if isinstance(raw.get("isPrimary"), bool) else None,
        "yes_amount": yes,
        "no_amount": no,
        # live rows carry a human "shares" string ("1.924115") next to base-unit yes/noAmount
        "shares": to_float(raw.get("shares")) if to_float(raw.get("shares")) is not None
        else ((yes or 0) + (no or 0) if (yes is not None or no is not None) else None),
        "fee_paid": to_float(raw.get("feePaid")),
        "usdc_amount": usdc(raw.get("amountUsdc"), raw.get("amountUsdcBase")),
        "side": side.lower() if isinstance(side, str) else side,
        "kind": raw.get("kind"),
        "block_time": bt,
        "signature": raw.get("signature"),
    }


def summarize_trades(trades: list[dict], now: datetime, limit_used: int) -> dict:
    """Activity measures from the real trade tape (relative to `now`)."""
    times = [t["block_time"] for t in trades if t.get("block_time")]
    h1 = [t for t in trades if t.get("block_time") and (now - t["block_time"]).total_seconds() <= 3600]
    h24 = [t for t in trades if t.get("block_time") and (now - t["block_time"]).total_seconds() <= 86400]
    return {
        "trades_sampled": len(trades),
        "trades_1h": len(h1),
        "trades_24h": len(h24),
        "shares_24h": sum((t.get("shares") or 0) for t in h24),
        "fees_24h": sum((t.get("fee_paid") or 0) for t in h24),
        "wallets_24h": len({t.get("wallet") for t in h24 if t.get("wallet")}),
        "last_trade_at": max(times) if times else None,
        "first_trade_at": min(times) if times else None,
        # if the tape is full, 24h counts are lower bounds
        "tape_capped": len(trades) >= limit_used,
    }
