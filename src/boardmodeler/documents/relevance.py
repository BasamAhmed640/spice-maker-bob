"""Electrical-spec relevance of datasheet pages (which pages extraction reads).

A 35-page datasheet carries its electrical rows on a handful of pages; the rest is
package drawings, tape-and-reel tables, revision history and legal text. Sending
every page to the extraction provider costs minutes and produces rows no probe can
test. This module scores each page and says, with reasons, which pages to send.

Honesty rules:

* **Every page is accounted for.** A page is either *selected* (with its score and
  the signals that matched: the provenance of the decision), *skipped* (with a
  written reason), or a *gap* (it could not be read, and why). Nothing is dropped
  silently.
* **Part identity and pins are never cut.** The first two pages and any page with a
  pin table are always selected, whatever their score.
* **No text layer is not "no content".** A page without embedded text is sent to the
  OCR engine when one is available; when OCR is unavailable or reads nothing, the
  page becomes an explicit gap such as ``page 7: no text layer; OCR unavailable``.
* Scoring only *orders and filters* pages. It never produces, edits or infers a
  value; the provider still reads the page text verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "ALWAYS_KEEP_FIRST_PAGES",
    "PageGap",
    "PageScore",
    "PageSelection",
    "SkippedPage",
    "numeric_unit_tokens",
    "score_page",
    "select_relevant_pages",
]

ALWAYS_KEEP_FIRST_PAGES = 2
SELECT_THRESHOLD = 2.0

_POSITIVE_WEIGHT = 4.0
_NEGATIVE_WEIGHT = 6.0
_TOKEN_WEIGHT = 0.5
_TOKEN_CAP = 20

#: Headings that mark electrical specification content. Matched anywhere on the
#: page: a false positive only costs one extra page.
_POSITIVE: tuple[tuple[str, str], ...] = (
    ("electrical_characteristics", r"ELECTRICAL\s+CHARACTERISTICS"),
    ("specifications", r"\bSPECIFICATIONS\b"),
    ("recommended_operating_conditions", r"RECOMMENDED\s+OPERATING\s+CONDITIONS"),
    ("absolute_maximum_ratings", r"ABSOLUTE\s+MAXIMUM\s+RATINGS"),
    ("thermal_information", r"THERMAL\s+INFORMATION"),
    ("switching_characteristics", r"SWITCHING\s+CHARACTERISTICS"),
    ("timing_requirements", r"TIMING\s+REQUIREMENTS"),
)

#: Pin tables: always kept, because the pin map is extracted from them.
_PIN_TABLE: tuple[tuple[str, str], ...] = (
    ("pin_configuration", r"PIN\s+CONFIGURATIONS?"),
    ("pin_functions", r"PIN\s+FUNCTIONS"),
    ("terminal_functions", r"TERMINAL\s+FUNCTIONS"),
    ("pin_descriptions", r"PIN\s+DESCRIPTIONS?"),
    ("pin_assignments", r"PIN\s+ASSIGNMENTS"),
    ("terminal_configuration", r"TERMINAL\s+CONFIGURATIONS?"),
)

#: Manufacturing, legal and history sections. Matched only as a heading (at the
#: start of a line, optionally numbered), because a false match here would cut a
#: page.
_NEGATIVE: tuple[tuple[str, str], ...] = (
    ("mechanical_data", r"MECHANICAL\s+DATA"),
    ("package_option_addendum", r"PACKAGE\s+OPTION\s+ADDENDUM"),
    ("package_materials_information", r"PACKAGE\s+MATERIALS\s+INFORMATION"),
    ("tape_and_reel", r"TAPE\s+AND\s+REEL"),
    ("land_pattern", r"(?:EXAMPLE\s+(?:BOARD\s+)?)?LAND\s+PATTERN"),
    ("layout_example", r"LAYOUT\s+EXAMPLES?"),
    ("revision_history", r"REVISION\s+HISTORY"),
    ("important_notice", r"IMPORTANT\s+NOTICE"),
    ("ordering_information", r"ORDERING\s+INFORMATION"),
)

_FLAGS = re.IGNORECASE | re.MULTILINE
_POSITIVE_RE = tuple((name, re.compile(pattern, _FLAGS)) for name, pattern in _POSITIVE)
_PIN_RE = tuple((name, re.compile(pattern, _FLAGS)) for name, pattern in _PIN_TABLE)
_NEGATIVE_RE = tuple(
    (name, re.compile(r"^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+)?" + pattern + r"\b", _FLAGS))
    for name, pattern in _NEGATIVE
)

#: A number followed by an electrical unit ("0.8 V", "1 MHz", "2 µA", "25°C",
#: "0.5 V/µs"). Lengths (mm, mil, in) are deliberately absent: package drawings
#: are full of them and they are not electrical specifications.
_NUMERIC_UNIT = re.compile(
    r"(?<![\w.])[-+±]?\d+(?:[.,]\d+)?\s?"
    r"(?:"
    r"°\s?C"
    r"|%"
    r"|dB[mV]?"
    r"|[GMkmunpµμ]?(?:V/[µμu]?s|A/V|V/V|Ω|ohms?|Hz|V|A|W|F|H|S|s)"
    r")(?![A-Za-z])"
)


def numeric_unit_tokens(text: str) -> list[str]:
    """Every number-with-electrical-unit token on ``text``, in order."""
    return [match.group(0).strip() for match in _NUMERIC_UNIT.finditer(text or "")]


# --------------------------------------------------------------------------- #
# records


@dataclass(frozen=True)
class PageScore:
    """One selected page and why it was selected (the decision's provenance)."""

    doc_id: str
    pdf_page: int  # 0-based
    score: float
    signals: tuple[str, ...]
    text_source: Literal["embedded", "ocr"] = "embedded"


@dataclass(frozen=True)
class SkippedPage:
    """A readable page that was deliberately not sent, with the reason."""

    doc_id: str
    pdf_page: int
    score: float
    signals: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class PageGap:
    """A page that could not be read at all: an explicit evidence gap."""

    doc_id: str
    pdf_page: int
    code: str  # "no_text_ocr_unavailable" | "no_text_ocr_failed" | "no_text_ocr_empty"
    reason: str  # human-readable, e.g. "page 7: no text layer; OCR unavailable"


@dataclass(frozen=True)
class PageSelection:
    """The full, auditable account of which pages were read and why."""

    selected: tuple[PageScore, ...]
    skipped: tuple[SkippedPage, ...]
    gaps: tuple[PageGap, ...]
    #: Text recognized by OCR for pages that had no text layer, keyed by
    #: ``(doc_id, pdf_page)``. Only pages whose OCR produced text appear.
    ocr_text: dict[tuple[str, int], str] = field(default_factory=dict)

    def selected_keys(self) -> frozenset[tuple[str, int]]:
        return frozenset((page.doc_id, page.pdf_page) for page in self.selected)

    def selected_pages(self, doc_id: str) -> list[int]:
        return sorted(page.pdf_page for page in self.selected if page.doc_id == doc_id)

    def merged(self, other: PageSelection) -> PageSelection:
        return PageSelection(
            selected=self.selected + other.selected,
            skipped=self.skipped + other.skipped,
            gaps=self.gaps + other.gaps,
            ocr_text={**self.ocr_text, **other.ocr_text},
        )

    def to_evidence(self) -> dict[str, Any]:
        """JSON-ready record of the selection, for the extraction evidence."""
        return {
            "selected": [
                {
                    "doc_id": page.doc_id,
                    "pdf_page": page.pdf_page,
                    "score": page.score,
                    "signals": list(page.signals),
                    "text_source": page.text_source,
                }
                for page in self.selected
            ],
            "skipped": [
                {
                    "doc_id": page.doc_id,
                    "pdf_page": page.pdf_page,
                    "score": page.score,
                    "signals": list(page.signals),
                    "reason": page.reason,
                }
                for page in self.skipped
            ],
            "gaps": [
                {
                    "doc_id": gap.doc_id,
                    "pdf_page": gap.pdf_page,
                    "code": gap.code,
                    "reason": gap.reason,
                }
                for gap in self.gaps
            ],
        }


# --------------------------------------------------------------------------- #
# scoring


def score_page(text: str) -> tuple[float, tuple[str, ...], bool]:
    """``(score, matched signals, has_pin_table)`` for one page's text."""
    signals: list[str] = []
    score = 0.0
    for name, pattern in _POSITIVE_RE:
        if pattern.search(text):
            signals.append(f"+{name}")
            score += _POSITIVE_WEIGHT
    pin_table = False
    for name, pattern in _PIN_RE:
        if pattern.search(text):
            signals.append(f"+{name}")
            score += _POSITIVE_WEIGHT
            pin_table = True
    for name, pattern in _NEGATIVE_RE:
        if pattern.search(text):
            signals.append(f"-{name}")
            score -= _NEGATIVE_WEIGHT
    tokens = len(numeric_unit_tokens(text))
    if tokens:
        signals.append(f"numeric_unit_tokens={tokens}")
        score += _TOKEN_WEIGHT * min(tokens, _TOKEN_CAP)
    return round(score, 2), tuple(signals), pin_table


def select_relevant_pages(
    doc_id: str,
    pages: Sequence[tuple[int, str]],
    *,
    ocr_page: Callable[[int], str] | None = None,
    ocr_unavailable: str | None = None,
    threshold: float = SELECT_THRESHOLD,
    always_keep_first: int = ALWAYS_KEEP_FIRST_PAGES,
) -> PageSelection:
    """Score ``pages`` (``(pdf_page, text)`` pairs) of one document and select.

    ``ocr_page`` recognizes a page that has no text layer (it may raise; the
    exception's message becomes the gap's reason). Pass ``None`` together with
    ``ocr_unavailable`` (the reason OCR cannot run) when there is no engine.
    """
    selected: list[PageScore] = []
    skipped: list[SkippedPage] = []
    gaps: list[PageGap] = []
    ocr_text: dict[tuple[str, int], str] = {}
    for pdf_page, raw in sorted(pages, key=lambda item: item[0]):
        text = raw or ""
        source: Literal["embedded", "ocr"] = "embedded"
        if not text.strip():
            if ocr_page is None:
                why = ocr_unavailable or "no OCR engine was provided"
                gaps.append(
                    PageGap(
                        doc_id,
                        pdf_page,
                        "no_text_ocr_unavailable",
                        f"page {pdf_page}: no text layer; OCR unavailable ({why})",
                    )
                )
                continue
            try:
                text = ocr_page(pdf_page) or ""
            except Exception as exc:  # reported verbatim as the gap's reason
                gaps.append(
                    PageGap(
                        doc_id,
                        pdf_page,
                        "no_text_ocr_failed",
                        f"page {pdf_page}: no text layer; OCR failed ({type(exc).__name__}: {exc})",
                    )
                )
                continue
            if not text.strip():
                gaps.append(
                    PageGap(
                        doc_id,
                        pdf_page,
                        "no_text_ocr_empty",
                        f"page {pdf_page}: no text layer; OCR recognized no text",
                    )
                )
                continue
            source = "ocr"
            ocr_text[(doc_id, pdf_page)] = text
        score, signals, pin_table = score_page(text)
        forced: list[str] = []
        if pdf_page < always_keep_first:
            forced.append("keep:part_identity_page")
        if pin_table:
            forced.append("keep:pin_table")
        if forced or score >= threshold:
            selected.append(PageScore(doc_id, pdf_page, score, (*forced, *signals), source))
            continue
        skipped.append(
            SkippedPage(doc_id, pdf_page, score, signals, _skip_reason(score, signals, threshold))
        )
    return PageSelection(tuple(selected), tuple(skipped), tuple(gaps), ocr_text)


def _skip_reason(score: float, signals: Sequence[str], threshold: float) -> str:
    negatives = [signal[1:] for signal in signals if signal.startswith("-")]
    if negatives:
        return f"non-electrical section ({', '.join(negatives)}); score {score} < {threshold}"
    if not signals:
        return (
            f"no electrical-spec signal (no spec heading, 0 numeric-with-unit tokens); "
            f"score {score} < {threshold}"
        )
    return f"weak electrical-spec signal ({', '.join(signals)}); score {score} < {threshold}"
