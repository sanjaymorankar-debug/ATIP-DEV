"""
ATIP wealth track (Waves 11-20): investor intelligence on top of the trading platform.

    dna.py          W11  Investor DNA: risk capacity / tolerance / requirement, horizon,
                         behavioural biases, trading personality (ATIP-INV-001)
    assets.py       W12  asset-class registry (supported / excluded classes)
    holdings.py     W12  normalized multi-asset holdings and the wealth summary (ATIP-WLT-001)
    goals.py        W13  goal planning: corpus, contributions, CAGR, shortfall, scenarios (ATIP-GOL-001)
    allocation.py   W14  strategic + tactical asset allocation from DNA, goals, regime (ATIP-AAL-001)
    rebalance.py    W15  drift, risk and cost-aware rebalance recommendations (ATIP-RBL-001)
    perf/           W15.5 performance ledger and attribution: model / executable / actual /
                         benchmark returns (ATIP-PERF-001)
    advisor.py      W16  explainable, evidence-backed advisor (ATIP-AIA-001)
    integrated.py   W17  the investor cycle and Investor / Trader mode (ATIP-INT-001)

Rules for the whole package:
  * ADVISORY ONLY. Nothing here creates an order, a position intent, a risk decision
    or an order rule, and nothing changes execution settings. Trading stays in
    orders/ (W1) and execution/ (W4) behind their own gates.
  * Additive. New tables only (db/schema.py WEALTH_TABLES); existing tables are read.
  * Every stored number carries its methodology version and inputs, so it can be
    traced back to source data.
  * Scope exclusions (brief section 3): NPS, FDs, tax, insurance and mutual funds are
    not implemented. assets.py lists them as EXCLUDED and the API refuses them.
"""
