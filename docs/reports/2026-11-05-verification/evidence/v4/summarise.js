// Summarise sweep_<server>.json + interact_<server>.json into summary.json and a printed digest.
const fs = require("fs");
const path = require("path");
const DIR = "/Users/agtci/ATIP/.ux_build/verify-2026-11-05/ui";
const S = {};
for (const label of ["test", "prod"]) {
  const o = JSON.parse(fs.readFileSync(path.join(DIR, `sweep_${label}.json`), "utf8"));
  const it = JSON.parse(fs.readFileSync(path.join(DIR, `interact_${label}.json`), "utf8"));
  const s = { axeVersion: o.axe, runs: 0, status: [], errors: [], theme: [], hscroll: [], axe: {}, contrastNodes: [], tabular: {}, colourBlind: {}, features: {}, rawSkeleton: {} };
  for (const [p, runs] of Object.entries(o.pages)) {
    s.rawSkeleton[p] = runs.rawHtml;
    const bg = {};
    for (const [k, r] of Object.entries(runs)) {
      if (k === "rawHtml") continue;
      s.runs++;
      const [theme, w] = k.split("_");
      if (r.fail) s.errors.push({ page: p, run: k, fail: r.fail });
      if (r.status !== 200) s.status.push({ page: p, run: k, status: r.status, finalUrl: r.finalUrl, title: r.title });
      const e = [].concat((r.pageErrors || []).map((x) => "pageerror: " + x), (r.consoleErrors || []).map((x) => "console: " + x), (r.server5xx || []).map((x) => "5xx: " + x), (r.subresource4xx || []).map((x) => "4xx: " + x));
      if (e.length) s.errors.push({ page: p, run: k, errors: e });
      if (r.theme) {
        bg[w] = bg[w] || {}; bg[w][theme] = r.theme.bodyBg;
        if (r.theme.dataTheme !== theme) s.theme.push({ page: p, run: k, problem: "data-theme=" + r.theme.dataTheme });
      }
      if (r.hscroll && r.hscroll.overflow) s.hscroll.push({ page: p, run: k, scrollWidth: r.hscroll.scrollWidth, innerWidth: r.hscroll.innerWidth, offenders: r.hscroll.offenders.slice(0, 3), widest: r.hscroll.widest });
      if (Array.isArray(r.axe)) for (const v of r.axe) {
        const a = (s.axe[v.id] = s.axe[v.id] || { impact: v.impact, nodes: 0, runs: 0, pages: new Set(), sample: v.nodes.slice(0, 2) });
        a.nodes += v.n; a.runs++; a.pages.add(p + "@" + k);
        if (v.id === "color-contrast") for (const n of v.nodes) s.contrastNodes.push(Object.assign({ page: p, run: k }, n));
      } else if (r.axe) s.errors.push({ page: p, run: k, axe: r.axe });
      if (r.tabular && theme === "light") s.tabular[p + "@" + w] = { tables: r.tabular.tables, checkedAll: r.tabular.checked, failingAll: r.tabular.failing, visibleChecked: r.tabular.visibleChecked, visibleFailing: r.tabular.visibleFailing,
        failTabular: r.tabular.failTabular, failAlign: r.tabular.failAlign, effectiveTabularFail: r.tabular.effectiveTabularFail, examples: r.tabular.examples };
      if (r.colourBlind) s.colourBlind[p + "@" + k] = { candidates: r.colourBlind.candidates, byToken: r.colourBlind.byToken, byClass: r.colourBlind.byClass, failing: r.colourBlind.failing, failingClasses: r.colourBlind.classHits, examples: r.colourBlind.examples.slice(0, 5), legacyHue: { candidates: r.colourBlind.legacyHue.candidates, failing: r.colourBlind.legacyHue.failing, examples: r.colourBlind.legacyHue.examples.slice(0, 3) } };
      if (r.features && k === "dark_1440") s.features[p] = r.features;
      if (r.features && k === "dark_375") s.features[p + "@375"] = { feedPill: r.features.feedPill, updatedStamp: r.features.updatedStamp, visibleSticky: r.features.tables.visibleSticky, tablesVisible: r.features.tables.visible };
    }
    for (const [w, t] of Object.entries(bg)) if (!t.light || !t.dark || t.light === t.dark) s.theme.push({ page: p, width: w, problem: "light/dark body background identical or missing", light: t.light, dark: t.dark });
  }
  for (const a of Object.values(s.axe)) a.pages = Array.from(a.pages);
  s.contrastWorst = s.contrastNodes.slice().sort((a, b) => a.ratio - b.ratio).slice(0, 10);
  s.contrastDistinct = Array.from(new Map(s.contrastNodes.map((n) => [n.target + "|" + n.fg + "|" + n.bg, { target: n.target, ratio: n.ratio, fg: n.fg, bg: n.bg, fontSize: n.fontSize, text: n.text, example: n.page + "@" + n.run }])).values());
  s.keyboard = Object.fromEntries(Object.entries(it.keyboard).map(([k, r]) => [k, r.fail ? { fail: r.fail } : { visibleInteractive: r.interactive.visibleInteractive, reached: r.summary.distinctReached, focusNotVisible: r.summary.focusNotVisible,
    focusNotVisibleExamples: r.summary.focusNotVisibleExamples, offScreenFocus: r.summary.offScreenFocus, wrapAt: r.summary.wrapAt, trap: r.summary.trap, skipped: r.unreachable.skippedCount, skippedList: r.unreachable.skipped, beyond200: r.unreachable.beyondCount, errors: r.errors }]));
  s.interactions = it.features;
  S[label] = s;
}
fs.writeFileSync(path.join(DIR, "summary.json"), JSON.stringify(S, null, 1));

// digest
for (const [label, s] of Object.entries(S)) {
  console.log(`\n================ ${label}  (axe ${s.axeVersion}, ${s.runs} runs)`);
  console.log("non-200:", JSON.stringify(s.status));
  console.log("errors:", JSON.stringify(s.errors).slice(0, 1500));
  console.log("theme problems:", JSON.stringify(s.theme));
  console.log("hscroll:", s.hscroll.length, JSON.stringify(s.hscroll).slice(0, 2500));
  console.log("axe:"); for (const [id, a] of Object.entries(s.axe)) console.log(`  ${id} [${a.impact}] nodes=${a.nodes} runs=${a.runs} pages=${a.pages.length}: ${a.pages.slice(0, 50).join(", ")}`);
  console.log("contrast distinct:", JSON.stringify(s.contrastDistinct));
  console.log("tabular (light):"); for (const [k, t] of Object.entries(s.tabular)) if (t.checkedAll) console.log(`  ${k} tables=${t.tables} visible ${t.visibleFailing}/${t.visibleChecked} fail (all ${t.failingAll}/${t.checkedAll}; noTabular ${t.failTabular} notRight ${t.failAlign}; effectiveTabularFail ${t.effectiveTabularFail}) ex: ${t.examples.slice(0, 2).map((e) => e.sel.split(" > ").slice(-2).join(">") + " '" + e.text + "' " + e.fvn + "/" + e.align).join(" ; ")}`);
  console.log("colour-blind:"); for (const [k, c] of Object.entries(s.colourBlind)) if (c.failing || c.legacyHue.failing) console.log(`  ${k} fail ${c.failing}/${c.candidates} legacy ${c.legacyHue.failing}/${c.legacyHue.candidates} ${JSON.stringify(c.failingClasses)} ex: ${c.examples.slice(0, 3).map((e) => "'" + e.text + "' (" + (e.cls || e.token) + ") ctx '" + (e.context || "").slice(0, 40) + "'").join(" ; ")}`);
}
