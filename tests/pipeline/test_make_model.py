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
from boardmodeler.domain.records import Condition, ProviderIdentity, Requirement
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
    import textwrap

    raw = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
    for page in range(raw["document"]["page_count"]):
        sheet.drawString(30, 810, "TEST_FIXTURE: synthetic evidence for pipeline contract tests")
        y = 790
        for requirement in raw["requirements"]:
            for evidence in requirement["evidence"]:
                if evidence["page"]["pdf_page"] == page:
                    for line in textwrap.wrap(evidence["excerpt"], width=100):
                        sheet.drawString(30, y, line)
                        y -= 10
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
    requirements = REQUIREMENTS
    if not DATASHEET.is_file():
        raw = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
        for row in raw["requirements"]:
            row["origin"] = "TEST_FIXTURE"
            for evidence in row["evidence"]:
                evidence["extraction"] = "synthetic_fixture"
        requirements = tmp_path / "synthetic-requirements.json"
        requirements.write_text(json.dumps(raw), encoding="utf-8")
    values: dict[str, object] = {
        "part": PART,
        "subckt": SUBCKT,
        "datasheet": datasheet_for(tmp_path),
        "out_dir": tmp_path / "out",
        "requirements_json": requirements,
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

    assert result.status == "UNKNOWN", result.detail
    assert "limited coverage" in result.detail
    assert not result.counts or result.counts["FAIL"] == 0

    # One row per datasheet row, in the fixture's order, with the reviewed split.
    fixture_ids = [requirement.req_id for requirement in fixture_requirements()]
    assert [row.req_id for row in result.rows] == fixture_ids
    by_id = {row.req_id: row for row in result.rows}
    assert {row.req_id for row in result.rows if row.status == "PASS"} == set(BOUND_IDS)
    unbound = [row for row in result.rows if row.status == "NOT_APPLICABLE"]
    assert len(unbound) == 8
    assert result.counts["UNKNOWN"] == 21
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
    assert results["status"] == "UNKNOWN"
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
    assert by_status["NOT_APPLICABLE"] == 8
    assert result.counts["FAIL"] == 1 and result.counts["UNKNOWN"] == 22


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
    assert sum(row.status == "UNKNOWN" for row in result.rows) == 30
    assert sum(row.status == "NOT_APPLICABLE" for row in result.rows) == 8
    assert result.counts == {
        "PASS": 0,
        "FAIL": 0,
        "UNKNOWN": 30,
        "BLOCKED": 0,
        "NOT_APPLICABLE": 8,
    }


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

    assert result.status == "UNKNOWN", result.detail
    assert "limited coverage" in result.detail
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
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))
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
    assert sum(row.status == "UNKNOWN" for row in result.rows) == 30
    assert sum(row.status == "NOT_APPLICABLE" for row in result.rows) == 8


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
# (f2) cited polarity: verified, scoped evidence or a declared gap


def _synthetic_io_rows(**overrides: object) -> list[Requirement]:
    doc = str(overrides.get("doc_id", "DOC_SYNTH_IO"))
    part = str(overrides.get("applies_to", "SYNTH_IO"))
    polarity_excerpt = str(
        overrides.get(
            "polarity_excerpt",
            "Output is noninverting: A high gives Y high; A low gives Y low.",
        )
    )
    rows = [
        {
            "req_id": "REQ-VOH",
            "applies_to": str(overrides.get("voh_part", part)),
            "kind": "ELECTRICAL",
            "class": "DOCUMENTED_LIMIT",
            "criticality": "IMPORTANT",
            "origin": "DOCUMENT",
            "statement": (
                "High-level output voltage VOH is a minimum of 3.20 V at VCC = 3.3 V, "
                "IOH = -2 mA, TA = 25 C."
            ),
            "limits": {"min": 3.2, "unit": "V"},
            "conditions": [
                {
                    "text": "VCC = 3.3 V, IOH = -2 mA, TA = 25 C",
                    "parameter_overrides": {"io_vcc": 3.3, "io_load_a": -0.002},
                }
            ],
            "signal_refs": overrides.get("voh_signals", ["Y"]),
            "evidence": [
                {
                    "doc_id": str(overrides.get("voh_doc", doc)),
                    "excerpt": (
                        "High-level output voltage VOH: minimum 3.20 V at VCC = 3.3 V, "
                        "IOH = -2 mA, TA = 25 C."
                    ),
                    "extraction": "embedded_text",
                }
            ],
            "citation_verified": True,
        },
        {
            "req_id": "REQ-POL",
            "applies_to": str(overrides.get("polarity_part", part)),
            "kind": "FUNCTIONAL",
            "class": "UNKNOWN",
            "criticality": "IMPORTANT",
            "origin": "DOCUMENT",
            "statement": "Output is noninverting: A high gives Y high; A low gives Y low.",
            "limits": None,
            "conditions": [],
            "signal_refs": overrides.get("polarity_signals", ["A", "Y"]),
            "evidence": [
                {
                    "doc_id": str(overrides.get("polarity_doc", doc)),
                    "excerpt": polarity_excerpt,
                    "extraction": "embedded_text",
                }
            ],
            "citation_verified": True,
        },
    ]
    return [Requirement.model_validate(row) for row in rows]


