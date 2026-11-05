"""VF-34 / VF-35 (found live after the 4 Nov 2026 deploy, fixed 5 Nov 2026). VF-34: in the stop window the top bar's
"⏸ ATIP stopped — resumes 06:00 IST" pushed the alert, theme and profile icons past the screen when the watchlist was
open; the bar now has short words for its run pill and gives way in a fixed order. VF-35: a Home session card split
"P&L +₹1,144.40" after the sign. The browser sweep is .ux_build/fix-2026-11-05/ui/header_cards.js; the defect log is
docs/reports/2026-11-04-verification/VERIFICATION_REPORT.md."""
import re
from pathlib import Path

from tests.test_operating_schedule import at, client, ops  # noqa: F401  (pytest fixtures)

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "atip"


# ── VF-34: the status carries the run pill's short words, and the shell draws them ─────────────────────────────

def test_the_status_has_short_words_for_each_state(ops):
    ops.set_clock(at(21, 30))
    st = ops.status()
    assert (st["status"], st["banner"], st["banner_short"]) == \
        ("STOPPED", "ATIP stopped — resumes 06:00 IST", "Stopped till 06:00")
    ops.set_clock(at(10, 40))                                # 06:00 passed and nothing has resumed yet
    assert (ops.status()["status"], ops.status()["banner_short"]) == ("RESUMING", "Resuming")
    ops.tick(trigger="startup")
    assert (ops.status()["status"], ops.status()["banner_short"]) == ("RUNNING", "Running")


def test_the_top_bar_draws_both_the_sentence_and_the_short_words(ops, client):
    ops.set_clock(at(21, 30))
    html = client.get("/home").text
    pill = re.search(r'<span class="ax-pill ax-run"[^>]*>.*?</span></span>', html).group(0)
    assert 'title="ATIP stopped — resumes 06:00 IST"' in pill
    assert '<span class="ax-runtext">ATIP stopped — resumes 06:00 IST</span>' in pill
    assert '<span class="ax-runshort">Stopped till 06:00</span>' in pill
    assert client.get("/api/v2/shell/state").json()["banner_short"] == "Stopped till 06:00"


def test_atip_js_repaints_the_short_words_and_fits_the_bar():
    js = (STATIC / "atip.js").read_text()
    paint = js.split("function paint(d)")[1].split("\n  }\n")[0]
    assert '<span class="ax-runshort">' in paint and "d.banner_short" in paint and "AX.fitTop()" in paint
    # what gives way, in this order: the "Ctrl K" hint, the feed's long words, the run's long words, the icons, then the
    # breadcrumb's page part
    order = re.search(r"var FIT = (\[.*?\]);", js, re.S).group(1)
    assert re.findall(r'"(ax-fit-\w+)"', order) == \
        ["ax-fit-kbd", "ax-fit-feed", "ax-fit-run", "ax-fit-icons", "ax-fit-crumb"]
    assert "new ResizeObserver(queueFit).observe(topBar)" in js


def test_atip_css_lets_the_pills_give_way_before_the_icons_are_pushed_out():
    css = (STATIC / "atip.css").read_text()
    block = css.split("/* VF-34 (5 Nov 2026)")[1].split("/* VF-35")[0]
    assert ".ax-top .ax-search { flex: none; }" in block                  # it shrank under its label and spilled
    for pill in (".ax-top .ax-run {", ".ax-top .ax-feed {"):
        assert "min-width: 0" in block.split(pill)[1].split("}")[0], pill
    assert "text-overflow: ellipsis" in block
    for cls in ("ax-fit-kbd", "ax-fit-feed", "ax-fit-run", "ax-fit-icons", "ax-fit-crumb"):
        assert f".ax-top.{cls} " in block, cls
    assert ".ax-top.ax-fit-icons .ax-overflow { display: inline-flex; }" in block


# ── VF-35: a session card keeps "P&L +₹1,144.40" and "Wins 1/1" each on one line ────────────────────────────────

def test_a_card_footer_keeps_each_figure_whole():
    css = (STATIC / "atip.css").read_text()
    block = css.split("/* VF-35 (5 Nov 2026)")[1]
    assert ".ax-tc-foot > span { white-space: nowrap; }" in block
    assert "flex-wrap: wrap" in block.split(".ax-tc-foot {")[1].split("}")[0]
