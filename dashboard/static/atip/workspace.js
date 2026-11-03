/* ATIP trading workspace (UI fixes v2, W1-W5, 3 Nov 2026) -- see dashboard/workspace.py.
   W1 watchlist rail and page · W2 the one order ticket (drawer / bottom sheet; saves through POST /api/orders, the
   order-rule API the old modal used, always asking before the entry is placed) · W3 the pro chart on /stock/{symbol}
   (TradingView Lightweight Charts 5.0.9, served locally) · W4 the market-depth ladder · W5 the P&L strip.
   Keys: W watchlist, B buy, S sell (the stock on screen, or the watchlist row under the pointer / focus), Esc close.
   Every number goes through AX.fmt (the mirror of dashboard/fmt.py). Live prices refresh every 15 s, in place. */
(function () {
  "use strict";
  var AX = window.AX = window.AX || {};
  var F = function () { return AX.fmt; };
  var BASE = (document.querySelector('meta[name="atip-root"]') || {}).content || "";
  var path = location.pathname.slice(BASE.length) || "/";
  function esc(s) { return AX.esc ? AX.esc(s) : String(s == null ? "" : s); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function visible() { return document.visibilityState !== "hidden"; }
  var current = null;                     // the symbol B / S act on

  // ── W1 watchlist ───────────────────────────────────────────────────────────────────────────
  var WL = { lists: [], idx: 0, quotes: {}, timer: null };
  function wlActive() { return WL.lists[WL.idx] || { name: "My watchlist", symbols: [] }; }
  function wlSave(list) {
    return AX.api("/api/v2/watchlists", { body: { id: list.id || "", name: list.name, symbols: list.symbols } }).then(function (r) {
      list.id = r.id; list.draft = false; return r;
    }).catch(function (e) { AX.toast(e.message, true); throw e; });
  }
  function wlRow(s, page) {
    var q = WL.quotes[s] || {}, f = F();
    var sig = q.signal ? '<span class="ax-wl-sig ' + esc(q.signal) + '">' + esc(q.signal) + "</span>" : "";
    var sc = q.atip_score != null ? '<span title="ATIP score">ATIP ' + f.score(q.atip_score) + "</span>" : "";
    return '<li class="ax-wl-row" data-sym="' + esc(s) + '"><a class="ax-wl-sym" href="' + esc(BASE + "/stock/" + s) + '">' + esc(s) + "</a>" +
      '<span class="ax-wl-px" data-px>' + (q.ltp != null ? f.price(q.ltp) : "—") + "</span>" +
      '<span class="ax-wl-meta">' + sig + sc + (q.live === false && q.ltp != null ? '<span title="Last close: no live quote now">close</span>' : "") + "</span>" +
      '<span class="ax-wl-chg" data-chg>' + f.pctHtml(q.change_pct, false, "No change yet") + "</span>" +
      '<span class="ax-wl-acts"><button type="button" class="b" data-ax-trade="BUY" data-sym="' + esc(s) + '" aria-label="Buy ' + esc(s) + ' (B)">B</button>' +
      '<button type="button" class="s" data-ax-trade="SELL" data-sym="' + esc(s) + '" aria-label="Sell ' + esc(s) + ' (S)">S</button>' +
      '<a href="' + esc(BASE + "/stock/" + s) + '" aria-label="Chart of ' + esc(s) + '" title="Chart">📈</a>' +
      '<a href="' + esc(BASE + "/stock/" + s + "#depth") + '" aria-label="Market depth of ' + esc(s) + '" title="Depth">≡</a>' +
      '<button type="button" data-ax-wl-rm="' + esc(s) + '" aria-label="Remove ' + esc(s) + ' from this watchlist" title="Remove">✕</button></span></li>';
  }
  function wlRender(box) {
    if (!box) return;
    var page = box.hasAttribute("data-ax-wl-page"), L = wlActive();
    var tabs = WL.lists.map(function (l, i) { return '<button type="button" role="tab" aria-selected="' + (i === WL.idx) + '" data-wl-i="' + i + '">' + esc(l.name) + "</button>"; }).join("");
    box.innerHTML = '<div class="ax-wl-head"><div class="ax-wl-tabs" role="tablist" aria-label="Watchlists">' + tabs + "</div>" +
      '<button class="ax-iconbtn" type="button" data-wl-new aria-label="New watchlist" title="New watchlist">＋</button>' +
      '<button class="ax-iconbtn" type="button" data-wl-menu aria-label="Rename or delete this watchlist" title="Rename or delete">⋯</button>' +
      (page ? "" : '<button class="ax-iconbtn" type="button" data-ax-wl-toggle aria-label="Hide the watchlist (W)" title="Hide (W)">«</button>') + "</div>" +
      '<div class="ax-wl-add"><input type="search" placeholder="Add a stock: type 2+ letters" aria-label="Add a stock to ' + esc(L.name) + '" autocomplete="off" data-wl-q>' +
      '<div class="ax-wl-sugg" hidden role="listbox"></div></div>' +
      '<ul class="ax-wl-list" aria-label="' + esc(L.name) + '">' + (L.symbols.length ? L.symbols.map(function (s) { return wlRow(s, page); }).join("") :
        '<li class="ax-wl-empty">No stocks in this list yet. Add one above, or press ☆ Watchlist on any stock.</li>') + "</ul>" +
      '<div class="ax-wl-foot"><span data-wl-asof>' + L.symbols.length + " stock" + (L.symbols.length === 1 ? "" : "s") + "</span>" +
      (L.draft ? ' · <span>suggested from today\'s top BUY signals: add or remove a stock to keep it</span>' : "") + "</div>";
    wlBind(box);
  }
  function wlBoxes() { return Array.prototype.slice.call(document.querySelectorAll("[data-ax-wl-rail],[data-ax-wl-page]")); }
  function wlRenderAll() { wlBoxes().forEach(wlRender); }
  function wlPaintQuotes() {
    wlBoxes().forEach(function (box) {
      box.querySelectorAll(".ax-wl-row").forEach(function (row) {
        var q = WL.quotes[row.getAttribute("data-sym")]; if (!q) return;
        var px = row.querySelector("[data-px]"), ch = row.querySelector("[data-chg]");
        if (px) px.textContent = q.ltp != null ? F().price(q.ltp) : "—";
        if (ch) ch.innerHTML = F().pctHtml(q.change_pct, false, "No change yet");
      });
      var a = box.querySelector("[data-wl-asof]");
      if (a) a.textContent = wlActive().symbols.length + " stocks · prices " + new Date().toTimeString().slice(0, 5) + " IST";
    });
  }
  function wlQuotes() {
    var syms = wlActive().symbols; if (!syms.length || !visible()) return;
    AX.api("/api/v2/quotes?symbols=" + encodeURIComponent(syms.join(","))).then(function (d) {
      var first = !Object.keys(WL.quotes).length;
      Object.keys(d.quotes || {}).forEach(function (k) { WL.quotes[k] = d.quotes[k]; });
      if (first) wlRenderAll(); else wlPaintQuotes();
    }).catch(function () {});
  }
  function wlAdd(sym) {
    var L = wlActive(); sym = String(sym || "").toUpperCase(); if (!sym) return;
    if (L.symbols.indexOf(sym) >= 0) { AX.toast(sym + " is already in " + L.name + "."); return; }
    L.symbols.push(sym); wlRenderAll();
    wlSave(L).then(function () { AX.toast(sym + " added to " + L.name + "."); wlQuotes(); });
  }
  function wlBind(box) {
    box.querySelectorAll("[data-wl-i]").forEach(function (b) { b.addEventListener("click", function () { WL.idx = +b.getAttribute("data-wl-i"); store("atip-wl-idx", String(WL.idx)); WL.quotes = {}; wlRenderAll(); wlQuotes(); }); });
    var nb = box.querySelector("[data-wl-new]");
    if (nb) nb.addEventListener("click", function () {
      var n = prompt("Name of the new watchlist", "Watchlist " + (WL.lists.length + 1)); if (!n) return;
      var L = { id: "", name: n.slice(0, 40), symbols: [] }; WL.lists.push(L); WL.idx = WL.lists.length - 1; wlRenderAll(); wlSave(L);
    });
    var mb = box.querySelector("[data-wl-menu]");
    if (mb) mb.addEventListener("click", function () {
      var L = wlActive(), n = prompt("Rename “" + L.name + "” (leave empty to delete it)", L.name);
      if (n === null) return;
      if (n.trim()) { L.name = n.trim().slice(0, 40); wlRenderAll(); wlSave(L); return; }
      if (!L.id) { WL.lists.splice(WL.idx, 1); WL.idx = 0; if (!WL.lists.length) WL.lists.push({ id: "", name: "My watchlist", symbols: [] }); wlRenderAll(); return; }
      if (!confirm("Delete the watchlist “" + L.name + "”? Its stocks are not affected.")) return;
      AX.api("/api/v2/watchlists/" + encodeURIComponent(L.id) + "/delete", { body: {} }).then(function () {
        WL.lists.splice(WL.idx, 1); WL.idx = 0; if (!WL.lists.length) WL.lists.push({ id: "", name: "My watchlist", symbols: [] }); wlRenderAll();
      }).catch(function (e) { AX.toast(e.message, true); });
    });
    box.querySelectorAll("[data-ax-wl-rm]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation(); var L = wlActive(), s = b.getAttribute("data-ax-wl-rm");
        L.symbols = L.symbols.filter(function (x) { return x !== s; }); wlRenderAll(); wlSave(L);
      });
    });
    box.querySelectorAll(".ax-wl-row").forEach(function (row) {
      var set = function () { current = row.getAttribute("data-sym"); };
      row.addEventListener("pointerenter", set); row.addEventListener("focusin", set);
    });
    var inp = box.querySelector("[data-wl-q]"), sug = box.querySelector(".ax-wl-sugg"), t = null;
    if (inp) {
      inp.addEventListener("input", function () {
        clearTimeout(t); var v = inp.value.trim(); if (v.length < 2) { sug.hidden = true; return; }
        t = setTimeout(function () {
          AX.api("/api/v2/search?q=" + encodeURIComponent(v)).then(function (d) {
            var rows = (d.symbols || []).slice(0, 8);
            sug.innerHTML = rows.map(function (r) { return '<button type="button" role="option" data-add="' + esc(r.symbol) + '"><b>' + esc(r.symbol) + "</b><span class=\"ax-muted\">" +
              (r.signal ? esc(r.signal) + " · " : "") + (r.atip_score != null ? "ATIP " + F().score(r.atip_score) : "") + "</span></button>"; }).join("") ||
              '<div class="ax-wl-empty">No stock matches “' + esc(v) + '”.</div>';
            sug.hidden = false;
            sug.querySelectorAll("[data-add]").forEach(function (b) { b.addEventListener("click", function () { wlAdd(b.getAttribute("data-add")); inp.value = ""; sug.hidden = true; }); });
          }).catch(function () {});
        }, 180);
      });
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); var b = sug.querySelector("[data-add]"); if (b) b.click(); } if (e.key === "Escape") { sug.hidden = true; } });
    }
    box.querySelectorAll("[data-ax-wl-toggle]").forEach(function (b) { b.addEventListener("click", function () { wlToggle(false); }); });
  }
  function wlToggle(open) {
    var rail = document.getElementById("ax-wl"); if (!rail) return;
    if (open === undefined) open = rail.hidden;
    rail.hidden = !open; document.documentElement.classList.toggle("ax-wl-open", open);
    document.querySelectorAll(".ax-top [data-ax-wl-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", open ? "true" : "false"); });
    store("atip-wl", open ? "open" : "closed");
    if (open && !WL.loaded) wlLoad();
    if (open) { var i = rail.querySelector("[data-wl-q]"); if (i && window.innerWidth < 1280) i.focus(); }
  }
  function wlLoad() {
    WL.loaded = true;
    return AX.api("/api/v2/watchlists").then(function (d) {
      WL.lists = d.lists || []; WL.idx = Math.min(+(store("atip-wl-idx") || 0) || 0, Math.max(0, WL.lists.length - 1));
      wlRenderAll(); wlQuotes(); clearInterval(WL.timer); WL.timer = setInterval(wlQuotes, 15000);
    }).catch(function (e) { wlBoxes().forEach(function (b) { b.innerHTML = '<div class="ax-wl-empty">The watchlist could not load: ' + esc(e.message) + "</div>"; }); });
  }
  AX.watchlistAdd = function (sym) { (WL.loaded ? Promise.resolve() : wlLoad()).then(function () { wlAdd(sym); }); };

  // ── W2 the order ticket ────────────────────────────────────────────────────────────────────
  var TK = { st: null, timer: null, seq: 0 };
  function num(v) { var f = F().num(v); return f; }
  function tkHtml(st) {
    var f = F();
    return '<div class="ax-tk-side" role="group" aria-label="Side"><button type="button" data-side="BUY" aria-pressed="' + (st.side === "BUY") + '">Buy</button>' +
      '<button type="button" data-side="SELL" aria-pressed="' + (st.side === "SELL") + '">Sell</button></div>' +
      '<div class="ax-tk-quote"><div><b>' + esc(st.symbol) + '</b> <span class="ax-small ax-muted" data-tk-name></span></div><div class="ax-num"><span data-tk-ltp>' +
      (st.ltp ? f.price(st.ltp) : "—") + '</span> <span class="ax-small ax-muted" data-tk-src>' + esc(st.src || "") + "</span></div></div>" +
      '<form class="ax-tk-grid" data-tk-form novalidate>' +
      '<div class="ax-field ax-full"><span class="ax-label" id="tk-qm">Size by</span><div class="ax-seg" role="group" aria-labelledby="tk-qm">' +
      '<button type="button" data-qm="SHARES" aria-pressed="' + (st.qm === "SHARES") + '">Quantity</button><button type="button" data-qm="AMOUNT" aria-pressed="' + (st.qm === "AMOUNT") + '">₹ Amount</button></div></div>' +
      '<div class="ax-field"><label for="tk-q">' + (st.qm === "SHARES" ? "Quantity (shares)" : "Amount (₹)") + '</label><input class="ax-input" id="tk-q" name="q" inputmode="decimal" autocomplete="off" value="' + esc(st.q || "") + '"><div class="ax-help" data-tk-qhelp></div></div>' +
      '<div class="ax-field"><label for="tk-trig">Trigger price (₹)</label><input class="ax-input" id="tk-trig" name="trig" inputmode="decimal" aria-describedby="tk-trig-h" value="' + esc(st.trig || "") + '"><div class="ax-help" id="tk-trig-h">' + (st.side === "BUY" ? "Buys at or below this" : "Sells at or above this") + "</div></div>" +
      '<div class="ax-field"><label for="tk-prod">Product</label><select class="ax-select" id="tk-prod" name="prod"><option value="CNC"' + (st.prod === "CNC" ? " selected" : "") + '>Delivery (CNC)</option><option value="INTRADAY"' + (st.prod === "INTRADAY" ? " selected" : "") + '>Intraday (MIS)</option></select></div>' +
      '<div class="ax-field"><label for="tk-ot">Price type</label><select class="ax-select" id="tk-ot" name="ot"><option value="MARKET"' + (st.ot === "MARKET" ? " selected" : "") + '>Market</option><option value="LIMIT"' + (st.ot === "LIMIT" ? " selected" : "") + '>Limit</option></select></div>' +
      '<div class="ax-field ax-full" data-tk-limit' + (st.ot === "LIMIT" ? "" : " hidden") + '><label for="tk-lim">Limit price (₹)</label><input class="ax-input" id="tk-lim" name="lim" inputmode="decimal" value="' + esc(st.lim || "") + '"></div>' +
      '<div class="ax-field"><label for="tk-sl">Stop-loss (₹)</label><input class="ax-input" id="tk-sl" name="sl" inputmode="decimal" value="' + esc(st.sl || "") + '"></div>' +
      '<div class="ax-field"><label for="tk-tg">Target (₹)</label><input class="ax-input" id="tk-tg" name="tg" inputmode="decimal" value="' + esc(st.tg || "") + '"></div>' +
      '<label class="ax-check ax-full" style="margin-bottom:8px"><input type="checkbox" name="auto"' + (st.auto ? " checked" : "") + '><span>Stop-loss and target exits fire without asking me</span></label>' +
      '</form><div class="ax-tk-sum" aria-live="polite" data-tk-sum><div class="ax-skel" style="height:60px"></div></div>' +
      '<p class="ax-small ax-muted">ATIP saves this as an order rule and <b>asks you to confirm</b> before the entry is placed (Trade › Command Center). ' +
      'It never sends while the kill switch is on.</p>' +
      '<div class="ax-tk-foot"><button type="button" class="ax-btn ghost" data-tk-adv>Advanced: % trigger, split targets, trailing</button>' +
      '<button type="button" class="ax-btn" data-ax-close>Cancel</button><button type="button" class="ax-btn primary" data-tk-save disabled>Review…</button></div>';
  }
  function tkRead() {
    var st = TK.st, fm = document.querySelector("[data-tk-form]"); if (!fm) return st;
    st.q = fm.q.value.replace(/[,₹\s]/g, ""); st.trig = fm.trig.value.replace(/[,₹\s]/g, ""); st.prod = fm.prod.value; st.ot = fm.ot.value;
    st.lim = fm.lim.value.replace(/[,₹\s]/g, ""); st.sl = fm.sl.value.replace(/[,₹\s]/g, ""); st.tg = fm.tg.value.replace(/[,₹\s]/g, ""); st.auto = fm.auto.checked;
    return st;
  }
  function tkQty(st) {
    var px = num(st.ot === "LIMIT" ? st.lim : st.trig) || num(st.ltp), q = num(st.q);
    if (!q || q <= 0) return null;
    return st.qm === "SHARES" ? Math.floor(q) : (px ? Math.floor(q / px) : null);
  }
  function tkPreview() {
    var st = tkRead(), sum = document.querySelector("[data-tk-sum]"), save = document.querySelector("[data-tk-save]"), f = F();
    var qty = tkQty(st), entry = num(st.ot === "LIMIT" ? st.lim : st.trig) || num(st.ltp);
    var qh = document.querySelector("[data-tk-qhelp]");
    if (qh) qh.textContent = st.qm === "AMOUNT" ? (qty ? "= " + qty + " shares at " + f.price(entry) : "") : (qty && entry ? "≈ " + f.inr(qty * entry) : "");
    if (!qty) { sum.innerHTML = '<p class="ax-small ax-muted">Enter a ' + (st.qm === "SHARES" ? "quantity" : "rupee amount") + " to see the ₹ risk, charges and checks.</p>"; save.disabled = true; return; }
    var q = "symbol=" + encodeURIComponent(st.symbol) + "&side=" + st.side + "&qty=" + qty + (entry ? "&entry=" + entry : "") +
      (num(st.sl) ? "&stop=" + num(st.sl) : "") + (num(st.tg) ? "&target=" + num(st.tg) : "");
    var my = ++TK.seq;
    AX.api("/api/command/preview?" + q).then(function (p) {
      if (my !== TK.seq) return;
      var gap = (p.checks || []).filter(function (c) { return /signal price/i.test(c.check); })[0];
      var budget = p.loss_budget || {}, value = p.value, margin = st.prod === "INTRADAY" ? value * 0.2 : value;
      var ch = p.charges || {}, chTot = (ch.entry || 0) + ((p.target ? ch.exit_at_target : ch.exit_at_stop) || 0);
      var rows = [
        ["Order value", f.inr(value)],
        ["₹ at risk (stop + charges)", p.net_risk_rs != null ? '<span class="ax-neg">' + f.inr(p.net_risk_rs) + "</span>" : '<span class="ax-muted" title="Add a stop-loss to see the risk">no stop set</span>'],
        ["Reward : risk", p.rr != null ? f.number(p.rr, 2) + " : 1" : "—"],
        ["Charges (in + out, est.)", f.inr(chTot)],
        ["Margin needed", f.inr(margin) + (st.prod === "INTRADAY" ? ' <span class="ax-small ax-muted">≈20%, the broker sets it</span>' : "")],
        ["Loss budget left today", budget.left != null ? f.inr(budget.left) + (budget.cap ? ' <span class="ax-small ax-muted">of ' + f.inr(budget.cap) + "</span>" : "") : '<span class="ax-muted" title="No daily loss limit is set">no limit set</span>'],
        ["EOD signal vs live price", gap ? esc(gap.detail) : '<span class="ax-muted">no ATIP signal for this stock today</span>']];
      var checks = (p.checks || []).map(function (c) { return '<li class="' + (c.ok ? "" : c.blocking ? "bad" : "warn") + '">' + (c.ok ? "✓ " : c.blocking ? "✗ " : "! ") + esc(c.check) + (c.detail ? ": " + esc(c.detail) : "") + "</li>"; }).join("");
      sum.innerHTML = "<dl>" + rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("") + "</dl><ul class=\"ax-tk-checks\">" + checks + "</ul>";
      TK.preview = p; TK.qty = qty;
      save.disabled = !p.ok; save.textContent = p.ok ? (st.side === "BUY" ? "Save buy rule" : "Save sell rule") : "Fix the ✗ above first";
    }).catch(function (e) { if (my !== TK.seq) return; sum.innerHTML = '<p class="ax-small ax-neg">' + esc(e.message) + "</p>"; save.disabled = true; save.textContent = "Review…"; });
  }
  function tkSave() {
    var st = tkRead(), p = TK.preview, qty = TK.qty; if (!p || !p.ok || !qty) return;
    var trig = num(st.trig) || num(st.ltp), sl = num(st.sl), tg = num(st.tg);
    var pct = function (x) { return Math.round(Math.abs(x - trig) / trig * 100000) / 1000; };
    var payload = { symbol: st.symbol, side: st.side, trigger_type: "PRICE", trigger_value: trig, trigger_percent: null,
      reference_price: num(st.ltp) || trig, quantity_type: st.qm, quantity_value: st.qm === "SHARES" ? qty : num(st.q),
      product_type: st.prod, order_type: st.ot, limit_price: st.ot === "LIMIT" ? num(st.lim) : null,
      require_confirmation: true, notes: "Order ticket " + new Date().toISOString().slice(0, 16).replace("T", " ") };
    if (sl || tg) { payload.bracket_stop_pct = sl ? pct(sl) : null; payload.bracket_target_pct = tg ? pct(tg) : null; payload.bracket_auto_exit = !!st.auto; }
    var save = document.querySelector("[data-tk-save]"); save.disabled = true; save.textContent = "Saving…";
    AX.api("/api/orders", { body: payload }).then(function () {
      AX.toast((st.side === "BUY" ? "Buy" : "Sell") + " rule saved for " + st.symbol + ". ATIP asks you to confirm when it triggers.");
      AX.closeDrawer("ax-ticket"); strip();
    }).catch(function (e) { save.disabled = false; save.textContent = "Try again"; AX.toast(e.message, true); });
  }
  AX.ticket = {
    open: function (o) {
      o = o || {}; var sym = String(o.symbol || current || "").toUpperCase(); if (!sym) { AX.toast("Pick a stock first: hover a watchlist row or open a stock."); return; }
      var ltp = num(o.ltp) || (WL.quotes[sym] || {}).ltp || null;
      TK.st = { symbol: sym, side: (o.side || "BUY").toUpperCase() === "SELL" ? "SELL" : "BUY", ltp: ltp, src: ltp ? "" : "", qm: "SHARES", q: o.qty || "",
        trig: o.entry ? F().roundTick(o.entry) : (ltp ? F().roundTick(ltp) : ""), prod: "CNC", ot: "MARKET", lim: "", sl: o.stop ? F().roundTick(o.stop) : "",
        tg: o.target ? F().roundTick(o.target) : "", auto: true };
      var d = document.getElementById("ax-ticket"); if (!d) return;
      d.setAttribute("data-side", TK.st.side); d.querySelector("#ax-ticket-h").textContent = (TK.st.side === "BUY" ? "Buy " : "Sell ") + sym;
      tkMount(); AX.openDrawer(d);
      AX.api("/api/v2/quotes?symbols=" + encodeURIComponent(sym)).then(function (r) {
        var q = (r.quotes || {})[sym]; if (!q || !TK.st || TK.st.symbol !== sym) return;
        TK.st.ltp = q.ltp; var el = d.querySelector("[data-tk-ltp]"), src = d.querySelector("[data-tk-src]"), nm = d.querySelector("[data-tk-name]");
        if (el) el.textContent = F().price(q.ltp); if (src) src.textContent = q.live ? "live" : "last close"; if (nm) nm.textContent = q.name || "";
        var fm = d.querySelector("[data-tk-form]"); if (fm && !fm.trig.value && q.ltp) fm.trig.value = F().roundTick(q.ltp);
        tkPreview();
      }).catch(function () { tkPreview(); });
    }
  };
  function tkMount() {
    var d = document.getElementById("ax-ticket"), b = d.querySelector("[data-ax-ticket-body]"); b.innerHTML = tkHtml(TK.st);
    var deb = null, later = function () { clearTimeout(deb); deb = setTimeout(tkPreview, 280); };
    b.querySelectorAll("[data-side]").forEach(function (x) { x.addEventListener("click", function () { tkRead(); TK.st.side = x.getAttribute("data-side"); d.setAttribute("data-side", TK.st.side); d.querySelector("#ax-ticket-h").textContent = (TK.st.side === "BUY" ? "Buy " : "Sell ") + TK.st.symbol; tkMount(); tkPreview(); }); });
    b.querySelectorAll("[data-qm]").forEach(function (x) { x.addEventListener("click", function () { tkRead(); var to = x.getAttribute("data-qm"); if (to === TK.st.qm) return; var qty = tkQty(TK.st), px = num(TK.st.trig) || num(TK.st.ltp);
      TK.st.q = to === "AMOUNT" ? (qty && px ? String(Math.round(qty * px)) : "") : (qty ? String(qty) : ""); TK.st.qm = to; tkMount(); tkPreview(); }); });
    var fm = b.querySelector("[data-tk-form]");
    fm.addEventListener("input", later); fm.addEventListener("change", function () { b.querySelector("[data-tk-limit]").hidden = fm.ot.value !== "LIMIT"; later(); });
    fm.addEventListener("submit", function (e) { e.preventDefault(); tkSave(); });
    b.querySelector("[data-tk-save]").addEventListener("click", tkSave);
    b.querySelector("[data-tk-adv]").addEventListener("click", function () {
      if (typeof AX.legacyOrderModal === "function") { AX.closeDrawer(d); AX.legacyOrderModal(TK.st.symbol, TK.st.ltp, TK.st.side); }
      else location.href = BASE + "/?stock=" + encodeURIComponent(TK.st.symbol);
    });
    var q = b.querySelector("#tk-q"); if (q) setTimeout(function () { q.focus(); }, 80);
  }

  // ── W5 the P&L strip ───────────────────────────────────────────────────────────────────────
  function strip() {
    var el = document.querySelector("[data-ax-pnlstrip]"); if (!el || !visible()) return;
    AX.api("/api/v2/pnl-strip").then(function (d) {
      var f = F(), t = d.today || {}, lb = d.loss_budget || {};
      el.querySelector('[data-k="today"]').innerHTML = f.inrHtml(t.total, true) + ' <span class="ax-small ax-muted">realised ' + f.inr(t.realised, true) + " · open " + f.inr(t.open, true) + "</span>";
      el.querySelector('[data-k="open"]').textContent = String(d.open_positions || 0);
      el.querySelector('[data-k="waiting"]').innerHTML = d.waiting ? '<a href="' + esc(BASE) + '/command">' + d.waiting + "</a>" : "0";
      el.querySelector('[data-k="budget"]').innerHTML = lb.cap ? (lb.used_pct != null ? f.number(lb.used_pct, 1) + "%" : "—") + ' <span class="ax-small ax-muted">of ' + f.inr(lb.cap) + "</span>" :
        '<span class="ax-muted" title="Set a daily loss limit in Settings › Risk limits">no limit set</span>';
    }).catch(function () {});
  }

  // ── W3 the pro chart and W4 depth on /stock/{symbol} ───────────────────────────────────────
  var SV = { sym: null, data: null, tf: "6M", kind: "candle", ind: { ma: 1, vol: 1, marks: 1 }, chart: null, lv: 5 };
  function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || "#888"; }
  function sma(a, n) { var o = [], s = 0; for (var i = 0; i < a.length; i++) { s += a[i]; if (i >= n) s -= a[i - n]; o.push(i >= n - 1 ? s / n : null); } return o; }
  function ema(a, n) { var o = [], k = 2 / (n + 1), p = null; for (var i = 0; i < a.length; i++) { p = p === null ? a[i] : a[i] * k + p * (1 - k); o.push(i >= n - 1 ? p : null); } return o; }
  function rsi(a, n) { var o = [null], g = 0, l = 0, al = 1 / n; for (var i = 1; i < a.length; i++) { var d = a[i] - a[i - 1], up = Math.max(d, 0), dn = Math.max(-d, 0);
      if (i === 1) { g = up; l = dn; } else { g = g * (1 - al) + up * al; l = l * (1 - al) + dn * al; } o.push(i >= n ? (l === 0 ? 100 : 100 - 100 / (1 + g / l)) : null); } return o; }
  function sd(a, n, m) { return a.map(function (_, i) { if (i < n - 1 || m[i] == null) return null; var s = 0; for (var j = i - n + 1; j <= i; j++) s += Math.pow(a[j] - m[i], 2); return Math.sqrt(s / n); }); }
  var TF = { "1M": 22, "3M": 66, "6M": 130, "1Y": 252, "3Y": 756, "All": 100000 };
  function svLoad() {
    var box = document.getElementById("ax-sv-chart"); if (!box) return;
    box.classList.add("ax-loading");
    var intraday = SV.tf === "1D" || SV.tf === "5D";
    var url = intraday ? "/api/stock/" + encodeURIComponent(SV.sym) + "/intraday?days=" + (SV.tf === "1D" ? 1 : 5) + "&interval=15"
      : "/api/stock/" + encodeURIComponent(SV.sym) + "/history?sessions=" + (SV.tf === "3Y" || SV.tf === "All" ? 2500 : 800);
    AX.api(url).then(function (d) {
      if (!intraday) SV.data = d; SV.intra = intraday ? d : null;
      box.classList.remove("ax-loading"); svDraw(); if (!intraday) { svStats(); svSignals(); }
    }).catch(function (e) { box.classList.remove("ax-loading"); box.innerHTML = '<div class="ax-empty">The chart could not load: ' + esc(e.message) + "</div>"; });
  }
  function svBars() {
    if (SV.intra) {
      return (SV.intra.bars || []).map(function (b) { var ts = Date.parse(String(b.ts || b.bar_ts || b.time).replace(" ", "T") + "+05:30") / 1000;
        return { time: ts + 19800, open: +b.open, high: +b.high, low: +b.low, close: +b.close, volume: +(b.volume || 0) }; }).filter(function (b) { return isFinite(b.time); });
    }
    var px = (SV.data && SV.data.prices) || [], n = TF[SV.tf] || 130;
    return px.slice(-n).map(function (p) { return { time: p.date, open: +(p.open || p.close), high: +(p.high || p.close), low: +(p.low || p.close), close: +p.close, volume: +(p.volume || 0) }; });
  }
  function svDraw() {
    var box = document.getElementById("ax-sv-chart"), LW = window.LightweightCharts;
    if (!box) return;
    if (!LW) { box.innerHTML = '<div class="ax-empty">The chart library did not load. Reload the page.</div>'; return; }
    if (SV.chart) { SV.chart.remove(); SV.chart = null; }
    box.innerHTML = "";
    var bars = svBars(); if (!bars.length) { box.innerHTML = '<div class="ax-empty">' + esc((SV.intra && SV.intra.message) || "No bars for this range.") + "</div>"; return; }
    var text = css("--ax-text-2"), grid = css("--ax-grid"), up = css("--ax-pos"), dn = css("--ax-neg"), s1 = css("--ax-series-1"), s2 = css("--ax-series-2");
    var chart = LW.createChart(box, { autoSize: true, layout: { background: { type: "solid", color: css("--ax-surface") }, textColor: text, fontFamily: css("--ax-font") },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } }, rightPriceScale: { borderColor: css("--ax-border") },
      timeScale: { borderColor: css("--ax-border"), timeVisible: !!SV.intra, secondsVisible: false }, crosshair: { mode: 0 },
      localization: { priceFormatter: function (p) { return F().number(p, 2); } } });
    SV.chart = chart;
    var main = SV.kind === "line" ? chart.addSeries(LW.LineSeries, { color: s1, lineWidth: 2 })
      : chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: dn, borderUpColor: up, borderDownColor: dn, wickUpColor: up, wickDownColor: dn });
    main.setData(SV.kind === "line" ? bars.map(function (b) { return { time: b.time, value: b.close }; }) : bars);
    SV.main = main; SV.bars = bars;
    var closes = bars.map(function (b) { return b.close; });
    var line = function (vals, color, w, style, pane) { var s = chart.addSeries(LW.LineSeries, { color: color, lineWidth: w || 1, lineStyle: style || 0, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, pane || 0);
      s.setData(vals.map(function (v, i) { return v == null ? { time: bars[i].time } : { time: bars[i].time, value: v }; })); return s; };
    if (SV.ind.ma) { line(sma(closes, 20), s1, 1); line(sma(closes, 50), s2, 1); line(sma(closes, 200), text, 1, 2); }
    if (SV.ind.bb) { var m = sma(closes, 20), d = sd(closes, 20, m); line(m.map(function (v, i) { return v == null ? null : v + 2 * d[i]; }), s1, 1, 2); line(m.map(function (v, i) { return v == null ? null : v - 2 * d[i]; }), s1, 1, 2); }
    var pane = 0;
    if (SV.ind.vol) { pane++; var vs = chart.addSeries(LW.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, pane);
      vs.setData(bars.map(function (b) { return { time: b.time, value: b.volume, color: b.close >= b.open ? up : dn }; })); }
    if (SV.ind.rsi) { pane++; var rs = line(rsi(closes, 14), s1, 1.5, 0, pane); rs.createPriceLine({ price: 70, color: grid, lineStyle: 2, axisLabelVisible: false }); rs.createPriceLine({ price: 30, color: grid, lineStyle: 2, axisLabelVisible: false }); }
    if (SV.ind.macd) { pane++; var e12 = ema(closes, 12), e26 = ema(closes, 26), ml = closes.map(function (_, i) { return e12[i] != null && e26[i] != null ? e12[i] - e26[i] : null; });
      var sg = ema(ml.map(function (v) { return v == null ? 0 : v; }), 9).map(function (v, i) { return i >= 33 ? v : null; });
      var hs = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
      hs.setData(ml.map(function (v, i) { return v == null || sg[i] == null ? { time: bars[i].time } : { time: bars[i].time, value: v - sg[i], color: v - sg[i] >= 0 ? up : dn }; }));
      line(ml, s1, 1.5, 0, pane); line(sg, s2, 1, 0, pane); }
    try { chart.panes().forEach(function (p, i) { if (i > 0) p.setHeight(90); }); } catch (e) {}
    // ATIP BUY / SELL markers and the stop / target lines of the active rules
    if (SV.ind.marks && SV.data && !SV.intra && LW.createSeriesMarkers) {
      var have = {}; bars.forEach(function (b) { have[b.time] = 1; });
      var mk = (SV.data.signals || []).filter(function (s) { return have[s.signal_date] && (s.signal === "BUY" || s.signal === "SELL"); }).map(function (s) {
        return { time: s.signal_date, position: s.signal === "BUY" ? "belowBar" : "aboveBar", color: s.signal === "BUY" ? up : dn, shape: s.signal === "BUY" ? "arrowUp" : "arrowDown", text: "ATIP " + s.signal };
      }).sort(function (a, b) { return a.time < b.time ? -1 : 1; });
      LW.createSeriesMarkers(main, mk);
    }
    ((SV.data && SV.data.order_rules) || []).filter(function (r) { return r.status === "ACTIVE" && r.resolved_trigger_price; }).forEach(function (r) {
      var stop = r.side === "SELL" && (r.role === "STOP" || +r.resolved_trigger_price < (bars[bars.length - 1] || {}).close);
      main.createPriceLine({ price: +r.resolved_trigger_price, color: r.side === "BUY" ? s1 : stop ? dn : up, lineStyle: 2, lineWidth: 1, axisLabelVisible: true,
        title: r.side === "BUY" ? "Buy rule" : stop ? "Stop" : "Target" });
    });
    // crosshair + the OHLC legend
    var leg = document.getElementById("ax-sv-legend");
    var show = function (b) { if (!leg || !b) return; var ch = b.open ? (b.close - b.open) / b.open * 100 : null;
      leg.innerHTML = esc(typeof b.time === "string" ? b.time : new Date((b.time - 19800) * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) + " IST") +
        " · O " + F().number(b.open) + " · H " + F().number(b.high) + " · L " + F().number(b.low) + " · C " + F().number(b.close) + " " + F().pctHtml(ch) + " · Vol " + F().number(b.volume, 0); };
    var byTime = {}; bars.forEach(function (b) { byTime[b.time] = b; });
    chart.subscribeCrosshairMove(function (p) { var b = p && p.time !== undefined ? byTime[p.time] : null; show(b || bars[bars.length - 1]); });
    show(bars[bars.length - 1]);
    chart.timeScale().fitContent();
    var note = document.getElementById("ax-sv-note");
    if (note) note.textContent = SV.intra ? "15-minute bars, 09:15–15:30 IST." : "Daily bars to the close of " + (bars[bars.length - 1].time) + "; the last bar moves with the live price in market hours.";
  }
  function liveStats(st, ltp) { var o = {}, k; for (k in st) o[k] = st[k];
    ["high_52w", "high_all"].forEach(function (q) { if (o[q] != null) o[q] = Math.max(+o[q], ltp); }); ["low_52w", "low_all"].forEach(function (q) { if (o[q] != null) o[q] = Math.min(+o[q], ltp); });
    var last = +o.last_close; if (last > 0) Object.keys(o).forEach(function (q) { if (q.indexOf("ret_") === 0 && o[q] != null) { var base = last / (1 + o[q] / 100); if (base) o[q] = Math.round((ltp / base - 1) * 10000) / 100; } }); return o; }
  function svStats() {
    var box = document.getElementById("ax-sv-stats"); if (!box || !SV.data) return; var f = F();
    var s = SV.ltp ? liveStats(SV.data.stats || {}, SV.ltp) : (SV.data.stats || {});
    var sc = (SV.data.scores || []).slice(-1)[0] || {};
    box.innerHTML = [["52W high", f.price(s.high_52w)], ["52W low", f.price(s.low_52w)], ["1W", f.pctHtml(s.ret_1w)], ["1M", f.pctHtml(s.ret_1m)], ["3M", f.pctHtml(s.ret_3m)],
      ["1Y", f.pctHtml(s.ret_1y)], ["Avg vol 20D", f.number(s.avg_volume_20d, 0)], ["ATIP score", f.score(sc.atip_score) + (sc.date ? ' <span class="ax-small ax-muted">' + esc(sc.date) + "</span>" : "")]]
      .map(function (x) { return "<div><span>" + x[0] + "</span><b>" + x[1] + "</b></div>"; }).join("");
  }
  function svSignals() {
    var box = document.getElementById("ax-sv-signals"); if (!box || !SV.data) return; var f = F();
    var rows = (SV.data.signals || []).slice(0, 12);
    box.innerHTML = rows.length ? '<div class="ax-tablewrap" style="max-height:none"><table class="ax-table"><thead><tr><th scope="col">Date</th><th scope="col">Signal</th><th class="n" scope="col">Entry</th><th class="n" scope="col">ATIP</th><th class="n" scope="col">Best move</th><th class="n" scope="col">Worst move</th></tr></thead><tbody>' +
      rows.map(function (r) { return "<tr><td>" + esc(r.signal_date) + '</td><td><span class="ax-wl-sig ' + esc(r.signal) + '">' + esc(r.signal) + '</span></td><td class="n">' + f.price(r.entry_price) + '</td><td class="n">' + f.score(r.atip_score) +
        '</td><td class="n">' + f.pctHtml(r.best_pct) + '</td><td class="n">' + f.pctHtml(r.worst_pct) + "</td></tr>"; }).join("") + "</tbody></table></div>"
      : '<div class="ax-empty">No ATIP BUY or SELL signal for this stock yet. Signals are logged after the evening scoring (18:30 IST).</div>';
  }
  function svQuote() {
    if (!visible()) return;
    AX.api("/api/v2/quotes?symbols=" + encodeURIComponent(SV.sym)).then(function (d) {
      var q = (d.quotes || {})[SV.sym]; if (!q || q.ltp == null) return; var f = F();
      SV.ltp = q.live ? q.ltp : null;
      var l = document.querySelector("[data-ax-ltp]"), c = document.querySelector("[data-ax-chg]"), s = document.querySelector("[data-ax-src]");
      if (l) l.textContent = f.price(q.ltp); if (c) c.innerHTML = f.pctHtml(q.change_pct); if (s) s.textContent = q.live ? "live · " + String(q.at || "").slice(11, 16) + " IST" : "close " + (q.at || "");
      if (q.live && SV.main && SV.bars && SV.bars.length && !SV.intra) {
        var last = SV.bars[SV.bars.length - 1], today = new Date().toISOString().slice(0, 10);
        if (last.time === today) { last.close = q.ltp; last.high = Math.max(last.high, q.ltp); last.low = Math.min(last.low, q.ltp); SV.main.update(SV.kind === "line" ? { time: last.time, value: q.ltp } : last); }
      }
      if (q.live) svStats();
    }).catch(function () {});
  }
  function depth() {
    var box = document.getElementById("ax-depth"); if (!box || !visible()) return;
    AX.api("/api/v2/depth/" + encodeURIComponent(SV.sym) + "?levels=" + SV.lv).then(function (d) {
      var f = F();
      if (d.feed !== "ON" || !d.bids) {
        var ls = d.last_snapshot;
        box.innerHTML = '<div class="ax-depth-off">' + esc(d.message || "No depth.") + "</div>" + (ls ? '<p class="ax-small ax-muted">Last stored best bid ' + f.price(ls.best_bid) +
          " × " + f.number(ls.best_bid_qty, 0) + " · best ask " + f.price(ls.best_ask) + " × " + f.number(ls.best_ask_qty, 0) + " (" + esc(String(ls.ts).replace("T", " ").slice(0, 16)) + " IST)</p>" : "");
        return;
      }
      var mx = Math.max.apply(null, d.bids.concat(d.asks).map(function (x) { return x.qty; }).concat([1])), n = Math.max(d.bids.length, d.asks.length), rows = "";
      for (var i = 0; i < n; i++) { var b = d.bids[i] || {}, a = d.asks[i] || {};
        rows += '<tr><td class="bq">' + (b.qty ? '<span class="bar bb" style="width:' + (b.qty / mx * 100).toFixed(1) + '%"></span>' + f.number(b.qty, 0) : "") + '</td><td class="bp">' + (b.price ? f.number(b.price, 2) : "") +
          '</td><td class="ap">' + (a.price ? f.number(a.price, 2) : "") + '</td><td class="aq">' + (a.qty ? '<span class="bar ab" style="width:' + (a.qty / mx * 100).toFixed(1) + '%"></span>' + f.number(a.qty, 0) : "") + "</td></tr>"; }
      box.innerHTML = '<table class="ax-depth"><thead><tr><th scope="col">Bid qty</th><th scope="col">Bid</th><th scope="col">Ask</th><th scope="col">Ask qty</th></tr></thead><tbody>' + rows +
        '</tbody><tfoot><tr><td>' + f.number(d.total_bid_qty, 0) + '</td><td colspan="2" style="text-align:center">' + (d.imbalance != null ? (d.imbalance >= 0 ? "buyers +" : "sellers ") + f.number(d.imbalance * 100, 0) + "%" : "") + "</td><td>" + f.number(d.total_ask_qty, 0) + "</td></tr></tfoot></table>";
    }).catch(function (e) { box.innerHTML = '<div class="ax-depth-off">' + esc(e.message) + "</div>"; });
  }
  function stockPage() {
    var m = document.querySelector('meta[name="atip-stock"]'); if (!m) return;
    SV.sym = m.content; current = SV.sym;
    document.querySelectorAll("[data-tf]").forEach(function (b) { b.addEventListener("click", function () { SV.tf = b.getAttribute("data-tf"); document.querySelectorAll("[data-tf]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); }); if (SV.tf === "1D" || SV.tf === "5D" || !SV.data || ((SV.tf === "3Y" || SV.tf === "All") && (SV.data.prices || []).length < 801)) svLoad(); else { SV.intra = null; svDraw(); } }); });
    document.querySelectorAll("[data-kind]").forEach(function (b) { b.addEventListener("click", function () { SV.kind = b.getAttribute("data-kind"); document.querySelectorAll("[data-kind]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); }); svDraw(); }); });
    document.querySelectorAll("[data-ind]").forEach(function (b) { b.addEventListener("click", function () { var k = b.getAttribute("data-ind"); SV.ind[k] = !SV.ind[k]; b.setAttribute("aria-pressed", SV.ind[k] ? "true" : "false"); svDraw(); }); });
    document.querySelectorAll("[data-lv]").forEach(function (b) { b.addEventListener("click", function () { SV.lv = +b.getAttribute("data-lv"); document.querySelectorAll("[data-lv]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); }); depth(); }); });
    var go = function () { svLoad(); svQuote(); depth(); };
    if (window.LightweightCharts) go(); else window.addEventListener("load", go);
    setInterval(svQuote, 15000); setInterval(depth, 5000);
    if (location.hash === "#depth") setTimeout(function () { var d = document.querySelector(".ax-sv-depth"); if (d) d.scrollIntoView({ block: "start" }); }, 600);
  }

  // ── wiring ────────────────────────────────────────────────────────────────────────────────
  function start() {
    // W2: every Buy / Sell button opens the one ticket; the older pages' order modal stays one click away ("Advanced")
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-ax-trade]"); if (b) { e.preventDefault(); e.stopPropagation(); AX.ticket.open({ symbol: b.getAttribute("data-sym"), side: b.getAttribute("data-ax-trade"),
        entry: b.getAttribute("data-entry"), stop: b.getAttribute("data-stop"), target: b.getAttribute("data-target"), ltp: b.getAttribute("data-ltp") }); return; }
      var w = e.target.closest && e.target.closest("[data-ax-wl-add]"); if (w) { AX.watchlistAdd(w.getAttribute("data-ax-wl-add")); return; }
      var t = e.target.closest && e.target.closest(".ax-top [data-ax-wl-toggle]"); if (t) { wlToggle(); }
    }, true);
    if (typeof window.openOrderModal === "function" && !window.openOrderModal.__ax) {
      AX.legacyOrderModal = window.openOrderModal;
      var tk = function (sym, cmp, side) { AX.ticket.open({ symbol: sym, ltp: cmp, side: side }); }; tk.__ax = true;
      window.openOrderModal = tk;
    }
    document.addEventListener("pointerover", function (e) { var r = e.target.closest && e.target.closest("[data-sym]"); if (r && !r.closest("#ax-ticket")) current = r.getAttribute("data-sym") || current; });
    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      if (/INPUT|TEXTAREA|SELECT/.test(tag) || e.metaKey || e.ctrlKey || e.altKey || (e.target && e.target.isContentEditable)) return;
      if (document.querySelector("dialog[open]")) return;
      var tkOpen = (document.getElementById("ax-ticket") || {}).classList; tkOpen = tkOpen && tkOpen.contains("open");
      if ((e.key === "b" || e.key === "B" || e.key === "s" || e.key === "S") && !tkOpen && current) { e.preventDefault(); AX.ticket.open({ symbol: current, side: /b/i.test(e.key) ? "BUY" : "SELL" }); }
      else if ((e.key === "w" || e.key === "W") && !tkOpen && document.getElementById("ax-wl")) { e.preventDefault(); wlToggle(); }
    });
    var rail = document.getElementById("ax-wl");
    if (rail && window.innerWidth >= 768 && !document.documentElement.classList.contains("ax-app")) {
      var pref = store("atip-wl"), want = pref === "open" || (pref !== "closed" && /^\/(command|stock\/|trading)/.test(path) && window.innerWidth >= 1440);
      if (want) wlToggle(true);
    }
    if (document.querySelector("[data-ax-wl-page]")) wlLoad();
    stockPage();
    if (document.querySelector("[data-ax-pnlstrip]")) { strip(); setInterval(strip, 30000); }
    document.addEventListener("visibilitychange", function () { if (visible()) { wlQuotes(); strip(); } });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