def _voh_entry(rows: list[Requirement], **kwargs: object) -> dict:
    return next(
        entry for entry in bind_requirements(rows, **kwargs) if entry["req_id"] == "REQ-VOH"
    )


def test_cited_polarity_binds_a_same_part_verified_voh_row() -> None:
    entry = _voh_entry(_synthetic_io_rows())
    assert entry["probe"] == "io_voh"
    assert entry["params"]["io_inverting"] == 0.0
    assert entry["params"]["io_load_a"] == pytest.approx(0.002)
    assert entry["params"]["io_vcc"] == pytest.approx(3.3)
    provenance = entry["derived_conditions"]["io_inverting"]
    assert provenance["source_req"] == "REQ-POL"
    assert "noninverting" in provenance["excerpt"]


def test_a_rejected_polarity_citation_leaves_the_row_a_gap() -> None:
    entry = _voh_entry(_synthetic_io_rows(), unverified={"REQ-POL": "excerpt absent"})
    assert entry["probe"] is None
    assert "condition_missing" in entry["not_testable_reason"]
    assert "io_inverting" in entry["not_testable_reason"]


def test_polarity_needs_a_verbatim_excerpt_not_a_statement() -> None:
    entry = _voh_entry(_synthetic_io_rows(polarity_excerpt=""))
    assert entry["probe"] is None


def test_polarity_from_another_part_or_document_does_not_seed() -> None:
    assert _voh_entry(_synthetic_io_rows(polarity_part="OTHER_PART"))["probe"] is None
    assert _voh_entry(_synthetic_io_rows(polarity_doc="DOC_OTHER"))["probe"] is None


def test_polarity_for_an_unrelated_signal_does_not_seed() -> None:
    entry = _voh_entry(_synthetic_io_rows(polarity_signals=["A", "B"]))
    assert entry["probe"] is None


def test_a_non_logic_mention_of_noninverting_does_not_seed_polarity() -> None:
    entry = _voh_entry(
        _synthetic_io_rows(
            polarity_excerpt="The noninverting amplifier drives Y but states no truth table."
        )
    )
    assert entry["probe"] is None


def test_polarity_requires_an_output_assertion_not_an_input_pin_description() -> None:
    entry = _voh_entry(
        _synthetic_io_rows(polarity_excerpt="Y is the output. A is the noninverting input.")
    )
    assert entry["probe"] is None


def test_a_unicode_dash_does_not_invert_a_noninverting_output() -> None:
    entry = _voh_entry(_synthetic_io_rows(polarity_excerpt="Y is a non\u2013inverting output."))
    assert entry["probe"] == "io_voh"
    assert entry["params"]["io_inverting"] == 0.0


def test_a_lost_hyphen_does_not_invert_a_noninverting_output() -> None:
    for excerpt in (
        "Y is a non inverting output.",
        "Y is a non\ninverting output.",
        "Y is a non - inverting output.",
        "Y is a non- inverting output.",
        "Y is a non -inverting output.",
    ):
        entry = _voh_entry(_synthetic_io_rows(polarity_excerpt=excerpt))
        assert entry["probe"] == "io_voh", excerpt
        assert entry["params"]["io_inverting"] == 0.0, excerpt


