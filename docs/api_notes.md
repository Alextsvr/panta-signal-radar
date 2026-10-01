# Panta API — confirmed notes

Status legend: **[verified]** = observed in a real response from this project ·
**[docs]** = from docs.panta.market / official playground source, not yet observed.

Sources: <https://docs.panta.market/llms-full.txt>, <https://github.com/Kaito-HQ/panta-api-playground>
(`.env.example`, `src/lib/types.ts`, `src/components/MarketsPanel.tsx`, `src/app/api/panta/[...path]/route.ts`).

## Base URL & environments
| | |
|---|---|
| Live | `https://live-api.panta.market/api/v1` **[verified]** |
| Staging | `https://staging-api.panta.market/api/v1` [playground `.env.example`] |
| Trailing slash | required on every path (`/markets/`, not `/markets`) [docs] — the client always appends it |

## Authentication
- Header `X-Api-Key: pk_test_… | pk_live_…` **[verified]** (alternatively `Authorization: Bearer <JWT>` [docs]).
- Without a key: `GET /categories/` → **HTTP 401** `{"code":"UNAUTHORIZED","message":"authentication required or invalid"}` **[verified, 2026-10-01]**. Read endpoints are *not* public.
- Server is Django REST Framework (browsable API page on 401) **[verified]**.

### ⚠️ pk_test_ keys return sandbox fixtures **[verified 2026-10-01]**
The docs say both prefixes are "accepted on the public API". In practice a `pk_test_` key returns:
- `/categories/` → `["crypto","politics","sports","entertainment"]` (docs list 8 categories)
- `/markets/` → exactly **1** market `TestMarket1111111111111111111111111111111` "Sandbox test market", volume `0.00`, prices `0.50`
- `/markets/{id}/` → adds `"disclaimer": "Test mode: this response uses sandbox fixtures and does not access Solana mainnet."`, `creatorAddress: "TestWallet111…"`, `onChain: null`
- `/markets/{id}/trades/` → `[]`

➡ The real catalog requires a **`pk_live_`** key (created the same way: Playground → API keys → env `live`).

## Rate limits
- Headers **[verified]**: `x-ratelimit-limit: 120`, `x-ratelimit-remaining`, `x-ratelimit-reset` (ISO-8601 UTC timestamp, not seconds).
- Families [docs]: read 120 / 60 s per account; `Retry-After` on 429. Client throttles to ≥0.55 s between calls and retries 429/5xx with backoff.

## Read-only endpoints used
| Endpoint | Params | Response | Status |
|---|---|---|---|
| `GET /categories/` | — | `{"categories": [str]}` | verified |
| `GET /markets/` | `limit` (≤50), `cursor`, `category`, `phase` (`primary`/`secondary`/`resolved`/`cancelled`), `createdBy=me` | `{"items": [Market], "nextCursor": str\|null}` | verified (1 page in sandbox; pagination not yet exercised) |
| `GET /markets/{marketId}/` | — | Market + spot prices (+ `creatorAddress`, `onChain`, `disclaimer`) | verified |
| `GET /markets/{marketId}/trades/` | `limit` (≤200) | `{"marketId", "items": [Trade]}` | verified (empty in sandbox) |
| `GET /wallets/{wallet}/trades/` | `limit` | `{"wallet", "items": [Trade]}` | docs only, not used |

Write endpoints (`/markets/create/*`, `/trades/` POST, `/claim/*`, `/account/keys/`) are **intentionally not used**.

## Market object (list row) **[verified field set]**
| Field | Example (real) | Notes |
|---|---|---|
| `marketId` | `"TestMarket111…"` | base58 event address |
| `category` | `"crypto"` | |
| `title`, `description` | | |
| `images` | `[]` | list of URLs |
| `phase` | `"primary"` | lifecycle: primary → secondary → resolved / cancelled |
| `status` | `"primary"` | **mirrors `phase`** in the real response (docs example shows `"open"`) |
| `marketType` | `"standard"` | |
| `startTime`, `endTime`, `resolutionTime` | `"2026-01-01T00:00:00Z"` | **ISO-8601 strings in reality**; docs show Unix seconds → normalizer accepts both |
| `region` | `"Global"` | |
| `resolved` | `false` | |
| `volumeUsdc` | `"0.00"` | human-decimal USDC string (cumulative) |
| `campaignId`, `createdByPartner` | `null`, `false` | |
| `yesPrice`, `noPrice` | `"0.50"` | decimal string 0..1 = implied probability. **Present on list rows in sandbox**; docs say list rows don't live-RPC prices (may be null in live) → detail call |
| `primaryYesPrice/NoPrice`, `secondaryYesPrice/NoPrice` | `"0.50"` / `null` | phase-specific prices |
| `totalVolumeUsdc`, `volumeUsdcBase`, `creationFee`, `oracle` | — | in playground types, **not present** in observed response |

## Trade object [docs; not yet observed — sandbox tape is empty]
`id, marketId, wallet, isPrimary, yesAmount, noAmount (share quantities, decimal strings), feePaid (USDC), blockTime (Unix s), signature, quoteAsset`.
Playground types also allow `kind, side, amountUsdc, amountUsdcBase`.

## What the API does NOT give us (→ why Signal Radar exists)
- **No price/probability history** endpoint → we accumulate snapshots locally (SQLite).
- **No volume time series** — only cumulative `volumeUsdc` → volume deltas come from our snapshots.
- **No `createdAt`** — "new market" uses `startTime`.
- **No liquidity / open-interest / order-book depth** fields.
- **No trade count field** — derived from the trade tape (≤200 rows per call; counts flagged as lower bounds when capped).
- Trade rows carry share amounts, not a USDC notional (unless `amountUsdc` appears) → no per-trade price.
