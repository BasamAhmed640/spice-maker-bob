"""Offline fault controls for the cited M4b1 waveform evaluator."""

import numpy as np
import pytest
from tools import tps54332_m4b1_verify as verify

from boardmodeler.authoring.spec import load_tps54320_spec


@pytest.fixture(scope="module")
def rows():
    spec = load_tps54320_spec(
        verify.FROZEN_REQUIREMENTS,
        verify.FROZEN_BINDINGS,
        part=verify.PART,
        subckt=verify.PART,
    )
    assert spec.digest() == verify.SPEC_DIGEST
    return verify._cited_rows(spec, verify.FROZEN_REQUIREMENTS)


@pytest.mark.parametrize(
    ("case", "clean", "wrong"),
    [
        ("vref", 0.8, 0.72),
        ("ss_charge", 2e-6, 1e-6),
        ("shutdown_iq", 1e-6, 10e-6),
        ("operating_iq", 82e-6, 200e-6),
    ],
)
def test_cited_bands_reject_wrong_waveform_values(rows, case, clean, wrong):
    def judgement(value):
        return verify._judge(rows[case], {"value": value, "state": {"status": "READY"}})

    assert judgement(clean)["verdict"] == "PASS"
    assert judgement(wrong)["verdict"] == "FAIL"
    assert judgement(clean)["typical_comparison"]["status"] == "WITHIN"


@pytest.mark.parametrize("case", verify.CASES)
def test_state_gate_prevents_a_numeric_pass(rows, case):
    measurement = {
        "value": rows[case].typ_value,
        "state": {"status": "UNKNOWN", "reason": "electrical state not observed"},
    }
    assert verify._judge(rows[case], measurement)["verdict"] == "UNKNOWN"


def test_ss_crossing_follows_late_wrong_charge_current():
    t = np.linspace(0.0, 8e-3, 1001)
    correct_ss = 2e-6 / 15e-9 * t
    wrong_ss = 1e-6 / 15e-9 * t
    correct_interval = verify._up_crossing(t, correct_ss, 0.45) - verify._up_crossing(
        t, correct_ss, 0.35
    )
    wrong_interval = verify._up_crossing(t, wrong_ss, 0.45) - verify._up_crossing(t, wrong_ss, 0.35)
    assert 15e-9 * 0.1 / correct_interval == pytest.approx(2e-6)
    assert 15e-9 * 0.1 / wrong_interval == pytest.approx(1e-6)
    assert verify._up_crossing(t, wrong_ss, 0.4) > verify._up_crossing(t, correct_ss, 0.4)


def test_iq_stability_rejects_equal_half_means_with_large_current_swing():
    steady = {
        "mean": 82e-6,
        "min": 81e-6,
        "max": 83e-6,
        "early_mean": 82e-6,
        "late_mean": 82e-6,
    }
    assert verify._stable_iq_draw(steady)
    alternating = {**steady, "min": 20e-6, "max": 144e-6}
    assert not verify._stable_iq_draw(alternating)
