"""Offline controls for the prespecified M4b2b external-waveform checks."""

from __future__ import annotations

import numpy as np
import pytest
from tools import tps54332_m4b2b_verify as verify

from boardmodeler.authoring.buck_system_fixtures import BuckBenchParts, build_buck_system_benches
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.simulation.raw import RawFile


@pytest.fixture(scope="module")
def setup():
    spec = load_tps54320_spec(
        verify.FROZEN_REQUIREMENTS,
        verify.FROZEN_BINDINGS,
        part=verify.PART,
        subckt=verify.PART,
    )
    assert spec.digest() == verify.SPEC_DIGEST
    rows = verify._cited_rows(spec, verify.FROZEN_REQUIREMENTS)
    parts = BuckBenchParts()
    m2 = {
        bench.name: bench
        for bench in build_buck_system_benches(
            spec, parts, requirements_path=verify.FROZEN_REQUIREMENTS
        )
    }
    return spec, rows, {**m2, "gain": verify._gain_bench(m2["gain"])}


def _raw(
    t: np.ndarray,
    current: np.ndarray,
    comp: np.ndarray,
    ph: np.ndarray,
    out: np.ndarray | None = None,
) -> RawFile:
    values = {
        "time": t,
        "V(vin)": np.full_like(t, 12.0),
        "V(en)": np.full_like(t, 3.3),
        "V(ss)": np.full_like(t, 1.0),
        "V(vsense)": np.full_like(t, 0.4),
        "V(out)": np.full_like(t, 1.0) if out is None else out,
        "V(ph)": ph,
        "V(comp)": comp,
        "I(Lout)": current,
    }
    names = list(values)
    return RawFile(
        None,
        "Transient Analysis",
        [],
        names,
        np.column_stack([values[name] for name in names]),
        ["time", *["voltage"] * (len(names) - 1)],
    )


def _gain_raw(bench, gain: float, mode: str) -> RawFile:
    step = 0.2e-6 if mode == "SW" else 2e-6
    t = np.arange(0.0, bench.stop_s + step, step)
    start = bench.window_s[0]
    index = np.clip(((t - start) / verify.GAIN_HOLD_S).astype(int), 0, 2)
    comp = np.asarray(verify.GAIN_LEVELS_V)[index]
    current = gain * (comp - 0.5)
    ph = np.where((t % 1e-6) < 0.5e-6, 12.0, 0.0) if mode == "SW" else np.ones_like(t)
    return _raw(t, current, comp, ph)


def _limit_raw(bench, peak: float, mode: str, *, collapsed: bool) -> RawFile:
    step = 0.2e-6 if mode == "SW" else 2e-6
    t = np.arange(0.0, bench.stop_s + step, step)
    post = 1.5 if collapsed else 2.5
    out = np.where(t < bench.metadata["switch_at_s"], 2.5, post)
    ph = np.where((t % 2e-6) < 1e-6, 12.0, 0.0) if mode == "SW" else np.ones_like(t)
    return _raw(t, np.full_like(t, peak), np.full_like(t, 2.4), ph, out)


@pytest.mark.parametrize("mode", verify.MODES)
@pytest.mark.parametrize(("gain", "expected"), [(12.0, "PASS"), (8.0, "FAIL")])
def test_three_fixed_external_points_detect_wrong_gain(setup, mode, gain, expected):
    _, rows, benches = setup
    bench = benches["gain"]
    assert tuple(point.comp_v for point in bench.gain_points) == (0.80, 0.85, 0.90)
    measured = verify._measure_gain(mode, bench, _gain_raw(bench, gain, mode))
    assert measured["state"]["status"] == "READY"
    assert len(measured["points"]) == 3
    assert measured["value"] == pytest.approx(gain, rel=1e-3)
    assert verify._judge_gain(rows["gain"], measured)["verdict"] == expected
    if mode == "SW":
        assert all(point["late_cycles"] >= 10 for point in measured["points"])
    else:
        assert all(point["late_cycles"] is None for point in measured["points"])


