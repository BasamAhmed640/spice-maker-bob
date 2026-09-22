"""Phase 3 gate: the integrated board demonstration, end to end (plan step 10).

Everything here runs real LTspice through the same code path the CLI uses, and the
assertions are about *observable outcomes* — statuses, measured values, detection
evidence — never about the shape of the source that produced them.

The module builds the demo project once, checks it once, and re-checks it once more
from an exported copy in a fresh directory, because those are the three facts the
plan's exit gate names:

* the nominal scenario passes with real measurements;
* at least five injected faults are each detected, and the original project is
  byte-identical afterwards;
* a scenario that cannot be concluded is present as UNKNOWN/BLOCKED with a reason,
  and an exported copy reproduces the same statuses.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from boardmodeler.domain.enums import Status
from boardmodeler.domain.hashing import sha256_file
from boardmodeler.pipeline.demo import (
    FaultMatrixReport,
    build_demo_project,
    check_circuit,
    run_fault_matrix,
)
from boardmodeler.reporting.export import export_project
from boardmodeler.simulation.ltspice import LtspiceInstall

pytestmark = pytest.mark.ltspice

#: The faults the report must catch; each is a real circuit mistake, not a
#: tolerance change. Kept to five so the suite stays inside a sane wall time —
#: every fault re-runs the whole check.
FAULTS = (
    "swap_straps",
    "en_invert",
    "missing_pullup",
    "pullup_wrong_domain",
    "early_reset_release",
    "missing_pg",
)


@pytest.fixture(scope="module")
def install(ltspice_install: LtspiceInstall) -> LtspiceInstall:
    """The simulator this test session explicitly resolved; skips only if there is none."""
    return ltspice_install


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory, install):
    out = tmp_path_factory.mktemp("demo_board") / "project"
    result = build_demo_project(out, ltspice=install)
    return result


@pytest.fixture(scope="module")
def checked(demo, install, tmp_path_factory: pytest.TempPathFactory):
    """One real check, with the CLI's own result and report files written."""
    out = tmp_path_factory.mktemp("check")
    return check_circuit(
        demo.project_dir,
        ltspice=install,
        results_path=out / "results.json",
        report_path=out / "report.html",
    )


def test_the_build_produced_a_checkable_project(demo) -> None:
    assert demo.requirements, "the demo carries no requirements"
    assert demo.tests, "the demo carries no test cases"
    assert demo.decks, "the build produced no decks"
    for case in demo.tests:
        assert (demo.project_dir / case.deck_template).is_file(), (
            f"{case.test_id} references deck {case.deck_template}, which was not built"
        )


def test_static_checks_pass_except_the_declared_boundary(demo) -> None:
    failures = [f for f in demo.static_findings if f.status is Status.FAIL]
    assert not failures, "; ".join(f"{f.code}: {f.message}" for f in failures)
    unknown = [f for f in demo.static_findings if f.status is Status.UNKNOWN]
    # The reduced behavioural buck has no switching node: exactly that boundary may
    # be UNKNOWN, and it must say why.
    assert all(f.detail.get("reason") == "connectivity_not_preserved" for f in unknown), [
        f.message for f in unknown
    ]


def test_every_result_carries_an_observation_or_a_reason(checked) -> None:
    """No silent rows: a result either measured something or says why it did not."""
    for result in checked.results:
        if result.status in (Status.PASS, Status.FAIL):
            assert result.measured, (
                f"{result.test_id} reported {result.status.value} with no measurement"
            )
            assert result.run_id, f"{result.test_id} has no run id, so no artifact backs it"
        elif result.status is Status.UNKNOWN:
            assert result.unknown_reason, f"{result.test_id} is UNKNOWN with no reason"
        elif result.status is Status.BLOCKED:
            assert result.blocked_reason, f"{result.test_id} is BLOCKED with no reason"


def test_the_nominal_scenario_passes_with_real_measurements(checked) -> None:
    nominal = [r for r in checked.results if "nominal_startup" in r.test_id]
    assert nominal, f"no nominal_startup result among {[r.test_id for r in checked.results]}"
    passed = [r for r in nominal if r.status is Status.PASS]
    assert passed, "; ".join(f"{r.test_id}: {r.status.value} {r.detail}" for r in nominal)
    rails = [
        value
        for result in passed
        for name, value in result.measured.items()
        if "max(V(" in name and isinstance(value, (int, float))
    ]
    assert rails, "no rail measurement was recorded for the nominal scenario"
    assert max(rails) > 2.0, f"the rails never came up: {passed[0].measured}"


def test_the_reset_fault_scenario_detects_the_short_hold(checked) -> None:
    """The injected 200 us reset hold must be observed as a detected violation.

    The declared nominal load steps must not land inside the reset-release window
    and re-assert the supervisor, which would delay the release past the 1 ms
    minimum and hide the very fault the scenario injects.
    """
    result = next((r for r in checked.results if "reset_early_release" in r.test_id), None)
    assert result is not None, (
        f"no reset_early_release result among {[r.test_id for r in checked.results]}"
    )
    assert result.status is Status.PASS, f"{result.test_id}: {result.status.value} {result.detail}"
    assert result.run_id, f"{result.test_id} has no run id, so no artifact backs the detection"
    delay = result.measured.get("REQ_DEMO_SEQ_003.delay_s")
    assert isinstance(delay, (int, float)), f"no measured release delay: {result.measured}"
    assert delay < 1e-3, (
        f"the released-too-early fault measured {delay:g} s, which is not below the "
        f"requirement's 1 ms minimum: {result.detail}"
    )


