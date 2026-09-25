"""A recovered operating-point search is a found operating point, not a failure."""

from __future__ import annotations

from boardmodeler.simulation.log import parse_log

RECOVERED = (
    "LTspice 26.0.0 for Windows\n"
    "Direct Newton iteration failed to find operating point.\n"
    "Starting Gmin stepping\n"
    'Gmin stepping failed to find operating point. Use".option gminsteps=0" to skip all '
    "Gmin stepping.\n"
    "Starting source stepping with srcstepmethod=0\n"
    "Source stepping succeeded in finding the operating point.\n"
    "\n"
    "Total elapsed time: 2.275 seconds.\n"
)


def test_gmin_failure_then_source_stepping_success_is_not_a_convergence_failure() -> None:
    """Observed on a template buck model: LTspice recovered and simulated to tstop, yet the
    harness called it ``sim_convergence_failure`` and deferred every other fixture."""
    summary = parse_log(text=RECOVERED)
    assert summary.completed
    assert summary.convergence_issues == []
    assert any("Gmin stepping failed" in warning for warning in summary.warnings)


def test_when_every_method_fails_it_is_still_a_convergence_failure() -> None:
    failed = RECOVERED.replace(
        "Source stepping succeeded in finding the operating point.",
        "Source stepping failed to find operating point.",
    )
    summary = parse_log(text=failed)
    assert any("Gmin stepping failed" in issue for issue in summary.convergence_issues)
    assert any("Source stepping failed" in issue for issue in summary.convergence_issues)


def test_a_recovered_operating_point_does_not_excuse_a_later_transient_failure() -> None:
    later = RECOVERED.replace(
        "Total elapsed time",
        'Time step too small; time = 1e-6, timestep = 1e-19: trouble with node "q"\n'
        "Total elapsed time",
    )
    summary = parse_log(text=later)
    assert any("Time step too small" in issue for issue in summary.convergence_issues)
    assert not any("stepping failed" in issue.lower() for issue in summary.convergence_issues)
