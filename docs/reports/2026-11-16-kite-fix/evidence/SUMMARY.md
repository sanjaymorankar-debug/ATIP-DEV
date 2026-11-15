# Replay engine and Kite retest study — 16 Nov 2026 (run 00:28–00:30 IST)

Branch claude/kite-formula-fix-2026-11-16. Data: atip_data/backups/atip-scheduled-20261115-191511.db, read-only
(mode=ro&immutable=1). No broker calls, atip.db untouched.

## Commands

    DB=atip_data/backups/atip-scheduled-20261115-191511.db; OUT=.ux_build/kite-fix-2026-11-16/research
    python -m research.session_ab_replay --db $DB --out $OUT all --fills pack,realistic --start 2026-07-28 --mode daily --split 2026-09-25
    python -m research.session_ab_replay --db $DB --out $OUT population --fills pack,realistic --start 2026-09-25 --mode intraday --split 2026-10-21
    python -m research.kite_retest_study --db $DB --out $OUT --fills pack,realistic
    python -m pytest -o addopts="" -q tests/test_session_ab_replay.py tests/test_kite_retest_study.py   # 48 passed

CSV suffix _pack = 15 Nov fills (5 bps every exit); _realistic = 15 bps on stop/trail exits (owner item 17).

## Reproduction (pack fills) — identical to the 15 Nov pack

- Calibration 34/34, corr 0.968, sim 4,069.89 vs actual 4,800.55
- What-if X1 3,064 (45.5%), X2 2,683, X6 6,679, X7 6,922, X8 11,957, X9 14,147
- Population daily: 470 candidates, E0 446, X1 31.6% / -0.895%, 14/14 exits negative
- Kite: 4,705 setups, fill 56%, K1 2,349 / 42.3% / +0.67% / PF 1.23 / last 12 m -0.08%; K1 b50>=50 1,685 / +0.97%
  (last 12 m 177, +1.14%); K4 gated +0.62, K2 gated +0.34, K5 -0.39, K6 +0.08
- Breadth 13 Nov: b20 80.7, b50 56.8 (max difference 0.0 over 1,731 days vs the pack)

## Calibration before / after (KGF-17)

| Setting | Reasons | Corr | Σ sim (actual 4,800.55) | Stop-exit error median | BHEL (actual 408.20) |
|---|---|---|---|---|---|
| pack 5 bps, gap at open | 34/34 | 0.968 | 4,069.89 | +4.1 bps | 410.57 |
| realistic 15 bps on stop/trail | 34/34 | 0.968 | 3,230.39 | -5.9 bps | 410.16 |
| 10 bps | 34/34 | 0.968 | 3,650.12 | -0.9 bps | — |
| 20 bps | 34/34 | 0.969 | 2,810.60 | -10.9 bps | — |
| realistic, without the 8 Nov Muhurat bars | 34/34 | 0.992 | 4,101.76 | — | — |

The gap rule (fill at the open when a bar opens through the stop) was already in the pack engine; it is now an explicit
flag. No stop gapped overnight in the 34 cycles. BHEL's 09:15 bar opened at 415.90, above the 410.78 stop, and fell to
400.75: the 408.20 fill happened inside one bar, which no bar-level fill can see. 10 bps fits the real stop fills best;
15 bps stays the default (the owner set 10–20).

## Realistic fills elsewhere

What-if X1 3,064 -> 2,199, X6 6,679 -> 6,034, X7 6,922 -> 6,302, X8 11,957 -> 11,530, X9 14,147 -> 13,948 (order
unchanged) | Population E0 X1 -0.895 -> -0.991% (all 14 still negative) | K1 +0.62% (last 12 m -0.14%), K1 b50>=50
+0.92% (last 12 m +1.10%), K4 gated +0.52%, K2 gated +0.30%, K5 -0.45%, K6 +0.03%.
