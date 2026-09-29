"""Synthetic acceptance tests; these waveforms are not measured device evidence."""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np
import pytest

from boardmodeler.authoring import buck_qualification as nominal
from boardmodeler.authoring import qualification as qualification
from boardmodeler.authoring.buck_system_fixtures import CitedRow
from boardmodeler.authoring.qualification import (
    MANDATORY_CHECKS,
    NOMINAL_CHECKS,
    QualificationPlan,
    build_buck_qualification_plan,
    run_qualification,
)
from boardmodeler.domain.hashing import sha256_file
from boardmodeler.simulation.ltspice import BatchResult
from tests.authoring.test_buck_system_fixtures import _model, _raw, _source


def _inputs(tmp_path):
    spec, source = _source(tmp_path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    renamed = {"vref": nominal.ROW_IDS["vref"], "iss": nominal.ROW_IDS["ss_charge"]}
    rows = tuple(
        replace(row, char_id=renamed.get(row.char_id, row.char_id)) for row in spec.characteristics
    )
    for row in raw["requirements"]:
        row["req_id"] = renamed.get(row["req_id"], row["req_id"])
    for case, value, maximum in (("shutdown_iq", 1e-6, 5e-6), ("operating_iq", 82e-6, 110e-6)):
        row = replace(
            rows[-1],
            char_id=nominal.ROW_IDS[case],
            statement=case,
            excerpt=f"synthetic {case} fixture row",
            min_value=None,
            typ_value=value,
            max_value=maximum,
            target=value,
        )
        rows += (row,)
        raw["requirements"].append(
            {
                "req_id": row.char_id,
                "citation_verified": True,
                "limits": {"unit": "A", "min": None, "typ": value, "max": maximum},
                "evidence": [
                    {
                        "doc_id": spec.doc_id,
                        "excerpt": row.excerpt,
                        "page": {"pdf_page": row.source_page},
                    }
                ],
            }
        )
    source.write_text(json.dumps(raw), encoding="utf-8")
    return replace(spec, characteristics=rows), source


def _fixture_waveform(bench, case, value, *, ready=True):
    t = np.linspace(0.0, bench.stop_s, 2001)

    def full(v):
        return np.full_like(t, v)

    vin = 12.0 if ready else 8.0
    en, ss, vsense, out, current = 3.0, full(1.0), 0.8, 0.8 * (1 + 10.2 / 4.75), 1.0
    if case == "vref":
        vsense, out = value, value * (1 + 10.2 / 4.75)
    elif case == "ss_charge":
        ss = value / 15e-9 * t
    elif case == "shutdown_iq":
        en, ss, out, current = 0.0, full(0.0), 0.0, 0.0
    elif case == "operating_iq":
        vsense, out, current = 0.85, 0.0, 0.0
    return _raw(
        (
            "time",
            "V(vin)",
            "V(dut_vin)",
            "V(en)",
            "V(ss)",
            "V(vsense)",
            "V(out)",
            "V(ph)",
            "V(comp)",
            "I(Lout)",
            "I(Vdutvin)",
        ),
        (
            t,
            full(vin),
            full(vin),
            full(en),
            ss,
            full(vsense),
            full(out),
            full(0.0),
            full(1.0),
            full(current),
            full(value),
        ),
    )


def test_frozen_plan_keeps_independent_limits_when_candidate_changes(tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source, include_controls=True)
    candidate = _model(tmp_path)
    serialized = plan.to_json()
    digest = plan.sha256
    candidate.write_text(candidate.read_text() + "* candidate VREF changed to 0.72\n")
    assert (
        build_buck_qualification_plan(spec, source, include_controls=True).to_json() == serialized
    )
    assert QualificationPlan.from_json(serialized).sha256 == digest
    check = plan.checks[0]
    row = CitedRow(**json.loads(check.row_json))
    bench = qualification._bench(check.bench_json)
    assert (
        nominal._judge(
            row, nominal._measure("vref", "SW", bench, row, _fixture_waveform(bench, "vref", 0.72))
        )["verdict"]
        == "FAIL"
    )
    assert plan.to_json() == serialized and plan.sha256 == digest
    with pytest.raises(FrozenInstanceError):
        plan.mode = "AVG"
    decoded = json.loads(check.bench_json)
    decoded["metadata"]["ss_cap_f"] = 1
    assert json.loads(check.bench_json)["metadata"]["ss_cap_f"] == 15e-9


@pytest.mark.parametrize(
    ("case", "clean", "wrong"),
    [
        ("vref", 0.8, 0.72),
        ("ss_charge", 2e-6, 1e-6),
        ("shutdown_iq", 1e-6, 10e-6),
        ("operating_iq", 82e-6, 200e-6),
    ],
)
def test_synthetic_clean_fault_and_wrong_state(case, clean, wrong, tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    check = next(c for c in plan.checks if c.check_id == case)
    bench = qualification._bench(check.bench_json)
    row = CitedRow(**json.loads(check.row_json))
    for value, ready, expected in (
        (clean, True, "PASS"),
        (wrong, True, "FAIL"),
        (clean, False, "UNKNOWN"),
    ):
        measurement = nominal._measure(
            case, "SW", bench, row, _fixture_waveform(bench, case, value, ready=ready)
        )
        assert nominal._judge(row, measurement)["verdict"] == expected


def test_plan_rejects_removed_checks_or_replaced_limits(tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    with pytest.raises(ValueError, match="mandatory_checklist"):
        replace(plan, checks=plan.checks[:-1])
    check = plan.checks[0]
    row = json.loads(check.row_json)
    row["min_value"] = 0.7
    changed = replace(check, row_json=qualification._canonical(row))
    with pytest.raises(ValueError, match="limit_not_verified_source"):
        replace(plan, checks=(changed, *plan.checks[1:]))
    with pytest.raises(ValueError, match="requirements_changed"):
        replace(plan, requirements_json=plan.requirements_json + " ")


def test_missing_citations_and_avg_gaps_stay_in_mandatory_checklist(tmp_path):
    spec, source = _inputs(tmp_path)
    raw = json.loads(source.read_text())
    raw["requirements"][0]["citation_verified"] = False
    source.write_text(json.dumps(raw))
    plan = build_buck_qualification_plan(spec, source)
    assert tuple(c.check_id for c in plan.checks) == MANDATORY_CHECKS
    assert all(c.kind == "gap" and c.reason for c in plan.checks)
    spec, source = _inputs(tmp_path)
    avg = build_buck_qualification_plan(spec, source, mode="AVG")
    assert all(
        c.kind == "gap" and "averaged_model" in c.reason
        for c in avg.checks
        if c.check_id in ("edge", "ripple")
    )


def test_missing_simulator_preserves_hashes_and_explicit_gaps(tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source, include_controls=True)
    model = _model(tmp_path)
    report = run_qualification(plan, model, None, tmp_path / "run", design_sha256="design-id")
    assert report["status"] == "BLOCKED" and not report["family_qualified"]
    assert report["model_sha256"] == sha256_file(model)
    assert report["requirements_sha256"] == sha256_file(source)
    assert report["plan_sha256"] == plan.sha256
    assert report["counts"] == {"PASS": 0, "FAIL": 0, "UNKNOWN": 12, "BLOCKED": 4}
    assert not report["controls"]["all_pairs_discriminate"]
    assert json.loads((tmp_path / "run/qualification-report.json").read_text()) == report
    assert (
        QualificationPlan.from_json((tmp_path / "run/qualification-plan.json").read_text()).sha256
        == plan.sha256
    )


def _fake_run(deck, folder, *, raw_bytes=10, with_log=True):
    raw = folder / "deck.raw"
    raw.write_bytes(b"x" * raw_bytes)
    log = folder / "deck.log"
    if with_log:
        log.write_text("Synthetic simulator stub; never device evidence")
    return BatchResult(
        deck, folder, 0, "", "", 0.01, False, raw_path=raw, log_path=log if with_log else None
    )


@pytest.mark.parametrize("failure", ["missing_log", "oversized", "candidate_changed"])
def test_incomplete_or_changed_artifacts_never_pass(tmp_path, monkeypatch, failure):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    model = _model(tmp_path)
    exe = tmp_path / "synthetic.exe"
    exe.write_bytes(b"synthetic test executable marker")

    def fake(executable, deck, folder, **kwargs):
        observed = _fake_run(
            deck,
            folder,
            raw_bytes=11 if failure == "oversized" else 10,
            with_log=failure != "missing_log",
        )
        if failure == "candidate_changed":
            model.write_text(model.read_text() + "* changed\n")
        return observed

    monkeypatch.setattr(qualification, "run_batch", fake)
    monkeypatch.setattr(
        qualification,
        "read_raw",
        lambda path: pytest.fail("incomplete evidence must not be measured"),
    )
    report = run_qualification(plan, model, exe, tmp_path / "run", max_raw_bytes=10)
    assert report["counts"]["PASS"] == report["counts"]["FAIL"] == 0
    assert report["status"] == "UNKNOWN"
    assert report["results"][0]["artifacts"]["raw"]["sha256"]
    if failure == "oversized":
        assert "output_limit_exceeded" in report["results"][0]["reason"]


def test_synthetic_runner_pairs_require_clean_pass_and_fault_fail(tmp_path, monkeypatch):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source, include_controls=True)
    model = _model(tmp_path)
    exe = tmp_path / "synthetic.exe"
    exe.write_bytes(b"synthetic test executable marker")
    monkeypatch.setattr(
        qualification, "run_batch", lambda exe, deck, folder, **kw: _fake_run(deck, folder)
    )
    clean_values = dict(zip(NOMINAL_CHECKS, (0.8, 2e-6, 1e-6, 82e-6), strict=True))

    def wave(path):
        name = path.parent.name
        check = next(c for c in plan.checks if c.check_id == name)
        case = name.removesuffix("_fault")
        if case not in NOMINAL_CHECKS:
            raise ValueError("synthetic test covers only nominal rows")
        bench = qualification._bench(check.bench_json)
        value = nominal.FAULT_OVERRIDES[case][1] if check.variant == "fault" else clean_values[case]
        return _fixture_waveform(bench, case, value)

    monkeypatch.setattr(qualification, "read_raw", wave)
    report = run_qualification(plan, model, exe, tmp_path / "run")
    assert report["counts"] == {"PASS": 4, "FAIL": 0, "UNKNOWN": 12, "BLOCKED": 0}
    assert report["controls"]["all_pairs_discriminate"]
    assert report["status"] == "UNKNOWN" and not report["family_qualified"]
    assert all(
        {"raw", "log", "deck"} <= set(r["artifacts"])
        for r in report["results"]
        if r["status"] == "PASS"
    )


def test_runner_enforces_bounds_and_cancellation(tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    model = _model(tmp_path)
    with pytest.raises(ValueError, match="120_seconds"):
        run_qualification(plan, model, None, tmp_path / "run", timeout_s=121)
    with pytest.raises(ValueError, match="64_MiB"):
        run_qualification(plan, model, None, tmp_path / "run", max_raw_bytes=65 * 1024 * 1024)
    exe = tmp_path / "synthetic.exe"
    exe.write_bytes(b"synthetic test executable marker")
    cancel = threading.Event()
    cancel.set()
    report = run_qualification(plan, model, exe, tmp_path / "run", cancel=cancel)
    assert report["status"] == "UNKNOWN" and report["simulation_seconds"] == 0
    assert "cancelled" in report["results"][0]["reason"]


def test_M2_diagnostics_are_opt_in_but_keep_identical_mandatory_coverage(tmp_path):
    spec, source = _inputs(tmp_path)
    normal = build_buck_qualification_plan(spec, source)
    diagnostic = build_buck_qualification_plan(spec, source, include_m2_observations=True)
    assert tuple(c.check_id for c in normal.checks) == tuple(c.check_id for c in diagnostic.checks)
    assert sum(c.kind == "nominal" for c in normal.checks) == 4
    assert sum(c.kind == "m2" for c in normal.checks) == 0
    assert sum(c.kind == "m2" for c in diagnostic.checks) == 7
    assert all(c.kind == "gap" and c.reason for c in diagnostic.checks[-5:])


def test_output_watcher_cancels_running_simulator_on_size_limit(tmp_path, monkeypatch):
    def fake(executable, deck, folder, **kwargs):
        observed = _fake_run(deck, folder, raw_bytes=11)
        assert kwargs["cancel"].wait(1.0), "watcher must stop the live oversized producer"
        return observed

    monkeypatch.setattr(qualification, "run_batch", fake)
    _, oversized = qualification._run_bounded(
        tmp_path / "fake.exe",
        tmp_path / "deck.cir",
        tmp_path,
        timeout_s=1.0,
        cancel=None,
        max_raw_bytes=10,
    )
    assert oversized


def test_implementation_changes_block_reuse_of_frozen_plan(tmp_path, monkeypatch):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    model = _model(tmp_path)
    monkeypatch.setattr(qualification, "_implementation_hashes", lambda: (("changed", "digest"),))
    report = run_qualification(plan, model, None, tmp_path / "run")
    assert report["status"] == "UNKNOWN"
    assert "implementation_changed" in report["results"][0]["reason"]


def test_rerun_keeps_prior_candidate_receipts_and_uses_new_artifact_folder(tmp_path):
    spec, source = _inputs(tmp_path)
    plan = build_buck_qualification_plan(spec, source)
    model = _model(tmp_path)
    first = run_qualification(plan, model, None, tmp_path / "run")
    receipt = Path(first["evidence_directory"]) / "qualification-report.json"
    saved = receipt.read_bytes()
    second = run_qualification(plan, model, None, tmp_path / "run")
    assert first["execution_id"] != second["execution_id"]
    assert first["candidate_path"] != second["candidate_path"]
    assert receipt.read_bytes() == saved
    assert first["model_sha256"] == second["model_sha256"]
    assert first["plan_sha256"] == second["plan_sha256"]
