"""The harness: discrimination on real LTspice runs, and honesty on failures.

Three things must hold, and each is proved on the local simulator:

* the stock template library passes at least five bound characteristics with zero
  FAIL (a harness that cannot pass anything is not a harness),
* a deliberately perturbed copy fails exactly the characteristic that was moved,
  with the measured value in the feedback text, and
* a model whose ports do not match, or a run that cannot complete, is UNKNOWN
  with a reason — never FAIL (the model is not blamed for missing data) and never
  PASS.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from boardmodeler.authoring import harness as harness_mod
from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome, run_harness
from boardmodeler.authoring.probes import ProbeError, model_ports
from boardmodeler.authoring.spec import Characteristic, SpecSet, load_tps54320_spec
from boardmodeler.domain.enums import Status
from boardmodeler.models.regulator import write_regulator_library
from boardmodeler.simulation.ltspice import BatchResult

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"
SUBCKT = "BM_REG_BUCK"


@pytest.fixture(scope="module")
def spec() -> SpecSet:
    return load_tps54320_spec(
        FIXTURE / "requirements.json",
        FIXTURE / "probes.json",
        part="TPS54320",
        subckt=SUBCKT,
    )


@pytest.fixture(scope="module")
def good_report(spec: SpecSet, ltspice_exe: Path, tmp_path_factory) -> HarnessReport:
    root = tmp_path_factory.mktemp("harness_good")
    lib = write_regulator_library(root / "buck.lib", [SUBCKT])
    return run_harness(
        model_lib=lib, subckt=SUBCKT, spec=spec, workdir=root / "work", ltspice=ltspice_exe
    )


def _perturb_reference(lib: Path, tmp_path: Path) -> Path:
    text = lib.read_text(encoding="utf-8")
    assert text.count("VREF=0.8") == 1, "the perturbation must be specific"
    target = tmp_path / "buck_vref_0p5.lib"
    target.write_text(text.replace("VREF=0.8", "VREF=0.5"), encoding="utf-8", newline="\n")
    return target


@pytest.fixture(scope="module")
def bad_report(spec: SpecSet, ltspice_exe: Path, tmp_path_factory) -> HarnessReport:
    root = tmp_path_factory.mktemp("harness_bad")
    lib = write_regulator_library(root / "buck.lib", [SUBCKT])
    return run_harness(
        model_lib=_perturb_reference(lib, root),
        subckt=SUBCKT,
        spec=spec,
        workdir=root / "work",
        ltspice=ltspice_exe,
    )


# --------------------------------------------------------------------------- #
# (a) the stock library passes


@pytest.mark.ltspice
def test_known_good_library_passes_at_least_five_and_fails_none(
    good_report: HarnessReport,
) -> None:
    counts = good_report.counts()
    judged = sum(len(outcome.char_ids) for outcome in good_report.outcomes)
    assert counts["FAIL"] == 0, good_report.feedback()
    assert counts["UNKNOWN"] == 0, good_report.feedback()
    assert counts["PASS"] >= 5, counts
    assert judged >= 5
    assert good_report.passed()
    assert good_report.failing() == ()
    assert good_report.feedback() == ""


@pytest.mark.ltspice
def test_every_outcome_names_the_characteristics_it_judged(
    good_report: HarnessReport, spec: SpecSet
) -> None:
    expected = {
        "load_regulation": ("REQ_TPS54320_ELEC_003",),
        "uvlo_rise": ("REQ_TPS54320_ELEC_004", "REQ_TPS54320_TEMPORAL_062"),
        "shutdown_current": ("REQ_TPS54320_ELEC_006",),
        "quiescent_current": ("REQ_TPS54320_ELEC_007",),
        "en_rise": ("REQ_TPS54320_ELEC_010",),
        "en_fall": ("REQ_TPS54320_ELEC_011",),
        "vref": ("REQ_TPS54320_ELEC_020",),
        "pg_threshold": ("REQ_TPS54320_PG_052",),
    }
    assert {o.probe_id: o.char_ids for o in good_report.outcomes} == expected
    assert {c.char_id for c in spec.covered()} == {
        char_id for ids in expected.values() for char_id in ids
    }
    for outcome in good_report.outcomes:
        assert outcome.measured, outcome.probe_id
        assert "measured" in outcome.detail
        assert Path(outcome.run_dir, "deck.cir").is_file()


@pytest.mark.ltspice
def test_report_records_the_frozen_spec_and_model_hash(
    good_report: HarnessReport, spec: SpecSet
) -> None:
    assert good_report.spec_digest == spec.digest()
    assert len(good_report.model_sha256) == 64
    assert good_report.part == "TPS54320"


@pytest.mark.ltspice
def test_typical_value_comparison_is_labelled(good_report: HarnessReport) -> None:
    outcome = next(o for o in good_report.outcomes if o.probe_id == "uvlo_rise")
    detail = outcome.detail
    assert "REQ_TPS54320_TEMPORAL_062" in detail
    assert "typical-value comparison, not a min/max limit check" in detail
    assert "REQ_TPS54320_ELEC_004" in detail
    assert "within 4 .. 4.5 V" in detail


# --------------------------------------------------------------------------- #
# (b) a perturbed model fails exactly the perturbed characteristic


@pytest.mark.ltspice
def test_perturbed_reference_fails_only_the_reference_characteristic(
    bad_report: HarnessReport, spec: SpecSet
) -> None:
    counts = bad_report.counts()
    assert counts["FAIL"] == 1, bad_report.feedback()
    assert not bad_report.passed()

    failing = bad_report.failing()
    assert len(failing) == 1
    outcome = failing[0]
    assert outcome.probe_id == "vref"
    assert outcome.char_ids == ("REQ_TPS54320_ELEC_020",)
    # the perturbation moved the reference from 0.8 V to 0.5 V
    assert outcome.measured["v_fb"] == pytest.approx(0.5, abs=5e-3)
    assert "0.499996" in outcome.detail or "0.5" in outcome.detail
    assert "0.792" in outcome.detail
    assert "minimum" in outcome.detail

    # the power-good pin never releases at a 2.04 V output, so that probe reports
    # UNKNOWN (with the reason) instead of blaming the pin for the moved reference
    unknown = bad_report.unknown()
    assert [o.probe_id for o in unknown] == ["pg_threshold"]
    assert (unknown[0].unknown_reason or "").startswith("pg_not_released")

    untouched = {o.probe_id for o in bad_report.outcomes if o.status == Status.PASS.value}
    assert untouched == set(spec.by_probe()) - {"vref", "pg_threshold"}


@pytest.mark.ltspice
def test_feedback_carries_measured_value_limits_page_and_excerpt(
    bad_report: HarnessReport,
) -> None:
    text = bad_report.feedback()
    assert "probe vref [FAIL]" in text
    assert "measured: v_fb = 0.499996" in text
    assert "REQ_TPS54320_ELEC_020 requires 0.792 .. 0.808 V" in text
    assert "PDF page" in text
    assert "Voltage reference" in text  # the verbatim datasheet excerpt
    assert "likely cause" in text
    assert "below the 0.792 V minimum" in text


@pytest.mark.ltspice
def test_report_json_round_trips_after_a_real_run(bad_report: HarnessReport) -> None:
    text = bad_report.to_json()
    again = HarnessReport.from_json(text)
    assert again == bad_report
    assert again.to_json() == text


# --------------------------------------------------------------------------- #
# (c) missing ports and runs that cannot complete are UNKNOWN


@pytest.mark.ltspice
def test_missing_port_is_unknown_never_fail(
    spec: SpecSet, ltspice_exe: Path, tmp_path: Path
) -> None:
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    text = lib.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith(f".subckt {SUBCKT}"))
    broken = tmp_path / "buck_no_vout.lib"
    broken.write_text(
        text.replace(header, header.replace(" VOUT ", " VOUTX ")), encoding="utf-8", newline="\n"
    )

    report = run_harness(
        model_lib=broken,
        subckt=SUBCKT,
        spec=spec,
        workdir=tmp_path / "work",
        ltspice=ltspice_exe,
        timeout_s=60.0,
    )
    counts = report.counts()
    assert counts["UNKNOWN"] == len(report.outcomes) > 0
    assert counts["FAIL"] == 0 and counts["PASS"] == 0
    assert not report.passed()
    for outcome in report.unknown():
        assert outcome.unknown_reason is not None
        assert outcome.unknown_reason.startswith("port_missing:VOUT")
        assert outcome.char_ids  # the report still says what was not judged
    assert "port_missing:VOUT" in report.feedback()


@pytest.mark.ltspice
def test_run_that_cannot_complete_is_unknown(
    spec: SpecSet, ltspice_exe: Path, tmp_path: Path
) -> None:
    """A deck whose transient cannot finish (huge stop time) is UNKNOWN, never FAIL."""
    char = spec.by_id("REQ_TPS54320_ELEC_004")
    slow = dataclasses.replace(char, probe_params={"ramp_v_per_s": 1e-5, "tstop_s": 600000.0})
    single = dataclasses.replace(spec, characteristics=(slow,))
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])

    report = run_harness(
        model_lib=lib,
        subckt=SUBCKT,
        spec=single,
        workdir=tmp_path / "work",
        ltspice=ltspice_exe,
        timeout_s=8.0,
    )
    assert report.counts()["UNKNOWN"] == 1
    assert not report.passed()
    outcome = report.outcomes[0]
    assert outcome.status == Status.UNKNOWN.value
    assert outcome.unknown_reason
    assert outcome.unknown_reason.startswith("run_timeout")
    assert "run_timeout" in report.feedback()


@pytest.mark.ltspice
def test_waveform_that_cannot_answer_is_unknown(
    spec: SpecSet, ltspice_exe: Path, tmp_path: Path
) -> None:
    """A completed run whose waveform never crosses the gate is UNKNOWN too."""
    char = spec.by_id("REQ_TPS54320_ELEC_004")
    flat = dataclasses.replace(char, probe_params={"ramp_v_per_s": 1e-5})
    single = dataclasses.replace(spec, characteristics=(flat,))
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])

    report = run_harness(
        model_lib=lib, subckt=SUBCKT, spec=single, workdir=tmp_path / "work", ltspice=ltspice_exe
    )
    outcome = report.outcomes[0]
    assert outcome.status == Status.UNKNOWN.value
    assert outcome.unknown_reason is not None
    assert outcome.unknown_reason.startswith("no_crossing")


def test_missing_library_is_unknown(tmp_path: Path, spec: SpecSet) -> None:
    report = run_harness(
        model_lib=tmp_path / "absent.lib",
        subckt=SUBCKT,
        spec=spec,
        workdir=tmp_path / "work",
        ltspice=tmp_path / "LTspice.exe",
    )
    assert report.counts()["UNKNOWN"] == len(report.outcomes) > 0
    assert report.model_sha256 == ""
    assert all((o.unknown_reason or "").startswith("model_lib_unreadable") for o in report.outcomes)


def test_convergence_failure_in_the_log_is_unknown(
    monkeypatch: pytest.MonkeyPatch, spec: SpecSet, tmp_path: Path
) -> None:
    """Deterministic proof of the failed-run path: no simulator is involved."""
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    log = tmp_path / "deck.log"
    log.write_text("Singular matrix: Check node n001\n", encoding="utf-8")
    calls: list[int] = []

    def fake_run_batch(exe, deck, run_dir, *, timeout_s, **kwargs):
        calls.append(1)
        return BatchResult(
            deck=Path(deck),
            run_dir=Path(run_dir),
            exit_code=1,
            stdout="",
            stderr="",
            wall_s=0.01,
            timed_out=False,
            raw_path=None,
            log_path=log,
        )

    monkeypatch.setattr(harness_mod, "run_batch", fake_run_batch)
    single = dataclasses.replace(spec, characteristics=(spec.by_id("REQ_TPS54320_ELEC_004"),))
    report = run_harness(
        model_lib=lib,
        subckt=SUBCKT,
        spec=single,
        workdir=tmp_path / "work",
        ltspice=tmp_path / "LTspice.exe",
    )
    assert calls, "the harness must have attempted the run"
    outcome = report.outcomes[0]
    assert outcome.status == Status.UNKNOWN.value
    assert outcome.unknown_reason is not None
    assert outcome.unknown_reason.startswith("sim_convergence_failure")
    assert report.counts()["PASS"] == 0 and report.counts()["FAIL"] == 0


def test_a_deck_the_simulator_rejects_names_the_simulators_own_error(
    monkeypatch: pytest.MonkeyPatch, spec: SpecSet, tmp_path: Path
) -> None:
    """A model the simulator cannot even parse must say what the simulator said.

    This is the feedback the authoring agent reads on its next turn: without the
    simulator's own line it knows only that no output appeared.
    """
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    log = tmp_path / "deck.log"
    log.write_text(
        "Circuit: deck.cir\n"
        "deck.cir(16): This sub-circuit cannot be instantiated because it contains these "
        "syntax errors:\n"
        "buck.lib(408): Expected a sequence <directive or device instantiation end of line> "
        "here.\n",
        encoding="utf-8",
    )

    def fake_run_batch(exe, deck, run_dir, *, timeout_s, **kwargs):
        return BatchResult(
            deck=Path(deck),
            run_dir=Path(run_dir),
            exit_code=1,
            stdout="",
            stderr="",
            wall_s=0.01,
            timed_out=False,
            raw_path=None,
            log_path=log,
        )

    monkeypatch.setattr(harness_mod, "run_batch", fake_run_batch)
    single = dataclasses.replace(spec, characteristics=(spec.by_id("REQ_TPS54320_ELEC_004"),))
    report = run_harness(
        model_lib=lib,
        subckt=SUBCKT,
        spec=single,
        workdir=tmp_path / "work",
        ltspice=tmp_path / "LTspice.exe",
    )

    outcome = report.outcomes[0]
    assert outcome.status == Status.UNKNOWN.value
    reason = outcome.unknown_reason or ""
    assert reason.startswith("sim_output_unreadable")
    assert "Expected a sequence" in reason, reason
    assert "buck.lib(408)" in reason, reason
    assert "Expected a sequence" in report.feedback(), report.feedback()


def test_an_empty_raw_file_still_names_what_the_simulator_said(
    monkeypatch: pytest.MonkeyPatch, spec: SpecSet, tmp_path: Path
) -> None:
    """A ``.raw`` that exists but holds no data is the same dead end — and says why too.

    LTspice writes the file before it fails, so the run reads as "no usable output";
    the log is the only place the author can learn what is wrong. This is the log of a
    real installed-app run: two source-driven nodes, reported without any prefix of its
    own, and unseen by the author until this line was kept.
    """
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    log = tmp_path / "deck.log"
    log.write_text(
        "Voltage source V_en and voltage source B_enable are paralleled making an "
        "over-defined circuit matrix.\n"
        "You will need to correct the circuit or add some series resistance.\n",
        encoding="utf-8",
    )
    raw = tmp_path / "deck.raw"
    raw.write_bytes(b"")

    def fake_run_batch(exe, deck, run_dir, *, timeout_s, **kwargs):
        return BatchResult(
            deck=Path(deck),
            run_dir=Path(run_dir),
            exit_code=1,
            stdout="",
            stderr="",
            wall_s=0.01,
            timed_out=False,
            raw_path=raw,
            log_path=log,
        )

    monkeypatch.setattr(harness_mod, "run_batch", fake_run_batch)
    single = dataclasses.replace(spec, characteristics=(spec.by_id("REQ_TPS54320_ELEC_004"),))
    report = run_harness(
        model_lib=lib,
        subckt=SUBCKT,
        spec=single,
        workdir=tmp_path / "work",
        ltspice=tmp_path / "LTspice.exe",
    )

    outcome = report.outcomes[0]
    assert outcome.status == Status.UNKNOWN.value
    reason = outcome.unknown_reason or ""
    assert "over-defined circuit matrix" in reason, reason
    assert "You will need to correct the circuit" in reason, reason
    assert "over-defined circuit matrix" in report.feedback(), report.feedback()


def test_cancelled_harness_reports_cancelled(spec: SpecSet, tmp_path: Path) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    report = run_harness(
        model_lib=lib,
        subckt=SUBCKT,
        spec=spec,
        workdir=tmp_path / "work",
        ltspice=tmp_path / "LTspice.exe",
        cancel=cancel,
    )
    assert report.counts()["UNKNOWN"] == len(report.outcomes) > 0
    assert all(o.unknown_reason == "cancelled" for o in report.outcomes)


# --------------------------------------------------------------------------- #
# report shape without a simulator


def test_report_round_trip_and_counts() -> None:
    report = HarnessReport(
        part="P",
        model_sha256="a" * 64,
        spec_digest="b" * 64,
        outcomes=(
            ProbeOutcome(
                probe_id="vref",
                status=Status.FAIL.value,
                measured={"v_fb": 0.5},
                detail="REQ_X: measured v_fb=0.5 V is below the required minimum 0.792 V",
                unknown_reason=None,
                run_dir="/runs/vref",
                char_ids=("REQ_X",),
                judged="v_fb = 0.5 V",
                citations=('REQ_X requires 0.792 .. 0.808 V (PDF page 4) — "Voltage reference"',),
                cause="measured 0.5 V is below the 0.792 V minimum",
            ),
            ProbeOutcome(
                probe_id="uvlo_rise",
                status=Status.UNKNOWN.value,
                measured={},
                detail="",
                unknown_reason="port_missing:VOUT",
                run_dir="/runs/uvlo_rise",
                char_ids=("REQ_Y",),
            ),
        ),
    )
    assert report.counts() == {
        "PASS": 0,
        "FAIL": 1,
        "UNKNOWN": 1,
        "BLOCKED": 0,
        "NOT_APPLICABLE": 0,
    }
    assert not report.passed()
    assert report.failing()[0].probe_id == "vref"
    text = report.to_json()
    assert HarnessReport.from_json(text).to_json() == text
    assert HarnessReport.from_json(text) == report
    feedback = report.feedback()
    assert feedback.index("[FAIL]") < feedback.index("[UNKNOWN]")
    assert "port_missing:VOUT" in feedback


def test_empty_report_is_not_a_pass() -> None:
    report = HarnessReport(part="P", model_sha256="", spec_digest="d" * 64, outcomes=())
    assert not report.passed()
    assert report.feedback() == ""


def test_judge_characteristic_never_upgrades_unavailable_evidence() -> None:
    char = Characteristic(
        char_id="REQ_X",
        statement="Output high",
        unit="V",
        min_value=2.4,
        max_value=None,
        typ_value=None,
        target=None,
        source_page=None,
        excerpt="",
        req_class="DOCUMENTED_LIMIT",
        probe="io_voh",
        probe_params={},
        not_testable_reason=None,
    )
    partial = ProbeOutcome(
        probe_id="io_voh",
        status="UNKNOWN",
        measured={"io_voltage_v": 3.0},
        detail="run_timeout",
        unknown_reason="run_timeout",
        run_dir="canned",
        char_ids=("REQ_X",),
    )
    assert harness_mod.judge_characteristic(char, partial) == ("UNKNOWN", "run_timeout")

    shared = dataclasses.replace(partial, unknown_reason="characteristic_without_numeric_limit")
    assert harness_mod.judge_characteristic(char, shared)[0] == "PASS"


def test_model_ports_is_reused_for_the_harness(tmp_path: Path) -> None:
    lib = write_regulator_library(tmp_path / "buck.lib", [SUBCKT])
    assert model_ports(lib, SUBCKT) == [
        "VIN",
        "EN",
        "FB",
        "PG",
        "VOUT",
        "GND",
        "SW",
        "ILIM_MODE",
    ]
    with pytest.raises(ProbeError) as excinfo:
        model_ports(lib, "BM_ABSENT")
    assert excinfo.value.reason == "subckt_missing:BM_ABSENT"
