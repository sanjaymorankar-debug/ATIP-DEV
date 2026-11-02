/* ATIP shell (UXD-02..05, 2 Nov 2026): one script for every page. Theme, the Ctrl/⌘K search, the kill switch, the
   live run state / environment badge, keyboard shortcuts, and the shared behaviours of the design system's tables
   (sort, filter, column chooser, CSV export, row -> drawer), drawers, toasts and chart hover. Every write goes
   through AX.api with the page's dashboard token; the server still refuses it while ATIP is stopped (423) or on the
   view-only server (403). */
(function () {
  "use strict";
  var AX = window.AX = window.AX || {};
  var root = document.documentElement;
  var BASE = (document.querySelector('meta[name="atip-root"]') || {}).content || "";

  // ── numbers ───────────────────────────────────────────────────────────────────────────────
  AX.inr = function (v, sign) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
    v = Number(v);
    var neg = v < -0.004, s = Math.abs(v).toFixed(2).split("."), w = s[0];
    if (w.length > 3) { w = w.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + w.slice(-3); }
    return (neg ? "−" : (sign && v > 0.004 ? "+" : "")) + "₹" + w + "." + s[1];
  };
  AX.pct = function (v) { if (v === null || v === undefined || isNaN(v)) return "—"; v = Number(v); return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%"; };
  AX.esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };

  // ── token and API ─────────────────────────────────────────────────────────────────────────
  AX.token = function () {
    var m = document.querySelector('meta[name="atip-token"]');
    if (m && m.content) return m.content;
    try { if (typeof TOKEN !== "undefined" && TOKEN) return TOKEN; } catch (e) {}
    try { if (typeof ATIP_TOKEN !== "undefined" && ATIP_TOKEN) return ATIP_TOKEN; } catch (e) {}
    return "";
  };
  AX.api = function (url, opts) {
    opts = opts || {};
    var o = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: { "X-ATIP-Token": AX.token() }, cache: "no-store" };
    if (opts.body !== undefined) {
      if (typeof opts.body === "string") { o.body = opts.body; o.headers["Content-Type"] = "text/plain"; }
      else { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
    }
    return fetch(BASE + url, o).then(function (r) {
      return r.text().then(function (t) {
        var d = null; try { d = t ? JSON.parse(t) : {}; } catch (e) { d = { error: t || ("HTTP " + r.status) }; }
        if (!r.ok) {
          var msg = (d && d.error) || ("HTTP " + r.status);
          if (r.status === 423) msg = "ATIP is stopped, so nothing can change until it resumes: " + msg;
          if (r.status === 401) msg = "This page has no permission token. Reload the page and try again.";
          var err = new Error(msg); err.status = r.status; err.data = d; throw err;
        }
        return d;
      });
    });
  };

  // ── toasts ────────────────────────────────────────────────────────────────────────────────
  AX.toast = function (msg, bad) {
    var t = document.createElement("div");
    t.className = "ax-toast" + (bad ? " bad" : ""); t.setAttribute("role", bad ? "alert" : "status"); t.textContent = msg;
    document.body.appendChild(t); setTimeout(function () { t.remove(); }, bad ? 7000 : 3500);
  };

  // ── theme ─────────────────────────────────────────────────────────────────────────────────
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  var q = new URLSearchParams(location.search);
  if (q.get("theme") === "dark" || q.get("theme") === "light") store("atip-theme", q.get("theme"));
  if (q.get("app") === "1") { try { sessionStorage.setItem("atip-app", "1"); } catch (e) {} }
  try { if (sessionStorage.getItem("atip-app") === "1") root.classList.add("ax-app"); } catch (e) {}
  function applyTheme() { var t = store("atip-theme"); if (t === "dark" || t === "light") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme"); }
  applyTheme();
  AX.cycleTheme = function () {
    var t = store("atip-theme"), next = t === "dark" ? "light" : t === "light" ? "auto" : "dark";
    store("atip-theme", next); applyTheme(); AX.toast("Theme: " + (next === "auto" ? "follows your device" : next));
  };

  // ── rail on small screens ─────────────────────────────────────────────────────────────────
  function rail(open) {
    var r = document.querySelector(".ax-rail"), s = document.querySelector(".ax-scrim"), b = document.querySelector(".ax-menu");
    if (!r) return; r.classList.toggle("open", open); if (s) s.classList.toggle("open", open); if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { var a = r.querySelector("a"); if (a) a.focus(); }
  }

  // ── live state: env badge, run state, halt, notifications ────────────────────────────────
  var state = null;
  function paint(d) {
    state = d;
    document.querySelectorAll("[data-ax-env]").forEach(function (el) { el.setAttribute("data-env", d.env); el.textContent = d.env; el.title = d.env === "LIVE" ? "LIVE: real money" : d.env === "SANDBOX" ? "SANDBOX: Dhan's test host" : "PAPER: simulated fills"; });
    var sym = { RUNNING: "●", STOPPED: "⏸", RESUMING: "↻" };
    document.querySelectorAll("[data-ax-run]").forEach(function (el) {
      el.setAttribute("data-status", d.status); el.innerHTML = (sym[d.status] || "●") + ' <span class="ax-runtext">' + AX.esc(d.banner) + "</span>";
      el.title = d.banner + (d.status === "STOPPED" ? " · read-only" : " · stops " + String(d.next_stop || "").slice(11) + " IST");
    });
    var k = document.querySelector("[data-ax-kill]");
    if (k) {
      if (d.role === "VIEWER") { k.textContent = "View only"; k.disabled = true; k.title = "Trading runs on the Mac"; }
      else { k.setAttribute("data-halted", d.halted ? "1" : "0"); k.textContent = d.halted ? "Halted" : "Kill switch"; k.title = d.halted ? ("Trading halted: " + (d.halt_reason || "")) : "Stop every order now"; }
    }
    var n = document.querySelector("[data-ax-unread]");
    if (n) { n.hidden = !d.unread; n.textContent = d.unread > 99 ? "99+" : String(d.unread || ""); }
  }
  function poll() { AX.api("/api/v2/shell/state").then(paint).catch(function () {}); }
  AX.state = function () { return state; };

  // ── kill switch ───────────────────────────────────────────────────────────────────────────
  function killDialog() {
    var dlg = document.getElementById("ax-kill-dialog"); if (!dlg) return;
    var d = state || {};
    if (d.halted) { location.href = BASE + "/command#kill"; return; }
    if (!AX.token()) { location.href = BASE + "/command#kill"; return; }
    var imp = dlg.querySelector("[data-ax-impact]");
    if (imp) imp.innerHTML = d.open_trades ? ("<b>" + d.open_trades + " open trade" + (d.open_trades > 1 ? "s" : "") + " worth " + AX.inr(d.open_value) + "</b> stay open with <b>no stop-loss or target sale</b> while trading is halted — ATIP places no order of any kind, exits included.") : "No session holds an open trade right now.";
    var run = dlg.querySelector("[data-ax-running]"); if (run) run.textContent = d.sessions_running || 0;
    dlg.showModal();
  }
  function doHalt(ev) {
    ev.preventDefault();
    var dlg = document.getElementById("ax-kill-dialog"), reason = dlg.querySelector("input[name=reason]").value;
    AX.api("/api/risk/halt", { body: { reason: reason } }).then(function (r) {
      dlg.close(); AX.toast("Trading halted. " + (r.sessions_paused || 0) + " running session(s) paused. Resume on Trade → Command Center."); poll();
    }).catch(function (e) { AX.toast(e.message, true); });
  }

  // ── search palette (Ctrl/⌘K) ──────────────────────────────────────────────────────────────
  // Data rides in data-json attributes, not <script type="application/json">: every inline <script> on a page is JS
  AX.data = function (id, dflt) { var el = document.getElementById(id); try { return el ? JSON.parse(el.getAttribute("data-json")) : dflt; } catch (e) { return dflt; } };
  var NAV = AX.data("ax-nav", []);
  var COMMANDS = [
    { label: "Halt all trading (kill switch)", kind: "command", run: killDialog },
    { label: "Switch theme (light / dark / device)", kind: "command", run: function () { AX.cycleTheme(); } },
    { label: "Set up a session's money", kind: "command", href: "/sessions#setup" },
    { label: "Move money between sessions", kind: "command", href: "/accounting?tab=transfers" },
    { label: "Generate a report now", kind: "command", href: "/reports" },
    { label: "FY tax estimate", kind: "command", href: "/accounting?tab=tax" },
    { label: "Keyboard shortcuts", kind: "command", run: function () { help(); } }
  ];
  for (var i = 1; i <= 10; i++) COMMANDS.push({ label: "Open session S" + i, kind: "session", href: "/sessions/S" + i });
  var sel = 0, items = [], timer = null;
  function paletteRender(list) {
    var ul = document.getElementById("ax-palette-list"); if (!ul) return;
    items = list; sel = 0;
    ul.innerHTML = list.slice(0, 40).map(function (it, i) {
      var inner = AX.esc(it.label) + '<span class="ax-kind">' + AX.esc(it.kind) + "</span>";
      return '<li role="option" id="ax-opt-' + i + '" aria-selected="' + (i === 0) + '">' + (it.href ? '<a href="' + AX.esc(BASE + it.href) + '" tabindex="-1">' + inner + "</a>" : '<button type="button" tabindex="-1" data-i="' + i + '">' + inner + "</button>") + "</li>";
    }).join("") || '<li class="ax-muted" style="padding:10px">Nothing matches. Try a symbol like RELIANCE, or a page like “tax”.</li>';
    ul.querySelectorAll("button[data-i]").forEach(function (b) { b.onclick = function () { pick(+b.getAttribute("data-i")); }; });
  }
  function pick(i) {
    var it = items[i]; if (!it) return; var dlg = document.getElementById("ax-palette");
    if (it.run) { dlg.close(); it.run(); } else location.href = BASE + it.href;
  }
  function paletteFilter(text) {
    var t = text.trim().toLowerCase();
    var base = NAV.concat(COMMANDS).filter(function (it) { return !t || (it.label + " " + (it.section || "") + " " + (it.keywords || "")).toLowerCase().indexOf(t) >= 0; });
    paletteRender(base);
    clearTimeout(timer);
    if (t.length >= 2) timer = setTimeout(function () {
      AX.api("/api/v2/search?q=" + encodeURIComponent(t)).then(function (d) {
        var syms = (d.symbols || []).map(function (s) { return { label: s.symbol + (s.signal ? " · " + s.signal : "") + (s.atip_score != null ? " · ATIP " + Number(s.atip_score).toFixed(0) : ""), kind: "symbol", href: "/?stock=" + encodeURIComponent(s.symbol) }; });
        paletteRender(base.concat(syms));
      }).catch(function () {});
    }, 180);
  }
  function openPalette() {
    var dlg = document.getElementById("ax-palette"); if (!dlg) return;
    var inp = dlg.querySelector("input"); inp.value = ""; paletteFilter(""); dlg.showModal(); inp.focus();
  }
  function paletteKeys(e) {
    var ul = document.getElementById("ax-palette-list");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); var n = Math.min(items.length, 40); if (!n) return;
      sel = (sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      ul.querySelectorAll("li[role=option]").forEach(function (li, i) { li.setAttribute("aria-selected", i === sel ? "true" : "false"); if (i === sel) li.scrollIntoView({ block: "nearest" }); });
      e.target.setAttribute("aria-activedescendant", "ax-opt-" + sel);
    } else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
  }

  // ── keyboard shortcuts ────────────────────────────────────────────────────────────────────
  var GO = { h: "/home", s: "/sessions", t: "/command", r: "/reports", a: "/accounting", m: "/market", l: "/strategies", w: "/wealth", e: "/settings", c: "/" };
  function help() {
    var dlg = document.getElementById("ax-help"); if (dlg) dlg.showModal();
  }
  var gPending = false;
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName) || "", typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (e.target && e.target.isContentEditable);
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); openPalette(); return; }
    if (e.key === "Escape") { rail(false); document.querySelectorAll(".ax-drawer.open").forEach(function (d) { AX.closeDrawer(d); }); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); openPalette(); return; }
    if (e.key === "?") { help(); return; }
    if (gPending && GO[e.key]) { gPending = false; location.href = BASE + GO[e.key]; return; }
    gPending = e.key === "g"; if (gPending) setTimeout(function () { gPending = false; }, 1200);
  });

  // ── drawers ───────────────────────────────────────────────────────────────────────────────
  var lastFocus = null;
  AX.openDrawer = function (el) {
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return; lastFocus = document.activeElement; el.classList.add("open"); el.setAttribute("aria-hidden", "false");
    var f = el.querySelector("input,select,textarea,button:not([data-ax-close])"); if (f) setTimeout(function () { f.focus(); }, 60);
  };
  AX.closeDrawer = function (el) {
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return; el.classList.remove("open"); el.setAttribute("aria-hidden", "true"); if (lastFocus && lastFocus.focus) lastFocus.focus();
  };
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest("[data-ax-close]"); if (c) { AX.closeDrawer(c.closest(".ax-drawer")); }
  });

  // ── tables: sort, filter, columns, CSV, row -> drawer ────────────────────────────────────
  function cellVal(td) { var v = td.getAttribute("data-v"); if (v !== null && v !== "" && !isNaN(v)) return Number(v); return (td.textContent || "").trim().toLowerCase(); }
  AX.enhanceTable = function (t) {
    if (!t || t.__ax) return; t.__ax = true;
    var ths = t.tHead ? Array.prototype.slice.call(t.tHead.rows[0].cells) : [];
    ths.forEach(function (th, i) {
      if (th.hasAttribute("data-nosort")) return;
      th.setAttribute("data-sort", ""); th.tabIndex = 0; th.setAttribute("aria-sort", "none");
      var go = function () {
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        ths.forEach(function (o) { if (o.hasAttribute("data-sort")) o.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", dir);
        var body = t.tBodies[0], rows = Array.prototype.slice.call(body.rows).filter(function (r) { return !r.classList.contains("ax-total"); });
        var totals = Array.prototype.slice.call(body.rows).filter(function (r) { return r.classList.contains("ax-total"); });
        rows.sort(function (a, b) { var x = cellVal(a.cells[i] || a), y = cellVal(b.cells[i] || b); var c = x < y ? -1 : x > y ? 1 : 0; return dir === "ascending" ? c : -c; });
        rows.concat(totals).forEach(function (r) { body.appendChild(r); });
      };
      th.addEventListener("click", go); th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
    Array.prototype.slice.call(t.tBodies[0] ? t.tBodies[0].rows : []).forEach(function (tr) {
      if (!tr.hasAttribute("data-drawer")) return;
      tr.tabIndex = 0;
      var open = function () { AX.rowDrawer(tr); };
      tr.addEventListener("click", function (e) { if (!e.target.closest("a,button,input")) open(); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") open(); });
    });
  };
  AX.rowDrawer = function (tr) {
    var d = document.getElementById("ax-row-drawer"); if (!d) return;
    var data = {}; try { data = JSON.parse(tr.getAttribute("data-drawer")); } catch (e) {}
    d.querySelector("h2").textContent = data.title || "Details";
    d.querySelector(".ax-body").innerHTML = '<dl class="ax-kv">' + (data.rows || []).map(function (r) { return "<dt>" + AX.esc(r[0]) + "</dt><dd>" + AX.esc(r[1]) + "</dd>"; }).join("") + "</dl>" + (data.link ? '<p><a class="ax-btn" href="' + AX.esc(BASE + data.link) + '">' + AX.esc(data.link_label || "Open") + "</a></p>" : "");
    AX.openDrawer(d);
  };
  AX.tableCSV = function (t, name) {
    var rows = Array.prototype.slice.call(t.rows).filter(function (r) { return r.offsetParent !== null || r.parentNode.tagName === "THEAD"; });
    var csv = rows.map(function (r) {
      return Array.prototype.slice.call(r.cells).filter(function (c) { return !c.classList.contains("ax-hidecol"); }).map(function (c) {
        var v = c.getAttribute("data-v"); v = (v !== null && v !== "") ? v : (c.textContent || "").trim();
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(",");
    }).join("\n");
    var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv" })); a.download = (name || "atip") + ".csv"; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  };
  function toolbar() {
    document.querySelectorAll("[data-ax-filter]").forEach(function (inp) {
      var t = document.getElementById(inp.getAttribute("data-ax-filter")); if (!t) return;
      inp.addEventListener("input", function () {
        var s = inp.value.trim().toLowerCase(), shown = 0;
        Array.prototype.slice.call(t.tBodies[0].rows).forEach(function (r) { if (r.classList.contains("ax-total")) return; var ok = !s || r.textContent.toLowerCase().indexOf(s) >= 0; r.style.display = ok ? "" : "none"; if (ok) shown++; });
        var c = document.querySelector('[data-ax-count="' + t.id + '"]'); if (c) c.textContent = shown + " shown";
      });
    });
    document.querySelectorAll("[data-ax-csv]").forEach(function (b) { b.addEventListener("click", function () { AX.tableCSV(document.getElementById(b.getAttribute("data-ax-csv")), b.getAttribute("data-name")); }); });
    document.querySelectorAll("[data-ax-cols]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var t = document.getElementById(b.getAttribute("data-ax-cols")), old = document.querySelector(".ax-colmenu"); if (old) { old.remove(); return; }
        var m = document.createElement("div"); m.className = "ax-colmenu"; m.setAttribute("role", "group"); m.setAttribute("aria-label", "Columns to show");
        Array.prototype.slice.call(t.tHead.rows[0].cells).forEach(function (th, i) {
          var l = document.createElement("label"), c = document.createElement("input"); c.type = "checkbox"; c.checked = !th.classList.contains("ax-hidecol");
          c.onchange = function () { Array.prototype.slice.call(t.rows).forEach(function (r) { if (r.cells[i]) r.cells[i].classList.toggle("ax-hidecol", !c.checked); }); };
          l.appendChild(c); l.appendChild(document.createTextNode(" " + th.textContent.trim())); m.appendChild(l);
        });
        var r = b.getBoundingClientRect(); m.style.left = (r.left + window.scrollX) + "px"; m.style.top = (r.bottom + window.scrollY + 4) + "px";
        document.body.appendChild(m); document.addEventListener("click", function off(ev) { if (!m.contains(ev.target)) { m.remove(); document.removeEventListener("click", off); } });
      });
    });
  }

  // ── charts: crosshair + tooltip on line charts, per-mark tooltip on bars ────────────────
  AX.charts = function () {
    document.querySelectorAll("[data-ax-line]").forEach(function (box) {
      if (box.__ax) return; box.__ax = true;
      var cfg; try { cfg = JSON.parse(box.getAttribute("data-ax-line")); } catch (e) { return; }
      var svg = box.querySelector("svg"), tip = box.querySelector(".ax-tip"), cross = svg.querySelector(".ax-cross");
      function at(ev) {
        var r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width * cfg.w;
        var n = cfg.points.length; if (!n) return;
        var i = Math.max(0, Math.min(n - 1, Math.round((x - cfg.pad) / ((cfg.w - cfg.pad - cfg.padr) / Math.max(1, n - 1)))));
        var p = cfg.points[i], px = cfg.pad + i * (cfg.w - cfg.pad - cfg.padr) / Math.max(1, n - 1);
        if (cross) { cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.style.display = ""; }
        tip.innerHTML = "<b>" + AX.esc(p.label) + "</b>" + cfg.series.map(function (s) { return "<div><i style=\"display:inline-block;width:10px;height:3px;background:var(" + s.color + ");margin-right:6px;vertical-align:middle\"></i>" + AX.esc(s.name) + " " + AX.inr(p[s.key]) + "</div>"; }).join("");
        tip.style.display = "block";
        var left = px / cfg.w * r.width; tip.style.left = Math.min(r.width - tip.offsetWidth - 4, Math.max(0, left + 10)) + "px"; tip.style.top = "8px";
      }
      svg.addEventListener("pointermove", at); svg.addEventListener("pointerdown", at);
      svg.addEventListener("pointerleave", function () { tip.style.display = "none"; if (cross) cross.style.display = "none"; });
    });
    document.querySelectorAll("[data-ax-bars]").forEach(function (box) {
      if (box.__ax) return; box.__ax = true; var tip = box.querySelector(".ax-tip");
      box.querySelectorAll("[data-tip]").forEach(function (g) {
        g.addEventListener("pointerenter", function () { tip.innerHTML = AX.esc(g.getAttribute("data-tip")); tip.style.display = "block"; });
        g.addEventListener("pointermove", function (ev) { var r = box.getBoundingClientRect(); tip.style.left = Math.min(r.width - tip.offsetWidth - 4, ev.clientX - r.left + 12) + "px"; tip.style.top = Math.max(0, ev.clientY - r.top - 34) + "px"; });
        g.addEventListener("pointerleave", function () { tip.style.display = "none"; });
      });
    });
  };

  // ── the Trade screens (section 7): each session's money on Command Center, Trade Setup and Live Monitor ──
  function tone(v) { return v > 0.004 ? "ax-pos" : v < -0.004 ? "ax-neg" : ""; }
  function bySid(d) { var o = {}; (d.sessions || []).forEach(function (s) { o[s.id] = s; }); return o; }
  function bar(p) {
    if (!p || p.pct === null || p.pct === undefined) return "";
    return '<div class="ax-progress" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + Math.round(p.pct) +
      '" aria-label="' + AX.esc("Cycle progress " + AX.pct(p.move_pct) + " from entry") + '"><span style="width:' + p.pct + '%"></span></div>' +
      '<div class="ax-progress-l"><span>stop ' + p.stop_pct + '%</span><span>now ' + AX.pct(p.move_pct) + '</span><span>target +' + p.target_pct + '%</span></div>';
  }
  function enhanceTrade() {
    var p = location.pathname.slice(BASE.length) || "/";
    if (p === "/command") {
      var box = document.getElementById("tiles"), data = null;
      if (!box) return;
      var paint = function () {
        if (!data) return;
        box.querySelectorAll("[data-sid]").forEach(function (t) {
          var s = data[t.getAttribute("data-sid")]; if (!s || !s.tiles || t.querySelector(".ax-gva-mini")) return;
          var sp = document.createElement("span"); sp.className = "ax-gva-mini ax-num ax-small";
          sp.innerHTML = 'Gross <b class="' + tone(s.tiles.gross_pnl) + '">' + AX.inr(s.tiles.gross_pnl, true) + '</b> · Actual <b class="' +
            tone(s.tiles.actual_pnl) + '">' + AX.inr(s.tiles.actual_pnl, true) + "</b>";
          t.appendChild(sp);
        });
      };
      var load = function () { AX.api("/api/v2/sessions").then(function (d) { data = bySid(d); box.querySelectorAll(".ax-gva-mini").forEach(function (x) { x.remove(); }); paint(); }).catch(function () {}); };
      new MutationObserver(paint).observe(box, { childList: true });
      load(); setInterval(load, 60000);
    }
    if (p === "/command/setup") {
      var sel = document.getElementById("ses"); if (!sel) return;
      var info = document.createElement("div"); info.className = "ax-small"; info.setAttribute("role", "status"); info.style.marginTop = "4px";
      sel.parentNode.appendChild(info);
      var show = function () {
        if (!sel.value) { info.textContent = ""; return; }
        AX.api("/api/v2/sessions/" + encodeURIComponent(sel.value)).then(function (b) {
          var t = b.tiles;
          if (!t) { info.textContent = sel.value + " has no starting amount: the order is sized as typed."; return; }
          info.innerHTML = AX.esc(sel.value) + ": <b>" + AX.inr(t.investable_cash) + "</b> free to invest · minimum per cycle <b>" + AX.inr(t.floor) + "</b> (" +
            AX.esc(String(t.floor_rule).replace("_", " ").toLowerCase()) + ")" + (t.waiting ? ' · <b class="ax-neg">' + AX.esc(t.waiting) + "</b>" : "") +
            (b.open && b.open.length ? " · holding " + AX.esc(b.open.map(function (o) { return o.symbol; }).join(", ")) : "");
        }).catch(function () { info.textContent = ""; });
      };
      sel.addEventListener("change", show); setTimeout(show, 800);
    }
    if (p === "/command/monitor") {
      var main = document.querySelector("main"); if (!main) return;
      var card = document.createElement("section"); card.className = "card"; card.setAttribute("aria-label", "Cycle progress by session");
      card.innerHTML = '<h2>Cycle progress by session</h2><div class="ax-skel" style="height:40px"></div>';
      main.insertBefore(card, main.children[2] || null);
      var draw = function () {
        AX.api("/api/v2/sessions").then(function (d) {
          card.innerHTML = "<h2>Cycle progress by session</h2>" + (d.sessions || []).filter(function (s) { return s.tiles; }).map(function (s) {
            var o = (s.open || [])[0], t = s.tiles;
            return '<div style="padding:8px 0;border-top:1px solid var(--line,#ddd)"><div class="ax-small"><b>' + AX.esc(s.id + " " + s.name) + "</b> · cycle " +
              (t.cycles_done + (o ? 1 : 0)) + " of " + t.max_cycles + " · " + (o ? AX.esc(o.symbol) + " " + AX.inr(o.unrealised, true) + " open" :
              (t.waiting ? AX.esc(t.waiting) : "waiting for a signal with " + AX.inr(t.investable_cash) + " free")) + "</div>" + (o ? bar(o.progress) : "") + "</div>";
          }).join("") || '<p class="ax-small">No session has a starting amount yet.</p>';
        }).catch(function (e) { card.innerHTML = "<h2>Cycle progress by session</h2><p class='ax-small'>" + AX.esc(e.message) + "</p>"; });
      };
      draw(); setInterval(draw, 30000);
    }
  }

  // ── start ─────────────────────────────────────────────────────────────────────────────────
  function start() {
    try { enhanceTrade(); } catch (e) {}
    document.querySelectorAll(".ax-menu").forEach(function (b) { b.addEventListener("click", function () { rail(!document.querySelector(".ax-rail.open")); }); });
    var s = document.querySelector(".ax-scrim"); if (s) s.addEventListener("click", function () { rail(false); });
    document.querySelectorAll("[data-ax-search]").forEach(function (b) { b.addEventListener("click", openPalette); });
    document.querySelectorAll("[data-ax-theme]").forEach(function (b) { b.addEventListener("click", AX.cycleTheme); });
    var k = document.querySelector("[data-ax-kill]"); if (k) k.addEventListener("click", killDialog);
    var kf = document.querySelector("#ax-kill-dialog form"); if (kf) kf.addEventListener("submit", doHalt);
    document.querySelectorAll("dialog [data-ax-dclose]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });
    var pi = document.querySelector("#ax-palette input");
    if (pi) { pi.addEventListener("input", function () { paletteFilter(pi.value); }); pi.addEventListener("keydown", paletteKeys); }
    document.querySelectorAll("table.ax-table").forEach(AX.enhanceTable);
    toolbar(); AX.charts();
    var init = AX.data("ax-state", null); if (init) { try { paint(init); } catch (e) {} }
    poll(); setInterval(poll, 30000);
    // a symbol picked in the search opens the stock panel on the ATIP Scores page
    var st = q.get("stock"); if (st && typeof window.openStock === "function") setTimeout(function () { try { window.openStock(st); } catch (e) {} }, 400);
    // ...and ?tab=news (or any of its tabs) opens that tab, so the rail can link straight to it
    var tb = q.get("tab");
    if (tb && /^[a-z]+$/.test(tb) && typeof window.showTab === "function") { var el = document.querySelector(".tab[onclick*=\"showTab('" + tb + "'\"]"); if (el) setTimeout(function () { el.click(); }, 50); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
