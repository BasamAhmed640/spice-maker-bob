"""The author loop: fail-then-pass, tampering, the cap, stalls, and BLOCKED.

``spec.py``/``harness.py`` are authored in parallel. When they have not landed
yet this module installs contract-shaped stand-ins in ``sys.modules`` so the loop
is provable on its own; when they have landed the real classes are used. Either
way ``run_harness`` is monkeypatched with a deterministic double, because what is
under test here is the loop's contract (when it runs the harness, when it stops,
and what it reports), not LTspice.

The stop conditions are satisfaction and progress: ``max_iterations`` is ``None``
by default, and an agent that repeats itself ends the build through
``stall_patience`` instead of through a clock.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
import threading
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path

from boardmodeler.authoring.backends import AuthorResult, ScriptedBackend
from boardmodeler.domain.hashing import canonical_json_bytes, sha256_file

PART = "TPS54320"
SUBCKT = "BM_TPS54320"
DOC_ID = "TPS54320_SLVS982C"
CHAR_ID = "REQ_TPS54320_ELEC_004"
OPEN_CHAR_ID = "REQ_TPS54320_THERM_001"
EXCERPT = "the device turns on when VIN rises above 4.1 V"
OPEN_REASON = "thermal shutdown is not modelled and cannot be probed deterministically"
TIMEOUT_S = 90.0


# ------------------------------------------------------ parallel-slice shims


def _fake_spec_module() -> types.ModuleType:
    module = types.ModuleType("boardmodeler.authoring.spec")

    @dataclass(frozen=True)
    class Characteristic:
        char_id: str
        statement: str
        unit: str
        min_value: float | None = None
        max_value: float | None = None
        typ_value: float | None = None
        target: float | None = None
        source_page: int | None = None
        excerpt: str = ""
        req_class: str = "DOCUMENTED_LIMIT"
        probe: str | None = None
        probe_params: dict[str, float] = field(default_factory=dict)
        not_testable_reason: str | None = None

    def _payload(instance: SpecSet) -> dict[str, object]:
        return {
            "part": instance.part,
            "subckt": instance.subckt,
            "doc_id": instance.doc_id,
            "characteristics": [asdict(entry) for entry in instance.characteristics],
        }

    @dataclass(frozen=True)
    class SpecSet:
        part: str
        subckt: str
        doc_id: str
        characteristics: tuple[Characteristic, ...]

        def digest(self) -> str:
            return hashlib.sha256(canonical_json_bytes(_payload(self))).hexdigest()

        def to_json(self) -> str:
            return json.dumps(_payload(self), indent=2, sort_keys=True)

        @classmethod
        def from_json(cls, text: str) -> SpecSet:
            data = json.loads(text)
            return cls(
                part=data["part"],
                subckt=data["subckt"],
                doc_id=data["doc_id"],
                characteristics=tuple(Characteristic(**entry) for entry in data["characteristics"]),
            )

        def by_probe(self) -> dict[str, tuple[Characteristic, ...]]:
            grouped: dict[str, list[Characteristic]] = {}
            for entry in self.covered():
                grouped.setdefault(entry.probe or "", []).append(entry)
            return {key: tuple(value) for key, value in grouped.items()}

        def covered(self) -> tuple[Characteristic, ...]:
            return tuple(entry for entry in self.characteristics if entry.probe is not None)

        def uncovered(self) -> tuple[Characteristic, ...]:
            return tuple(entry for entry in self.characteristics if entry.probe is None)

    module.Characteristic = Characteristic
    module.SpecSet = SpecSet
    return module


def _fake_harness_module() -> types.ModuleType:
    module = types.ModuleType("boardmodeler.authoring.harness")

    @dataclass(frozen=True)
    class ProbeOutcome:
        probe_id: str
        status: str
        measured: dict[str, float | str] = field(default_factory=dict)
        detail: str = ""
        unknown_reason: str | None = None
        run_dir: str = ""
        char_ids: tuple[str, ...] = ()

        def to_json(self) -> dict[str, object]:
            return {
                "probe_id": self.probe_id,
                "status": self.status,
                "measured": dict(self.measured),
                "detail": self.detail,
                "unknown_reason": self.unknown_reason,
                "run_dir": self.run_dir,
                "char_ids": list(self.char_ids),
            }

    @dataclass(frozen=True)
    class HarnessReport:
        part: str
        model_sha256: str
        spec_digest: str
        outcomes: tuple[ProbeOutcome, ...]

        def counts(self) -> dict[str, int]:
            counts: dict[str, int] = {}
            for outcome in self.outcomes:
                counts[str(outcome.status)] = counts.get(str(outcome.status), 0) + 1
            return counts

        def passed(self) -> bool:
            return bool(self.outcomes) and all(
                str(outcome.status) == "PASS" for outcome in self.outcomes
            )

        def failing(self) -> tuple[ProbeOutcome, ...]:
            return tuple(outcome for outcome in self.outcomes if str(outcome.status) != "PASS")

        def feedback(self) -> str:
            lines = [f"{outcome.probe_id}: {outcome.detail}" for outcome in self.failing()]
            return "\n".join(lines) if lines else "all probes passed"

        def to_json(self) -> str:
            return json.dumps(
                {
                    "part": self.part,
                    "model_sha256": self.model_sha256,
                    "spec_digest": self.spec_digest,
                    "outcomes": [outcome.to_json() for outcome in self.outcomes],
                },
                indent=2,
                sort_keys=True,
            )

        @classmethod
        def from_json(cls, text: str) -> HarnessReport:
            data = json.loads(text)
            return cls(
                part=data["part"],
                model_sha256=data["model_sha256"],
                spec_digest=data["spec_digest"],
                outcomes=tuple(ProbeOutcome(**entry) for entry in data["outcomes"]),
            )

    def run_harness(**kwargs: object) -> HarnessReport:
        raise AssertionError("tests must monkeypatch run_harness with a deterministic double")

    module.ProbeOutcome = ProbeOutcome
    module.HarnessReport = HarnessReport
    module.run_harness = run_harness
    return module


def _install_shims() -> None:
    for name, factory in (
        ("boardmodeler.authoring.spec", _fake_spec_module),
        ("boardmodeler.authoring.harness", _fake_harness_module),
    ):
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = factory()


_install_shims()
loop = importlib.import_module("boardmodeler.authoring.loop")
spec_module = importlib.import_module("boardmodeler.authoring.spec")
harness_module = importlib.import_module("boardmodeler.authoring.harness")
Characteristic = spec_module.Characteristic
SpecSet = spec_module.SpecSet
ProbeOutcome = harness_module.ProbeOutcome
HarnessReport = harness_module.HarnessReport


# ------------------------------------------------------------------ fixtures


def characteristic(char_id: str = CHAR_ID, *, probe: str | None = None, **overrides: object):
    fields: dict[str, object] = {
        "char_id": char_id,
        "statement": "VIN UVLO rising threshold",
        "unit": "V",
        "min_value": 3.8,
        "max_value": 4.4,
        "typ_value": 4.1,
        "target": 4.1,
        "source_page": 7,
        "excerpt": EXCERPT,
        "req_class": "DOCUMENTED_LIMIT",
        "probe": probe,
        "probe_params": {"vin_start": 0.0, "vin_stop": 6.0},
        "not_testable_reason": None,
    }
    fields.update(overrides)
    return Characteristic(**fields)


def probe_id() -> str:
    """A probe id the registry actually knows, so the port list can be derived."""
    try:
        from boardmodeler.authoring.probes import PROBES
    except Exception:
        return "uvlo_rise"
    return next(iter(PROBES), "uvlo_rise")


def probe_ids(count: int) -> tuple[str, ...]:
    """``count`` distinct probe ids the registry knows (deterministic, first-seen)."""
    try:
        from boardmodeler.authoring.probes import PROBES
    except Exception:  # pragma: no cover - the registry ships with the harness
        PROBES = {}  # type: ignore[assignment]
    known = tuple(str(name) for name in PROBES)
    if len(known) >= count:
        return known[:count]
    return tuple(f"probe_{index + 1}" for index in range(count))


def build_spec(probe: str | None = None) -> SpecSet:
    probed = characteristic(probe=probe or probe_id())
    uncovered = characteristic(
        OPEN_CHAR_ID,
        probe=None,
        statement="thermal shutdown threshold",
        unit="degC",
        min_value=None,
        typ_value=165.0,
        max_value=None,
        target=None,
        source_page=9,
        excerpt="thermal shutdown occurs at 165 degC typical",
        not_testable_reason=OPEN_REASON,
    )
    return SpecSet(part=PART, subckt=SUBCKT, doc_id=DOC_ID, characteristics=(probed, uncovered))


class HarnessDouble:
    """A deterministic ``run_harness``: the verdict follows the bytes written."""

    def __init__(self, probe: str, *, pass_marker: str = "CORRECTED") -> None:
        self.probe = probe
        self.pass_marker = pass_marker
        self.calls: list[dict[str, object]] = []
        self.reports: list[HarnessReport] = []

    def __call__(
        self,
        *,
        model_lib,
        subckt: str,
        spec,
        workdir,
        ltspice,
        timeout_s: float = 120.0,
        cancel: threading.Event | None = None,
    ) -> HarnessReport:
        path = Path(model_lib)
        text = path.read_text(encoding="utf-8")
        passing = self.pass_marker in text
        self.calls.append(
            {
                "model_lib": path,
                "subckt": subckt,
                "spec": spec,
                "workdir": Path(workdir),
                "ltspice": Path(ltspice),
                "timeout_s": timeout_s,
                "text": text,
            }
        )
        if passing:
            detail = f"model {text.strip()!r} measured 4.05 V, inside 3.8 V .. 4.4 V (page 7)"
            measured: dict[str, float | str] = {"uvlo_rise_v": 4.05}
        else:
            detail = (
                f"model {text.strip()!r} measured 5.9 V against min 3.8 V / max 4.4 V "
                f"(page 7): {EXCERPT}"
            )
            measured = {"uvlo_rise_v": 5.9}
        outcome = ProbeOutcome(
            probe_id=self.probe,
            status="PASS" if passing else "FAIL",
            measured=measured,
            detail=detail,
            unknown_reason=None,
            run_dir=str(Path(workdir) / self.probe),
            char_ids=(CHAR_ID,),
        )
        report = HarnessReport(
            part=spec.part,
            model_sha256=sha256_file(path),
            spec_digest=spec.digest(),
            outcomes=(outcome,),
        )
        self.reports.append(report)
        return report


class UnavailableBackend:
    """A backend that says why it cannot run; ``author`` must never be reached."""

    name = "unavailable"
    reason = (
        "bob_shell_not_installed: install from "
        "https://bob.ibm.com/docs/shell/getting-started/install-and-setup"
    )

    def availability(self) -> tuple[bool, str]:
        return False, self.reason

    def author(self, request: object, cancel: object = None) -> object:
        raise AssertionError("an unavailable backend must not be asked to author")


def make_request(
    tmp_path: Path,
    spec: SpecSet,
    backend: object,
    *,
    max_iterations: int | None = None,
    stall_patience: int = 2,
    turn_timeout_s: float | None = None,
    workdir: Path | None = None,
) -> object:
    return loop.BuildRequest(
        part=PART,
        subckt=SUBCKT,
        spec=spec,
        workdir=workdir if workdir is not None else tmp_path / "build",
        ltspice=tmp_path / "LTspice.exe",
        backend=backend,
        max_iterations=max_iterations,
        stall_patience=stall_patience,
        turn_timeout_s=turn_timeout_s,
        timeout_s=TIMEOUT_S,
    )


def write_model(workdir: Path, text: str) -> None:
    model_dir = workdir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / f"{SUBCKT}.lib").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------- tests


def test_fail_then_pass_reaches_pass_on_turn_two(monkeypatch, tmp_path: Path) -> None:
    probe = probe_id()
    spec = build_spec(probe)
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe)
    monkeypatch.setattr(loop, "run_harness", double)

    prompts: list[str] = []
    requests: list[object] = []

    def script(turn: int, path: Path, prompt: str) -> None:
        prompts.append(prompt)
        write_model(path, f"* attempt {turn}\n" + ("CORRECTED\n" if turn >= 2 else ""))

    class Recorder(ScriptedBackend):
        def author(self, request, cancel=None):
            requests.append(request)
            return super().author(request, cancel)

    backend = Recorder(script)
    outcome = loop.build_model(make_request(tmp_path, spec, backend), None)

    assert outcome.status == "PASS"
    assert outcome.iterations == 2
    assert [entry.status for entry in outcome.report.outcomes] == ["PASS"]
    assert len(outcome.history) == 2
    assert outcome.history[0] == f"turn 1: progress; failing {probe}"
    assert outcome.history[1] == "turn 2: progress; failing none"

    assert len(double.calls) == 2
    assert double.calls[0]["model_lib"] == workdir / "model" / f"{SUBCKT}.lib"
    assert double.calls[0]["text"] == "* attempt 1\n"
    assert double.calls[1]["text"] == "* attempt 2\nCORRECTED\n"
    assert double.calls[1]["subckt"] == SUBCKT
    assert double.calls[1]["workdir"] == workdir / "harness" / "turn-2"
    assert double.calls[1]["ltspice"] == tmp_path / "LTspice.exe"
    assert double.calls[1]["timeout_s"] == TIMEOUT_S
    assert double.calls[0]["spec"].digest() == spec.digest()

    assert len(prompts) == 2
    feedback = double.reports[0].feedback()
    assert feedback
    assert feedback in prompts[1]
    assert "Harness feedback so far" in prompts[1]
    assert "Harness feedback so far" not in prompts[0]

    assert len(requests) == 2
    assert requests[0].workdir == workdir
    assert requests[0].model_dir == workdir / "model"
    assert requests[0].max_turns == loop.AUTHOR_MAX_TURNS
    assert requests[0].prompt == prompts[0]

    assert loop.spec_file(workdir).read_text(encoding="utf-8") == spec.to_json()


def test_tampering_with_the_frozen_spec_aborts_before_any_simulation(
    monkeypatch, tmp_path: Path
) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        payload = json.loads(spec.to_json())
        try:
            payload["characteristics"][0]["max_value"] = 999.0
        except KeyError, IndexError, TypeError:
            payload["part"] = "TAMPERED-PART"
        loop.spec_file(path).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        write_model(path, "* a model with a relaxed target\n")

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), None)

    assert outcome.status == "UNKNOWN"
    assert "spec_tampered" in outcome.detail
    assert outcome.iterations == 1
    assert double.calls == []
    assert double.reports == []
    assert outcome.report.outcomes == ()
    assert "spec_tampered" in outcome.history[-1]
    assert loop.spec_file(workdir).read_text(encoding="utf-8") != spec.to_json()


def test_the_iteration_cap_is_unknown_and_names_the_failing_probe(
    monkeypatch, tmp_path: Path
) -> None:
    """A capped build stops at the cap even when the agent is also not progressing.

    The caller's ``max_iterations`` is checked before the stall rule, so an
    explicit budget is always the reason that is reported for a capped run.
    """
    probe = probe_id()
    spec = build_spec(probe)
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe)
    monkeypatch.setattr(loop, "run_harness", double)
    prompts: list[str] = []

    def script(turn: int, path: Path, prompt: str) -> None:
        prompts.append(prompt)
        write_model(path, f"* attempt {turn}, still wrong\n")

    outcome = loop.build_model(
        make_request(tmp_path, spec, ScriptedBackend(script), max_iterations=3), None
    )

    assert outcome.status == "UNKNOWN"
    assert outcome.iterations == 3
    assert len(double.calls) == 3
    assert len(outcome.history) == 3
    assert outcome.history[0] == f"turn 1: progress; failing {probe}"
    assert outcome.history[1] == f"turn 2: no progress; failing {probe}"
    assert outcome.history[2] == f"turn 3: no progress; failing {probe}"
    assert loop.STALLED_PREFIX not in outcome.detail, "the caller's cap is the reason here"
    assert probe in outcome.detail
    assert "FAIL" in outcome.detail
    assert "max_iterations=3" in outcome.detail
    assert outcome.report is double.reports[0], "keep the best candidate when later turns tie"

    assert "Harness feedback so far" not in prompts[0]
    assert double.reports[0].feedback() in prompts[1]
    assert double.reports[0].feedback() in prompts[2]
    assert double.reports[1].feedback() in prompts[2]


# ---------------------------------------- progress-based stopping (no clock)

FIVE = probe_ids(5)


def turn_of(text: str) -> int:
    """The turn number a scripted agent stamped into the model, for verdict functions."""
    found = re.findall(r"turn (\d+)", text)
    return int(found[-1]) if found else 0


class RevisionHarness:
    """A ``run_harness`` double whose report is a function of the model text.

    ``verdict(text)`` is the whole outcome list as ``(probe_id, status)`` pairs,
    so a test can make the failing set shrink, change, or repeat exactly as it
    needs. Every outcome carries a char id, which is what the real
    ``HarnessReport.passed`` requires before it will call a report a pass.
    """

    def __init__(self, verdict, spec) -> None:
        self.verdict = verdict
        self.spec = spec
        self.calls: list[dict[str, object]] = []
        self.reports: list[HarnessReport] = []

    def __call__(
        self,
        *,
        model_lib,
        subckt: str,
        spec,
        workdir,
        ltspice,
        timeout_s: float = 120.0,
        cancel: threading.Event | None = None,
    ) -> HarnessReport:
        path = Path(model_lib)
        text = path.read_text(encoding="utf-8")
        outcomes = tuple(
            ProbeOutcome(
                probe_id=name,
                status=status,
                measured={"measured_v": 1.0},
                detail=f"{name} measured 1.0 V: {status}",
                unknown_reason=None,
                run_dir=str(Path(workdir) / name),
                char_ids=(CHAR_ID,),
            )
            for name, status in self.verdict(text)
        )
        self.calls.append({"model_lib": path, "subckt": subckt, "text": text, "cancel": cancel})
        report = HarnessReport(
            part=spec.part,
            model_sha256=sha256_file(path),
            spec_digest=spec.digest(),
            outcomes=outcomes,
        )
        self.reports.append(report)
        return report


def test_no_hidden_cap_an_agent_that_improves_six_times_reaches_pass(
    monkeypatch, tmp_path: Path
) -> None:
    """Six improving turns end in PASS: ``max_iterations=None`` is not a cap in disguise.

    Each turn fixes one more probe, so the failing set shrinks every turn and the
    loop keeps going well past any implicit budget until the harness passes.
    """
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)

    def verdict(text: str) -> tuple[tuple[str, str], ...]:
        fixed = turn_of(text) - 1  # turn n has fixed the first n-1 probes
        return tuple((name, "PASS" if index < fixed else "FAIL") for index, name in enumerate(FIVE))

    double = RevisionHarness(verdict, spec)
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, f"* turn {turn}\n")

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), None)

    assert outcome.status == "PASS", (outcome.detail, outcome.history)
    assert outcome.iterations == 6
    assert len(double.calls) == 6
    assert len(outcome.history) == 6
    for turn, line in enumerate(outcome.history, start=1):
        assert line.startswith(f"turn {turn}: progress; failing "), line
        assert "no progress" not in line
    assert outcome.history[-1] == "turn 6: progress; failing none"
    assert outcome.report is double.reports[-1]
    assert outcome.report.passed() is True


def test_a_repeating_agent_stops_after_stall_patience_no_progress_turns(
    monkeypatch, tmp_path: Path
) -> None:
    """Byte-identical model text every turn: two no-progress turns and the loop stops."""
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = RevisionHarness(lambda text: ((FIVE[0], "FAIL"),), spec)
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, "* the same wrong model, every single time\n")

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), None)

    assert outcome.status == "UNKNOWN"
    assert outcome.iterations == 3, outcome.history  # turn 1 moved; turns 2 and 3 did not
    assert loop.STALLED_PREFIX in outcome.detail
    assert "stopped making progress" in outcome.detail
    assert "2 consecutive turn(s)" in outcome.detail
    assert "3 turn(s)" in outcome.detail
    assert FIVE[0] in outcome.detail
    assert outcome.report is double.reports[0]
    assert outcome.report.model_sha256 == double.reports[-1].model_sha256
    assert len(double.calls) == 3
    assert outcome.history[0] == f"turn 1: progress; failing {FIVE[0]}"
    assert outcome.history[1] == f"turn 2: no progress; failing {FIVE[0]}"
    assert outcome.history[2] == f"turn 3: no progress; failing {FIVE[0]}"


def test_an_agent_that_improves_twice_then_stalls_stops_at_the_stall(
    monkeypatch, tmp_path: Path
) -> None:
    """Two improving turns buy two more attempts: the stall, not a cap, ends this build."""
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)

    def verdict(text: str) -> tuple[tuple[str, str], ...]:
        if "v2" in text:
            return ((FIVE[0], "PASS"), (FIVE[1], "FAIL"))
        return ((FIVE[0], "FAIL"), (FIVE[1], "FAIL"))

    double = RevisionHarness(verdict, spec)
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, "* v1\n" if turn == 1 else "* v2\n")

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), None)

    assert outcome.status == "UNKNOWN"
    assert outcome.iterations == 4, outcome.history  # past a 3-turn cap: no cap is set
    assert loop.STALLED_PREFIX in outcome.detail
    assert FIVE[1] in outcome.detail
    assert len(double.calls) == 4
    assert outcome.history[0] == f"turn 1: progress; failing {FIVE[0]}, {FIVE[1]}"
    assert outcome.history[1] == f"turn 2: progress; failing {FIVE[1]}"
    assert outcome.history[2] == f"turn 3: no progress; failing {FIVE[1]}"
    assert outcome.history[3] == f"turn 4: no progress; failing {FIVE[1]}"


def test_max_iterations_still_caps_an_agent_that_would_otherwise_continue(
    monkeypatch, tmp_path: Path
) -> None:
    """Alternating failures are not improvement; an explicit cap still takes precedence."""
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)

    def verdict(text: str) -> tuple[tuple[str, str], ...]:
        odd = turn_of(text) % 2 == 1
        return (
            ((FIVE[0], "FAIL"), (FIVE[1], "PASS"))
            if odd
            else ((FIVE[0], "PASS"), (FIVE[1], "FAIL"))
        )

    double = RevisionHarness(verdict, spec)
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, f"* turn {turn}\n")

    outcome = loop.build_model(
        make_request(tmp_path, spec, ScriptedBackend(script), max_iterations=3), None
    )

    assert outcome.status == "UNKNOWN"
    assert outcome.iterations == 3
    assert len(double.calls) == 3
    assert "max_iterations=3" in outcome.detail
    assert FIVE[0] in outcome.detail
    assert any("no progress" in line for line in outcome.history)
    assert outcome.report is double.reports[0]


def test_cancellation_during_a_turn_ends_unknown_cancelled(monkeypatch, tmp_path: Path) -> None:
    """The caller's cancel still wins: no harness run, no verdict, ``cancelled``."""
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = RevisionHarness(lambda text: ((FIVE[0], "FAIL"),), spec)
    monkeypatch.setattr(loop, "run_harness", double)
    cancel = threading.Event()

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, "* a model the harness never got to judge\n")
        cancel.set()

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), cancel)

    assert outcome.status == "UNKNOWN"
    assert outcome.detail.startswith("cancelled")
    assert outcome.iterations == 1
    assert double.calls == []
    assert outcome.report.outcomes == ()
    assert outcome.history[-1].startswith("turn 1:")


