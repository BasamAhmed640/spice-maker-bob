"""Published physical buck examples must match the model's actual pin order."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from boardmodeler.authoring import card
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import read_raw


@pytest.mark.parametrize(
    ("ports", "instance"),
    [
        (
            "PH GND COMP VSENSE SS EN VIN BOOT POWERPAD",
            "XU1 ph 0 comp vsense ss en vin boot 0 BUCK",
        ),
        (
            "BOOT VIN EN SS VSENSE COMP GND PH",
            "XU1 boot vin en ss vsense comp 0 ph BUCK",
        ),
    ],
)
def test_physical_buck_deck_wires_actual_ports_and_power_stage(
    tmp_path, monkeypatch, ports, instance
) -> None:
    (tmp_path / "BUCK.lib").write_text(f".subckt BUCK {ports}\n.ends BUCK\n", encoding="utf-8")
    monkeypatch.setattr(card, "render_card", lambda **kwargs: "example card")
    spec = SimpleNamespace(covered=lambda: [])

    card.write_deliverables(out_dir=tmp_path, part="BUCK", subckt="BUCK", spec=spec, report=None)

    deck = (tmp_path / "example.cir").read_text(encoding="utf-8")
    assert instance in deck
    assert "Dcatch 0 ph DCATCH" in deck
    assert "CBOOT boot ph 100n" in deck
    assert "RCOMP comp comp_mid 75k" in deck
    assert "RFB1 out vsense 15k" in deck
    assert "RFB2 vsense 0 4.75k" in deck
    assert "ILOAD" not in deck


def _buck_seed_spec() -> SpecSet:
    ports = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "POWERPAD")
    return SpecSet(
        part="BUCK",
        subckt="BUCK",
        doc_id="buck-example-smoke",
        pin_map=tuple(
            {"name": name, "physical_pin": index + 1, "direction": "input"}
            for index, name in enumerate(ports)
        ),
        characteristics=(
            Characteristic(
                char_id="R_FSW",
                statement="Buck switching frequency",
                unit="Hz",
                min_value=None,
                max_value=None,
                typ_value=1_000_000,
                target=1_000_000,
                source_page=5,
                excerpt="Switching frequency 1000 kHz",
                req_class="TYPICAL_VALUE",
                probe=None,
                probe_params={},
                not_testable_reason="example smoke",
            ),
        ),
    )


@pytest.mark.ltspice
def test_physical_buck_example_runs_in_ltspice(ltspice_exe, tmp_path, monkeypatch) -> None:
    spec = _buck_seed_spec()
    seed = seed_from_spec(spec)
    assert seed is not None
    seed.write(tmp_path / "BUCK.lib")
    monkeypatch.setattr(card, "render_card", lambda **kwargs: "Unjudged example smoke")
    card.write_deliverables(out_dir=tmp_path, part="BUCK", subckt="BUCK", spec=spec, report=None)

    result = run_batch(ltspice_exe, tmp_path / "example.cir", tmp_path, timeout_s=60)
    assert result.exit_code == 0
    raw = read_raw(result.raw_path)
    assert raw.column("V(out)")[-1] > 1.0
