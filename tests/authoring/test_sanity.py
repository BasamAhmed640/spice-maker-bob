"""Structural checks and quick-mode control flow using synthetic circuit fixtures."""

import json
import threading
import types
from dataclasses import replace
from pathlib import Path

import pytest

from boardmodeler.authoring.backends import AuthorResult
from boardmodeler.authoring.loop import prepare_workdir
from boardmodeler.authoring.sanity import author_model, check_model, load_check, record_load
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.domain.records import Requirement
from boardmodeler.pipeline import make_model as engine
from boardmodeler.simulation import ltspice as ltspice_mod
from boardmodeler.simulation.ltspice import BatchResult, LtspiceLockTimeout

PINS = (
    {"name": "IN", "physical_pin": "1", "direction": "input"},
    {"name": "OUT", "physical_pin": "2", "direction": "output"},
)
VALID = "* TEST_FIXTURE: resistor, not device data\n.subckt TEST IN OUT\nR1 IN OUT 1k\n.ends TEST\n"


class Backend:
    name = "synthetic-test"

    def __init__(self, text: str | None = VALID, tamper: bool = False):
        self.text, self.tamper, self.calls = text, tamper, 0

    def availability(self):
        return True, "test fixture"

    def author(self, request, cancel):
        self.calls += 1
        if self.text:
            (request.model_dir / "TEST.lib").write_text(self.text, encoding="utf-8")
        if self.tamper:
            (request.workdir / "spec/characteristics.json").write_text("{}", encoding="utf-8")
        return AuthorResult(True, "synthetic circuit written", {}, "", None)


@pytest.mark.parametrize(
    "text, error",
    [
        (VALID.replace(".ends TEST", ""), "missing .ends"),
        (VALID.replace("R1 IN OUT 1k", "R1 IN OUT 1k\nR1 IN OUT 2k"), "duplicate component"),
        (VALID.replace("IN OUT\nR1", "IN BAD\nR1"), "physical pin map"),
        (VALID.replace("R1 IN OUT 1k", "X1 IN OUT MISSING"), "unresolved subcircuit"),
        (VALID.replace("R1 IN OUT 1k", '.include "outside.lib"'), "external library"),
        (VALID.replace("R1 IN OUT 1k", "R1 IN OUT {1k"), "braces"),
    ],
)
def test_structural_failures(tmp_path, text, error):
    path = tmp_path / "TEST.lib"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        check_model(path, "TEST", PINS)


def test_internal_subcircuits_and_parameters(tmp_path):
    path = tmp_path / "TEST.lib"
    path.write_text(
        ".subckt TEST IN OUT\nX1 IN OUT CHILD gain = {2}\n.ends TEST\n"
        ".subckt CHILD A B params: gain=1\nR1 A B {gain}\n.ends CHILD\n",
        encoding="utf-8",
    )
    result = check_model(path, "TEST", PINS)
    assert result["electrical_accuracy_verified"] is False
    assert result["simulation_run"] is False


# --------------------------------------------------------------------------- #
# the bounded load check: it may say "loaded" or nothing, never "verified"

CLEAN_LOG = "Total elapsed time: 0.001 seconds\n"
OP_RAW = (
    b"Title: TEST_FIXTURE\nPlotname: Operating Point\nFlags: real\n"
    b"No. Variables: 2\nNo. Points: 1\nVariables:\n"
    b"0 V(p0) voltage\n1 V(p1) voltage\nValues:\n0 0\n0\n"
)
REJECTED_LOG = "UCC28251.lib(68): Expected 2 node names here\n"


def _model(tmp_path: Path) -> Path:
    path = tmp_path / "TEST.lib"
    path.write_text(VALID, encoding="utf-8")
    return path


def _scripted_simulator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log_text: str | None = CLEAN_LOG,
    raw: bool = True,
    exit_code: int = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    log_path: bool = True,
    raises: Exception | None = None,
    terminated_after_marker: bool = False,
    raw_bytes: bytes = OP_RAW,
) -> list[dict]:
    """Patch ``run_batch`` with a scripted run and return the calls it observed.

    ``load_check`` imports ``run_batch`` inside its own body, so patching the module
    attribute is the seam that works. Real log and raw files are written beside the
    deck, so ``parse_log`` and the artifact hashing see genuine content.
    """
    calls: list[dict] = []

    def fake(exe, deck, run_dir, **kwargs):
        calls.append({"exe": Path(exe), "deck": Path(deck), "kwargs": kwargs})
        if raises is not None:
            raise raises
        run_dir = Path(run_dir)
        log = run_dir / "load.log"
        if log_text is not None:
            log.write_text(log_text, encoding="utf-8")
        raw_file = run_dir / "load.raw"
        if raw:
            raw_file.write_bytes(raw_bytes)
        return BatchResult(
            deck=Path(deck),
            run_dir=run_dir,
            exit_code=exit_code,
            stdout="",
            stderr="",
            wall_s=0.01,
            timed_out=timed_out,
            raw_path=raw_file if raw else None,
            log_path=log if log_path else None,
            cancelled=cancelled,
            terminated_after_marker=terminated_after_marker,
        )

    monkeypatch.setattr(ltspice_mod, "run_batch", fake)
    return calls