def test_a_buffer_is_output_polarity_evidence_too() -> None:
    entry = _voh_entry(_synthetic_io_rows(polarity_excerpt="Y is a noninverting buffer."))
    assert entry["probe"] == "io_voh"
    assert entry["params"]["io_inverting"] == 0.0


def test_polarity_matches_a_node_syntax_signal_reference() -> None:
    """The extractor stores ``V(Y)``; datasheet prose writes ``Y`` for the same node."""
    entry = _voh_entry(_synthetic_io_rows(voh_signals=["V(Y)"], polarity_signals=["V(A)", "V(Y)"]))
    assert entry["probe"] == "io_voh"
    assert entry["params"]["io_inverting"] == 0.0
    assert entry["derived_conditions"]["io_inverting"]["source_req"] == "REQ-POL"


def test_a_differential_signal_reference_does_not_seed_polarity() -> None:
    entry = _voh_entry(_synthetic_io_rows(voh_signals=["V(A)-V(B)"]))
    assert entry["probe"] is None


def test_conflicting_polarity_evidence_leaves_the_row_a_gap() -> None:
    rows = _synthetic_io_rows()
    polarity = rows[1]
    conflict = polarity.model_copy(
        update={
            "req_id": "REQ-POL2",
            "statement": "Output is inverting: A high gives Y low.",
            "evidence": [
                polarity.evidence[0].model_copy(
                    update={
                        "excerpt": "Output is inverting: A high gives Y low; A low gives Y high."
                    }
                )
            ],
        }
    )
    rows.append(conflict)
    assert _voh_entry(rows)["probe"] is None


def _io_row(
    req_id: str,
    statement: str,
    unit: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    conditions: list[Condition] | None = None,
    section: str | None = None,
) -> Requirement:
    """One datasheet-shaped row copied from the fixture's first requirement."""
    base = fixture_requirements()[0]
    evidence = base.evidence[0].model_copy(update={"excerpt": statement, "section": section})
    return base.model_copy(
        deep=True,
        update={
            "req_id": req_id,
            "statement": statement,
            "limits": base.limits.model_copy(
                update={"min": minimum, "typ": None, "max": maximum, "unit": unit}
            ),
            "conditions": conditions or [],
            "evidence": [evidence],
        },
    )


def test_shutdown_ioff_binds_to_the_regulator_and_power_off_leakage_to_the_io_probe() -> None:
    """``IOFF`` is ambiguous: ``shutdown`` disambiguates it to the regulator probe."""
    shutdown = _io_row("REQ_SHUT", "Shutdown current IOFF is at most 1 uA", "A", maximum=1e-6)
    leak = _io_row(
        "REQ_LEAK",
        "Power-off leakage current IOFF at VCC = 0 V",
        "A",
        maximum=5e-6,
        conditions=[Condition(text="VCC = 0 V", parameter_overrides={"io_test_v": 3.3})],
    )
    bare = _io_row(
        "REQ_BARE",
        "Power-off leakage current at VCC = 0 V",
        "A",
        maximum=5e-6,
        conditions=[Condition(text="VCC = 0 V", parameter_overrides={"io_test_v": 3.3})],
    )

    entries = {entry["req_id"]: entry for entry in bind_requirements([shutdown, leak, bare])}

    assert entries["REQ_SHUT"]["probe"] == "shutdown_current"
    assert entries["REQ_LEAK"]["probe"] == "io_power_off_leakage"
    assert entries["REQ_LEAK"]["params"]["io_vcc"] == 0.0
    assert entries["REQ_BARE"]["probe"] == "io_power_off_leakage"


