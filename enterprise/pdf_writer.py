"""
A small, dependency-free PDF writer for ATIP's reports (RPT-03, 2 Nov 2026).

ATIP's report PDFs come from a headless Chrome print of the HTML report when a Chrome / Chromium binary is found
(enterprise/session_reports.py pdf()); this module is the engine that always works -- on the view-only web server,
in a container, in the tests. It lays out a report document (title, subtitle, sections of tables and key / value
lines) on A4 landscape pages in Helvetica, repeats a table's header row on every page it runs over, right-aligns
numbers, and puts the disclaimer and "page x of y" in every footer.

The standard PDF fonts have no rupee sign: "₹" is written "Rs". Text outside Windows-1252 becomes "?".
"""

from __future__ import annotations

import re

PAGE_W, PAGE_H = 842.0, 595.0          # A4 landscape, points
MARGIN = 36.0
FONT, BOLD = "F1", "F2"
SUBST = {"→": "->", "−": "-", "—": "-", "–": "-", "✓": "v", "▲": "+", "▼": "-", "●": "*", "⏸": "||", "↻": "~",
         " ": " ", " ": " "}

# Helvetica advance widths (per 1000 em) for ASCII; others use 556.
_W = dict(zip(range(32, 127), [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
    556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
    722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556,
    556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334,
    260, 334, 584]))


def clean(s) -> str:
    s = "" if s is None else str(s)
    s = re.sub(r"₹(?=[0-9])", "Rs ", s).replace("₹", "Rs")
    for k, v in SUBST.items():
        s = s.replace(k, v)
    return s.encode("cp1252", "replace").decode("cp1252")


def width(s: str, size: float, bold: bool = False) -> float:
    w = sum(_W.get(ord(c), 556) for c in s) * size / 1000
    return w * (1.05 if bold else 1.0)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fit(s: str, w: float, size: float, bold=False) -> str:
    if width(s, size, bold) <= w:
        return s
    while s and width(s + "...", size, bold) > w:
        s = s[:-1]
    return s + "..."


