"""Page relevance: which datasheet pages extraction reads, and the reason for each.

Offline and deterministic: every page is plain text, and OCR is a test double.
"""

from __future__ import annotations

from boardmodeler.documents.ocr import OcrFailed
from boardmodeler.documents.relevance import (
    numeric_unit_tokens,
    score_page,
    select_relevant_pages,
)

SYNTHETIC_SHEET = [
    "SYNTH123 4.5-V to 60-V step-down converter (synthetic test document)",
    "Table of Contents\n1 Features\n2 Description",
    "5 PIN CONFIGURATION AND FUNCTIONS\nPIN NAME NO. I/O DESCRIPTION\nVIN 1 I Supply\nGND 2 G",
    "7.5 ELECTRICAL CHARACTERISTICS\nVIN operating 4.5 V 60 V\nShutdown current 2 µA\n"
    "Switching frequency 1 MHz\nReference 0.8 V",
    "The device is used in many kinds of industrial systems.",
    "12 REVISION HISTORY\nChanges from Revision A to Revision B",
    "MECHANICAL DATA\nD (R-PDSO-G8) PLASTIC SMALL OUTLINE\n4.90 mm 3.91 mm 1.27 mm",
    "PACKAGE OPTION ADDENDUM\nOrderable Device Status Package Type Pins Op Temp",
    "",
]


def select(pages, **kwargs):
    return select_relevant_pages("DOC_1", list(enumerate(pages)), **kwargs)


def test_numeric_unit_tokens_count_electrical_units_and_not_lengths() -> None:
    text = "0.8 V, 1 MHz, 2 µA, 25°C, 10 mA, 0.5 V/µs, 3 kΩ; package 4.90 mm, 8 SOIC"
    tokens = numeric_unit_tokens(text)
    assert tokens == ["0.8 V", "1 MHz", "2 µA", "25°C", "10 mA", "0.5 V/µs", "3 kΩ"]
    assert numeric_unit_tokens("Figure 3 shows the layout of the board") == []


def test_spec_and_pin_pages_are_kept_and_manufacturing_pages_skipped_with_reasons() -> None:
    selection = select(SYNTHETIC_SHEET, ocr_unavailable="tesseract_not_found: not on PATH")

    kept = {page.pdf_page: page for page in selection.selected}
    assert sorted(kept) == [0, 1, 2, 3]
    assert "keep:part_identity_page" in kept[0].signals
    assert "keep:part_identity_page" in kept[1].signals
    assert "keep:pin_table" in kept[2].signals
    assert "+electrical_characteristics" in kept[3].signals
    assert "numeric_unit_tokens=5" in kept[3].signals

    skipped = {page.pdf_page: page.reason for page in selection.skipped}
    assert sorted(skipped) == [4, 5, 6, 7]
    assert skipped[4].startswith("no electrical-spec signal")
    assert "revision_history" in skipped[5]
    assert "mechanical_data" in skipped[6]
    assert "package_option_addendum" in skipped[7]

    # The blank page is neither sent nor silently dropped: it is a named gap.
    assert [(gap.pdf_page, gap.code) for gap in selection.gaps] == [(8, "no_text_ocr_unavailable")]
    assert selection.gaps[0].reason.startswith("page 8: no text layer; OCR unavailable")
    # Every page is accounted for exactly once.
    accounted = sorted(
        [page.pdf_page for page in selection.selected]
        + [page.pdf_page for page in selection.skipped]
        + [gap.pdf_page for gap in selection.gaps]
    )
    assert accounted == list(range(len(SYNTHETIC_SHEET)))


def test_first_two_pages_and_pin_tables_are_kept_even_when_they_score_negative() -> None:
    pages = [
        "REVISION HISTORY\nno numbers here",
        "ORDERING INFORMATION\nnothing electrical",
        "x",
        "IMPORTANT NOTICE\nTERMINAL FUNCTIONS\nPIN 1 VIN",
    ]
    selection = select(pages)
    assert selection.selected_pages("DOC_1") == [0, 1, 3]
    assert [page.pdf_page for page in selection.skipped] == [2]


def test_a_negative_word_inside_prose_does_not_cut_a_spec_page() -> None:
    text = "ELECTRICAL CHARACTERISTICS\nsee the revision history for changes\nVREF 0.8 V"
    score, signals, _ = score_page(text)
    assert "-revision_history" not in signals
    assert score > 0


class FakeOcr:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pages: list[int] = []

    def __call__(self, pdf_page: int) -> str:
        self.pages.append(pdf_page)
        return self.text


def test_scanned_page_is_read_by_ocr_and_scored_on_the_recognized_text() -> None:
    ocr = FakeOcr("ELECTRICAL CHARACTERISTICS\nVIN 4.5 V 60 V")
    selection = select(["identity", "toc", "   "], ocr_page=ocr)
    assert ocr.pages == [2]
    page = {page.pdf_page: page for page in selection.selected}[2]
    assert page.text_source == "ocr"
    assert selection.ocr_text == {("DOC_1", 2): "ELECTRICAL CHARACTERISTICS\nVIN 4.5 V 60 V"}
    assert selection.gaps == ()


def test_ocr_failure_or_empty_ocr_is_an_explicit_gap_never_a_silent_drop() -> None:
    def failing(pdf_page: int) -> str:
        raise OcrFailed("tesseract exited with code 1: bad image")

    failed = select(["a", "b", ""], ocr_page=failing)
    assert [(gap.pdf_page, gap.code) for gap in failed.gaps] == [(2, "no_text_ocr_failed")]
    assert "tesseract exited with code 1" in failed.gaps[0].reason

    empty = select(["a", "b", ""], ocr_page=FakeOcr("  \n"))
    assert [(gap.pdf_page, gap.code) for gap in empty.gaps] == [(2, "no_text_ocr_empty")]
    assert empty.ocr_text == {}


def test_selection_evidence_is_json_ready_and_complete() -> None:
    import json

    evidence = select(SYNTHETIC_SHEET).to_evidence()
    round_trip = json.loads(json.dumps(evidence, ensure_ascii=False))
    assert [row["pdf_page"] for row in round_trip["selected"]] == [0, 1, 2, 3]
    assert all(row["reason"] for row in round_trip["skipped"])
    assert round_trip["gaps"][0]["code"] == "no_text_ocr_unavailable"