@pytest.mark.parametrize(
    "separator",
    ["supply current", "supply-current", "supply\u2013current", "supply \u2014 current"],
)
@pytest.mark.parametrize("vcc", ["0", "3.3"])
def test_a_supply_current_row_never_falls_through_to_output_leakage(separator, vcc) -> None:
    """A supply-current limit is not measured as output leakage by claiming IOFF."""
    row = _io_row(
        "REQ_OFF",
        f"Off-state {separator} IOFF is at most 1 uA at VCC = {vcc} V",
        "A",
        maximum=1e-6,
        conditions=[Condition(text=f"VCC = {vcc} V", parameter_overrides={"io_test_v": 3.3})],
    )
    entry = bind_requirements([row])[0]
    assert entry["probe"] is None, (separator, vcc)
    assert "no deterministic probe" in entry["not_testable_reason"], (separator, vcc)


def test_a_valid_output_leakage_row_under_a_supply_current_heading_still_binds() -> None:
    """The exclusion looks at the statement, not a section/table heading."""
    row = _io_row(
        "REQ_LEAK_HEADING",
        "Power-off leakage current IOFF at VCC = 0 V",
        "A",
        maximum=5e-6,
        conditions=[Condition(text="VCC = 0 V", parameter_overrides={"io_test_v": 3.3})],
        section="Supply current",
    )
    entry = bind_requirements([row])[0]
    assert entry["probe"] == "io_power_off_leakage"
    assert entry["params"]["io_vcc"] == 0.0


# --------------------------------------------------------------------------- #
# (f3) the product path's finite per-turn default


def test_the_default_api_backend_bounds_a_turn_even_for_the_bob_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.authoring.api_backend import DEFAULT_TIMEOUT_S
    from boardmodeler.authoring.backends import BobShellBackend
    from boardmodeler.config import AppConfig

    monkeypatch.setattr(
        api_module, "load_config", lambda path=None: AppConfig(agent_provider="bob")
    )

    backend = engine.build_backend(make_request(tmp_path, backend_name="api"))

    assert isinstance(backend, BobShellBackend)
    assert backend.timeout_s == DEFAULT_TIMEOUT_S


def test_an_explicit_turn_timeout_overrides_the_api_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.authoring.backends import BobShellBackend
    from boardmodeler.config import AppConfig

    monkeypatch.setattr(
        api_module, "load_config", lambda path=None: AppConfig(agent_provider="bob")
    )

    backend = engine.build_backend(make_request(tmp_path, backend_name="api", turn_timeout_s=42.0))

    assert isinstance(backend, BobShellBackend)
    assert backend.timeout_s == 42.0


def _zero_coverage_inputs(tmp_path: Path) -> tuple[Path, Path]:
    requirements = {
        "document": {"doc_id": "DOC_SYNTH"},
        "requirements": [
            {
                "req_id": "REQ-HYST",
                "applies_to": "SYNTH_REG",
                "kind": "ELECTRICAL",
                "class": "DOCUMENTED_LIMIT",
                "criticality": "IMPORTANT",
                "origin": "TEST_FIXTURE",
                "statement": "The hysteresis is 100 mV.",
                "limits": {"max": 0.1, "unit": "V"},
                "conditions": [],
                "signal_refs": [],
                "evidence": [
                    {
                        "doc_id": "DOC_SYNTH",
                        "excerpt": "hysteresis 100 mV",
                        "extraction": "synthetic_fixture",
                    }
                ],
            }
        ],
        "pin_map": [],
    }
    bindings = {
        "part": "SYNTH_REG",
        "subckt": "SYNTH_REG",
        "doc_id": "DOC_SYNTH",
        "bindings": [
            {"req_id": "REQ-HYST", "probe": None, "not_testable_reason": "derived hysteresis"}
        ],
    }
    requirements_path = tmp_path / "zero-req.json"
    bindings_path = tmp_path / "zero-bind.json"
    requirements_path.write_text(json.dumps(requirements), encoding="utf-8")
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
    return requirements_path, bindings_path


def test_a_zero_coverage_spec_skips_author_reinforcement_and_simulation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requirements_path, bindings_path = _zero_coverage_inputs(tmp_path)

    def forbidden_script(turn, workdir, prompt):
        pytest.fail("zero covered rows must not invoke the author")

    use_backend(monkeypatch, ScriptedBackend(forbidden_script))
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))
    monkeypatch.setattr(engine, "reinforce", lambda **kwargs: pytest.fail("no reinforcement"))

    result, _events, _wall = run(
        tmp_path,
        requirements_json=requirements_path,
        bindings_json=bindings_path,
        reinforce=True,
    )

    assert result.status == "UNKNOWN"
    assert "no_covered_characteristics" in result.detail
    assert result.rows and all(row.status == "UNKNOWN" for row in result.rows)