class BlockingBackend:
    """A backend that only finishes when its turn is cancelled — a hung agent.

    It waits for the cancel event the loop passes it (which is how a real backend
    kills a hung process tree), writes the model anyway, and reports itself as
    cancelled, so the loop must tell its own turn timeout apart from a caller
    cancellation.
    """

    name = "blocking"

    def __init__(self) -> None:
        self.calls = 0

    def availability(self) -> tuple[bool, str]:
        return True, "blocking backend"

    def author(self, request: object, cancel: threading.Event | None = None) -> AuthorResult:
        self.calls += 1
        stopped = cancel is not None and cancel.wait(timeout=30.0)
        write_model(Path(request.workdir), "* the blocking model\n")
        return AuthorResult(
            ok=not stopped,
            detail="cancelled: the blocking turn was stopped" if stopped else "blocking turn",
            usage={},
            stdout_tail="",
            session_id=None,
        )


def test_a_turn_timeout_is_reported_as_its_own_reason_and_spends_the_turn(
    monkeypatch, tmp_path: Path
) -> None:
    """``turn_timeout_s`` bounds one invocation; the timed-out turn is not retried free."""
    spec = build_spec(FIVE[0])
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = RevisionHarness(lambda text: ((FIVE[0], "FAIL"),), spec)
    monkeypatch.setattr(loop, "run_harness", double)
    backend = BlockingBackend()

    outcome = loop.build_model(
        make_request(tmp_path, spec, backend, turn_timeout_s=0.05, max_iterations=2), None
    )

    assert backend.calls == 2, "the timed-out turn was spent, not retried for free"
    assert outcome.status == "UNKNOWN"
    assert len(double.calls) == 1, "unchanged bytes after a timeout need no repeated simulation"
    assert "turn_timeout" in outcome.detail
    assert outcome.history[0].startswith(f"turn 1: progress; failing {FIVE[0]}; ")
    assert "turn_timeout" in outcome.history[0] and "0.05 s" in outcome.history[0]
    assert "cancelled" not in outcome.history[0], "the loop's own reason, not the backend's"
    assert "existing candidate preserved" in outcome.history[1]
    assert "turn_timeout" in outcome.history[1]