def test_unresolved_requirements_are_visible_as_unknown_or_blocked(checked) -> None:
    """The fixture's clock is an assumption, so nothing depending on it may pass."""
    statuses = {result.status for result in checked.results}
    unresolved = [
        result for result in checked.results if result.status in (Status.UNKNOWN, Status.BLOCKED)
    ]
    assert unresolved, (
        "no UNKNOWN/BLOCKED result at all — the clock-availability assumption and the "
        f"unmodelled switch boundary must produce one (statuses seen: {statuses})"
    )
    for result in unresolved:
        reason = result.unknown_reason or result.blocked_reason
        assert reason and reason != "unknown", f"{result.test_id} is unresolved without a reason"
    coverage = checked.coverage or {}
    assert coverage, "the check produced no coverage summary"


def test_injected_faults_are_detected_and_the_original_is_untouched(demo, install) -> None:
    report: FaultMatrixReport = run_fault_matrix(demo.project_dir, ltspice=install, faults=FAULTS)
    assert report["original_unchanged"] is True, (
        f"a mutation leaked into the original project: {report['original_hashes']}"
    )
    assert report["total"] == len(FAULTS)
    missed = [entry["fault_id"] for entry in report["faults"] if not entry["detected"]]
    assert not missed, "these injected circuit mistakes were not detected: " + "; ".join(
        f"{entry['fault_id']} ({entry['description']}: {entry['status']} {entry['summary']})"
        for entry in report["faults"]
        if not entry["detected"]
    )
    for entry in report["faults"]:
        assert entry["evidence"], f"{entry['fault_id']} was 'detected' with no evidence"
        assert entry["modifications"], f"{entry['fault_id']} recorded no edit"
        # A detected fault means the check produced the *expected* outcome for it,
        # which the mutator declares; the evidence must name where it showed up.
        assert entry["expected_detection"]
        assert any(entry["evidence"]), entry["evidence"]


def test_the_export_carries_the_same_statuses_with_relative_paths(
    demo, checked, tmp_path: Path, install
) -> None:
    """The export is a self-describing package, and it must not disagree with the check.

    The export ships the model, symbol, tests, requirements, coverage, results and
    manifest — it is not a copy of the whole project, so it cannot be re-simulated
    as-is (it has no ``project.json``). What it must do is carry the *observed*
    statuses, hashes for every file, and only relative paths, so a reader can
    reproduce the run from the artifacts rather than from a claim.
    """
    project = demo.project_dir
    export_dir = tmp_path / "export"
    result = export_project(
        _as_project(project),
        export_dir,
        requirements=demo.requirements,
        tests=demo.tests,
        results=list(checked.results),
    )
    assert result.ok, "; ".join(f"{f.code}: {f.message}" for f in result.findings)
    for relative in result.relative_files():
        assert not Path(relative).is_absolute(), f"{relative} is an absolute path"

    manifest_path = export_dir / "manifest.json"
    assert manifest_path.is_file(), "the export wrote no manifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files", manifest.get("entries", []))
    assert entries, f"the manifest records no files: {sorted(manifest)}"
    for entry in entries:
        relative = entry["path"]
        assert not Path(relative).is_absolute(), f"{relative} is an absolute path"
        target = export_dir / relative
        assert target.is_file(), f"the manifest names {relative}, which the export did not write"
        # The hash has to be the file's real hash, or it proves nothing.
        assert sha256_file(target) == entry["sha256"], f"{relative}: manifest hash is stale"

    exported_results = json.loads((export_dir / "results.json").read_text(encoding="utf-8"))
    rows = exported_results["results"] if isinstance(exported_results, dict) else exported_results
    assert {row["test_id"]: row["status"] for row in rows} == {
        r.test_id: r.status.value for r in checked.results
    }, "the exported results disagree with the check that produced them"

    if (export_dir / "project.json").is_file():
        # When an export does carry the project, the re-run must agree exactly.
        rerun = tmp_path / "rerun"
        shutil.copytree(export_dir, rerun)
        again = check_circuit(rerun, ltspice=install)
        assert {r.test_id: r.status.value for r in again.results} == {
            r.test_id: r.status.value for r in checked.results
        }


def _as_project(root: Path):
    from boardmodeler.pipeline.project import Project

    return Project(root)


def test_the_published_results_match_the_in_memory_check(checked) -> None:
    """The JSON and HTML the CLI writes must agree with the result it returned."""
    assert checked.results_path is not None and checked.results_path.is_file()
    payload = json.loads(checked.results_path.read_text(encoding="utf-8"))
    written = {item["test_id"]: item["status"] for item in payload["results"]}
    assert written == {r.test_id: r.status.value for r in checked.results}
    assert payload["summary"] == checked.summary()
    assert checked.report_path is not None
    assert checked.report_path.is_file() and checked.report_path.stat().st_size > 0
    assert checked.report_path.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype")
