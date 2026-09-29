"""A datasheet digest: spec tables rebuilt from character positions, not from the text layer.

The PDF text layer keeps the words of a table and loses its columns: the number 3.5 in
"Internal undervoltage lockout threshold ... 3.5 V" could be a minimum, a typical value
or a maximum, and a reader (an AI included) has to guess. The characters themselves carry
their exact positions, and a spec table right-aligns every number under its MIN / TYP /
MAX header. This module reads those positions, so the column of a number is measured, not
guessed, and every row keeps its page and vertical position for review.

Only the standard library and pypdfium2 (already a dependency). A page without a
MIN / TYP / MAX header line yields no rows; nothing is guessed for it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SpecRow",
    "Word",
    "digest_markdown",
    "read_pdf_rows",
    "rows_from_words",
    "words_from_page",
]

_DASHES = "\u2013\u2212"  # en dash, minus sign
_NUMBER = re.compile(rf"^[±+\-{_DASHES}]?\d+(?:\.\d+)?%?$")
_COLUMNS = ("MIN", "TYP", "MAX")
#: A number belongs to the column whose header right edge is this close to its own.
_EDGE_PT = 8.0
#: A description-only line describes number rows this close above or below it.
_SPAN_PT = 12.0
_NOISE = ("copyright", "submit document", "product folder")


@dataclass(frozen=True)
class SpecRow:
    """One spec-table row with the column of each number measured from its position."""

    page: int  # 1-based
    y: float  # vertical position on the page, PDF units from the bottom
    label: str  # parameter text, plus the test conditions when the layout does not separate them
    conditions: str
    min: str | None
    typ: str | None
    max: str | None
    unit: str


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    yc: float


def words_from_page(page) -> list[Word]:
    """Group a page's characters into words by their boxes, using the vertical centre.

    A word continues while the next character is on the same baseline band and less than
    3 points to the right of the last one; the narrow glyph "1" would otherwise split
    numbers such as 12 or 110 in two.
    """
    textpage = page.get_textpage()
    words: list[Word] = []
    current: dict | None = None
    for index in range(textpage.count_chars()):
        char = unicodedata.normalize("NFKC", textpage.get_text_range(index, 1))
        if not char.strip():
            current = None
            continue
        x0, y0, x1, y1 = textpage.get_charbox(index)
        yc = (y0 + y1) / 2
        if current and abs(yc - current["yc"]) < 3.5 and 0 <= x0 - current["x1"] < 3.0:
            current["text"] += char
            current["x1"] = max(current["x1"], x1)
        else:
            current = {"text": char, "x0": x0, "x1": x1, "yc": yc}
            words.append(current)  # type: ignore[arg-type]
    return [Word(w["text"], w["x0"], w["x1"], w["yc"]) for w in words]  # type: ignore[index]


def _lines(words: list[Word]) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (-w.yc, w.x0)):
        if lines and abs(lines[-1][0].yc - word.yc) < 4.0:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


@dataclass(frozen=True)
class _Header:
    right: dict[str, float]
    cond_x0: float | None
    unit_x0: float


def _header(line: list[Word]) -> _Header | None:
    by_name = {w.text.strip(".:").upper(): w for w in line}
    if not all(name in by_name for name in _COLUMNS):
        return None
    right = {name: by_name[name].x1 for name in _COLUMNS}
    unit = by_name.get("UNIT") or by_name.get("UNITS")
    test = by_name.get("TEST") or by_name.get("CONDITIONS")
    return _Header(
        right=right,
        cond_x0=test.x0 - 3 if test is not None else None,
        unit_x0=unit.x0 - 8 if unit is not None else right["MAX"] + 12,
    )


def _clean(text: str) -> str:
    text = re.sub(r"\((\d)\)", "", text)  # footnote markers such as gain(1)
    return re.sub(r"\s+", " ", text).strip(" ,;")


def _is_title(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def rows_from_words(words: list[Word], page_number: int) -> list[SpecRow]:
    """Spec rows of one page, from its positioned words."""
    header: _Header | None = None
    data: list[dict] = []
    descriptions: list[dict] = []
    for line in _lines(words):
        text_of_line = " ".join(w.text for w in line).lower()
        if any(noise in text_of_line for noise in _NOISE):
            continue
        found = _header(line)
        if found is not None:
            header = found
            continue
        if header is None:
            continue
        numbers: dict[str, str] = {}
        label: list[str] = []
        cond: list[str] = []
        unit: list[str] = []
        for word in line:
            column = min(_COLUMNS, key=lambda name: abs(header.right[name] - word.x1))
            if _NUMBER.match(word.text) and abs(header.right[column] - word.x1) <= _EDGE_PT:
                numbers[column] = word.text.translate(str.maketrans({"\u2013": "-", "\u2212": "-"}))
            elif word.x0 >= header.unit_x0:
                unit.append(word.text)
            elif word.x0 >= header.right["MIN"] - 60 and _NUMBER.match(word.text):
                continue  # a number in the value area that lines up with no column
            elif header.cond_x0 is not None and word.x0 >= header.cond_x0:
                cond.append(word.text)
            else:
                label.append(word.text)
        row = {
            "y": line[0].yc,
            "label": _clean(" ".join(label)),
            "cond": _clean(" ".join(cond)),
            "unit": " ".join(unit),
            "numbers": numbers,
        }
        if numbers:
            data.append(row)
        elif (
            row["label"] and not _is_title(row["label"]) and not re.match(r"^\(\d\)", row["label"])
        ):
            descriptions.append(row)
    rows: list[SpecRow] = []
    for row in data:
        label, unit = row["label"], row["unit"]
        if not label:  # the description sits on a neighbouring line (a row-spanning cell)
            near = [d for d in descriptions if abs(d["y"] - row["y"]) <= _SPAN_PT]
            if near:
                best = min(near, key=lambda d: abs(d["y"] - row["y"]))
                label = best["label"]
                unit = unit or best["unit"]
        rows.append(
            SpecRow(
                page=page_number,
                y=round(row["y"], 1),
                label=label,
                conditions=row["cond"],
                min=row["numbers"].get("MIN"),
                typ=row["numbers"].get("TYP"),
                max=row["numbers"].get("MAX"),
                unit=unit,
            )
        )
    return rows


def read_pdf_rows(path: str | Path, pages: range | None = None) -> list[SpecRow]:
    """Every spec-table row in ``path`` (or in the 0-based ``pages``)."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        rows: list[SpecRow] = []
        for index in pages if pages is not None else range(len(document)):
            page = document[index]
            try:
                rows.extend(rows_from_words(words_from_page(page), index + 1))
            finally:
                page.close()
        return rows
    finally:
        document.close()


def digest_markdown(rows: list[SpecRow]) -> str:
    """The rows as one Markdown table per page, with explicit MIN / TYP / MAX / UNIT columns."""
    out: list[str] = []
    page = None
    for row in rows:
        if row.page != page:
            page = row.page
            out += [
                "",
                f"### Page {page}",
                "",
                "| Parameter | Conditions | MIN | TYP | MAX | Unit |",
            ]
            out.append("|---|---|---|---|---|---|")
        cells = [row.label, row.conditions, row.min or "", row.typ or "", row.max or "", row.unit]
        out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(out).lstrip("\n") + "\n"