def test_blocked_propagates_the_availability_reason_verbatim(monkeypatch, tmp_path: Path) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)

    outcome = loop.build_model(make_request(tmp_path, spec, UnavailableBackend()), None)

    assert outcome.status == "BLOCKED"
    assert outcome.detail == UnavailableBackend.reason
    assert outcome.iterations == 0
    assert outcome.history == ()
    assert double.calls == []
    assert outcome.report.outcomes == ()
    assert outcome.report.model_sha256 == ""
    assert (workdir / "model").is_dir()


def test_a_missing_model_file_stops_the_build(monkeypatch, tmp_path: Path) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)

    backend = ScriptedBackend(lambda turn, path, prompt: None)
    outcome = loop.build_model(make_request(tmp_path, spec, backend), None)

    assert outcome.status == "UNKNOWN"
    assert "model_file_missing" in outcome.detail
    assert outcome.iterations == 1
    assert backend.turns == 1
    assert double.calls == []
    assert "model_file_missing" in outcome.history[-1]


def test_build_model_freezes_the_spec_even_without_prepare_workdir(
    monkeypatch, tmp_path: Path
) -> None:
    spec = build_spec(probe_id())
    workdir = tmp_path / "unprepared"
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        write_model(path, "* model\nCORRECTED\n")

    outcome = loop.build_model(
        make_request(tmp_path, spec, ScriptedBackend(script), workdir=workdir), None
    )

    assert outcome.status == "PASS"
    assert loop.spec_file(workdir).read_text(encoding="utf-8") == spec.to_json()
    assert double.calls[0]["spec"].digest() == spec.digest()


