"""
Futures short legs in the W2 backtest (QR-05 / QR-06, W40).

The W2 engine (backtest/engine.py) is a long cash book. A strategy's SHORT / COVER signals
-- pair trades (strategy_engine.kinds.PairsEvaluator) and long/short portfolios
(PortfolioEvaluator) with "short_via_futures": true, or a code strategy -- are simulated
here, and only when one arrives: a run with no SHORT never builds this book, never reads
the futures tables and gives exactly the result it did before (tests/test_w40_futures_backtest
pins the digests). This is research simulation only; LIVE futures do not exist (the W4 risk
engine and the adapter refuse them).

INSTRUMENT AND PRICES
    The near-month stock (or index) future of each session: fo_underlying_daily (the NSE F&O
    bhavcopy summary, data/derivatives.py) gives that session's near-month close (fut_close),
    expiry (near_expiry) and lot size. A contract's own series, and the NEXT month for a roll,
    come from fo_contract_daily (data/derivatives_store.py: futures of every listed expiry);
    where both have a session, fo_contract_daily names the near contract (the summary's
    near_expiry is the nearest OPTION expiry, a weekly one for indices). Prices are the
    bhavcopy's end-of-day closes: there is no futures open, so

        a futures leg FILLS AT THE FUTURES CLOSE of the session after the decision (the cash
        legs of the same decision fill at that session's OPEN -- the two legs of a pair are
        filled hours apart, and the bias report says so), moved against the order by the
        run's slippage model (SELL to open a short, BUY to cover).

MISSING FUTURES HISTORY -> THE LEG IS REFUSED, never priced off spot. Without a stored
contract there is no lot size (whole-lot sizing is impossible), no basis (a future trades at
spot plus carry -- typically 6-8% a year annualised, more around events) and no expiry; the
stock may simply not have been in the F&O segment then (membership changes, and today's list
would be survivorship bias). So:
    SHORT with no near-month contract on the decision session  -> refused (event)
    SHORT with no close for its contract on the fill session    -> dropped (event)
    every refusal is counted per symbol in bias_report.futures.refused and named in a bias
    warning ("... legs refused: no futures history ... the paired long legs, if any, traded
    unhedged"), so a pair whose short side could not trade is visible, not silently long.
    A HELD leg whose contract has no close one session keeps its last mark (a stale mark,
    counted); a COVER with no close waits for the next session (event), like a SELL with no bar.

SIZING  (at the decision close d, like a BUY)
    rupees = SHORT.value, else SHORT.quantity x the near-month close, else
             sizing.max_position_pct of equity; capped at max_position_pct of equity (the W4
             risk engine's _futures_leg rule; a cap is an event)
    lots   = floor(rupees / (near-month close x lot size)) -- whole lots, rounded DOWN; fewer
             than one lot -> refused (event), never rounded up. A quantity is floored to lots.
    At the fill the margin (below) plus the opening charges must fit free cash, else the lots
    are cut (event) or the leg is rejected. max_positions counts open shorts with longs.
    A SHORT is ignored while the symbol is held long or short (event when held long); a BUY
    is ignored while it is held short (event).

CONTRACT AT THE FILL
    the near-month contract; but never one inside the roll window (roll on) or with fewer than
    futures.min_days_to_expiry calendar days left (as the paper book: no new shorts in the
    expiry week): the NEXT month instead when its close is stored, else refused (event).
    If the lot size changed between decision and fill, lots are re-derived from the rupees.

CASH, MARGIN, MARK-TO-MARKET  (equity is right every session)
    open     margin = futures.margin_pct % x lots x lot size x fill price is BLOCKED from free
             cash (released at the close); opening charges are paid from cash
    daily    variation margin: each held leg is marked at its contract's close and
             (previous mark - close) x shares is paid into / out of free cash -- the exchange's
             daily settlement; the first mark is the fill price, so fill slippage shows that day
    close    the last variation to the exit fill, the margin released, closing charges paid
    equity   = free cash + long positions at the close + futures margin blocked. Equity points
             of a run with futures also carry futures_margin and futures_notional (|shares| x
             mark); exposure_pct is then GROSS: (longs + futures notional) / equity; unrealized
             adds the futures' (entry - mark) x shares. Free cash can go negative after a bad
             day (a broker would call margin): each such session is an event and counted.

ROLL / EXPIRY  (each session, at the futures close, before new shorts fill)
    futures.roll_days_before_expiry = N (default 2): on the first session with N or fewer NSE
    sessions left to the held contract's expiry (utils.trading_calendar; 0 = on expiry day),
    the expiring contract is bought back at its close and the NEXT month sold at its close, the
    same lot count (re-derived for the same shares if the lot size changed); each leg pays
    slippage and charges, the margin is re-blocked at the new notional. The closed contract's
    trade row carries rolled: true, calendar_spread (next close - expiring close, per share,
    Rs; > 0 = contango, the new short is sold higher) and roll_cost (both legs' charges +
    slippage). No next-month close stored that day -> the roll waits (event) and is retried;
    still none on expiry day -> settled at expiry (event "roll failed").
    roll_days_before_expiry = null: no roll -- like the paper book, a leg held to expiry is
    settled at that expiry's final close (reason EXPIRY, no slippage, charged as a closing
    leg). NSE stock futures are PHYSICALLY settled: a real short held into expiry must deliver
    the shares (delivery STT etc.); that is not modelled, which is why rolling is the default.

TRADE ROWS AND STATISTICS
    One row per contract leg: the standard row (qty = shares = lots x lot size, entry / exit
    prices of that contract, gross = shares x (entry - exit), costs = that leg's charges,
    return_pct on the leg's notional + opening charges -- the notional, not the margin, so a
    futures return compares with a cash one) plus instrument "FUT", direction "SHORT", expiry,
    lots, lot_size and leg (1 = the opening contract, 2.. after each roll). Every row of one
    position shares its entry_date. Round trips: a rolled position's rows fold into ONE trade
    (as PF-06 folds partial exits): net = the sum of its rows, return % = that over the FIRST
    leg's basis (each leg holds the whole position, so the bases are not summed). Win rate,
    expectancy, the walk-forward stitched metrics and the Monte Carlo trade shuffle all count
    that one trade (engine.round_trips_from_rows folds stored rows the same way).

CONFIG  snapshot "futures" (request "futures": {...}, config.json "backtest": {"futures": {...}})
    {"margin_pct": 20, "roll_days_before_expiry": 2, "min_days_to_expiry": 3,
     "cost_model": "nse_futures", "cost_overrides": {}}
    margin_pct and min_days_to_expiry are the paper book's defaults (execution/futures_paper.py).
    A snapshot gets the "futures" key only when the request or config.json sets one, so the
    snapshot of every other run is unchanged; such a run uses these DEFAULTS.
    Costs: backtest.costs.FuturesCostModel (the F&O segment, rates cited there).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date

DEFAULTS = {"margin_pct": 20.0, "roll_days_before_expiry": 2, "min_days_to_expiry": 3,
            "cost_model": "nse_futures", "cost_overrides": {}}

# additive columns (registered in db/schema_w39b.py W39B_COLUMNS)
TRADE_COLUMNS = {"instrument": "TEXT", "direction": "TEXT", "expiry": "DATE", "lots": "INTEGER",
                 "lot_size": "INTEGER", "leg": "INTEGER", "rolled": "INTEGER", "calendar_spread": "REAL",
                 "roll_cost": "REAL"}
EQUITY_COLUMNS = {"futures_margin": "REAL", "futures_notional": "REAL"}
ROW_EXTRA = tuple(TRADE_COLUMNS)


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def settings(cfg: dict | None = None) -> dict:
    """DEFAULTS with `cfg` over them, validated. ValueError on a bad value."""
    from backtest.costs import futures_cost_model
    out = copy.deepcopy(DEFAULTS)
    cfg = cfg or {}
    bad = set(cfg) - set(DEFAULTS)
    if bad:
        raise ValueError(f"unknown futures settings {sorted(bad)}")
    out.update(copy.deepcopy(cfg))
    m = out["margin_pct"]
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 < m <= 100:
        raise ValueError("futures.margin_pct must be a percentage in (0, 100]")
    out["margin_pct"] = float(m)
    r = out["roll_days_before_expiry"]
    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
        raise ValueError("futures.roll_days_before_expiry must be a whole number of sessions >= 0, or null (no roll)")
    n = out["min_days_to_expiry"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("futures.min_days_to_expiry must be a whole number of days >= 0")
    out["cost_overrides"] = dict(out.get("cost_overrides") or {})
    futures_cost_model(out["cost_model"], out["cost_overrides"] or None)
    return out


@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    lot_size: int
    close: float


class FuturesHistory:
    """Futures closes per (underlying, expiry, session) from fo_contract_daily and
    fo_underlying_daily, for [start, end]. Point in time: a value dated d is the
    bhavcopy of d (published after its close) and is only ever read for d."""

    def __init__(self, conn, symbols, start, end):
        from execution.futures_paper import ALIAS
        self.alias = ALIAS
        unds = {ALIAS.get(s, s) for s in symbols}
        self._series = {}                     # (und, expiry) -> {date: (close, lot)}
        self._listed = {}                     # (und, date) -> sorted expiries with a close
        self._near = {}                       # (und, date) -> near-month expiry
        self.contract_rows = self.summary_rows = 0
        seen = set()
        try:
            rows = conn.execute("SELECT date, symbol, expiry, close, lot_size FROM fo_contract_daily WHERE "
                                "instrument IN ('STF','IDF') AND date>=? AND date<=? AND close IS NOT NULL",
                                (str(start), str(end))).fetchall()
        except Exception:
            rows = []
        for d, s, e, c, lot in rows:
            if s not in unds or not c:
                continue
            d, e = _d(d), _d(e)
            self._series.setdefault((s, e), {})[d] = (float(c), int(lot) if lot else None)
            seen.add((s, d))
            self.contract_rows += 1
        summary_lot = {}
        try:
            rows = conn.execute("SELECT date, symbol, fut_close, near_expiry, lot_size FROM fo_underlying_daily "
                                "WHERE date>=? AND date<=? AND fut_close IS NOT NULL", (str(start), str(end))).fetchall()
        except Exception:
            rows = []
        for d, s, c, e, lot in rows:
            if s not in unds:
                continue
            d = _d(d)
            if lot:
                summary_lot[(s, d)] = int(lot)
            self.summary_rows += 1
            if (s, d) in seen or not c or not e:      # the contract rows say which future it is
                continue
            self._series.setdefault((s, _d(e)), {})[d] = (float(c), int(lot) if lot else None)
        for (s, e), pts in self._series.items():
            for d, (c, lot) in list(pts.items()):
                if lot is None and summary_lot.get((s, d)):
                    pts[d] = (c, summary_lot[(s, d)])
                if e >= d:
                    self._listed.setdefault((s, d), []).append(e)
        for k, es in self._listed.items():
            es.sort()
            self._near[k] = es[0]
        self.dates = sorted({d for (_s, d) in self._listed})

    def und(self, symbol: str) -> str:
        return self.alias.get(symbol, symbol)

    def contract(self, symbol, expiry, d) -> Contract | None:
        """That contract on session d (close and lot size), or None."""
        u = self.und(symbol)
        v = self._series.get((u, _d(expiry)), {}).get(d)
        if not v or not v[0] or not v[1]:
            return None
        return Contract(u, _d(expiry), int(v[1]), float(v[0]))

    def price(self, symbol, expiry, d) -> float | None:
        v = self._series.get((self.und(symbol), _d(expiry)), {}).get(d)
        return float(v[0]) if v and v[0] else None

    def near(self, symbol, d) -> Contract | None:
        e = self._near.get((self.und(symbol), d))
        return self.contract(symbol, e, d) if e else None

    def next_after(self, symbol, d, expiry) -> Contract | None:
        """The first listed contract expiring after `expiry`, priced on session d."""
        for e in self._listed.get((self.und(symbol), d), []):
            if e > _d(expiry):
                return self.contract(symbol, e, d)
        return None

    def final(self, symbol, expiry) -> tuple | None:
        """(session, close) of the contract's last stored close on or before its expiry."""
        pts = self._series.get((self.und(symbol), _d(expiry)), {})
        ds = [d for d in pts if d <= _d(expiry) and pts[d][0]]
        if not ds:
            return None
        d = max(ds)
        return d, float(pts[d][0])

    def fingerprint(self) -> dict:
        return {"fo_contract_daily_rows": self.contract_rows, "fo_underlying_daily_rows": self.summary_rows,
                "underlyings": sorted({u for (u, _e) in self._series}),
                "first": self.dates[0] if self.dates else None, "last": self.dates[-1] if self.dates else None}