def test_gain_does_not_pass_when_forced_comp_is_not_observed(setup):
    _, rows, benches = setup
    bench = benches["gain"]
    raw = _gain_raw(bench, 12.0, "AVG")
    raw.data[:, raw.index("V(comp)")] = 0.9
    measured = verify._measure_gain("AVG", bench, raw)
    assert measured["state"]["status"] == "UNKNOWN"
    assert verify._judge_gain(rows["gain"], measured)["verdict"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("case", "target", "fault_peak"),
    [("limit_min", 4.2, 6.3), ("limit_max", 6.5, 8.0)],
)
def test_limit_dynamic_allowance_is_separate_and_wrong_eight_fails(setup, case, target, fault_peak):
    _, rows, benches = setup
    bench = benches[case]
    ready = {
        "state": {"status": "READY", "post_fault_output_collapse_observed": True},
        "value": target + 0.005,
    }
    clean = verify._judge_limit(rows[case], "SW", bench, ready)
    # At the minimum-setting fixture, ILIM=8 can leave a ~6.3 A load below
    # the wrong limit; the measured peak still must fail the 4.2 A response.
    wrong = verify._judge_limit(rows[case], "SW", bench, {**ready, "value": fault_peak})
    assert clean["verdict"] == "PASS"
    assert wrong["verdict"] == "FAIL"
    assert clean["dynamic_allowance_a"] == 0.1
    assert clean["cited_static_threshold_band_a"] == [4.2, 6.5]
    assert clean["selected_template_ilim_a"] == target


def test_avg_mean_does_not_claim_peak_current_limit(setup):
    _, rows, benches = setup
    bench = benches["limit_max"]
    raw = _limit_raw(bench, 6.5, "AVG", collapsed=True)
    measured = verify._measure_limit("AVG", bench, raw)
    assert measured["state"]["status"] == "READY"
    assert measured["state"]["post_fault_output_collapse_observed"]
    assert measured["value"] is None
    assert measured["inductor_mean_a"] == pytest.approx(6.5)
    result = verify._judge_limit(rows["limit_max"], "AVG", bench, measured)
    assert result["verdict"] == "UNKNOWN"
    assert result["reason"] == "AVG external mean current cannot prove the cited peak threshold"


def test_sw_peak_near_limit_without_output_collapse_cannot_pass(setup):
    _, rows, benches = setup
    bench = benches["limit_min"]
    measured = verify._measure_limit("SW", bench, _limit_raw(bench, 4.205, "SW", collapsed=False))
    assert measured["state"]["status"] == "READY"
    assert not measured["state"]["post_fault_output_collapse_observed"]
    assert verify._judge_limit(rows["limit_min"], "SW", bench, measured)["verdict"] == "UNKNOWN"
    measured["value"] = 6.3
    assert verify._judge_limit(rows["limit_min"], "SW", bench, measured)["verdict"] == "FAIL"


def test_sw_clean_limit_requires_stable_post_fault_collapse(setup):
    _, rows, benches = setup
    bench = benches["limit_min"]
    measured = verify._measure_limit("SW", bench, _limit_raw(bench, 4.205, "SW", collapsed=True))
    assert measured["state"]["status"] == "READY"
    assert measured["state"]["post_fault_output_collapse_observed"]
    assert measured["pre_fault_output_v"]["mean"] == pytest.approx(2.5)
    assert measured["post_fault_output_v"]["mean"] == pytest.approx(1.5)
    assert verify._judge_limit(rows["limit_min"], "SW", bench, measured)["verdict"] == "PASS"


def test_decks_keep_m2_resistive_load_and_use_per_instance_faults(setup, tmp_path):
    spec, _, benches = setup
    sw = seed_from_spec(spec, mode="SW")
    avg = seed_from_spec(spec, mode="AVG")
    assert sw is not None and avg is not None
    models = {"SW": tmp_path / "sw.lib", "AVG": tmp_path / "avg.lib"}
    sw.write(models["SW"])
    avg.write(models["AVG"])
    gain, _ = verify._deck(benches["gain"], models["SW"], "SW", "fault", 2.5e-6)
    assert "GMCS=8" in next(line for line in gain.splitlines() if line.startswith("XU1 "))
    assert "Vforce comp 0 PWL(" in gain
    assert "B002_TPS54332DDA_SW_CURRENT_TO_COMP" in gain
    clean, _ = verify._deck(benches["limit_min"], models["SW"], "SW", "clean", 2.5e-6)
    fault, _ = verify._deck(benches["limit_min"], models["AVG"], "AVG", "fault", 2.5e-6)
    assert "ILIM=4.2" in next(line for line in clean.splitlines() if line.startswith("XU1 "))
    fault_instance = next(line for line in fault.splitlines() if line.startswith("XU1 "))
    assert "ILIM=8" in fault_instance and "L_EXT=2.5e-06" in fault_instance
    assert "Sfault " in clean and "Rfault " in clean
    assert "B002_TPS54332DDA_ILIM" in clean