def test_load_check_without_a_simulator_is_unavailable(tmp_path, monkeypatch):
    """Absent LTspice stays visibly unchecked instead of being called "verified"."""
    calls = _scripted_simulator(monkeypatch)

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", None)

    assert result["status"] == "unavailable"
    assert "no LTspice" in result["detail"]
    assert result["electrical_accuracy_verified"] is False
    assert calls == []


def test_load_check_after_cancellation_never_starts_the_simulator(tmp_path, monkeypatch):
    calls = _scripted_simulator(monkeypatch)
    cancel = threading.Event()
    cancel.set()

    result = load_check(
        _model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe", cancel
    )

    assert result["status"] == "cancelled"
    assert result["electrical_accuracy_verified"] is False
    assert calls == []


def test_load_check_a_clean_log_and_raw_file_is_loaded(tmp_path, monkeypatch):
    """``ok`` needs exit_code 0, no timeout, no cancellation and a log path."""
    calls = _scripted_simulator(monkeypatch)

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    assert result["status"] == "loaded"
    assert result["electrical_accuracy_verified"] is False
    assert result["wall_s"] == 0.01
    assert [call["deck"].name for call in calls] == ["load.cir"]
    assert {"load.cir", "load.log", "load.raw"} <= set(result["artifacts"])


def test_load_check_a_timeout_is_inconclusive_not_loaded(tmp_path, monkeypatch):
    _scripted_simulator(monkeypatch, exit_code=-1, timed_out=True, raw=False)

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    assert result["status"] == "inconclusive"
    assert result["electrical_accuracy_verified"] is False


@pytest.mark.parametrize(
    ("log_text", "raw_bytes", "expected"),
    [
        (CLEAN_LOG, OP_RAW, "loaded"),
        ("", OP_RAW, "inconclusive"),
        (CLEAN_LOG, b"truncated raw", "inconclusive"),
    ],
)
def test_completed_load_survives_watchdog_cleanup(
    tmp_path, monkeypatch, log_text, raw_bytes, expected
):
    _scripted_simulator(
        monkeypatch,
        exit_code=1,
        terminated_after_marker=True,
        log_text=log_text,
        raw_bytes=raw_bytes,
    )
    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")
    assert result["status"] == expected
    assert result["electrical_accuracy_verified"] is False


def test_load_check_without_a_log_does_not_raise(tmp_path, monkeypatch):
    """A missing log is an ordinary outcome; the defect was ``parse_log(None)``."""
    _scripted_simulator(monkeypatch, log_text=None, log_path=False, raw=False)

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    assert result["status"] == "inconclusive"
    assert result["electrical_accuracy_verified"] is False


def test_load_check_a_busy_simulator_is_unavailable_without_waiting(tmp_path, monkeypatch):
    """A locked simulator must not stall quick mode for the 900 s lock default."""
    calls = _scripted_simulator(
        monkeypatch, raises=LtspiceLockTimeout("another run holds the lock")
    )

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    assert result["status"] == "unavailable"
    assert "LtspiceLockTimeout" in result["detail"]
    assert calls[0]["kwargs"]["lock_timeout_s"] == 0.0


def test_load_check_repeats_the_simulators_own_rejection(tmp_path, monkeypatch):
    _scripted_simulator(monkeypatch, log_text=REJECTED_LOG, raw=False)

    with pytest.raises(ValueError) as excinfo:
        load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    message = str(excinfo.value)
    assert "UCC28251.lib(68): Expected 2 node names here" in message
    assert "rejected" in message


@pytest.mark.parametrize(
    "diagnostic",
    [
        "UCC28251.lib(68): Expected 2 node names here.",
        "TEST.lib(3): No such node.",
        "deck.cir(23): This sub-circuit name is not defined.",
    ],
)
def test_load_check_repeats_any_ltspice_diagnostic_shape(tmp_path, monkeypatch, diagnostic: str):
    """Real rejections include wordings a keyword list cannot anticipate.

    The observed escape was ``No such node.``: LTspice rejected the deck, the wording
    matched no known phrase, and the model was published anyway.
    """
    _scripted_simulator(monkeypatch, log_text=f"Circuit: load.cir\n{diagnostic}\n", raw=False)

    with pytest.raises(ValueError) as excinfo:
        load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    message = str(excinfo.value)
    assert diagnostic in message, message
    assert "rejected" in message


