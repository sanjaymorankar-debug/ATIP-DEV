"""
UI review harness (UI fixes v2, 3 Nov 2026): a throwaway, seeded ATIP served on a local port, screenshots of every
route at desktop / tablet / phone in light and dark, and the automated DOM scan of the v2 acceptance criteria.

    python -m tools.ui_review serve  [--port 8765] [--work DIR]
    python -m tools.ui_review shots  --base http://127.0.0.1:8765 --out DIR [--routes /home,/] [--themes light,dark]
    python -m tools.ui_review scan   --base http://127.0.0.1:8765 [--json out.json]

Never point this at atip_data/atip.db: `serve` builds its own database under --work (default: a temp folder) with
tools/load_test.py's synthetic market, then adds the rows that reproduce the issues found on 3 Nov 2026 (a session
with a loss since start and none today, alerts stored with HTML entities, a job logged twice, a rising India VIX, no
GIFT Nifty quote, a market-health score with decimals, technical signals older than the scores).

The scan (scan_page) is the single definition of "clean" shared with tests/test_ui_fixes_v2_browser.py.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# §4 of the v2 brief: every route the scan and the screenshots cover
ROUTES = ["/home", "/", "/command", "/command/signals", "/command/setup", "/command/monitor", "/command/journal",
          "/command/pnl", "/sessions", "/signals", "/screener", "/market-pulse", "/research", "/trading",
          "/options-builder", "/strategies", "/quant", "/ml", "/wealth", "/accounting", "/reports", "/settings", "/m",
          "/watchlist", "/stock/ACME", "/?stock=ACME", "/accounting?tab=pnl"]
VIEWPORTS = [("desktop", 1440, 900), ("tablet", 768, 1024), ("phone", 375, 812)]
THEMES = ["light", "dark"]


def chromium_path() -> str | None:
    """The browser this machine has: $ATIP_CHROMIUM, then any Playwright Chromium under /opt/pw-browsers."""
    env = os.environ.get("ATIP_CHROMIUM")
    if env and Path(env).exists():
        return env
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux*/chrome", "/opt/pw-browsers/chromium/chrome-linux*/chrome"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


# -- the seed ------------------------------------------------------------------------------------------------------

def seed_review(conn, today: date | None = None) -> dict:
    """Rows on top of load_test.seed() that reproduce the 3 Nov 2026 findings. Idempotent."""
    today = today or date.today()
    d, y = today.isoformat(), (today - timedelta(days=1)).isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from orders import sessions as S
    from accounting import cycles as C
    S.ensure_tables(conn)
    S.seed(conn)
    C.ensure_tables(conn)
    s1 = S.get_session(conn, "S1")
    if not s1.get("start_capital"):
        S.start_pool(conn, "S1", 25000.0)
        s1 = S.get_session(conn, "S1")
    for i in range(2, 11):                       # 3 Nov: nine more sessions with money and no trade yet (H5)
        if not S.get_session(conn, f"S{i}").get("start_capital"):
            S.start_pool(conn, f"S{i}", 25000.0)
    if not conn.execute("SELECT 1 FROM session_cycles WHERE session_id = 'S1'").fetchone():
        base = dict(session_id="S1", qty=98, start_amount=25000.0, compound_basis="CASH_AFTER_CHARGES", env="VIRTUAL",
                    pool_started_at=s1.get("pool_started_at"), fy="FY 2026-27", tax_head="STCG", written_at=now,
                    floor_at_start=5000.0, idle_cash=12.4, ch_other=0.0, ch_dp=15.93, ch_sebi=0.03, ch_stamp=3.6)
        rows = [dict(base, n=1, rule_id="REVIEW-1", symbol="WELSPUNLIV", buy_price=254.0, sell_price=249.2,
                     buy_value=24892.0, sell_value=24420.26, gross_pnl=-471.74, ch_brokerage=40.0, ch_stt=49.31,
                     ch_exchange=1.63, ch_gst=7.5, charges=118.0, net_pnl=-589.74, tax_est=0.0, actual_pnl=-589.74,
                     end_amount=24410.26, opened_at=f"{y} 09:20:00", closed_at=f"{y} 14:05:00", holding_days=0,
                     exit_reason="Stop")]
        for r in rows:
            cols = [c for c in C.CYCLE_COLUMNS if c in r]
            conn.execute(f"INSERT OR IGNORE INTO session_cycles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         tuple(r[c] for c in cols))
    conn.execute("UPDATE market_health SET mh_score = 60.73, regime = 'BULL', vix_level = 13.42, nifty_close = 24350.5 "
                 "WHERE date = (SELECT MAX(date) FROM market_health)")
    if not conn.execute("SELECT 1 FROM index_levels WHERE date = ?", (d,)).fetchone():
        conn.execute("INSERT INTO index_levels (date, time, nifty50, nifty50_chg, banknifty, banknifty_chg, india_vix, "
                     "india_vix_chg, gift_nifty) VALUES (?, '13:55', 24350.5, 0.42, 52110.3, -0.18, 13.42, 3.48, NULL)", (d,))
    if not conn.execute("SELECT 1 FROM pipeline_log WHERE job_name = 'broker_health'").fetchone():
        for job, st, at in (("broker_health", "SUCCESS", "13:50:02"), ("global_intraday", "SUCCESS", "13:45:11"),
                            ("broker_health", "SUCCESS", "13:35:01"), ("intraday_scan", "SUCCESS", "13:30:40"),
                            ("technical_signals", "FAILED", "06:10:00"), ("stock_feed", "SKIPPED", "09:00:00")):
            conn.execute("INSERT INTO pipeline_log (run_date, job_name, start_time, end_time, status, rows_processed, "
                         "error_msg, kind, duration_s) VALUES (?, ?, ?, ?, ?, 0, ?, 'run', 1.2)",
                         (d, job, f"{d} {at}", f"{d} {at}", st, "no bars" if st == "FAILED" else None))
    if not conn.execute("SELECT 1 FROM alert_log WHERE category = 'intraday'").fetchone():
        alerts = [
            ("intraday", "info", "Intraday signals",
             "<b>Intraday signals</b>\nWELSPUNLIV breakout above the opening range on 2.4x the slot&#x27;s usual volume "
             "(the 13:45 bar); entry 254.10, stop 249.20 &amp; target 263.90. LOAD003 crossed VWAP with volume N times "
             "the slot&#x27;s median."),
            ("intraday", "info", "Intraday signals",
             "<b>Intraday signals</b>\nLOAD005 volume spike: 3.1x the slot&#x27;s usual volume at 13:15."),
            ("intraday", "info", "Intraday signals", "<b>Intraday signals</b>\nACME reclaimed VWAP on rising volume."),
            ("intraday", "info", "Intraday signals", "<b>Intraday signals</b>\nLOAD007 opening-range breakdown."),
            ("job", "warning", "Job failed: technical_signals",
             "Job failed: technical_signals — no bars for 2026-10-31 (the 15-minute bars job did not run)."),
            ("broker", "critical", "Dhan token expires today", "Dhan token expires today at 23:59 IST: renew it on Settings."),
        ]
        for i, (cat, sev, title, msg) in enumerate(alerts):
            at = (datetime.now() - timedelta(minutes=5 * (len(alerts) - i))).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO alert_log (created_at, category, severity, title, message) VALUES (?, ?, ?, ?, ?)",
                         (at, cat, sev, title, msg))
    if not conn.execute("SELECT 1 FROM predictions WHERE is_tod = 1").fetchone():
        conn.execute("INSERT INTO predictions (pred_date, symbol, `signal`, atip_score, entry_price, stop_loss, target_1, "
                     "target_2, risk_reward, reasoning, is_tod) VALUES (?, 'ACME', 'BUY', 72.4, 101.35, 98.2, 107.65, "
                     "110.0, 2.0, 'Momentum and volume agree', 1)", (y,))
    if not conn.execute("SELECT 1 FROM technical_signal").fetchone():
        old = "2026-10-30" if today >= date(2026, 11, 2) else (today - timedelta(days=4)).isoformat()
        for i, sym in enumerate(("ACME", "LOAD001", "LOAD002")):
            conn.execute("INSERT INTO technical_signal (signal_id, symbol, date, scan, name, direction, reason, entry, "
                         "stop, target, atr, horizon, confluence, status, market_gate, created_at) VALUES "
                         "(?, ?, ?, 'golden_cross', 'Golden cross', 'LONG', '50-day average crossed the 200-day', "
                         "100, 96, 108, 2, 20, ?, 'OPEN', 'OPEN', ?)", (f"REVIEW-{i}", sym, old, 3 + i, f"{old} 18:00:00"))
    try:                                         # W6: a NIFTY option chain from the last session's F&O bhavcopy
        if not conn.execute("SELECT 1 FROM fo_contract_daily WHERE symbol = 'NIFTY'").fetchone():
            from quant.derivatives import bs_price
            sess = y if date.fromisoformat(y).weekday() < 5 else (today - timedelta(days=3)).isoformat()
            exp = (today + timedelta(days=(1 - today.weekday()) % 7 or 7)).isoformat()      # the next Tuesday
            spot, T = 24350.0, max(1, (date.fromisoformat(exp) - date.fromisoformat(sess)).days) / 365
            sql = ("INSERT INTO fo_contract_daily (date, symbol, instrument, expiry, strike, option_type, close, settle, "
                   "prev_close, oi, oi_chg, volume, underlying, lot_size, iv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
            for i in range(-10, 11):
                k = 24350.0 + 50 * i
                for kind in ("CE", "PE"):
                    px = round(bs_price(spot, k, T, 0.065, 0.13, "call" if kind == "CE" else "put"), 2)
                    oi = int(4_00_000 + 60_000 * abs(10 - abs(i)) + (90_000 if (kind == "CE") == (i > 0) else 0))
                    conn.execute(sql, (sess, "NIFTY", "OPTIDX", exp, k, kind, px, px, round(px * 0.94, 2), oi,
                                       int(oi * 0.05) * (1 if i % 2 else -1), 12_000 + 300 * i * i, spot, 75, None))
    except Exception as e:                       # an older schema without the F&O table: the card says why
        print(f"  (no option chain seeded: {e})")
    conn.commit()
    return {"today": d}


def serve(port: int = 8765, work: str | None = None) -> None:
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("ATIP_OPERATING_SCHEDULE", "off")
    wd = Path(work or tempfile.mkdtemp(prefix="atip-ui-review-"))
    wd.mkdir(parents=True, exist_ok=True)
    os.chdir(wd)
    import db.schema as schema
    schema.DB_PATH = wd / "review.db"
    from dashboard import security
    security.TOKEN_PATH = wd / "dashboard_token.txt"
    security.CONFIG_PATH = wd / "config.json"
    import dashboard.server as server
    server.STATE_PATH = wd / "dashboard_state.json"
    from tools import load_test as LT
    with LT.block_network():
        if not (wd / ".seeded").exists():
            LT.seed(wd, extra_symbols=12)
            (wd / ".seeded").write_text("1")
        conn = schema.get_connection()
        try:
            seed_review(conn)
        finally:
            conn.close()
        with LT.serve(server.app, port=port) as base:
            print(f"ATIP UI review server on {base} (work folder {wd})", flush=True)
            while True:
                time.sleep(3600)


# -- the DOM scan --------------------------------------------------------------------------------------------------

SCAN_JS = r"""() => {
 const out = {raw_float: [], bad_text: [], red_zero: [], dev_text: [], dup_nav: 0, hscroll: false, clickable: []};
 const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
   return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
 const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
 const skip = el => el.closest('script,style,code,pre,details:not([open]),[hidden],[aria-hidden=true],.ax-tech,dialog:not([open]),template,textarea,input,svg title');
 let n;
 while ((n = walker.nextNode())) {
   const t = n.nodeValue; if (!t || !t.trim()) continue;
   const el = n.parentElement; if (!el || skip(el) || !vis(el)) continue;
   const s = t.trim().slice(0, 140);
   if (/\b(None|NaN|undefined|null)\b/.test(t) && !/\bnull hypothesis\b/i.test(t)) out.bad_text.push(s);
   if (/₹\s*—|&#x27;|&#39;|&amp;|&lt;|&gt;|&quot;/.test(t)) out.bad_text.push(s);
   const m = t.match(/(?<![\w.])-?\d+\.\d{3,}(?![\w.])/g);
   if (m) out.raw_float.push(s);
   if (/python -m|\b[\w/]+\.py\b|config\.json/.test(t)) out.dev_text.push(s);
   if (/^[+−-]?0\.00%$/.test(t.trim())) {
     const c = getComputedStyle(el).color.match(/\d+/g).map(Number);
     if (c[0] > 150 && c[0] > c[1] * 1.6 && c[0] > c[2] * 1.6) out.red_zero.push(s);
   }
 }
 // a second navigation bar: a block of 4+ links to other ATIP pages outside the shell's own rail / top / tab bar
 document.querySelectorAll('#ax-main nav, #ax-main .top, #ax-main header, #ax-main .nav, #ax-main .links').forEach(b => {
   if (!vis(b) || b.closest('.ax-seealso')) return;
   const other = new Set([...b.querySelectorAll('a[href^="/"]')].filter(vis)
     .map(a => new URL(a.href).pathname).filter(p => p !== location.pathname));
   if (other.size >= 4) out.dup_nav++;
 });
 out.hscroll = document.documentElement.scrollWidth > window.innerWidth + 1;
 document.querySelectorAll('[onclick]').forEach(el => {
   if (!vis(el)) return;
   const tag = el.tagName.toLowerCase();
   if (el.querySelector('a,button,input,select,textarea,[tabindex],[role=dialog]') || /-back$|backdrop|overlay/i.test(el.id + ' ' + el.className)) return;   // a container (a modal's backdrop) whose own controls do the work
   if (!['a', 'button', 'input', 'select', 'summary', 'label', 'option', 'textarea'].includes(tag) &&
       el.getAttribute('role') !== 'button' && el.getAttribute('role') !== 'tab' && el.getAttribute('role') !== 'link')
     out.clickable.push(tag + (el.id ? '#' + el.id : '') + ' ' + (el.textContent || '').trim().slice(0, 40));
 });
 for (const k of ['raw_float', 'bad_text', 'red_zero', 'dev_text', 'clickable']) out[k] = [...new Set(out[k])].slice(0, 25);
 return out;
}"""


def scan_page(page) -> dict:
    return page.evaluate(SCAN_JS)


def _ctx(browser, base, w, h, theme):
    ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=theme)
    ctx.add_init_script(f"try{{localStorage.setItem('atip-theme','{theme}')}}catch(e){{}}")
    ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    return ctx


def scan(base: str, routes=None, widths=((1440, 900), (360, 780))) -> dict:
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chromium_path())
        try:
            for w, h in widths:
                ctx = _ctx(b, base, w, h, "light")
                for r in routes or ROUTES:
                    pg = ctx.new_page()
                    errs = []
                    pg.on("pageerror", lambda e: errs.append(str(e)))
                    try:
                        resp = pg.goto(base + r, wait_until="networkidle", timeout=30000)
                        pg.wait_for_timeout(600)
                        res = scan_page(pg)
                        if resp is not None and resp.status >= 400:
                            res["load_error"] = f"HTTP {resp.status}"
                    except Exception as e:                      # a route that does not load is a finding too
                        res = {"load_error": str(e)[:200]}
                    res["js_errors"] = errs[:5]
                    out[f"{r} @{w}"] = res
                    pg.close()
                ctx.close()
        finally:
            b.close()
    return out


def problems(res: dict, phone: bool = False) -> list:
    """The findings of one scanned page that fail the v2 acceptance criteria."""
    bad = []
    for k in ("raw_float", "bad_text", "red_zero", "dev_text", "clickable", "js_errors"):
        if res.get(k):
            bad.append((k, res[k]))
    if res.get("dup_nav"):
        bad.append(("dup_nav", res["dup_nav"]))
    if phone and res.get("hscroll"):
        bad.append(("hscroll", True))
    if res.get("load_error"):
        bad.append(("load_error", res["load_error"]))
    return bad


# -- screenshots ---------------------------------------------------------------------------------------------------

def shots(base: str, out: str, routes=None, themes=None, viewports=None, full_page: bool = False) -> list:
    from playwright.sync_api import sync_playwright
    od = Path(out)
    od.mkdir(parents=True, exist_ok=True)
    made = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chromium_path())
        try:
            for vname, w, h in viewports or VIEWPORTS:
                for theme in themes or THEMES:
                    ctx = _ctx(b, base, w, h, theme)
                    for r in routes or ROUTES:
                        pg = ctx.new_page()
                        try:
                            pg.goto(base + r, wait_until="networkidle", timeout=30000)
                            pg.wait_for_timeout(500)
                            slug = (r.strip("/").replace("/", "_").replace("?", "_").replace("=", "-") or "root")
                            f = od / f"{slug}__{vname}__{theme}.jpg"
                            pg.screenshot(path=str(f), full_page=full_page, type="jpeg", quality=70)
                            made.append(str(f))
                        except Exception as e:
                            print(f"  {r} {vname} {theme}: {e}", file=sys.stderr)
                        finally:
                            pg.close()
                    ctx.close()
        finally:
            b.close()
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.ui_review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("serve"); a.add_argument("--port", type=int, default=8765); a.add_argument("--work")
    a = sub.add_parser("shots"); a.add_argument("--base", required=True); a.add_argument("--out", required=True)
    a.add_argument("--routes"); a.add_argument("--themes"); a.add_argument("--viewports"); a.add_argument("--full", action="store_true")
    a = sub.add_parser("scan"); a.add_argument("--base", required=True); a.add_argument("--routes"); a.add_argument("--json")
    ns = ap.parse_args(argv)
    if ns.cmd == "serve":
        serve(ns.port, ns.work)
        return 0
    routes = ns.routes.split(",") if getattr(ns, "routes", None) else None
    if ns.cmd == "shots":
        vps = [v for v in VIEWPORTS if not ns.viewports or v[0] in ns.viewports.split(",")]
        made = shots(ns.base, ns.out, routes, ns.themes.split(",") if ns.themes else None, vps, ns.full)
        print(f"{len(made)} screenshots in {ns.out}")
        return 0
    res = scan(ns.base, routes)
    fails = 0
    for k, v in res.items():
        bad = problems(v, phone=k.endswith("@360"))
        fails += bool(bad)
        print(("FAIL " if bad else "ok   ") + k + ("" if not bad else "  " + json.dumps(bad, ensure_ascii=False)[:600]))
    if ns.json:
        Path(ns.json).write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(f"{len(res) - fails}/{len(res)} pages clean")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
