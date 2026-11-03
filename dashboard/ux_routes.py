"""
Routes of the redesign (2 Nov 2026): the shell, the new pages and /api/v2.

Pages (GET, rendered in the shell): /home /sessions /sessions/{sid} /accounting /reports /settings /more /markets /trade
/lab /ops. Every older page keeps its URL and is served inside the shell by dashboard/shell.py's middleware.

    GET  /api/v2/shell/state                     run state (same words as /api/status), env, halt, open trades
    GET  /api/v2/search?q=                       symbols for the Ctrl/⌘K search
    GET  /api/v2/home                            the Home page's data
    GET  /api/v2/sessions                        every session with gross / charges / tax / actual tiles and totals
    GET  /api/v2/sessions/{sid}                  one session's book: the 4.3 table per cycle, tiles, charts, activity
    POST /api/v2/sessions/{sid}/setup            name, strategy, universe, mode (+ack), status, start_amount (+restart),
                                                 min_buy_value, floor_rule / floor_value / floor_step_every,
                                                 compound_basis, target / stop / trail / hold, max_occurrences, auto_pick;
                                                 400 {"error", "errors": {field: plain-English message}}
    POST /api/v2/sessions/split    {total, ids}  proposed equal amounts (nothing saved)
    POST /api/v2/sessions/sync     {force?}      store closed cycles now (force: recompute them)
    GET  /api/v2/accounting/{ledger|trial-balance|session/{sid}|charges|tax|funds|recon}
    POST /api/v2/accounting/transfer {from, to, amount, reason}   /adjust {account, amount, reason}
    POST /api/v2/accounting/recon/import (CSV text)   /recon/cash   /recon/{id} {status, note}
    GET/POST /api/v2/tax/config
    GET  /api/v2/reports/kinds | schedules | archive | archive/{id}/download
    POST /api/v2/reports/generate {kind, params, formats}   /schedules   /schedules/{id}   /schedules/{id}/delete   /run-due

Every POST needs the X-ATIP-Token header; the operating schedule's guard refuses it with 423 while ATIP is stopped,
and a viewer refuses it with 403 (both middlewares already wrap every route). Nothing here places, confirms or
cancels an order: session setup goes through orders/sessions.py, which keeps AUTO's ack, the halt and the SEBI
Algo ID guards.
"""

# No `from __future__ import annotations` (FastAPI must see the real Request class).
import math
from datetime import date

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool


def _finite(obj):
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (bytes, bytearray)):
        return None
    return obj


class SetupError(ValueError):
    def __init__(self, errors: dict):
        self.errors = errors
        super().__init__(next(iter(errors.values())))


