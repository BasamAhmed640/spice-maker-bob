"""The support gate in the pipeline: blocked, unclassified and unsupported parts stop before
any generation, on every engine, and no engine is a fallback for another."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest
from tests.pipeline import test_make_model as helpers

from boardmodeler.authoring.backends import ScriptedBackend
from boardmodeler.pipeline import make_model as engine
from boardmodeler.pipeline.make_model import MakeModelRequest, make_model
from boardmodeler.simulation.ltspice import LtspiceInstall

FROZEN_DIR = Path(__file__).resolve().parents[2] / "models" / "T1-tps54332" / "spec"
FROZEN_SW_LIBRARY = "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2"


def _neutral_requirements(tmp_path: Path, wording: str) -> Path:
    """The reviewed fixture with every statement reworded, so only the wording identifies it."""
    raw = json.loads(helpers.REQUIREMENTS.read_text(encoding="utf-8"))
    for index, row in enumerate(raw["requirements"]):
        row["statement"] = f"{wording} item {index}"
        if not helpers.DATASHEET.is_file():
            row["origin"] = "TEST_FIXTURE"
            for evidence in row["evidence"]:
                evidence["extraction"] = "synthetic_fixture"
    path = tmp_path / "neutral-requirements.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wording: str, **overrides: object):
    backend = helpers.use_backend(monkeypatch, ScriptedBackend(helpers.template_script()))
    request = helpers.make_request(
        tmp_path, requirements_json=_neutral_requirements(tmp_path, wording), **overrides
    )
    result = make_model(request)
    return result, backend


@pytest.mark.parametrize("engine_name", ["legacy_ai", "behavioral", "pin_only"])
def test_an_unclassified_part_is_refused_on_every_engine_before_any_agent_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_name: str
) -> None:
    result, backend = _run(tmp_path, monkeypatch, "A voltage", engine=engine_name)
    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part: unclassified:")
    assert backend.turns == 0
    assert result.lib_path is None
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["state"] == "unclassified"
    assert record["engine"] == engine_name
    assert not any(route["allowed"] for route in record["routes"].values())


def test_the_behavioral_route_refuses_a_part_with_no_implementation_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, backend = _run(tmp_path, monkeypatch, "Operational amplifier", engine="behavioral")
    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part: unsupported_family:")
    assert backend.turns == 0
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["family"] == "amplifier_comparator"
    assert record["routes"]["pin_only"]["allowed"] is True


def test_pin_only_is_a_separate_limited_route_that_never_becomes_the_agent_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, backend = _run(tmp_path, monkeypatch, "Operational amplifier", engine="pin_only")
    assert result.status == "BLOCKED"
    assert result.detail.startswith("pin_only_unavailable:")
    assert backend.turns == 0
    assert result.lib_path is None


def test_the_gate_and_its_seconds_are_recorded_even_when_the_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch, "A voltage", engine="behavioral")
    timing = json.loads((tmp_path / "out" / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert "gate" in timing["stage_seconds"]
    assert "author" not in timing["stage_seconds"]


def test_an_unknown_engine_or_a_behavioral_quick_check_is_rejected_up_front(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="engine must be one of"):
        make_model(helpers.make_request(tmp_path, engine="magic"))
    with pytest.raises(ValueError, match="verification=full"):
        make_model(helpers.make_request(tmp_path, engine="behavioral", verification="sanity"))


@pytest.mark.ltspice
@pytest.mark.slow
@pytest.mark.skipif(
    not (FROZEN_DIR / "requirements.json").is_file()
    or not (
        Path(os.environ.get("SPICE_MAKER_DATASHEET_DIR", ".")) / "tps54332_datasheet.pdf"
    ).is_file(),
    reason="needs the local frozen TPS54332 spec and SPICE_MAKER_DATASHEET_DIR with its datasheet",
)
def test_the_behavioral_route_builds_a_supported_part_with_no_agent_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ltspice_exe: Path
) -> None:
    def no_backend(request: object) -> object:
        raise AssertionError("the behavioral route must not construct an agent backend")

    def no_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("the behavioral route must not touch the network")

    monkeypatch.setattr(engine, "build_backend", no_backend)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: LtspiceInstall(ltspice_exe, "test"))
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    sheet = Path(os.environ["SPICE_MAKER_DATASHEET_DIR"]) / "tps54332_datasheet.pdf"
    request = MakeModelRequest(
        part="TPS54332DDA",
        subckt="TPS54332DDA",
        datasheet=sheet,
        out_dir=tmp_path / "out",
        backend_name="api",
        requirements_json=FROZEN_DIR / "requirements.json",
        bindings_json=FROZEN_DIR / "bindings.json",
        timeout_s=120.0,
        engine="behavioral",
    )
    result = make_model(request)
    assert result.status in ("PASS", "UNKNOWN", "FAIL"), result.detail
    assert result.lib_path is not None
    assert hashlib.sha256(result.lib_path.read_bytes()).hexdigest() == FROZEN_SW_LIBRARY
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["state"] == "supported" and record["implementation"] == "peak_current_buck"
    timing = json.loads((tmp_path / "out" / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert timing["author_turns"] == 0
    design = json.loads((tmp_path / "out" / engine.DESIGN_RECORD_NAME).read_text(encoding="utf-8"))
    assert design["association"] == "exact"
    assert result.counts["FAIL"] == 4, "the known FAIL rows stay visible"