class _Doc:
    def __init__(self, footer: str):
        self.pages, self.ops, self.y = [], None, 0.0
        self.footer = clean(footer)
        self.new_page()

    def new_page(self):
        self.ops = []
        self.pages.append(self.ops)
        self.y = PAGE_H - MARGIN

    def need(self, h: float) -> bool:
        if self.y - h < MARGIN + 24:
            self.new_page()
            return True
        return False

    def text(self, x, y, s, size=9.0, bold=False, gray=0.0):
        self.ops.append(f"BT {gray:.2f} g /{BOLD if bold else FONT} {size:.1f} Tf {x:.2f} {y:.2f} Td "
                        f"({_esc(clean(s))}) Tj ET")

    def line(self, x1, y1, x2, y2, gray=0.75, w=0.5):
        self.ops.append(f"{gray:.2f} G {w:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def rect(self, x, y, w, h, gray=0.93):
        self.ops.append(f"{gray:.2f} g {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")


def _table(d: _Doc, sec: dict, size=7.6):
    cols = sec.get("columns") or []
    rows = list(sec.get("rows") or [])
    rows += [{**t, "_total": True} for t in (sec.get("totals") or ([sec["total"]] if sec.get("total") else []))]
    if not cols:
        return
    avail = PAGE_W - 2 * MARGIN
    want = []
    for c in cols:
        cells = [clean(c["label"])] + [clean(r.get("_fmt", {}).get(c["key"], r.get(c["key"], ""))) for r in rows[:200]]
        want.append(min(220.0, max(width(x, size, True) for x in cells) + 8))
    scale = min(1.0, avail / sum(want)) if sum(want) else 1.0
    ws = [w * scale for w in want]
    rh = size + 6

    def header():
        d.rect(MARGIN, d.y - rh + 2, avail, rh, 0.90)
        x = MARGIN
        for c, w in zip(cols, ws):
            lab = _fit(clean(c["label"]), w - 6, size, True)
            tx = x + w - 3 - width(lab, size, True) if c.get("num") else x + 3
            d.text(tx, d.y - rh + 6, lab, size, True)
            x += w
        d.y -= rh

    d.need(rh * 3)
    header()
    for r in rows:
        if d.need(rh):
            header()
        x = MARGIN
        if r.get("_total"):
            d.rect(MARGIN, d.y - rh + 2, avail, rh, 0.95)
        for c, w in zip(cols, ws):
            v = clean(r.get("_fmt", {}).get(c["key"], r.get(c["key"], "")))
            v = _fit(v, w - 6, size, bool(r.get("_total")))
            tx = x + w - 3 - width(v, size, bool(r.get("_total"))) if c.get("num") else x + 3
            d.text(tx, d.y - rh + 6, v, size, bool(r.get("_total")))
            x += w
        d.line(MARGIN, d.y - rh + 2, MARGIN + avail, d.y - rh + 2, 0.85, 0.3)
        d.y -= rh
    d.y -= 8


def _wrap(s: str, w: float, size: float) -> list:
    out, cur = [], ""
    for word in clean(s).split():
        t = (cur + " " + word).strip()
        if width(t, size) > w and cur:
            out.append(cur)
            cur = word
        else:
            cur = t
    return out + ([cur] if cur else [])


def render(doc: dict) -> bytes:
    """A report document (enterprise/session_reports.py build) as PDF bytes."""
    d = _Doc(doc.get("disclaimer", ""))
    d.text(MARGIN, d.y - 16, doc.get("title", "ATIP report"), 16, True)
    d.y -= 24
    for line in _wrap(doc.get("subtitle", ""), PAGE_W - 2 * MARGIN, 9):
        d.text(MARGIN, d.y - 10, line, 9, gray=0.35)
        d.y -= 13
    d.y -= 6
    for sec in doc.get("sections") or []:
        d.need(40)
        d.text(MARGIN, d.y - 12, sec.get("heading", ""), 11.5, True)
        d.y -= 18
        for line in _wrap(sec.get("note") or "", PAGE_W - 2 * MARGIN, 8):
            d.need(11)
            d.text(MARGIN, d.y - 9, line, 8, gray=0.35)
            d.y -= 11
        if sec.get("note"):
            d.y -= 5                                    # clear of the table header below
        if sec.get("kv"):
            for k, v in sec["kv"]:
                d.need(12)
                d.text(MARGIN, d.y - 9, k, 8.5, gray=0.25)
                d.text(MARGIN + 260, d.y - 9, v, 8.5, True)
                d.y -= 12
            d.y -= 6
        if sec.get("columns"):
            _table(d, sec)
    n = len(d.pages)
    for i, ops in enumerate(d.pages, 1):
        foot = _fit(d.footer, PAGE_W - 2 * MARGIN - 90, 7)
        ops.append(f"BT 0.40 g /{FONT} 7.0 Tf {MARGIN:.2f} 20.00 Td ({_esc(foot)}) Tj ET")
        pg = f"Page {i} of {n}"
        ops.append(f"BT 0.40 g /{FONT} 7.0 Tf {PAGE_W - MARGIN - width(pg, 7):.2f} 20.00 Td ({_esc(pg)}) Tj ET")
    return _assemble(d.pages, clean(doc.get("title", "ATIP report")))


def _assemble(pages: list, title: str) -> bytes:
    objs = []                                   # index i -> object i+1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    cat = add(b"")                              # placeholders, filled below
    pages_id = add(b"")
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for ops in pages:
        stream = "\n".join(ops).encode("cp1252", "replace")
        cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                         f"/Resources << /Font << /{FONT} {f1} 0 R /{BOLD} {f2} 0 R >> >> /Contents {cid} 0 R >>")
                        .encode()))
    objs[cat - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objs[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    info = add(f"<< /Title ({_esc(title)}) /Producer (ATIP) >>".encode("cp1252", "replace"))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {cat} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
