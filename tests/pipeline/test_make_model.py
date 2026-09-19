"""make_model: the model maker's single entry point, proven offline and on LTspice.

Every test here is deterministic. The offline scenarios supply the committed
TPS54320 extraction result (``requirements.json`` plus the reviewed
``probes.json``) and a :class:`ScriptedBackend`; the authoring scenarios run real
LTspice, because what is under test is exactly "an agent writes, the simulator
judges". No test reaches the network.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from boardmodeler.authoring.backends import (
    BOB_CREDENTIALS_UNAVAILABLE,
    BOB_NOT_INSTALLED,
    ScriptedBackend,
)
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.domain.enums import ProviderKind
from boardmodeler.domain.records import ProviderIdentity, Requirement
from boardmodeler.models.library import subckt_ports
from boardmodeler.models.regulator import write_regulator_library
from boardmodeler.models.symbolism import symbol_pin_orders, symbol_text, validate_symbol
from boardmodeler.pipeline import make_model as engine
from boardmodeler.pipeline.make_model import MakeModelRequest, bind_requirements, make_model
from boardmodeler.providers.base import (
    MAX_SNIPPET_CHARS,
    ExtractionRequest,
    ExtractionResponse,
    ExtractionTask,
    ProviderCapabilities,
    ProviderHealth,
)
from boardmodeler.providers.registry import ProviderSelection
from boardmodeler.security.credentials import Credential, SecretSource
from boardmodeler.simulation.ltspice import LtspiceInstall

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "regulator" / "tps54320"
REQUIREMENTS = FIXTURE / "requirements.json"
BINDINGS = FIXTURE / "probes.json"
DATASHEET = FIXTURE / "originals" / "tps54320_datasheet.pdf"

PART = "TPS54320"
SUBCKT = "BM_REG_BUCK"
BOUND_ID = "REQ_TPS54320_ELEC_004"  # UVLO rising, 4.0 .. 4.5 V
UNBOUND_ID = "REQ_TPS54320_ELEC_001"  # power-stage input range: no probe
VREF_ID = "REQ_TPS54320_ELEC_020"  # 0.792 .. 0.808 V
PG_ID = "REQ_TPS54320_PG_052"

#: The bound rows of the reviewed fixture: nine rows judged by eight probes.
BOUND_IDS = (
    "REQ_TPS54320_ELEC_003",
    "REQ_TPS54320_ELEC_004",
    "REQ_TPS54320_ELEC_006",
    "REQ_TPS54320_ELEC_007",
    "REQ_TPS54320_ELEC_010",
    "REQ_TPS54320_ELEC_011",
    "REQ_TPS54320_ELEC_020",
    "REQ_TPS54320_PG_052",
    "REQ_TPS54320_TEMPORAL_062",
)


# --------------------------------------------------------------------------- #
# helpers


def datasheet_for(tmp_path: Path) -> Path:
    """The real datasheet when the git-ignored original is present, else a stand-in.

    The extraction result under test is the committed ``requirements.json``; its
    citations are re-verified against the real PDF when it is there. When the
    original is absent the run must still work, so a minimal PDF stands in and
    the supplied extraction result is taken at its own (all-verified) word.
    """
    if DATASHEET.is_file():
        return DATASHEET
    from reportlab.pdfgen import canvas

    path = tmp_path / "stand_in_datasheet.pdf"
    sheet = canvas.Canvas(str(path))
    sheet.drawString(72, 720, "Stand-in datasheet: the real original is not checked out.")
    sheet.showPage()
    sheet.save()
    return path


def fixture_requirements() -> list[Requirement]:
    raw = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    return [Requirement.model_validate(item) for item in raw["requirements"]]


def reviewed_probes() -> dict[str, str | None]:
    raw = json.loads(BINDINGS.read_text(encoding="utf-8"))
    return {entry["req_id"]: entry.get("probe") for entry in raw["bindings"]}


def buck_library(path: Path, *, vref: str | None = None, drop_ports: tuple[str, ...] = ()) -> Path:
    """The bundled BM_REG_BUCK template, optionally perturbed."""
    write_regulator_library(path, [SUBCKT])
    text = path.read_text(encoding="utf-8")
    if vref is not None:
        assert text.count("VREF=0.8") == 1, "the perturbation must be specific"
        text = text.replace("VREF=0.8", f"VREF={vref}")
    if drop_ports:
        lines = text.splitlines()
        header = next(line for line in lines if line.startswith(f".subckt {SUBCKT}"))
        without = " ".join(token for token in header.split() if token not in drop_ports)
        text = text.replace(header, without)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def template_script(*, vref: str | None = None, drop_ports: tuple[str, ...] = ()):
    """A scripted agent that writes the bundled template and a permuted symbol."""

    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        lib = buck_library(model_dir / f"{SUBCKT}.lib", vref=vref, drop_ports=drop_ports)
        ports = list(subckt_ports(lib.read_text(encoding="utf-8"), SUBCKT))
        # A plausible but permuted symbol: the SpiceOrder values are still a
        # bijection, so only the pipeline's port-order check catches it.
        text = symbol_text(SUBCKT, list(reversed(ports)), model_file=f"{SUBCKT}.lib")
        model_dir.joinpath(f"{SUBCKT}.asy").write_text(text, encoding="utf-8")

    return script


def fail_then_pass_script():
    """Turn 1 writes a wrong reference; turn 2 writes the stock library."""

    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        buck_library(model_dir / f"{SUBCKT}.lib", vref="0.5" if turn == 1 else None)
        if turn == 2:
            assert "vref" in prompt, "the harness feedback must reach the next turn"

    return script


def use_backend(monkeypatch: pytest.MonkeyPatch, backend: object) -> object:
    """Install a test backend at the module boundary the pipeline calls."""
    monkeypatch.setattr(engine, "build_backend", lambda request: backend)
    return backend


def fake_ltspice(tmp_path: Path) -> LtspiceInstall:
    return LtspiceInstall(tmp_path / "ltspice.exe", "test stand-in")


def make_request(tmp_path: Path, **overrides: object) -> MakeModelRequest:
    values: dict[str, object] = {
        "part": PART,
        "subckt": SUBCKT,
        "datasheet": datasheet_for(tmp_path),
        "out_dir": tmp_path / "out",
        "requirements_json": REQUIREMENTS,
        "bindings_json": BINDINGS,
    }
    values.update(overrides)
    return MakeModelRequest(**values)  # type: ignore[arg-type]


def run(
    tmp_path: Path,
    *,
    cancel: threading.Event | None = None,
    **overrides: object,
):
    """Run one model build, collecting the progress events and the wall time."""
    events: list[object] = []
    request = make_request(tmp_path, **overrides)
    started = time.monotonic()
    result = make_model(request, progress=events.append, cancel=cancel)
    return result, events, time.monotonic() - started


def judge_events(events: list[object]) -> list[object]:
    return [event for event in events if event.stage == "judge" and "turn" in event.counts]


def last_stage(result, stage: str):
    """The last event a stage emitted (a stage emits running then its outcome)."""
    return [event for event in result.stages if event.stage == stage][-1]


# --------------------------------------------------------------------------- #
# (a) the whole path: fixtures in, PASS out, every deliverable published


@pytest.mark.ltspice
def test_scenario_a_scripted_template_passes_and_publishes_the_deliverables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ltspice_exe: Path
) -> None:
    use_backend(monkeypatch, ScriptedBackend(template_script()))
    result, _events, wall_s = run(tmp_path)
    print(f"\nscenario (a) wall time: {wall_s:.1f} s for {len(result.rows)} rows")

    assert result.status == "PASS", result.detail
    assert not result.counts or result.counts["FAIL"] == 0

    # One row per datasheet row, in the fixture's order, with the reviewed split.
    fixture_ids = [requirement.req_id for requirement in fixture_requirements()]
    assert [row.req_id for row in result.rows] == fixture_ids
    by_id = {row.req_id: row for row in result.rows}
    assert {row.req_id for row in result.rows if row.status == "PASS"} == set(BOUND_IDS)
    unbound = [row for row in result.rows if row.status == "NOT_APPLICABLE"]
    assert len(unbound) == 29
    assert all(row.required.strip() for row in unbound), "every gap needs its reason"
    assert by_id[UNBOUND_ID].required.startswith("operating-range")
    assert by_id[UNBOUND_ID].measured == "-"
    assert by_id[BOUND_ID].required == "min 4 V / max 4.5 V"
    assert by_id[BOUND_ID].measured.startswith("vin_at_start =")
    assert all(by_id[req_id].measured != "-" for req_id in BOUND_IDS)

    # The six deliverable kinds the contract names.
    names = {path.name for path in result.out_dir.iterdir()}
    assert {f"{SUBCKT}.lib", f"{SUBCKT}.asy", "MODEL_CARD.md"} <= names
    # 'example.cir' is written by the card module; the contract names EXAMPLE.cir,
    # so the same deck is published under that name too (one file on Windows).
    assert (result.out_dir / "EXAMPLE.cir").is_file()
    assert (result.out_dir / "example.cir").is_file()
    assert (result.out_dir / "harness-report.json").is_file()
    assert (result.out_dir / "results.json").is_file()
    assert (result.out_dir / "spec" / "characteristics.json").is_file()
    assert (result.out_dir / "spec" / "bindings.json").is_file()
    assert (result.out_dir / "spec" / "requirements.json").is_file()
    assert result.lib_path is not None and result.lib_path.is_file()
    assert result.asy_path is not None and result.asy_path.is_file()
    assert result.card_path is not None and result.card_path.is_file()

    # The agent's permuted symbol was rejected and regenerated as a real bijection.
    lib_text = result.lib_path.read_text(encoding="utf-8")
    ports = list(subckt_ports(lib_text, SUBCKT))
    asy_text = result.asy_path.read_text(encoding="utf-8")
    assert validate_symbol(asy_text, ports=ports, model_file=f"{SUBCKT}.lib") == []
    assert sorted(symbol_pin_orders(asy_text), key=lambda pair: pair[1]) == [
        (port, index + 1) for index, port in enumerate(ports)
    ]
    assert "regenerated" in " ".join(event.detail for event in result.stages)

    # The frozen spec the agent was judged against is the one published.
    spec = load_tps54320_spec(REQUIREMENTS, BINDINGS, part=PART, subckt=SUBCKT)
    frozen = json.loads(
        (result.out_dir / "spec" / "characteristics.json").read_text(encoding="utf-8")
    )
    assert frozen == json.loads(spec.to_json())

    results = json.loads((result.out_dir / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == "PASS"
    assert [row["req_id"] for row in results["rows"]] == fixture_ids


# --------------------------------------------------------------------------- #
# (b) row-level propagation: one measured failure, one unjudgeable row


@pytest.mark.ltspice
def test_scenario_b_one_failing_probe_is_unknown_and_keeps_every_row_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ltspice_exe: Path
) -> None:
    """A wrong reference fails its row; a missing PG port leaves that row UNKNOWN.

    The overall status stays UNKNOWN — the harness could not judge every bound
    row, so the model is not declared wrong — while every row keeps its own
    measured status.
    """
    use_backend(monkeypatch, ScriptedBackend(template_script(vref="0.5", drop_ports=("PG",))))
    result, _events, _wall = run(tmp_path, max_iterations=1)

    assert result.status == "UNKNOWN", result.detail
    by_status = Counter(row.status for row in result.rows)
    assert by_status["FAIL"] == 1, result.rows
    failed = next(row for row in result.rows if row.status == "FAIL")
    assert failed.req_id == VREF_ID
    assert failed.measured.startswith("v_fb =")
    assert failed.required == "min 0.792 V / max 0.808 V"
    pg_row = next(row for row in result.rows if row.req_id == PG_ID)
    assert pg_row.status == "UNKNOWN"
    assert "port_missing" in pg_row.measured
    assert {row.req_id for row in result.rows if row.status == "PASS"} == set(BOUND_IDS) - {
        VREF_ID,
        PG_ID,
    }
    assert by_status["NOT_APPLICABLE"] == 29
    assert result.counts["FAIL"] == 1 and result.counts["UNKNOWN"] == 1


# --------------------------------------------------------------------------- #
# (c) no LTspice: BLOCKED with a reason that names LTspice


def test_scenario_c_missing_ltspice_is_blocked_and_never_runs_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = use_backend(monkeypatch, ScriptedBackend(template_script()))
    monkeypatch.setattr(engine, "locate", lambda explicit=None: None)

    result, events, _wall = run(tmp_path)

    assert result.status == "BLOCKED"
    assert "ltspice" in result.detail.lower() and "LTSPICE_EXE" in result.detail
    assert backend.turns == 0, "the agent must not run when the judge cannot"
    author = [event for event in result.stages if event.stage == "author"]
    assert author[-1].status == "failed"
    assert "ltspice" in author[-1].detail.lower()
    assert result.lib_path is None and result.card_path is None
    assert judge_events(events) == []
    # The rows are still listed: bound rows UNKNOWN, unbound rows with reasons.
    assert len(result.rows) == 38
    assert sum(row.status == "UNKNOWN" for row in result.rows) == 9
    assert sum(row.status == "NOT_APPLICABLE" for row in result.rows) == 29
    assert sum(result.counts.values()) == 0


# --------------------------------------------------------------------------- #
# (d) Bob without a key: BLOCKED with Bob's own reason, verbatim, no fallback


def _hide_bob(monkeypatch: pytest.MonkeyPatch) -> None:
    from boardmodeler.authoring import backends

    monkeypatch.setattr(
        backends.shutil, "which", lambda name: None if name == "bob" else shutil.which(name)
    )
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOARDMODELER_BOB_SHELL_API_KEY", raising=False)


def test_scenario_d_bob_shell_not_installed_is_blocked_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hide_bob(monkeypatch)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    result, events, _wall = run(tmp_path, backend_name="bob")

    assert result.status == "BLOCKED"
    assert BOB_NOT_INSTALLED in result.detail
    author = [event for event in result.stages if event.stage == "author"]
    assert author[-1].detail == BOB_NOT_INSTALLED
    # No silent substitution: nothing was authored and no harness turn ran.
    assert judge_events(events) == []
    assert result.lib_path is None


def test_scenario_d_bob_without_a_credential_is_blocked_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.authoring import backends

    monkeypatch.setattr(backends.shutil, "which", lambda name: "bob.exe" if name == "bob" else None)
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOARDMODELER_BOB_SHELL_API_KEY", raising=False)
    monkeypatch.setattr(
        backends,
        "get_credential",
        lambda name: Credential(
            name=name, value=None, source=SecretSource.MISSING, detail="no credential stored"
        ),
    )
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    result, _events, _wall = run(tmp_path, backend_name="bob")

    assert result.status == "BLOCKED"
    assert BOB_CREDENTIALS_UNAVAILABLE in result.detail


def test_an_unknown_backend_name_is_blocked_rather_than_substituted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))
    result, _events, _wall = run(tmp_path, backend_name="magic")
    assert result.status == "BLOCKED"
    assert "magic_backend_unavailable" in result.detail


@pytest.mark.ltspice
def test_the_scripted_backend_authors_the_bundled_template_without_injection(
    tmp_path: Path, ltspice_exe: Path
) -> None:
    """``backend_name="scripted"`` is the offline author: bundled template, no key.

    Nothing is injected here: this is the path the GUI's integration run and any
    user without an agent key take, judged by the same real LTspice harness.
    """
    result, events, _wall = run(tmp_path, backend_name="scripted")

    assert result.status == "PASS", result.detail
    assert result.lib_path is not None and result.lib_path.is_file()
    assert result.asy_path is not None and result.asy_path.is_file()
    assert [event.counts["turn"] for event in judge_events(events)] == [1]
    assert last_stage(result, "author").status == "ok"


@pytest.mark.ltspice
def test_the_scripted_backend_writes_nothing_for_a_subcircuit_it_cannot_author(
    tmp_path: Path, ltspice_exe: Path
) -> None:
    """No bundled template declares this subcircuit, so no model is invented."""
    result, _events, _wall = run(tmp_path, backend_name="scripted", subckt="BM_NOT_A_TEMPLATE")

    assert result.status == "UNKNOWN"
    assert result.lib_path is None
    assert "model_file_missing" in result.detail
    assert len(result.rows) == 38


# --------------------------------------------------------------------------- #
# (e) cancellation before the first turn


def test_scenario_e_cancellation_before_the_first_turn_is_unknown_with_the_stage_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = use_backend(monkeypatch, ScriptedBackend(template_script()))
    cancel = threading.Event()
    cancel.set()

    result, events, _wall = run(tmp_path, cancel=cancel)

    assert result.status == "UNKNOWN"
    assert "cancelled" in result.detail
    assert backend.turns == 0
    stages = [(event.stage, event.status) for event in result.stages]
    assert ("read", "ok") in stages and ("bind", "ok") in stages
    assert ("author", "failed") in stages
    author = next(
        event for event in result.stages if event.stage == "author" and event.status == "failed"
    )
    assert "cancelled" in author.detail
    judge = [event for event in result.stages if event.stage == "judge"]
    assert judge and judge[-1].status == "skipped"
    assert judge_events(events) == []
    assert ("save", "skipped") in stages
    assert sum(row.status == "UNKNOWN" for row in result.rows) == 9
    assert sum(row.status == "NOT_APPLICABLE" for row in result.rows) == 29


# --------------------------------------------------------------------------- #
# (f) the binder is deterministic and never stretches a row onto a probe


def test_scenario_f_binder_is_byte_stable_and_reproduces_the_reviewed_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements = fixture_requirements()
    first = bind_requirements(requirements)
    assert first == bind_requirements(requirements)
    assert len(first) == len(requirements)
    assert all(entry["not_testable_reason"].strip() for entry in first if entry["probe"] is None), (
        "every unbound row must explain itself"
    )

    # The reviewed fixture is the oracle for the keyword table: same nine probes.
    expected = reviewed_probes()
    assert {entry["req_id"]: entry["probe"] for entry in first} == expected

    # Two make_model runs writing bindings.json produce byte-identical files.
    monkeypatch.setattr(engine, "locate", lambda explicit=None: None)
    first_dir, second_dir = tmp_path / "run1", tmp_path / "run2"
    make_model(make_request(tmp_path, out_dir=first_dir, bindings_json=None))
    make_model(make_request(tmp_path, out_dir=second_dir, bindings_json=None))
    first_text = (first_dir / "spec" / "bindings.json").read_bytes()
    assert first_text == (second_dir / "spec" / "bindings.json").read_bytes()
    assert {
        entry["req_id"]: entry["probe"] for entry in json.loads(first_text)["bindings"]
    } == expected


def test_a_row_no_probe_can_answer_gets_a_concrete_reason() -> None:
    requirements = {requirement.req_id: requirement for requirement in fixture_requirements()}
    entries = {entry["req_id"]: entry for entry in bind_requirements(fixture_requirements())}

    hysteresis = entries["REQ_TPS54320_ELEC_005"]
    assert hysteresis["probe"] is None
    assert "hysteresis" in hysteresis["not_testable_reason"]

    thermal = entries["REQ_TPS54320_THERM_034"]
    assert thermal["probe"] is None
    assert "thermal" in thermal["not_testable_reason"]

    oscillator = entries["REQ_TPS54320_ELEC_064"]
    assert oscillator["probe"] is None
    assert "oscillator" in oscillator["not_testable_reason"]

    package = entries["REQ_TPS54320_CONN_066"]
    assert package["probe"] is None
    assert package["not_testable_reason"].strip()

    # A row whose unit the probe cannot judge is not bound either, even though
    # its wording matches a probe rule: judging volts against amps is a stretch.
    reference = requirements[VREF_ID]
    mismatched = reference.model_copy(
        update={"limits": reference.limits.model_copy(update={"unit": "A"})}
    )
    entry = bind_requirements([mismatched])[0]
    assert entry["probe"] is None
    assert "declares" in entry["not_testable_reason"]

    # A statement whose words match no rule at all still says why.
    foreign = reference.model_copy(
        update={"statement": "The startup delay depends on the external timing capacitor."}
    )
    entry = bind_requirements([foreign])[0]
    assert entry["probe"] is None
    assert "no deterministic probe" in entry["not_testable_reason"]


# --------------------------------------------------------------------------- #
# (g) one judge event per turn, in order, matching the final report


@pytest.mark.ltspice
def test_scenario_g_every_turn_reports_its_own_counts_and_the_last_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ltspice_exe: Path
) -> None:
    use_backend(monkeypatch, ScriptedBackend(fail_then_pass_script()))
    result, events, _wall = run(tmp_path, max_iterations=2)

    turns = judge_events(events)
    assert [event.counts["turn"] for event in turns] == [1, 2]
    assert turns[0].counts["FAIL"] >= 1 and "failing: vref" in turns[0].detail
    assert turns[1].counts["FAIL"] == 0
    assert turns[1].counts == {**result.counts, "turn": 2}
    assert result.status == "PASS", result.detail


# --------------------------------------------------------------------------- #
# extraction: provider-driven, cached by content hash, zero requests on replay


EXCERPT = "VIN internal UVLO threshold VIN rising 4.0 4.5 V"


def _synthesize_datasheet(path: Path) -> Path:
    from reportlab.pdfgen import canvas

    sheet = canvas.Canvas(str(path))
    for text in ("Synthetic stand-in sheet (test fixture).", EXCERPT):
        sheet.drawString(72, 720, text)
        sheet.showPage()
    sheet.save()
    return path


class CountingProvider:
    """An offline provider double that counts how often it is asked."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(provider=self.name, kind=ProviderKind.FIXTURE, usage_units="tokens")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            structured_output=True,
            max_snippet_chars_pages=MAX_SNIPPET_CHARS,
            streaming=False,
            usage_units="tokens",
        )

    def health(self, timeout_s: float) -> ProviderHealth:
        return ProviderHealth(ok=True, code="ok", detail="in-process double")

    def extract(self, request: ExtractionRequest, cancel: object = None) -> ExtractionResponse:
        self.calls += 1
        snippet = next(item for item in request.snippets if EXCERPT in item.text)
        requirement = {
            "req_id": "REQ_SYNTH_UVLO_001",
            "applies_to": "SYNTH",
            "kind": "ELECTRICAL",
            "class": "DOCUMENTED_LIMIT",
            "criticality": "CRITICAL",
            "origin": "DOCUMENT",
            "statement": "The VIN UVLO rising threshold is between 4.0 V and 4.5 V.",
            "limits": {"min": 4.0, "max": 4.5, "unit": "V"},
            "evidence": [
                {
                    "doc_id": snippet.doc_id,
                    "page": {"pdf_page": snippet.pdf_page},
                    "excerpt": EXCERPT,
                    "extraction": "embedded_text",
                }
            ],
        }
        payloads = {
            ExtractionTask.IDENTITY: {"part": None},
            ExtractionTask.PINMAP: {"part_id": "SYNTH", "pins": []},
            ExtractionTask.REQUIREMENTS: {"requirements": [requirement]},
            ExtractionTask.CAPABILITY_SUMMARY: {"behaviors": {}},
        }
        return ExtractionResponse(
            payload=payloads[request.task],
            identity=self.identity(),
            raw_text=json.dumps(payloads[request.task]),
            from_cache=False,
            request_hash="double",
        )


