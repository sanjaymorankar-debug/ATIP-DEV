"""Hard-coded colour literals (hex and rgb()/rgba()) in the dashboard source, 4 Nov 2026 verification sweep.
Scope: dashboard/**/*.py, dashboard/static/**/*.js, dashboard/static/**/*.css in the verify-2026-11-04 worktree.
atip.css: custom-property declarations inside :root / [data-theme] blocks (the token definitions) are excluded and
counted separately; every other colour literal in atip.css is reported as a component-rule raw colour.
usage: python3 colours.py  -> writes colours.json next to this file and prints a summary."""
import json, os, re, glob

ROOT = "/Users/agtci/ATIP/.claude/worktrees/verify-2026-11-04/dashboard"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "colours.json")
HEX = re.compile(r"(?<![&\w])#([0-9a-fA-F]{3,8})\b")
RGB = re.compile(r"\brgba?\(\s*[\d.]+%?\s*[, ]\s*[\d.]+%?\s*[, ]\s*[\d.]+%?(?:\s*[,/]\s*[\d.]+%?)?\s*\)")


def scan_text(text):
    hexes = [m for m in HEX.finditer(text) if len(m.group(1)) in (3, 4, 6, 8)]
    rgbs = list(RGB.finditer(text))
    return hexes, rgbs


def token_spans(css):
    """(start, end) spans of custom-property declarations inside :root / [data-theme] rule blocks."""
    spans, stack, i, sel_start = [], [], 0, 0
    while i < len(css):
        c = css[i]
        if css.startswith("/*", i):
            j = css.find("*/", i + 2); i = (j + 2) if j >= 0 else len(css); sel_start = i; continue
        if c == "{":
            stack.append((css[sel_start:i].strip(), i + 1)); sel_start = i + 1
        elif c == "}":
            if stack:
                sel, body_start = stack.pop()
                if re.search(r":root|\[data-theme", sel):
                    body = css[body_start:i]
                    for m in re.finditer(r"--[\w-]+\s*:[^;{}]*;?", body):
                        spans.append((body_start + m.start(), body_start + m.end()))
            sel_start = i + 1
        elif c == ";" and not stack:
            sel_start = i + 1
        i += 1
    return spans


files = sorted(set(glob.glob(ROOT + "/**/*.py", recursive=True)) | set(glob.glob(ROOT + "/static/**/*.js", recursive=True)) | set(glob.glob(ROOT + "/static/**/*.css", recursive=True)))
files = [f for f in files if "__pycache__" not in f]
per = {}
atip = {}
for f in files:
    text = open(f, encoding="utf-8", errors="replace").read()
    hexes, rgbs = scan_text(text)
    rel = os.path.relpath(f, os.path.dirname(ROOT))
    if rel.endswith("static/atip/atip.css"):
        spans = token_spans(text)
        inside = lambda pos: any(a <= pos < b for a, b in spans)
        th = [m for m in hexes if inside(m.start())]; tr = [m for m in rgbs if inside(m.start())]
        ch = [m for m in hexes if not inside(m.start())]; cr = [m for m in rgbs if not inside(m.start())]
        comments = [(c.start(), c.end()) for c in re.finditer(r"/\*.*?\*/", text, re.S)]
        in_comment = lambda pos: any(a <= pos < b for a, b in comments)
        def kind(m):
            if in_comment(m.start()): return "comment"
            ls = text.rfind("\n", 0, m.start()) + 1
            before = text[ls:m.start()]
            if re.search(r'\[style\*?=\s*"[^"]*$', before): return "attr-selector (legacy bridge matcher)"
            return "declaration (applied colour)"
        kinds = {}
        for m in ch + cr:
            k = kind(m); kinds[k] = kinds.get(k, 0) + 1
        applied = [m for m in ch + cr if kind(m).startswith("declaration")]
        lines = {}
        for m in applied:
            ln = text.count("\n", 0, m.start()) + 1
            lines.setdefault(ln, []).append(m.group(0))
        atip = {"tokenHex": len(th), "tokenRgb": len(tr), "componentHex": len(ch), "componentRgb": len(cr),
                "componentKinds": kinds, "appliedCount": len(applied), "componentLines": [{"line": ln, "colours": v, "src": text.splitlines()[ln - 1].strip()[:160]} for ln, v in sorted(lines.items())]}
        hexes, rgbs = ch, cr
    vals = {}
    for m in hexes + rgbs:
        k = m.group(0).lower(); vals[k] = vals.get(k, 0) + 1
    per[rel] = {"hex": len(hexes), "rgb": len(rgbs), "total": len(hexes) + len(rgbs), "vendor": "/vendor/" in rel,
                "top": sorted(vals.items(), key=lambda kv: -kv[1])[:8]}

ranked = sorted(per.items(), key=lambda kv: -kv[1]["total"])
summary = {
    "scope": ROOT, "filesScanned": len(files),
    "filesWithColours": sum(1 for v in per.values() if v["total"]),
    "totalLiterals": sum(v["total"] for v in per.values()),
    "totalExcludingVendor": sum(v["total"] for v in per.values() if not v["vendor"]),
    "byKind": {k: sum(v["total"] for r, v in per.items() if r.endswith(k) and not v["vendor"]) for k in (".py", ".js", ".css")},
    "top20": [{"file": r, **v} for r, v in ranked[:20]],
    "atipCss": atip,
}
json.dump({"summary": summary, "perFile": per}, open(OUT, "w"), indent=1, ensure_ascii=False)
print(json.dumps({k: summary[k] for k in ("filesScanned", "filesWithColours", "totalLiterals", "totalExcludingVendor", "byKind")}, ensure_ascii=False))
for t in summary["top20"]:
    print(f'{t["total"]:6d}  hex {t["hex"]:5d}  rgb {t["rgb"]:4d}  {t["file"]}{"  (vendor)" if t["vendor"] else ""}')
print("atip.css component colour kinds:", atip.get("componentKinds"))
print("atip.css tokens:", atip.get("tokenHex"), "hex,", atip.get("tokenRgb"), "rgb; component rules:", atip.get("componentHex"), "hex,", atip.get("componentRgb"), "rgb")
print("atip.css applied (declaration) raw colours, by line:")
for l in atip.get("componentLines", [])[:60]:
    print("  L%d %s :: %s" % (l["line"], l["colours"], l["src"][:110]))
