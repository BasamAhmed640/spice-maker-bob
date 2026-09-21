"""A model that is already known to be invalid must never be published.

The 1.1.11 run this guards against exported an invalid library and offered its
install action: the simulator had rejected line 68 in 0.77 s, the remaining 69
probes were deferred, and the model was still written into the deliverable folder.
"""

from pathlib import Path

import pytest

from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome
from boardmodeler.pipeline import make_model as engine

REJECTION = (
    "sim_output_unreadable: no .raw was written (exit_code=0 wall=0.77s raw=no log=yes); "
    "LTspice said: UCC28251.lib(68): Expected 2 node names here"
)
VALID = "* candidate\n.subckt TEST IN OUT\nR1 IN OUT 1k\n.ends TEST\n"
REJECTED = (
    "* candidate\n.subckt TEST IN OUT\n"
    "G_EA nEA GND I = 1m * (V(FB_EAM,GND) - V(nEA_ref,GND))\n"
    "R1 IN OUT 1k\n.ends TEST\n"
)


def build_run(tmp_path: Path, reason: str | None):
    """A sanity-mode run with only what the publish guard reads."""
    request = engine.MakeModelRequest(
        "TEST", "TEST", tmp_path / "fake.pdf", tmp_path / "out", verification="sanity"
    )
    run = engine._Run(request, engine._StageLog(None))
    run.workdir = Path(request.out_dir) / engine.WORK_DIRNAME
    run.workdir.mkdir(parents=True, exist_ok=True)
    outcomes = (
        ()
        if reason is None
        else (
            ProbeOutcome(
                probe_id="circuit_measurement",
                status="UNKNOWN",
                measured={},
                detail="",
                unknown_reason=reason,
                run_dir="",
                char_ids=("REQ_1",),
            ),
        )
    )
    run.report = HarnessReport(
        part="TEST", model_sha256="b" * 64, spec_digest="c" * 64, outcomes=outcomes
    )
    return run


def test_a_model_the_simulator_rejected_is_never_published(tmp_path):
    """The simulator's own rejection is evidence the model is unusable."""
    run = build_run(tmp_path, REJECTION)
    source = tmp_path / "candidate.lib"
    source.write_text(VALID, encoding="utf-8")

    with pytest.raises(ValueError, match="simulator_rejected_model"):
        run._publish(source, [])


def test_the_invalid_behavioral_source_form_is_never_published(tmp_path):
    """The deterministic rule catches the same class without a simulator run."""
    run = build_run(tmp_path, None)
    source = tmp_path / "candidate.lib"
    source.write_text(REJECTED, encoding="utf-8")

    with pytest.raises(ValueError, match="model_syntax_rejected"):
        run._publish(source, [])


def test_an_inconclusive_run_does_not_block_a_publish(tmp_path):
    """A timeout or a convergence failure is inconclusive, not a rejection."""
    run = build_run(tmp_path, "run_timeout: exit_code=-1 wall=120.00s raw=no log=yes")

    run._refuse_known_invalid(VALID)


def test_a_clean_model_and_report_pass_the_guard(tmp_path):
    run = build_run(tmp_path, "no .raw was written (exit_code=1 wall=0.10s raw=no log=yes)")

    run._refuse_known_invalid(VALID)


def test_a_deferred_hard_failure_still_refuses(tmp_path):
    """The harness wraps a rejection in the deferral prefix, so it must be unwrapped."""
    run = build_run(tmp_path, f"deferred_after_invalid_simulation: {REJECTION}")
    source = tmp_path / "candidate.lib"
    source.write_text(VALID, encoding="utf-8")

    with pytest.raises(ValueError, match="simulator_rejected_model"):
        run._publish(source, [])


def test_a_soft_reason_naming_a_rejection_word_does_not_refuse(tmp_path):
    """A warning that merely contains 'undefined' must not withhold a valid model."""
    run = build_run(tmp_path, "waveform_that_cannot_answer: the signal is undefined in this window")

    run._refuse_known_invalid(VALID)


def test_a_soft_prefix_quoting_a_diagnostic_does_not_refuse(tmp_path):
    """A timeout is inconclusive: it must never be reported as an invalid model."""
    run = build_run(tmp_path, "run_timeout: LTspice said: deck.cir(4): No such node.")

    run._refuse_known_invalid(VALID)


def test_a_soft_reason_naming_a_library_does_not_refuse(tmp_path):
    """Only the reasons the pipeline actually produces count as a hard failure."""
    run = build_run(tmp_path, "library_note: the vendor library is unrecognized here")

    run._refuse_known_invalid(VALID)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "UCC28251.lib(68): Expected 2 node names here.",
        "TEST.lib(3): No such node.",
        "deck.cir(23): This sub-circuit name is not defined.",
    ],
)
def test_every_localized_ltspice_diagnostic_refuses(tmp_path, diagnostic: str):
    """A rejection is recognized by LTspice's own file(line) shape, not by wording."""
    run = build_run(
        tmp_path,
        "sim_output_unreadable: no .raw was written (exit_code=1 wall=0.5s raw=no log=yes); "
        f"LTspice said: {diagnostic}",
    )
    source = tmp_path / "candidate.lib"
    source.write_text(VALID, encoding="utf-8")

    with pytest.raises(ValueError, match="simulator_rejected_model"):
        run._publish(source, [])
