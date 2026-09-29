"""Offline waveform and fault controls for bidirectional TPS thresholds."""

import numpy as np
import pytest
from tools import tps54332_m4b2a_verify as verify

from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.simulation.raw import RawFile


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


def _synthetic_raw(
    case,
    rising_v,
    falling_v,
    *,
    mode="AVG",
    output_present=True,
    ss_onset=True,
    ss_spike_v=None,
):
    t = np.linspace(0.0, verify.STOP_S, 106001)
    if case == "uvlo":
        stimulus = np.interp(t, [0.0, 2e-3, 8e-3, 10e-3, verify.STOP_S], [0, 6, 6, 1.5, 1.5])
        vin, en = stimulus, np.full_like(t, 3.3)
        on_t = 2e-3 * rising_v / 6
        off_t = 8e-3 + 2e-3 * (6 - falling_v) / 4.5
    else:
        stimulus = np.interp(t, [0.0, 2e-3, 8e-3, 10e-3, verify.STOP_S], [0, 3.3, 3.3, 0, 0])
        vin, en = np.full_like(t, 12.0), stimulus
        on_t = 2e-3 * rising_v / 3.3
        off_t = 8e-3 + 2e-3 * (3.3 - falling_v) / 3.3
    active = (t >= on_t) & (t <= off_t)
    current = np.where(active, 0.2, 1e-6)
    delivered = np.where(active & output_present, 1.0, 0.0)
    out = np.where(
        active & output_present,
        2.5 * (1.0 - np.exp(-np.maximum(t - on_t, 0.0) / 0.3e-3)),
        0.0,
    )
    if mode == "SW":
        ph_on = np.floor(t / 0.5e-6).astype(int) % 2 == 0
        ph = np.where(active & output_present & ph_on, 2.5, 0.0)
    else:
        ph = np.where(active & output_present, 2.5, 0.0)
    ss_at_off = min((off_t - on_t) * 133.3, 1.5)
    ss = np.where(
        t <= off_t,
        np.minimum(np.maximum(t - on_t, 0.0) * 133.3, 1.5),
        ss_at_off * np.exp(-np.maximum(t - off_t, 0.0) / 2e-6),
    )
    if not ss_onset:
        ss[:] = 0.0
    if ss_spike_v is not None:
        spike_t = 2e-3 * ss_spike_v / (6 if case == "uvlo" else 3.3)
        ss[np.argmin(np.abs(t - spike_t))] = 1e-3
    traces = {
        "V(vin)": vin,
        "V(dut_vin)": vin,
        "V(en)": en,
        "V(ss)": ss,
        "V(vsense)": 0.32 * out,
        "V(out)": out,
        "V(ph)": ph,
        "I(Lout)": delivered,
        "I(Vdutvin)": current,
    }
    names = ["time", *traces]
    data = np.column_stack((t, *traces.values()))
    return RawFile(None, "Transient Analysis", [], names, data, ["time", *["voltage"] * 9])


@pytest.mark.parametrize("mode", ["SW", "AVG"])
@pytest.mark.parametrize(
    ("case", "rising_v", "falling_v", "expected"),
    [
        ("uvlo", 3.5, 3.3, ("PASS", "FAIL")),
        ("uvlo", 2.5, 2.3, ("FAIL", "FAIL")),
        ("en", 1.3, 1.26, ("PASS", "PASS")),
        ("en", 1.0, 0.95, ("FAIL", "FAIL")),
    ],
)
def test_external_waveform_events_judge_both_directions(
    rows, mode, case, rising_v, falling_v, expected
):
    measurement = verify._measure(case, mode, _synthetic_raw(case, rising_v, falling_v, mode=mode))
    assert measurement["stage"]["status"] == "READY"
    assert (
        tuple(
            verify._judge(rows[case], measurement["directions"][direction])["verdict"]
            for direction in verify.DIRECTIONS
        )
        == expected
    )


@pytest.mark.parametrize("mode", ["SW", "AVG"])
def test_stage_guard_rejects_numeric_threshold_without_output(rows, mode):
    measurement = verify._measure(
        "en", mode, _synthetic_raw("en", 1.3, 1.26, mode=mode, output_present=False)
    )
    assert measurement["stage"]["status"] == "UNKNOWN"
    assert all(
        verify._judge(rows["en"], measurement["directions"][direction])["verdict"] == "UNKNOWN"
        for direction in verify.DIRECTIONS
    )


@pytest.mark.parametrize("mode", ["SW", "AVG"])
def test_isolated_ss_spike_rejects_pass(rows, mode):
    measurement = verify._measure(
        "en", mode, _synthetic_raw("en", 1.3, 1.26, mode=mode, ss_spike_v=1.0)
    )
    assert measurement["stage"]["status"] == "READY"
    rising = measurement["directions"]["rising"]
    assert rising["status"] == "UNKNOWN"
    assert not rising["checks"]["ss_charge_persists"]
    assert verify._judge(rows["en"], rising)["verdict"] == "UNKNOWN"


def test_missing_ss_charge_never_becomes_a_pass(rows):
    measurement = verify._measure("en", "AVG", _synthetic_raw("en", 1.3, 1.26, ss_onset=False))
    assert measurement["stage"]["status"] == "READY"
    assert verify._judge(rows["en"], measurement["directions"]["rising"])["verdict"] == "UNKNOWN"


def test_unknown_event_never_becomes_a_pass(rows):
    event = {"status": "UNKNOWN", "value": 1.3, "reason": "SS transition ambiguous"}
    assert verify._judge(rows["en"], event)["verdict"] == "UNKNOWN"
