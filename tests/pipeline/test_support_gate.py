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
from boardmodeler.authoring.viability import GateCheck, GateReport
from boardmodeler.domain.enums import Status
from boardmodeler.pipeline import make_model as engine
from boardmodeler.pipeline.make_model import MakeModelRequest, make_model
from boardmodeler.simulation.ltspice import LtspiceInstall

FROZEN_DIR = Path(__file__).resolve().parents[2] / "models" / "T1-tps54332" / "spec"
FROZEN_SW_LIBRARY = "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2"


def _neutral_requirements(tmp_path: Path, wording: str, pins: list | None = None) -> Path:
    """The reviewed fixture with every statement reworded, so only the wording identifies it."""
    raw = json.loads(helpers.REQUIREMENTS.read_text(encoding="utf-8"))
    for index, row in enumerate(raw["requirements"]):
        row["statement"] = f"{wording} item {index}"
        row["origin"] = "TEST_FIXTURE"
        for evidence in row["evidence"]:
            evidence["extraction"] = "synthetic_fixture"
    if pins is not None:
        raw["pin_map"] = pins
    path = tmp_path / "neutral-requirements.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


NEUTRAL_HEAD = "TEST_FIXTURE: a first page that says nothing about the part"


def _datasheet(tmp_path: Path, head: str) -> Path:
    """A stand-in datasheet whose first page says only ``head``, with the fixture page count."""
    from reportlab.pdfgen import canvas

    pages = json.loads(helpers.REQUIREMENTS.read_text(encoding="utf-8"))["document"]["page_count"]
    path = tmp_path / "gate_datasheet.pdf"
    sheet = canvas.Canvas(str(path))
    for page in range(pages):
        sheet.drawString(30, 810, head if page == 0 else f"page {page + 1}")
        sheet.showPage()
    sheet.save()
    return path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wording: str,
    head: str = NEUTRAL_HEAD,
    pins: list | None = None,
    **overrides: object,
):
    helpers.stub_pinout_confirmation(monkeypatch)
    backend = helpers.use_backend(monkeypatch, ScriptedBackend(helpers.template_script()))
    request = helpers.make_request(
        tmp_path,
        requirements_json=_neutral_requirements(tmp_path, wording, pins),
        datasheet=_datasheet(tmp_path, head),
        **overrides,
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
    result, backend = _run(
        tmp_path, monkeypatch, "Input offset voltage and slew rate", engine="behavioral"
    )
    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part: unsupported_family:")
    assert backend.turns == 0
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["family"] == "amplifier_comparator"
    assert record["routes"]["pin_only"]["allowed"] is True


AMPLIFIER_WORDING = "Input offset voltage and slew rate"


def _pin(name: str, direction: str, number: int, **extra: object) -> dict:
    return {
        "part_id": "X1",
        "physical_pin": str(number),
        "name": name,
        "function": "test pin",
        "polarity": "not_applicable",
        "direction": direction,
        "output_topology": extra.pop("topology", "unknown"),
        "connection_requirement": extra.pop("requirement", "optional"),
        **extra,
    }


PIN_TABLE = [
    _pin("VIN", "power", 1),
    _pin("GND", "ground", 2),
    _pin("EN", "input", 3),
    _pin("PG", "output", 4, topology="open_drain"),
    _pin("EP", "ground", 5, requirement="required"),
]


def test_pin_only_without_a_pin_table_is_refused_and_no_other_route_takes_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, backend = _run(tmp_path, monkeypatch, AMPLIFIER_WORDING, engine="pin_only")
    assert result.status == "BLOCKED"
    assert result.detail.startswith("pin_only_no_pin_table:")
    assert backend.turns == 0
    assert result.lib_path is None


def test_pin_only_refuses_a_pin_table_with_two_supply_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = [
        _pin("VCCA", "power", 1, supply_domain="A side"),
        _pin("VCCB", "power", 2, supply_domain="B side"),
        _pin("GND", "ground", 3),
    ]
    result, backend = _run(tmp_path, monkeypatch, AMPLIFIER_WORDING, pins=pins, engine="pin_only")
    assert result.status == "BLOCKED"
    assert result.detail.startswith("pin_only_multiple_rails:")
    assert backend.turns == 0 and result.lib_path is None


def test_pin_only_without_ltspice_is_blocked_and_never_replaced_by_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "locate", lambda explicit=None: None)
    result, backend = _run(
        tmp_path, monkeypatch, AMPLIFIER_WORDING, pins=PIN_TABLE, engine="pin_only"
    )
    assert result.status == "BLOCKED"
    assert result.detail == engine.LTSPICE_MISSING
    assert backend.turns == 0 and result.lib_path is None


def test_the_other_engines_never_write_a_pin_only_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "locate", lambda explicit=None: None)
    for name in ("behavioral", "legacy_ai"):
        sub = tmp_path / name
        sub.mkdir()
        result, _backend = _run(
            sub, monkeypatch, AMPLIFIER_WORDING, pins=PIN_TABLE, engine=name, family=None
        )
        out = sub / "out"
        assert not (out / "pin-only-report.json").exists()
        assert result.detail is not None and not result.detail.startswith("pin_only:")


