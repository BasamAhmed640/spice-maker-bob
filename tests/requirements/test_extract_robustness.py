"""Extraction robustness: relevant-page selection, scanned pages, malformed rows,
truncated responses and pin-map checks.

Offline: a recording provider double stands in for the remote provider, OCR is a
test double or the explicit unavailable engine, and nothing needs LTspice.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.requirements.test_extract import (
    RecordingProvider,
    make_project,
    payloads,
    pin_payload,
    requirement_payload,
)

from boardmodeler.documents.ocr import OcrUnavailable, UnavailableOcrEngine
from boardmodeler.documents.store import DocumentStore
from boardmodeler.domain.records import DocumentRecord, PinDefinition
from boardmodeler.pipeline.project import Project
from boardmodeler.providers.base import ExtractionResponse, ExtractionTask, ProviderError
from boardmodeler.requirements.extract import check_pin_map, extract_requirements
from boardmodeler.requirements.model import normalize_unit
from boardmodeler.security.policy import DataPolicy

SPEC_LINE = "Input voltage range 4.5 V to 60 V"
SHEET = [
    ["SYNTH123 step-down converter", "synthetic test document"],
    ["Table of Contents", "1 Features"],
    ["PIN CONFIGURATION AND FUNCTIONS", "VIN 1 supply input", "GND 2 ground"],
    ["ELECTRICAL CHARACTERISTICS", SPEC_LINE, "Shutdown current 2 uA", "Reference 0.8 V"],
    ["The device is used in many kinds of industrial systems."],
    ["REVISION HISTORY", "Changes from Revision A to Revision B"],
    ["MECHANICAL DATA", "PLASTIC SMALL OUTLINE 4.90 mm 3.91 mm"],
    ["PACKAGE OPTION ADDENDUM", "Orderable Device Status Package Type"],
]


def store_sheet(project: Project, pages: Sequence[Sequence[str]]) -> DocumentRecord:
    """A real PDF whose pages carry the given lines (an empty list = no text layer)."""
    from reportlab.pdfgen import canvas

    source = project.root / "sheet.pdf"
    sheet = canvas.Canvas(str(source))
    for lines in pages:
        for index, line in enumerate(lines):
            sheet.drawString(72, 720 - 16 * index, line)
        sheet.showPage()
    sheet.save()
    return DocumentStore(project.root).add_file(
        source,
        doc_type="datasheet",
        provenance="downloaded_public",
        classification="public",
        remote_inference_allowed=True,
        title="sheet",
    )


def run(project: Project, provider, tmp_path: Path, **kwargs):
    return extract_requirements(
        project,
        provider=provider,
        cache_dir=tmp_path / "cache",
        policy=DataPolicy(),
        allow_remote=True,
        **kwargs,
    )


def sent_pages(provider: RecordingProvider, task: ExtractionTask) -> set[int]:
    return {snippet.pdf_page for snippet in provider.snippets_for(task)}


NO_OCR = UnavailableOcrEngine(
    OcrUnavailable(reason="tesseract_not_found", detail="not on PATH (test)", engine=None)
)


class FakeOcrEngine:
    def __init__(self, text: str) -> None:
        self.text = text
        self.images: list[bytes] = []

    def available(self) -> bool:
        return True

    def describe(self):
        return None

    def page_text(self, image_png: bytes) -> str:
        self.images.append(image_png)
        return self.text


# --------------------------------------------------------------------------- #
# (e) relevance selection wired into extraction


def test_only_relevant_pages_are_sent_and_every_page_is_accounted_for(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    document = store_sheet(project, SHEET)
    data = payloads(requirement_payload("REQ_VIN", SPEC_LINE, page=3, doc_id=document.doc_id))
    provider = RecordingProvider(data)

    result = run(project, provider, tmp_path, ocr_engine=NO_OCR)

    for task in ExtractionTask:
        assert sent_pages(provider, task) == {0, 1, 2, 3}
    selection = result.page_selection
    assert selection is not None
    assert [row["pdf_page"] for row in selection["selected"]] == [0, 1, 2, 3]
    skipped = {row["pdf_page"]: row["reason"] for row in selection["skipped"]}
    assert sorted(skipped) == [4, 5, 6, 7]
    assert "revision_history" in skipped[5] and "mechanical_data" in skipped[6]
    assert selection["gaps"] == []
    assert "pages_sent=4 pages_skipped=4 page_gaps=0" in result.detail
    recorded = json.loads(project.path("evidence/page-selection.json").read_text("utf-8"))
    assert recorded == selection
    # The disclosure names only the pages that actually left the machine.
    assert [disclosure.page_ranges for disclosure in result.disclosures] == [
        {document.doc_id: [0, 1, 2, 3]}
    ]
    assert result.review.verified == ["REQ_VIN"]


def test_selection_can_be_disabled_to_send_every_page(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store_sheet(project, SHEET)
    provider = RecordingProvider(payloads())

    result = run(project, provider, tmp_path, select_relevant=False, ocr_engine=NO_OCR)

    assert sent_pages(provider, ExtractionTask.REQUIREMENTS) == set(range(len(SHEET)))
    assert result.page_selection is None


# --------------------------------------------------------------------------- #
# (b) scanned pages


def test_scanned_page_without_ocr_is_an_explicit_gap_finding(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store_sheet(project, [*SHEET[:4], []])
    provider = RecordingProvider(payloads())

    result = run(project, provider, tmp_path, ocr_engine=NO_OCR)

    assert 4 not in sent_pages(provider, ExtractionTask.REQUIREMENTS)
    gaps = [finding for finding in result.findings if finding.code == "extract_page_gap"]
    assert len(gaps) == 1
    assert "page 4: no text layer; OCR unavailable" in gaps[0].message
    assert gaps[0].detail["pdf_page"] == "4"
    assert result.page_selection["gaps"][0]["code"] == "no_text_ocr_unavailable"
    assert "page_gaps=1" in result.detail


def test_scanned_page_goes_to_ocr_and_citations_are_checked_against_ocr_text(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    document = store_sheet(project, [*SHEET[:2], []])
    recognized = "ELECTRICAL CHARACTERISTICS\nInput voltage range 4.5 V to 60 V"
    engine = FakeOcrEngine(recognized)
    data = payloads(requirement_payload("REQ_OCR", SPEC_LINE, page=2, doc_id=document.doc_id))
    provider = RecordingProvider(data)

    result = run(project, provider, tmp_path, ocr_engine=engine)

    assert len(engine.images) == 1 and engine.images[0].startswith(b"\x89PNG")
    sent = [s for s in provider.snippets_for(ExtractionTask.REQUIREMENTS) if s.pdf_page == 2]
    assert "".join(snippet.text for snippet in sent) == recognized
    row = {row["pdf_page"]: row for row in result.page_selection["selected"]}[2]
    assert row["text_source"] == "ocr"
    assert result.page_selection["gaps"] == []
    assert result.review.verified == ["REQ_OCR"]


def test_a_document_with_no_readable_page_is_refused_by_name(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store_sheet(project, [[], []])
    provider = RecordingProvider(payloads())
    with pytest.raises(ProviderError) as caught:
        run(project, provider, tmp_path, ocr_engine=NO_OCR)
    assert caught.value.code == "no_readable_pages"
    assert "page 0: no text layer; OCR unavailable" in caught.value.detail
    assert provider.requests == []


# --------------------------------------------------------------------------- #
# (a) missing data


def test_a_page_without_numbers_yields_no_invented_rows(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store_sheet(project, [SHEET[0], SHEET[1], SHEET[4]])
    provider = RecordingProvider(payloads())  # the provider found no rows

    result = run(project, provider, tmp_path, ocr_engine=NO_OCR)

    assert result.requirements == []
    assert sent_pages(provider, ExtractionTask.REQUIREMENTS) == {0, 1}
    skipped = result.page_selection["skipped"]
    assert [row["pdf_page"] for row in skipped] == [2]
    assert skipped[0]["reason"].startswith("no electrical-spec signal")


# --------------------------------------------------------------------------- #
# (c) malformed tables and units


def test_misaligned_min_max_columns_are_refused_not_swapped(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    document = store_sheet(project, SHEET[:4])
    row = requirement_payload("REQ_SWAPPED", SPEC_LINE, page=3, doc_id=document.doc_id)
    row["limits"] = {"min": 60.0, "max": 4.5, "unit": "V"}  # columns shifted
    provider = RecordingProvider(payloads(row))

    with pytest.raises(ProviderError) as caught:
        run(project, provider, tmp_path, ocr_engine=NO_OCR)

    assert caught.value.code == "extraction_payload_invalid"
    assert "requirements[0]" in caught.value.detail
    assert "nothing from this payload was accepted" in caught.value.detail


def test_garbled_unit_is_kept_verbatim_and_flagged(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    document = store_sheet(project, SHEET[:4])
    row = requirement_payload("REQ_GARBLED", SPEC_LINE, page=3, doc_id=document.doc_id)
    row["limits"] = {"min": 4.5, "max": 60.0, "unit": "V 60"}  # a value bled into the unit
    provider = RecordingProvider(payloads(row))

    result = run(project, provider, tmp_path, ocr_engine=NO_OCR)

    [kept] = result.requirements
    assert (kept.limits.min, kept.limits.max, kept.limits.unit) == (4.5, 60.0, "V 60")
    flagged = [issue for issue in result.issues if issue.code == "unit_unknown"]
    assert [issue.req_id for issue in flagged] == ["REQ_GARBLED"]


@pytest.mark.parametrize(
    "unit",
    [
        "V",
        "mV",
        "µV",
        "uV",
        "A",
        "mA",
        "µA",
        "uA",
        "nA",
        "Ω",
        "ohm",
        "kΩ",
        "MΩ",
        "Hz",
        "kHz",
        "MHz",
        "s",
        "ms",
        "µs",
        "us",
        "ns",
        "°C",
        "%",
        "dB",
        "V/µs",
        "W",
        "mW",
        "F",
        "µF",
        "nF",
        "pF",
        "H",
        "µH",
        "S",
        "µS",
        "uS",
        "A/V",
        "V/V",
    ],
)
def test_datasheet_units_are_in_the_vocabulary(unit: str) -> None:
    assert normalize_unit(unit)


# --------------------------------------------------------------------------- #
# (d) truncated provider responses


class TruncatingProvider(RecordingProvider):
    """Answers REQUIREMENTS with JSON text that stops mid-row."""

    def extract(self, request, cancel=None) -> ExtractionResponse:
        response = super().extract(request, cancel)
        if request.task is not ExtractionTask.REQUIREMENTS:
            return response
        full = json.dumps(response.payload)
        cut = full[: len(full) // 2]
        return ExtractionResponse(
            payload=cut,  # what a provider that returned raw text would hand back
            identity=response.identity,
            raw_text=cut,
            from_cache=False,
            request_hash=response.request_hash,
        )


def test_truncated_response_is_a_named_failure_with_no_rows(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    document = store_sheet(project, SHEET[:4])
    rows = [
        requirement_payload(f"REQ_{index}", SPEC_LINE, page=3, doc_id=document.doc_id)
        for index in range(4)
    ]
    provider = TruncatingProvider(payloads(*rows))

    with pytest.raises(ProviderError) as caught:
        run(project, provider, tmp_path, ocr_engine=NO_OCR)

    assert caught.value.code == "extraction_response_truncated"
    assert "ends mid-JSON" in caught.value.detail
    assert "nothing from it was accepted" in caught.value.detail


def test_truncated_agent_output_is_not_parsed_into_rows(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from boardmodeler.authoring.backends import AuthorResult
    from boardmodeler.providers.agent import AgentExtractionProvider

    project = make_project(tmp_path)
    document = store_sheet(project, SHEET[:4])
    full = json.dumps(
        {
            key.value: value
            for key, value in payloads(
                requirement_payload("REQ_A", SPEC_LINE, page=3, doc_id=document.doc_id)
            ).items()
        }
    )

    class CutOffAgent:
        name = "selected-test-provider"
        provider = SimpleNamespace(model="m", endpoint="https://example.invalid")
        model = "m"

        def __init__(self) -> None:
            self.calls = 0

        def availability(self):
            return True, "test double"

        def author(self, request, cancel):
            self.calls += 1
            return AuthorResult(
                ok=True,
                detail="test",
                usage={},
                stdout_tail=full[: len(full) - 40],  # the stream stopped early
                session_id=None,
            )

    backend = CutOffAgent()
    with pytest.raises(ProviderError) as caught:
        run(project, AgentExtractionProvider(backend), tmp_path, ocr_engine=NO_OCR)
    assert caught.value.code in {"agent_extraction_failed", "extraction_payload_invalid"}
    assert backend.calls >= 1


# --------------------------------------------------------------------------- #
# pin-map checks


def pin(number: str, name: str) -> PinDefinition:
    return PinDefinition.model_validate(
        {
            "part_id": "SYNTH",
            "physical_pin": number,
            "name": name,
            "function": "test pin",
            "polarity": "not_applicable",
            "direction": "power",
            "output_topology": "power",
            "connection_requirement": "required",
        }
    )


def test_pin_checks_flag_duplicates_and_keep_every_pin() -> None:
    pins = [pin("1", "VIN"), pin("2", "GND"), pin("3", "GND"), pin("1", "EN"), pin("4", "PH")]
    issues = {(issue.code, issue.severity): issue for issue in check_pin_map(pins)}
    assert set(issues) == {("pin_physical_conflict", "error"), ("pin_name_duplicate", "warning")}
    assert issues[("pin_physical_conflict", "error")].detail["names"] == "EN, VIN"
    assert issues[("pin_name_duplicate", "warning")].detail["physical_pins"] == "2, 3"
    assert len(pins) == 5  # nothing merged or dropped
    assert check_pin_map([pin("1", "VIN"), pin("2", "GND")]) == []
    repeated = check_pin_map([pin("1", "VIN"), pin("1", "vin")])
    assert [(issue.code, issue.severity) for issue in repeated] == [
        ("pin_physical_duplicate", "warning")
    ]


def test_conflicting_pin_rows_reach_the_extraction_issues(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    store_sheet(project, SHEET[:4])
    data = payloads()
    pins = pin_payload()
    pins["pins"].append({**pins["pins"][0], "name": "EN"})  # pin 1 twice, two names
    data[ExtractionTask.PINMAP] = pins
    provider = RecordingProvider(data)

    result = run(project, provider, tmp_path, ocr_engine=NO_OCR)

    assert len(result.pins) == 3
    codes = [(issue.code, issue.severity) for issue in result.issues]
    assert ("pin_physical_conflict", "error") in codes
