"""
The design system's server-side components (UXD-03, 2 Nov 2026): money, tiles, tables and charts as HTML, so every
new page is built from the same parts. The browser side of them (sort, filter, column chooser, CSV export,
row -> drawer, chart hover) is dashboard/static/atip/atip.js.

Money is Indian-grouped to the paisa (₹1,25,000.00) and a gain or loss always carries its sign (+ / −) as well as
its colour, so colour is never the only cue. Charts follow the dataviz rules: one axis, thin 2 px lines, a recessive
grid, a legend for two or more series plus direct labels, a crosshair tooltip, and the table beside them.
"""

from __future__ import annotations

import html
import json

from enterprise.session_reports import inr


def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def money(v, sign: bool = False) -> str:
    """₹ figure; sign=True for P&L (+ / − and the gain / loss colour)."""
    if v is None or v == "":
        return '<span class="ax-muted">—</span>'
    v = float(v)
    tone = (" ax-pos" if v > 0.004 else " ax-neg" if v < -0.004 else "") if sign else ""
    return f'<span class="ax-num{tone}">{e(inr(v, sign))}</span>'


def pct(v, sign: bool = True) -> str:
    if v is None:
        return '<span class="ax-muted">—</span>'
    v = float(v)
    tone = (" ax-pos" if v > 0 else " ax-neg" if v < 0 else "") if sign else ""
    return f'<span class="ax-num{tone}">{"+" if sign and v > 0 else "−" if v < 0 else ""}{abs(v):.2f}%</span>'


def compact(v) -> str:
    """₹25.7K, ₹1.25L, ₹2.10Cr for axis labels."""
    v = float(v)
    a, s = abs(v), "−" if v < 0 else ""
    if a >= 1e7:
        return f"{s}₹{a / 1e7:.2f}Cr"
    if a >= 1e5:
        return f"{s}₹{a / 1e5:.2f}L"
    if a >= 1e3:
        return f"{s}₹{a / 1e3:.1f}K"
    return f"{s}₹{a:.0f}"


def tile(label: str, value_html: str, sub: str = "", key: bool = False) -> str:
    return (f'<div class="ax-tile{" ax-key" if key else ""}"><div class="ax-lbl">{e(label)}</div>'
            f'<div class="ax-val">{value_html}</div>{f"<div class=ax-sub2>{sub}</div>" if sub else ""}</div>')


def status(text: str, tone: str = "") -> str:
    return f'<span class="ax-st {e(tone)}">{e(text)}</span>'


def mode_status(mode: str, st: str) -> str:
    tone = {"RUNNING": "good", "PAUSED": "warn", "OFF": ""}.get(st, "")
    sym = {"RUNNING": "●", "PAUSED": "⏸", "OFF": "○"}.get(st, "")
    return status(f"{sym} {st}", tone) + " " + status(mode, "warn" if mode == "AUTO" else "info" if mode == "CONFIRM" else "")


def _cell(kind: str, v) -> tuple[str, str]:
    """(html, data-v) for one table cell."""
    if kind == "money":
        return money(v), "" if v is None else str(v)
    if kind == "pnl":
        return money(v, True), "" if v is None else str(v)
    if kind == "pct":
        return pct(v), "" if v is None else str(v)
    if kind == "rate":                                 # a rate or a share, not a gain: no sign, no colour
        return pct(v, sign=False), "" if v is None else str(v)
    if kind == "int":
        return ("—" if v is None else f"{int(float(v)):,}"), "" if v is None else str(v)
    if kind == "html":
        return str(v if v is not None else ""), ""
    return e("—" if v in (None, "") else v), ""


