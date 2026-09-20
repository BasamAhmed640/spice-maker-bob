"""Qualify the instruments against synthetic constants, not a claimed device model."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from boardmodeler.authoring.lm358_reference import DATASHEET_SHA256, matches, records
from boardmodeler.authoring.probes import PROBES
from boardmodeler.simulation.ltspice import run_batch
from boardmodeler.simulation.raw import RawFormatError, read_raw

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "opamp"


def test_reviewed_extraction_matches_only_exact_part_and_datasheet():
    assert matches(" lm358 ", DATASHEET_SHA256)
    for part, digest in [
        ("LM358B", DATASHEET_SHA256),
        ("LM358A", DATASHEET_SHA256),
        ("LM358", "bad"),
    ]:
        assert not matches(part, digest)
    page = (FIXTURE / "ti-lm358-rev-ab-page9.txt").read_text(encoding="utf-8")
    rows, bindings, pins = records(SimpleNamespace(doc_id="reviewed-source"), page)
    assert len(rows) == len(bindings) == 42
    assert sum(bool(b["probe"]) for b in bindings) == 32
    assert [p["physical_pin"] for p in pins] == list("12345678")
    for row in rows:
        assert row.evidence[0].excerpt in page
        assert row.evidence[0].page.pdf_page == 9


@pytest.mark.ltspice
@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize(
    "kind,expected,params",
    [
        ("offset", 0.003, {}),
        ("bias", 21e-9, {}),
        ("offset_current", 2e-9, {}),
        ("gain", 98502.35, {"op_vcc": 15, "op_vout": 6, "op_load": 2000}),
        ("gbw", 700000, {}),
        ("slew_rise", 300000, {}),
        ("slew_fall", 300000, {}),
        ("quiescent", 350e-6, {}),
        ("swing_high", 2.02797, {"op_vcc": 30, "op_load": 10000}),
        ("swing_low", 0.004995, {"op_load": 10000}),
        ("follower", 0.002975, {}),
    ],
)
def test_measuring_instruments_recover_independent_constants(
    ltspice_exe, tmp_path, channel, kind, expected, params
):
    params = {**params, "op_channel": channel}
    probe = PROBES["opamp_" + kind]
    deck = tmp_path / "oracle.cir"
    deck.write_text(
        probe.render(model_lib=FIXTURE / "synthetic.lib", subckt="ORACLE", params=params)
    )
    result = run_batch(ltspice_exe, deck, tmp_path, timeout_s=20)
    assert result.exit_code == 0
    assert probe.measure(result.raw_path, params)["opamp_value"] == pytest.approx(
        expected, rel=0.01
    )


@pytest.mark.ltspice
def test_wrong_bandwidth_is_observed(ltspice_exe, tmp_path):
    model = tmp_path / "mutated.lib"
    model.write_text(
        (FIXTURE / "synthetic.lib").read_text().replace("Cdom pole 0 1n", "Cdom pole 0 10n")
    )
    probe = PROBES["opamp_gbw"]
    deck = tmp_path / "slow.cir"
    deck.write_text(probe.render(model_lib=model, subckt="ORACLE", params={}))
    result = run_batch(ltspice_exe, deck, tmp_path, timeout_s=20)
    measured = probe.measure(result.raw_path, {})["opamp_value"]
    assert measured == pytest.approx(70000, rel=0.01)
    assert abs(measured - 700000) > 70000


@pytest.mark.ltspice
def test_complex_reader_matches_analytic_rc_amplitude_and_phase(ltspice_exe, tmp_path):
    deck = tmp_path / "rc.cir"
    deck.write_text(
        "* independent AC oracle\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 1u\n.ac dec 20 1 1Meg\n.end\n"
    )
    result = run_batch(ltspice_exe, deck, tmp_path, timeout_s=20)
    raw = read_raw(result.raw_path)
    assert raw.complex_data
    expected = 1 / (1 + 2j * np.pi * raw.data[:, 0].real * 0.001)
    np.testing.assert_allclose(raw.column("V(out)"), expected, rtol=1e-6, atol=1e-9)
    damaged = tmp_path / "short.raw"
    damaged.write_bytes(result.raw_path.read_bytes()[:-1])
    with pytest.raises(RawFormatError, match="truncated complex"):
        read_raw(damaged)


@pytest.mark.ltspice
def test_exported_example_runs_the_actual_dual_amplifier(ltspice_exe, tmp_path, monkeypatch):
    from boardmodeler.authoring import card

    library = tmp_path / "ORACLE.lib"
    library.write_bytes((FIXTURE / "synthetic.lib").read_bytes())
    monkeypatch.setattr(card, "render_card", lambda **kwargs: "Synthetic export test")
    spec = SimpleNamespace(covered=lambda: [SimpleNamespace(probe="opamp_gbw")])
    card.write_deliverables(
        out_dir=tmp_path, part="SYNTHETIC_TEST_FIXTURE", subckt="ORACLE", spec=spec, report=None
    )
    deck = tmp_path / "example.cir"
    assert '.include "ORACLE.lib"' in deck.read_text()
    assert "RFB1" not in deck.read_text()
    result = run_batch(ltspice_exe, deck, tmp_path, timeout_s=20)
    assert result.exit_code == 0
    raw = read_raw(result.raw_path)
    assert raw.column("V(out)")[-1] == pytest.approx(1.003, abs=0.0001)
