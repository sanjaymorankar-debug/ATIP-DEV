"""
W39 equity research: institutional-style valuation, ratings and per-stock research reports.

    research/valuation.py   pure maths: cost of equity, two-stage DCF with scenarios and a
                            sensitivity grid, justified P/B, peer / own-history multiples,
                            blended fair value, 12-month target and rating bands
    research/report.py      gathers ATIP's stored data for one symbol, runs the valuation,
                            writes the report (thesis, risks, catalysts, peers, ownership,
                            SEBI-style disclosures) and tracks whether targets were met
"""
