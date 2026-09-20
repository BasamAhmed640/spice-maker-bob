"""The CLI's datasheet path: same engine as the window, printed for a terminal.

The engine is stubbed here on purpose — its own tests cover the simulation work. What
these tests pin is the CLI contract: argument routing, the JSON payload the automation
consumes, the human output, and the exit codes.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from boardmodeler import cli


@dataclass(frozen=True)
class _Row:
    req_id: str
    required: str
    measured: str
    status: str
    page: int | None
    statement: str = ""


@dataclass(frozen=True)
class _Result:
    status: str
    detail: str
    part: str
    out_dir: Path
    rows: tuple[_Row, ...]
    counts: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "status": self.status,
                "detail": self.detail,
                "part": self.part,
                "out_dir": str(self.out_dir),
                "counts": self.counts,
            },
            indent=2,
        )


@dataclass(frozen=True)
class _Request:
    part: str
    subckt: str
    datasheet: Path
    out_dir: Path
    backend_name: str = "api"
    team_id: str | None = None
    max_iterations: int = 3
    timeout_s: float = 120.0
    allow_remote: bool = False
    provider: str | None = None
    agent_model: str | None = None
    agent_max_tokens: int | None = None
    requirements_json: Path | None = None
    bindings_json: Path | None = None
    reinforce: bool | None = None


class _Stage:
    def __init__(self, stage: str, status: str, detail: str) -> None:
        self.stage = stage
        self.status = status
        self.detail = detail
        self.counts: dict[str, int] = {}


def _install_fake_engine(monkeypatch, result: _Result, calls: list[_Request]) -> None:
    module = ModuleType("boardmodeler.pipeline.make_model")

    def make_model(request: _Request, progress=None, cancel=None) -> _Result:
        calls.append(request)
        if callable(progress):
            progress(_Stage("extract", "ok", "38 rows"))
            progress(_Stage("judge", "ok", "turn 1 PASS 1"))
        return result

    module.make_model = make_model  # type: ignore[attr-defined]
    module.MakeModelRequest = _Request  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boardmodeler.pipeline.make_model", module)


@pytest.fixture
def datasheet(tmp_path: Path) -> Path:
    path = tmp_path / "tps54320.pdf"
    path.write_bytes(b"%PDF-1.4 fake datasheet")
    return path


def test_datasheet_mode_reports_rows_and_exits_zero(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    calls: list[_Request] = []
    result = _Result(
        status="PASS",
        detail="every testable row passes",
        part="TPS54320",
        out_dir=tmp_path / "out",
        rows=(
            _Row("REQ_1", "min 4 / max 4.5 V", "vin_uvlo_rise=4.21 V", "PASS", 4),
            _Row("REQ_2", "no simulation probe", "-", "NOT_APPLICABLE", 9),
        ),
        counts={"PASS": 1, "NOT_APPLICABLE": 1},
    )
    _install_fake_engine(monkeypatch, result, calls)

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320-Q1",
            "--datasheet",
            str(datasheet),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert calls and calls[0].part == "TPS54320-Q1"
    assert calls[0].subckt == "TPS54320_Q1", "the subcircuit name must be SPICE-legal"
    assert calls[0].backend_name == "api", "the API-key provider is the default author"
    assert "extract" in output and "judge" in output
    assert "REQ_1" in output and "vin_uvlo_rise=4.21 V" in output


def test_json_mode_emits_the_rows_for_automation(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    calls: list[_Request] = []
    result = _Result(
        status="UNKNOWN",
        detail="harness left one probe UNKNOWN",
        part="TPS54320",
        out_dir=tmp_path,
        rows=(_Row("REQ_1", "min 4 / max 4.5 V", "-", "UNKNOWN", 4),),
        counts={"UNKNOWN": 1},
    )
    _install_fake_engine(monkeypatch, result, calls)

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(datasheet),
            "--out",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "model build"
    assert payload["status"] == "UNKNOWN"
    assert payload["rows"][0]["req_id"] == "REQ_1"
    assert payload["counts"] == {"UNKNOWN": 1}


def test_the_reinforcement_switch_reaches_the_request(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    """--no-reinforce must reach the engine; the default follows the persisted setting."""
    calls: list[_Request] = []
    _install_fake_engine(monkeypatch, _Result("PASS", "", "TPS54320", tmp_path, (), {}), calls)
    base = [
        "model",
        "build",
        "--part",
        "TPS54320",
        "--datasheet",
        str(datasheet),
        "--out",
        str(tmp_path),
    ]

    cli.main([*base, "--json"])
    capsys.readouterr()
    assert calls[-1].reinforce is None

    cli.main([*base, "--json", "--no-reinforce"])
    capsys.readouterr()
    assert calls[-1].reinforce is False


def test_strict_turns_a_non_pass_into_a_failure_exit(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    calls: list[_Request] = []
    result = _Result("UNKNOWN", "cap reached", "TPS54320", tmp_path, (), {})
    _install_fake_engine(monkeypatch, result, calls)
    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(datasheet),
            "--out",
            str(tmp_path),
            "--json",
            "--strict",
        ]
    )
    capsys.readouterr()
    assert code == 1


def test_a_missing_datasheet_is_blocked_before_any_engine_call(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls: list[_Request] = []
    _install_fake_engine(monkeypatch, _Result("PASS", "", "X", tmp_path, (), {}), calls)
    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(tmp_path / "nope.pdf"),
            "--out",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "BLOCKED" and "nope.pdf" in payload["detail"]
    assert calls == []


def test_neither_datasheet_nor_fixtures_is_rejected(tmp_path: Path, capsys) -> None:
    code = cli.main(["model", "build", "--part", "TPS54320", "--out", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "--datasheet" in payload["detail"] and "--bindings" in payload["detail"]


def test_the_supplied_extraction_reaches_the_request(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    """--requirements/--bindings must skip extraction instead of asking a provider."""
    calls: list[_Request] = []
    _install_fake_engine(monkeypatch, _Result("PASS", "", "TPS54320", tmp_path, (), {}), calls)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    requirements = fixtures / "requirements.json"
    bindings = fixtures / "probes.json"
    requirements.write_text("{}", encoding="utf-8")
    bindings.write_text("{}", encoding="utf-8")

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(datasheet),
            "--requirements",
            str(requirements),
            "--bindings",
            str(bindings),
            "--out",
            str(tmp_path),
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert calls[-1].requirements_json == requirements
    assert calls[-1].bindings_json == bindings


@pytest.mark.parametrize("missing", ["--bindings", "--requirements"])
def test_half_a_supplied_extraction_is_refused_before_any_engine_call(
    missing: str, tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    """Half a reviewed extraction would silently fall back to provider extraction."""
    calls: list[_Request] = []
    _install_fake_engine(monkeypatch, _Result("PASS", "", "TPS54320", tmp_path, (), {}), calls)
    half = tmp_path / "half.json"
    half.write_text("{}", encoding="utf-8")
    flag = "--requirements" if missing == "--bindings" else "--bindings"

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(datasheet),
            flag,
            str(half),
            "--out",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["status"] == "BLOCKED"
    assert "--requirements" in payload["detail"] and "--bindings" in payload["detail"]
    assert calls == []


def test_the_agent_provider_model_and_budget_reach_the_request(
    tmp_path: Path, datasheet: Path, monkeypatch, capsys
) -> None:
    from boardmodeler import agent_providers
    from boardmodeler.authoring import api_backend

    calls: list[_Request] = []
    _install_fake_engine(monkeypatch, _Result("PASS", "", "TPS54320", tmp_path, (), {}), calls)

    # A provider this build accepts (it can reach a backend instead of refusing the id)
    # and a model id of the caller's own: both are forwarded, not validated or replaced.
    entry = next(
        (p for p in agent_providers.CATALOG if p.wire in api_backend.HTTP_WIRES),
        agent_providers.default_provider(),
    )
    override = "cli-model-override"

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--datasheet",
            str(datasheet),
            "--out",
            str(tmp_path),
            "--backend",
            "api",
            "--provider",
            entry.id,
            "--model",
            override,
            "--max-tokens",
            "4096",
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert calls[-1].backend_name == "api"
    assert calls[-1].provider == entry.id
    assert calls[-1].agent_model == override
    assert calls[-1].agent_max_tokens == 4096


def test_the_direct_build_path_forwards_the_bob_team_id(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """``model build --requirements ... --team-id`` must reach the resolved backend."""
    from boardmodeler.authoring import api_backend, card
    from boardmodeler.authoring import loop as loop_module
    from boardmodeler.simulation import ltspice

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "regulator" / "tps54320"
    captured: dict[str, object] = {}

    class _Backend:
        name = "api"

    def spy(**kwargs: object) -> _Backend:
        captured.update(kwargs)
        return _Backend()

    class _Report:
        model_sha256 = ""
        outcomes: tuple[object, ...] = ()

        def counts(self) -> dict[str, int]:
            return {}

        def to_json(self) -> str:
            return "{}"

    class _Outcome:
        status = "UNKNOWN"
        detail = "stub"
        iterations = 0
        history: tuple[str, ...] = ()
        report = _Report()

    monkeypatch.setattr(api_backend, "build_api_backend", spy)
    monkeypatch.setattr(loop_module, "build_model", lambda request: _Outcome())
    monkeypatch.setattr(card, "write_deliverables", lambda **kwargs: [])
    monkeypatch.setattr(
        ltspice,
        "locate",
        lambda explicit=None: SimpleNamespace(path=tmp_path / "ltspice.exe"),
    )

    code = cli.main(
        [
            "model",
            "build",
            "--part",
            "TPS54320",
            "--requirements",
            str(fixture / "requirements.json"),
            "--bindings",
            str(fixture / "probes.json"),
            "--out",
            str(tmp_path),
            "--backend",
            "api",
            "--team-id",
            "team-x",
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert captured.get("team_id") == "team-x"
