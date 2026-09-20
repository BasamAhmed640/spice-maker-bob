"""The probe registry: deck shape, port binding, and real measured values.

Rendering is proved without a simulator (port binding by name, no ``.ic``/``uic``,
explicit timestep, ``.save`` whitelist). The measurements are proved against the
stock ``BM_REG_BUCK`` template library on the local LTspice: every probe has to
recover the model's documented physical constant, which is what makes the probe
a measuring instrument rather than a deck that merely runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from boardmodeler.authoring.probes import (
    PROBES,
    ProbeError,
    judge_value,
    model_ports,
    required_ports,
)
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.regulator import write_regulator_library
from boardmodeler.simulation.ltspice import BatchResult, run_batch

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"

#: (judged key, expected value for the stock BM_REG_BUCK defaults, absolute
#: tolerance). The expectations are the model's own documented constants
#: (``boardmodeler.models.regulator``): UVLO 4.3/3.9 V, EN 1.25/1.15 V,
#: VREF 0.8 V, ILIM 3.0 A, ISS/CSS soft start, no shutdown/quiescent consumption.
_EXPECTED: dict[str, tuple[str, float, float]] = {
    "uvlo_rise": ("vin_at_start", 4.3, 0.02),
    "uvlo_fall": ("vin_at_stop", 3.9, 0.02),
    "en_rise": ("en_at_start", 1.25, 0.01),
    "en_fall": ("en_at_stop", 1.15, 0.01),
    "vref": ("v_fb", 0.8, 3e-3),
    "load_regulation": ("i_heavy", 2.0, 0.02),
    "current_limit": ("i_out_limit", 3.0, 0.05),
    "pg_threshold": ("pg_leak_a", 0.0, 1e-9),
    "soft_start": ("t_ss_s", 4.47e-3, 0.15e-3),
    "shutdown_current": ("i_vin_a", 0.0, 5e-6),
    "quiescent_current": ("i_vin_a", 76e-6, 20e-6),
}

_SUBCKT = "BM_REG_BUCK"


@pytest.fixture(scope="module")
def buck_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_regulator_library(tmp_path_factory.mktemp("models") / "buck.lib", [_SUBCKT])


@dataclass(frozen=True)
class ProbeRun:
    probe_id: str
    result: BatchResult
    measured: dict[str, float]
    params: dict[str, float]


@pytest.fixture(scope="module")
def probe_runs(buck_lib: Path, ltspice_exe: Path, tmp_path_factory: pytest.TempPathFactory):
    """Every probe, rendered and run once against the stock library."""
    root = tmp_path_factory.mktemp("probe_runs")
    runs: dict[str, ProbeRun] = {}
    for probe_id in _EXPECTED:
        spec = PROBES[probe_id]
        run_dir = root / probe_id
        run_dir.mkdir(parents=True)
        deck = run_dir / "deck.cir"
        deck.write_text(
            spec.render(model_lib=buck_lib, subckt=_SUBCKT, params={}),
            encoding="utf-8",
            newline="\n",
        )
        result = run_batch(ltspice_exe, deck, run_dir, timeout_s=120.0)
        measured = spec.measure(result.raw_path, {}) if result.raw_path else {}
        runs[probe_id] = ProbeRun(
            probe_id=probe_id, result=result, measured=measured, params=spec.merged_params({})
        )
    return runs


# --------------------------------------------------------------------------- #
# rendering (no simulator needed)


def test_registry_has_the_required_probes() -> None:
    required = {
        "uvlo_rise",
        "uvlo_fall",
        "en_rise",
        "en_fall",
        "vref",
        "load_regulation",
        "current_limit",
        "pg_threshold",
        "soft_start",
        "shutdown_current",
        "quiescent_current",
    }
    assert required <= set(PROBES)
    for probe_id, spec in PROBES.items():
        assert spec.probe_id == probe_id
        assert spec.title and spec.question
        assert spec.unit in {"V", "A", "s", "ohm", "F", "Hz", "V/V", "V/s"}
        assert spec.ports_needed
        assert spec.renderer is not None and spec.measurer is not None
        assert spec.judge_key
        # the harness reads both when it builds the run diagnosis
        assert "tstop_s" in spec.merged_params({})
        assert "tmax_s" in spec.merged_params({})


def test_render_binds_ports_by_name(buck_lib: Path) -> None:
    spec = PROBES["vref"]
    deck = spec.render(model_lib=buck_lib, subckt=_SUBCKT, params={})
    ports = model_ports(buck_lib, _SUBCKT)
    assert f".include {buck_lib.resolve().as_posix()}" in deck
    assert ".options plotwinsize=0" in deck
    assert ".tran 0 " in deck
    assert ".ic " not in deck and "uic" not in deck
    assert "VREF" not in deck  # the model runs with its own defaults
    instance = next(line for line in deck.splitlines() if line.startswith("X1 "))
    tokens = instance.split()
    nodes = tokens[1 : 1 + len(ports)]
    assert tokens[-1] == _SUBCKT
    assert len(nodes) == len(ports)
    # the application nodes are bound by port name, not by position
    for port, net in (("VIN", "vin"), ("EN", "en"), ("FB", "fb"), ("VOUT", "vout"), ("GND", "0")):
        assert nodes[ports.index(port)] == net
    for signal in ("V(fb)", "V(vout)"):
        assert signal in deck


def test_render_refuses_a_missing_port(buck_lib: Path, tmp_path: Path) -> None:
    text = buck_lib.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith(f".subckt {_SUBCKT}"))
    broken = tmp_path / "buck_no_vout.lib"
    broken.write_text(
        text.replace(header, header.replace(" VOUT ", " VOUTX ")), encoding="utf-8", newline="\n"
    )
    with pytest.raises(ProbeError) as excinfo:
        PROBES["vref"].render(model_lib=broken, subckt=_SUBCKT, params={})
    assert excinfo.value.reason == "port_missing:VOUT"
    assert "VOUTX" in excinfo.value.detail


def test_render_wires_ports_the_probe_does_not_need(buck_lib: Path, tmp_path: Path) -> None:
    text = buck_lib.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith(f".subckt {_SUBCKT}"))
    extra = header.replace(" params:", " COMP params:")
    with_extra = tmp_path / "buck_extra_port.lib"
    with_extra.write_text(text.replace(header, extra), encoding="utf-8", newline="\n")

    deck = PROBES["vref"].render(model_lib=with_extra, subckt=_SUBCKT, params={})
    assert "bmx_comp" in deck
    instance = next(line for line in deck.splitlines() if line.startswith("X1 "))
    assert len(instance.split()) == 2 + len(model_ports(with_extra, _SUBCKT))


def test_required_ports_union_is_deterministic() -> None:
    spec = load_tps54320_spec(
        FIXTURE / "requirements.json",
        FIXTURE / "probes.json",
        part="TPS54320",
        subckt=_SUBCKT,
    )
    assert required_ports(spec.covered()) == ("VIN", "EN", "FB", "VOUT", "GND", "PG")
    assert required_ports(()) == ()


def test_judge_value_reports_a_missing_key() -> None:
    with pytest.raises(ProbeError) as excinfo:
        judge_value("vref", {"v_out": 3.2})
    assert excinfo.value.reason == "judge_value_missing:v_fb"
    with pytest.raises(ValueError):
        judge_value("not_a_probe", {})


def test_unknown_probe_parameters_are_rejected(buck_lib: Path) -> None:
    with pytest.raises(ValueError, match="no parameter"):
        PROBES["vref"].render(model_lib=buck_lib, subckt=_SUBCKT, params={"typo": 1.0})


# --------------------------------------------------------------------------- #
# measurements (real LTspice)


@pytest.mark.ltspice
def test_every_probe_measured_the_stock_library(probe_runs: dict[str, ProbeRun]) -> None:
    lines = ["probe                judged value                    expected"]
    for probe_id, run in probe_runs.items():
        assert run.result.exit_code == 0, (probe_id, run.result.observed())
        assert run.measured, f"{probe_id} produced no measurement"
        key, expected, tolerance = _EXPECTED[probe_id]
        value = run.measured[key]
        assert math.isfinite(value)
        assert value == pytest.approx(expected, abs=tolerance), probe_id
        lines.append(f"{probe_id:20s} {key}={value:<24.6g} {expected:g} +/- {tolerance:g}")
    print("\n" + "\n".join(lines))


@pytest.mark.ltspice
def test_uvlo_probes_measure_both_edges_and_the_hysteresis(probe_runs: dict[str, ProbeRun]) -> None:
    rise = probe_runs["uvlo_rise"].measured["vin_at_start"]
    fall = probe_runs["uvlo_fall"].measured["vin_at_stop"]
    assert rise > fall
    assert rise - fall == pytest.approx(0.4, abs=0.02)


@pytest.mark.ltspice
def test_load_regulation_is_a_real_load_step(probe_runs: dict[str, ProbeRun]) -> None:
    measured = probe_runs["load_regulation"].measured
    assert measured["i_light"] == pytest.approx(0.05, abs=1e-3)
    assert measured["i_heavy"] / measured["i_light"] > 10.0
    assert measured["vout_heavy"] == pytest.approx(measured["vout_light"], rel=0.01)
    assert measured["load_reg_error_pct"] < 1.0


@pytest.mark.ltspice
def test_current_limit_probe_recovers_the_model_limit(probe_runs: dict[str, ProbeRun]) -> None:
    measured = probe_runs["current_limit"].measured
    assert measured["i_out_limit"] == pytest.approx(3.0, abs=0.05)
    assert measured["i_load_at_limit"] == pytest.approx(measured["i_out_limit"], abs=0.05)
    assert measured["vout_at_limit"] > 0.9 * measured["vout_ref"]


@pytest.mark.ltspice
def test_power_good_probe_measures_the_open_drain_pin(probe_runs: dict[str, ProbeRun]) -> None:
    measured = probe_runs["pg_threshold"].measured
    params = probe_runs["pg_threshold"].params
    assert measured["pg_high_v"] > 0.95 * params["pg_rail"]
    assert measured["pg_leak_a"] < 1e-7
    assert measured["pg_low_v"] < 0.3
    assert measured["pg_low_current_a"] == pytest.approx(
        params["pg_rail"] / params["pg_pullup"], rel=0.05
    )
    assert 0.85 < measured["pg_ratio_release"] < 1.0
    assert 1.05 < measured["pg_ratio_trip"] < 1.25


@pytest.mark.ltspice
def test_soft_start_probe_recovers_the_ss_law(probe_runs: dict[str, ProbeRun]) -> None:
    """t(95 %) = 0.95 * VREF / (ISS / CSS) with the template's ISS = 1.7 uA."""
    measured = probe_runs["soft_start"].measured
    params = probe_runs["soft_start"].params
    expected = 0.95 * 0.8 / (1.7e-6 / 10e-9)
    assert measured["t_ss_s"] == pytest.approx(expected, abs=0.2e-3)
    assert measured["vout_final_v"] > 3.0
    assert measured["overshoot_v"] < 0.1
    assert measured["t_enable_s"] == pytest.approx(params["t_enable_s"] / 2.0, abs=5e-6)