def test_a_model_that_parses_but_never_converges_is_inconclusive(tmp_path, monkeypatch):
    """The real repaired UCC28251 outcome: parsed, no operating point, not a rejection."""
    _scripted_simulator(
        monkeypatch,
        log_text=(
            "Circuit: load.cir\n"
            "Direct Newton iteration failed to find the operating point.\n"
            "Gmin stepping failed to find operating point.\n"
            "Iteration limit reached\n"
        ),
        raw=False,
        exit_code=1,
        timed_out=True,
    )

    result = load_check(_model(tmp_path), "TEST", tmp_path / "check", tmp_path / "LTspice.exe")

    assert result["status"] == "inconclusive"
    assert result["electrical_accuracy_verified"] is False


@pytest.mark.parametrize(
    ("status", "simulation_run"),
    [
        ("loaded", True),
        ("inconclusive", True),
        ("unavailable", False),
        ("cancelled", False),
    ],
)
def test_record_load_states_whether_a_simulation_ran(status, simulation_run):
    """``simulation_run`` is truthful per status, and accuracy stays unverified."""
    checked = {"simulation_run": False, "electrical_accuracy_verified": False}
    load = {"status": status, "electrical_accuracy_verified": False}

    returned = record_load(checked, load)

    assert returned is checked
    assert returned["load_check"] == load
    assert returned["simulation_run"] is simulation_run
    assert returned["electrical_accuracy_verified"] is False
    assert ("load ran" in returned["simulation_note"]) is simulation_run


def test_cache_requires_matching_spec_and_model(tmp_path):
    spec = SpecSet("TEST", "TEST", "TEST_FIXTURE", (), PINS)
    backend = Backend()
    prepare_workdir(spec=spec, subckt="TEST", workdir=tmp_path)
    _, count = author_model(spec, backend, tmp_path, None, lambda _: None, {})
    assert count == 1
    assert author_model(spec, backend, tmp_path, None, lambda _: None, {})[1] == 0
    changed = replace(spec, doc_id="ANOTHER_TEST_FIXTURE")
    prepare_workdir(spec=changed, subckt="TEST", workdir=tmp_path)
    assert author_model(changed, backend, tmp_path, None, lambda _: None, {})[1] == 1
    (tmp_path / "model/TEST.lib").write_text(VALID.replace("1k", "2k"), encoding="utf-8")
    assert author_model(changed, backend, tmp_path, None, lambda _: None, {})[1] == 1
    assert backend.calls == 3


def test_repairs_are_bounded_and_spec_tampering_stops(tmp_path):
    spec = SpecSet("TEST", "TEST", "TEST_FIXTURE", (), PINS)
    prepare_workdir(spec=spec, subckt="TEST", workdir=tmp_path)
    broken = Backend(VALID.replace(".ends TEST", ""))
    with pytest.raises(ValueError, match="sanity_check_failed"):
        author_model(spec, broken, tmp_path, None, lambda _: None, {})
    assert broken.calls == 2
    assert not (tmp_path / "model/TEST.lib").exists()
    with pytest.raises(ValueError, match="spec_tampered"):
        author_model(spec, Backend(tamper=True), tmp_path, None, lambda _: None, {})


def test_cancellation_during_cached_load_keeps_receipt_and_never_reauthors(tmp_path, monkeypatch):
    import threading

    from boardmodeler.authoring import sanity

    spec = SpecSet("TEST", "TEST", "TEST_FIXTURE", (), PINS)
    backend = Backend()
    prepare_workdir(spec=spec, subckt="TEST", workdir=tmp_path)
    author_model(spec, backend, tmp_path, None, lambda _: None, {})
    receipt = (tmp_path / "sanity-report.json").read_bytes()
    cancel = threading.Event()

    def cancelled(*args, **kwargs):
        cancel.set()
        return {"status": "cancelled"}

    monkeypatch.setattr(sanity, "load_check", cancelled)
    with pytest.raises(ValueError, match="cancelled"):
        author_model(spec, backend, tmp_path, cancel, lambda _: None, {})
    assert backend.calls == 1
    assert (tmp_path / "sanity-report.json").read_bytes() == receipt