def test_extraction_is_cached_by_content_hash_so_a_second_run_makes_zero_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.config import AppConfig

    provider = CountingProvider()
    monkeypatch.setattr(engine, "load_config", lambda path=None: AppConfig())
    monkeypatch.setattr(
        engine,
        "select_provider",
        lambda config, **kwargs: ProviderSelection(
            provider=provider, kind=ProviderKind.FIXTURE, detail="in-process double"
        ),
    )
    cancel = threading.Event()
    cancel.set()
    out_dir = tmp_path / "out"
    request = MakeModelRequest(
        part="SYNTH",
        subckt=SUBCKT,
        datasheet=_synthesize_datasheet(tmp_path / "synth.pdf"),
        out_dir=out_dir,
        backend_name="scripted",
        requirements_json=None,
        bindings_json=None,
    )

    first = make_model(request, cancel=cancel)
    assert provider.calls == 4, "one request per extraction task"
    extract = last_stage(first, "extract")
    assert extract.status == "ok" and extract.counts["rows"] == 1
    assert len(sorted((out_dir / "cache").glob("*.json"))) == 4, (
        "every response is content-addressed on disk"
    )

    second = make_model(request, cancel=cancel)
    assert provider.calls == 4, "the second run must make zero provider calls"
    assert last_stage(second, "extract").counts["cache_hits"] == 4

    # The single row was bound to the UVLO probe, so the extraction result drives
    # the binding rather than being dropped.
    bindings = json.loads((out_dir / "spec" / "bindings.json").read_text(encoding="utf-8"))
    assert bindings["bindings"] == [
        {"probe": "uvlo_rise", "params": {}, "req_id": "REQ_SYNTH_UVLO_001"}
    ]