def _two_corner_voh_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Two VOH rows that share one probe at different supply corners."""

    def row(req_id: str, vcc: float) -> dict:
        return {
            "req_id": req_id,
            "applies_to": "SYNTH_IO",
            "kind": "ELECTRICAL",
            "class": "DOCUMENTED_LIMIT",
            "criticality": "IMPORTANT",
            "origin": "TEST_FIXTURE",
            "statement": f"High-level output voltage VOH at VCC = {vcc} V is a minimum of 1.5 V.",
            "limits": {"min": 1.5, "unit": "V"},
            "conditions": [
                {
                    "text": f"VCC = {vcc} V, IOH = -2 mA",
                    "parameter_overrides": {"io_vcc": vcc, "io_load_a": -0.002},
                }
            ],
            "signal_refs": ["Y"],
            "evidence": [
                {
                    "doc_id": "DOC_SYNTH_IO",
                    "excerpt": f"VOH at VCC = {vcc} V",
                    "extraction": "synthetic_fixture",
                }
            ],
        }

    requirements = {
        "document": {"doc_id": "DOC_SYNTH_IO"},
        "pin_map": [],
        "requirements": [row("REQ_VOH_33", 3.3), row("REQ_VOH_18", 1.8)],
    }
    bindings = {
        "part": "SYNTH_IO",
        "subckt": "SYNTH_IO",
        "doc_id": "DOC_SYNTH_IO",
        "bindings": [
            {
                "req_id": "REQ_VOH_33",
                "probe": "io_voh",
                "params": {
                    "io_vcc": 3.3,
                    "io_load_a": 0.002,
                    "io_inverting": 0,
                    "io_input_high": 3.3,
                },
            },
            {
                "req_id": "REQ_VOH_18",
                "probe": "io_voh",
                "params": {
                    "io_vcc": 1.8,
                    "io_load_a": 0.002,
                    "io_inverting": 0,
                    "io_input_high": 1.8,
                },
            },
        ],
    }
    requirements_path = tmp_path / "two-corner-req.json"
    bindings_path = tmp_path / "two-corner-bind.json"
    requirements_path.write_text(json.dumps(requirements), encoding="utf-8")
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
    return requirements_path, bindings_path


def test_each_operating_point_keeps_its_own_row_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One probe, two corners: each row shows the outcome that judged it, not its twin's."""
    from boardmodeler.authoring import loop as loop_module
    from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome

    requirements_path, bindings_path = _two_corner_voh_inputs(tmp_path)

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        del model_lib, subckt, workdir, ltspice, timeout_s, cancel
        verdicts = (("REQ_VOH_33", "FAIL", 1.2), ("REQ_VOH_18", "PASS", 1.7))
        outcomes = tuple(
            ProbeOutcome(
                probe_id="io_voh",
                status=status,
                measured={"io_voltage_v": value},
                detail=f"{char_id}: io_voltage_v = {value:.6g} V",
                unknown_reason=None,
                run_dir="canned",
                char_ids=(char_id,),
                judged=f"io_voltage_v = {value:.6g} V",
            )
            for char_id, status, value in verdicts
        )
        return HarnessReport(
            part=spec.part, model_sha256="a" * 64, spec_digest=spec.digest(), outcomes=outcomes
        )

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    def script(turn: int, workdir: Path, prompt: str) -> None:
        del turn, prompt
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "SYNTH_IO.lib").write_text(
            ".subckt SYNTH_IO VCC A Y GND\nR1 Y 0 1k\n.ends SYNTH_IO\n", encoding="utf-8"
        )

    use_backend(monkeypatch, ScriptedBackend(script))
    result, _events, _wall = run(
        tmp_path,
        subckt="SYNTH_IO",
        requirements_json=requirements_path,
        bindings_json=bindings_path,
        max_iterations=1,
        reinforce=False,
    )

    by_id = {row.req_id: row for row in result.rows}
    assert by_id["REQ_VOH_33"].status == "FAIL"
    assert by_id["REQ_VOH_18"].status == "PASS"
    assert by_id["REQ_VOH_33"].measured != by_id["REQ_VOH_18"].measured