def test_a_pre_cancelled_build_never_calls_the_agent(monkeypatch, tmp_path: Path) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)
    cancel = threading.Event()
    cancel.set()
    backend = ScriptedBackend(lambda turn, path, prompt: write_model(path, "* model\n"))

    outcome = loop.build_model(make_request(tmp_path, spec, backend), cancel)

    assert outcome.status == "UNKNOWN"
    assert outcome.detail.startswith("cancelled")
    assert outcome.iterations == 0
    assert backend.turns == 0
    assert double.calls == []


def test_a_backend_that_raises_is_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)

    def script(turn: int, path: Path, prompt: str) -> None:
        raise RuntimeError("the agent process died")

    outcome = loop.build_model(make_request(tmp_path, spec, ScriptedBackend(script)), None)

    assert outcome.status == "UNKNOWN"
    assert "backend_error" in outcome.detail
    assert "the agent process died" in outcome.detail
    assert "model_file_missing" in outcome.detail
    assert double.calls == []


def test_prepare_workdir_freezes_the_spec_prompt_and_model_dir(tmp_path: Path) -> None:
    spec = build_spec()
    workdir = tmp_path / "build"

    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)

    assert loop.spec_file(workdir).read_text(encoding="utf-8") == spec.to_json()
    readme = workdir / "spec" / "README.md"
    assert readme.is_file()
    assert "frozen" in readme.read_text(encoding="utf-8")
    assert "spec_tampered" in readme.read_text(encoding="utf-8")
    assert (workdir / "model").is_dir()
    assert workdir.joinpath("prompt.md").read_text(encoding="utf-8") == loop.build_prompt(
        spec, SUBCKT
    )

    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir, prompt="custom brief")
    assert workdir.joinpath("prompt.md").read_text(encoding="utf-8") == "custom brief"