# --------------------------------------------------------------------------- #
# stopping on progress: no cap, no deadline, one turn bound if the caller sets one


def _canned_report(spec, outcome_char: str, *, status: str = "FAIL"):
    """One harness report with a single judged probe, for the stopping-rule tests."""
    from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome

    probe = spec.by_id(outcome_char).probe
    outcome = ProbeOutcome(
        probe_id=probe,
        status=status,
        measured={"v_fb": 0.5},
        detail=f"{outcome_char} [{spec.by_id(outcome_char).req_class}]: measured v_fb=0.5 V",
        unknown_reason=None,
        run_dir="canned",
        char_ids=(outcome_char,),
        judged="v_fb = 0.5 V",
        citations=(),
        cause="measured 0.5 V is below the 0.792 V minimum",
    )
    return HarnessReport(
        part=spec.part,
        model_sha256="a" * 64,
        spec_digest=spec.digest(),
        outcomes=(outcome,),
    )


def test_a_stalled_agent_stops_the_build_and_keeps_the_measured_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent that repeats itself ends the build: no cap, no deadline, no clock.

    The scripted agent writes the same bytes every turn and the canned harness
    reports the same failure, so turn 1 is progress and turns 2-3 are not. The
    build must stop as UNKNOWN naming the stall and the probe, keep the measured
    row, and never claim a verdict the simulator did not produce. No LTspice is
    involved: the harness is the same canned-report seam the author-loop tests
    use, so this pins the stopping rule, not the simulator.
    """
    from boardmodeler.authoring import loop as loop_module

    reports = []

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        del model_lib, workdir, ltspice, timeout_s, cancel
        report = _canned_report(spec, VREF_ID)
        reports.append(report)
        return report

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        buck_library(model_dir / f"{SUBCKT}.lib")  # the same wrong model, every turn

    backend = use_backend(monkeypatch, ScriptedBackend(script))
    result, events, _wall = run(tmp_path)  # no max_iterations: there is no cap

    assert backend.turns == 3, "one turn that moved, then two that changed nothing"
    assert result.status == "UNKNOWN"
    assert "stopped making progress" in result.detail
    assert "3 turn(s)" in result.detail and "vref" in result.detail
    assert len(reports) == 3
    assert [event.counts["turn"] for event in judge_events(events)] == [1, 2, 3]
    failed = next(row for row in result.rows if row.req_id == VREF_ID)
    assert failed.status == "FAIL" and failed.measured == "v_fb = 0.5 V"
    assert result.counts["FAIL"] == 1


def test_a_capped_run_with_every_row_measured_wrong_is_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap is checked before the stall, so a capped all-FAIL run keeps its FAIL.

    The canned harness fails the reference row every turn and reports no UNKNOWN,
    so the cap is reached with every bound row fully judged — the one case the
    status ladder calls ``FAIL`` rather than ``UNKNOWN``.
    """
    from boardmodeler.authoring import loop as loop_module

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        del model_lib, workdir, ltspice, timeout_s, cancel
        return _canned_report(spec, VREF_ID)

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        buck_library(model_dir / f"{SUBCKT}.lib")  # unchanging bytes, and no progress

    use_backend(monkeypatch, ScriptedBackend(script))
    result, _events, _wall = run(tmp_path, max_iterations=2)

    assert result.status == "FAIL", result.detail
    assert "outside the datasheet limits" in result.detail and "vref" in result.detail
    assert result.counts["FAIL"] == 1 and result.counts["UNKNOWN"] == 0
    failed = next(row for row in result.rows if row.req_id == VREF_ID)
    assert failed.status == "FAIL" and failed.measured == "v_fb = 0.5 V"


