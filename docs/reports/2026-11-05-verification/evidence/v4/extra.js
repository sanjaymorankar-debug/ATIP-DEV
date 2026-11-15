// Verification sweep (4 Nov 2026), part 3: follow-ups to what the sweep found. Read-only.
// 1 top-bar "Updated" stamp visibility by width; 2 forced stale stamp (AX.stamp(hms, true)) -> ⚠; 3 /research skeletons after 12 s;
// 4 the stock panel opened from a scores row (role, aria-modal, focus); 5 scores-table header sorting; 6 score badge focusability.
// usage: node extra.js <label> <base-url>
const fs = require("fs");
const path = require("path");
const pw = require("/Users/agtci/GoKesari/node_modules/playwright");
const EXE = "/Users/agtci/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell";
const OUT = "/Users/agtci/ATIP/.ux_build/verify-2026-11-05/ui";
const [label, base] = process.argv.slice(2);
const host = new URL(base).host;

(async () => {
  const browser = await pw.chromium.launch({ executablePath: EXE, headless: true });
  const mk = async (w, h) => {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, colorScheme: "dark" });
    await ctx.route("**/*", (r) => { const u = new URL(r.request().url()); return u.host === host ? r.continue() : r.abort(); });
    const page = await ctx.newPage(); page.on("dialog", (d) => d.dismiss().catch(() => {}));
    return { ctx, page };
  };
  const go = async (page, p) => { await page.goto(base + p + (p.includes("?") ? "&" : "?") + "theme=dark", { waitUntil: "domcontentloaded" }); await page.waitForLoadState("load").catch(() => {}); await page.waitForTimeout(2000); };
  const out = { label, base, at: new Date().toISOString() };

  // 1 + 2: stamp
  out.stamp = {};
  for (const [w, h] of [[1440, 900], [1680, 1050], [1920, 1080], [1024, 768], [375, 812]]) {
    const { ctx, page } = await mk(w, h);
    await go(page, "/sessions");
    out.stamp[w] = await page.evaluate(() => {
      const el = document.querySelector(".ax-top [data-ax-updated], .ax-top .ax-updated");
      if (!el) return { found: false };
      let c = el.parentElement; while (c && getComputedStyle(c).containerName.indexOf("axtop") < 0) c = c.parentElement;
      return { found: true, display: getComputedStyle(el).display, text: el.textContent.trim(), state: el.getAttribute("data-state"), axtopContainerWidth: c ? Math.round(c.getBoundingClientRect().width) : null, axtopContainer: c ? c.className : null };
    });
    if (w === 1920) {
      out.staleForced = await page.evaluate(() => {
        if (!(window.AX && window.AX.stamp)) return { axStamp: false };
        window.AX.stamp("12:00:00", true);
        const els = Array.from(document.querySelectorAll("[data-ax-updated]"));
        return { axStamp: true, els: els.map((e) => ({ text: e.textContent.trim(), state: e.getAttribute("data-state"), before: getComputedStyle(e, "::before").content, color: getComputedStyle(e).color, weight: getComputedStyle(e).fontWeight, title: e.title, visible: getComputedStyle(e).display !== "none" })) };
      });
      await page.screenshot({ path: `/Users/agtci/ATIP/docs/testing/screenshots/verify-2026-11-05/web/${label}_sessions-stale-stamp_dark_1920.png`, clip: { x: 0, y: 0, width: 1920, height: 120 } });
    }
    await ctx.close();
  }

  // 3: /research skeletons
  {
    const { ctx, page } = await mk(1440, 900);
    const reqs = [];
    page.on("response", (r) => { if (r.url().startsWith(base) && r.request().resourceType().match(/fetch|xhr/)) reqs.push(r.status() + " " + r.url().slice(base.length, base.length + 100)); });
    const failed = []; page.on("requestfailed", (r) => failed.push(r.url().slice(0, 100) + " " + (r.failure() || {}).errorText));
    await go(page, "/research");
    const at2 = await page.evaluate(() => document.querySelectorAll(".ax-skel").length);
    await page.waitForTimeout(10000);
    out.research = { skelAt2s: at2, skelAt12s: await page.evaluate(() => document.querySelectorAll(".ax-skel").length),
      skelWhere: await page.evaluate(() => Array.from(document.querySelectorAll(".ax-skel")).slice(0, 5).map((e) => { const s = e.closest("section,.ax-card,[id]"); return (s ? (s.id || s.className) : "") + " :: " + ((s && s.querySelector("h2,h3")) ? s.querySelector("h2,h3").textContent.trim().slice(0, 40) : ""); })),
      ariaBusy: await page.evaluate(() => document.querySelectorAll("[aria-busy=true]").length), xhr: reqs.slice(0, 20), failed: failed.slice(0, 10) };
    await ctx.close();
  }

  // 4 + 5 + 6: scores page panel, header sort, badges
  {
    const { ctx, page } = await mk(1440, 900);
    await go(page, "/");
    out.scoresHeaders = await page.evaluate(() => {
      const t = document.querySelector("table#st"); if (!t) return { found: false };
      const ths = Array.from(t.tHead.rows[0].cells);
      return { found: true, ths: ths.length, withOnclick: ths.filter((h) => h.onclick || h.getAttribute("onclick")).length, ariaSort: ths.filter((h) => h.hasAttribute("aria-sort")).length,
        cursorPointer: ths.filter((h) => getComputedStyle(h).cursor === "pointer").length, stickyTh: getComputedStyle(ths[0]).position, wrapOverflowY: getComputedStyle(t.parentElement).overflowY };
    });
    out.scoresHeaderClick = await page.evaluate(async () => {
      const t = document.querySelector("table#st"); if (!t) return null;
      const th = Array.from(t.tHead.rows[0].cells).find((h) => /ATIP/.test(h.textContent));
      const first = () => Array.from(t.tBodies[0].rows).slice(0, 3).map((r) => r.getAttribute("data-sym"));
      const b = first(); th.click(); await new Promise((r) => setTimeout(r, 300)); const a = first();
      return { header: th.textContent.trim(), before: b, after: a, changed: JSON.stringify(a) !== JSON.stringify(b) };
    });
    const row = await page.$("table#st tbody tr[data-sym] td:nth-child(2)");
    if (row) {
      await row.click(); await page.waitForTimeout(2500);
      out.stockPanel = await page.evaluate(() => {
        const sv = document.getElementById("sv"); if (!sv) return { found: false };
        const a = (n) => sv.getAttribute(n);
        return { found: true, role: a("role"), ariaModal: a("aria-modal"), ariaLabel: a("aria-label"), ariaLabelledby: a("aria-labelledby"), tag: sv.tagName.toLowerCase(), className: sv.className,
          activeElement: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null, focusInside: sv.contains(document.activeElement),
          closeButton: !!sv.querySelector("button[aria-label*=lose], button[title*=lose], .close, [onclick*=close]"), headings: Array.from(sv.querySelectorAll("h1,h2,h3")).map((h) => h.textContent.trim().slice(0, 40)).slice(0, 4) };
      });
      await page.keyboard.press("Tab"); await page.waitForTimeout(100);
      out.stockPanel.afterOneTab = await page.evaluate(() => { const sv = document.getElementById("sv"); const ae = document.activeElement; return { focusInside: sv ? sv.contains(ae) : null, active: ae ? (ae.tagName.toLowerCase() + (ae.id ? "#" + ae.id : "") + " " + (ae.textContent || "").trim().slice(0, 30)) : null }; });
      for (let i = 0; i < 25; i++) await page.keyboard.press("Tab");
      out.stockPanel.after26Tabs = await page.evaluate(() => { const sv = document.getElementById("sv"); const ae = document.activeElement; return { focusInside: sv ? sv.contains(ae) : null, active: ae ? (ae.tagName.toLowerCase() + (ae.id ? "#" + ae.id : "") + " " + (ae.textContent || "").trim().slice(0, 30)) : null }; });
    }
    await ctx.close();
  }
  {
    const { ctx, page } = await mk(1440, 900);
    await go(page, "/sessions");
    out.scoreBadges = await page.evaluate(() => { const s = Array.from(document.querySelectorAll(".ax-score")); return { count: s.length, focusable: s.filter((e) => e.tabIndex >= 0 || e.closest("a,button,[tabindex]")).length, role: Array.from(new Set(s.map((e) => e.getAttribute("role")))), ariaLabelSample: s[0] ? s[0].getAttribute("aria-label") : null, titleSample: s[0] ? s[0].title.slice(0, 220) : null }; });
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, `extra_${label}.json`), JSON.stringify(out, null, 1));
  console.log(JSON.stringify(out, null, 1));
})();