def test_build_prompt_states_the_whole_contract() -> None:
    spec = build_spec()

    prompt = loop.build_prompt(spec, SUBCKT)

    assert PART in prompt
    assert f".subckt {SUBCKT}" in prompt
    assert f"model/{SUBCKT}.lib" in prompt
    assert f"model/{SUBCKT}.asy" in prompt
    assert "PINATTR SpiceOrder" in prompt
    assert CHAR_ID in prompt
    assert "min 3.8 V" in prompt
    assert "max 4.4 V" in prompt
    assert "page 7" in prompt
    assert EXCERPT in prompt
    assert OPEN_CHAR_ID in prompt
    assert OPEN_REASON in prompt
    assert "spec_tampered" in prompt
    assert "model test --out" in prompt
    for port in loop.required_ports_for(spec):
        assert port in prompt


def test_build_model_accepts_the_located_install_record(monkeypatch, tmp_path: Path) -> None:
    from boardmodeler.simulation.ltspice import LtspiceInstall

    spec = build_spec(probe_id())
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = HarnessDouble(probe_id())
    monkeypatch.setattr(loop, "run_harness", double)
    install = LtspiceInstall(path=tmp_path / "LTspice.exe", source="explicit")
    request = loop.BuildRequest(
        part=PART,
        subckt=SUBCKT,
        spec=spec,
        workdir=workdir,
        ltspice=install,  # type: ignore[arg-type] - locate() returns this record
        backend=ScriptedBackend(
            lambda turn, path, prompt: write_model(path, "* model\nCORRECTED\n")
        ),
        timeout_s=TIMEOUT_S,
    )

    outcome = loop.build_model(request, None)

    assert outcome.status == "PASS"
    assert double.calls[0]["ltspice"] == tmp_path / "LTspice.exe"


