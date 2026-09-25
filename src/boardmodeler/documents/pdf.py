"""PDF reading: page inventory, embedded-text status, and citation matching.

Three honesty rules shape this module:

* ``text_extraction`` reports what was actually found. A page whose text exists
  only as pixels is never counted as embedded text, and a document with mixed
  pages is reported as ``"hybrid"`` instead of being upgraded to ``"embedded"``.
  OCR is never run here; :mod:`boardmodeler.documents.ocr` is where the
  "OCR is not available" state is reported.
* Page labels come from the document's own ``/PageLabels`` tree. A document
  that does not declare labels gets ``{}``: the numbers printed on a datasheet
  page are not its PDF page index (a cover page offsets them), so a guess would
  put a wrong label on a citation.
* :func:`excerpt_on_page` normalizes typography and line-break hyphenation and
  then does a plain substring test. An excerpt that is not on the page returns
  ``False``; nothing here is fuzzy-matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from boardmodeler.domain.hashing import sha256_file

__all__ = [
    "PdfDocument",
    "PdfPage",
    "excerpt_on_page",
    "normalize_for_citation",
    "page_text",
    "read_pdf",
    "text_extraction_of",
]

TextExtraction = Literal["embedded", "ocr", "hybrid", "none"]

# Form XObjects may nest; the cap only exists so a self-referential or
# pathological file cannot make image counting recurse forever.
_MAX_XOBJECT_DEPTH = 8


@dataclass(frozen=True)
class PdfPage:
    """One page of a PDF as read, including whether its text is really text."""

    pdf_page: int  # 0-based index into the PDF's page tree
    printed_label: str | None
    text: str
    char_count: int
    has_embedded_text: bool
    images: int = 0  # raster XObjects reachable from this page


@dataclass(frozen=True)
class PdfDocument:
    """A PDF plus the inventory needed to cite it page by page.

    ``page_count`` is the document's real page count even when ``max_pages``
    limited how many pages were read into ``pages``.
    """

    path: Path
    file_hash: str
    page_count: int
    pages: list[PdfPage]
    page_labels: dict[int, str]  # pdf_page -> printed label, only where declared
    text_extraction: TextExtraction
    title: str | None
    metadata: dict[str, str]


def read_pdf(path: str | Path, *, max_pages: int | None = None) -> PdfDocument:
    """Read ``path`` into a :class:`PdfDocument`.

    ``max_pages`` limits how many pages are extracted (large datasheets are read
    a few pages at a time); ``page_count`` and ``page_labels`` still describe the
    whole document. Raises whatever pypdf raises for an unreadable file rather
    than returning an empty document.
    """
    if max_pages is not None and max_pages < 1:
        raise ValueError(f"max_pages must be >= 1 or None, got {max_pages}")
    source = Path(path)
    try:
        return _read_with_pypdf(source, max_pages)
    except Exception as exc:
        # pypdf's parser has failed intermittently on readable datasheets with errors
        # that say nothing about the file (observed: ``NameError: _LENGTH_LIMIT`` inside
        # ``NumberObject.read_from_stream`` in 1 of 4 runs of the same PDF). pdfium is an
        # independent reader, so a build is not stopped by one library's fault; when
        # pdfium cannot read the file either, pypdf's own error is what the caller sees.
        try:
            return _read_with_pdfium(source, max_pages)
        except Exception:
            raise exc from None


def _read_with_pypdf(source: Path, max_pages: int | None) -> PdfDocument:
    reader = PdfReader(str(source))
    page_count = len(reader.pages)
    metadata, title = _metadata(reader)
    labels = _page_labels(reader)
    limit = page_count if max_pages is None else min(max_pages, page_count)
    pages = [_read_page(reader, index, labels.get(index)) for index in range(limit)]
    return PdfDocument(
        path=source,
        file_hash=sha256_file(source),
        page_count=page_count,
        pages=pages,
        page_labels=labels,
        text_extraction=text_extraction_of(pages),
        title=title,
        metadata=metadata,
    )


def _read_with_pdfium(source: Path, max_pages: int | None) -> PdfDocument:
    """The same inventory read with pdfium: text, raster images and ``/Info`` metadata.

    Page labels are left undeclared (``{}``) — the rule above is that a label is only
    reported when the document's own tree was read, and this path does not read it.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    document = pdfium.PdfDocument(str(source))
    try:
        page_count = len(document)
        limit = page_count if max_pages is None else min(max_pages, page_count)
        pages: list[PdfPage] = []
        for index in range(limit):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = (textpage.get_text_range() or "").replace("\r\n", "\n")
                finally:
                    textpage.close()
                images = sum(
                    1
                    for _ in page.get_objects(
                        filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,), max_depth=_MAX_XOBJECT_DEPTH
                    )
                )
            finally:
                page.close()
            pages.append(
                PdfPage(
                    pdf_page=index,
                    printed_label=None,
                    text=text,
                    char_count=len(text),
                    has_embedded_text=bool(text.strip()),
                    images=images,
                )
            )
        try:
            metadata = {
                str(key): str(value).strip()
                for key, value in document.get_metadata_dict(skip_empty=True).items()
                if str(value).strip()
            }
        except Exception:
            metadata = {}
    finally:
        document.close()
    return PdfDocument(
        path=source,
        file_hash=sha256_file(source),
        page_count=page_count,
        pages=pages,
        page_labels={},
        text_extraction=text_extraction_of(pages),
        title=metadata.get("Title") or None,
        metadata=metadata,
    )