def _num(b, k, errs, label, cast=float, positive=True, optional=False):
    v = b.get(k)
    if v in (None, ""):
        if not optional:
            errs[k] = f"Enter the {label}."
        return None
    try:
        v = cast(str(v).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        errs[k] = f"The {label} must be a {'whole number' if cast is int else 'number'}."
        return None
    if positive and v <= 0:
        errs[k] = f"The {label} must be more than {'₹0' if 'amount' in label or 'minimum' in label else '0'}."
        return None
    return v


def setup_session(conn, sid: str, b: dict) -> dict:
    """The Session setup drawer: validate everything in plain English first, then apply it through
    orders/sessions.py (start_pool, update_session, set_mode, set_status), each change logged in session_events."""
    from accounting.cycles import FLOOR_RULES, BASES
    from orders import sessions as S
    from accounting import cycles as C
    sid = sid.upper()
    s = S.get_session(conn, sid)
    errs, changes = {}, []
    if "name" in b and not str(b.get("name") or "").strip():
        errs["name"] = "Give the session a name."
    old_start = float(s.get("start_capital") or 0)
    start = _num(b, "start_amount", errs, "starting amount", optional=bool(old_start) or "start_amount" not in b)
    if start is None and not old_start and "start_amount" in b and "start_amount" not in errs:
        errs["start_amount"] = "Enter the starting amount."
    eff_start = start or old_start
    minimum = _num(b, "min_buy_value", errs, "minimum amount per cycle", optional=True)
    floor_rule = str(b.get("floor_rule") or s.get("floor_rule") or "FIXED").upper()
    if floor_rule not in FLOOR_RULES:
        errs["floor_rule"] = "Pick how the minimum grows: fixed, ratchet or step."
    fval = _num(b, "floor_value", errs, "ratchet %" if floor_rule == "RATCHET_PCT" else "step amount", optional=floor_rule == "FIXED")
    every = _num(b, "floor_step_every", errs, "number of profitable cycles", int, optional=floor_rule != "STEP")
    if floor_rule == "RATCHET_PCT" and fval is not None and fval > 100:
        errs["floor_value"] = f"The ratchet is a % of the highest cycle start: {fval:g}% is more than 100%."
    basis = str(b.get("compound_basis") or s.get("compound_basis") or "CASH_AFTER_CHARGES").upper()
    if basis not in BASES:
        errs["compound_basis"] = "Pick what the next cycle starts with."
    tgt = _num(b, "target_pct", errs, "target %", optional=True)
    stop = _num(b, "stop_pct", errs, "stop %", optional=True)
    trail = _num(b, "trail_pct", errs, "trailing %", optional=True)
    hold = _num(b, "hold_days", errs, "hold days", optional=True)
    maxc = _num(b, "max_occurrences", errs, "max cycles", int, optional=True)
    m = minimum if minimum is not None else float(s.get("min_buy_value") or S.POOL_DEFAULTS["min_buy_value"])
    if eff_start and m > eff_start + 0.004 and "min_buy_value" not in errs:
        errs["min_buy_value"] = f"Minimum ₹{m:,.0f} is more than the ₹{eff_start:,.0f} starting amount."
    if trail and not (stop or s.get("stop_pct")):
        errs["trail_pct"] = "A trailing exit moves the stop, so the session needs a stop % first."
    if maxc is not None and old_start:
        L = S.ledger(conn, sid, s=s)
        if L and maxc < L["closed"]:
            errs["max_occurrences"] = f"{sid} has already run {L['closed']} cycles; max cycles cannot be less than that."
    mode = str(b.get("mode") or s["mode"]).upper()
    if mode not in S.MODES:
        errs["mode"] = "Pick Manual, Confirm or Auto."
    elif mode == "AUTO" and s["mode"] != "AUTO" and not b.get("ack"):
        errs["ack"] = f"AUTO sends orders without asking. Tick “I understand” to switch {sid} to AUTO."
    status = str(b.get("status") or s["status"]).upper()
    if status not in S.STATUSES:
        errs["status"] = "Pick Running, Paused or Off."
    start_changes = start is not None and (not old_start or abs(start - old_start) >= 0.01)
    if start_changes and old_start:
        if S._has_open_trade(conn, sid, s.get("pool_started_at")):
            L = S.ledger(conn, sid, s=s)
            errs["start_amount"] = (f"{sid} holds an open trade ({', '.join(h['symbol'] for h in L['open'])}). Its starting "
                                    f"amount can change only after that trade closes, because the trade is using the cash.")
        elif not b.get("restart"):
            errs["restart"] = (f"Changing the starting amount from ₹{old_start:,.2f} to ₹{start:,.2f} restarts {sid}'s books "
                               f"from now. Tick the box to confirm.")
    if errs:
        raise SetupError(errs)
    if mode == "AUTO" and s["mode"] != "AUTO":
        S._auto_guards("AUTO")                 # halted / LIVE without a SEBI Algo ID: refused before anything changes
    if start_changes:
        S.start_pool(conn, sid, start, tgt or s.get("target_pct"), stop or s.get("stop_pct"), m,
                     maxc or s.get("max_occurrences"), trail if "trail_pct" in b else s.get("trail_pct"),
                     hold if "hold_days" in b else s.get("hold_days"))
        changes.append(f"starting amount ₹{start:,.2f} (books from now)")
    fields = {k: b[k] for k in ("name", "strategy", "universe") if k in b and str(b.get(k) or "") != str(s.get(k) or "")}
    for k, v in (("min_buy_value", minimum), ("target_pct", tgt), ("stop_pct", stop), ("max_occurrences", maxc),
                 ("floor_value", fval), ("floor_step_every", every)):
        if v is not None and (s.get(k) is None or abs(float(v) - float(s.get(k) or 0)) > 1e-9):
            fields[k] = v
    if floor_rule != (s.get("floor_rule") or "FIXED"):
        fields["floor_rule"] = floor_rule
    if basis != (s.get("compound_basis") or "CASH_AFTER_CHARGES"):
        fields["compound_basis"] = basis
    for k, v in (("trail_pct", trail), ("hold_days", hold)):
        if k in b and (v or None) != (s.get(k) or None):
            fields[k] = v if v else "off"
    if "auto_pick" in b and bool(b.get("auto_pick")) != bool(s.get("auto_pick")):
        fields["auto_pick"] = "on" if b.get("auto_pick") else "off"
    if fields:
        S.update_session(conn, sid, fields)
        changes += [k.replace("_", " ") for k in fields]
    if mode != s["mode"]:
        S.set_mode(conn, sid, mode, ack=bool(b.get("ack")))
        changes.append(f"mode {mode}")
    if status != s["status"]:
        S.set_status(conn, sid, status, "from the Session setup drawer")
        changes.append(f"status {status}")
    C.sync(conn, sid)
    return {"session": S.get_session(conn, sid), "pool": S.ledger(conn, sid), "changes": changes}


def search(conn, q: str) -> dict:
    q = str(q or "").strip().upper()[:20]
    if len(q) < 2:
        return {"symbols": []}
    rows = []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT symbol, `signal` AS `signal`, atip_score FROM ai_scores WHERE date = (SELECT MAX(date) FROM ai_scores) "
            "AND symbol LIKE ? ORDER BY (symbol = ?) DESC, (symbol LIKE ?) DESC, atip_score DESC LIMIT 10",
            (f"%{q}%", q, f"{q}%")).fetchall()]
    except Exception:
        pass
    return {"symbols": rows}


