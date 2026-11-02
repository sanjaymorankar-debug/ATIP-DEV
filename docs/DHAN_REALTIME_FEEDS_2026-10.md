# Dhan real-time market data — 31 Oct 2026

Tracker: DP-01, DP-03, DP-04, DP-05, DP-08, MON-07, BR-01. Testing: [TEST_REPORT_2026-10-31_DHAN_FEEDS.md](TEST_REPORT_2026-10-31_DHAN_FEEDS.md).

**Everything below is OFF until the owner turns it on**, after a test window on a live session. Nothing here places,
changes or blocks an order: no feed module imports the order code, and the broker-health feed checks are capped at
DEGRADED, so the RK-17 BUY gate decides on the same checks as before. The Data API plan these feeds need is Active
(Dhan profile, 31 Oct 2026: valid to 28 Oct 2027).

## The feeds

| Feed | Module | Switch (default) | What it does | Status row |
|---|---|---|---|---|
| Index WebSocket | `data/dhan_ws.py` | always on (the Mac) | 14 NSE indexes, Ticker packets, `index_levels` every 15 s | `indices` |
| Stock WebSocket | `data/stock_feed.py` | `live_feed.stocks_enabled` (off) | ≤100 stocks on **one** connection, Quote packets, `live_quotes`; REST failover | `stocks` |
| Tick capture | `data/ticks.py` | `live_feed.capture_ticks` (off) | every Quote packet to the lake `ticks`; kept **90 days** | `ticks` |
| 1-minute bars | `data/ticks.py` `MinuteBars` | `live_feed.minute_bars` (off) | bars from the WebSocket into `intraday_bars` (interval 1, source `ws`); REST fallback (source `dhan`) | `minute_bars` |
| 20-level depth | `data/depth20.py` | `depth20.enabled` (off) | ≤50 stocks on one connection, `depth20_snapshot` with DWI / OFI | `depth20` |
| Option chain | `data/derivatives_store.py` | `derivatives.option_chain_enabled` (off) | NIFTY + BANKNIFTY from Dhan's option-chain API every `chain_minutes` (NSE fallback), `option_chain_snapshot` | `option_chain` |
| 200-level depth | — | — | **Not built**: no strategy, order or score reads the order book, and Dhan's 200-level feed takes one connection per instrument | — |

All socket feeds connect only inside the feed window — 09:00–15:45 IST on an NSE trading day, never in the 21:00–06:00
stop window or under a manual stop (`data/dhan_ws.feed_window_open`) — and close when it ends.

## Turning a feed on (the test window)

In `atip_data/config.json` on the Mac, then restart ATIP (or wait for the 06:00 resume):

```json
"live_feed": {"stocks_enabled": true, "symbols": ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"],
              "max_symbols": 5, "capture_ticks": true, "minute_bars": true},
"depth20": {"enabled": true, "symbols": ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]},
"derivatives": {"option_chain_enabled": true, "chain_symbols": ["NIFTY", "BANKNIFTY"], "chain_minutes": 15,
                "chain_source": "dhan"}
```

Then watch the **Live** tab (dashboard), `/api/live-feeds`, or the phone's Live tab. Remove a block (or set its switch
to false) to turn it off again; its row then reads OFF.

## Dhan limits the design keeps to

| Limit (Dhan) | How ATIP stays inside it |
|---|---|
| 5,000 instruments per WebSocket connection | the stock feed takes at most 100 (`max_symbols` above 100 is cut, logged); the SDK subscribes 100 per message |
| 5 WebSocket connections per user | index + stock + depth = 3 at most; 200-level depth (one connection per instrument) not built |
| 50 instruments per 20-level connection | `depth20` uses one connection, at most 50 stocks |
| Quote APIs 1 call/s; Data APIs 5/s; option chain 1 per 3 s | every call takes its turn in `data/dhan.py` (`PACE_SECONDS`: quote 1.1 s, data 0.3 s, chain 3.1 s) |
| DH-904 "too many requests" | REST failover doubles its wait (60 → 300 s); the `dhan_data` circuit breaker opens after 6 failures; socket error bursts park the feed 5 → 30 min |
| 805–809 depth refusals | 805 / 807 park 30 min; 806 (no Data API), 808, 809 park until the next trading day |

## Health (MON-07, BR-06) and alerts

`ops/data_health.feeds()` reads `live_feed_status` for all six feeds and gives each a state:
**OFF** (switched off), **NOT STARTED** (switched on, no status yet), **IDLE** (outside the window), the feed's own mode
(**LIVE**, **WS**, **REST**, **PARTIAL**, **CONNECTING**, **RECONNECTING**, **PARKED**, **ERROR**), or **STALE** (in the
session, no data for 5 minutes — 2 for depth, two chain intervals for the option chain — or no status for 3 minutes).
It is shown in:

- `GET /health/market_data` → `feeds` (MON-07); DEGRADED when an enabled feed has a problem in the session
- broker health (every 5 min in the session) → checks `index_feed`, `stock_feed`, `tick_capture`, `minute_bars`,
  `depth20_feed`, `option_chain`; OFF is its own status and never raises the overall; one Telegram alert when a feed
  check turns DEGRADED and one when it recovers
- the dashboard's **Live** tab, `/market`, and `/m?tab=live` (the Android and iOS **Live** tab), all from
  `GET /api/live-feeds` (one read per 15 s shared by every tab, like `/api/live-quotes`; on the view-only server it
  shows the Mac's states from the copied database)

## The 30 Oct index-feed errors

| Time | Log | Cause | Change |
|---|---|---|---|
| 13:29:56 | `Index WebSocket error: no close frame received or sent` | Dhan dropped the socket; the SDK reconnected and resubscribed at 13:29:58, and `index_levels` kept its 15-second rows | not an ATIP fault: now a WARNING, and the reconnect is logged with its duration and counted |
| 15:45:07 | `Index WebSocket error: sent 1000 (OK); no close frame received` | ATIP's own close at the end of the window, reported by the SDK through `on_error` | fixed: callbacks from a socket ATIP has let go of are INFO and never counted toward an error burst (stock feed too) |

The 09:00–10:04 failures that morning were the expired token (DH-901 at 06:03), fixed by the token-refresh change of 30 Oct.

## Retention

`db/purge.py` SHORT tier (90 days, `retention.short_days`) now also deletes the lake's `ticks` partitions (files and
manifest rows, `data/lake.purge`) and `tick_capture_status`. `intraday_bars`, `live_quotes`, `depth20_snapshot` and
`option_chain_snapshot` were already on it.
