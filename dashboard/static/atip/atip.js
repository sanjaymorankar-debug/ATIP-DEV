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


  // ── UI fixes v2: the one formatter, mirroring dashboard/fmt.py (same text for the same value) ──────────────
  var TICKS = [[250, 0.01], [1000, 0.05], [5000, 0.10], [10000, 0.50], [20000, 1.00], [Infinity, 5.00]];
  function fnum(v) { if (v === null || v === undefined || v === "" || typeof v === "boolean") return null; var f = typeof v === "string" ? Number(v.replace(/[,₹\s]/g, "")) : Number(v); return isFinite(f) ? f : null; }
  function group(w) { return w.length > 3 ? w.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + w.slice(-3) : w; }
  AX.fmt = {
    num: fnum,
    inr: function (v, sign, compact) {
      var f = fnum(v); if (f === null) return "—";
      var pre = f < -0.004 ? "−" : (sign && f > 0.004 ? "+" : ""), a = Math.abs(f);
      if (compact) { if (a >= 1e7) return pre + "₹" + (a / 1e7).toFixed(2) + " Cr"; if (a >= 1e5) return pre + "₹" + (a / 1e5).toFixed(2) + " L"; if (a >= 1e3) return pre + "₹" + (a / 1e3).toFixed(1) + "K"; return pre + "₹" + a.toFixed(0); }
      var p = a.toFixed(2).split("."); return pre + "₹" + group(p[0]) + "." + p[1];
    },
    number: function (v, dp) { var f = fnum(v); if (f === null) return "—"; dp = dp === undefined ? 2 : dp; var p = Math.abs(f).toFixed(dp).split("."); return (f < 0 && Number(Math.abs(f).toFixed(dp)) ? "−" : "") + group(p[0]) + (dp ? "." + p[1] : ""); },
    tick: function (price) { var f = Math.abs(fnum(price) || 0); for (var i = 0; i < TICKS.length; i++) if (f < TICKS[i][0]) return TICKS[i][1]; return 5; },
    roundTick: function (price, tick) { var f = fnum(price); if (f === null) return null; var t = tick || AX.fmt.tick(f); return Math.round(Math.round(f / t) * t * 100) / 100; },
    price: function (v, tick) { var f = AX.fmt.roundTick(v, tick); return f === null ? "—" : AX.fmt.inr(f); },
    score: function (v) { var f = fnum(v); return f === null ? "—" : String(Math.floor(f + 0.5)); },
    tone: function (v) { var f = fnum(v); if (f === null) return "none"; if (Math.abs(f) < 0.005) return "flat"; return f > 0 ? "up" : "down"; },
    pct: function (v, sign) { var f = fnum(v); if (f === null) return "—"; if (Math.abs(f) < 0.005) return "0.00%"; return (sign === false ? (f < 0 ? "−" : "") : (f > 0 ? "+" : "−")) + Math.abs(f).toFixed(2) + "%"; },
    toneClass: function (v, inverse) { var t = AX.fmt.tone(v); if (t === "none") return "ax-muted"; if (t === "flat") return "ax-flat"; if (inverse) return t === "up" ? "ax-warn-t" : "ax-pos"; return t === "up" ? "ax-pos" : "ax-neg"; },
    pctHtml: function (v, inverse, why) {
      if (fnum(v) === null) return '<span class="ax-muted ax-missing" title="' + AX.esc(why || "No value") + '">—</span>';
      var t = AX.fmt.tone(v), a = t === "up" ? "▲ " : t === "down" ? "▼ " : "";
      return '<span class="ax-num ' + AX.fmt.toneClass(v, inverse) + '">' + a + AX.fmt.pct(v) + "</span>";
    },
    inrHtml: function (v, sign, why) {
      if (fnum(v) === null) return '<span class="ax-muted ax-missing" title="' + AX.esc(why || "No value") + '">—</span>';
      var t = AX.fmt.tone(v), a = sign ? (t === "up" ? "▲ " : t === "down" ? "▼ " : "") : "";
      return '<span class="ax-num ' + (sign ? AX.fmt.toneClass(v) : "") + '">' + a + AX.fmt.inr(v, sign) + "</span>";
    }
  };

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
    var n2 = document.querySelector("[data-ax-unread2]"), n3 = document.querySelector("[data-ax-unread3]");
    if (n2) { n2.hidden = !d.unread; n2.textContent = d.unread > 99 ? "99+" : String(d.unread || ""); }
    if (n3) n3.textContent = d.unread ? "(" + d.unread + " new)" : "";
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
        var syms = (d.symbols || []).map(function (s) { return { label: s.symbol + (s.signal ? " · " + s.signal : "") + (s.atip_score != null ? " · ATIP " + Number(s.atip_score).toFixed(0) : ""), kind: "symbol", href: "/stock/" + encodeURIComponent(s.symbol) }; });
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
    if (e.key === "Escape") {
      // an open dialog closes first (browsers may not, under their close-request rules), then drawers and the menu
      var dlg = document.querySelector("dialog[open]");
      if (dlg) { e.preventDefault(); dlg.close(); return; }
      rail(false); document.querySelectorAll(".ax-drawer.open").forEach(function (d) { AX.closeDrawer(d); }); return;
    }
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
    if (!el) return; lastFocus = document.activeElement; el.removeAttribute("inert"); el.classList.add("open"); el.setAttribute("aria-hidden", "false");
    var f = el.querySelector("input,select,textarea,button:not([data-ax-close])"); if (f) setTimeout(function () { f.focus(); }, 60);
  };
  AX.closeDrawer = function (el) {
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return; el.classList.remove("open"); el.setAttribute("aria-hidden", "true"); el.setAttribute("inert", ""); if (lastFocus && lastFocus.focus) lastFocus.focus();
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


  // ── UI fixes v2: older pages inside the shell ────────────────────────────────────────────────────────────
  var SHELL_SKIP = ".ax-top,.ax-rail,.ax-tabbar,.ax-dialog,.ax-drawer,.ax-seealso,.ax-ovmenu,.ax-wl,.ax-ticket";
  function pathOf(a) { try { return new URL(a.href, location.href).pathname; } catch (e) { return ""; } }
  // G2: a block of links to other ATIP pages in an older page's own top bar or side list is a second navigation bar.
  // The shell's rail and breadcrumb already do that job: the block hides and its links move to one "See also" row.
  AX.foldLegacyNav = function () {
    var main = document.getElementById("ax-main"); if (!main || document.body.classList.contains("ax-native")) return;
    var here = location.pathname, seen = {}, links = [];
    var cands = main.querySelectorAll(".top, header, nav, .nav, .links, .topbar, .tabs-links, aside.side, .side-nav, .cc-nav, [data-legacy-nav]");
    Array.prototype.forEach.call(cands, function (b) {
      if (b.closest(SHELL_SKIP) || b.__axFolded) return;
      var all = Array.prototype.slice.call(b.querySelectorAll('a[href^="/"]'));
      var as = all.filter(function (a) { var p = pathOf(a); return p && p !== here; });
      var uniq = {}; as.forEach(function (a) { uniq[pathOf(a)] = 1; });
      if (Object.keys(uniq).length < 4) return;
      all.forEach(function (a) { if (pathOf(a) === here) { a.classList.add("ax-legacy-nav-hidden"); a.setAttribute("aria-hidden", "true"); a.tabIndex = -1; } });
      b.__axFolded = true;
      // keep anything in the bar that is not a link to another page (status chips, buttons, the page title)
      var keep = Array.prototype.filter.call(b.querySelectorAll("button, select, input, .pill, .chip, [role=status], h1"), function (x) { return !x.closest('a[href^="/"]'); });
      as.forEach(function (a) { var p = pathOf(a); if (!seen[p]) { seen[p] = 1; links.push({ href: a.getAttribute("href"), text: (a.textContent || "").replace(/^[←→\s]+|[←→\s]+$/g, "").trim() }); } });
      if (keep.length || b.matches("header, .top, .topbar")) {
        as.forEach(function (a) { a.classList.add("ax-legacy-nav-hidden"); a.setAttribute("aria-hidden", "true"); a.tabIndex = -1; });
        // a bar left with only separators / an empty wrapper collapses too
        Array.prototype.forEach.call(b.querySelectorAll("div, span"), function (w) { if (!w.closest(SHELL_SKIP) && w.querySelector("a.ax-legacy-nav-hidden") && !(w.textContent || "").replace(/[·|•\s]/g, "").replace(new RegExp(Array.prototype.map.call(w.querySelectorAll("a.ax-legacy-nav-hidden"), function (a) { return (a.textContent || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") || "$^", "g"), "").trim()) w.classList.add("ax-legacy-nav-hidden"); });
      } else {
        b.classList.add("ax-legacy-nav-hidden"); b.setAttribute("aria-hidden", "true");
      }
      b.setAttribute("data-ax-folded", "1");
    });
    if (links.length) {
      var row = document.createElement("div"); row.className = "ax-seealso"; row.setAttribute("role", "navigation"); row.setAttribute("aria-label", "See also");
      row.innerHTML = '<span>See also</span>' + links.filter(function (l) { return l.text; }).map(function (l) { return '<a href="' + AX.esc(l.href) + '">' + AX.esc(l.text) + "</a>"; }).join("");
      main.appendChild(row);
    }
  };
  // G3: file names, CLI commands, API paths and config keys are not user text. In an older page (and in API messages
  // painted into it), each such fragment folds into a closed "Technical details" disclosure next to its sentence.
  var DEV = /(python -m [\w.]+(?: [\w.\-]+)*|\b(?:[\w-]+\/)+[\w-]+\.py\b|\b[\w-]+\.py\b|(?:atip_data\/)?config\.json(?: [\w.]+(?: = \w+)?)?|\b(?:GET|POST|PUT|DELETE) \/api\/[\w\/{}.\-]+(?: \{[^}]*\})?|\b[a-z]+(?:\.[a-z0-9_]+)+\(\)|\b[a-z]+\.[a-z_]+\.[a-z_]+\b)/;
  function techFold(node) {
    var t = node.nodeValue; if (!t || !DEV.test(t)) return;
    var el = node.parentElement; if (!el || el.closest("code,pre,script,style,textarea,details,.ax-tech,input,select,option," + SHELL_SKIP)) return;
    var bits = [], plain = t;
    // 1. a parenthetical that holds a technical fragment goes whole: "End-of-day scans (research/technicals.py): ..."
    plain = plain.replace(/\s*\(([^()]*)\)/g, function (m, inner) { if (DEV.test(inner)) { bits.push(inner.trim()); return ""; } return m; });
    // 2. a clause after ";" or " — " or ":" that is technical goes too
    plain = plain.replace(/([;:—]|\s-\s)\s*([^;:—]*)/g, function (m, sep, clause) { if (DEV.test(clause) && clause.length < 220) { bits.push(clause.trim()); return ""; } return m; });
    // 3. what is left still technical: the fragment itself
    var guard = 0;
    while (DEV.test(plain) && guard++ < 6) { var mm = plain.match(DEV); bits.push(mm[0]); plain = plain.replace(mm[0], "").replace(/\s{2,}/g, " "); }
    plain = plain.replace(/\s+([,.;:])/g, "$1").replace(/\(\s*\)/g, "").replace(/(with|or|and|run|set|via|use|see|in|from|by|:)\s*([.,;]|$)/i, "$2");
    if (!bits.length) return;
    node.nodeValue = plain.trim() ? plain.replace(/\s+$/, " ") : "";
    // one disclosure per paragraph / cell, at its end, so a sentence is never split by it
    var block = el.closest("p,li,td,dd,th,section,article,div,label") || el;
    if (block.closest(SHELL_SKIP)) block = el;
    var d = block.querySelector(":scope > details.ax-tech[data-ax-auto]");
    if (!d) {
      d = document.createElement("details"); d.className = "ax-tech"; d.setAttribute("data-ax-auto", "");
      d.innerHTML = '<summary title="Commands and settings for the operator">ⓘ Technical details</summary><code></code>';
      block.appendChild(document.createTextNode(" ")); block.appendChild(d);
    }
    var c = d.querySelector("code"); c.textContent = (c.textContent ? c.textContent + "\n" : "") + bits.join("\n");
  }
  AX.foldTech = function (rootEl) {
    var r = rootEl || document.getElementById("ax-main"); if (!r) return;
    var w = document.createTreeWalker(r, NodeFilter.SHOW_TEXT), n, todo = [];
    while ((n = w.nextNode())) if (n.nodeValue && n.nodeValue.length > 3 && DEV.test(n.nodeValue)) todo.push(n);
    todo.forEach(techFold);
    // a <code> block in user text (outside a technical disclosure) moves into one as well
    Array.prototype.forEach.call(r.querySelectorAll("code"), function (c) {
      if (c.closest("details,pre,.ax-tech,textarea," + SHELL_SKIP) || !DEV.test(c.textContent || "")) return;
      var d = document.createElement("details"); d.className = "ax-tech"; d.innerHTML = '<summary>ⓘ Technical details</summary>';
      c.parentNode.insertBefore(d, c); d.appendChild(c);
    });
  };
  // G5: an older page's clickable <div>/<span>/<tr> becomes keyboard reachable: role, tab stop, Enter / Space
  AX.keyboardClickables = function (rootEl) {
    var r = rootEl || document.getElementById("ax-main") || document.body;
    Array.prototype.forEach.call(r.querySelectorAll("[onclick]"), function (el) {
      if (el.__axKey || /^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY|LABEL|OPTION)$/.test(el.tagName) || el.closest(SHELL_SKIP)) return;
      el.__axKey = true;
      // a clickable container whose own buttons / links do the work (a modal's backdrop, a row with actions) needs no role
      if (el.querySelector("a,button,input,select,textarea,[tabindex],[role=dialog]") || /-back$|backdrop|overlay/i.test(el.id + " " + el.className)) { el.setAttribute("data-ax-container", ""); return; }
      el.setAttribute("data-ax-clickable", "");
      if (!el.hasAttribute("role")) {
        var tab = el.classList.contains("tab") || !!el.closest(".tabs,[role=tablist]");
        el.setAttribute("role", tab ? "tab" : el.tagName === "TR" ? "link" : "button");
        if (tab && el.parentElement && !el.parentElement.hasAttribute("role")) el.parentElement.setAttribute("role", "tablist");
        if (tab) el.setAttribute("aria-selected", el.classList.contains("active") || el.classList.contains("on") ? "true" : "false");
      }
      if (!el.hasAttribute("tabindex")) el.tabIndex = 0;
      el.addEventListener("keydown", function (e) { if ((e.key === "Enter" || e.key === " ") && e.target === el) { e.preventDefault(); el.click(); } });
    });
  };
  // accessibility of older pages: a field with no label gets one (its title, placeholder, the text before it or its
  // first option); a box that scrolls gets a tab stop so the keyboard can scroll it
  AX.a11yFix = function (rootEl) {
    var r = rootEl || document.getElementById("ax-main"); if (!r) return;
    Array.prototype.forEach.call(r.querySelectorAll("input:not([type=hidden]),select,textarea"), function (f) {
      if (f.closest(SHELL_SKIP) || f.getAttribute("aria-label") || f.getAttribute("aria-labelledby") || f.closest("label") ||
          (f.id && document.querySelector('label[for="' + f.id + '"]'))) return;
      var prev = f.previousSibling, txt = "";
      while (prev && !txt) { txt = (prev.textContent || "").trim(); prev = prev.previousSibling; }
      var name = f.getAttribute("title") || f.getAttribute("placeholder") || txt ||
        (f.tagName === "SELECT" && f.options && f.options[0] ? f.options[0].text : "") || f.name || f.id;
      if (name) f.setAttribute("aria-label", String(name).slice(0, 80));
    });
    Array.prototype.forEach.call(r.querySelectorAll(".ax-hscroll,.scroll,.sx,[style*='overflow']"), function (b) {
      if (b.hasAttribute("tabindex") || b.closest(SHELL_SKIP)) return;
      if (b.scrollWidth > b.clientWidth + 1 || b.scrollHeight > b.clientHeight + 1) {
        if (!b.querySelector("a,button,input,select,textarea,[tabindex]")) { b.tabIndex = 0; if (!b.getAttribute("role")) { b.setAttribute("role", "region"); b.setAttribute("aria-label", "Scrollable table"); } }
      }
    });
    var sv = document.getElementById("sv"); if (sv && !sv.getAttribute("aria-label")) sv.setAttribute("aria-label", "Stock details");
  };
  // G4: ATIP's own terms explain themselves on hover / focus
  AX.terms = function (rootEl) {
    var g = AX.data("ax-glossary", null); if (!g) return;
    var r = rootEl || document.getElementById("ax-main"); if (!r) return;
    Array.prototype.forEach.call(r.querySelectorAll("th, b, .ax-lbl, label"), function (el) {
      if (el.__axTerm || el.closest(SHELL_SKIP)) return;
      var k = (el.textContent || "").trim().toLowerCase(); if (!g[k] || el.querySelector("*")) return;
      el.__axTerm = true; el.classList.add("ax-term"); el.title = g[k]; if (!el.hasAttribute("tabindex") && el.tagName !== "TH") el.tabIndex = 0;
    });
  };
  // H4: a clamped text gets "Show more" only when it really overflows its two lines
  AX.clamps = function () {
    document.querySelectorAll("[data-ax-clamp]").forEach(function (c) {
      if (c.__ax || c.closest("details:not([open])")) return; c.__ax = true;
      if (c.scrollHeight <= c.clientHeight + 2) return;
      var b = document.createElement("button"); b.type = "button"; b.className = "ax-linkbtn"; b.textContent = "Show more"; b.setAttribute("aria-expanded", "false");
      b.addEventListener("click", function () { var o = c.classList.toggle("open"); b.textContent = o ? "Show less" : "Show more"; b.setAttribute("aria-expanded", o ? "true" : "false"); });
      c.parentNode.insertBefore(b, c.nextSibling);
    });
  };
  function overflowMenu() {
    var b = document.querySelector("[data-ax-overflow]"), m = document.getElementById("ax-ovmenu"); if (!b || !m) return;
    var set = function (o) { m.hidden = !o; b.setAttribute("aria-expanded", o ? "true" : "false"); if (o) { var f = m.querySelector("a,button"); if (f) f.focus(); } };
    b.addEventListener("click", function (e) { e.stopPropagation(); set(m.hidden); });
    document.addEventListener("click", function (e) { if (!m.hidden && !m.contains(e.target)) set(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !m.hidden) { set(false); b.focus(); } });
  }
  // §3: no horizontal page scroll at 360 px -- an older page's wide table scrolls inside its own box
  AX.wrapWide = function (rootEl) {
    var r = rootEl || document.getElementById("ax-main"); if (!r) return;
    Array.prototype.forEach.call(r.querySelectorAll("table"), function (t) {
      if (t.__axWrap || t.closest(SHELL_SKIP + ",.ax-tablewrap,.ax-hscroll")) return;
      t.__axWrap = true;
      var p = t.parentElement, cs = p ? getComputedStyle(p) : null;
      if (cs && /(auto|scroll)/.test(cs.overflowX)) return;
      var w = document.createElement("div"); w.className = "ax-hscroll"; p.insertBefore(w, t); w.appendChild(t);
    });
  };
  function legacy() {
    if (document.body.classList.contains("ax-native")) { AX.terms(); return; }
    try { AX.wrapWide(); } catch (e) {}
    try { AX.foldLegacyNav(); } catch (e) {}
    try { AX.foldTech(); } catch (e) {}
    try { AX.keyboardClickables(); } catch (e) {}
    try { AX.terms(); } catch (e) {}
    try { AX.a11yFix(); } catch (e) {}
    root.classList.add("ax-ready");                          // the page's content may show now (see atip.css)
    // older pages paint from their APIs after load: fold what they add, at most once per frame
    var main = document.getElementById("ax-main"); if (!main || !window.MutationObserver) return;
    var queued = false;
    new MutationObserver(function () {
      if (queued) return; queued = true;
      (window.requestAnimationFrame || setTimeout)(function () { queued = false; try { AX.foldLegacyNav(); AX.foldTech(); AX.keyboardClickables(); AX.terms(); AX.wrapWide(); AX.a11yFix(); } catch (e) {} });
    }).observe(main, { childList: true, subtree: true });
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
    toolbar(); AX.charts(); overflowMenu(); AX.clamps(); legacy();
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
