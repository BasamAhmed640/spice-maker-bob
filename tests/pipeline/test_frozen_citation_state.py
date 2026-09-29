"""Synthetic page text proves replay flags cannot certify qualification sources."""

from __future__ import annotations

import json

import pytest
from tests.authoring.test_qualification import _inputs
from tests.requirements.test_model import make_evidence, make_requirement

from boardmodeler.authoring.buck_qualification import ROW_IDS
from boardmodeler.authoring.qualification import build_buck_qualification_plan
from boardmodeler.domain.enums import RequirementOrigin
from boardmodeler.domain.records import DocumentRecord, Limit
from boardmodeler.pipeline import make_model as engine


def _run(tmp_path):
    spec, _source = _inputs(tmp_path)
    run = engine._Run(
        engine.MakeModelRequest(
            spec.part, spec.subckt, tmp_path / "synthetic.pdf", tmp_path / "out"
        ),
        engine._StageLog(None),
    )
    run.spec = spec
    run.record = DocumentRecord(
        doc_id=spec.doc_id,
        title="Synthetic citation test",
        doc_type="synthetic_contract",
        file_hash="a" * 64,
        provenance="synthetic_fixture",
        page_count=20,
        text_extraction="embedded",
    )
    run.requirements = [
        make_requirement(
            req_id=row.char_id,
            statement=row.statement,
            limits=Limit(min=row.min_value, typ=row.typ_value, max=row.max_value, unit=row.unit),
            evidence=[make_evidence(doc_id=spec.doc_id, excerpt=row.excerpt, page=row.source_page)],
        ).model_copy(update={"citation_verified": True})
        for row in spec.characteristics
    ]
    return run


def _pages(run, *, omitted=None):
    def lookup(doc_id, page):
        assert doc_id == run.spec.doc_id
        return "\n".join(
            row.excerpt
            for row in run.spec.characteristics
            if row.source_page == page and row.char_id != omitted
        )

    return lookup


@pytest.mark.parametrize("case", ["shutdown_iq", "operating_iq"])
def test_failed_current_IQ_citation_cannot_enter_frozen_qualification(tmp_path, case):
    run = _run(tmp_path)
    stale_rows = run.requirements.copy()
    omitted = ROW_IDS[case]
    assert all(row.citation_verified for row in stale_rows)
    run.citation_lookup = _pages(run, omitted=omitted)
    run._verify_citations()
    assert set(run.unverified) == {omitted}
    assert next(row for row in run.requirements if row.req_id == omitted).citation_verified is False
    assert all(row.citation_verified for row in stale_rows), "input objects remain untouched"
    source = run._write_requirements()
    saved = {row["req_id"]: row for row in json.loads(source.read_text())["requirements"]}
    assert saved[omitted]["citation_verified"] is False
    plan = build_buck_qualification_plan(run.spec, source)
    check = next(check for check in plan.checks if check.check_id == case)
    assert check.kind == "gap" and "missing verified operating citation" in check.reason

    # A later positive check must also replace a stale false result.
    run.citation_lookup = _pages(run)
    run._verify_citations()
    assert run.unverified == {}
    assert all(row.citation_verified for row in run.requirements)
    repaired_source = run._write_requirements()
    new_plan = build_buck_qualification_plan(run.spec, repaired_source)
    assert next(check for check in new_plan.checks if check.check_id == case).kind == "nominal"


def test_missing_current_document_clears_all_document_flags_but_preserves_other_origins(tmp_path):
    run = _run(tmp_path)
    user = run.requirements[0].model_copy(
        update={"req_id": "USER_ROW", "origin": RequirementOrigin.USER}
    )
    fixture = run.requirements[0].model_copy(
        update={
            "req_id": "FIXTURE_ROW",
            "origin": RequirementOrigin.TEST_FIXTURE,
            "citation_verified": False,
        }
    )
    run.requirements.extend((user, fixture))
    run.record = None
    run._verify_citations()
    assert all(
        not row.citation_verified
        for row in run.requirements
        if row.origin is RequirementOrigin.DOCUMENT
    )
    assert run.requirements[-2] is user and user.citation_verified
    assert run.requirements[-1] is fixture and not fixture.citation_verified
    saved = json.loads(run._write_requirements().read_text())["requirements"]
    assert all(not row["citation_verified"] for row in saved if row["origin"] == "DOCUMENT")


def test_missing_verifier_result_cannot_preserve_a_true_flag(tmp_path, monkeypatch):
    run = _run(tmp_path)
    run.citation_lookup = _pages(run)
    monkeypatch.setattr(engine, "verify_citations", lambda *args, **kwargs: {})
    run._verify_citations()
    assert len(run.unverified) == len(run.requirements)
    assert all(not row.citation_verified for row in run.requirements)
