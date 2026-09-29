"""The digest reads a spec table's columns from character positions.

The synthetic page has the geometry of a real TI table (numbers right-aligned under the
MIN / TYP / MAX header words, a unit column, a row-spanning description). The golden test
reads the three rows an AI extraction once misread from the raw text layer of the TPS54332
datasheet; it needs that PDF, which is never committed, and skips without it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from boardmodeler.documents.digest import Word, digest_markdown, read_pdf_rows, rows_from_words


def w(text: str, x0: float, x1: float, yc: float) -> Word:
    return Word(text, x0, x1, yc)


HEADER = [
    w("DESCRIPTION", 59, 113, 700),
    w("TEST", 259, 279, 700),
    w("CONDITIONS", 282, 331, 700),
    w("MIN", 443, 457, 700),
    w("TYP", 471, 486, 700),
    w("MAX", 499, 516, 700),
    w("UNIT", 528, 546, 700),
]

PAGE = [
    *HEADER,
    w("SUPPLY", 59, 85, 690),  # a section title: ignored
    w("VOLTAGE", 92, 129, 690),
    # UVLO: the only number is right-aligned under MAX
    w("Internal", 60, 85, 680),
    w("undervoltage", 88, 134, 680),
    w("lockout", 137, 162, 680),
    w("Rising", 259, 280, 680),
    w("and", 284, 296, 680),
    w("Falling", 299, 322, 680),
    w("3.5", 505, 516, 680),
    w("V", 534, 539, 680),
    # a two-sided window
    w("Enable", 59, 83, 660),
    w("threshold", 86, 118, 660),
    w("1.25", 472, 486, 660),
    w("1.35", 501, 516, 660),
    w("V", 534, 539, 660),
    # min and typ, no max
    w("Current", 59, 85, 640),
    w("limit", 88, 102, 640),
    w("VIN", 259, 271, 640),
    w("=", 275, 279, 640),
    w("12", 282, 290, 640),
    w("V", 292, 298, 640),
    w("4.2", 446, 457, 640),
    w("6.5", 476, 486, 640),
    w("A", 534, 539, 640),
    # a row-spanning description between two number rows
    w("BOOT", 259, 281, 620),
    w("=", 297, 301, 620),
    w("3", 304, 308, 620),
    w("115", 475, 486, 620),
    w("200", 503, 516, 620),
    w("On", 59, 69, 613),
    w("resistance", 72, 108, 613),
    w("mΩ", 531, 543, 613),
    w("BOOT", 259, 281, 606),
    w("=", 297, 301, 606),
    w("6", 304, 308, 606),
    w("80", 478, 486, 606),
    w("150", 504, 516, 606),
    # a footnote and page furniture: ignored
    w("(1)", 57, 66, 590),
    w("Specified", 78, 110, 590),
    w("Copyright", 404, 433, 34),
]


def test_a_number_takes_the_column_it_is_aligned_under() -> None:
    rows = {r.label + "|" + r.conditions: r for r in rows_from_words(PAGE, 1)}
    uvlo = rows["Internal undervoltage lockout|Rising and Falling"]
    assert (uvlo.min, uvlo.typ, uvlo.max, uvlo.unit) == (None, None, "3.5", "V")
    enable = (
        rows["Enable threshold|Rising and Falling"]
        if "Enable threshold|Rising and Falling" in rows
        else rows["Enable threshold|"]
    )
    assert (enable.min, enable.typ, enable.max) == (None, "1.25", "1.35")
    limit = rows["Current limit|VIN = 12 V"]
    assert (limit.min, limit.typ, limit.max, limit.unit) == ("4.2", "6.5", None, "A")


def test_a_row_spanning_description_and_its_unit_reach_both_rows() -> None:
    on = [r for r in rows_from_words(PAGE, 1) if r.label == "On resistance"]
    assert [(r.typ, r.max, r.unit) for r in on] == [("115", "200", "mΩ"), ("80", "150", "mΩ")]


def test_titles_footnotes_and_page_furniture_make_no_rows() -> None:
    labels = [r.label for r in rows_from_words(PAGE, 1)]
    assert len(labels) == 5
    assert not any(
        "SUPPLY" in label or "Specified" in label or "Copyright" in label for label in labels
    )


def test_a_page_without_a_min_typ_max_header_yields_nothing() -> None:
    assert rows_from_words([w("Just", 60, 80, 500), w("prose", 84, 110, 500)], 3) == []


def test_the_markdown_has_one_explicit_column_per_value() -> None:
    text = digest_markdown(rows_from_words(PAGE, 6))
    assert "### Page 6" in text
    assert "| Parameter | Conditions | MIN | TYP | MAX | Unit |" in text
    assert "|  |  | 3.5 | V |" in text.replace(
        "Internal undervoltage lockout | Rising and Falling ", ""
    )


DATASHEETS = os.environ.get("SPICE_MAKER_DATASHEET_DIR")


@pytest.mark.skipif(
    not DATASHEETS or not (Path(DATASHEETS) / "tps54332_datasheet.pdf").is_file(),
    reason="set SPICE_MAKER_DATASHEET_DIR to a folder holding tps54332_datasheet.pdf",
)
def test_tps54332_columns_are_read_correctly_from_the_real_pdf() -> None:
    rows = read_pdf_rows(Path(DATASHEETS or ".") / "tps54332_datasheet.pdf", pages=range(5, 6))
    by_label = {}
    for row in rows:
        by_label.setdefault(row.label.split("(")[0].strip(), row)
    expect = {
        "Internal under voltage lockout threshold": (None, None, "3.5"),
        "Enable threshold": (None, "1.25", "1.35"),
        "Current limit threshold": ("4.2", "6.5", None),
        "Pulse-skipping Eco-mode switch current threshold": (None, "160", None),
        "Switch current to COMP transconductance": (None, "12", None),
    }
    for label, (low, typical, high) in expect.items():
        row = by_label[label]
        assert (row.min, row.typ, row.max) == (low, typical, high), label
    on = [r for r in rows if r.label == "On resistance"]
    assert [(r.typ, r.max, r.unit) for r in on] == [("115", "200", "mΩ"), ("80", "150", "mΩ")]
    everything = " ".join(f"{r.label} {r.min} {r.typ} {r.max}" for r in rows)
    assert "0.772" in everything and "0.828" in everything
    assert any(r.typ == "110" and r.max == "135" and r.unit == "ns" for r in rows)
    assert any(r.min == "800" and r.typ == "1000" and r.max == "1200" for r in rows)
