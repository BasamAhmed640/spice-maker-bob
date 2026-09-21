"""Metadata routing must retain full electrical coverage and complete pin sections."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from boardmodeler.providers.agent import AgentExtractionProvider, _metadata_pages
from boardmodeler.providers.base import DocSnippet, ExtractionRequest, ExtractionTask


def page(index, text):
    return DocSnippet("datasheet", index, None, text)


def test_numbered_pin_section_includes_continuation_and_boundary_page():
    pages = tuple(
        page(i, text)
        for i, text in enumerate(
            [
                "Device cover",
                "2 Contents\n6 Pin Configuration ........... 4",
                "Overview",
                "6 Pin Configuration and Functions\nPin Functions\nA 1 input",
                "Pin Functions (continued)\nZ 20 output\n7 Specifications",
                "Electrical data",
                "8 Detailed Description",
            ]
        )
    )
    selected = _metadata_pages(pages)
    assert [s.pdf_page for s in selected] == [0, 1, 2, 3, 4]
    assert "Z 20" in selected[-1].text


def test_unrecognized_pin_layout_uses_all_pages():
    pages = tuple(page(i, "unrecognized format") for i in range(10))
    assert _metadata_pages(pages) == pages


def test_routing_changes_only_metadata_not_requirements(monkeypatch):
    from boardmodeler.providers import agent

    pages = tuple(
        page(i, text + "\n" + ("electrical values " * 200))
        for i, text in enumerate(
            [
                "Cover",
                "Contents",
                "Overview",
                "6 Pin Configuration and Functions",
                "Pin Functions (continued)\n7 Specifications",
                "Electrical data",
                "8 Detailed Description",
                "Operating conditions",
                "9 Applications",
            ]
        )
    )
    rows = ExtractionRequest(ExtractionTask.REQUIREMENTS, "", "{}", pages, allow_remote=True)
    pins = ExtractionRequest(ExtractionTask.PINMAP, "", "{}", pages, allow_remote=True)
    captured = []

    def capture(jobs, *args):
        captured.extend(jobs)
        raise RuntimeError("captured without contacting API")

    monkeypatch.setattr(agent, "_run_batches", capture)
    provider = AgentExtractionProvider(SimpleNamespace(name="test"))
    with pytest.raises(RuntimeError, match="captured"):
        provider.extract_many([pins, rows])
    assert len(captured[0][0].snippets) == 5
    assert tuple(s for job in captured[1:] for req in job for s in req.snippets) == pages