def test_a_silent_harness_report_still_reaches_the_next_prompt(monkeypatch, tmp_path: Path) -> None:
    class Silent(HarnessDouble):
        """A report that cannot pass and produces no feedback text of its own.

        ``feedback()`` covers FAIL and UNKNOWN blocks; a BLOCKED outcome (a run
        that could not start at all) is exactly the case where the loop must
        supply the missing text itself.
        """

        def __call__(
            self, *, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None
        ):
            report = super().__call__(
                model_lib=model_lib,
                subckt=subckt,
                spec=spec,
                workdir=workdir,
                ltspice=ltspice,
                timeout_s=timeout_s,
                cancel=cancel,
            )
            silent = HarnessReport(
                part=report.part,
                model_sha256=report.model_sha256,
                spec_digest=report.spec_digest,
                outcomes=tuple(
                    ProbeOutcome(
                        probe_id=entry.probe_id,
                        status="BLOCKED",
                        measured={},
                        detail="",
                        unknown_reason=None,
                        run_dir=entry.run_dir,
                        char_ids=entry.char_ids,
                    )
                    for entry in report.outcomes
                ),
            )
            assert silent.feedback() == ""
            assert silent.passed() is False
            self.reports[-1] = silent
            return silent

    probe = probe_id()
    spec = build_spec(probe)
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    double = Silent(probe)
    monkeypatch.setattr(loop, "run_harness", double)
    prompts: list[str] = []

    def script(turn: int, path: Path, prompt: str) -> None:
        prompts.append(prompt)
        write_model(path, f"* attempt {turn}\n")

    outcome = loop.build_model(
        make_request(tmp_path, spec, ScriptedBackend(script), max_iterations=2), None
    )

    assert outcome.status == "UNKNOWN"
    assert len(prompts) == 2, (outcome.detail, outcome.history)
    assert "unresolved probes" in prompts[1]
    assert f"{probe}=BLOCKED" in prompts[1]


