"""Quick mode must never publish a model the real simulator rejected.

Marked ``ltspice``: a real LTspice is required, and the test is skipped where none is
installed. The case here is the one that escaped: LTspice rejects a deck for a reason no
keyword list anticipated (``No such node.``), the bounded load check must say so, and the
build must publish nothing rather than offer an unusable model.
"""

import json
from pathlib import Path

import pytest

from boardmodeler.authoring.backends import AuthorResult
from boardmodeler.pipeline import make_model as engine

pytestmark = pytest.mark.ltspice

PART, SUBCKT = "TEST", "TEST"
PINS = (
    {"name": "IN", "physical_pin": "1", "direction": "input"},
    {"name": "OUT", "physical_pin": "2", "direction": "output"},
)
# A behavioral source that reads a node the subcircuit never declares. LTspice rejects
# the instantiated model instead of simulating it.
UNDECLARED_NODE = (
    "* TEST_FIXTURE\n.subckt TEST IN OUT\nB1 OUT 0 I = 1m * V(NEVER_DECLARED)\n.ends TEST\n"
)


class Backend:
    name = "synthetic-test"

    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    def availability(self):
        return True, "test fixture"

    def author(self, request, cancel):
        self.calls += 1
        (request.model_dir / f"{SUBCKT}.lib").write_text(self.text, encoding="utf-8")
        return AuthorResult(True, "synthetic circuit written", {}, "", None)


def test_a_model_the_real_simulator_rejects_is_never_published(tmp_path, monkeypatch):
    from boardmodeler.authoring import test_planner
    from boardmodeler.domain.records import Requirement
    from boardmodeler.simulation.ltspice import locate

    if locate() is None:
        pytest.skip("LTspice is not installed")

    backend = Backend(UNDECLARED_NODE)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/regulator/tps54320/requirements.json"
    rows = [
        Requirement.model_validate(r)
        for r in json.loads(fixture.read_text(encoding="utf-8"))["requirements"][:2]
    ]

    def extract(run, cancel):
        run.requirements, run.pin_map = rows, PINS

    def forbidden(*args, **kwargs):
        pytest.fail("quick mode invoked test planning or the numerical harness")

    monkeypatch.setattr(engine._Run, "read", lambda self: None)
    monkeypatch.setattr(engine._Run, "extract", extract)
    monkeypatch.setattr(engine, "build_backend", lambda request: backend)
    monkeypatch.setattr(engine, "build_model", forbidden)
    monkeypatch.setattr(engine._Run, "_gather_supporting_material", forbidden)
    monkeypatch.setattr(test_planner, "plan_bindings", forbidden)

    out = tmp_path / "out"
    request = engine.MakeModelRequest(
        PART, SUBCKT, tmp_path / "fake.pdf", out, verification="sanity"
    )
    result = engine.make_model(request)

    assert result.lib_path is None, f"a rejected model must not be published: {result.detail}"
    assert not list(out.glob("*.lib")), "no library belongs in the deliverable folder"
    assert "rejected" in result.detail, result.detail
    assert backend.calls == 2, "one draft plus the single bounded repair"
    assert result.status == "UNKNOWN"
    assert not (out / "sanity-report.json").exists(), "a refused build publishes no report"