@pytest.mark.ltspice
def test_supply_current_probes_see_the_documented_states(probe_runs: dict[str, ProbeRun]) -> None:
    shutdown = probe_runs["shutdown_current"].measured
    quiescent = probe_runs["quiescent_current"].measured
    assert 0.0 <= shutdown["i_vin_a"] < 5e-6
    assert shutdown["vout_v"] < 0.1
    assert 1e-6 < quiescent["i_vin_a"] < 8e-4
    assert quiescent["vout_v"] > 3.0


@pytest.mark.ltspice
def test_measure_refuses_a_truncated_run(probe_runs: dict[str, ProbeRun]) -> None:
    run = probe_runs["shutdown_current"]
    with pytest.raises(ProbeError) as excinfo:
        PROBES["shutdown_current"].measure(run.result.raw_path, {"tstop_s": 10.0})
    assert excinfo.value.reason == "run_truncated"


@pytest.mark.ltspice
def test_measure_refuses_a_signal_that_was_not_saved(probe_runs: dict[str, ProbeRun]) -> None:
    run = probe_runs["vref"]
    tstop = PROBES["vref"].merged_params({})["tstop_s"]
    with pytest.raises(ProbeError) as excinfo:
        PROBES["uvlo_rise"].measure(run.result.raw_path, {"tstop_s": tstop})
    assert excinfo.value.reason == "signal_missing:V(vin)"


@pytest.mark.ltspice
def test_extra_declared_ports_do_not_change_the_measurement(
    buck_lib: Path, tmp_path: Path, ltspice_exe: Path
) -> None:
    text = buck_lib.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith(f".subckt {_SUBCKT}"))
    with_extra = tmp_path / "buck_extra_port.lib"
    with_extra.write_text(
        text.replace(header, header.replace(" params:", " COMP params:")),
        encoding="utf-8",
        newline="\n",
    )
    run_dir = tmp_path / "extra_port_run"
    run_dir.mkdir()
    deck = run_dir / "deck.cir"
    deck.write_text(
        PROBES["vref"].render(model_lib=with_extra, subckt=_SUBCKT, params={}),
        encoding="utf-8",
        newline="\n",
    )
    result = run_batch(ltspice_exe, deck, run_dir, timeout_s=120.0)
    assert result.exit_code == 0, result.observed()
    assert PROBES["vref"].measure(result.raw_path, {})["v_fb"] == pytest.approx(0.8, abs=3e-3)
