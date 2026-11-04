// Verification sweep (4 Nov 2026), part 2: keyboard walk (Tab x200) on /home, /sessions and / at 1440 x 900 in both
// themes, plus the interactive feature checks: session card link, first scores row (stock detail panel), the
// Sessions "Table view" sort, the /home table sort, AX.flash on a CMP cell. Read-only: it never clicks Buy/Sell/order controls.
// usage: node interact.js <label test|prod> <base-url>
const fs = require("fs");
const path = require("path");
const pw = require("/Users/agtci/GoKesari/node_modules/playwright");
const EXE = "/Users/agtci/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell";
const OUT = "/Users/agtci/ATIP/.ux_build/verify-2026-11-04/ui";
const SHOTS = "/Users/agtci/ATIP/docs/testing/screenshots/verify-2026-11-04/web";
const [label, base] = process.argv.slice(2);
const host = new URL(base).host;

const SEL_JS = String.raw`window.__sel = function (el) {
  const parts = []; let e = el;
  for (let i = 0; i < 4 && e && e.nodeType === 1 && e !== document.documentElement; i++) {
    let s = e.tagName.toLowerCase();
    if (e.id) { parts.unshift(s + "#" + e.id); break; }
    const cls = Array.from(e.classList).slice(0, 2).join("."); if (cls) s += "." + cls;
    const p = e.parentElement; if (p) { const same = Array.from(p.children).filter((c) => c.tagName === e.tagName); if (same.length > 1) s += ":nth-of-type(" + (same.indexOf(e) + 1) + ")"; }
    parts.unshift(s); e = e.parentElement;
  }
  return parts.join(" > ");
};
window.__sortAudit = function (t) {
  const th0 = Array.from(t.tHead ? t.tHead.rows[0].cells : []); const out = [];
  const num = (x) => { const s = String(x).replace(/[₹,%\s]|Rs\.?/g, "").replace(/[−–]/g, "-").replace(/^\+/, ""); return /^-?\d+(\.\d+)?$/.test(s) ? Number(s) : null; };
  th0.forEach((th, i) => {
    if (!th.hasAttribute("aria-sort")) return;
    const rows = () => Array.from(t.tBodies[0].rows).filter((r) => !r.classList.contains("ax-total"));
    const cells = rows().map((r) => r.cells[i]).filter(Boolean);
    const withDataV = cells.filter((c) => c.hasAttribute("data-v")).length;
    th.click(); if (th.getAttribute("aria-sort") !== "ascending") th.click();
    const vals = rows().map((r) => r.cells[i] ? r.cells[i].textContent.replace(/\s+/g, " ").trim() : "");
    const nums = vals.map(num); const numeric = nums.filter((v) => v !== null).length;
    let mono = null;
    if (numeric >= 2 && numeric === vals.filter((v) => v && v !== "—" && v !== "-").length) { mono = true; const nn = nums.filter((v) => v !== null); for (let k = 1; k < nn.length; k++) if (nn[k] < nn[k - 1]) { mono = false; break; } }
    out.push({ col: th.textContent.trim().slice(0, 24), aria: th.getAttribute("aria-sort"), cells: cells.length, withDataV, numericCells: numeric, ascNumericMonotonic: mono, ascFirst4: vals.slice(0, 4) });
  });
  return out;
};
window.__vis = function (el) { if (!el.getClientRects().length) return false; if (el.checkVisibility && !el.checkVisibility({ visibilityProperty: true })) return false; const cs = getComputedStyle(el); if (cs.visibility === "hidden" || cs.display === "none") return false; if (el.closest("[inert]")) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };`;