def test_the_bob_backend_receives_the_turn_timeout(tmp_path: Path) -> None:
    from boardmodeler.authoring.backends import BobShellBackend

    request = make_request(tmp_path, backend_name="bob", turn_timeout_s=42.5, team_id="team-9")
    backend = engine.build_backend(request)
    assert isinstance(backend, BobShellBackend)
    assert backend.timeout_s == 42.5
    assert backend.team_id == "team-9"

    # The default is no per-turn limit at all: the agent runs until it is done.
    unlimited = engine.build_backend(make_request(tmp_path, backend_name="bob"))
    assert isinstance(unlimited, BobShellBackend)
    assert unlimited.timeout_s is None


def test_the_default_agent_backend_still_honours_a_bob_team_id(tmp_path: Path) -> None:
    """``--backend api`` (an accepted alias) must not silently drop ``--team-id`` for Bob."""
    from boardmodeler.authoring.backends import BobShellBackend

    backend = engine.build_backend(
        make_request(tmp_path, backend_name="bob", provider="bob", team_id="team-api")
    )

    assert isinstance(backend, BobShellBackend)
    assert backend.team_id == "team-api"


def test_the_agent_backend_is_built_from_the_catalog_entry(tmp_path: Path) -> None:
    """The provider the catalog holds is the backend this build builds: the Bob CLI."""
    from boardmodeler.agent_providers import default_provider
    from boardmodeler.authoring.backends import BobShellBackend

    entry = default_provider()
    request = make_request(tmp_path, backend_name="bob", provider=entry.id, turn_timeout_s=42.0)

    backend = engine.build_backend(request)

    assert isinstance(backend, BobShellBackend)
    assert backend.name == "bob_shell" and backend.timeout_s == 42.0


