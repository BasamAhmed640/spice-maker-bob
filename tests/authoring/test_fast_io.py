"""Electrical I/O discrimination, evidence reuse, and preserved operating points."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from boardmodeler.authoring.backends import ScriptedBackend
from boardmodeler.authoring.conditions import operating_params
from boardmodeler.authoring.harness import run_harness
from boardmodeler.authoring.io_probes import _cross
from boardmodeler.authoring.loop import BuildRequest, build_model
from boardmodeler.authoring.probes import PROBES
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.authoring.validation_cache import read_report, validation_key, write_report
from boardmodeler.domain.hashing import sha256_file
from boardmodeler.domain.records import Condition

# Synthetic electrical test double, not a model of a vendor device.
BUFFER = """* TEST_FIXTURE: synthetic noninverting tri-state buffer
.subckt IO VCC A Y GND OE
Bdriver Y GND I=if(V(VCC,GND)>1,if(V(OE,GND)>1,(V(Y,GND)-V(A,GND))/25,0),0)
Rleak Y GND 1e12
Rinput A GND 1e12
.ends IO
"""


def io_spec():
    chars = []
    for probe_id, lo, hi in (
        ("io_voh", 3.2, 3.3),
        ("io_vol", 0, 0.1),
        ("io_leakage", 0, 1e-9),
        ("io_power_off_leakage", 0, 1e-9),
        ("io_input_leakage", 0, 1e-9),
        ("io_delay_rise", 0, 5e-9),
        ("io_delay_fall", 0, 5e-9),
        ("io_rise_time", 0, 5e-9),
        ("io_fall_time", 0, 5e-9),
    ):
        chars.append(
            Characteristic(
                char_id=probe_id,
                statement="TEST_FIXTURE",
                unit=PROBES[probe_id].unit,
                min_value=lo,
                max_value=hi,
                probe=probe_id,
                typ_value=None,
                target=None,
                source_page=0,
                excerpt="synthetic",
                req_class="DOCUMENTED_LIMIT",
                not_testable_reason=None,
                probe_params={
                    "io_vcc": 3.3,
                    "io_load_a": 0.002,
                    "io_cap_f": 15e-12,
                    "io_test_v": 3.3,
                    "io_inverting": 0,
                    "io_oe_active_high": 1,
                },
            )
        )
    return SpecSet(
        part="SYNTHETIC_IO", subckt="IO", doc_id="TEST_FIXTURE", characteristics=tuple(chars)
    )


@pytest.mark.ltspice
def test_real_io_model_is_judged_and_bad_output_resistance_fails(tmp_path, ltspice_exe):
    model = tmp_path / "buffer.lib"
    model.write_text(BUFFER)
    spec = io_spec()
    good = run_harness(
        model_lib=model, subckt="IO", spec=spec, workdir=tmp_path / "good", ltspice=ltspice_exe
    )
    assert good.passed(), good.feedback()
    assert len(good.outcomes) == 9
    assert all(len(row.artifacts) == 2 for row in good.outcomes)
    model.write_text(BUFFER.replace("/25", "/250"))
    bad = run_harness(
        model_lib=model,
        subckt="IO",
        spec=replace(spec, characteristics=spec.characteristics[:2]),
        workdir=tmp_path / "bad",
        ltspice=ltspice_exe,
    )
    assert len(bad.failing()) == 2, bad.feedback()


@pytest.mark.ltspice
def test_repeat_build_uses_real_evidence_without_author_or_simulator(
    tmp_path, ltspice_exe, monkeypatch
):
    from boardmodeler.authoring import loop

    spec = replace(io_spec(), characteristics=io_spec().characteristics[:1])
    calls = []

    def write(turn, workdir, prompt):
        calls.append(prompt)
        (workdir / "model/IO.lib").write_text(BUFFER)

    request = BuildRequest(
        part=spec.part,
        subckt="IO",
        spec=spec,
        workdir=tmp_path,
        ltspice=ltspice_exe,
        backend=ScriptedBackend(write),
    )
    first = build_model(request)
    assert first.status == "PASS", first.report.feedback()
    assert len(calls) == 1

    def forbidden(**kwargs):
        pytest.fail("a valid repeat must not simulate or invoke the author")

    monkeypatch.setattr(loop, "run_harness", forbidden)
    second = build_model(replace(request, backend=ScriptedBackend(forbidden)))
    assert second.status == "PASS" and second.iterations == 0
    assert second.report.model_sha256 == first.report.model_sha256
    key = validation_key(tmp_path / "model/IO.lib", spec, ltspice_exe, request.timeout_s)
    cache = tmp_path / "validation-cache"
    assert read_report(cache, key, spec, tmp_path / "model/IO.lib") is not None
    report_path = cache / key / "report.json"
    altered = json.loads(report_path.read_text())
    altered["outcomes"][0]["measured"]["io_voltage_v"] = 100.0
    report_path.write_text(json.dumps(altered))
    assert read_report(cache, key, spec, tmp_path / "model/IO.lib") is None
    write_report(cache, key, first.report)
    raw = next(path for path in first.report.outcomes[0].artifacts if path.endswith(".raw"))
    from pathlib import Path

    Path(raw).write_bytes(b"corrupted")
    assert read_report(cache, key, spec, tmp_path / "model/IO.lib") is None
    # A damaged artifact is not made trustworthy by rewriting the report.
    write_report(cache, key, first.report)
    assert read_report(cache, key, spec, tmp_path / "model/IO.lib") is None


def _forge_cache_entry(cache_root, key, model, report) -> None:
    """Write a genuine passing report and its artifacts under ``key`` for ``model``.

    This is exactly what an author with filesystem access can do. It relabels the model
    hash, run directory and artifact paths, and keeps the measured values and hashes.
    """
    directory = cache_root / key
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.loads(report.to_json())
    payload["model_sha256"] = sha256_file(model)
    for row in payload["outcomes"]:
        row["run_dir"] = str(directory)
        artifacts = {}
        for filename, digest in row["artifacts"].items():
            target = directory / Path(filename).name
            target.write_bytes(Path(filename).read_bytes())
            artifacts[str(target)] = digest
        row["artifacts"] = artifacts
    (directory / "report.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.ltspice
def test_borrowed_passing_waveforms_do_not_validate_a_different_model(
    tmp_path, ltspice_exe, monkeypatch
):
    from boardmodeler.authoring import validation_cache

    spec = replace(io_spec(), characteristics=io_spec().characteristics[:1])

    def write_good(turn, workdir, prompt):
        (workdir / "model/IO.lib").write_text(BUFFER)

    template = BuildRequest(
        part=spec.part,
        subckt="IO",
        spec=spec,
        workdir=tmp_path / "good",
        ltspice=ltspice_exe,
        backend=ScriptedBackend(write_good),
    )
    good = build_model(template)
    assert good.status == "PASS", good.report.feedback()

    bad_dir = tmp_path / "bad"
    (bad_dir / "model").mkdir(parents=True)
    bad_model = bad_dir / "model/IO.lib"
    bad_model.write_text(BUFFER.replace("/25", "/250"))
    bad_key = validation_key(bad_model, spec, ltspice_exe, template.timeout_s)
    assert bad_key is not None
    _forge_cache_entry(bad_dir / "validation-cache", bad_key, bad_model, good.report)

    # No process observed this entry, so it is refused and the model is re-judged.
    monkeypatch.setattr(validation_cache, "_OBSERVED", {})
    assert read_report(bad_dir / "validation-cache", bad_key, spec, bad_model) is None

    def keep_bad(turn, workdir, prompt):
        (workdir / "model/IO.lib").write_text(BUFFER.replace("/25", "/250"))

    result = build_model(
        replace(template, workdir=bad_dir, backend=ScriptedBackend(keep_bad), max_iterations=1)
    )
    assert result.status != "PASS", result.report.feedback()
    assert result.report.failing(), result.report.feedback()


@pytest.mark.ltspice
def test_an_author_forged_cache_entry_is_ignored(tmp_path, ltspice_exe, monkeypatch):
    from boardmodeler.authoring import validation_cache

    spec = replace(io_spec(), characteristics=io_spec().characteristics[:1])

    def write_good(turn, workdir, prompt):
        (workdir / "model/IO.lib").write_text(BUFFER)

    template = BuildRequest(
        part=spec.part,
        subckt="IO",
        spec=spec,
        workdir=tmp_path / "good",
        ltspice=ltspice_exe,
        backend=ScriptedBackend(write_good),
    )
    good = build_model(template)
    assert good.status == "PASS", good.report.feedback()

    author_dir = tmp_path / "author"
    (author_dir / "model").mkdir(parents=True)

    def forge(turn, workdir, prompt):
        model = workdir / "model/IO.lib"
        model.write_text(BUFFER.replace("/25", "/250"))
        key = validation_key(model, spec, ltspice_exe, template.timeout_s)
        assert key is not None
        _forge_cache_entry(workdir / "validation-cache", key, model, good.report)

    monkeypatch.setattr(validation_cache, "_OBSERVED", {})
    result = build_model(
        BuildRequest(
            part=spec.part,
            subckt="IO",
            spec=spec,
            workdir=author_dir,
            ltspice=ltspice_exe,
            backend=ScriptedBackend(forge),
            max_iterations=1,
        )
    )
    assert result.status != "PASS", result.report.feedback()
    assert result.report.failing(), result.report.feedback()


def test_an_undecodable_model_is_never_reused(tmp_path: Path) -> None:
    """A model that is not UTF-8 cannot be shown to have no external includes."""
    model = tmp_path / "IO.lib"
    model.write_bytes(b".subckt IO A B\n\xff\xfe data\n.ends IO\n")
    simulator = tmp_path / "LTspice.exe"
    simulator.write_bytes(b"")
    assert validation_key(model, SimpleNamespace(), simulator, 120.0) is None


def test_an_undecodable_existing_candidate_still_reaches_the_author(
    tmp_path: Path, monkeypatch
) -> None:
    """A vendor-encoded candidate is offered to the agent as text, never a traceback."""
    from boardmodeler.authoring import loop as loop_module
    from boardmodeler.authoring.harness import HarnessReport

    spec = replace(io_spec(), characteristics=io_spec().characteristics[:1])
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model_dir.joinpath("IO.lib").write_bytes(
        b".subckt IO VCC A Y GND OE\n\xff\xfe vendor bytes\n.ends IO\n"
    )
    prompts: list[str] = []

    def script(turn, workdir, prompt):
        prompts.append(prompt)
        (workdir / "model/IO.lib").write_text(BUFFER)

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        return HarnessReport(
            part=spec.part,
            model_sha256=sha256_file(model_lib),
            spec_digest=spec.digest(),
            outcomes=(),
        )

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    result = build_model(
        BuildRequest(
            part=spec.part,
            subckt="IO",
            spec=spec,
            workdir=tmp_path,
            ltspice=tmp_path / "LTspice.exe",
            backend=ScriptedBackend(script),
            max_iterations=1,
        )
    )
    assert result.status == "UNKNOWN"
    assert prompts and "Current candidate SHA256" in prompts[0]
    assert "\ufffd" in prompts[0], "the undecodable bytes must be shown, not dropped"


def test_a_spec_with_no_covered_rows_never_invokes_the_author(tmp_path: Path, monkeypatch) -> None:
    from boardmodeler.authoring import loop as loop_module

    spec = replace(
        io_spec(),
        characteristics=tuple(replace(char, probe=None) for char in io_spec().characteristics),
    )
    calls: list[int] = []

    def script(turn, workdir, prompt):
        calls.append(turn)
        (workdir / "model/IO.lib").write_text(BUFFER)

    def forbidden(**kwargs):
        pytest.fail("a spec with no covered rows must not simulate")

    monkeypatch.setattr(loop_module, "run_harness", forbidden)
    result = build_model(
        BuildRequest(
            part=spec.part,
            subckt="IO",
            spec=spec,
            workdir=tmp_path,
            ltspice=tmp_path / "LTspice.exe",
            backend=ScriptedBackend(script),
        )
    )
    assert result.status == "UNKNOWN"
    assert "no_covered_characteristics" in result.detail
    assert calls == []


@pytest.mark.ltspice
def test_numerical_improvement_continues_even_when_same_rows_fail(tmp_path, ltspice_exe):
    spec = replace(io_spec(), characteristics=io_spec().characteristics[:2])

    def write(turn, workdir, prompt):
        resistance = (250, 125, 25)[turn - 1]
        if turn > 1:
            assert f"/{(250, 125)[turn - 2]}" in prompt
            assert "io_voltage_v" in prompt
        (workdir / "model/IO.lib").write_text(BUFFER.replace("/25", f"/{resistance}"))

    result = build_model(
        BuildRequest(
            part=spec.part,
            subckt="IO",
            spec=spec,
            workdir=tmp_path,
            ltspice=ltspice_exe,
            backend=ScriptedBackend(write),
            max_iterations=3,
            stall_patience=1,
        )
    )
    assert result.status == "PASS" and result.iterations == 3, result.history
    attempts = list((tmp_path / "candidates").glob("*/*/result.json"))
    assert len(attempts) == 3


def test_distinct_conditions_produce_distinct_cases():
    char = io_spec().characteristics[0]
    second = replace(char, char_id="1V8", probe_params={**char.probe_params, "io_vcc": 1.8})
    spec = replace(io_spec(), characteristics=(char, second))
    cases = spec.cases()
    assert len(cases) == 2 and cases[0][0] != cases[1][0]
    assert sorted(case[2][0].probe_params["io_vcc"] for case in cases) == [1.8, 3.3]
    assert SpecSet.from_json(spec.to_json()).digest() == spec.digest()


def test_crossings_in_same_sample_interval_are_interpolated():
    t, y = np.array([0.0, 1.0]), np.array([0.0, 1.0])
    first = _cross(t, y, 0.1, True, 0)
    assert _cross(t, y, 0.9, True, first) - first == pytest.approx(0.8)


def test_io_conditions_are_required_and_units_checked():
    probe = PROBES["io_voh"]

    def compile(text, overrides=None):
        return operating_params(
            SimpleNamespace(conditions=[Condition(text=text, parameter_overrides=overrides or {})]),
            probe,
        )

    assert compile("VCC = 3.3 V; IOH = -2 mA", {"io_inverting": 0}) == (
        {"io_vcc": 3.3, "io_load_a": 0.002, "io_inverting": 0, "io_input_high": 3.3},
        None,
    )
    assert compile("VCC = 3.3 V")[1].startswith("condition_missing")
    assert compile("VCC = 3.3 A")[1].startswith("condition_unit_invalid")
    assert compile("VCC = 3.3 V", {"VCC": 1.8})[1].startswith("condition_conflict")


def test_ranges_are_checked_against_explicit_nominal_regardless_of_order():
    requirement = SimpleNamespace(
        conditions=[Condition(text="", parameter_overrides={"vin_max": 2.0, "vin": 1.8})]
    )
    params, reason = operating_params(requirement, PROBES["vref"])
    assert reason is None and params["vin_dc"] == 1.8
