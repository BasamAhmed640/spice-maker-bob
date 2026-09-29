"""The fixed qualification must judge the exact library delivered to the user."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from tests.models.test_peak_current_buck import _basis, _spec

from boardmodeler.authoring import qualification
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.pipeline import make_model as engine


@pytest.mark.parametrize("alter_saved_design", [False, True])
def test_qualification_freezes_before_publish_and_judges_delivered_bytes(
    tmp_path, monkeypatch, alter_saved_design
):
    spec = _spec(*_basis())
    seed = seed_from_spec(spec)
    assert seed is not None
    request = engine.MakeModelRequest(
        spec.part,
        spec.subckt,
        tmp_path / "datasheet.pdf",
        tmp_path / "out",
        engine="behavioral",
    )
    run = engine._Run(request, engine._StageLog(None))
    run.spec = spec
    run.support = SimpleNamespace(implementation="peak_current_buck")
    requirements = run.spec_dir / engine.REQUIREMENTS_NAME
    run._write_text(requirements, '{"requirements": []}\n')
    plan = SimpleNamespace(to_json=lambda: '{"frozen": true}\n')

    def freeze(_spec, path):
        assert _spec is spec
        assert path == requirements
        assert run.lib_path is None
        return plan

    monkeypatch.setattr(qualification, "build_buck_qualification_plan", freeze)
    run.freeze_qualification()
    assert run.qualification_plan is plan
    assert json.loads((run.spec_dir / engine.QUALIFICATION_PLAN_NAME).read_text()) == {
        "frozen": True
    }

    source = run.workdir / "model" / f"{spec.subckt}.lib"
    seed.write(source)
    run.template_design = seed.design
    run.template_seed = seed.payload()
    run.template_seed_bytes = source.read_bytes()
    run.report = engine.HarnessReport(
        part=spec.part,
        model_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        spec_digest=spec.digest(),
        outcomes=(),
    )
    run._publish(source, [])
    assert run.lib_path is not None
    delivered_sha = hashlib.sha256(run.lib_path.read_bytes()).hexdigest()
    design = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert design["association"] == "exact"
    if alter_saved_design:
        next(item for item in design["design"]["parameters"] if item["name"] == "VREF")["value"] = (
            0.72
        )
        run._write_json(run.out_dir / engine.DESIGN_RECORD_NAME, design)

    monkeypatch.setattr(engine, "locate", lambda: SimpleNamespace(path=tmp_path / "LTspice.exe"))

    def judge(actual_plan, model_library, _ltspice, workdir, **kwargs):
        assert actual_plan is plan
        assert model_library == run.lib_path
        assert workdir == run.workdir / "qualification"
        assert kwargs["design_sha256"] == (None if alter_saved_design else design["design_sha256"])
        assert hashlib.sha256(model_library.read_bytes()).hexdigest() == delivered_sha
        return {
            "record_kind": "qualification_report",
            "status": "UNKNOWN",
            "family_qualified": False,
            "model_sha256": delivered_sha,
            "counts": {"PASS": 4, "FAIL": 0, "UNKNOWN": 12, "BLOCKED": 0},
            "simulation_seconds": 45.0,
        }

    monkeypatch.setattr(qualification, "run_qualification", judge)
    run.qualify(None)
    report = json.loads((run.out_dir / engine.QUALIFICATION_REPORT_NAME).read_text())
    assert report["model_sha256"] == delivered_sha
    assert report["family_qualified"] is False
    assert "passing subset does not qualify the whole family" in run.card_path.read_text()


@pytest.mark.parametrize(
    ("row_status", "fixed_status", "expected"),
    [
        ("PASS", "UNKNOWN", "UNKNOWN"),
        ("PASS", "BLOCKED", "BLOCKED"),
        ("UNKNOWN", "BLOCKED", "BLOCKED"),
        ("FAIL", "BLOCKED", "BLOCKED"),
        ("UNKNOWN", "FAIL", "FAIL"),
        ("FAIL", "UNKNOWN", "FAIL"),
    ],
)
def test_independent_qualification_controls_the_public_verdict(row_status, fixed_status, expected):
    report = {
        "status": fixed_status,
        "counts": {"PASS": 3, "FAIL": 1, "UNKNOWN": 12, "BLOCKED": 0},
    }
    status, detail = engine._apply_qualification_status(row_status, "row result", report)
    assert status == expected
    if fixed_status == "FAIL":
        assert "independent fixed qualification measured a failure" in detail
    if row_status == "PASS" and fixed_status == "UNKNOWN":
        assert "missing mandatory checks" in detail
    if fixed_status == "BLOCKED":
        assert "required simulation refusal" in detail
        assert "row result" in detail