def test_an_unknown_agent_provider_is_blocked_rather_than_substituted(tmp_path: Path) -> None:
    from boardmodeler.agent_providers import ids

    backend = engine.build_backend(make_request(tmp_path, backend_name="bob", provider="magic"))

    usable, reason = backend.availability()
    assert usable is False
    assert reason.startswith("api_provider_unavailable:") and "'magic'" in reason
    assert all(f"'{name}'" in reason for name in ids())


def test_the_reinforcement_stage_runs_on_the_backend_the_author_loop_uses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One agent for both stages: the search may not pick a provider of its own."""
    from boardmodeler.authoring.reinforce import ReinforcementReport

    seen: list[object] = []

    def spy(**kwargs: object) -> ReinforcementReport:
        seen.append(kwargs.get("backend"))
        return ReinforcementReport(
            part=PART,
            enabled=True,
            status="skipped",
            detail="spy",
            sources=(),
            caveats=(),
            suggested_probes=(),
            spec_digest="",
        )

    backend = use_backend(monkeypatch, ScriptedBackend(lambda turn, workdir, prompt: None))
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))
    monkeypatch.setattr(engine, "reinforce", spy)

    result, _events, _wall = run(tmp_path, reinforce=True)

    assert seen == [backend], "the author loop's backend must be the one reinforced with"
    assert result.status == "UNKNOWN"


def test_the_saved_result_round_trips_including_the_turn_bounds(tmp_path: Path) -> None:
    from boardmodeler.pipeline.make_model import MakeModelResult

    def saved(request):
        return MakeModelResult(
            status="UNKNOWN",
            detail="d",
            part=PART,
            out_dir=tmp_path,
            card_path=None,
            lib_path=None,
            asy_path=None,
            rows=(),
            counts={"PASS": 0},
            stages=(),
            request=request,
        )

    request = make_request(tmp_path, turn_timeout_s=42.5, max_iterations=5, stall_patience=3)
    result = saved(request)
    restored = MakeModelResult.from_json(result.to_json())
    assert restored == result
    assert restored.request is not None
    assert restored.request.turn_timeout_s == 42.5
    assert restored.request.max_iterations == 5
    assert restored.request.stall_patience == 3

    uncapped = MakeModelResult.from_json(saved(make_request(tmp_path)).to_json())
    assert uncapped.request is not None
    assert uncapped.request.max_iterations is None
    assert uncapped.request.turn_timeout_s is None
    assert uncapped.request.stall_patience == 2


# --------------------------------------------------------------------------- #
# expected failures never raise


def test_a_missing_datasheet_is_blocked_not_raised(tmp_path: Path) -> None:
    result = make_model(make_request(tmp_path, datasheet=tmp_path / "absent.pdf"))
    assert result.status == "BLOCKED"
    assert "datasheet_missing" in result.detail
    assert result.rows == () and result.lib_path is None


def test_tampering_with_the_frozen_spec_is_unknown_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def script(turn: int, workdir: Path, prompt: str) -> None:
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        buck_library(model_dir / f"{SUBCKT}.lib")
        frozen = workdir / "spec" / "characteristics.json"
        text = frozen.read_text(encoding="utf-8")
        assert '"min_value": 4.0' in text
        frozen.write_text(text.replace('"min_value": 4.0', '"min_value": 3.0', 1), encoding="utf-8")

    use_backend(monkeypatch, ScriptedBackend(script))
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))
    result, _events, _wall = run(tmp_path, max_iterations=1)
    assert result.status == "UNKNOWN"
    assert "spec_tampered" in result.detail


def test_an_unsanitized_subcircuit_name_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SPICE identifier"):
        make_model(make_request(tmp_path, subckt="2N7000-BUCK"))
    with pytest.raises(ValueError, match="max_iterations"):
        make_model(make_request(tmp_path, max_iterations=0))
    with pytest.raises(ValueError, match="stall_patience"):
        make_model(make_request(tmp_path, stall_patience=0))
    with pytest.raises(ValueError, match="timeout_s"):
        make_model(make_request(tmp_path, timeout_s=0.0))
    with pytest.raises(ValueError, match="turn_timeout_s"):
        make_model(make_request(tmp_path, turn_timeout_s=0.0))


def test_an_invalid_token_budget_is_rejected_before_any_stage(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from boardmodeler.config import AppConfig

    with pytest.raises(ValidationError):
        AppConfig(agent_max_tokens=0)
    with pytest.raises(ValueError, match="agent_max_tokens"):
        make_model(make_request(tmp_path, agent_max_tokens=0))