async function newPage(browser, theme) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme });
  await ctx.route("**/*", (r) => { const u = new URL(r.request().url()); if (u.host === host || u.protocol === "data:" || u.protocol === "blob:") return r.continue(); return r.abort(); });
  await ctx.addInitScript(SEL_JS);
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e.message).slice(0, 200)));
  page.on("console", (m) => { if (m.type() === "error" && !/net::ERR|Failed to load resource/.test(m.text())) errs.push("console: " + m.text().slice(0, 200)); });
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
  return { ctx, page, errs };
}
async function open(page, p, theme) {
  const resp = await page.goto(base + p + (p.includes("?") ? "&" : "?") + "theme=" + theme, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForLoadState("load", { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(2000);
  return resp ? resp.status() : null;
}

async function keyboard(browser, p, theme) {
  const { ctx, page, errs } = await newPage(browser, theme);
  const rec = { page: p, theme, status: await open(page, p, theme) };
  rec.interactive = await page.evaluate(() => {
    const all = Array.from(document.querySelectorAll("a[href], button, input:not([type=hidden]), select, textarea, [tabindex], summary"));
    let n = 0, visible = 0;
    all.forEach((el) => {
      el.setAttribute("data-kb", String(n++));
      const ti = el.getAttribute("tabindex");
      const native = el.matches("a[href], button, input, select, textarea, summary");
      const isInteractive = native || (ti !== null && Number(ti) >= 0);
      const vis = window.__vis(el) && !el.disabled && el.getAttribute("aria-hidden") !== "true";
      el.__kbVisible = isInteractive && vis;
      if (el.__kbVisible) { el.setAttribute("data-kbv", "1"); visible++; }
    });
    return { tagged: n, visibleInteractive: visible };
  });
  await page.evaluate(() => { if (document.activeElement) document.activeElement.blur(); window.scrollTo(0, 0); });
  const steps = [];
  const seen = new Map();
  let wrapAt = null, stuck = 0, stuckAt = null, prev = null;
  for (let i = 0; i < 200; i++) {
    await page.keyboard.press("Tab");
    const s = await page.evaluate(() => {
      const a = document.activeElement;
      if (!a || a === document.body || a === document.documentElement) return { tag: a ? a.tagName.toLowerCase() : null, body: true };
      const cs = getComputedStyle(a);
      const outline = cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0;
      const shadow = cs.boxShadow && cs.boxShadow !== "none";
      const r = a.getBoundingClientRect();
      return { kb: a.getAttribute("data-kb"), tag: a.tagName.toLowerCase(), id: a.id || null, cls: (typeof a.className === "string" ? a.className : "").slice(0, 40),
        text: ((a.getAttribute("aria-label") || a.textContent || a.value || "") + "").replace(/\s+/g, " ").trim().slice(0, 30),
        focusVisible: !!(outline || shadow), how: outline ? "outline " + cs.outlineWidth + " " + cs.outlineStyle + " " + cs.outlineColor : shadow ? "box-shadow" : "none",
        matchesFV: a.matches(":focus-visible"), onScreen: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth, inDialog: !!a.closest("dialog,[role=dialog],.ax-drawer"),
        sel: window.__sel(a) };
    });
    const key = s.body ? "BODY" : (s.kb !== null ? "kb" + s.kb : "dyn:" + s.sel);
    s.i = i + 1; steps.push(s);
    if (key === prev) { stuck++; if (stuck >= 2 && stuckAt === null) stuckAt = i + 1; } else stuck = 0;
    prev = key;
    if (key !== "BODY" && seen.has(key) && wrapAt === null) wrapAt = { step: i + 1, firstSeenStep: seen.get(key), key };
    if (!seen.has(key)) seen.set(key, i + 1);
  }
  const reached = new Set(steps.filter((s) => s.kb !== undefined && s.kb !== null).map((s) => Number(s.kb)));
  const maxReached = reached.size ? Math.max(...reached) : -1;
  const un = await page.evaluate(({ reached, maxReached }) => {
    const set = new Set(reached); const skipped = [], beyond = [];
    document.querySelectorAll("[data-kbv]").forEach((el) => {
      const k = Number(el.getAttribute("data-kb")); if (set.has(k)) return;
      const o = { kb: k, sel: window.__sel(el), tag: el.tagName.toLowerCase(), tabindex: el.getAttribute("tabindex"),
        text: ((el.getAttribute("aria-label") || el.textContent || el.value || "") + "").replace(/\s+/g, " ").trim().slice(0, 30) };
      (k < maxReached ? skipped : beyond).push(o);
    });
    return { skippedCount: skipped.length, skipped: skipped.slice(0, 15), beyondCount: beyond.length, beyondSample: beyond.slice(0, 5) };
  }, { reached: Array.from(reached), maxReached });
  const focusSteps = steps.filter((s) => !s.body);
  rec.summary = {
    presses: 200, distinctReached: reached.size, dynamicReached: new Set(steps.filter((s) => !s.body && (s.kb === null || s.kb === undefined)).map((s) => s.sel)).size,
    bodySteps: steps.filter((s) => s.body).length, focusNotVisible: focusSteps.filter((s) => !s.focusVisible).length,
    focusNotVisibleExamples: Array.from(new Map(focusSteps.filter((s) => !s.focusVisible).map((s) => [s.sel, { sel: s.sel, text: s.text, step: s.i }])).values()).slice(0, 10),
    offScreenFocus: focusSteps.filter((s) => !s.onScreen).length,
    wrapAt, stuckAt, trap: null };
  // trap: focus wraps to an element seen before without passing through <body>, and the cycle is short while many controls remain
  if (wrapAt) {
    const cyc = steps.slice(wrapAt.firstSeenStep - 1, wrapAt.step - 1);
    const passedBody = cyc.some((s) => s.body);
    const cycleLen = wrapAt.step - wrapAt.firstSeenStep;
    rec.summary.trap = (!passedBody && cycleLen < rec.interactive.visibleInteractive * 0.5) ? { cycleLen, firstSeenStep: wrapAt.firstSeenStep, members: cyc.slice(0, 10).map((s) => s.sel) } : false;
    rec.summary.cycleLen = cycleLen; rec.summary.cyclePassedBody = passedBody;
  }
  if (stuckAt) rec.summary.trap = Object.assign({ stuckAt }, rec.summary.trap || {});
  rec.unreachable = un;
  rec.steps = steps;
  rec.errors = errs;
  await ctx.close();
  return rec;
}

async function features(browser, theme) {
  const res = {};
  // Sessions: Table view + first session card link
  {
    const { ctx, page, errs } = await newPage(browser, theme);
    await open(page, "/sessions", theme);
    res.sessionsTableView = await page.evaluate(() => {
      const d = Array.from(document.querySelectorAll("details")).find((x) => /table view/i.test((x.querySelector("summary") || {}).textContent || ""));
      if (!d) return { found: false };
      d.querySelector("summary").click();
      const t = d.querySelector("table");
      if (!t) return { found: true, table: false };
      const ths = Array.from(t.querySelectorAll("th[aria-sort]"));
      const th0 = t.tHead ? t.tHead.rows[0].cells : [];
      const sticky = th0.length ? getComputedStyle(th0[0]).position : null;
      const out = { found: true, open: d.open, table: true, tableId: t.id || null, tableClass: t.className, rows: t.tBodies[0] ? t.tBodies[0].rows.length : 0,
        headers: Array.from(th0).map((h) => h.textContent.trim().slice(0, 20)), sortableThs: ths.length, stickyTh: sticky,
        filterInput: !!document.querySelector('[data-ax-filter="' + t.id + '"]'), csv: !!document.querySelector('[data-ax-csv="' + t.id + '"]') };
      // sort: click the last sortable header twice and see aria-sort + row order change
      const th = ths.find((h) => /p&l|pnl|return|%|equity|value/i.test(h.textContent)) || ths[ths.length - 1];
      if (th) {
        const idx = Array.from(th0).indexOf(th);
        const col = () => Array.from(t.tBodies[0].rows).map((r) => (r.cells[idx] || {}).textContent ? r.cells[idx].textContent.trim() : "");
        const before = col(); th.click(); const a1 = th.getAttribute("aria-sort"); const after1 = col(); th.click(); const a2 = th.getAttribute("aria-sort"); const after2 = col();
        out.sortTest = { header: th.textContent.trim().slice(0, 30), ariaAfter1: a1, ariaAfter2: a2, orderChanged: JSON.stringify(before) !== JSON.stringify(after1) || JSON.stringify(after1) !== JSON.stringify(after2), first3asc: after1.slice(0, 3), first3desc: after2.slice(0, 3) };
      }
      out.sortAudit = window.__sortAudit(t);
      return out;
    });
    await page.screenshot({ path: path.join(SHOTS, `${label}_sessions-tableview_${theme}_1440.png`), fullPage: false }).catch(() => {});
    const link = await page.$("a.ax-tc-id");
    if (link) {
      const href = await link.getAttribute("href");
      await Promise.all([page.waitForLoadState("domcontentloaded").catch(() => {}), link.click()]);
      await page.waitForTimeout(1500);
      res.sessionCardClick = { href, landedOn: page.url().slice(base.length), title: await page.title(),
        h1: await page.evaluate(() => (document.querySelector("h1") || {}).textContent || null),
        drawerOpen: await page.evaluate(() => Array.from(document.querySelectorAll(".ax-drawer.open, dialog[open], [role=dialog]")).filter(window.__vis).map((d) => window.__sel(d))) };
    } else res.sessionCardClick = { found: false };
    res.sessionsErrors = errs;
    await ctx.close();
  }
  // Home: table sort + first session card link
  {
    const { ctx, page, errs } = await newPage(browser, theme);
    await open(page, "/home", theme);
    res.homeTableSort = await page.evaluate(() => {
      const t = document.querySelector("table.ax-table"); if (!t) return { found: false };
      const ths = Array.from(t.querySelectorAll("th[aria-sort]")); const th0 = Array.from(t.tHead.rows[0].cells);
      const th = ths[ths.length - 1]; if (!th) return { found: true, sortable: 0 };
      const idx = th0.indexOf(th); const col = () => Array.from(t.tBodies[0].rows).map((r) => r.cells[idx] ? r.cells[idx].textContent.trim() : "");
      const b = col(); th.click(); const a1 = th.getAttribute("aria-sort"); const c1 = col(); th.click(); const a2 = th.getAttribute("aria-sort"); const c2 = col();
      const audit = window.__sortAudit(t);
      return { sortAudit: audit, found: true, tableId: t.id || null, sortable: ths.length, header: th.textContent.trim(), ariaAfter1: a1, ariaAfter2: a2, rows: b.length, orderChanged: JSON.stringify(b) !== JSON.stringify(c1) || JSON.stringify(c1) !== JSON.stringify(c2),
        stickyTh: getComputedStyle(th0[0]).position, filterInput: !!document.querySelector('[data-ax-filter="' + t.id + '"]') };
    });
    const link = await page.$(".ax-sgrid a.ax-tc-id");
    if (link) {
      const href = await link.getAttribute("href");
      await Promise.all([page.waitForLoadState("domcontentloaded").catch(() => {}), link.click()]);
      await page.waitForTimeout(1500);
      res.homeCardClick = { href, landedOn: page.url().slice(base.length), h1: await page.evaluate(() => (document.querySelector("h1") || {}).textContent || null) };
    } else res.homeCardClick = { found: false };
    res.homeErrors = errs;
    await ctx.close();
  }
  // Scores (/): CMP cells, AX.flash, first row click -> stock detail
  {
    const { ctx, page, errs } = await newPage(browser, theme);
    await open(page, "/", theme);
    res.cmpFlash = await page.evaluate(async () => {
      const cells = Array.from(document.querySelectorAll("td.cmpcell[data-cmp]")); const vis = cells.filter(window.__vis);
      const o = { cmpCells: cells.length, visible: vis.length, attrs: vis[0] ? Array.from(vis[0].attributes).map((a) => a.name) : [], axFlash: !!(window.AX && window.AX.flash) };
      if (vis[0] && o.axFlash) {
        const c = vis[0]; const before = c.className;
        window.AX.flash(c, 1); const up = c.className; const anim = getComputedStyle(c).animationName;
        await new Promise((r) => setTimeout(r, 50)); const bgUp = getComputedStyle(c).backgroundColor;
        window.AX.flash(c, -1); const down = c.className; const animD = getComputedStyle(c).animationName;
        await new Promise((r) => setTimeout(r, 1400)); const after = c.className;
        Object.assign(o, { classBefore: before, classUp: up, animationUp: anim, bgDuringUp: bgUp, classDown: down, animationDown: animD, classAfter1400ms: after });
      }
      return o;
    });
    const before = await page.evaluate(() => { window.__before = new Set(Array.from(document.querySelectorAll("body *")).filter((e) => { const cs = getComputedStyle(e); return (cs.position === "fixed" || cs.position === "absolute") && window.__vis(e); })); return window.__before.size; });
    const urlBefore = page.url();
    const row = await page.evaluateHandle(() => Array.from(document.querySelectorAll("table#st tbody tr[data-sym]")).find(window.__vis) || Array.from(document.querySelectorAll("tr[data-sym]")).find(window.__vis));
    const el = row.asElement();
    if (el) {
      const sym = await el.getAttribute("data-sym");
      const cell = await el.$("td:nth-child(2)");
      await (cell || el).click();
      await page.waitForTimeout(2500);
      res.scoresRowClick = await page.evaluate(({ sym }) => {
        const now = Array.from(document.querySelectorAll("body *")).filter((e) => { const cs = getComputedStyle(e); return (cs.position === "fixed" || cs.position === "absolute") && window.__vis(e) && !window.__before.has(e); });
        const big = now.filter((e) => { const r = e.getBoundingClientRect(); return r.width > 200 && r.height > 150; });
        const top = big.filter((e) => !big.some((o) => o !== e && o.contains(e)));
        const dlg = Array.from(document.querySelectorAll("dialog[open], [role=dialog], .ax-drawer.open, [aria-modal=true]")).filter(window.__vis);
        return { sym, newOverlayCount: now.length, panels: top.slice(0, 4).map((e) => { const r = e.getBoundingClientRect(); const h = e.querySelector("h1,h2,h3,h4,.title,b");
          return { sel: window.__sel(e), role: e.getAttribute("role"), ariaModal: e.getAttribute("aria-modal"), w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left), heading: h ? h.textContent.trim().slice(0, 50) : null, mentionsSym: (e.textContent || "").includes(sym), text: (e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120) }; }),
          dialogs: dlg.map((d) => window.__sel(d)), focusIn: document.activeElement ? window.__sel(document.activeElement) : null };
      }, { sym });
      res.scoresRowClick.urlChanged = page.url() !== urlBefore ? page.url().slice(base.length) : false;
      await page.screenshot({ path: path.join(SHOTS, `${label}_scores-rowclick_${theme}_1440.png`), fullPage: false }).catch(() => {});
      // Escape closes it?
      await page.keyboard.press("Escape"); await page.waitForTimeout(600);
      res.scoresRowClick.afterEscapeStillOpen = await page.evaluate(() => Array.from(document.querySelectorAll("body *")).filter((e) => { const cs = getComputedStyle(e); const r = e.getBoundingClientRect(); return (cs.position === "fixed" || cs.position === "absolute") && window.__vis(e) && !window.__before.has(e) && r.width > 200 && r.height > 150; }).length);
    } else res.scoresRowClick = { found: false, overlaysBefore: before };
    res.scoresErrors = errs;
    await ctx.close();
  }
  return res;
}

(async () => {
  const browser = await pw.chromium.launch({ executablePath: EXE, headless: true });
  const out = { label, base, started: new Date().toISOString(), keyboard: {}, features: {} };
  for (const theme of ["dark", "light"]) {
    for (const p of ["/home", "/sessions", "/"]) {
      try { out.keyboard[p + "@" + theme] = await keyboard(browser, p, theme); } catch (e) { out.keyboard[p + "@" + theme] = { fail: String(e.message).slice(0, 200) }; }
      process.stdout.write("kb " + p + "@" + theme + " ");
    }
    try { out.features[theme] = await features(browser, theme); } catch (e) { out.features[theme] = { fail: String(e.message).slice(0, 300) }; }
    process.stdout.write("features@" + theme + " ");
  }
  out.finished = new Date().toISOString();
  await browser.close();
  fs.writeFileSync(path.join(OUT, `interact_${label}.json`), JSON.stringify(out, null, 1));
  console.log("\ndone " + label);
})();