@dataclass
class FuturesPosition:
    symbol: str
    expiry: date
    lots: int
    lot_size: int
    entry_price: float       # this contract leg's fill (after slippage)
    entry_ref_price: float   # the futures close it filled against
    entry_date: date         # the POSITION's first fill: shared by all its rows
    entry_idx: int
    entry_costs: float       # this leg's opening charges
    margin: float            # blocked from free cash
    mark: float              # the price variation margin was last settled at
    uid: int
    leg: int = 1
    max_hold: int | None = None
    reason: str = ""

    @property
    def shares(self) -> int:
        return self.lots * self.lot_size


@dataclass
class _FutOrder:
    symbol: str
    side: str                # SHORT | COVER
    lots: int = 0
    lot_size: int = 0
    value: float = 0.0       # the rupees the lots were sized from
    close: float = 0.0       # the decision-session near-month close
    signal: object = None
    reason: str = ""
    queued: date | None = None


class FuturesBook:
    """The short futures legs of one W2 run. Built by the engine on the first SHORT signal;
    shares the run's SimState (cash, costs, trade rows, round-trip legs, events)."""

    def __init__(self, conn, snapshot, symbols, start, end, slip, st, event):
        from backtest.costs import futures_cost_model
        self.cfg = settings(snapshot.get("futures"))
        self.costs = futures_cost_model(self.cfg["cost_model"], self.cfg["cost_overrides"] or None)
        self.hist = FuturesHistory(conn, symbols, start, end)
        self.slip, self.st, self.event = slip, st, event
        self.positions: dict = {}
        self.pending: list = []
        self.refused: dict = {}          # symbol -> refusals for missing futures history
        self.refused_other = 0           # sized below one lot, no margin, contract too close...
        self.stale_marks = 0
        self.rolls: list = []
        self.roll_waits = 0
        self.expiries = 0
        self.closed = 0                  # futures positions closed (round trips)
        self.costs_paid = 0.0
        self.negative_cash_sessions = 0
        self._cash_was_negative = False

    # ── strategy side (at the decision close) ──────────────────────────────
    def queue(self, s, d, eq, sizing, n_open: int):
        st = self.st
        sym = s.symbol
        if s.side == "COVER":
            if sym not in self.positions:
                self.event(d, sym, f"cover ignored: {sym} is not held short")
            elif not any(o.side == "COVER" and o.symbol == sym for o in self.pending):
                self.pending.append(_FutOrder(sym, "COVER", signal=s, reason="COVER_SIGNAL", queued=d))
            return
        if sym in self.positions or any(o.side == "SHORT" and o.symbol == sym for o in self.pending):
            return
        if sym in st.positions:
            self.event(d, sym, f"short ignored: {sym} is held long in the cash book")
            return
        if n_open + self.open_count() >= int(sizing["max_positions"]):
            self.event(d, sym, "short skipped: max_positions")
            return
        c = self.hist.near(sym, d)
        if not c:
            self._missing(d, sym, f"short refused: no futures history for {sym} on {d} (no near-month contract "
                                  f"in fo_underlying_daily / fo_contract_daily) -- leg not simulated")
            return
        cap = eq * float(sizing["max_position_pct"]) / 100
        lot_value = c.close * c.lot_size
        if s.quantity is not None:                  # shares, floored to whole lots
            lots = int(s.quantity) // c.lot_size
            want = lots * lot_value
        else:                                       # rupees of notional, floored to whole lots
            want = float(s.value) if s.value is not None else cap
            lots = int(want // lot_value)
        if want - cap > 0.01:                       # (a value rounded to paise is not over the cap)
            self.event(d, sym, f"short capped to {cap:,.0f} of {want:,.0f}: max_position_pct "
                               f"({sizing['max_position_pct']}% of equity)")
            want = cap
            lots = int(cap // lot_value)
        if lots < 1:
            self.refused_other += 1
            self.event(d, sym, f"short refused: one lot ({c.lot_size} x {c.close} = {lot_value:,.0f}) is more "
                               f"than the leg's size ({want:,.0f}); lots are never rounded up")
            return
        self.pending.append(_FutOrder(sym, "SHORT", lots, c.lot_size, want, c.close, s, s.reason, d))

    def views(self, i) -> dict:
        from backtest.strategy import FuturesView
        return {s: FuturesView(s, -p.shares, p.entry_price, p.entry_date, i - p.entry_idx, p.lots, p.lot_size,
                               p.expiry) for s, p in self.positions.items()}

    def open_count(self) -> int:
        return len(self.positions) + sum(1 for o in self.pending if o.side == "SHORT")

    # ── the session, at the futures close ──────────────────────────────────
    def session(self, d, i, sessions_left):
        """Covers, rolls / expiries, new shorts, then the daily mark -- all at d's EOD
        futures closes. sessions_left(d, expiry) counts NSE sessions to an expiry."""
        keep = []
        for o in sorted((o for o in self.pending if o.side == "COVER"), key=lambda o: o.symbol):
            pos = self.positions.get(o.symbol)
            if not pos:
                continue
            ref = self.hist.price(o.symbol, pos.expiry, d)
            if ref is None:
                keep.append(o)
                self.event(d, o.symbol, f"cover deferred: no {pos.expiry} futures close on {d}")
                continue
            self._close(pos, ref, d, i, o.reason or "COVER_SIGNAL")
        rd = self.cfg["roll_days_before_expiry"]
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            if rd is not None and d <= pos.expiry and sessions_left(d, pos.expiry) <= rd:
                if self._roll(pos, d, i):
                    continue
            if d >= pos.expiry:
                self._expire(pos, d, i, rolled_wanted=rd is not None)
        for o in sorted((o for o in self.pending if o.side == "SHORT"), key=lambda o: o.symbol):
            self._open(o, d, i, sessions_left)
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            ref = self.hist.price(sym, pos.expiry, d)
            if ref is None:
                self.stale_marks += 1
                continue
            self.st.cash += (pos.mark - ref) * pos.shares          # variation margin
            pos.mark = ref
        if self.st.cash < 0:
            self.negative_cash_sessions += 1
            if not self._cash_was_negative:
                self.event(d, None, f"free cash {self.st.cash:,.0f} after futures variation margin: a broker "
                                    f"would call margin")
        self._cash_was_negative = self.st.cash < 0
        covering = {o.symbol for o in keep}
        for sym, pos in self.positions.items():
            if pos.max_hold and i - pos.entry_idx >= pos.max_hold and sym not in covering:
                keep.append(_FutOrder(sym, "COVER", reason="MAX_HOLD", queued=d))
                covering.add(sym)
        self.pending = keep

    def _too_close(self, d, expiry, sessions_left) -> bool:
        rd = self.cfg["roll_days_before_expiry"]
        return ((rd is not None and sessions_left(d, expiry) <= rd)
                or (expiry - d).days < int(self.cfg["min_days_to_expiry"]))

    def _open(self, o, d, i, sessions_left):
        st, sym = self.st, o.symbol
        if sym in st.positions:
            self.event(d, sym, "short dropped: held long in the cash book at the fill")
            return
        c = self.hist.near(sym, d)
        if not c:
            self._missing(d, sym, f"short dropped: no futures close for {sym} on {d} at the fill")
            return
        if self._too_close(d, c.expiry, sessions_left):
            nxt = self.hist.next_after(sym, d, c.expiry)
            if not nxt:
                self.refused_other += 1
                self.event(d, sym, f"short refused: the near-month future expires {c.expiry} (inside the roll "
                                   f"window / min_days_to_expiry) and no next-month close is stored for {d}")
                return
            c = nxt
        lots = o.lots
        if c.lot_size != o.lot_size:
            lots = int(o.value // (o.close * c.lot_size))
            self.event(d, sym, f"lot size {o.lot_size} -> {c.lot_size} at the fill: {lots} lot(s)")
        wanted = lots
        px = self.slip.fill_price("SELL", c.close)
        mp = self.cfg["margin_pct"] / 100

        def need(n):
            v = n * c.lot_size * px
            return v * mp + self.costs.total("SELL", v, n)
        while lots > 0 and need(lots) > st.cash:
            lots -= 1
        if lots < 1:
            self.refused_other += 1
            self.event(d, sym, f"short rejected: free cash {st.cash:,.0f} does not cover the margin of one lot")
            return
        if lots < wanted:
            self.event(d, sym, f"short cut to {lots} of {wanted} lot(s): cash for margin")
        value = lots * c.lot_size * px
        margin = value * mp
        ch = self.costs.total("SELL", value, lots)
        st.cash -= margin + ch
        st.costs_paid += ch
        self.costs_paid += ch
        st.slippage_paid += lots * c.lot_size * (c.close - px)
        st.turnover += value
        st.next_uid += 1
        sig = o.signal
        self.positions[sym] = FuturesPosition(sym, c.expiry, lots, c.lot_size, px, c.close, d, i, ch, margin, px,
                                              st.next_uid, 1, getattr(sig, "max_hold_sessions", None), o.reason or "")

    def _close(self, pos, ref, d, i, reason, slip=True, roll=None):
        """Buy the leg back at ref (slippage unless a settlement): last variation, margin
        released, charges; one trade row. roll = {calendar_spread, roll_cost} for a roll."""
        st = self.st
        px = self.slip.fill_price("BUY", ref) if slip else round(ref, 4)
        n = pos.shares
        st.cash += (pos.mark - px) * n + pos.margin
        value = n * px
        ch = self.costs.total("BUY", value, pos.lots)
        st.cash -= ch
        st.costs_paid += ch
        self.costs_paid += ch
        st.slippage_paid += n * (px - ref)
        st.turnover += value
        gross = n * (pos.entry_price - px)
        net = gross - pos.entry_costs - ch
        basis = n * pos.entry_price + pos.entry_costs
        st.realized += net
        row = {"symbol": pos.symbol, "entry_date": pos.entry_date, "entry_price": round(pos.entry_price, 4),
               "entry_ref_price": round(pos.entry_ref_price, 4), "qty": n, "exit_date": d,
               "exit_price": round(px, 4), "exit_ref_price": round(ref, 4), "exit_reason": reason,
               "gross_pnl": round(gross, 2), "costs": round(pos.entry_costs + ch, 2), "net_pnl": round(net, 2),
               "return_pct": round(net / basis * 100, 4) if basis else None,
               "holding_sessions": i - pos.entry_idx, "entry_reason": pos.reason,
               "instrument": "FUT", "direction": "SHORT", "expiry": pos.expiry, "lots": pos.lots,
               "lot_size": pos.lot_size, "leg": pos.leg}
        if roll:
            row.update({"rolled": True, "calendar_spread": round(roll["calendar_spread"], 4),
                        "roll_cost": round(roll["roll_cost"], 2)})
        st.trades.append(row)
        # round trips: a rolled position is ONE trade on its first leg's basis (engine.fold_round_trips)
        st.trade_legs.append((pos.uid, net, basis if pos.leg == 1 else 0.0))
        del self.positions[pos.symbol]
        if not roll:
            self.closed += 1
        return px, ch

    def _roll(self, pos, d, i) -> bool:
        """Expiring contract -> next month at d's closes. False when it cannot happen today."""
        sym = pos.symbol
        ref = self.hist.price(sym, pos.expiry, d)
        nxt = self.hist.next_after(sym, d, pos.expiry)
        if ref is None or nxt is None:
            self.roll_waits += 1
            what = f"no {pos.expiry} close" if ref is None else "no next-month close stored"
            self.event(d, sym, f"roll deferred: {what} on {d}")
            return False
        lots = pos.lots if nxt.lot_size == pos.lot_size else pos.shares // nxt.lot_size
        if nxt.lot_size != pos.lot_size:
            self.event(d, sym, f"lot size {pos.lot_size} -> {nxt.lot_size} at the roll: {lots} lot(s) for "
                               f"{pos.shares} shares")
        if lots < 1:
            self.event(d, sym, f"roll impossible: {pos.shares} shares are less than one {nxt.expiry} lot "
                               f"({nxt.lot_size}); leg closed")
            self._close(pos, ref, d, i, "ROLL_FAILED")
            return True
        st = self.st
        mp = self.cfg["margin_pct"] / 100
        old_px = self.slip.fill_price("BUY", ref)
        old_ch = self.costs.total("BUY", pos.shares * old_px, pos.lots)
        free = st.cash + (pos.mark - old_px) * pos.shares + pos.margin - old_ch
        new_px = self.slip.fill_price("SELL", nxt.close)

        def need(n):
            v = n * nxt.lot_size * new_px
            return v * mp + self.costs.total("SELL", v, n)
        wanted = lots
        while lots > 0 and need(lots) > free:
            lots -= 1
        if lots < 1:
            self.event(d, sym, f"roll impossible: free cash does not cover the {nxt.expiry} margin; leg closed")
            self._close(pos, ref, d, i, "ROLL_FAILED")
            return True
        if lots < wanted:
            self.event(d, sym, f"roll cut to {lots} of {wanted} lot(s): cash for the {nxt.expiry} margin")
        new_value = lots * nxt.lot_size * new_px
        new_ch = self.costs.total("SELL", new_value, lots)
        spread = nxt.close - ref
        cost = (old_ch + new_ch + pos.shares * (old_px - ref) + lots * nxt.lot_size * (nxt.close - new_px))
        uid, entry_date, entry_idx, leg, mh, why = (pos.uid, pos.entry_date, pos.entry_idx, pos.leg, pos.max_hold,
                                                    pos.reason)
        old_expiry, old_lots = pos.expiry, pos.lots
        self._close(pos, ref, d, i, "ROLL", roll={"calendar_spread": spread, "roll_cost": cost})
        margin = new_value * mp
        st.cash -= margin + new_ch
        st.costs_paid += new_ch
        self.costs_paid += new_ch
        st.slippage_paid += lots * nxt.lot_size * (nxt.close - new_px)
        st.turnover += new_value
        self.positions[sym] = FuturesPosition(sym, nxt.expiry, lots, nxt.lot_size, new_px, nxt.close, entry_date,
                                              entry_idx, new_ch, margin, new_px, uid, leg + 1, mh, why)
        self.rolls.append({"date": d, "symbol": sym, "from_expiry": old_expiry, "to_expiry": nxt.expiry,
                           "lots": old_lots, "to_lots": lots, "expiring_close": ref, "next_close": nxt.close,
                           "calendar_spread": round(spread, 4), "roll_cost": round(cost, 2)})
        self.event(d, sym, f"rolled {old_lots} lot(s) {old_expiry} -> {nxt.expiry}: spread {spread:+.2f}/share, "
                           f"cost {cost:,.2f}")
        return True

    def _expire(self, pos, d, i, rolled_wanted: bool):
        """Settle at the expiry's final close (cash; NSE stock futures are physically settled)."""
        fin = self.hist.final(pos.symbol, pos.expiry)
        if fin:
            px = fin[1]
        else:
            px = pos.mark
            self.event(d, pos.symbol, f"{pos.expiry} expiry: no close stored for the contract; settled at the "
                                      f"last mark {px}")
        if rolled_wanted:
            self.event(d, pos.symbol, f"roll failed: no next-month close before the {pos.expiry} expiry; "
                                      f"settled at expiry")
        self.expiries += 1
        self._close(pos, px, d, i, "EXPIRY", slip=False)

    def close_all(self, d, i):
        """End of window: buy every leg back at its last mark (charged, with slippage)."""
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            self._close(pos, pos.mark, d, i, "END_OF_WINDOW")

    def _missing(self, d, sym, text):
        self.refused[sym] = self.refused.get(sym, 0) + 1
        self.event(d, sym, text)

    # ── what the equity point / metrics / bias report carry ───────────────
    def margin(self) -> float:
        return sum(p.margin for p in self.positions.values())

    def notional(self) -> float:
        return sum(p.shares * p.mark for p in self.positions.values())

    def unrealized(self) -> float:
        return sum(p.shares * (p.entry_price - p.mark) for p in self.positions.values())

    def metrics(self) -> dict:
        return {"futures_trades": self.closed, "futures_rolls": len(self.rolls),
                "roll_costs": round(sum(r["roll_cost"] for r in self.rolls), 2),
                "futures_costs": round(self.costs_paid, 2), "futures_refused": sum(self.refused.values())
                + self.refused_other, "futures_stale_marks": self.stale_marks,
                "open_futures_at_end": len(self.positions)}

    def bias(self) -> tuple[dict, list]:
        rd = self.cfg["roll_days_before_expiry"]
        section = {
            "instrument": "near-month stock futures (fo_underlying_daily; fo_contract_daily for each contract and "
                          "the next month)",
            "fills": "the futures close of the session after the decision (EOD bhavcopy; no futures open) +/- "
                     "slippage; cash legs of the same decision fill at that session's open",
            "settings": self.cfg, "cost_model": self.costs.as_dict(),
            "margin": f"{self.cfg['margin_pct']}% of notional blocked from cash; daily variation margin through cash",
            "roll": (f"{rd} session(s) before expiry, into the next month at the same lot count" if rd is not None
                     else "no roll: settled at expiry's final close"),
            "missing_history": "a SHORT with no stored futures contract is refused (event), never priced off spot",
            "refused": dict(sorted(self.refused.items())), "refused_other": self.refused_other,
            "stale_marks": self.stale_marks, "rolls": len(self.rolls), "roll_waits": self.roll_waits,
            "rolls_sample": self.rolls[:20], "expiry_settlements": self.expiries,
            "negative_cash_sessions": self.negative_cash_sessions,
            "data": self.hist.fingerprint(),
        }
        warns = ["futures legs fill at the session's end-of-day futures close, the cash legs of the same decision "
                 "at its open: a pair's two legs are filled hours apart"]
        if self.refused:
            what = ", ".join(f"{s} x{n}" for s, n in sorted(self.refused.items()))
            warns.append(f"futures legs refused: no futures history (fo_underlying_daily / fo_contract_daily) for "
                         f"{what} -- not simulated, never priced off spot; the paired long legs, if any, traded "
                         f"unhedged")
        if self.stale_marks:
            warns.append(f"futures stale marks: {self.stale_marks} (a held contract had no close that session)")
        if self.expiries:
            warns.append(f"{self.expiries} futures leg(s) settled at expiry in cash at the final close; NSE stock "
                         f"futures are physically settled (delivery not modelled)")
        if self.negative_cash_sessions:
            warns.append(f"free cash was negative on {self.negative_cash_sessions} session(s) after futures "
                         f"variation margin: a broker would have called margin")
        return section, warns
