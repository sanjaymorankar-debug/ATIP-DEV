# W16: AI investment advisor (ATIP-AIA-001) handoff

**Status:** IMPLEMENTED BUT NOT VERIFIED. Build check plus a scratch-copy smoke run of all eight topics (narration off). ChatGPT testing is pending.
**Branch:** `w16-ai-advisor` (on top of W15.5). Not merged, not deployed.

## Design: evidence first, no black box

`wealth/advisor.py` (methodology AIA-1.0) answers from ATIP's own engines and tables. Every statement is a **claim with evidence** (source, field, value, as-of date), so the same data gives the same answer.

| Topic | Built from |
|---|---|
| overview | W12 summary, W11 DNA (band, flags, staleness), W15 rebalance verdict, W13 goals, latest W15.5 report, regime |
| risk | DNA capacity / tolerance / requirement, CMA volatility and bad year vs the stated max loss, concentration, ATIP-flagged holdings |
| goals | Per goal: status, Monte Carlo success, gap, extra monthly / lump sum, required vs assumed return |
| allocation | W14 target and its strongest signals, out-of-band classes |
| performance | The latest stored W15.5 report: model / executable / actual / benchmark and the gaps between them |
| symbol | W5 `ml/assistant.explain_symbol` (scores, factors, ML, strategy decisions), holding weight, sizing room under the single-stock cap |
| scenario | Stress shocks (equity −20 / −30%, gold +10%, rates +1%) on net worth vs the stated maximum loss |
| market | Regime and the ten tactical signals |

- **Routing:** a symbol ATIP scores (an upper-case token), else keywords, else overview; or an explicit `topic`.
- **Suitability:** every recommendation is checked against the Investor DNA.
  - With no DNA, a risk-raising action is replaced by "complete your Investor DNA".
  - For a conservative profile it is marked CAUTION.
  - A stale profile is marked.
- **Confidence:** HIGH / MEDIUM / LOW from evidence coverage and caveats.
- **Audit:** every question and full response goes to `wealth_advice_log`, with narration status / model and user feedback.
- **No trading path:** the advisor cannot place, modify or cancel an order (stated in every response).

## Optional Claude narration (off by default)

- Config: `wealth.advisor_llm_enabled` (false) and `wealth.advisor_llm_model` (default `claude-opus-5`).
- It uses the Anthropic SDK already in `requirements.txt`. Credentials come from the environment (`ANTHROPIC_API_KEY` or an `ant` profile) and are never stored.
- Claude only **rewrites the evidence pack**. The frozen system prompt forbids new numbers or recommendations and is cached with `cache_control`. Effort is low, max_tokens 2,000.
- Server-side refusal fallbacks are enabled (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
- Any error, refusal or missing key returns `narration.status` (OFF / UNAVAILABLE / ERROR / REFUSED / EMPTY), and the deterministic answer stands.
- The narration is always shown **alongside** the evidence, labelled as narration.

Enabling narration sends the evidence pack to Anthropic's API: holdings values, goals and profile scores, but no account credentials. That is the owner's decision.

## Interfaces

- API: `POST /api/wealth/advisor/ask`, `GET /advisor/topics`, `GET /advisor/history`, `GET /advisor/{id}`, `POST /advisor/{id}/feedback`.
- Table: `wealth_advice_log`.
- Page: tab "Advisor", with suggested questions, evidence tags (hover to see the value), suitability pills, caveats and helpful / not helpful feedback.

## Known limitations

- Keyword routing is deliberately simple and transparent. An ambiguous question falls back to the overview.
- Scenario shocks are fixed illustrative assumptions (listed in `SHOCKS`).
- Narration was not exercised in the smoke run: no key in the scratch environment, and it is off by default.