def table(tid: str, columns: list, rows: list, totals: list | None = None, name: str = "atip", empty: str = "",
          drawer=None, caption: str = "", tools: bool = True) -> str:
    """columns: [(key, label, kind)], kind text | money | pnl | pct | int | html. drawer(row) -> {"title", "rows":
    [[k, v]], "link"} makes each row open the detail drawer."""
    num = {"money", "pnl", "pct", "rate", "int"}
    head = "".join(f'<th scope="col" class="{"n" if k in num else ""}">{e(l)}</th>' for _, l, k in columns)
    body = []
    for r in rows:
        tds = []
        for key, _, kind in columns:
            h, dv = _cell(kind, r.get(key))
            tds.append(f'<td class="{"n" if kind in num else ""}"{f" data-v={e(dv)}" if dv else ""}>{h}</td>')
        d = f' data-drawer="{e(json.dumps(drawer(r), default=str))}"' if drawer else ""
        body.append(f"<tr{d}>{''.join(tds)}</tr>")
    for t in totals or []:
        tds = []
        for key, _, kind in columns:
            h, dv = _cell(kind, t.get(key))
            tds.append(f'<td class="{"n" if kind in num else ""}">{h}</td>')
        body.append(f'<tr class="ax-total">{"".join(tds)}</tr>')
    if not rows:
        body.insert(0, f'<tr><td colspan="{len(columns)}"><div class="ax-empty">{e(empty or "Nothing here yet.")}</div></td></tr>')
    bar = ""
    if tools and rows:
        bar = (f'<div class="ax-tablebar"><input class="ax-input" type="search" placeholder="Filter rows" '
               f'aria-label="Filter the table" data-ax-filter="{e(tid)}"><span class="ax-small ax-muted" '
               f'data-ax-count="{e(tid)}">{len(rows)} rows</span><span class="ax-spacer" style="flex:1"></span>'
               f'<button class="ax-btn sm" type="button" data-ax-cols="{e(tid)}" aria-haspopup="true">Columns</button>'
               f'<button class="ax-btn sm" type="button" data-ax-csv="{e(tid)}" data-name="{e(name)}">Export CSV</button></div>')
    cap = f'<caption class="ax-muted ax-small" style="text-align:left;padding:6px 10px">{e(caption)}</caption>' if caption else ""
    return (f'{bar}<div class="ax-tablewrap"><table class="ax-table" id="{e(tid)}">{cap}<thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def kv(pairs: list) -> str:
    return '<dl class="ax-kv">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


# -- charts ---------------------------------------------------------------------------------------

def line_chart(points: list, series: list, title: str, w: int = 640, h: int = 230) -> str:
    """points: [{"label", key: value...}]; series: [(key, name, "--ax-series-1")]. One y-axis, a legend, the last
    value labelled on each line, a crosshair tooltip (atip.js)."""
    if not points:
        return '<div class="ax-empty">No closed cycle yet: the curve starts with the first sale.</div>'
    pad, padr, top, bot = 58, 92, 12, 24
    vals = [float(p[k]) for p in points for k, *_ in series if p.get(k) is not None]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    lo, hi = lo - span * .08, hi + span * .08
    n = max(1, len(points) - 1)
    X = lambda i: pad + i * (w - pad - padr) / n
    Y = lambda v: top + (hi - float(v)) / (hi - lo) * (h - top - bot)
    grid = []
    for j in range(5):
        v = lo + (hi - lo) * j / 4
        grid.append(f'<line class="ax-gridline" x1="{pad}" x2="{w - padr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                    f'<text class="ax-axis" x="{pad - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{e(compact(v))}</text>')
    paths, labels = [], []
    ends = []
    for i, (k, name, var) in enumerate(series):
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(j):.1f},{Y(p[k]):.1f}" for j, p in enumerate(points) if p.get(k) is not None)
        cls = "ax-s1" if var.endswith("1") else "ax-s2"
        paths.append(f'<path class="{cls}" d="{d}" stroke-linejoin="round" stroke-linecap="round"/>')
        last = points[-1].get(k)
        if last is not None:
            ends.append([Y(last), name, last, cls])
    ends.sort()
    for j in range(1, len(ends)):                       # keep end labels apart
        if ends[j][0] - ends[j - 1][0] < 13:
            ends[j][0] = ends[j - 1][0] + 13
    for y, name, last, cls in ends:
        labels.append(f'<text class="ax-lab" x="{w - padr + 6}" y="{y + 4:.1f}">{e(name)} {e(compact(last))}</text>')
    xl = [f'<text class="ax-axis" x="{X(0):.1f}" y="{h - 6}" text-anchor="start">{e(points[0]["label"])}</text>']
    if len(points) > 1:
        xl.append(f'<text class="ax-axis" x="{X(len(points) - 1):.1f}" y="{h - 6}" text-anchor="end">{e(points[-1]["label"])}</text>')
    legend = '<div class="ax-legend">' + "".join(
        f'<span><i style="background:var({var})"></i>{e(name)}</span>' for _, name, var in series) + "</div>"
    cfg = {"w": w, "pad": pad, "padr": padr, "points": points,
           "series": [{"key": k, "name": n, "color": v} for k, n, v in series]}
    return (f'{legend}<div class="ax-chart" data-ax-line="{e(json.dumps(cfg, default=str))}">'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(title)}"><title>{e(title)}</title>{"".join(grid)}'
            f'<line class="ax-cross" x1="0" x2="0" y1="{top}" y2="{h - bot}" style="display:none"/>'
            f'{"".join(paths)}{"".join(labels)}{"".join(xl)}</svg><div class="ax-tip" role="status"></div></div>')


