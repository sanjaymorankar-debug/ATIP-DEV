// KGF sweep (16 Nov 2026), from the verification sweep (4 Nov 2026), adapted from .ux_build/auditfix-2026-11-03/ui/ux_check.js.
// Every page x theme (light, dark) x width (1440x900, 375x812): HTTP status, JS errors, applied theme + body background,
// horizontal scroll (widest offender), axe-core wcag2a/wcag2aa/wcag21aa (contrast ratio/fg/bg), tabular numerals and
// right alignment of numeric table cells, colour-blind heuristic, feature presence, one viewport screenshot.
// usage: node sweep.js <label test|prod> <base-url> [pages=a,b]
const fs = require("fs");
const path = require("path");
const pw = require("/Users/agtci/GoKesari/node_modules/playwright");
const AXE = fs.readFileSync("/Users/agtci/GoKesari/node_modules/axe-core/axe.min.js", "utf8");
const EXE = "/Users/agtci/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell";
const OUT = "/Users/agtci/ATIP/.ux_build/kite-fix-2026-11-16/ui";
const SHOTS = "/Users/agtci/ATIP/.claude/worktrees/kite-fix-2026-11-16/docs/testing/screenshots/kgf-2026-11-16";
const [label, base, pagesArg] = process.argv.slice(2);
const PAGES = pagesArg ? pagesArg.split(",") : ["/home", "/sessions", "/sessions/S3", "/ab", "/algo-signals",
  "/accounting?tab=tax", "/accounting", "/rca", "/more", "/reports", "/command"];
const TAGS = ["wcag2a", "wcag2aa", "wcag21aa"];
const SIZES = [[1440, 900], [390, 844]];
const pageName = (p) => (p === "/" ? "scores" : p.slice(1).replace(/[/?=&]+/g, "_"));