def test_the_gate_and_its_seconds_are_recorded_even_when_the_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch, "A voltage", engine="behavioral")
    timing = json.loads((tmp_path / "out" / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert "gate" in timing["stage_seconds"]
    assert "author" not in timing["stage_seconds"]
    assert timing["route"] == "refused_before_authoring"
    assert timing["provider_calls"] == 0 and timing["provider_calls_complete"] is True


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


def test_the_first_page_names_the_family_when_the_rows_and_the_title_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, backend = _run(
        tmp_path,
        monkeypatch,
        "A voltage",
        head="LM358 Industry-Standard Dual Operational Amplifiers",
        engine="behavioral",
    )
    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part: unsupported_family:")
    assert backend.turns == 0
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["family"] == "amplifier_comparator"
    assert "first page" in record["identified_from"]


def test_a_family_named_by_the_operator_is_recorded_and_still_never_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, backend = _run(
        tmp_path, monkeypatch, "A voltage", engine="behavioral", family="linear_regulator"
    )
    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part: unsupported_family:")
    assert backend.turns == 0
    record = json.loads((tmp_path / "out" / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["family"] == "linear_regulator"
    assert record["identified_from"] == "declared by the operator"
    assert record["state"] == "unsupported_family"


def test_an_unknown_family_option_is_rejected_up_front(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="family must be one of"):
        make_model(helpers.make_request(tmp_path, family="quantum_widget"))


@pytest.mark.ltspice
def test_pin_only_builds_a_limited_model_that_is_judged_by_the_gate_and_is_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ltspice_exe: Path
) -> None:
    monkeypatch.setattr(engine, "locate", lambda explicit=None: LtspiceInstall(ltspice_exe, "test"))
    result, backend = _run(
        tmp_path, monkeypatch, AMPLIFIER_WORDING, pins=PIN_TABLE, engine="pin_only"
    )
    out = tmp_path / "out"
    assert result.status == "UNKNOWN", result.detail
    assert result.detail.startswith("pin_only:")
    assert backend.turns == 0
    assert result.counts["PASS"] == 0 and result.counts["FAIL"] == 0
    assert result.lib_path is not None and result.lib_path.is_file()
    report = json.loads((out / "pin-only-report.json").read_text(encoding="utf-8"))
    statuses = {check["id"]: check["status"] for check in report["gate_checks"]}
    assert "FAIL" not in statuses.values() and "BLOCKED" not in statuses.values()
    assert statuses["alarm_tie_fires_on_fault"] == "PASS", "the exposed-pad alarm is proven"
    assert statuses["alarm_ovl_fires_on_fault"] == "PASS"
    assert report["verdict"] == "UNJUDGED" and report["rail"] == "VIN"
    card = (out / "MODEL_CARD.md").read_text(encoding="utf-8")
    assert "## Pin-only model (limited)" in card and "models no function" in card
    timing = json.loads((out / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert timing["route"] == "pin_only_shell" and timing["author_turns"] == 0
    support = json.loads((out / engine.SUPPORT_RECORD_NAME).read_text(encoding="utf-8"))
    assert support["engine"] == "pin_only" and support["routes"]["pin_only"]["allowed"]


@pytest.mark.parametrize("failure_status", [Status.FAIL, Status.BLOCKED])
def test_a_withheld_pin_only_model_keeps_the_gate_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_status: Status
) -> None:
    monkeypatch.setattr(
        engine, "locate", lambda explicit=None: LtspiceInstall(Path(__file__), "test")
    )
    check = GateCheck(
        "current_conservation",
        failure_status,
        "the pin currents do not balance",
        {"residual_a": 2e-9},
    )
    monkeypatch.setattr(
        "boardmodeler.authoring.viability.run_gate",
        lambda *args, **kwargs: GateReport("X1", [check], runs=1),
    )

    result, backend = _run(
        tmp_path, monkeypatch, AMPLIFIER_WORDING, pins=PIN_TABLE, engine="pin_only"
    )
    out = tmp_path / "out"
    assert result.status == "BLOCKED"
    assert result.detail.startswith("pin_only_model_not_viable:")
    assert backend.turns == 0
    assert result.lib_path is None and result.asy_path is None and result.card_path is None
    assert not any(out.glob("*.lib")) and not any(out.glob("*.asy"))
    assert not (out / "MODEL_CARD.md").exists()
    report = json.loads((out / "pin-only-report.json").read_text(encoding="utf-8"))
    assert report["gate_checks"] == [check.as_dict()]
    assert report["gate_status"] == ("FAIL" if failure_status is Status.FAIL else "UNKNOWN")
    assert report["verdict"] == "UNJUDGED"
    timing = json.loads((out / engine.TIMING_NAME).read_text(encoding="utf-8"))
    assert timing["route"] == "pin_only_shell"
    assert timing["provider_calls"] == 0 and timing["provider_calls_complete"] is True


@pytest.mark.parametrize("route", ["behavioral", "pin_only"])
def test_the_local_routes_never_ask_the_agent_to_plan_test_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    def no_planner(*args: object, **kwargs: object) -> None:
        raise AssertionError("the agent test planner must not run on a local route")

    monkeypatch.setattr("boardmodeler.authoring.test_planner.plan_bindings", no_planner)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: None)
    result, backend = _run(
        tmp_path,
        monkeypatch,
        AMPLIFIER_WORDING,
        pins=PIN_TABLE,
        engine=route,
        bindings_json=None,
        backend_name="api",
    )
    assert backend.turns == 0
    assert "binding computed from the reviewed keyword table" in " ".join(
        event.detail for event in result.stages if event.stage == "bind"
    )


def test_plan_tests_is_the_explicit_opt_in_to_the_agent_planner_on_a_local_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def planner(*args: object, **kwargs: object) -> None:
        raise ValueError("planner was asked")

    monkeypatch.setattr("boardmodeler.authoring.test_planner.plan_bindings", planner)
    result, _backend = _run(
        tmp_path,
        monkeypatch,
        AMPLIFIER_WORDING,
        pins=PIN_TABLE,
        engine="behavioral",
        bindings_json=None,
        backend_name="api",
        plan_tests=True,
    )
    assert result.status == "BLOCKED" and "planner was asked" in result.detail