def test_required_ports_degrades_for_an_unknown_probe_binding() -> None:
    spec = SpecSet(
        part=PART,
        subckt=SUBCKT,
        doc_id=DOC_ID,
        characteristics=(characteristic(probe="not_a_registered_probe"),),
    )

    assert loop.required_ports_for(spec) == ()

    prompt = loop.build_prompt(spec, SUBCKT)
    assert "did not report a port list" in prompt
    assert "PINATTR SpiceOrder" in prompt


def test_build_outcome_json_round_trips() -> None:
    spec = build_spec()
    report = HarnessReport(
        part=PART,
        model_sha256="b" * 64,
        spec_digest=spec.digest(),
        outcomes=(
            ProbeOutcome(
                probe_id="uvlo_rise",
                status="FAIL",
                measured={"uvlo_rise_v": 5.9},
                detail="measured 5.9 V against max 4.4 V (page 7)",
                unknown_reason=None,
                run_dir="harness/uvlo_rise",
                char_ids=(CHAR_ID,),
            ),
        ),
    )
    outcome = loop.BuildOutcome(
        status="UNKNOWN",
        iterations=2,
        report=report,
        history=("turn 1: x", "turn 2: y"),
        detail="max_iterations=2 exhausted; still failing: uvlo_rise=FAIL",
    )

    text = outcome.to_json()

    payload = json.loads(text)
    assert payload["status"] == "UNKNOWN"
    assert payload["iterations"] == 2
    assert payload["history"] == ["turn 1: x", "turn 2: y"]
    assert isinstance(payload["report"], dict)

    restored = loop.BuildOutcome.from_json(text)
    assert restored.status == outcome.status
    assert restored.iterations == outcome.iterations
    assert restored.detail == outcome.detail
    assert restored.history == outcome.history
    assert restored.report.spec_digest == report.spec_digest
    assert restored.report.model_sha256 == report.model_sha256
    assert [entry.status for entry in restored.report.outcomes] == ["FAIL"]
    assert restored.report.passed() is False


