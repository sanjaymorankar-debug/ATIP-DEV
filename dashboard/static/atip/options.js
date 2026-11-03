/* ATIP option chain (W6, 3 Nov 2026; benchmark Sensibull / Kite): the Option chain card on /options-builder.
   Reads GET /api/v2/options/chain/{symbol} (dashboard/options_v2.py) and shows it ATM-centred -- calls | strike | puts:
   OI with a bar scaled to the largest OI on its side (call bars grow toward the strike), OI change, IV, LTP, and on
   demand Δ Γ Θ ν -- with ITM shading, the ATM row, a spot line, PCR and max pain, an "as of" chip and a "stale" chip.
   Clicking an LTP adds that option as a leg (BUY; shift-click, or "Click sells": SELL). When the page's strategy
   builder exposes its legs (LEGS / drawLegs, dashboard/w39_page.py) the leg joins them; otherwise the card keeps its
   own list. Either way the card prices the legs with POST /api/options/analyse through AX.api (the page token) and
   draws the payoff: the expiry line, today's line dashed, zero, spot and breakevens, with max profit / loss,
   breakevens, net premium and margin (when the analyser gives one). Every number goes through AX.fmt.
   Analysis only: nothing here places an order. */
(function () {
  "use strict";
  var booted = false;

  function boot() {
    if (booted) return;
    booted = true;
    var AX = (window.AX && window.AX.fmt && window.AX.api) ? window.AX : shim();
    var F = AX.fmt;
    var esc = AX.esc || shim().esc;
    var DASH = '<span class="ax-oc-na" title="Not available">—</span>';
    var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var GREEKS = [["delta", "Δ", "Delta: the option's price change per ₹1 move in the underlying", 2],
                  ["gamma", "Γ", "Gamma: delta's change per ₹1 move in the underlying", 4],
                  ["theta", "Θ", "Theta: ₹ per unit the option loses per calendar day", 2],
                  ["vega", "ν", "Vega: ₹ per unit per 1 point of implied volatility", 2]];
    var SYM_RE = /^[A-Z0-9&_-]{1,32}$/;
    var S = { sym: "", n: 10, greeks: pref("ax-oc-greeks") === "1", sell: false, d: null, own: [], res: null,
              sig: null, seq: 0, aseq: 0, t: 0, poll: 0, shift: false, hover: -1, cid: 0 };

    // ── small helpers ──────────────────────────────────────────────────────────────────────
    function $(id) { return document.getElementById(id); }
    function has(v) { return v !== null && v !== undefined && v !== "" && typeof v !== "boolean" && isFinite(Number(v)); }
    function pref(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; }
    function clean(s) { s = String(s || "").trim().toUpperCase(); return SYM_RE.test(s) ? s : ""; }
    function r1(v) { return Math.round(v * 10) / 10; }
    function dmy(s, withYear) {
      var m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?/.exec(String(s || ""));
      if (!m) return esc(s || "");
      return (+m[3]) + " " + MON[+m[2] - 1] + (withYear === false ? "" : " " + m[1]) + (m[4] ? ", " + m[4] + " IST" : "");
    }
    function num(v, dp) { return has(v) ? esc(F.number(v, dp)) : DASH; }
    function qtyText(v) {                       // OI / volume: Indian compact (K, L, Cr)
      var a = Math.abs(v);
      if (a >= 1e7) return F.number(v / 1e7, 2) + " Cr";
      if (a >= 1e5) return F.number(v / 1e5, 2) + " L";
      if (a >= 1e3) return F.number(v / 1e3, 1) + "K";
      return F.number(v, 0);
    }
    function qty(v) { return has(v) ? esc(qtyText(Number(v))) : DASH; }
    function pctMag(a) { return F.pct(a, false); }
    function numMag(a) { return F.number(a, 2); }
    // a change: colour + ▲ / ▼ + sign, always together; `mag` formats its size
    function move(v, mag) {
      if (!has(v)) return DASH;
      v = Number(v);
      var t = Math.abs(v) < 1e-9 ? "flat" : v > 0 ? "up" : "down";
      return '<span class="ax-oc-' + t + '">' + (t === "up" ? "▲ +" : t === "down" ? "▼ −" : "") + esc(mag(Math.abs(v))) + "</span>";
    }
    function money(v, cls) {                    // a P&L level: colour + sign
      if (!has(v)) return DASH;
      v = Number(v);
      return '<span class="' + (cls || (v > 0.004 ? "ax-oc-pos" : v < -0.004 ? "ax-oc-neg" : "ax-oc-flat")) + '">' + esc(F.inr(v, true)) + "</span>";
    }
    function strikeDp() { return S.d && S.d.rows.some(function (r) { return r.strike % 1; }) ? 2 : 0; }
    function say(msg) { var l = $("ax-oc-live"); if (l) { l.textContent = ""; setTimeout(function () { l.textContent = msg; }, 30); } }

    // ── the card ───────────────────────────────────────────────────────────────────────────
    var host = $("ax-oc");
    if (!host) {
      host = document.createElement("section");
      host.id = "ax-oc";
      var wrap = document.querySelector("#ax-main .wrap") || document.querySelector(".wrap") || $("ax-main") || document.body;
      wrap.insertBefore(host, wrap.firstChild);
    }
    host.className = "ax-oc";
    host.setAttribute("aria-labelledby", "ax-oc-h");
    host.innerHTML =
      '<div class="ax-oc-head"><h2 id="ax-oc-h">Option chain</h2>' +
      '<form class="ax-oc-ctrls" id="ax-oc-form" autocomplete="off" novalidate>' +
      '<label class="ax-oc-f">Symbol<input id="ax-oc-sym" class="ax-oc-in" maxlength="32" spellcheck="false" autocapitalize="characters" aria-describedby="ax-oc-meta"></label>' +
      '<label class="ax-oc-f">Expiry<select id="ax-oc-exp" class="ax-oc-in"></select></label>' +
      '<label class="ax-oc-f">Strikes each side<select id="ax-oc-n" class="ax-oc-in"><option>5</option><option selected>10</option><option>15</option><option>20</option></select></label>' +
      '<button type="submit" class="ax-oc-btn ax-oc-primary">Load</button>' +
      '<button type="button" class="ax-oc-btn" id="ax-oc-greeks" aria-pressed="false">Greeks</button></form></div>' +
      '<div class="ax-oc-meta" id="ax-oc-meta"></div>' +
      '<div class="ax-oc-body"><div class="ax-oc-chain">' +
      '<div class="ax-oc-tools"><span class="ax-oc-seg" role="group" aria-label="Clicking an LTP">' +
      '<button type="button" class="ax-oc-btn" id="ax-oc-buy" aria-pressed="true">Click buys</button>' +
      '<button type="button" class="ax-oc-btn" id="ax-oc-sell" aria-pressed="false">Click sells</button></span>' +
      '<span>Shift-click an LTP for the other side. <span class="ax-oc-key" aria-hidden="true"></span>In the money</span></div>' +
      '<div class="ax-oc-scroll ax-tablewrap" id="ax-oc-scroll" style="overflow:auto" tabindex="0" role="region" aria-label="Option chain table"></div>' +
      '<p class="ax-oc-note">OI bars scale to the largest open interest on each side. An IV in italics is implied from the price (none stored). Greeks: Black-Scholes at that IV.</p></div>' +
      '<div class="ax-oc-strat" role="region" aria-labelledby="ax-oc-sh"><h3 id="ax-oc-sh">Strategy from the chain</h3>' +
      '<div id="ax-oc-legs"></div><div id="ax-oc-res" aria-live="polite"></div></div></div>' +
      '<p class="ax-oc-foot">Analysis only: nothing here places an order.</p><div class="ax-oc-sr" id="ax-oc-live" aria-live="polite"></div>';

    var form = $("ax-oc-form"), inp = $("ax-oc-sym"), expSel = $("ax-oc-exp"), nSel = $("ax-oc-n"), gBtn = $("ax-oc-greeks");
    var meta = $("ax-oc-meta"), scroll = $("ax-oc-scroll"), legsBox = $("ax-oc-legs"), res = $("ax-oc-res");
    var bsym = $("sym");                           // the strategy builder's symbol box, when the page has the builder

    // ── the builder's legs (dashboard/w39_page.py: `let LEGS`, `function drawLegs`), else the card's own ─────────
    function builder() {
      try { return typeof LEGS !== "undefined" && Array.isArray(LEGS) && typeof drawLegs === "function" && !!$("legs"); } catch (e) { return false; }
    }
    function legs() { return builder() ? LEGS : S.own; }    // eslint-disable-line no-undef
    function syncBuilder() {
      var d = S.d, set = function (id, v) { var el = $(id); if (el && v !== null && v !== undefined && v !== "") el.value = v; };
      if (!d) return;
      set("sym", d.symbol); set("spot", d.spot); set("lot", d.lot_size); set("exp", d.expiry);
    }
    function changed() {
      if (builder()) { syncBuilder(); try { drawLegs(); } catch (e) {} }      // eslint-disable-line no-undef
      renderLegs();
      schedule(0);
    }

    // ── loading the chain ──────────────────────────────────────────────────────────────────
    function busy(on) { host.classList.toggle("ax-oc-loading", on); host.setAttribute("aria-busy", on ? "true" : "false"); }
    function load(sym, exp, keepScroll) {
      var my = ++S.seq;
      clearTimeout(S.poll);
      S.sym = sym;
      inp.value = sym;
      inp.removeAttribute("aria-invalid");
      busy(true);
      if (!S.d) scroll.innerHTML = '<p class="ax-oc-empty">Loading the ' + esc(sym) + " option chain…</p>";
      var url = "/api/v2/options/chain/" + encodeURIComponent(sym) + "?strikes=" + S.n + (exp ? "&expiry=" + encodeURIComponent(exp) : "");
      return AX.api(url).then(function (d) {
        if (my !== S.seq) return;
        busy(false);
        if (!d || d.status !== "OK") { S.d = null; empty(d && d.message ? d.message : "No option chain stored for " + sym + ".", false); return; }
        var top = scroll.scrollTop, left = scroll.scrollLeft;
        S.d = d;
        renderMeta();
        expSel.innerHTML = d.expiries.map(function (e) {
          return '<option value="' + esc(e) + '"' + (e === d.expiry ? " selected" : "") + ">" + dmy(e) + "</option>";
        }).join("");
        renderTable();
        if (keepScroll) { scroll.scrollTop = top; scroll.scrollLeft = left; } else centre();
        renderLegs();
        schedule(0);
        if (d.source === "live" && !d.stale) {      // the intraday feed: refresh every minute while the page is in view
          S.poll = setTimeout(function tick() {
            if (document.visibilityState === "hidden") { S.poll = setTimeout(tick, 60000); return; }
            load(S.sym, S.d && S.d.expiry, true);
          }, 60000);
        }
      }).catch(function (e) {
        if (my !== S.seq) return;
        busy(false);
        S.d = null;
        empty(e.message, true);
      });
    }
    function empty(msg, bad) {
      meta.innerHTML = "";
      expSel.innerHTML = "";
      scroll.innerHTML = '<p class="' + (bad ? "ax-oc-msg" : "ax-oc-empty") + '" role="' + (bad ? "alert" : "status") + '">' + esc(msg) + "</p>";
      renderLegs();
    }

    // ── header numbers and chips ───────────────────────────────────────────────────────────
    function stat(label, value, title) {
      return '<span class="ax-oc-stat"' + (title ? ' title="' + esc(title) + '"' : "") + '><span class="ax-oc-lbl">' + label +
        '</span> <b class="ax-oc-num">' + value + "</b></span>";
    }
    function renderMeta() {
      var d = S.d, dp = strikeDp(), t = d.totals || {};
      var pcrWhy = "Put-call ratio: total put OI ÷ total call OI over every strike of this expiry" +
        (has(t.pe_oi) && has(t.ce_oi) ? " (" + qtyText(t.pe_oi) + " ÷ " + qtyText(t.ce_oi) + ")" : "");
      var dte = has(d.days_to_expiry) ? esc(F.number(d.days_to_expiry, 0)) + (d.days_to_expiry === 1 ? " day" : " days") : DASH;
      var html = '<div class="ax-oc-stats">' +
        stat("Spot", num(d.spot, 2), d.spot_source ? "From the " + d.spot_source : "") +
        stat("ATM", num(d.atm_strike, dp)) +
        stat("PCR", num(d.pcr, 2), pcrWhy) +
        stat("Max pain", num(d.max_pain, dp), "The strike at which option buyers' holdings of this expiry would be worth least") +
        stat("Lot", num(d.lot_size, 0)) +
        stat("To expiry", dte) + "</div>";
      html += '<div class="ax-oc-chips"><span class="ax-oc-chip" title="' + esc(d.source_label || "") + '">As of ' + dmy(d.as_of) +
        " · " + (d.source === "live" ? "live feed" : "bhavcopy, end of day") + "</span>";
      if (d.stale) html += '<span class="ax-oc-chip ax-oc-warn" title="' + esc(d.stale_reason || "") + '">⚠ Stale · last session ' + dmy(d.last_trading_day) + "</span>";
      meta.innerHTML = html + "</div>";
    }

    // ── the table ──────────────────────────────────────────────────────────────────────────
    function cols() {
      var c = [["oi", "OI", "Open interest (contracts)"], ["oi_change", "OI chg", "Change in open interest since the previous session"]];
      if (S.greeks) GREEKS.slice().reverse().forEach(function (g) { c.push([g[0], g[1], g[2], g[3]]); });
      return c.concat([["iv", "IV", "Implied volatility, % a year"], ["ltp", "LTP", "Last traded price; click to add the leg"]]);
    }
    function th(c) { return '<th scope="col"><abbr title="' + esc(c[2]) + '">' + esc(c[1]) + "</abbr></th>"; }
    function cell(side, c, q, itm, strike, maxOi) {
      var cls = "ax-oc-" + side + (itm ? " ax-oc-itm" : ""), k = c[0];
      if (!q) return '<td class="' + cls + '">' + (k === "ltp" ? '<span class="ax-oc-na" title="No contract stored at this strike">—</span>' : DASH) + "</td>";
      if (k === "oi") {
        var bar = has(q.oi) && maxOi > 0 ? '<span class="ax-oc-bar" aria-hidden="true" style="width:' + r1(Math.max(0, q.oi) / maxOi * 90) + '%"></span>' : "";
        return '<td class="' + cls + ' ax-oc-oi"' + (has(q.oi) ? ' title="OI ' + esc(F.number(q.oi, 0)) + '"' : "") + ">" + bar + '<span class="ax-oc-v">' + qty(q.oi) + "</span></td>";
      }
      if (k === "oi_change") return '<td class="' + cls + '"' + (has(q.oi_change) ? ' title="OI change ' + esc(F.number(q.oi_change, 0)) + '"' : "") + ">" + move(q.oi_change, qtyText) + "</td>";
      if (k === "iv") {
        if (!has(q.iv)) return '<td class="' + cls + '">' + DASH + "</td>";
        var imp = q.iv_source === "implied";
        return '<td class="' + cls + '"><span' + (imp ? ' class="ax-oc-implied" title="Implied from the price: no IV stored"' : "") + ">" + esc(F.pct(q.iv, false)) + "</span></td>";
      }
      if (k === "ltp") {
        if (!has(q.ltp)) return '<td class="' + cls + ' ax-oc-ltpc">' + DASH + "</td>";
        var kind = side === "ce" ? "CE" : "PE", what = S.d.symbol + " " + F.number(strike, strikeDp()) + " " + kind;
        var chg = has(q.change_pct) ? move(q.change_pct, pctMag) : has(q.change) ? move(q.change, numMag) : "";
        var tip = has(q.change) ? "Change " + (q.change > 0 ? "+" : "") + F.number(q.change, 2) + " vs the previous close" : "";
        return '<td class="' + cls + ' ax-oc-ltpc"><button type="button" class="ax-oc-ltp" data-k="' + kind + '" data-s="' + esc(String(strike)) +
          '" aria-label="' + esc((S.sell ? "Sell " : "Buy ") + what + " at " + F.number(q.ltp, 2) + "; shift for " + (S.sell ? "buy" : "sell")) +
          '"' + (tip ? ' title="' + esc(tip) + '"' : "") + "><span>" + esc(F.number(q.ltp, 2)) + '</span><span class="ax-oc-sub">' + chg + "</span></button></td>";
      }
      return '<td class="' + cls + '">' + num(q[k], c[3]) + "</td>";
    }
    function renderTable() {
      var d = S.d;
      if (!d) return;
      var C = cols(), P = C.slice().reverse(), dp = strikeDp(), spot = has(d.spot) ? Number(d.spot) : null;
      var mx = function (s) { var m = 0; d.rows.forEach(function (r) { if (r[s] && has(r[s].oi)) m = Math.max(m, Number(r[s].oi)); }); return m; };
      var mce = mx("ce"), mpe = mx("pe"), width = C.length * 2 + 1;
      var h = '<table class="ax-oc-table"><caption class="ax-oc-sr">' + esc(d.symbol) + " option chain, expiry " + dmy(d.expiry) +
        ", as of " + dmy(d.as_of) + "</caption><thead><tr>" +
        '<th scope="colgroup" colspan="' + C.length + '" class="ax-oc-sideh">Calls (CE)</th><th scope="col" rowspan="2" class="ax-oc-k">Strike</th>' +
        '<th scope="colgroup" colspan="' + C.length + '" class="ax-oc-sideh">Puts (PE)</th></tr><tr>' +
        C.map(th).join("") + P.map(th).join("") + "</tr></thead><tbody>";
      var spotDone = spot === null;
      var spotRow = function () {
        return '<tr class="ax-oc-spot"><td colspan="' + C.length + '"></td><th scope="row" class="ax-oc-k"><span class="ax-oc-spotlbl">Spot ' +
          esc(F.number(spot, 2)) + '</span></th><td colspan="' + C.length + '"></td></tr>';
      };
      d.rows.forEach(function (r) {
        if (!spotDone && r.strike > spot) { h += spotRow(); spotDone = true; }
        var ceItm = spot !== null && r.strike < spot, peItm = spot !== null && r.strike > spot;
        h += "<tr" + (r.atm ? ' class="ax-oc-atm"' : "") + ">" +
          C.map(function (c) { return cell("ce", c, r.ce, ceItm, r.strike, mce); }).join("") +
          '<th scope="row" class="ax-oc-k">' + num(r.strike, dp) + (r.atm ? '<span class="ax-oc-tag">ATM</span>' : "") + "</th>" +
          P.map(function (c) { return cell("pe", c, r.pe, peItm, r.strike, mpe); }).join("") + "</tr>";
      });
      if (!spotDone) h += spotRow();
      scroll.innerHTML = h + "</tbody></table>";
      scroll.setAttribute("data-cols", String(width));
    }
    function centre() {                              // the ATM row in the middle, the strike column in view
      var row = scroll.querySelector("tr.ax-oc-atm"), k = row && row.querySelector(".ax-oc-k");
      if (!row || !k) return;
      var sr = scroll.getBoundingClientRect(), rr = row.getBoundingClientRect(), prev = k.previousElementSibling;
      scroll.scrollTop += rr.top - sr.top - (scroll.clientHeight - rr.height) / 2;
      if (prev && scroll.scrollWidth > scroll.clientWidth) {
        scroll.scrollLeft += prev.getBoundingClientRect().right - sr.left - (scroll.clientWidth - k.offsetWidth) / 2;
      }
    }

    // ── legs ───────────────────────────────────────────────────────────────────────────────
    function addLeg(kind, strike, sell) {
      var d = S.d;
      if (!d) return;
      var row = d.rows.filter(function (r) { return r.strike === strike; })[0], q = row && row[kind === "CE" ? "ce" : "pe"];
      if (!q || !has(q.ltp)) return;
      var side = sell ? "SELL" : "BUY", L = legs();
      var same = L.filter(function (l) {
        return String(l.kind).toUpperCase() === kind && Number(l.strike) === strike && String(l.expiry).slice(0, 10) === d.expiry &&
          String(l.side).toUpperCase() === side;
      })[0];
      if (same) same.lots = (Number(same.lots) || 1) + 1;
      else {
        if (L.length >= 12) { say("A strategy can have at most 12 legs."); renderLegs("A strategy can have at most 12 legs."); return; }
        L.push({ kind: kind, side: side, strike: strike, expiry: d.expiry, lots: 1, premium: q.ltp,
                 iv: has(q.iv) ? Math.round(q.iv * 100) / 10000 : null, price_source: "option chain LTP, " + dmy(d.as_of) });
      }
      changed();
      say("Added to the strategy: " + side + " " + d.symbol + " " + F.number(strike, strikeDp()) + " " + kind + " at " + F.number(q.ltp, 2) + ". Nothing is ordered.");
    }
    function renderLegs(note) {
      var L = legs(), dp = strikeDp();
      if (!L.length) {
        legsBox.innerHTML = '<p class="ax-oc-note">' + esc(note || (builder() ?
          "Click an LTP in the chain: the leg joins the strategy builder's legs on this page, and its payoff draws here." :
          "Click an LTP in the chain to add a leg; its payoff draws here.")) + "</p>";
        S.res = null; S.sig = null; res.innerHTML = "";
        return;
      }
      legsBox.innerHTML = '<ul class="ax-oc-legs">' + L.map(function (l, i) {
        var buy = String(l.side).toUpperCase() !== "SELL", kind = String(l.kind || "").toUpperCase(), lots = Number(l.lots) || 1;
        var what = kind === "FUT" ? "Future" : num(l.strike, dp) + " " + esc(kind);
        return '<li class="ax-oc-leg"><button type="button" class="ax-oc-btn ' + (buy ? "ax-oc-buy" : "ax-oc-sell") + '" data-act="side" data-i="' + i +
          '" aria-label="' + (buy ? "Buy" : "Sell") + ". Switch to " + (buy ? "sell" : "buy") + '">' + (buy ? "B" : "S") + "</button>" +
          '<span class="ax-oc-legname"><b>' + what + "</b>" + '<span class="ax-oc-sub">' + dmy(String(l.expiry || "").slice(0, 10)) + " · " +
          (has(l.premium) ? "@ " + esc(F.number(l.premium, 2)) : "priced from the stored chain") + "</span></span>" +
          '<span class="ax-oc-lots"><button type="button" class="ax-oc-btn" data-act="minus" data-i="' + i + '" aria-label="One lot fewer"' + (lots <= 1 ? " disabled" : "") + ">−</button>" +
          '<span class="ax-oc-num">' + esc(F.number(lots, 0)) + (lots === 1 ? " lot" : " lots") + "</span>" +
          '<button type="button" class="ax-oc-btn" data-act="plus" data-i="' + i + '" aria-label="One lot more">+</button></span>' +
          '<button type="button" class="ax-oc-btn" data-act="rm" data-i="' + i + '" aria-label="Remove this leg">✕</button></li>';
      }).join("") + '</ul><button type="button" class="ax-oc-btn ax-oc-ghost" data-act="clear">Clear all legs</button>' +
        (note ? '<p class="ax-oc-note" role="status">' + esc(note) + "</p>" : "");
    }

    // ── pricing: POST /api/options/analyse (the token rides in AX.api) ─────────────────────
    function schedule(ms) { clearTimeout(S.t); S.t = setTimeout(analyse, ms === undefined ? 300 : ms); }
    function body() {
      var d = S.d, spot = d && d.spot, lot = d && d.lot_size;
      if (builder()) {
        spot = spot || Number(($("spot") || {}).value) || null;
        lot = lot || Number(($("lot") || {}).value) || null;
      }
      return { symbol: S.sym || null, spot: spot || null, lot_size: lot || 1,
               legs: legs().map(function (l) {
                 return { kind: l.kind, side: l.side, strike: l.strike, expiry: l.expiry, lots: l.lots, premium: l.premium, iv: l.iv };
               }) };
    }
    function analyse() {
      var b = body(), sig = JSON.stringify(b);
      if (!b.legs.length) { S.res = null; S.sig = null; res.innerHTML = ""; return; }
      if (sig === S.sig) return;
      S.sig = sig;
      var my = ++S.aseq;
      res.setAttribute("aria-busy", "true");
      AX.api("/api/options/analyse", { body: b }).then(function (r) {
        if (my !== S.aseq) return;
        res.removeAttribute("aria-busy");
        S.res = r;
        renderResult();
      }).catch(function (e) {
        if (my !== S.aseq) return;
        res.removeAttribute("aria-busy");
        S.res = null;
        S.sig = null;
        res.innerHTML = '<p class="ax-oc-msg" role="alert">' + esc(e.message) + "</p>";
      });
    }
    function tile(label, val, sub) {
      return '<div class="ax-oc-tile"><div class="ax-oc-lbl">' + label + '</div><div class="ax-oc-val">' + val + "</div>" +
        (sub ? '<div class="ax-oc-sub">' + sub + "</div>" : "") + "</div>";
    }
    function renderResult() {
      var r = S.res;
      if (!r) { res.innerHTML = ""; return; }
      var g = r.greeks || {}, np = Number(r.net_premium) || 0, spot = Number(r.spot);
      var mp = r.unlimited_profit ? '<span class="ax-oc-pos">Unlimited</span>' : money(r.max_profit);
      var ml = r.unlimited_loss ? '<span class="ax-oc-neg">Unlimited</span>' : money(r.max_loss);
      var bes = (r.breakevens || []).length ? r.breakevens.map(function (b) {
        return '<span class="ax-oc-be">' + esc(F.number(b, 2)) + ' <span class="ax-oc-sub">' + move((b / spot - 1) * 100, pctMag) + "</span></span>";
      }).join("") : '<span class="ax-oc-muted">None</span>';
      var margin = has(r.margin) ? esc(F.inr(r.margin)) : '<span class="ax-oc-muted">Not computed</span>';
      var html = '<div class="ax-oc-tiles">' +
        tile("Max profit", mp) + tile("Max loss", ml) + tile("Breakevens", bes) +
        tile("Net premium", esc(F.inr(Math.abs(np))), np > 0 ? "credit received" : np < 0 ? "debit paid" : "") +
        tile("Margin", margin, has(r.margin) ? "estimate" : "check your broker's calculator") +
        tile("Chance of profit", has(r.probability_of_profit_pct) ? esc(F.pct(r.probability_of_profit_pct, false)) : DASH,
             has(r.reward_to_risk) ? "reward : risk " + esc(F.number(r.reward_to_risk, 2)) + " : 1" : "") + "</div>";
      html += '<div class="ax-oc-greekline"><span><span class="ax-oc-lbl">Net Δ</span> <b class="ax-oc-num">' + num(g.delta, 2) + "</b></span>" +
        '<span><span class="ax-oc-lbl">Θ per day</span> <b class="ax-oc-num">' + money(g.theta) + "</b></span>" +
        '<span><span class="ax-oc-lbl">ν per vol point</span> <b class="ax-oc-num">' + money(g.vega) + "</b></span></div>";
      var now = r.payoff && r.payoff.target && r.target_date !== r.first_expiry;
      html += '<div class="ax-oc-legend"><span><i class="ax-oc-sw" aria-hidden="true"></i>At expiry ' + dmy(r.first_expiry) + "</span>" +
        (now ? '<span><i class="ax-oc-sw ax-oc-sw-now" aria-hidden="true"></i>Today, ' + dmy(r.target_date) + "</span>" : "") +
        "<span>" + esc(F.number(r.legs.length, 0)) + (r.legs.length === 1 ? " leg" : " legs") + " × lot " + esc(F.number(r.lot_size, 0)) + "</span></div>" +
        '<div class="ax-oc-chart" id="ax-oc-chart"></div>' +
        '<p class="ax-oc-note">' + (r.notes || []).map(esc).join(" ") + "</p>";
      res.innerHTML = html;
      chart();
    }

    // ── the payoff chart (SVG) ─────────────────────────────────────────────────────────────
    function chart() {
      var r = S.res, box = $("ax-oc-chart");
      if (!r || !box || !r.payoff || !r.payoff.spot || r.payoff.spot.length < 2) return;
      var xs = r.payoff.spot, e = r.payoff.expiry, t = r.payoff.target;
      var now = !!(t && t.length === xs.length && r.target_date !== r.first_expiry);
      var W = Math.max(260, Math.floor(box.clientWidth || 320)), narrow = W < 440, H = narrow ? 230 : 280;
      var m = { l: narrow ? 66 : 76, r: 12, t: 22, b: 28 };
      var x0 = xs[0], x1 = xs[xs.length - 1], ys = e.concat(now ? t : []).concat([0]);
      var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys), pad = (y1 - y0) * 0.08 || 1;
      y0 -= pad; y1 += pad;
      var X = function (v) { return m.l + (v - x0) / ((x1 - x0) || 1) * (W - m.l - m.r); };
      var Y = function (v) { return m.t + (y1 - v) / (y1 - y0) * (H - m.t - m.b); };
      var P = function (a) { return a.map(function (v, i) { return (i ? "L" : "M") + r1(X(xs[i])) + "," + r1(Y(v)); }).join(""); };
      var id = "ax-oc-c" + (++S.cid), z = r1(Y(0)), spot = Number(r.spot);
      var bes = r.breakevens || [];
      var label = "Payoff chart. At expiry: max profit " + (r.unlimited_profit ? "unlimited" : F.inr(r.max_profit, true)) +
        ", max loss " + (r.unlimited_loss ? "unlimited" : F.inr(r.max_loss, true)) +
        (bes.length ? ", breakevens " + bes.map(function (b) { return F.number(b, 2); }).join(" and ") : ", no breakeven") +
        ". Use the left and right arrow keys to read the P&L at each price.";
      var s = '<svg class="ax-oc-svg" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + " " + H + '" role="img" tabindex="0" aria-label="' + esc(label) + '">' +
        '<defs><clipPath id="' + id + 'u"><rect x="0" y="0" width="' + W + '" height="' + z + '"/></clipPath>' +
        '<clipPath id="' + id + 'd"><rect x="0" y="' + z + '" width="' + W + '" height="' + Math.max(0, H - z) + '"/></clipPath></defs>';
      var area = "M" + r1(X(x0)) + "," + z + P(e).replace(/^M/, "L") + "L" + r1(X(x1)) + "," + z + "Z";
      s += '<path class="ax-oc-fill-up" d="' + area + '" clip-path="url(#' + id + 'u)"/><path class="ax-oc-fill-dn" d="' + area + '" clip-path="url(#' + id + 'd)"/>';
      for (var k = 0; k <= 4; k++) {
        var v = y0 + (y1 - y0) * k / 4, y = r1(Y(v));
        s += '<line class="ax-oc-gl" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y + '" y2="' + y + '"/>' +
          '<text class="ax-oc-tick" x="' + (m.l - 6) + '" y="' + r1(y + 4) + '" text-anchor="end">' + esc(F.inr(v, true, true)) + "</text>";
      }
      (narrow ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]).forEach(function (f) {
        var v = x0 + (x1 - x0) * f;
        s += '<text class="ax-oc-tick" x="' + r1(X(v)) + '" y="' + (H - 8) + '" text-anchor="' + (f === 0 ? "start" : f === 1 ? "end" : "middle") + '">' + esc(F.number(v, 0)) + "</text>";
      });
      s += '<line class="ax-oc-zero" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + z + '" y2="' + z + '"/>';
      if (has(spot) && spot >= x0 && spot <= x1) {
        var sx = r1(X(spot));
        s += '<line class="ax-oc-spotl" x1="' + sx + '" x2="' + sx + '" y1="' + m.t + '" y2="' + (H - m.b) + '"/>' +
          '<text class="ax-oc-tick ax-oc-spott" x="' + sx + '" y="' + (m.t - 8) + '" text-anchor="middle">Spot ' + esc(F.number(spot, 0)) + "</text>";
      }
      if (now) s += '<path class="ax-oc-pl-now" d="' + P(t) + '"/>';
      s += '<path class="ax-oc-pl-exp" d="' + P(e) + '"/>';
      bes.forEach(function (b, i) {
        var bx = r1(X(b));
        s += '<circle class="ax-oc-bep" cx="' + bx + '" cy="' + z + '" r="4.5"/><text class="ax-oc-bel" x="' + bx + '" y="' + r1(z + (i % 2 ? 18 : -9)) +
          '" text-anchor="middle">' + esc(F.number(b, 0)) + "</text>";
      });
      s += '<line class="ax-oc-xh" y1="' + m.t + '" y2="' + (H - m.b) + '" style="display:none"/><circle class="ax-oc-dot1" r="4.5" style="display:none"/>' +
        (now ? '<circle class="ax-oc-dot2" r="4.5" style="display:none"/>' : "") +
        '<rect class="ax-oc-hit" x="' + m.l + '" y="' + m.t + '" width="' + (W - m.l - m.r) + '" height="' + (H - m.t - m.b) + '"/></svg>' +
        '<div class="ax-oc-tip" hidden></div>';
      box.innerHTML = s;

      var svg = box.querySelector("svg"), tip = box.querySelector(".ax-oc-tip"), xh = svg.querySelector(".ax-oc-xh");
      var d1 = svg.querySelector(".ax-oc-dot1"), d2 = svg.querySelector(".ax-oc-dot2");
      var near = function (v) { var best = 0, bd = Infinity; xs.forEach(function (x, i) { var dd = Math.abs(x - v); if (dd < bd) { bd = dd; best = i; } }); return best; };
      var show = function (i) {
        i = Math.max(0, Math.min(xs.length - 1, i));
        S.hover = i;
        var px = r1(X(xs[i])), scale = svg.getBoundingClientRect().width / W || 1;
        xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.style.display = "";
        d1.setAttribute("cx", px); d1.setAttribute("cy", r1(Y(e[i]))); d1.style.display = "";
        if (d2) { d2.setAttribute("cx", px); d2.setAttribute("cy", r1(Y(t[i]))); d2.style.display = ""; }
        tip.innerHTML = "<b>Spot " + esc(F.number(xs[i], 2)) + "</b> " + (has(spot) ? move((xs[i] / spot - 1) * 100, pctMag) : "") +
          "<br>At expiry " + money(e[i]) + (now ? "<br>Today " + money(t[i]) : "");
        tip.hidden = false;
        var left = px * scale + 12;
        if (left + tip.offsetWidth > box.clientWidth) left = px * scale - tip.offsetWidth - 12;
        tip.style.left = Math.max(0, left) + "px";
        tip.style.top = "4px";
      };
      var hide = function () { tip.hidden = true; xh.style.display = "none"; d1.style.display = "none"; if (d2) d2.style.display = "none"; };
      var at = function (ev) { var b = svg.getBoundingClientRect(); show(near(x0 + ((ev.clientX - b.left) / (b.width / W) - m.l) / (W - m.l - m.r) * (x1 - x0))); };
      svg.addEventListener("pointermove", at);
      svg.addEventListener("pointerdown", at);
      svg.addEventListener("pointerleave", hide);
      svg.addEventListener("focus", function () { show(near(has(spot) ? spot : (x0 + x1) / 2)); });
      svg.addEventListener("blur", hide);
      svg.addEventListener("keydown", function (ev) {
        var i = S.hover < 0 ? near(spot) : S.hover, step = ev.shiftKey ? 10 : 1;
        if (ev.key === "ArrowRight") show(i + step);
        else if (ev.key === "ArrowLeft") show(i - step);
        else if (ev.key === "Home") show(0);
        else if (ev.key === "End") show(xs.length - 1);
        else if (ev.key === "Escape") hide();
        else return;
        ev.preventDefault();
      });
    }

    // ── events ─────────────────────────────────────────────────────────────────────────────
    function pickSymbol(v) {
      if (v !== S.sym && legs().length) {
        var L = legs();
        L.splice(0, L.length);
        if (builder()) { try { drawLegs(); } catch (e) {} }                 // eslint-disable-line no-undef
        say("Legs cleared: they were for " + S.sym + ".");
      }
      S.d = null;
      load(v, null, false);
      if (builder() && bsym && clean(bsym.value) !== v) {                   // keep the builder on the same underlying
        bsym.value = v;
        try { bsym.dispatchEvent(new Event("change")); } catch (e) {}
      }
    }
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var v = clean(inp.value);
      if (!v) {
        inp.setAttribute("aria-invalid", "true");
        meta.innerHTML = '<span class="ax-oc-neg" role="alert">Type an F&amp;O symbol such as NIFTY, BANKNIFTY or RELIANCE.</span>';
        inp.focus();
        return;
      }
      pickSymbol(v);
    });
    expSel.addEventListener("change", function () { if (expSel.value) load(S.sym, expSel.value, false); });
    nSel.addEventListener("change", function () { S.n = Number(nSel.value) || 10; if (S.sym) load(S.sym, S.d && S.d.expiry, false); });
    gBtn.setAttribute("aria-pressed", S.greeks ? "true" : "false");
    gBtn.addEventListener("click", function () {
      S.greeks = !S.greeks;
      gBtn.setAttribute("aria-pressed", S.greeks ? "true" : "false");
      pref("ax-oc-greeks", S.greeks ? "1" : "0");
      var top = scroll.scrollTop;
      renderTable();
      scroll.scrollTop = top;
      if (S.d) centre();
    });
    function mode(sell) {
      S.sell = sell;
      $("ax-oc-buy").setAttribute("aria-pressed", sell ? "false" : "true");
      $("ax-oc-sell").setAttribute("aria-pressed", sell ? "true" : "false");
      var top = scroll.scrollTop, left = scroll.scrollLeft;
      renderTable();
      scroll.scrollTop = top; scroll.scrollLeft = left;
    }
    $("ax-oc-buy").addEventListener("click", function () { mode(false); });
    $("ax-oc-sell").addEventListener("click", function () { mode(true); });
    document.addEventListener("keydown", function (e) { if (e.key === "Shift") S.shift = true; });
    document.addEventListener("keyup", function (e) { if (e.key === "Shift") S.shift = false; });
    window.addEventListener("blur", function () { S.shift = false; });
    scroll.addEventListener("click", function (ev) {
      var b = ev.target.closest && ev.target.closest("button.ax-oc-ltp");
      if (!b) return;
      var flip = ev.shiftKey || S.shift;
      addLeg(b.getAttribute("data-k"), Number(b.getAttribute("data-s")), flip ? !S.sell : S.sell);
    });
    legsBox.addEventListener("click", function (ev) {
      var b = ev.target.closest && ev.target.closest("button[data-act]");
      if (!b) return;
      var L = legs(), act = b.getAttribute("data-act"), i = Number(b.getAttribute("data-i")), l = L[i];
      if (act === "clear") L.splice(0, L.length);
      else if (!l) return;
      else if (act === "side") l.side = String(l.side).toUpperCase() === "SELL" ? "BUY" : "SELL";
      else if (act === "plus") l.lots = Math.min(1000, (Number(l.lots) || 1) + 1);
      else if (act === "minus") l.lots = Math.max(1, (Number(l.lots) || 1) - 1);
      else if (act === "rm") L.splice(i, 1);
      changed();
      var again = legsBox.querySelector('[data-act="' + act + '"][data-i="' + i + '"]:not([disabled])') ||
        legsBox.querySelector('[data-act="rm"]') || scroll;
      again.focus();
    });
    // the builder's own edits (its leg table, "+ leg", Build, Analyse) redraw its list: price them here too
    var bl = $("legs");
    if (bl) {
      if (window.MutationObserver) new MutationObserver(function () { renderLegs(); schedule(120); }).observe(bl, { childList: true });
      bl.addEventListener("change", function () { renderLegs(); schedule(); });
    }
    if (bsym) bsym.addEventListener("change", function () { var v = clean(bsym.value); if (v && v !== S.sym) { S.d = null; load(v, null, false); } });
    var rt = 0;
    window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { if (S.res) chart(); }, 150); });

    // ── start ──────────────────────────────────────────────────────────────────────────────
    var first = clean(new URLSearchParams(location.search).get("symbol")) || clean(bsym && bsym.value) || "NIFTY";
    renderLegs();
    load(first, null, false);
    if (builder() && bsym && clean(bsym.value) !== first) { bsym.value = first; try { bsym.dispatchEvent(new Event("change")); } catch (e) {} }
  }

  // Only for a page served without the shell (?shell=0, so no atip.js): the few AX pieces this card uses.
  function shim() {
    function f(v) { if (v === null || v === undefined || v === "" || typeof v === "boolean") return null; var x = Number(v); return isFinite(x) ? x : null; }
    function grp(w) { return w.length > 3 ? w.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + w.slice(-3) : w; }
    var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
    var fmt = {
      number: function (v, dp) { var x = f(v); if (x === null) return "—"; dp = dp === undefined ? 2 : dp; var p = Math.abs(x).toFixed(dp).split("."); return (x < 0 && Number(Math.abs(x).toFixed(dp)) ? "−" : "") + grp(p[0]) + (dp ? "." + p[1] : ""); },
      inr: function (v, sign, compact) {
        var x = f(v); if (x === null) return "—";
        var pre = x < -0.004 ? "−" : (sign && x > 0.004 ? "+" : ""), a = Math.abs(x);
        if (compact) { if (a >= 1e7) return pre + "₹" + (a / 1e7).toFixed(2) + " Cr"; if (a >= 1e5) return pre + "₹" + (a / 1e5).toFixed(2) + " L"; if (a >= 1e3) return pre + "₹" + (a / 1e3).toFixed(1) + "K"; return pre + "₹" + a.toFixed(0); }
        var p = a.toFixed(2).split("."); return pre + "₹" + grp(p[0]) + "." + p[1];
      },
      pct: function (v, sign) { var x = f(v); if (x === null) return "—"; if (Math.abs(x) < 0.005) return "0.00%"; return (sign === false ? (x < 0 ? "−" : "") : (x > 0 ? "+" : "−")) + Math.abs(x).toFixed(2) + "%"; }
    };
    var token = function () {
      var m = document.querySelector('meta[name="atip-token"]'); if (m && m.content) return m.content;
      try { if (typeof TOKEN !== "undefined" && TOKEN) return TOKEN; } catch (e) {}                  // eslint-disable-line no-undef
      return "";
    };
    var api = function (url, opts) {
      opts = opts || {};
      var o = { method: opts.body !== undefined ? "POST" : "GET", headers: { "X-ATIP-Token": token() }, cache: "no-store" };
      if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
      return fetch(url, o).then(function (r) {
        return r.text().then(function (t) {
          var d; try { d = t ? JSON.parse(t) : {}; } catch (e) { d = { error: t || "HTTP " + r.status }; }
          if (r.status === 401) throw new Error("This page has no permission token. Reload the page and try again.");
          if (!r.ok) throw new Error((d && d.error) || "HTTP " + r.status);
          return d;
        });
      });
    };
    return { fmt: fmt, esc: esc, api: api, token: token };
  }

  // atip.js (window.AX) is loaded with `defer` at the end of the page: start once the document has parsed, after it.
  if (document.readyState === "complete") boot();
  else { document.addEventListener("DOMContentLoaded", boot); window.addEventListener("load", boot); }
})();