def waterfall(steps: list, title: str, w: int = 640, h: int = 230) -> str:
    """Gross P&L, each charge and the tax taken off, actual P&L: one bar per step, totals from zero."""
    if not steps or all(abs(float(s["value"] or 0)) < 0.005 for s in steps):
        return '<div class="ax-empty">No closed cycle yet.</div>'
    pad, top, bot = 58, 16, 40
    bars, run = [], 0.0
    for s in steps:
        v = float(s["value"] or 0)
        if s["kind"] == "total":
            bars.append((s, 0.0, v)); run = v
        else:
            bars.append((s, run, run + v)); run += v
    vals = [0.0] + [x for _, a, b in bars for x in (a, b)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1:
        hi = lo + 1
    Y = lambda v: top + (hi - v) / (hi - lo) * (h - top - bot)
    bw = (w - pad - 8) / len(bars)
    g = [f'<line class="ax-gridline" x1="{pad}" x2="{w - 8}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" style="stroke:var(--ax-muted)"/>',
         f'<text class="ax-axis" x="{pad - 6}" y="{Y(hi) + 4:.1f}" text-anchor="end">{e(compact(hi))}</text>',
         f'<text class="ax-axis" x="{pad - 6}" y="{Y(0) + 4:.1f}" text-anchor="end">₹0</text>']
    for i, (s, a, b) in enumerate(bars):
        t, bt = Y(max(a, b)), Y(min(a, b))
        cls = "ax-f1" if s["kind"] == "total" else "ax-f2"
        x = pad + i * bw + 3
        tip = f'{s["label"]}: {inr(s["value"], True)}'
        lab = f'<text class="ax-lab" x="{x + (bw - 6) / 2:.1f}" y="{t - 4:.1f}" text-anchor="middle">{e(compact(s["value"]))}</text>' \
            if s["kind"] == "total" else ""
        g.append(f'<g data-tip="{e(tip)}"><rect class="{cls}" x="{x:.1f}" y="{t:.1f}" width="{max(2, bw - 6):.1f}" '
                 f'height="{max(1.5, bt - t):.1f}" rx="3"/>{lab}<text class="ax-axis" x="{x + (bw - 6) / 2:.1f}" '
                 f'y="{h - 22}" text-anchor="middle">{e(s["label"][:11])}</text></g>')
    legend = ('<div class="ax-legend"><span><i class="box" style="background:var(--ax-series-1)"></i>Gross and actual P&amp;L'
              '</span><span><i class="box" style="background:var(--ax-series-2)"></i>Charge or tax taken off</span></div>')
    return (f'{legend}<div class="ax-chart" data-ax-bars><svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(title)}">'
            f'<title>{e(title)}</title>{"".join(g)}</svg><div class="ax-tip" role="status"></div></div>')


def staircase(rows: list, title: str, w: int = 640, h: int = 230) -> str:
    """Each cycle's start amount as a step line (the compounding), the floor it had to clear as a dashed step, with a
    crosshair tooltip. A line, not bars: the values sit far from zero, and bars must start at zero."""
    if not rows:
        return '<div class="ax-empty">The staircase starts once a cycle closes.</div>'
    pts = [{"label": f'Cycle {r["n"]}', "start": r["start"], "floor": r["floor"]} for r in rows]
    if len(pts) == 1:
        pts.append({**pts[0], "label": pts[0]["label"] + " (now)"})
    return line_chart(pts, [("start", "Start amount", "--ax-series-1"), ("floor", "Floor", "--ax-series-2")], title, w, h) \
        .replace('<path class="ax-s2" d="M', '<path class="ax-s2" stroke-dasharray="5 3" d="M')


def spark(values: list, label: str) -> str:
    vals = [float(v) for v in values or [] if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    w, h = 120, 30
    d = " ".join(f"{'M' if i == 0 else 'L'}{i * w / (len(vals) - 1):.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}"
                 for i, v in enumerate(vals))
    return f'<svg class="ax-spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="{e(label)}"><path d="{d}"/></svg>'


def progress(p: dict) -> str:
    """An open cycle between its stop (left) and its target (right)."""
    if not p or p.get("pct") is None:
        return ""
    return (f'<div class="ax-progress" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{p["pct"]:.0f}" '
            f'aria-label="Cycle progress: {p["move_pct"]:+.2f}% from entry, stop at {p["stop_pct"]:g}%, target +{p["target_pct"]:g}%">'
            f'<span style="width:{p["pct"]:.1f}%"></span></div><div class="ax-progress-l"><span>stop {p["stop_pct"]:g}%</span>'
            f'<span>now {p["move_pct"]:+.2f}%</span><span>target +{p["target_pct"]:g}%</span></div>')