def page_text(doc: PdfDocument, pdf_page: int) -> str:
    """Extracted embedded text of ``pdf_page`` (empty when there is none)."""
    return _page(doc, pdf_page).text


def text_extraction_of(pages: list[PdfPage]) -> TextExtraction:
    """Classify a page inventory honestly.

    ``"embedded"`` requires every page that carries content to have text;
    ``"none"`` means no page has any text at all; anything in between is
    ``"hybrid"``. A page with neither text nor images is blank and does not
    count against ``"embedded"``.
    """
    with_text = [page for page in pages if page.has_embedded_text]
    content = [page for page in pages if page.has_embedded_text or page.images > 0]
    if not with_text:
        return "none"
    if len(with_text) == len(content):
        return "embedded"
    return "hybrid"


def normalize_for_citation(text: str) -> str:
    """Collapse whitespace and unify the typography PDF extraction varies on.

    Dashes, quotation marks, ligatures and invisible characters (soft hyphens,
    zero-width spaces) are folded so that an excerpt keeps matching when a
    viewer or a PDF producer chose different but equivalent characters.
    """
    return _WHITESPACE.sub(" ", text.translate(_TYPOGRAPHY)).strip()


def excerpt_on_page(doc: PdfDocument, excerpt: str, pdf_page: int, *, max_chars: int = 400) -> bool:
    """Return whether ``excerpt`` really appears on ``pdf_page``.

    Only the first ``max_chars`` characters of ``excerpt`` are compared (an
    evidence excerpt is capped at 400 characters). Both sides are normalized, and
    each side is additionally tried with line-break hyphenation joined
    (``"configura-\\ntion"`` -> ``"configuration"``), because PDF extraction keeps
    the printed line breaks. Anything that is not there returns ``False``.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    needle = normalize_for_citation(excerpt[:max_chars])
    if not needle:
        return False
    haystack = normalize_for_citation(page_text(doc, pdf_page))
    if not haystack:
        return False
    variants = {needle, _join_hyphenation(needle)}
    texts = {haystack, _join_hyphenation(haystack)}
    return any(variant in text for variant in variants for text in texts)


# --------------------------------------------------------------------------- #
# internals


def _page(doc: PdfDocument, pdf_page: int) -> PdfPage:
    if pdf_page < 0 or pdf_page >= len(doc.pages):
        raise IndexError(
            f"page {pdf_page} is outside the {len(doc.pages)} pages read from {doc.path}"
        )
    return doc.pages[pdf_page]


def _read_page(reader: PdfReader, index: int, printed_label: str | None) -> PdfPage:
    page = reader.pages[index]
    text = page.extract_text() or ""
    return PdfPage(
        pdf_page=index,
        printed_label=printed_label,
        text=text,
        char_count=len(text),
        has_embedded_text=bool(text.strip()),
        images=_count_images(page),
    )


def _count_images(page: object) -> int:
    """Count raster XObjects reachable from a page, following nested forms."""
    return _count_image_xobjects(_dictionary(page).get("/Resources"), depth=0)


def _count_image_xobjects(resources: object, *, depth: int) -> int:
    if depth > _MAX_XOBJECT_DEPTH:
        return 0
    xobjects = _dictionary(resources).get("/XObject")
    if xobjects is None:
        return 0
    entries = xobjects.values() if isinstance(xobjects, DictionaryObject) else ()
    count = 0
    for entry in entries:
        xobject = _dictionary(entry)
        subtype = xobject.get("/Subtype")
        if subtype == "/Image":
            count += 1
        elif subtype == "/Form":
            count += _count_image_xobjects(xobject.get("/Resources"), depth=depth + 1)
    return count


def _dictionary(value: object) -> DictionaryObject:
    """Resolve indirect references and return a dictionary, or an empty one."""
    resolved = value
    get_object = getattr(resolved, "get_object", None)
    if callable(get_object):
        resolved = get_object()
    return resolved if isinstance(resolved, DictionaryObject) else DictionaryObject()


def _metadata(reader: PdfReader) -> tuple[dict[str, str], str | None]:
    """Best-effort ``/Info`` dictionary; a malformed one yields absence, not a guess."""
    # An unreadable /Info dictionary must not sink an otherwise readable
    # document; absence is reported as absence.
    try:
        info = reader.metadata
        if info is None:
            return {}, None
        entries: dict[str, str] = {}
        for key, value in info.items():
            text = "" if value is None else str(value).strip()
            if text:
                entries[str(key).lstrip("/")] = text
        return entries, entries.get("Title") or None
    except Exception:
        return {}, None


def _page_labels(reader: PdfReader) -> dict[int, str]:
    """Printed labels declared by the document itself, keyed by 0-based page.

    ``reader.page_labels`` synthesises decimal labels for documents without a
    ``/PageLabels`` tree; those are the viewer's defaults, not the numbers the
    document prints, so a document without the tree reports no labels at all.
    """
    # A broken /PageLabels tree stays absent rather than being guessed at.
    try:
        labels = reader.page_labels if reader.root_object.get("/PageLabels") else []
    except Exception:
        return {}
    return {index: text for index, label in enumerate(labels) if (text := str(label).strip())}


# Typography folding: one pass, no per-character branching at call time.
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\ufe58\ufe63\uff0d"
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}
_QUOTES = {
    "\u2018": "'",  # left single
    "\u2019": "'",  # right single / apostrophe
    "\u201a": "'",  # single low-9
    "\u201b": "'",  # single high-reversed-9
    "\u02bc": "'",  # modifier letter apostrophe
    "\u201c": '"',  # left double
    "\u201d": '"',  # right double
    "\u201e": '"',  # double low-9
    "\u201f": '"',  # double high-reversed-9
    "\u00ab": '"',  # left guillemet
    "\u00bb": '"',  # right guillemet
}
_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"  # soft hyphen, zero-widths, BOM
_TYPOGRAPHY = str.maketrans(
    {**{char: "-" for char in _DASHES}, **_LIGATURES, **_QUOTES, **dict.fromkeys(_INVISIBLE, "")}
)
_WHITESPACE = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"-\s")


def _join_hyphenation(text: str) -> str:
    """Undo hyphenation introduced by a printed line break."""
    return _HYPHEN_BREAK.sub("", text)