def test_a_spec_with_no_covered_row_never_asks_the_agent_or_the_harness(
    monkeypatch, tmp_path: Path
) -> None:
    """Zero covered characteristics is an explicit UNKNOWN, not a wasted author turn."""
    spec = SpecSet(
        part=PART,
        subckt=SUBCKT,
        doc_id=DOC_ID,
        characteristics=(characteristic(probe=None),),
    )
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)

    def double(**kwargs: object) -> object:
        raise AssertionError("nothing is covered, so the harness must not run")

    monkeypatch.setattr(loop, "run_harness", double)
    authored: list[int] = []
    backend = ScriptedBackend(lambda turn, path, prompt: authored.append(turn))

    outcome = loop.build_model(make_request(tmp_path, spec, backend, workdir=workdir))

    assert outcome.status == "UNKNOWN"
    assert "no_covered_characteristics" in outcome.detail
    assert authored == []
    assert outcome.report.outcomes == ()


def test_a_fresh_process_revalidates_a_passing_candidate_without_authoring(
    monkeypatch, tmp_path: Path
) -> None:
    """No process receipt for an existing pass: one simulator run, zero author turns."""
    from boardmodeler.authoring import validation_cache

    probe = probe_id()
    spec = build_spec(probe)
    workdir = tmp_path / "build"
    loop.prepare_workdir(spec=spec, subckt=SUBCKT, workdir=workdir)
    (tmp_path / "LTspice.exe").write_bytes(b"")
    write_model(workdir, "CORRECTED MODEL\n")
    double = HarnessDouble(probe)
    monkeypatch.setattr(loop, "run_harness", double)
    monkeypatch.setattr(validation_cache, "_OBSERVED", {})

    authored: list[int] = []
    backend = ScriptedBackend(lambda turn, path, prompt: authored.append(turn))

    outcome = loop.build_model(make_request(tmp_path, spec, backend, workdir=workdir))

    assert outcome.status == "PASS", outcome.detail
    assert outcome.iterations == 0
    assert authored == []
    assert len(double.calls) == 1


def test_loop_request_bounds_are_validated_and_no_cap_is_the_default(tmp_path: Path) -> None:
    import pytest

    spec = build_spec()
    backend = UnavailableBackend()

    assert make_request(tmp_path, spec, backend).max_iterations is None
    assert make_request(tmp_path, spec, backend, max_iterations=None).stall_patience == 2

    with pytest.raises(ValueError, match="max_iterations"):
        make_request(tmp_path, spec, backend, max_iterations=0)
    with pytest.raises(ValueError, match="max_iterations"):
        make_request(tmp_path, spec, backend, max_iterations=-2)
    with pytest.raises(ValueError, match="stall_patience"):
        make_request(tmp_path, spec, backend, stall_patience=0)
    with pytest.raises(ValueError, match="turn_timeout_s"):
        make_request(tmp_path, spec, backend, turn_timeout_s=0.0)