def _split_voh_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Split VOH rows plus a missing-outcome row, all at one operating point each."""

    def row(req_id: str, class_: str, limits: dict) -> dict:
        return {
            "req_id": req_id,
            "applies_to": "SYNTH_IO",
            "kind": "ELECTRICAL",
            "class": class_,
            "criticality": "IMPORTANT",
            "origin": "TEST_FIXTURE",
            "statement": "High-level output voltage VOH at VCC = 3.3 V.",
            "limits": limits,
            "conditions": [
                {
                    "text": "VCC = 3.3 V, IOH = -2 mA",
                    "parameter_overrides": {"io_vcc": 3.3, "io_load_a": -0.002},
                }
            ],
            "signal_refs": ["Y"],
            "evidence": [
                {
                    "doc_id": "DOC_SYNTH_IO",
                    "excerpt": "VOH at VCC = 3.3 V",
                    "extraction": "synthetic_fixture",
                }
            ],
        }

    requirements = {
        "document": {"doc_id": "DOC_SYNTH_IO"},
        "pin_map": [],
        "requirements": [
            row("REQ_VOH_MIN", "DOCUMENTED_LIMIT", {"min": 2.4, "unit": "V"}),
            row("REQ_VOH_TYP", "TYPICAL_VALUE", {"typ": 3.2, "unit": "V"}),
            row("REQ_VOL_MISSING", "DOCUMENTED_LIMIT", {"max": 0.4, "unit": "V"}),
        ],
    }
    params = {"io_vcc": 3.3, "io_load_a": 0.002, "io_inverting": 0, "io_input_high": 3.3}
    bindings = {
        "part": "SYNTH_IO",
        "subckt": "SYNTH_IO",
        "doc_id": "DOC_SYNTH_IO",
        "bindings": [
            {"req_id": "REQ_VOH_MIN", "probe": "io_voh", "params": dict(params)},
            {"req_id": "REQ_VOH_TYP", "probe": "io_voh", "params": dict(params)},
            {"req_id": "REQ_VOL_MISSING", "probe": "io_vol", "params": dict(params)},
        ],
    }
    requirements_path = tmp_path / "split-req.json"
    bindings_path = tmp_path / "split-bind.json"
    requirements_path.write_text(json.dumps(requirements), encoding="utf-8")
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
    return requirements_path, bindings_path


def test_rows_sharing_one_case_get_their_own_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rows sharing one probe case are judged separately, from one simulation."""
    from boardmodeler.authoring import loop as loop_module
    from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome

    requirements_path, bindings_path = _split_voh_inputs(tmp_path)
    calls: list[str] = []

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        del model_lib, subckt, workdir, ltspice, timeout_s, cancel
        calls.append("harness")
        outcome = ProbeOutcome(
            probe_id="io_voh",
            status="FAIL",
            measured={"io_voltage_v": 2.7},
            detail="REQ_VOH_MIN: pass; REQ_VOH_TYP: fail",
            unknown_reason=None,
            run_dir="canned",
            char_ids=("REQ_VOH_MIN", "REQ_VOH_TYP"),
            judged="io_voltage_v = 2.7 V",
        )
        return HarnessReport(
            part=spec.part, model_sha256="a" * 64, spec_digest=spec.digest(), outcomes=(outcome,)
        )

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    def script(turn: int, workdir: Path, prompt: str) -> None:
        del turn, prompt
        model_dir = workdir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "SYNTH_IO.lib").write_text(
            ".subckt SYNTH_IO VCC A Y GND\nR1 Y 0 1k\n.ends SYNTH_IO\n", encoding="utf-8"
        )

    use_backend(monkeypatch, ScriptedBackend(script))
    result, _events, _wall = run(
        tmp_path,
        subckt="SYNTH_IO",
        requirements_json=requirements_path,
        bindings_json=bindings_path,
        max_iterations=1,
        reinforce=False,
    )

    by_id = {row.req_id: row for row in result.rows}
    assert by_id["REQ_VOH_MIN"].status == "PASS", result.detail
    assert by_id["REQ_VOH_TYP"].status == "FAIL"
    assert by_id["REQ_VOL_MISSING"].status == "UNKNOWN"
    assert calls == ["harness"], "one shared operating point means one simulation"
    assert result.counts == {
        "PASS": 1,
        "FAIL": 1,
        "UNKNOWN": 1,
        "BLOCKED": 0,
        "NOT_APPLICABLE": 0,
    }

    card = (tmp_path / "out" / "MODEL_CARD.md").read_text(encoding="utf-8")
    statuses = {
        line.split("|")[1].strip().strip("`"): line.split("|")[5].strip()
        for line in card.splitlines()
        if line.startswith("| `REQ_VOH") or line.startswith("| `REQ_VOL")
    }
    assert statuses == {
        "REQ_VOH_MIN": "PASS",
        "REQ_VOH_TYP": "FAIL",
        "REQ_VOL_MISSING": "UNKNOWN",
    }
    totals = next(line for line in card.splitlines() if line.startswith("**Totals:**"))
    assert "1 pass" in totals and "1 fail" in totals and "1 unknown" in totals, totals


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
    from boardmodeler.authoring.harness import HarnessReport

    final_report = HarnessReport.from_json(
        (tmp_path / "out" / "harness-report.json").read_text(encoding="utf-8")
    )
    assert turns[1].counts == {**final_report.counts(), "turn": 2}
    assert result.counts["PASS"] == sum(row.status == "PASS" for row in result.rows)
    assert result.status == "UNKNOWN", result.detail
    assert "limited coverage" in result.detail


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
    assert result.counts["FAIL"] == 1
    assert result.counts["UNKNOWN"] == 29, "the rows the canned harness did not report are gaps"
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