// Runs inside the page: everything except axe.
function probe(args) {
  const { width } = args;
  const sel = (el) => {
    const parts = []; let e = el;
    for (let i = 0; i < 4 && e && e.nodeType === 1 && e !== document.documentElement; i++) {
      let s = e.tagName.toLowerCase();
      if (e.id) { parts.unshift(s + "#" + e.id); break; }
      const cls = Array.from(e.classList).slice(0, 2).join(".");
      if (cls) s += "." + cls;
      const p = e.parentElement;
      if (p) { const same = Array.from(p.children).filter((c) => c.tagName === e.tagName); if (same.length > 1) s += ":nth-of-type(" + (same.indexOf(e) + 1) + ")"; }
      parts.unshift(s); e = e.parentElement;
    }
    return parts.join(" > ");
  };
  const vis = (el) => { if (!el.getClientRects().length) return false; if (el.checkVisibility && !el.checkVisibility({ visibilityProperty: true })) return false; /* closed <details> content, content-visibility:hidden */ const cs = getComputedStyle(el); return cs.visibility !== "hidden" && cs.display !== "none"; };
  const r = {};
  // 2. theme
  const root = document.documentElement;
  r.theme = { dataTheme: root.dataset.theme || null, bodyBg: getComputedStyle(document.body).backgroundColor,
    htmlBg: getComputedStyle(root).backgroundColor, bodyColor: getComputedStyle(document.body).color,
    colorScheme: getComputedStyle(root).colorScheme, bodyClass: document.body.className.slice(0, 80) };
  // 3. horizontal scroll
  const iw = window.innerWidth, sw = root.scrollWidth;
  r.hscroll = { innerWidth: iw, scrollWidth: sw, bodyScrollWidth: document.body.scrollWidth, overflow: sw > iw + 1, offenders: [] };
  if (sw > iw + 1) {
    const clipped = (el) => { for (let a = el.parentElement; a && a !== document.body && a !== root; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if (o !== "visible") return true; } return false; };
    const offs = [];
    document.body.querySelectorAll("*").forEach((el) => {
      const rc = el.getBoundingClientRect(); if (!rc.width || rc.right <= iw + 1) return;
      const cs = getComputedStyle(el); if (cs.position === "fixed" || cs.visibility === "hidden") return;
      if (clipped(el)) return;
      offs.push({ el, right: Math.round(rc.right + window.scrollX), width: Math.round(rc.width) });
    });
    // outermost offenders first (an offender whose parent is not an offender is where the overflow starts)
    const set = new Set(offs.map((o) => o.el));
    const top = offs.filter((o) => !set.has(o.el.parentElement));
    top.sort((a, b) => b.right - a.right || b.width - a.width);
    offs.sort((a, b) => b.right - a.right || b.width - a.width);
    r.hscroll.offenderCount = offs.length;
    r.hscroll.offenders = top.slice(0, 5).map((o) => ({ sel: sel(o.el), right: o.right, width: o.width, text: (o.el.textContent || "").trim().slice(0, 40) }));
    r.hscroll.widest = offs.slice(0, 3).map((o) => ({ sel: sel(o.el), right: o.right, width: o.width }));
  }
  // 5. tabular numerals + right alignment
  const isNum = (t) => {
    if (!t || t.length > 40 || !/\d/.test(t)) return false;
    if (/^\d{4}-\d{2}-\d{2}/.test(t) || /^\d{1,2}[ -](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(t) || /\d:\d\d/.test(t)) return false;
    const rest = t.replace(/[₹$€+\-−–%,.\d\s()▲▼↑↓×x/]/g, " ").replace(/\b(L|Cr|K|k|cr|lakh|lakhs|crore|bn|mn|M|B|pp|bp|bps)\b/g, "").trim();
    return rest === "";
  };
  const tab = { tables: document.querySelectorAll("table").length, checked: 0, failing: 0, failTabular: 0, failAlign: 0,
    visibleChecked: 0, visibleFailing: 0, effectiveTabularFail: 0, examples: [] };
  document.querySelectorAll("table td").forEach((td) => {
    const t = (td.textContent || "").replace(/\s+/g, " ").trim();
    if (!isNum(t)) return;
    tab.checked++;
    const cs = getComputedStyle(td), v = vis(td);
    if (v) tab.visibleChecked++;
    const tn = /tabular-nums/.test(cs.fontVariantNumeric), al = /^(right|end|-webkit-right)$/.test(cs.textAlign);
    // effective: every element that holds a text node of the cell renders tabular-nums
    let eff = true; const tw = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) { if (!/\d/.test(n.nodeValue)) continue; if (!/tabular-nums/.test(getComputedStyle(n.parentElement).fontVariantNumeric)) { eff = false; break; } }
    if (!eff) tab.effectiveTabularFail++;
    if (!tn || !al) {
      tab.failing++; if (!tn) tab.failTabular++; if (!al) tab.failAlign++; if (v) tab.visibleFailing++;
      if (tab.examples.length < 5 && v) tab.examples.push({ sel: sel(td), text: t.slice(0, 30), fvn: cs.fontVariantNumeric, align: cs.textAlign });
    }
  });
  if (tab.examples.length < 5) document.querySelectorAll("table td").forEach((td) => {
    if (tab.examples.length >= 5) return; const t = (td.textContent || "").replace(/\s+/g, " ").trim(); if (!isNum(t)) return;
    const cs = getComputedStyle(td); if (/tabular-nums/.test(cs.fontVariantNumeric) && /^(right|end)$/.test(cs.textAlign)) return;
    if (tab.examples.some((x) => x.sel === sel(td))) return;
    tab.examples.push({ sel: sel(td), text: t.slice(0, 30), fvn: cs.fontVariantNumeric, align: cs.textAlign, hidden: true });
  });
  r.tabular = tab;
  // 6. colour-blind heuristic
  const rs = getComputedStyle(root);
  const toRgb = (c) => { const d = document.createElement("span"); d.style.color = c; document.body.appendChild(d); const v = getComputedStyle(d).color; d.remove(); return v; };
  const posTok = rs.getPropertyValue("--ax-pos").trim(), negTok = rs.getPropertyValue("--ax-neg").trim();
  const posRgb = posTok ? toRgb(posTok) : null, negRgb = negTok ? toRgb(negTok) : null;
  const CLS = /(pos|neg|up|down|gain|loss|buy|sell)/i;
  const CLS_STOP = /group|popup|setup|dropdown|update|upload|lookup|markup|backup|startup|signup|cleanup|pickup|followup|position|compos|purpos|expos|deposit|repos|propos|suppl|super|upper|cupboard|sup$|^sup|popover|topup|pos-?rel|pos-?abs|ax-pop|negotia|possib|posit/i;
  const MEAN = /[▲▼↑↓+−%✓✔✗✘⚠]|(^|[^\w])-\s?[\d₹]|\b(buy|sell|sold|bought|hold|holding|held|wait|exit|exited|long|short|up|down|gain|gains|gained|loss|losses|lost|profit|profits|positive|negative|good|bad|strong|weak|bull|bullish|bear|bearish|rise|rising|fall|falling|pass|passed|fail|failed|ok|live|stale|closed|open|above|below|win|wins|won|error|halted|running|stopped|stop|target|yes|no|on|off|enabled|disabled|active|inactive|connected|missing|danger|warning|safe|risk|filled|unfilled|healthy|degraded|outage|neutral|improving|worsening|better|worse|over|under|drop|dropped|surge|cut|added|removed|paid|due|done|complete|completed|success|succeeded|pending|ready|skipped|valid|invalid|approved|rejected|blocked|allowed|true|false|none|new|fresh|fine|normal|high|low|cheap|expensive|overbought|oversold|accumulation|distribution|sideways|uptrend|downtrend|breakout|breakdown)\b/i;
  const hsl = (rgb) => { const m = rgb.match(/[\d.]+/g); if (!m) return null; let [R, G, B] = m.slice(0, 3).map((x) => x / 255); const a = m[3] !== undefined ? +m[3] : 1;
    const mx = Math.max(R, G, B), mn = Math.min(R, G, B), l = (mx + mn) / 2; let h = 0, s = 0;
    if (mx !== mn) { const d = mx - mn; s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn); h = mx === R ? (G - B) / d + (G < B ? 6 : 0) : mx === G ? (B - R) / d + 2 : (R - G) / d + 4; h *= 60; }
    return { h, s, l, a }; };
  const cb = { posToken: posTok, negToken: negTok, posRgb, negRgb, candidates: 0, byToken: 0, byClass: 0, failing: 0, classHits: {}, candidateClasses: {}, examples: [],
    legacyHue: { candidates: 0, failing: 0, classHits: {}, examples: [] } };
  const cand = new Map();
  const legacy = new Map();
  document.body.querySelectorAll("*").forEach((el) => {
    if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|svg|path|g|SVG)$/i.test(el.tagName) || el.closest("svg")) return;
    const txt = (el.textContent || "").trim(); if (!txt) return;
    if (!vis(el)) return;
    const cs = getComputedStyle(el), col = cs.color;
    const parentCol = el.parentElement ? getComputedStyle(el.parentElement).color : null;
    const tokenHit = (col === posRgb || col === negRgb) && parentCol !== col;
    const clsTok = Array.from(el.classList).find((c) => CLS.test(c) && !CLS_STOP.test(c));
    if (tokenHit || clsTok) { cand.set(el, { token: tokenHit ? (col === posRgb ? "pos" : "neg") : null, cls: clsTok || null }); return; }
    const h = hsl(col);
    if (h && parentCol !== col && h.a > 0.5 && h.s > 0.35 && h.l > 0.2 && h.l < 0.85 && ((h.h >= 90 && h.h <= 170) || h.h >= 345 || h.h <= 12)) legacy.set(el, { color: col, hue: Math.round(h.h) });
  });
  const hasAncestorIn = (el, m) => { let a = el.parentElement; for (let i = 0; i < 4 && a; i++, a = a.parentElement) if (m.has(a)) return true; return false; };
  const labelled = (el) => { for (let a = el, i = 0; a && i < 4; a = a.parentElement, i++) { const s = (a.getAttribute("aria-label") || "") + " " + (a.getAttribute("title") || ""); if (MEAN.test(s)) return true; } return false; };
  const judge = (m, out, isMain) => {
    m.forEach((info, el) => {
      if (hasAncestorIn(el, m)) return;
      out.candidates++;
      if (isMain) { if (info.token) cb.byToken++; else cb.byClass++; if (info.cls) cb.candidateClasses[info.cls] = (cb.candidateClasses[info.cls] || 0) + 1; }
      const txt = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (MEAN.test(txt) || labelled(el)) return;
      out.failing++;
      if (info.cls) out.classHits[info.cls] = (out.classHits[info.cls] || 0) + 1; else if (info.token) out.classHits['(token ' + info.token + ')'] = (out.classHits['(token ' + info.token + ')'] || 0) + 1;
      if (out.examples.length < 8) out.examples.push(Object.assign({ sel: sel(el), text: txt.slice(0, 40), context: el.parentElement ? (el.parentElement.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 70) : null }, info));
    });
  };
  judge(cand, cb, true); judge(legacy, cb.legacyHue, false);
  r.colourBlind = cb;
  // 7. feature presence (static part)
  const f = {};
  const kp = document.querySelector(".ax-kpis");
  f.kpiStrip = { selector: ".ax-kpis", found: !!kp, tiles: kp ? Array.from(kp.querySelectorAll(".ax-kpi")).map((k) => (k.getAttribute("aria-label") || "") + ": " + ((k.querySelector(".ax-val") || {}).textContent || "").trim()).slice(0, 12) : [] };
  const sg = document.querySelector(".ax-sgrid");
  f.sessionGrid = { selector: "ul.ax-sgrid", found: !!sg, compact: sg ? sg.classList.contains("compact") : false, cards: document.querySelectorAll(".ax-tcard").length };
  const scores = Array.from(document.querySelectorAll(".ax-score"));
  f.scoreBadges = { selector: ".ax-score", count: scores.length, withTitle: scores.filter((s) => s.title).length,
    withFormulaTitle: scores.filter((s) => /\.\d{2}|×|weighted|mean/i.test(s.title || "")).length, withDescribedby: scores.filter((s) => s.hasAttribute("aria-describedby")).length,
    sampleTitle: scores[0] ? (scores[0].title || "").slice(0, 160) : null };
  const tv = Array.from(document.querySelectorAll("details")).find((d) => /table view/i.test((d.querySelector("summary") || {}).textContent || ""));
  f.tableView = { selector: "details>summary ~ 'Table view'", found: !!tv, hasTable: tv ? !!tv.querySelector("table") : false,
    tableClass: tv && tv.querySelector("table") ? tv.querySelector("table").className : null,
    sortableThs: tv ? tv.querySelectorAll("th[aria-sort]").length : 0 };
  const tables = Array.from(document.querySelectorAll("table"));
  const visTables = tables.filter(vis);
  const stickyOf = (t) => { const th = t.querySelector("thead th, th"); const thead = t.tHead; return (th && getComputedStyle(th).position === "sticky") || (thead && getComputedStyle(thead).position === "sticky"); };
  f.tables = { total: tables.length, visible: visTables.length, stickyHeader: tables.filter(stickyOf).length, visibleSticky: visTables.filter(stickyOf).length,
    axTable: document.querySelectorAll("table.ax-table").length,
    thAriaSort: document.querySelectorAll("th[aria-sort]").length, tablesWithSort: tables.filter((t) => t.querySelector("th[aria-sort]")).length,
    filterInputs: document.querySelectorAll("[data-ax-filter]").length, searchInputs: document.querySelectorAll("input[type=search]").length,
    csvButtons: document.querySelectorAll("[data-ax-csv]").length, rowDrawers: document.querySelectorAll("tr[data-drawer]").length };
  f.sparklines = { selector: "svg.ax-spark | [class*=spark] svg", axSpark: document.querySelectorAll("svg.ax-spark").length, other: document.querySelectorAll("[class*=spark]:not(svg) svg, svg[class*=spark]:not(.ax-spark)").length };
  f.updatedStamp = Array.from(document.querySelectorAll("[data-ax-updated], .ax-updated")).slice(0, 4).map((e) => ({ text: (e.textContent || "").trim().slice(0, 40), state: e.getAttribute("data-state"), visible: vis(e),
    staleMark: e.getAttribute("data-state") === "stale" ? getComputedStyle(e, "::before").content : null }));
  f.feedPillCount = document.querySelectorAll("[data-ax-feed]").length;
  f.feedPill = Array.from(document.querySelectorAll("[data-ax-feed]")).filter(vis).slice(0, 3).map((e) => ({ text: (e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60), state: e.getAttribute("data-state"), visible: vis(e) }));
  f.skeletonInDom = document.querySelectorAll(".ax-skel").length;
  f.drawers = Array.from(document.querySelectorAll(".ax-drawer")).map((d) => d.id || d.className);
  f.cmpCells = { selector: "td.cmpcell[data-cmp]", count: document.querySelectorAll("td.cmpcell[data-cmp], [data-cmp]").length, axFlash: !!(window.AX && typeof window.AX.flash === "function") };
  f.navMain = document.querySelectorAll('nav[aria-label="Main"]').length;
  f.h1 = (document.querySelector("h1") || {}).textContent ? document.querySelector("h1").textContent.trim().slice(0, 60) : null;
  r.features = f;
  return r;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true }); fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await pw.chromium.launch({ executablePath: EXE, headless: true });
  const out = { label, base, axe: null, tags: TAGS, started: new Date().toISOString(), pages: {} };
  const host = new URL(base).host;
  for (const p of PAGES) {
    out.pages[p] = { rawHtml: null };
    // raw HTML (no JS): skeleton loaders present before data loads
    try {
      const ctx0 = await browser.newContext(); const rq = await ctx0.request.get(base + p, { timeout: 45000 });
      const body = await rq.text();
      out.pages[p].rawHtml = { status: rq.status(), bytes: body.length, axSkel: (body.match(/ax-skel/g) || []).length, ariaBusy: (body.match(/aria-busy="true"/g) || []).length };
      await ctx0.close();
    } catch (e) { out.pages[p].rawHtml = { fail: String(e.message).slice(0, 160) }; }
    for (const theme of ["light", "dark"]) {
      for (const [w, h] of SIZES) {
        const t0 = Date.now();
        const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
        const blocked = new Set();
        await ctx.route("**/*", (r) => { const u = new URL(r.request().url()); if (u.host === host || u.protocol === "data:" || u.protocol === "blob:") return r.continue(); blocked.add(u.host); return r.abort(); });
        const page = await ctx.newPage();
        const errs = [], cons = [];
        page.on("pageerror", (e) => errs.push(String(e.message).slice(0, 200)));
        page.on("console", (m) => { if (m.type() === "error" && !/net::ERR|Failed to load resource/.test(m.text())) cons.push(m.text().slice(0, 200)); });
        const s5 = [], s4 = [];
        page.on("response", (r) => { if (!r.url().startsWith(base)) return; if (r.status() >= 500) s5.push(r.status() + " " + r.url().slice(base.length)); else if (r.status() >= 400 && r.url() !== base + p) s4.push(r.status() + " " + r.url().slice(base.length, base.length + 120)); });
        const rec = { pageErrors: errs, consoleErrors: cons, server5xx: s5, subresource4xx: s4 };
        try {
          const url = base + p + (p.includes("?") ? "&" : "?") + "theme=" + theme;
          const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
          rec.status = resp ? resp.status() : null;
          rec.finalUrl = page.url().slice(base.length);
          await page.waitForLoadState("load", { timeout: 15000 }).catch(() => {});
          await page.waitForTimeout(2000);
          rec.title = await page.title();
          Object.assign(rec, await page.evaluate(probe, { width: w }));
          await page.screenshot({ path: path.join(SHOTS, `${label}_${pageName(p)}_${theme}_${w}.png`), fullPage: false });
          await page.addScriptTag({ content: AXE });
          if (!out.axe) out.axe = await page.evaluate(() => window.axe.version);
          rec.axe = await Promise.race([new Promise((_, rej) => setTimeout(() => rej(new Error("axe timeout 90s")), 90000)), page.evaluate(async (tags) => {
            const r = await window.axe.run(document, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations"] });
            return r.violations.map((v) => ({ id: v.id, impact: v.impact, n: v.nodes.length,
              nodes: v.nodes.slice(0, 6).map((n) => { const d = (n.any && n.any[0] && n.any[0].data) || {};
                const o = { target: n.target.join(" ").slice(0, 140) };
                if (v.id === "color-contrast" || v.id === "color-contrast-enhanced") Object.assign(o, { ratio: d.contrastRatio, expected: d.expectedContrastRatio, fg: d.fgColor, bg: d.bgColor, fontSize: d.fontSize, fontWeight: d.fontWeight, text: (n.html || "").replace(/<[^>]+>/g, "").trim().slice(0, 40) });
                else o.html = (n.html || "").slice(0, 120);
                return o; }) }));
          }, TAGS)]).catch((e) => ({ error: String(e.message) }));
        } catch (e) {
          rec.fail = String(e.message).slice(0, 200);
        }
        rec.blockedHosts = Array.from(blocked);
        rec.ms = Date.now() - t0;
        out.pages[p][theme + "_" + w] = rec;
        await ctx.close();
      }
    }
    process.stdout.write(p + " ");
    fs.writeFileSync(path.join(OUT, `sweep_${label}.json`), JSON.stringify(out, null, 1));
  }
  out.finished = new Date().toISOString();
  await browser.close();
  fs.writeFileSync(path.join(OUT, `sweep_${label}.json`), JSON.stringify(out, null, 1));
  console.log("\ndone " + label + " " + out.started + " -> " + out.finished);
})();
