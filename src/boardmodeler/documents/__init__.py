"""Document layer: PDF reading, page rasterization, OCR state, content-addressed store.

* :mod:`boardmodeler.documents.pdf` — page inventory, embedded-text status,
  page labels, and citation matching.
* :mod:`boardmodeler.documents.pages` — page rasterization for figure inspection.
* :mod:`boardmodeler.documents.ocr` — the pluggable OCR interface and the
  explicit "OCR is not available" state (tesseract is not installed here).
* :mod:`boardmodeler.documents.store` — where a project's documents live.
* :mod:`boardmodeler.documents.relevance` — which pages extraction reads, with reasons.
"""

from __future__ import annotations

__all__ = ["ocr", "pages", "pdf", "relevance", "store"]