def test_the_default_api_backend_still_honours_a_bob_team_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--backend api`` (the default) must not silently drop ``--team-id`` for Bob."""
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.authoring.backends import BobShellBackend
    from boardmodeler.config import AppConfig

    monkeypatch.setattr(api_module, "load_config", lambda path=None: AppConfig())

    backend = engine.build_backend(
        make_request(tmp_path, backend_name="api", provider="bob", team_id="team-api")
    )

    assert isinstance(backend, BobShellBackend)
    assert backend.team_id == "team-api"


def test_the_api_backend_is_built_from_the_catalog_entry_and_the_config(monkeypatch, tmp_path):
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.authoring.backends import BobShellBackend
    from boardmodeler.config import AppConfig

    monkeypatch.setattr(api_module, "load_config", lambda path=None: AppConfig())
    backend = engine.build_backend(make_request(tmp_path, backend_name="api", provider="bob"))
    assert isinstance(backend, BobShellBackend)
    refused = engine.build_backend(
        make_request(tmp_path, backend_name="api", provider="unaccepted")
    )
    usable, reason = refused.availability()
    assert not usable and "IBM Bob only" in reason and "unaccepted" not in reason


def test_an_unknown_agent_provider_is_blocked_rather_than_substituted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from boardmodeler.authoring import api_backend as api_module
    from boardmodeler.config import AppConfig

    monkeypatch.setattr(api_module, "load_config", lambda path=None: AppConfig())

    backend = engine.build_backend(make_request(tmp_path, backend_name="api", provider="magic"))

    usable, reason = backend.availability()
    assert usable is False
    assert reason.startswith("api_provider_unavailable:") and "magic" not in reason
    assert "IBM Bob only" in reason


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


@pytest.mark.ltspice
def test_a_fresh_process_revalidates_before_reinforcement_or_the_author(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ltspice_exe: Path
) -> None:
    """A process-local cache miss must re-judge the passing candidate, not call out."""
    from boardmodeler.authoring import validation_cache

    pdf = tmp_path / "fixed_datasheet.pdf"
    shutil.copyfile(datasheet_for(tmp_path), pdf)
    use_backend(monkeypatch, ScriptedBackend(template_script()))
    first, _events, _wall = run(tmp_path, datasheet=pdf, reinforce=False)
    assert first.status == "PASS", first.detail

    def forbidden_script(turn, workdir, prompt):
        pytest.fail("a passing candidate must be revalidated before the author")

    use_backend(monkeypatch, ScriptedBackend(forbidden_script))
    monkeypatch.setattr(engine, "reinforce", lambda **kwargs: pytest.fail("no reinforcement"))
    monkeypatch.setattr(validation_cache, "_OBSERVED", {})

    second, _events2, _wall2 = run(tmp_path, datasheet=pdf, reinforce=True)

    assert second.status == "PASS", second.detail


def test_a_run_author_revalidates_the_candidate_before_gathering_reinforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh process has no receipt: the existing pass is re-judged before any search."""
    from boardmodeler.authoring.harness import HarnessReport
    from boardmodeler.authoring.loop import BuildOutcome

    spec = load_tps54320_spec(REQUIREMENTS, BINDINGS, part=PART, subckt=SUBCKT)
    request = make_request(tmp_path)
    run = engine._Run(request, engine._StageLog(None))
    run.spec = spec
    run.workdir = Path(request.out_dir) / engine.WORK_DIRNAME
    run.backend = ScriptedBackend(template_script())
    run.backend_name = "scripted"
    monkeypatch.setattr(engine, "locate", lambda explicit=None: fake_ltspice(tmp_path))

    report = HarnessReport(part=PART, model_sha256="a" * 64, spec_digest=spec.digest(), outcomes=())
    sentinel = BuildOutcome(
        status="PASS",
        iterations=0,
        report=report,
        history=(),
        detail="existing candidate revalidated by the simulator; zero author turns",
    )
    calls: list[str] = []

    def fake_revalidate(request: object, cancel: object = None) -> BuildOutcome:
        calls.append("revalidate")
        return sentinel

    monkeypatch.setattr(engine, "revalidate_candidate", fake_revalidate)

    def forbidden(**kwargs: object) -> object:
        raise AssertionError("reinforcement must not run before revalidation")

    monkeypatch.setattr(engine, "reinforce", forbidden)

    run.author(None)

    assert calls == ["revalidate"]
    assert run.outcome is sentinel


