"""
Bulk / block deals are stored for the session they belong to (2026-10-26).

NSE's bulk.csv and block.csv hold only the latest session. download_bulk_block_deals ignored the date it was
asked for, so the catch-up that recovered 15, 16, 19, 21 and 22 Oct stored 26 Oct's deals under each of them,
and block.csv's "NO RECORDS" line became a symbol called NAN.
"""

from datetime import date


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, files):
        self.files = files

    def get(self, url, timeout=None):
        return _Resp(self.files["bulk" if url.endswith("bulk.csv") else "block"])


BULK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
        "26-OCT-2026,APOLLO,Apollo Micro Systems Ltd,QE SECURITIES LLP,BUY,2465951,410.93,-\n"
        "26-OCT-2026,XTRANET,Xtranet Technologies Ltd,NEO APEX,SELL,93000,251.73,-\n")
NO_RECORDS = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n"


def test_the_files_session_is_stored_and_nothing_else():
    from data.bhavcopy import download_bulk_block_deals
    s = _Session({"bulk": BULK, "block": NO_RECORDS})
    got = download_bulk_block_deals(date(2026, 10, 26), s)
    assert set(got) == {"APOLLO", "XTRANET"}                       # no NAN from "NO RECORDS"
    assert round(got["APOLLO"]["net_value_cr"], 2) == round(2465951 * 410.93 / 1e7, 2)
    assert got["XTRANET"]["net_value_cr"] < 0
    assert download_bulk_block_deals(date(2026, 10, 22), s) == {}  # a past session gets nothing


def test_a_file_without_a_date_column_is_read_as_before():
    from data.bhavcopy import download_bulk_block_deals
    undated = "Symbol,Buy/Sell,Quantity Traded,Trade Price\nACME,BUY,1000,100\n"
    got = download_bulk_block_deals(date(2026, 10, 26), _Session({"bulk": undated, "block": NO_RECORDS}))
    assert set(got) == {"ACME"}
