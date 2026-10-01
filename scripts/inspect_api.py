"""Read-only inspection of the real Panta API.

Usage:  python scripts/inspect_api.py
Prints counts, field names and one sample market. Never prints the API key.
Raw responses are saved to data/raw/ (git-ignored) for schema inspection.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panta_radar.api import PantaAPIError, PantaClient  # noqa: E402
from panta_radar.config import RAW_DIR, load_settings  # noqa: E402


def save_raw(name: str, payload) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"{ts}_{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def short(v, n=90):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def main() -> int:
    s = load_settings()
    print(f"base_url : {s.base_url}")
    print(f"api key  : {'FOUND ' + s.masked_key if s.api_key else 'MISSING'}")
    if not s.api_key:
        print("-> add PANTA_API_KEY=pk_test_... to .env")
        return 1
    c = PantaClient(s)
    try:
        cats = c.get_categories()
        print(f"\nGET /categories/        OK  {cats}")

        page = c.get_markets_page(limit=50)
        save_raw("markets_page1", page)
        items = page["items"]
        print(f"GET /markets/?limit=50  OK  items={len(items)} nextCursor={'yes' if page.get('nextCursor') else 'no'}")
        print(f"rate-limit headers: {c.last_rate_limit}")

        all_items = items if not page.get("nextCursor") else c.get_markets(max_pages=40)
        save_raw("markets_all", all_items)
        print(f"\nTOTAL markets (all pages): {len(all_items)}")
        print("by phase   :", dict(Counter(m.get("phase") for m in all_items)))
        print("by status  :", dict(Counter(m.get("status") for m in all_items)))
        print("by category:", dict(Counter(m.get("category") for m in all_items)))
        keys = Counter(k for m in all_items for k in m)
        print("\nFIELDS (present in N of markets, non-null in M):")
        for k, n in sorted(keys.items()):
            nn = sum(1 for m in all_items if m.get(k) not in (None, "", []))
            ex = next((m[k] for m in all_items if m.get(k) not in (None, "", [])), None)
            print(f"  {k:22s} {n:4d} / {nn:4d}   e.g. {short(ex, 60)}")
        if not all_items:
            print("No markets returned.")
            return 0

        def vol(m):
            try:
                return float(m.get("volumeUsdc") or 0)
            except (TypeError, ValueError):
                return 0.0
        open_items = [m for m in all_items if m.get("phase") in ("primary", "secondary")] or all_items
        sample = max(open_items, key=vol)
        print("\nSAMPLE market (list row, highest volume among open):")
        for k, v in sample.items():
            print(f"  {k:22s} {short(v)}")

        mid = sample.get("marketId")
        detail = c.get_market(mid)
        save_raw("market_detail_sample", detail)
        print("\nGET /markets/{id}/ OK — fields that differ from list row:")
        for k, v in detail.items():
            if sample.get(k) != v:
                print(f"  {k:22s} list={short(sample.get(k), 30)}  detail={short(v, 40)}")
        extra = set(detail) - set(sample)
        print(f"  detail-only fields: {sorted(extra)}")

        trades = c.get_market_trades(mid, limit=200)
        save_raw("market_trades_sample", trades)
        print(f"\nGET /markets/{{id}}/trades/?limit=200 OK  rows={len(trades)}")
        if trades:
            print("  trade fields:", sorted({k for t in trades for k in t}))
            print("  first row   :", short(trades[0], 300))
            bts = [t.get("blockTime") for t in trades if t.get("blockTime")]
            if bts:
                fmt = lambda x: datetime.fromtimestamp(x, timezone.utc).isoformat()
                print(f"  blockTime range: {fmt(min(bts))} .. {fmt(max(bts))}")
        print(f"\nAPI calls made: {c.calls}. Raw JSON saved under {RAW_DIR}")
        return 0
    except PantaAPIError as e:
        print(f"\nAPI ERROR: {e}")
        if e.status in (401, 403):
            print("Most likely cause: key missing/invalid/revoked in .env.")
        elif e.status is None:
            print("Most likely cause: network/firewall/proxy blocks live-api.panta.market.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
