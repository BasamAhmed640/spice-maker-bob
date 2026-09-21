"""Structural checks and quick-mode control flow using synthetic circuit fixtures."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from boardmodeler.authoring.backends import AuthorResult
from boardmodeler.authoring.loop import prepare_workdir
from boardmodeler.authoring.sanity import author_model, check_model
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.domain.records import Requirement
from boardmodeler.pipeline import make_model as engine

PINS = (
    {"name": "IN", "physical_pin": "1", "direction": "input"},
    {"name": "OUT", "physical_pin": "2", "direction": "output"},
)
VALID = "* TEST_FIXTURE: resistor, not device data\n.subckt TEST IN OUT\nR1 IN OUT 1k\n.ends TEST\n"


class Backend:
    name = "synthetic-test"

    def __init__(self, text=VALID, tamper=False):
        self.text, self.tamper, self.calls = text, tamper, 0

    def availability(self):
        return True, "test fixture"

    def author(self, request, cancel):
        self.calls += 1
        if self.text:
            (request.model_dir / "TEST.lib").write_text(self.text, encoding="utf-8")
        if self.tamper:
            (request.workdir / "spec/characteristics.json").write_text("{}", encoding="utf-8")
        return AuthorResult(True, "synthetic circuit written", {}, "", None)


@pytest.mark.parametrize(
    "text, error",
    [
        (VALID.replace(".ends TEST", ""), "missing .ends"),
        (VALID.replace("R1 IN OUT 1k", "R1 IN OUT 1k\nR1 IN OUT 2k"), "duplicate component"),
        (VALID.replace("IN OUT\nR1", "IN BAD\nR1"), "physical pin map"),
        (VALID.replace("R1 IN OUT 1k", "X1 IN OUT MISSING"), "unresolved subcircuit"),
        (VALID.replace("R1 IN OUT 1k", '.include "outside.lib"'), "external library"),
        (VALID.replace("R1 IN OUT 1k", "R1 IN OUT {1k"), "braces"),
    ],
)
def test_structural_failures(tmp_path, text, error):
    path = tmp_path / "TEST.lib"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        check_model(path, "TEST", PINS)


def test_internal_subcircuits_and_parameters(tmp_path):
    path = tmp_path / "TEST.lib"
    path.write_text(
        ".subckt TEST IN OUT\nX1 IN OUT CHILD gain = {2}\n.ends TEST\n"
        ".subckt CHILD A B params: gain=1\nR1 A B {gain}\n.ends CHILD\n",
        encoding="utf-8",
    )
    result = check_model(path, "TEST", PINS)
    assert result["electrical_accuracy_verified"] is False
    assert result["simulation_run"] is False


def test_cache_requires_matching_spec_and_model(tmp_path):
    spec = SpecSet("TEST", "TEST", "TEST_FIXTURE", (), PINS)
    backend = Backend()
    prepare_workdir(spec=spec, subckt="TEST", workdir=tmp_path)
    _, count = author_model(spec, backend, tmp_path, None, lambda _: None, {})
    assert count == 1
    assert author_model(spec, backend, tmp_path, None, lambda _: None, {})[1] == 0
    changed = replace(spec, doc_id="ANOTHER_TEST_FIXTURE")
    prepare_workdir(spec=changed, subckt="TEST", workdir=tmp_path)
    assert author_model(changed, backend, tmp_path, None, lambda _: None, {})[1] == 1
    (tmp_path / "model/TEST.lib").write_text(VALID.replace("1k", "2k"), encoding="utf-8")
    assert author_model(changed, backend, tmp_path, None, lambda _: None, {})[1] == 1
    assert backend.calls == 3


def test_repairs_are_bounded_and_spec_tampering_stops(tmp_path):
    spec = SpecSet("TEST", "TEST", "TEST_FIXTURE", (), PINS)
    prepare_workdir(spec=spec, subckt="TEST", workdir=tmp_path)
    broken = Backend(VALID.replace(".ends TEST", ""))
    with pytest.raises(ValueError, match="sanity_check_failed"):
        author_model(spec, broken, tmp_path, None, lambda _: None, {})
    assert broken.calls == 2
    assert not (tmp_path / "model/TEST.lib").exists()
    with pytest.raises(ValueError, match="spec_tampered"):
        author_model(spec, Backend(tamper=True), tmp_path, None, lambda _: None, {})


@pytest.mark.parametrize("write_model", [True, False])
def test_quick_pipeline_skips_planner_simulator_and_never_claims_accuracy(
    tmp_path, monkeypatch, write_model
):
    from boardmodeler.authoring import test_planner

    backend = Backend(VALID if write_model else None)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/regulator/tps54320/requirements.json"
    rows = [
        Requirement.model_validate(r)
        for r in json.loads(fixture.read_text(encoding="utf-8"))["requirements"][:2]
    ]

    def extract(run, cancel):
        run.requirements, run.pin_map = rows, PINS

    def forbidden(*args, **kwargs):
        pytest.fail("quick mode invoked test planning, reinforcement, or simulation")

    monkeypatch.setattr(engine._Run, "read", lambda self: None)
    monkeypatch.setattr(engine._Run, "extract", extract)
    monkeypatch.setattr(engine, "build_backend", lambda request: backend)
    monkeypatch.setattr(engine, "locate", forbidden)
    monkeypatch.setattr(engine, "build_model", forbidden)
    monkeypatch.setattr(engine._Run, "_gather_supporting_material", forbidden)
    monkeypatch.setattr(test_planner, "plan_bindings", forbidden)
    out = tmp_path / "out"
    old = out / "build/model/TEST.lib"
    old.parent.mkdir(parents=True)
    old.write_text(VALID, encoding="utf-8")
    request = engine.MakeModelRequest(
        "TEST", "TEST", tmp_path / "fake.pdf", out, verification="sanity"
    )
    result = engine.make_model(request)
    assert result.status == "UNKNOWN"
    assert result.counts["PASS"] == 0
    assert all(row.status == "UNKNOWN" for row in result.rows)
    if write_model:
        assert result.lib_path and result.asy_path
        assert "electrical accuracy unverified" in result.card_path.read_text(encoding="utf-8")
        assert json.loads((out / "harness-report.json").read_text())["outcomes"] == []
        assert backend.calls == 1
    else:
        assert result.lib_path is None, (
            "an old candidate must not be published after an empty reply"
        )
        assert backend.calls == 2
