"""The verified PowerPAD row governs both card ties and a synthetic alarm."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from boardmodeler.domain.enums import Status
from boardmodeler.schematic.netlist import build_netmap
from boardmodeler.schematic.neutral import ComponentRow, ConnectionRow, NeutralProject, to_circuit
from boardmodeler.simulation.raw import RawFile
from boardmodeler.verification.required_connections import (
    check_required_connection,
    evaluate_pad_alarm_waveform,
    load_required_connection_rule,
    render_pad_diagnostic_deck,
)


def _source(tmp_path: Path) -> Path:
    doc_id = "verified-fixture"
    excerpt = "PowerPAD 9 — GND pin must be connected to the exposed pad for proper operation."
    pin_names = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "PowerPAD")
    pins = [
        {
            "part_id": "TPS54332DDA",
            "name": name,
            "physical_pin": str(index),
            "direction": "ground" if name in {"GND", "PowerPAD"} else "input",
            "connection_requirement": "required",
            "evidence": [
                {
                    "doc_id": doc_id,
                    "excerpt": excerpt if name == "PowerPAD" else name,
                    "page": {"pdf_page": 2, "printed_label": "3"},
                }
            ],
        }
        for index, name in enumerate(pin_names, 1)
    ]
    source = tmp_path / "requirements.json"
    source.write_text(
        json.dumps(
            {
                "document": {"doc_id": doc_id},
                "pin_map": pins,
                "requirements": [
                    {
                        "req_id": "B001_PIN_POWERPAD",
                        "applies_to": "TPS54332DDA",
                        "citation_verified": True,
                        "kind": "CONNECTIVITY",
                        "origin": "DOCUMENT",
                        "status": "active",
                        "statement": "PowerPAD pin 9 is GND; GND pin must be connected to the exposed pad for proper operation.",
                        "evidence": [
                            {
                                "doc_id": doc_id,
                                "excerpt": excerpt,
                                "page": {"pdf_page": 2},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return source


def _card(pad: str | None = "DGND", ground: str | None = "DGND") -> NeutralProject:
    rows = []
    if ground is not None:
        rows.append(ConnectionRow("U1", "7", ground))
    if pad is not None:
        rows.append(ConnectionRow("U1", "9", pad))
    return NeutralProject(
        components=[ComponentRow("U1", part_number="TPS54332DDA")],
        connections=rows,
    )


def _netmap(card: NeutralProject):
    circuit = to_circuit(card, symbols={"U1": tuple(str(index) for index in range(1, 10))})
    return build_netmap(circuit)


def _model(tmp_path: Path) -> Path:
    path = tmp_path / "pad-model.lib"
    path.write_text(
        ".subckt TPS54332DDA BOOT VIN EN SS VSENSE COMP GND PH POWERPAD\n"
        "Rpad_leak POWERPAD GND 1G\n"
        "Bchk_powerpad chk_powerpad GND V=if(abs(V(POWERPAD,GND))>0.1,1,0)\n"
        ".ends TPS54332DDA\n",
        encoding="utf-8",
    )
    return path


def _raw(value: float, alarm: float) -> RawFile:
    t = np.linspace(0, 100e-6, 1001)
    return RawFile(
        path=None,
        plotname="Transient Analysis",
        flags=["real"],
        variables=["time", "V(pad)", "V(xu1:chk_powerpad)"],
        data=np.column_stack((t, np.full_like(t, value), np.full_like(t, alarm))),
        variable_types=["time", "voltage", "voltage"],
    )


def test_clean_card_has_zero_findings_and_cites_pin_pair(tmp_path: Path) -> None:
    rule = load_required_connection_rule(_source(tmp_path))
    assert rule.ground_physical_pin == "7"
    assert rule.pad_physical_pin == "9"
    assert rule.pdf_page_index == 2
    assert rule.printed_page_label == "3"
    assert len(rule.source_sha256) == 64
    card = _card()
    result = check_required_connection(card, rule, netmap=_netmap(card))
    assert result.status == Status.PASS
    assert result.inspected_refdes == ("U1",)
    assert result.findings == ()


@pytest.mark.parametrize(
    ("pad", "ground", "reason"),
    [
        (None, "DGND", "pad_pin_missing"),
        ("NC_09", "DGND", "pad_pin_disconnected"),
        ("VIN", "DGND", "pad_wrong_net"),
        ("DGND", None, "ground_pin_missing"),
    ],
)
def test_missing_or_wrong_pad_reports_exact_net_and_citation(
    tmp_path: Path, pad: str | None, ground: str | None, reason: str
) -> None:
    rule = load_required_connection_rule(_source(tmp_path))
    result = check_required_connection(_card(pad, ground), rule)
    assert result.status == Status.FAIL
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.refdes == "U1"
    assert finding.detail["reason"] == reason
    assert finding.detail["pad_physical_pin"] == "9"
    assert finding.detail["ground_physical_pin"] == "7"
    assert finding.detail["requirement_id"] == "B001_PIN_POWERPAD"
    assert finding.detail["pdf_page_index"] == "2"
    assert finding.detail["pad_net"] == (pad or "<missing>")
    assert finding.detail["ground_net"] == (ground or "<missing>")


def test_card_and_netlist_disagreement_is_not_a_clean_pass(tmp_path: Path) -> None:
    rule = load_required_connection_rule(_source(tmp_path))
    disconnected = _card("NC_09")
    disconnected_result = check_required_connection(
        disconnected, rule, netmap=_netmap(disconnected)
    )
    assert [finding.code for finding in disconnected_result.findings] == [
        "RC001_required_pin_connection"
    ]
    result = check_required_connection(_card(), rule, netmap=_netmap(_card("VIN")))
    assert result.status == Status.FAIL
    assert any(item.code == "RC002_card_netlist_disagreement" for item in result.findings)
    empty = NeutralProject(components=[ComponentRow("U2", part_number="DIFFERENT")])
    assert check_required_connection(empty, rule).status == Status.NOT_APPLICABLE


def test_unverified_source_is_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path)
    record = json.loads(source.read_text(encoding="utf-8"))
    record["requirements"][0]["citation_verified"] = False
    source.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="verified active document citation"):
        load_required_connection_rule(source)


def test_synthetic_diagnostic_differs_only_by_pcb_tie_and_measures_raw(tmp_path: Path) -> None:
    rule = load_required_connection_rule(_source(tmp_path))
    model = _model(tmp_path)
    clean = render_pad_diagnostic_deck(rule, model, clean=True)
    open_pad = render_pad_diagnostic_deck(rule, model, clean=False)
    assert clean.replace("Rpcb pad 0 1m\n", "") == open_pad
    assert "Ipad_diag 0 pad 1e-09" in clean
    assert "XU1 boot vin en ss vsense comp 0 ph pad TPS54332DDA" in clean
    assert "Bpad_alarm" not in clean
    assert ".save V(pad) V(xu1:chk_powerpad)" in clean
    assert "B001_PIN_POWERPAD" in clean
    good = evaluate_pad_alarm_waveform(_raw(1e-12, 0), rule)
    fault = evaluate_pad_alarm_waveform(_raw(1.0, 1), rule)
    assert good.status == fault.status == "MEASURED"
    assert good.alarm_active is False
    assert fault.alarm_active is True
    assert evaluate_pad_alarm_waveform(_raw(1.0, 0), rule).status == "UNKNOWN"
