"""Digest of sweep_test.json / sweep_prod.json: one line per page and run (status, errors, horizontal scroll, axe)."""
import json
import sys
from pathlib import Path

D = Path(__file__).parent
tot = {"runs": 0, "ok": 0}
rows = []
for label in ("test", "prod"):
    f = D / f"sweep_{label}.json"
    if not f.exists():
        continue
    o = json.loads(f.read_text())
    for page, runs in o["pages"].items():
        for k, r in runs.items():
            if k == "rawHtml":
                continue
            tot["runs"] += 1
            errs = (r.get("pageErrors") or []) + (r.get("consoleErrors") or []) + (r.get("server5xx") or [])
            hs = r.get("hscroll") or {}
            over = hs.get("overflow") if isinstance(hs, dict) else hs
            axe = r.get("axe")
            av = [f"{v['id']}({v['impact']},{v['n']})" for v in axe] if isinstance(axe, list) else [str(axe)]
            ok = r.get("status") == 200 and not errs and not r.get("fail") and not over
            tot["ok"] += ok
            rows.append(f"{label:4} {page:22} {k:11} {r.get('status')} {'OK ' if ok else 'BAD'} "
                        f"theme={((r.get('theme') or {}).get('dataTheme'))} errs={len(errs)} hscroll={over} "
                        f"axe={','.join(av) or '0'}" + (f" FAIL {r['fail']}" if r.get("fail") else "")
                        + (f" {errs[:2]}" if errs else ""))
print("\n".join(rows))
print(f"\n{tot['ok']} of {tot['runs']} runs clean (HTTP 200, no JS / server error, no horizontal page scroll)")
