// App-view screenshots (verification 4 Nov 2026): the Android and iOS apps are WebView shells that load these pages
// with "ATIPApp/<version>" in the user agent (the page shell then hides its own nav; the native tab bar replaces it).
// Native builds are not possible on this Mac (no Xcode, no Android SDK / Java), so this renders exactly what the web
// views show, at 390 x 844 (iPhone) and 412 x 915 (Android), dark (the apps' default) and light.
// usage: node app_views.js <base-url> <out-dir> <label>
const fs = require("fs");
const path = require("path");
const pw = require("/Users/agtci/GoKesari/node_modules/playwright");
const [base, outDir, label] = process.argv.slice(2);
const TABS = [["home", "/home"], ["watchlist", "/watchlist"], ["trade", "/command"], ["sessions", "/sessions"],
  ["live", "/m?tab=live"], ["more", "/more"]];
const DEVICES = { ios: { w: 390, h: 844, ua: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 ATIPApp/2026.11.04.1" },
  android: { w: 412, h: 915, ua: "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36 ATIPApp/2026.11.04.1" } };
(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await pw.chromium.launch({ executablePath: "/Users/agtci/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell" });
  const out = [];
  for (const [dev, d] of Object.entries(DEVICES)) {
    for (const theme of ["dark", "light"]) {
      const ctx = await browser.newContext({ viewport: { width: d.w, height: d.h }, userAgent: d.ua, isMobile: true, hasTouch: true, colorScheme: theme });
      await ctx.route("**/*", (r) => (r.request().url().startsWith(base) ? r.continue() : r.abort()));
      for (const [name, p] of TABS) {
        const page = await ctx.newPage();
        const errs = [];
        page.on("pageerror", (e) => errs.push(String(e.message).slice(0, 120)));
        const rec = { device: dev, theme, tab: name, path: p };
        try {
          const resp = await page.goto(base + p + (p.includes("?") ? "&" : "?") + "theme=" + theme, { waitUntil: "domcontentloaded", timeout: 45000 });
          await page.waitForTimeout(2000);
          rec.status = resp ? resp.status() : null;
          rec.theme = await page.evaluate(() => document.documentElement.dataset.theme || null);
          rec.hscroll = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
          rec.shellNavHidden = await page.evaluate(() => { const n = document.querySelector('nav[aria-label="Main"]'); return !n || getComputedStyle(n).display === "none" || n.offsetParent === null; });
          rec.cards = await page.evaluate(() => document.querySelectorAll(".ax-tcard").length);
          rec.criArrow = await page.evaluate(() => /CRI↓/.test(document.body.innerText));
          rec.errors = errs;
          await page.screenshot({ path: path.join(outDir, `${label}_${dev}_${name}_${theme}.png`) });
        } catch (e) { rec.fail = String(e.message).slice(0, 160); }
        out.push(rec);
        await page.close();
      }
      await ctx.close();
    }
  }
  await browser.close();
  fs.writeFileSync(path.join(outDir, `${label}_app_views.json`), JSON.stringify(out, null, 1));
  for (const r of out) console.log(`${r.device} ${r.theme} ${r.tab}: ${r.status} theme=${r.theme} hscroll=${r.hscroll} navHidden=${r.shellNavHidden} cards=${r.cards} cri↓=${r.criArrow} ${r.fail || ""} ${(r.errors || []).join("; ")}`);
})();
