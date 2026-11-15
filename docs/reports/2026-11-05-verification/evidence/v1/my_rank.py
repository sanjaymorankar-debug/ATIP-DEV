# My own re-ranking (5 Nov QA): plain SQL over ai_scores + prices_daily, no pandas, no orders/ import.
import sqlite3, json, sys, os
db = sys.argv[1]
c = sqlite3.connect(db)
for d in ["2026-11-04", "2026-10-30", "2026-10-27", "2026-10-14"]:
    buy = [r[0] for r in c.execute("SELECT symbol FROM ai_scores WHERE date=? AND `signal`='BUY' AND atip_score IS NOT NULL ORDER BY atip_score DESC", (d,))]
    hold = []
    for sym, atip, cri in c.execute("SELECT symbol, atip_score, cri FROM ai_scores WHERE date=? AND `signal`='HOLD' AND atip_score IS NOT NULL ORDER BY atip_score DESC, cri IS NULL, cri, symbol", (d,)):
        if cri is not None and cri >= 75: continue
        px = c.execute("SELECT close FROM prices_daily WHERE symbol=? AND date=? AND series='EQ'", (sym, d)).fetchone()
        if not px or px[0] < 20: continue
        rows = c.execute("SELECT close*volume FROM prices_daily WHERE symbol=? AND date<=? AND series='EQ' ORDER BY date DESC LIMIT 20", (sym, d)).fetchall()
        if not rows or sum(r[0] for r in rows)/len(rows) < 2e7: continue
        hold.append(sym)
    mine = (buy + [h for h in hold if h not in buy])[:10]
    got = json.load(open(os.path.join(os.environ['TMPDIR'], 'v1', f'v1_{d}.json')))["picks_in_session_order"]
    print(d, "BUY", len(buy), "| match" if mine == got else "| MISMATCH", "\n  mine:", mine, "\n  got: ", got)