@pytest.mark.parametrize("write_model", [True, False])
def test_quick_pipeline_skips_planner_simulator_and_never_claims_accuracy(
    tmp_path, monkeypatch, write_model
):
    from boardmodeler.authoring import test_planner

    backend = Backend(VALID if write_model else None)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/regulator/tps54320/requirements.json"
    rows = [
        Requirement.model_validate(r)
        for r in json.loads(fixture.read_text(encoding="utf-8"))["requirements"][:2]
    ]

    def extract(run, cancel):
        run.requirements, run.pin_map = rows, PINS

    def forbidden(*args, **kwargs):
        pytest.fail("quick mode invoked test planning, reinforcement, or simulation")

    monkeypatch.setattr(engine._Run, "read", lambda self: None)
    monkeypatch.setattr(engine._Run, "extract", extract)
    monkeypatch.setattr(engine, "build_backend", lambda request: backend)
    # Quick mode calls locate() for the bounded load check; this test deliberately
    # states that the simulator is unavailable, rather than relying on a machine state.
    monkeypatch.setattr(engine, "locate", lambda: None)
    monkeypatch.setattr(engine, "build_model", forbidden)
    monkeypatch.setattr(engine._Run, "_gather_supporting_material", forbidden)
    monkeypatch.setattr(test_planner, "plan_bindings", forbidden)
    out = tmp_path / "out"
    old = out / "build/model/TEST.lib"
    old.parent.mkdir(parents=True)
    old.write_text(VALID, encoding="utf-8")
    request = engine.MakeModelRequest(
        "TEST", "TEST", tmp_path / "fake.pdf", out, verification="sanity"
    )
    result = engine.make_model(request)
    assert result.status == "UNKNOWN"
    assert result.counts["PASS"] == 0
    assert all(row.status == "UNKNOWN" for row in result.rows)
    if write_model:
        assert result.lib_path and result.asy_path
        assert result.card_path is not None, "a published model must have a card"
        assert "electrical accuracy unverified" in result.card_path.read_text(encoding="utf-8")
        assert json.loads((out / "harness-report.json").read_text())["outcomes"] == []
        assert backend.calls == 1
    else:
        assert result.lib_path is None, (
            "an old candidate must not be published after an empty reply"
        )
        assert backend.calls == 2


def test_quick_pipeline_records_the_one_load_check_and_publishes(tmp_path, monkeypatch):
    """With LTspice present, quick mode runs exactly one bounded deck and records it.

    The numerical harness (``build_model``) must stay untouched: this is a parse/solve
    check, not an electrical-accuracy run, and the receipt must say so.
    """
    from boardmodeler.authoring import test_planner

    backend = Backend(VALID)
    fixture = Path(__file__).resolve().parents[2] / "fixtures/regulator/tps54320/requirements.json"
    rows = [
        Requirement.model_validate(r)
        for r in json.loads(fixture.read_text(encoding="utf-8"))["requirements"][:2]
    ]

    def extract(run, cancel):
        run.requirements, run.pin_map = rows, PINS

    def forbidden(*args, **kwargs):
        pytest.fail("quick mode invoked the numerical harness or test planning")

    install = types.SimpleNamespace(path=tmp_path / "LTspice.exe")
    monkeypatch.setattr(engine._Run, "read", lambda self: None)
    monkeypatch.setattr(engine._Run, "extract", extract)
    monkeypatch.setattr(engine, "build_backend", lambda request: backend)
    monkeypatch.setattr(engine, "locate", lambda: install)
    monkeypatch.setattr(engine, "build_model", forbidden)
    monkeypatch.setattr(engine._Run, "_gather_supporting_material", forbidden)
    monkeypatch.setattr(test_planner, "plan_bindings", forbidden)

    calls: list[tuple[Path, Path]] = []

    def fake_run_batch(exe, deck, run_dir, **kwargs):
        calls.append((Path(exe), Path(deck)))
        run_dir = Path(run_dir)
        log = run_dir / "load.log"
        log.write_text(CLEAN_LOG, encoding="utf-8")
        raw = run_dir / "load.raw"
        raw.write_bytes(OP_RAW)
        return BatchResult(
            deck=Path(deck),
            run_dir=run_dir,
            exit_code=0,
            stdout="",
            stderr="",
            wall_s=0.01,
            timed_out=False,
            raw_path=raw,
            log_path=log,
        )

    monkeypatch.setattr(ltspice_mod, "run_batch", fake_run_batch)

    out = tmp_path / "out"
    request = engine.MakeModelRequest(
        "TEST", "TEST", tmp_path / "fake.pdf", out, verification="sanity"
    )
    result = engine.make_model(request)

    assert [(exe.name, deck.name) for exe, deck in calls] == [("LTspice.exe", "load.cir")], (
        "the bounded load check must be the only simulator invocation"
    )
    receipt = json.loads((out / "build/sanity-report.json").read_text(encoding="utf-8"))
    assert receipt["load_check"]["status"] == "loaded"
    assert receipt["simulation_run"] is True
    assert receipt["electrical_accuracy_verified"] is False
    assert result.lib_path is not None
    assert result.card_path is not None
    card = result.card_path.read_text(encoding="utf-8")
    assert "load check" in card.lower()
    assert "loaded" in card