def register(app, guard, Req, get_connection, json_safe):
    from dashboard import shell
    from dashboard import ux_pages as P
    shell.install(app)

    def call(fn):
        conn = get_connection()
        try:
            return JSONResponse(_finite(json_safe(fn(conn))))
        except SetupError as e:
            return JSONResponse({"error": str(e), "errors": e.errors}, status_code=400)
        except LookupError as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
        except (ValueError, TypeError, KeyError) as e:
            return JSONResponse({"error": str(e).strip("'\"")}, status_code=400)
        finally:
            conn.close()

    def html_page(fn):
        conn = get_connection()
        try:
            return HTMLResponse(fn(conn))
        except LookupError as e:
            return HTMLResponse(shell.page("Not found", f'<div class="ax-empty">{P.e(e)}</div>', "/"), status_code=404)
        finally:
            conn.close()

    async def body(request) -> dict:
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    def ctx(request):
        root = request.scope.get("root_path") or ""
        ua = request.headers.get("user-agent", "")
        return {"root": root, "app": "ATIPApp/" in ua or request.query_params.get("app") == "1"}

    # ── pages ─────────────────────────────────────────────────────────────────────────────
    @app.get("/home", response_class=HTMLResponse)
    async def ux_home(request: Req):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: P.home(conn, **c))

    @app.get("/sessions", response_class=HTMLResponse)
    async def ux_sessions(request: Req):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: P.sessions(conn, **c))

    @app.get("/sessions/{sid}", response_class=HTMLResponse)
    async def ux_session(sid: str, request: Req, tab: str = "manage"):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: P.session_detail(conn, sid.upper(), tab, **c))

    @app.get("/accounting", response_class=HTMLResponse)
    async def ux_accounting(request: Req):
        c, q = ctx(request), dict(request.query_params)
        return await run_in_threadpool(html_page, lambda conn: P.accounting(conn, q, **c))

    @app.get("/reports", response_class=HTMLResponse)
    async def ux_reports(request: Req):
        c, q = ctx(request), dict(request.query_params)
        return await run_in_threadpool(html_page, lambda conn: P.reports(conn, q, **c))

    @app.get("/settings", response_class=HTMLResponse)
    async def ux_settings(request: Req):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: P.settings(conn, **c))

    @app.get("/ops", response_class=HTMLResponse)
    async def ux_ops(request: Req):
        c = ctx(request)
        return await run_in_threadpool(html_page, lambda conn: P.ops(conn, **c))

    @app.get("/more", response_class=HTMLResponse)
    async def ux_more(request: Req):
        return HTMLResponse(P.more(**ctx(request)))

    for _key, _path in (("markets", "/markets"), ("trade", "/trade"), ("lab", "/lab")):
        def _make(key):
            async def ux_hub(request: Req):
                return HTMLResponse(P.hub(key, **ctx(request)))
            return ux_hub
        app.add_api_route(_path, _make(_key), methods=["GET"], response_class=HTMLResponse, name=f"ux_hub_{_key}")

    # ── shell, search, home ───────────────────────────────────────────────────────────────
    @app.get("/api/v2/shell/state")
    async def api_v2_shell_state():
        return await run_in_threadpool(call, shell.state)

    @app.get("/api/v2/search")
    async def api_v2_search(q: str = ""):
        return await run_in_threadpool(call, lambda c: search(c, q))

    @app.get("/api/v2/home")
    async def api_v2_home():
        return await run_in_threadpool(call, P.home_data)

    # ── sessions ──────────────────────────────────────────────────────────────────────────
    @app.get("/api/v2/sessions")
    async def api_v2_sessions():
        from accounting.cycles import overview
        return await run_in_threadpool(call, overview)

    @app.post("/api/v2/sessions/split", dependencies=guard)
    async def api_v2_split(request: Req):
        b = await body(request)
        from accounting.cycles import split
        return await run_in_threadpool(call, lambda c: split(b.get("total"), b.get("ids")))

    @app.post("/api/v2/sessions/sync", dependencies=guard)
    async def api_v2_sync(request: Req):
        b = await body(request)
        from accounting.cycles import sync
        return await run_in_threadpool(call, lambda c: sync(c, b.get("sid"), bool(b.get("force"))))

    @app.get("/api/v2/sessions/{sid}")
    async def api_v2_session(sid: str):
        from accounting.cycles import book
        return await run_in_threadpool(call, lambda c: book(c, sid.upper()))

    @app.post("/api/v2/sessions/{sid}/setup", dependencies=guard)
    async def api_v2_setup(sid: str, request: Req):
        b = await body(request)
        return await run_in_threadpool(call, lambda c: setup_session(c, sid, b))

    # ── accounting ────────────────────────────────────────────────────────────────────────
    @app.get("/api/v2/accounting/ledger")
    async def api_v2_ledger(session: str = None, account: str = None, kind: str = None, env: str = None,
                            fy: str = None, frm: str = None, to: str = None, limit: int = 500):
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.entries(c, session, account, kind, env, fy, frm, to, limit))

    @app.get("/api/v2/accounting/trial-balance")
    async def api_v2_tb(env: str = None, fy: str = None):
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.trial_balance(c, env, fy))

    @app.get("/api/v2/accounting/session/{sid}")
    async def api_v2_sledger(sid: str):
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.session_ledger(c, sid.upper()))

    @app.get("/api/v2/accounting/charges")
    async def api_v2_charges(by: str = "type", fy: str = None, env: str = None):
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.charges_register(c, by, fy, env))

    @app.get("/api/v2/accounting/tax")
    async def api_v2_tax(fy: str = None):
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.tax_register(c, fy))

    @app.get("/api/v2/accounting/funds")
    async def api_v2_funds():
        from accounting import ledger as L
        return await run_in_threadpool(call, L.funds)

    @app.get("/api/v2/accounting/recon")
    async def api_v2_recon(status: str = None, kind: str = None):
        from accounting import reconcile as R
        return await run_in_threadpool(call, lambda c: {"items": R.items(c, status, kind)})

    @app.post("/api/v2/accounting/transfer", dependencies=guard)
    async def api_v2_transfer(request: Req):
        b = await body(request)
        from accounting import ledger as L
        return await run_in_threadpool(call, lambda c: L.transfer(c, b.get("from"), b.get("to"), b.get("amount"),
                                                                  b.get("reason")))

    @app.post("/api/v2/accounting/adjust", dependencies=guard)
    async def api_v2_adjust(request: Req):
        b = await body(request)
        from accounting import ledger as L
        sid = str(b.get("account") or "").split(":")[-1] if str(b.get("account") or "").startswith("Cash:") else b.get("session")
        return await run_in_threadpool(call, lambda c: L.adjust(c, sid, b.get("account"), b.get("amount"), b.get("reason")))

    @app.post("/api/v2/accounting/recon/import", dependencies=guard)
    async def api_v2_recon_import(request: Req):
        text = (await request.body()).decode("utf-8", "replace")
        from accounting import reconcile as R
        return await run_in_threadpool(call, lambda c: R.import_contract_note(c, text))

    @app.post("/api/v2/accounting/recon/cash", dependencies=guard)
    async def api_v2_recon_cash():
        from accounting import reconcile as R
        return await run_in_threadpool(call, R.check_cash)

    @app.post("/api/v2/accounting/recon/{item_id}", dependencies=guard)
    async def api_v2_recon_resolve(item_id: int, request: Req):
        b = await body(request)
        from accounting import reconcile as R
        return await run_in_threadpool(call, lambda c: R.resolve(c, item_id, b.get("status"), b.get("note")))

    # ── tax ───────────────────────────────────────────────────────────────────────────────
    @app.get("/api/v2/tax/config")
    async def api_v2_tax_config():
        from accounting import tax as TX
        return JSONResponse({**TX.settings(), "disclaimer": TX.DISCLAIMER})

    @app.post("/api/v2/tax/config", dependencies=guard)
    async def api_v2_tax_save(request: Req):
        b = await body(request)
        from accounting import tax as TX
        return await run_in_threadpool(call, lambda c: TX.save(b))

    # ── reports ───────────────────────────────────────────────────────────────────────────
    @app.get("/api/v2/reports/kinds")
    async def api_v2_report_kinds():
        from enterprise import session_reports as R
        return JSONResponse({"kinds": R.KINDS, "formats": R.FORMATS, "freqs": R.FREQS, "sections": R.CUSTOM_SECTIONS})

    @app.get("/api/v2/reports/schedules")
    async def api_v2_schedules():
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: {"schedules": R.schedules(c)})

    @app.get("/api/v2/reports/archive")
    async def api_v2_archive(kind: str = None, limit: int = 200):
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: {"archive": R.archive(c, kind, limit)})

    @app.get("/api/v2/reports/archive/{aid}/download")
    async def api_v2_archive_file(aid: int):
        from enterprise import session_reports as R

        def go():
            conn = get_connection()
            try:
                data, mime, name = R.archive_file(conn, aid)
                return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})
            except LookupError as e:
                return JSONResponse({"error": str(e).strip("'\"")}, status_code=404)
            finally:
                conn.close()
        return await run_in_threadpool(go)

    @app.post("/api/v2/reports/generate", dependencies=guard)
    async def api_v2_generate(request: Req):
        b = await body(request)
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: R.generate(c, b.get("kind"), b.get("params") or {},
                                                                  b.get("formats") or ["html"], by="owner"))

    @app.post("/api/v2/reports/schedules", dependencies=guard)
    async def api_v2_schedule_new(request: Req):
        b = await body(request)
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: R.save_schedule(c, b))

    @app.post("/api/v2/reports/schedules/{sid}", dependencies=guard)
    async def api_v2_schedule_edit(sid: int, request: Req):
        b = await body(request)
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: R.save_schedule(c, b, sid))

    @app.post("/api/v2/reports/schedules/{sid}/delete", dependencies=guard)
    async def api_v2_schedule_delete(sid: int):
        from enterprise import session_reports as R
        return await run_in_threadpool(call, lambda c: R.delete_schedule(c, sid))

    @app.post("/api/v2/reports/run-due", dependencies=guard)
    async def api_v2_run_due():
        from enterprise import session_reports as R
        return await run_in_threadpool(call, R.run_due)

    # UI fixes v2 (W1-W5): watchlists, quotes, depth, the P&L strip, freshness, /stock/{symbol} and /watchlist
    from dashboard import workspace
    workspace.register(app, guard, call, html_page, ctx, Req)
