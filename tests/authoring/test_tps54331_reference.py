"""The reviewed source columns are not guessed from flattened table text."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from boardmodeler.authoring import tps54331_reference as reference
from boardmodeler.domain.records import DocumentRecord
from boardmodeler.models.support import RouteVerdict, SupportDecision
from boardmodeler.pipeline import make_model as engine
from boardmodeler.requirements.model import validate_requirements
from boardmodeler.requirements.review import find_conflicts, verify_citations

FIXTURE = Path(__file__).parents[2] / "fixtures/regulator/tps54331/reviewed-pages.json"


def source():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record = DocumentRecord(
        doc_id="TPS54331_REVIEWED_SOURCE",
        title="TPS54331 step-down converter",
        doc_type="datasheet",
        file_hash=payload["source_sha256"],
        provenance="user_supplied",
        page_count=41,
        text_extraction="embedded",
    )
    return record, {int(page): text for page, text in payload["pages"].items()}


def test_only_the_reviewed_part_and_exact_document_match():
    assert reference.matches(" tps54331 ", reference.DATASHEET_SHA256)
    assert not reference.matches("TPS54332", reference.DATASHEET_SHA256)
    assert not reference.matches("TPS54331DDA", reference.DATASHEET_SHA256)
    assert not reference.matches("TPS54331", "0" * 64)
    record, pages = source()
    with pytest.raises(ValueError, match="hash_mismatch"):
        reference.records(record.model_copy(update={"file_hash": "0" * 64}), pages)


def test_min_typ_max_columns_conditions_and_citations_are_preserved():
    record, pages = source()
    rows, pins, provenance = reference.records(record, pages)
    by_id = {row.req_id: row for row in rows}
    expected = {
        "VIN_UVLO": (None, None, 3.5, "V"),
        "EN_THRESHOLD": (None, 1.25, 1.35, "V"),
        "VREF": (0.772, 0.8, 0.828, "V"),
        "ILIM": (3.5, 5.8, None, "A"),
        "FSW": (456, 570, 684, "kHz"),
        "TONMIN": (None, 105, 130, "ns"),
        "DMAX": (90, 93, None, "%"),
        "SS_CHARGE": (None, 2, None, "uA"),
    }
    for name, values in expected.items():
        limits = by_id[f"TPS54331_{name}"].limits
        assert (limits.min, limits.typ, limits.max, limits.unit) == values
    assert "VIN = 12 V" in by_id["TPS54331_ILIM"].conditions[0].text
    assert by_id["TPS54331_VIN_UVLO_TYP_UNSPECIFIED"].limits is None
    assert by_id["TPS54331_ILIM"].evidence[0].page.pdf_page == 4
    assert by_id["TPS54331_ILIM"].evidence[0].page.printed_label == "5"
    verified = verify_citations(
        rows, {record.doc_id: record}, excerpt_lookup=lambda _doc, page: pages[page]
    )
    assert all(verified.values()) and all(row.citation_verified for row in rows)
    assert not validate_requirements(rows, documents={record.doc_id: record}).errors
    assert provenance["document_sha256"] == reference.DATASHEET_SHA256
    assert not provenance["complete_datasheet_extraction"]
    assert provenance["qualified_probes_added"] == 0
    assert [pin["name"] for pin in pins] == list(reference.PIN_NAMES)
    assert all(pin["package_resolution"] == "unresolved" for pin in pins)
    assert provenance["package_options"]["DDA"]["pins"][-1] == "PowerPAD"
    assert by_id["TPS54331_DDA_POWERPAD"].configuration == "DDA"


@pytest.mark.parametrize("damage", ["missing_page", "changed_value"])
def test_unreadable_or_changed_source_text_refuses_the_profile(damage):
    record, pages = source()
    if damage == "missing_page":
        pages[4] = ""
    else:
        pages[4] = pages[4].replace("3.5 5.8 A", "3.5 8.5 A")
    with pytest.raises(ValueError, match=r"unreadable|citation_missing"):
        reference.records(record, pages)


def test_quantity_identity_prevents_false_conflicts_but_keeps_real_ones():
    record, pages = source()
    rows, _pins, _provenance = reference.records(record, pages)
    assert find_conflicts(rows) == []
    vref = next(row for row in rows if row.req_id == "TPS54331_VREF")
    wrong = vref.model_copy(
        update={
            "req_id": "CONFLICTING_VREF",
            "limits": vref.limits.model_copy(update={"min": 0.9, "typ": 1.0, "max": 1.1}),
        }
    )
    assert any(
        {left, right} == {vref.req_id, wrong.req_id}
        for left, right, _reason in find_conflicts([*rows, wrong])
    )


def profile_run(tmp_path, monkeypatch):
    record, pages = source()
    request = engine.MakeModelRequest(
        "TPS54331",
        "TPS54331",
        tmp_path / "unused.pdf",
        tmp_path / "out",
        engine="behavioral",
        reinforce=False,
    )
    run = engine._Run(request, engine._StageLog(None))
    run.record = record
    monkeypatch.setattr(engine, "_page_lookup", lambda *_args: lambda _doc, page: pages[page])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a reviewed source must not construct a backend or plan tests")

    monkeypatch.setattr(engine, "build_backend", forbidden)
    monkeypatch.setattr("boardmodeler.authoring.test_planner.plan_bindings", forbidden)
    return run


def test_pipeline_uses_profile_offline_and_still_refuses_uvlo_and_package(tmp_path, monkeypatch):
    run = profile_run(tmp_path, monkeypatch)
    run.extract(None)
    run.bind()
    with pytest.raises(engine._Stop, match="cited input UVTH"):
        run.support_gate()
    support = json.loads((run.out_dir / engine.SUPPORT_RECORD_NAME).read_text())
    assert "cited input UVTH" in support["missing"]
    assert reference.PACKAGE_GAP in support["missing"]
    assert all(not route["allowed"] for route in support["routes"].values())
    assert run._provider_call_fields()["provider_calls"] == 0
    saved = json.loads((run.spec_dir / "requirements.json").read_text())
    assert saved["document"]["reviewed_extraction"]["profile_id"] == reference.PROFILE_ID


def test_package_guard_survives_future_numeric_support_and_saved_input_replay(
    tmp_path, monkeypatch
):
    run = profile_run(tmp_path, monkeypatch)
    run.extract(None)
    run.bind()
    supported = SupportDecision(
        "TPS54331",
        "supported",
        "switching_regulator",
        "peak_current_buck",
        "all numerical tests covered",
        (),
        {name: RouteVerdict(True, "allowed") for name in engine.ENGINES},
    )
    monkeypatch.setattr(
        "boardmodeler.models.support.decide_support", lambda *_args, **_kw: supported
    )
    replay = engine._Run(
        dataclasses.replace(run.request, out_dir=tmp_path / "replay"), engine._StageLog(None)
    )
    saved = json.loads((run.spec_dir / "requirements.json").read_text())
    replay.pin_map = tuple(saved["pin_map"])
    with pytest.raises(engine._Stop, match="unresolved_package"):
        replay.support_gate()
    assert replay.support.state == "unsupported_family"
    assert reference.PACKAGE_GAP in replay.support.missing


def test_supplied_requirements_bypass_reference_profile(tmp_path, monkeypatch):
    run = profile_run(tmp_path, monkeypatch)
    record, pages = source()
    rows, _pins, _provenance = reference.records(record, pages)
    run.request = dataclasses.replace(run.request, requirements_json=tmp_path / "historical.json")
    historical = rows[0].model_copy(
        update={"limits": rows[0].limits.model_copy(update={"typ": 3.5})}
    )
    run.supplied = [historical]
    monkeypatch.setattr(
        reference, "records", lambda *_args: pytest.fail("must preserve supplied rows")
    )
    run.extract(None)
    assert run.requirements == [historical]
    assert run.reviewed_extraction is None