def test_a_broken_candidate_is_prechecked_once_without_a_spurious_judge_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An uncacheable UNKNOWN precheck must not make build_model simulate it again."""
    from boardmodeler.authoring import loop as loop_module
    from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome

    calls: list[str] = []

    def canned_harness(*, model_lib, subckt, spec, workdir, ltspice, timeout_s=120.0, cancel=None):
        del subckt, workdir, ltspice, timeout_s, cancel
        calls.append(str(model_lib))
        outcome = ProbeOutcome(
            probe_id="vref",
            status="UNKNOWN",
            measured={},
            detail="",
            unknown_reason="model_lib_unreadable: no usable .subckt",
            run_dir="canned",
            char_ids=(VREF_ID,),
        )
        return HarnessReport(
            part=spec.part, model_sha256="a" * 64, spec_digest=spec.digest(), outcomes=(outcome,)
        )

    monkeypatch.setattr(loop_module, "run_harness", canned_harness)
    install = fake_ltspice(tmp_path)
    install.path.write_bytes(b"")
    monkeypatch.setattr(engine, "locate", lambda explicit=None: install)

    stub = f".subckt {SUBCKT} VIN EN FB VOUT GND SW\nR1 VOUT FB 1k\n.ends {SUBCKT}\n"
    model_dir = tmp_path / "out" / engine.WORK_DIRNAME / "model"
    model_dir.mkdir(parents=True)
    (model_dir / f"{SUBCKT}.lib").write_text(stub, encoding="utf-8")

    def script(turn: int, workdir: Path, prompt: str) -> None:
        del turn, prompt
        target = workdir / "model"
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{SUBCKT}.lib").write_text(stub, encoding="utf-8")

    use_backend(monkeypatch, ScriptedBackend(script))
    result, events, _wall = run(tmp_path, max_iterations=1, reinforce=False)

    assert len(calls) == 2, "one precheck plus one authoring turn, never a second precheck"
    assert [event.counts["turn"] for event in judge_events(events)] == [1]
    assert result.rows


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
