"""The behavioral route builds a second, materially different family with no agent and no network.

A dual op amp from the local frozen LM358 rows (git-ignored): the design is read from cited rows, rendered on the
pin shell, and judged by the op-amp probes in real LTspice. Support is not a pass; the counts here
are what LTspice measured.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from boardmodeler.pipeline import make_model as engine
from boardmodeler.pipeline.make_model import MakeModelRequest, make_model
from boardmodeler.simulation.ltspice import LtspiceInstall

SPEC_DIR = Path(__file__).resolve().parents[2] / "models" / "L1-lm358" / "spec"

PIN_FUNCTIONS = {
    "OUT1": ("output", "push_pull", "Output of amplifier 1"),
    "IN1M": ("input", "input_only", "Inverting input of amplifier 1"),
    "IN1P": ("input", "input_only", "Non-inverting input of amplifier 1"),
    "VEE": ("ground", "power", "Ground or negative supply"),
    "IN2P": ("input", "input_only", "Non-inverting input of amplifier 2"),
    "IN2M": ("input", "input_only", "Inverting input of amplifier 2"),
    "OUT2": ("output", "push_pull", "Output of amplifier 2"),
    "VCC": ("power", "power", "Positive supply"),
}


def _full_requirements(target: Path) -> Path:
    """The frozen rows with the pin table written out in full (the file keeps names only)."""
    raw = json.loads((SPEC_DIR / "requirements.json").read_text(encoding="utf-8"))
    raw["pin_map"] = [
        {
            "part_id": "LM358",
            "physical_pin": pin["physical_pin"],
            "name": pin["name"],
            "function": PIN_FUNCTIONS[pin["name"]][2],
            "polarity": "not_applicable",
            "direction": PIN_FUNCTIONS[pin["name"]][0],
            "output_topology": PIN_FUNCTIONS[pin["name"]][1],
            "connection_requirement": "required",
            "mapped_symbol_pin": pin["name"],
        }
        for pin in raw["pin_map"]
    ]
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


@pytest.mark.ltspice
@pytest.mark.slow
@pytest.mark.skipif(
    not (SPEC_DIR / "requirements.json").is_file()
    or not (
        Path(os.environ.get("SPICE_MAKER_DATASHEET_DIR", ".")) / "lm358_datasheet.pdf"
    ).is_file(),
    reason="needs the local frozen LM358 rows and SPICE_MAKER_DATASHEET_DIR with lm358_datasheet.pdf",
)
def test_the_behavioral_route_builds_a_dual_op_amp_with_no_agent_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ltspice_exe: Path
) -> None:
    def no_backend(request: object) -> object:
        raise AssertionError("the behavioral route must not construct an agent backend")

    def no_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("the behavioral route must not touch the network")

    monkeypatch.setattr(engine, "build_backend", no_backend)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: LtspiceInstall(ltspice_exe, "test"))
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    request = MakeModelRequest(
        part="LM358",
        subckt="LM358",
        datasheet=Path(os.environ["SPICE_MAKER_DATASHEET_DIR"]) / "lm358_datasheet.pdf",
        out_dir=tmp_path / "out",
        backend_name="api",
        requirements_json=_full_requirements(tmp_path / "lm358-requirements.json"),
        bindings_json=SPEC_DIR / "bindings.json",
        timeout_s=120.0,
        engine="behavioral",
    )
    result = make_model(request)
    out = tmp_path / "out"
    assert result.lib_path is not None
    assert result.status in ("PASS", "UNKNOWN", "FAIL"), result.detail
    assert result.counts["FAIL"] == 0 and result.counts["PASS"] == 32, result.counts
    support = json.loads((out / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert support["state"] == "supported" and support["implementation"] == "dual_op_amp"
    timing = json.loads((out / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert timing["author_turns"] == 0 and timing["route"] == "op_amp_template_seed"
    record = json.loads((out / engine.DESIGN_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["association"] == "exact"
    assert record["design"]["record_kind"] == "op_amp_design"
    delivered = hashlib.sha256(result.lib_path.read_bytes()).hexdigest()
    assert record["delivered_library_sha256"] == delivered
    card = (out / "MODEL_CARD.md").read_text(encoding="utf-8")
    assert "Op amp template parameter origins" in card


def test_the_local_routes_do_not_need_the_agent_network_switch() -> None:
    def request(engine: str) -> MakeModelRequest:
        return MakeModelRequest(
            part="LM358",
            subckt="LM358",
            datasheet=Path("x.pdf"),
            out_dir=Path("out"),
            engine=engine,
        )

    assert engine._author_needs_network(request("legacy_ai")) is True
    assert engine._author_needs_network(request("behavioral")) is False
    assert engine._author_needs_network(request("pin_only")) is False


@pytest.mark.ltspice
@pytest.mark.slow
@pytest.mark.skipif(
    not (Path(os.environ.get("SPICE_MAKER_DATASHEET_DIR", ".")) / "lm358_datasheet.pdf").is_file(),
    reason="needs SPICE_MAKER_DATASHEET_DIR with the TI lm358_datasheet.pdf",
)
def test_the_lm358_datasheet_alone_builds_with_the_network_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ltspice_exe: Path
) -> None:
    """Datasheet PDF and part number in, judged model out: no rows supplied, no agent, no network."""

    def no_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError("the local route must not touch the network")

    monkeypatch.setattr(engine, "internet_allowed", lambda: False)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: LtspiceInstall(ltspice_exe, "test"))
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    result = make_model(
        MakeModelRequest(
            part="LM358",
            subckt="LM358",
            datasheet=Path(os.environ["SPICE_MAKER_DATASHEET_DIR"]) / "lm358_datasheet.pdf",
            out_dir=tmp_path / "out",
            timeout_s=120.0,
            engine="behavioral",
        )
    )
    assert result.status == "PASS", result.detail
    assert result.counts["PASS"] == 32 and result.counts["FAIL"] == 0
    timing = json.loads((tmp_path / "out" / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert timing["author_turns"] == 0 and timing["route"] == "op_amp_template_seed"
