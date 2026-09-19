"""BoardModeler command line interface.

The CLI is the automation/test surface: it must be usable in scripts and CI, so
every command that reports a result also supports ``--json`` and exits non-zero
only when the request itself failed (a FAIL status is data, not a crash).

Commands are registered as the phases land; ``doctor`` is the environment
surface and reports *only* observed facts (nothing is assumed about the machine).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

from boardmodeler import __version__
from boardmodeler.agent_providers import CATALOG
from boardmodeler.authoring.part_class import classify
from boardmodeler.config import config_path, load_config
from boardmodeler.simulation.backend import probe_backend
from boardmodeler.simulation.ltspice import (
    BATCH_RESOLUTION_NOTES,
    default_lib_dir,
    locate_outcome,
    smoke_test,
)
from boardmodeler.simulation.ltspice import (
    version as ltspice_version,
)

__all__ = ["build_parser", "doctor_payload", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boardmodeler",
        description=(
            "Make LTspice models for ICs from their datasheets: an agent authors the "
            "model and real simulator runs judge it against the datasheet's own rows."
        ),
        epilog=(
            "start here:  boardmodeler model build --part <PN> --datasheet <pdf> "
            "--out <dir>\n"
            "             boardmodeler model install --out <dir> --user-lib --apply\n"
            "             boardmodeler ui      (the same thing as a window)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"boardmodeler {__version__}")
    sub = parser.add_subparsers(dest="command", required=False)

    doctor = sub.add_parser(
        "doctor",
        help="report the environment: LTspice, reader backend, OCR, credentials",
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument(
        "--no-smoke",
        action="store_true",
        help="skip the LTspice smoke test (reports smoke_test=null, never 'pass')",
    )
    doctor.add_argument(
        "--smoke-workdir",
        type=Path,
        default=None,
        help="directory for the smoke artifacts (default: a fresh temp directory)",
    )

    version_cmd = sub.add_parser("version", help="print the version")
    version_cmd.add_argument("--json", action="store_true")

    setup_cmd = sub.add_parser(
        "setup", help="the one page of persistent settings (LTspice, API key, model folder)"
    )
    setup_cmd.add_argument(
        "--json",
        action="store_true",
        help="print the resolved settings instead of a window",
    )

    ui_cmd = sub.add_parser("ui", help="launch the model maker window (add --installer for setup)")
    ui_cmd.add_argument("--project", type=Path, default=None, help="project directory to open")
    ui_cmd.add_argument(
        "--installer", action="store_true", help="open the setup page instead of the model maker"
    )

    run = sub.add_parser("run", help="execute project work (earlier board workflow)")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_tests = run_sub.add_parser(
        "tests", help="run the project's test cases against the simulator"
    )
    run_tests.add_argument("--project", type=Path, required=True, help="project directory")
    run_tests.add_argument("--scope", default=None, help="only cases in this scope")
    run_tests.add_argument(
        "--test",
        action="append",
        dest="test_ids",
        default=None,
        help="only these test ids (repeatable)",
    )
    run_tests.add_argument("--list-tests", action="store_true", help="list cases and exit")
    run_tests.add_argument("--json", action="store_true", help="machine-readable output")
    run_tests.add_argument("--out", type=Path, default=None, help="write results JSON here")
    run_tests.add_argument(
        "--timeout", type=float, default=None, help="per-case simulator timeout in seconds"
    )
    run_tests.add_argument("--ltspice", type=Path, default=None, help="explicit LTspice executable")
    run_tests.add_argument(
        "--ascii-raw",
        action="store_true",
        help="write .raw in ASCII (more precise float rendering, larger files)",
    )
    run_tests.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any result is not PASS (statuses are data otherwise)",
    )

    run_mutations = run_sub.add_parser(
        "mutations", help="inject every fault into its own copy and record detection"
    )
    run_mutations.add_argument("--project", type=Path, required=True, help="project directory")
    run_mutations.add_argument(
        "--report", type=Path, required=True, help="where to write the report"
    )
    run_mutations.add_argument(
        "--fault", action="append", default=None, help="only these faults (repeatable)"
    )
    run_mutations.add_argument("--json", action="store_true")

    demo = sub.add_parser("demo", help="the board demonstration (earlier spec)")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    demo_build = demo_sub.add_parser("build", help="assemble the demo project from the fixtures")
    demo_build.add_argument("--out", type=Path, required=True, help="project directory to create")
    demo_build.add_argument("--json", action="store_true")
    demo_build.add_argument(
        "--no-probe", action="store_true", help="skip capability probing (faster, less evidence)"
    )

    circuit = sub.add_parser("circuit", help="circuit-level checks (earlier spec)")
    circuit_sub = circuit.add_subparsers(dest="circuit_command", required=True)
    circuit_check = circuit_sub.add_parser(
        "check", help="static checks plus the dynamic scenarios against a built project"
    )
    circuit_check.add_argument("--project", type=Path, required=True)
    circuit_check.add_argument(
        "--circuit", type=Path, default=None, help="schematic to netlist-check"
    )
    circuit_check.add_argument("--scope", default=None, help="only cases in this scope")
    circuit_check.add_argument(
        "--fault-matrix", action="store_true", help="also run the fault matrix"
    )
    circuit_check.add_argument("--json", action="store_true")
    circuit_check.add_argument("--out", type=Path, default=None, help="results JSON path")
    circuit_check.add_argument("--report", type=Path, default=None, help="HTML report path")
    circuit_check.add_argument(
        "--strict", action="store_true", help="exit 1 when the overall status is not PASS"
    )

    export = sub.add_parser("export", help="write the portable model/test export")
    export.add_argument("--project", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--json", action="store_true")
    export.add_argument(
        "--model", default=None, help="model id to export (default: first generated)"
    )

    extract = sub.add_parser(
        "extract", help="extract requirements from a document through a provider"
    )
    extract.add_argument("--project", type=Path, required=True)
    extract.add_argument("--doc", type=Path, default=None, help="document to ingest first")
    extract.add_argument(
        "--provider", default=None, help="provider name (fixture, http_inference, bob_direct)"
    )
    extract.add_argument("--allow-remote", action="store_true", help="permit remote inference")
    extract.add_argument("--json", action="store_true")

    model = sub.add_parser(
        "model",
        help="author an LTspice model from a datasheet and judge it with real simulation",
    )
    model_sub = model.add_subparsers(dest="model_command")

    model_build = model_sub.add_parser(
        "build",
        help="let an agent author the model, then judge it against the datasheet rows",
    )
    model_build.add_argument(
        "--part",
        required=True,
        help="part number, e.g. TPS54320; microcontrollers and programmable-logic parts are "
        "refused with BLOCKED and the reason (no probe can judge them)",
    )
    model_build.add_argument(
        "--subckt",
        default=None,
        help="subcircuit name the model must declare (default: the part number, sanitized)",
    )
    model_build.add_argument(
        "--datasheet",
        type=Path,
        default=None,
        help="the datasheet PDF: its rows are extracted, bound to probes, and judged",
    )
    model_build.add_argument(
        "--requirements",
        type=Path,
        default=None,
        help="extracted requirements JSON with citations (offline alternative to --datasheet)",
    )
    model_build.add_argument(
        "--bindings", type=Path, default=None, help="requirement -> probe binding JSON"
    )
    model_build.add_argument(
        "--out", type=Path, required=True, help="output directory for the model"
    )
    model_build.add_argument(
        "--backend",
        default="bob",
        choices=["bob", "scripted", "fixture"],
        help="which agent authors the model (bob = the IBM Bob CLI, "
        "scripted/fixture = the bundled offline template)",
    )
    model_build.add_argument("--team-id", default=None, help="Bob team id for a general API key")
    model_build.add_argument(
        "--provider",
        default=None,
        help="agent provider id (default: the configured provider, else this build's own)",
    )
    model_build.add_argument(
        "--allow-remote", action="store_true", help="permit sending the datasheet to the provider"
    )
    model_build.add_argument(
        "--no-reinforce",
        action="store_true",
        help="do not search the web for supporting material about the part",
    )
    model_build.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop after this many author turns (default: run until every datasheet row is "
        "satisfied, or until the agent stops improving)",
    )
    model_build.add_argument("--timeout", type=float, default=120.0, help="seconds per simulation")
    model_build.add_argument("--json", action="store_true")
    model_build.add_argument(
        "--strict", action="store_true", help="exit 1 when the outcome is not PASS"
    )

    model_test = model_sub.add_parser(
        "test", help="re-run the probe harness on a built model and rewrite its card"
    )
    model_test.add_argument("--out", type=Path, required=True)
    model_test.add_argument("--timeout", type=float, default=120.0)
    model_test.add_argument("--json", action="store_true")
    model_test.add_argument("--strict", action="store_true")

    model_install = model_sub.add_parser(
        "install", help="copy a built model where LTspice can find it"
    )
    model_install.add_argument("--out", type=Path, required=True, help="a built model directory")
    model_install.add_argument(
        "--into", type=Path, default=None, help="destination directory for the .lib and .asy"
    )
    model_install.add_argument(
        "--user-lib",
        action="store_true",
        help="use the per-user LTspice library (%%LOCALAPPDATA%%\\LTspice\\lib)",
    )
    model_install.add_argument(
        "--apply", action="store_true", help="actually copy (without it, the plan is printed)"
    )
    model_install.add_argument("--json", action="store_true")
    return parser


def _ltspice_section(*, run_smoke: bool, smoke_workdir: Path | None) -> dict[str, Any]:
    config = load_config()
    explicit = config.ltspice.path
    outcome = locate_outcome(explicit)
    install = outcome.install

    section: dict[str, Any] = {
        "found": install is not None,
        "path": str(install.path) if install else None,
        "source": install.source if install else None,
        "reason": outcome.reason,
        "env_override": os.environ.get("LTSPICE_EXE"),
        "config_path_setting": explicit,
        "probed": outcome.probed_paths,
        "batch_resolution": BATCH_RESOLUTION_NOTES,
        "lib_dir": str(default_lib_dir()) if default_lib_dir() else config.ltspice.lib_dir,
        "timeout_s": config.ltspice.timeout_s,
    }

    if install is None:
        override = os.environ.get("LTSPICE_EXE")
        if outcome.reason == "configured_missing":
            detail = (
                f"configured LTspice path does not exist: {explicit} "
                "(discovery does not fall back to another installation)"
            )
        elif outcome.reason == "env_missing":
            detail = (
                f"LTSPICE_EXE points at a file that does not exist: {override} "
                "(discovery does not fall back to another installation)"
            )
        else:
            detail = "LTspice executable not found; probed: " + ", ".join(outcome.probed_paths)
        section.update(
            {
                "version": None,
                "smoke_test": "fail" if run_smoke else None,
                "smoke_detail": detail,
            }
        )
        return section

    section["version"] = ltspice_version(install.path)
    if not run_smoke:
        section["smoke_test"] = None
        section["smoke_detail"] = "smoke test skipped (--no-smoke)"
        return section

    workdir = smoke_workdir
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix="boardmodeler-smoke-"))
    result = smoke_test(install.path, workdir, timeout_s=config.ltspice.timeout_s)
    section.update(result.as_dict())
    section["smoke_workdir"] = str(workdir)
    return section


def _reader_section(smoke_raw: Path | None) -> dict[str, Any]:
    return probe_backend(smoke_raw).as_dict()


def _ocr_section() -> dict[str, Any]:
    try:
        ocr = importlib.import_module("boardmodeler.documents.ocr")
    except ImportError as exc:  # pragma: no cover - only before the module lands
        return {"available": None, "reason": "documents_module_unavailable", "detail": str(exc)}
    unavailable = ocr.probe_ocr()
    if unavailable is None:
        return {"available": True, "reason": None, "detail": "OCR engine detected"}
    return {
        "available": False,
        "reason": unavailable.reason,
        "detail": unavailable.detail,
        "engine": unavailable.engine,
    }


def _credentials_section() -> dict:
    """One entry per provider this build accepts, and where its key resolves from.

    The entry is the resolution's *source* only — never a value — and it walks the
    same sources the backend does, the catalog's own environment aliases included,
    so ``doctor`` cannot contradict a build that then succeeds.
    """
    try:
        from boardmodeler.authoring import backends as api
    except ImportError as exc:  # pragma: no cover - only before the module lands
        return {"available": None, "reason": "credentials_module_unavailable", "detail": str(exc)}
    return {
        provider.id: (
            f"credential {provider.credential!r}: "
            f"source={api.credential_for(provider).source.value.lower()}"
        )
        for provider in CATALOG
    }


def doctor_payload(*, run_smoke: bool = True, smoke_workdir: Path | None = None) -> dict[str, Any]:
    """Collect the environment report. Every field is observed, never assumed."""
    cfg_path = config_path()
    ltspice = _ltspice_section(run_smoke=run_smoke, smoke_workdir=smoke_workdir)
    smoke_raw = None
    if ltspice.get("smoke_workdir"):
        candidate = Path(str(ltspice["smoke_workdir"])) / "smoke_rc.raw"
        if candidate.is_file():
            smoke_raw = candidate

    payload: dict[str, Any] = {
        "tool": "boardmodeler",
        "version": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "executable": sys.executable,
        "config": {"path": str(cfg_path), "exists": cfg_path.is_file()},
        "ltspice": ltspice,
        "reader_backend": _reader_section(smoke_raw),
        "ocr": _ocr_section(),
        "credentials": _credentials_section(),
        "telemetry": "none",
    }
    payload["ok"] = bool(ltspice.get("found")) and (ltspice.get("smoke_test") in ("pass", None))
    return payload


def _render_doctor_human(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"boardmodeler {payload['version']} on python {payload['python']} ({payload['platform']})"
    )
    config = payload["config"]
    lines.append(f"config: {config['path']} ({'found' if config['exists'] else 'not present'})")

    ltspice = payload["ltspice"]
    if ltspice["found"]:
        lines.append(
            f"ltspice: {ltspice['path']} (version {ltspice['version']}, via {ltspice['source']})"
        )
        lines.append(f"  lib dir: {ltspice['lib_dir']}")
        if ltspice.get("smoke_test") is None:
            lines.append(f"  smoke test: skipped ({ltspice.get('smoke_detail')})")
        else:
            lines.append(f"  smoke test: {ltspice['smoke_test']}")
            lines.append(f"  {ltspice.get('smoke_detail')}")
    else:
        lines.append("ltspice: NOT FOUND")
        lines.append(f"  probed: {ltspice.get('smoke_detail')}")

    backend = payload["reader_backend"]
    lines.append(
        f"reader backend: {backend['reader_backend']} (spicelib={backend['spicelib_version']}, "
        f"max deviation={backend['max_deviation']})"
    )
    lines.append(f"  {backend['detail']}")

    ocr = payload["ocr"]
    lines.append(
        f"ocr: {'available' if ocr.get('available') else 'unavailable'}"
        f"{' - ' + str(ocr.get('reason')) if ocr.get('reason') else ''}"
    )
    lines.append(f"  {ocr.get('detail')}")

    creds = payload["credentials"]
    lines.append("credentials: " + ", ".join(f"{k}={v}" for k, v in creds.items()))
    lines.append("telemetry: none")
    return "\n".join(lines)


def _cmd_run_tests(args: argparse.Namespace) -> int:
    from boardmodeler.domain.enums import Status
    from boardmodeler.pipeline.project import Project, ProjectError
    from boardmodeler.pipeline.runner import run_deck_tests
    from boardmodeler.simulation.ltspice import locate_outcome
    from boardmodeler.verification.engine import evaluate_case

    try:
        project = Project(args.project)
    except ProjectError as exc:
        print(f"error: {exc}")
        return 2

    cases = project.tests(scope=args.scope, test_ids=args.test_ids)
    if args.list_tests:
        payload = [
            {
                "test_id": case.test_id,
                "scenario_id": case.scenario_id,
                "scope": case.scope,
                "expected": case.expected.kind,
                "requirement_ids": case.requirement_ids,
                "deck_template": case.deck_template,
            }
            for case in cases
        ]
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for row in payload:
                print(
                    f"{row['test_id']:32} {row['scenario_id']:28} {row['scope']:20} "
                    f"{row['expected']:16} {len(row['requirement_ids'])} requirement(s)"
                )
            print(f"{len(cases)} case(s)")
        return 0

    if not cases:
        print(
            "error: no test cases matched"
            + (f" scope={args.scope!r}" if args.scope else "")
            + (f" test_ids={args.test_ids}" if args.test_ids else "")
            + f"; the project has {len(project.read_tests_file())} case(s)"
        )
        return 1

    requirements = project.requirements()
    if not requirements:
        print(
            "warning: the project has no requirements, so every case will be UNKNOWN "
            f"({project.requirements_path} is missing or empty)"
        )

    outcome = locate_outcome(args.ltspice)
    install = outcome.install
    if install is None:
        print(
            "error: LTspice was not found, so no case can run "
            f"(reason={outcome.reason}); probed: {', '.join(outcome.probed_paths)}"
        )
        return 2

    ctx = project.run_context(
        ltspice=install, timeout_s=args.timeout or 120.0, ascii_raw=args.ascii_raw
    )
    artifacts = run_deck_tests(ctx, cases)

    results = []
    summary: dict[str, int] = {}
    for case, artifact in zip(cases, artifacts, strict=True):
        cap_gate = _capability_gate(project, case)
        result = evaluate_case(
            case,
            artifact,
            requirements,
            supply_domains=project.config.supply_domains,
            capability_gate=cap_gate,
        )
        summary[result.status.value] = summary.get(result.status.value, 0) + 1
        results.append(
            {
                "result": json.loads(result.model_dump_json()),
                "run": {
                    "run_id": artifact.run_id,
                    "run_dir": str(artifact.run_dir),
                    "deck_sha256": artifact.deck_sha256,
                    "raw_sha256": artifact.raw_sha256,
                    "log_sha256": artifact.log_sha256,
                    "observed": artifact.detail,
                    "usable": artifact.usability.usable,
                    "blocked_reason": artifact.blocked_reason,
                },
            }
        )

    payload = {
        "tool": "boardmodeler",
        "command": "run tests",
        "project": str(project.root),
        "project_id": project.config.project_id,
        "ltspice": {"path": str(install.path), "version": ltspice_version(install.path)},
        "requirements": len(requirements),
        "summary": summary,
        "results": results,
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for row in results:
            result = row["result"]
            print(f"{result['status']:16} {result['test_id']:32} {result['detail'][:110]}")
        print(f"summary: {summary}  ({len(results)} case(s))")
        if args.out:
            print(f"results written to {args.out}")

    if args.strict and any(r["result"]["status"] != Status.PASS.value for r in results):
        return 1
    return 0


def _capability_gate(project: object, case: object) -> dict[str, str] | None:
    """Capability gate for a project, built from its declared capabilities.

    Reads ``models/capabilities/*.json`` (``ModelCapability`` records) and
    ``evidence/capability_map.json`` (``{requirement_id: behavior}``). A project
    without either file has no capability declaration, so nothing is gated.
    """
    from boardmodeler.domain.records import ModelCapability
    from boardmodeler.verification.engine import gate_from_capability

    root = Path(str(getattr(project, "root", ".")))
    caps_dir = root / "models" / "capabilities"
    behavior_map_file = root / "evidence" / "capability_map.json"
    if not caps_dir.is_dir() or not behavior_map_file.is_file():
        return None

    capabilities = [
        ModelCapability.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(caps_dir.glob("*.json"))
    ]
    raw_map = json.loads(behavior_map_file.read_text(encoding="utf-8"))
    requirement_behaviors = {str(k): str(v) for k, v in raw_map.items()}
    gate = gate_from_capability(capabilities, requirement_behaviors)
    return gate or None


def _cmd_demo_build(args: argparse.Namespace) -> int:
    from boardmodeler.pipeline.demo import build_demo_project
    from boardmodeler.simulation.ltspice import locate_outcome

    install = locate_outcome().install
    if install is None:
        print("error: LTspice was not found, so the demo cannot be built and probed")
        return 2
    result = build_demo_project(args.out, ltspice=install, workdir=Path(args.out) / "probe_project")
    payload = {
        "tool": "boardmodeler",
        "command": "demo build",
        "project": str(result.project_dir),
        "detail": result.detail,
        "files": result.files,
        "decks": sorted(result.decks),
        "tests": [case.test_id for case in result.tests],
        "static_findings": [
            {"code": f.code, "status": f.status.value, "refdes": f.refdes, "message": f.message}
            for f in result.static_findings
        ],
        "capabilities": {mid: cap.behaviors for mid, cap in result.capabilities.items()},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"built {result.project_dir}")
        print(f"  {result.detail}")
        print(f"  files: {len(result.files)}")
        print(f"  decks: {', '.join(sorted(result.decks))}")
        failing = [f for f in result.static_findings if f.status.value == "FAIL"]
        print(f"  static checks: {len(result.static_findings)} findings, {len(failing)} failing")
        for finding in failing:
            print(f"    {finding.code} {finding.refdes or ''} {finding.message[:90]}")
    return 0


def _cmd_circuit_check(args: argparse.Namespace) -> int:
    from boardmodeler.domain.enums import Status
    from boardmodeler.pipeline.demo import check_circuit, run_fault_matrix
    from boardmodeler.simulation.ltspice import locate_outcome

    install = locate_outcome().install
    if install is None:
        print("error: LTspice was not found, so no dynamic check can run")
        return 2
    out = args.out or Path(args.project) / "results.json"
    report = args.report or Path(args.project) / "report.html"
    result = check_circuit(
        args.project,
        circuit_path=args.circuit,
        ltspice=install,
        scope=args.scope,
        report_path=report,
        results_path=out,
    )
    fault_matrix = None
    if args.fault_matrix:
        fault_matrix = run_fault_matrix(args.project, ltspice=install)
    payload = {
        "tool": "boardmodeler",
        "command": "circuit check",
        "project": str(result.project_dir),
        "status": result.status.value,
        "summary": result.summary(),
        "coverage": result.coverage,
        "findings": [json.loads(f.model_dump_json()) for f in result.findings],
        "results": [json.loads(r.model_dump_json()) for r in result.results],
        "results_path": str(out),
        "report_path": str(report),
        "fault_matrix": fault_matrix,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"status: {result.status.value}   {result.summary()}")
        for finding in result.findings:
            if finding.status is not Status.PASS:
                print(f"  {finding.status.value:12} {finding.code:32} {finding.message[:90]}")
        for test_result in result.results:
            print(
                f"  {test_result.status.value:12} {test_result.test_id:28} {test_result.detail[:80]}"
            )
        print(f"results -> {out}")
        print(f"report  -> {report}")
        if fault_matrix:
            print(
                f"fault matrix: {fault_matrix['detected']}/{fault_matrix['total']} detected, "
                f"original unchanged: {fault_matrix['original_unchanged']}"
            )
    if args.strict and result.status is not Status.PASS:
        return 1
    return 0


def _cmd_run_mutations(args: argparse.Namespace) -> int:
    from boardmodeler.pipeline.demo import run_fault_matrix
    from boardmodeler.simulation.ltspice import locate_outcome

    install = locate_outcome().install
    if install is None:
        print("error: LTspice was not found, so faults cannot be exercised")
        return 2
    report = run_fault_matrix(
        args.project,
        out_dir=args.report.parent / "fault_matrix",
        ltspice=install,
        faults=args.fault,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for entry in report["faults"]:
            mark = "detected" if entry["detected"] else "NOT DETECTED"
            print(f"  {entry['fault_id']:22} {mark:14} {entry['expected_detection']}")
        print(f"{report['detected']}/{report['total']} faults detected")
        print(f"original project unchanged: {report['original_unchanged']}")
        print(f"report -> {args.report}")
    return 0 if report["detected"] == report["total"] else 1


def _cmd_export(args: argparse.Namespace) -> int:
    from boardmodeler.pipeline.project import Project, ProjectError
    from boardmodeler.reporting.export import export_project

    try:
        project = Project(args.project)
    except ProjectError as exc:
        print(f"error: {exc}")
        return 2
    result = export_project(project, args.out, model_id=args.model)
    payload = {
        "tool": "boardmodeler",
        "command": "export",
        "out": str(result.out_dir),
        "files": result.relative_files(),
        "findings": [json.loads(f.model_dump_json()) for f in result.findings],
        "ok": result.ok,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"exported {len(result.files)} file(s) to {result.out_dir}")
        for name in result.relative_files():
            print(f"  {name}")
        for finding in result.findings:
            print(f"  {finding.status.value:10} {finding.code}: {finding.message[:90]}")
    return 0


def _cmd_model(args: argparse.Namespace) -> int:
    action = getattr(args, "model_command", None)
    if action == "build":
        return _cmd_model_build(args)
    if action == "test":
        return _cmd_model_test(args)
    if action == "install":
        return _cmd_model_install(args)
    print("error: specify a model subcommand: build, test or install")
    return 2


def _spec_json_in(out_dir: Path) -> Path:
    for candidate in (
        out_dir / "spec" / "characteristics.json",
        out_dir / "build" / "spec" / "characteristics.json",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"no spec/characteristics.json under {out_dir}; build the model first with 'model build'"
    )


def _model_lib_in(out_dir: Path, subckt: str) -> Path:
    for candidate in (
        out_dir / f"{subckt}.lib",
        out_dir / "model" / f"{subckt}.lib",
        out_dir / "build" / "model" / f"{subckt}.lib",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no {subckt}.lib found under {out_dir}")


def _publish_model_files(
    *, out_dir: Path, workdir: Path, subckt: str
) -> tuple[Path, Path, list[str]]:
    """Copy the agent's model into the output directory, and guarantee a valid symbol."""
    from boardmodeler.authoring.card import write_symbol_for
    from boardmodeler.models.library import subckt_ports
    from boardmodeler.models.symbolism import validate_symbol

    notes: list[str] = []
    source_lib = None
    source_asy = None
    for directory in (workdir / "model", workdir):
        if source_lib is None and (directory / f"{subckt}.lib").is_file():
            source_lib = directory / f"{subckt}.lib"
        if source_asy is None and (directory / f"{subckt}.asy").is_file():
            source_asy = directory / f"{subckt}.asy"
    if source_lib is None:
        raise FileNotFoundError(f"the agent left no {subckt}.lib in {workdir}")

    lib_text = source_lib.read_text(encoding="utf-8", errors="replace")
    ports = list(subckt_ports(lib_text, subckt))
    if not ports:
        raise ValueError(f"{source_lib} declares no .subckt {subckt}")
    lib_target = out_dir / f"{subckt}.lib"
    lib_target.write_text(lib_text, encoding="utf-8", newline="\n")

    asy_target = out_dir / f"{subckt}.asy"
    if source_asy is not None:
        asy_text = source_asy.read_text(encoding="utf-8", errors="replace")
        findings = validate_symbol(asy_text, ports=ports, model_file=lib_target.name)
        if not findings:
            asy_target.write_text(asy_text, encoding="utf-8", newline="\n")
            return lib_target, asy_target, notes
        notes.append(
            "the agent's symbol was rejected ("
            + "; ".join(f"{f.code}" for f in findings)
            + "); generated one instead"
        )
    write_symbol_for(
        out_path=asy_target,
        name=subckt,
        ports=ports,
        model_file=lib_target.name,
        model_name=subckt,
        description=f"{subckt} generated model",
    )
    if not notes:
        notes.append("symbol generated from the model's declared ports")
    return lib_target, asy_target, notes


def _cmd_model_build(args: argparse.Namespace) -> int:
    from boardmodeler.authoring.backends import build_agent_backend
    from boardmodeler.authoring.card import write_deliverables
    from boardmodeler.authoring.loop import BuildRequest, build_model, prepare_workdir
    from boardmodeler.authoring.spec import load_tps54320_spec
    from boardmodeler.simulation.ltspice import locate

    install = locate()
    out_dir: Path = args.out

    def emit(payload: dict[str, Any], code: int) -> int:
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"model build: {payload['status']} — {payload['detail']}")
            for line in payload.get("history", []):
                print(f"  {line}")
            for row in payload.get("rows", []):
                print(
                    f"  {row['status']:14} {row['req_id']:28} {row['required'][:28]:28} "
                    f"{str(row['measured'])[:28]}"
                )
            for probe in payload.get("probes", []):
                print(f"  {probe['status']:8} {probe['probe_id']:20} {probe['detail'][:80]}")
            for path in payload.get("files", []):
                print(f"  wrote {path}")
        return code

    subckt = args.subckt or _sanitize_subckt(args.part)
    part_class = classify(args.part)
    if not part_class.supported:
        # The same refusal the pipeline raises, reported before it reads anything:
        # the probes judge analogue rows, and a part this classifier names is one
        # whose datasheet rows no probe can bind. No new flag -- it is a BLOCKED
        # result like any other, and --strict turns that into exit 1.
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "part": args.part,
                "subckt": subckt,
                "status": "BLOCKED",
                "detail": part_class.detail,
                "history": [],
                "probes": [],
                "files": [],
            },
            1 if args.strict else 0,
        )
    if args.datasheet is not None:
        return _cmd_model_build_from_datasheet(args, subckt=subckt, emit=emit)
    if args.requirements is None or args.bindings is None:
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": "give --datasheet, or both --requirements and --bindings",
                "history": [],
                "probes": [],
                "files": [],
            },
            2,
        )
    try:
        spec = load_tps54320_spec(args.requirements, args.bindings, part=args.part, subckt=subckt)
    except (OSError, ValueError) as exc:
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": f"spec could not be loaded: {exc}",
                "history": [],
                "probes": [],
                "files": [],
            },
            1,
        )
    if install is None:
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": "LTspice was not found; run 'boardmodeler doctor' or set LTSPICE_EXE",
                "history": [],
                "probes": [],
                "files": [],
            },
            1,
        )

    workdir = out_dir / "build"
    prepare_workdir(spec=spec, subckt=subckt, workdir=workdir)
    backend_name = str(args.backend or "bob").strip().lower()
    if backend_name in ("api", "bob"):
        backend = build_agent_backend(provider_id=args.provider, team_id=args.team_id)
    else:
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": (
                    f"{backend_name}_backend_unavailable: the offline author writes the bundled "
                    "template only on the --datasheet path; use --backend bob here"
                ),
                "history": [],
                "probes": [],
                "files": [],
            },
            1,
        )
    request = BuildRequest(
        part=args.part,
        subckt=subckt,
        spec=spec,
        workdir=workdir,
        ltspice=install.path,
        backend=backend,
        max_iterations=args.iterations,
        timeout_s=args.timeout,
    )
    outcome = build_model(request)

    files: list[str] = []
    notes: list[str] = []
    if outcome.report.model_sha256:
        try:
            lib, asy, symbol_notes = _publish_model_files(
                out_dir=out_dir, workdir=workdir, subckt=subckt
            )
            files += [str(lib), str(asy)]
            notes += symbol_notes
        except (OSError, ValueError) as exc:
            notes.append(f"model files not published: {exc}")

    written = write_deliverables(
        out_dir=out_dir,
        part=args.part,
        subckt=subckt,
        spec=spec,
        report=outcome.report,
        document=spec.doc_id,
        backend=backend.name,
        iterations=outcome.iterations,
    )
    files += [str(path) for path in written]
    report_path = out_dir / "harness-report.json"
    report_path.write_text(outcome.report.to_json(), encoding="utf-8", newline="\n")
    files.append(str(report_path))

    payload = {
        "tool": "boardmodeler",
        "command": "model build",
        "part": args.part,
        "subckt": subckt,
        "status": outcome.status,
        "detail": outcome.detail + (f"; {'; '.join(notes)}" if notes else ""),
        "iterations": outcome.iterations,
        "counts": outcome.report.counts(),
        "probes": [outcome_probe.to_json() for outcome_probe in outcome.report.outcomes],
        "history": list(outcome.history),
        "files": files,
        "card": str(out_dir / "MODEL_CARD.md"),
    }
    return emit(payload, 1 if (args.strict and outcome.status != "PASS") else 0)


def _sanitize_subckt(part: str) -> str:
    """A SPICE-legal subcircuit name derived from the part number."""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in part.strip())
    return cleaned.upper() or "MODEL"


def _cmd_model_build_from_datasheet(args: argparse.Namespace, *, subckt: str, emit: Any) -> int:
    """The product path: datasheet in, agent-authored and simulator-judged model out."""
    from boardmodeler.pipeline.make_model import MakeModelRequest, make_model

    if not args.datasheet.is_file():
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": f"datasheet not found: {args.datasheet}",
                "history": [],
                "probes": [],
                "files": [],
            },
            1,
        )
    supplied = (args.requirements, args.bindings)
    if any(supplied) and not all(supplied):
        # A reviewed extraction result is a pair; half of it would silently fall
        # back to asking a provider for the rows the user already has.
        return emit(
            {
                "tool": "boardmodeler",
                "command": "model build",
                "status": "BLOCKED",
                "detail": "give --datasheet, or both --requirements and --bindings",
                "history": [],
                "probes": [],
                "files": [],
            },
            2,
        )
    request = MakeModelRequest(
        part=args.part,
        subckt=subckt,
        datasheet=args.datasheet,
        out_dir=args.out,
        backend_name=args.backend,
        provider=args.provider,
        team_id=args.team_id,
        max_iterations=args.iterations,
        timeout_s=args.timeout,
        allow_remote=args.allow_remote,
        reinforce=False if args.no_reinforce else None,
        requirements_json=args.requirements,
        bindings_json=args.bindings,
    )
    quiet = args.json

    def on_stage(event: Any) -> None:
        if not quiet:
            counts = f" {event.counts}" if getattr(event, "counts", None) else ""
            print(f"  {event.stage:8} {event.status:8} {event.detail}{counts}"[:160], flush=True)

    result = make_model(request, progress=on_stage)
    payload = json.loads(result.to_json())
    payload.update({"tool": "boardmodeler", "command": "model build"})
    payload["rows"] = [
        {
            "req_id": row.req_id,
            "required": row.required,
            "measured": row.measured,
            "status": row.status,
            "page": row.page,
        }
        for row in result.rows
    ]
    return emit(payload, 1 if (args.strict and result.status != "PASS") else 0)


def _cmd_model_test(args: argparse.Namespace) -> int:
    from boardmodeler.authoring.card import write_deliverables
    from boardmodeler.authoring.harness import run_harness
    from boardmodeler.authoring.spec import SpecSet
    from boardmodeler.simulation.ltspice import locate

    out_dir: Path = args.out
    install = locate()
    if install is None:
        payload = {
            "tool": "boardmodeler",
            "command": "model test",
            "status": "BLOCKED",
            "detail": "LTspice was not found; run 'boardmodeler doctor' or set LTSPICE_EXE",
        }
        print(json.dumps(payload, indent=2) if args.json else f"model test: {payload['detail']}")
        return 1

    try:
        spec_json = _spec_json_in(out_dir)
        spec = SpecSet.from_json(spec_json.read_text(encoding="utf-8"))
        lib = _model_lib_in(out_dir, spec.subckt)
    except (OSError, ValueError) as exc:
        payload = {
            "tool": "boardmodeler",
            "command": "model test",
            "status": "BLOCKED",
            "detail": str(exc),
        }
        print(json.dumps(payload, indent=2) if args.json else f"model test: {exc}")
        return 1

    report = run_harness(
        model_lib=lib,
        subckt=spec.subckt,
        spec=spec,
        workdir=out_dir / "harness",
        ltspice=install.path,
        timeout_s=args.timeout,
    )
    report_path = out_dir / "harness-report.json"
    report_path.write_text(report.to_json(), encoding="utf-8", newline="\n")
    written = write_deliverables(
        out_dir=out_dir,
        part=spec.part,
        subckt=spec.subckt,
        spec=spec,
        report=report,
        document=spec.doc_id,
    )
    status = "PASS" if report.passed() else "UNKNOWN"
    payload = {
        "tool": "boardmodeler",
        "command": "model test",
        "part": spec.part,
        "subckt": spec.subckt,
        "status": status,
        "counts": report.counts(),
        "probes": [outcome.to_json() for outcome in report.outcomes],
        "files": [str(report_path), *(str(path) for path in written)],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"model test: {status} {payload['counts']} (model {report.model_sha256[:12]})")
        for outcome in report.outcomes:
            print(f"  {outcome.status:8} {outcome.probe_id:20} {outcome.detail[:80]}")
    return 1 if (args.strict and status != "PASS") else 0


def _cmd_model_install(args: argparse.Namespace) -> int:
    from boardmodeler.authoring.card import plan_install
    from boardmodeler.authoring.spec import SpecSet

    out_dir: Path = args.out
    try:
        spec = SpecSet.from_json(_spec_json_in(out_dir).read_text(encoding="utf-8"))
        lib = _model_lib_in(out_dir, spec.subckt)
    except (OSError, ValueError) as exc:
        payload = {
            "tool": "boardmodeler",
            "command": "model install",
            "status": "BLOCKED",
            "detail": str(exc),
        }
        print(json.dumps(payload, indent=2) if args.json else f"model install: {exc}")
        return 1
    asy = out_dir / f"{spec.subckt}.asy"
    if not asy.is_file():
        payload = {
            "tool": "boardmodeler",
            "command": "model install",
            "status": "BLOCKED",
            "detail": f"no symbol at {asy}; run 'model test' to regenerate it",
        }
        print(json.dumps(payload, indent=2) if args.json else payload["detail"])
        return 1

    if args.into is None and not args.user_lib and not args.apply:
        args.apply = False
    plan = plan_install(
        part=spec.part,
        subckt=spec.subckt,
        lib=lib,
        asy=asy,
        into=args.into,
        user_lib=args.user_lib,
        apply=args.apply,
    )
    payload = {
        "tool": "boardmodeler",
        "command": "model install",
        "part": plan.part,
        "copied": [str(path) for path in plan.copied],
        "lib_target": str(plan.lib_target) if plan.lib_target else None,
        "asy_target": str(plan.asy_target) if plan.asy_target else None,
        "steps": list(plan.steps),
        "detail": plan.detail,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"model install: {plan.detail}")
        for step in plan.steps:
            print(f"  {step}")
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from boardmodeler.pipeline.project import Project, ProjectError

    try:
        project = Project(args.project)
    except ProjectError as exc:
        print(f"error: {exc}")
        return 2
    try:
        from boardmodeler.providers.registry import select_provider
        from boardmodeler.requirements.extract import extract_requirements
    except ImportError as exc:
        print(f"error: the extraction layer is unavailable: {exc}")
        return 2

    from boardmodeler.config import load_config

    config = load_config()
    if args.allow_remote:
        config.data_policy.allow_remote = True
    if args.doc is not None:
        from boardmodeler.documents.store import DocumentStore

        store = DocumentStore(project.root)
        record = store.add_file(
            Path(args.doc),
            doc_type="datasheet",
            provenance="user_supplied",
            remote_inference_allowed=bool(args.allow_remote),
        )
        print(f"ingested {record.doc_id} ({record.page_count} pages, {record.text_extraction})")
    selection = select_provider(
        config,
        requested=args.provider,
        allow_bob_shell=False,
        fixture_dir=str(project.root / "evidence" / "cache"),
    )
    result = extract_requirements(project, provider=selection.provider)
    payload = {
        "tool": "boardmodeler",
        "command": "extract",
        "project": str(project.root),
        "provider": {"name": selection.detail, "kind": selection.kind.value},
        "detail": result.detail,
        "requirements": len(result.requirements),
        "pins": len(result.pins),
        "cache_hits": result.cache_hits,
        "issues": [issue.code for issue in result.issues],
        "disclosures": [json.loads(d.model_dump_json()) for d in result.disclosures],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"provider: {selection.detail}")
        print(f"  requirements: {len(result.requirements)}  pins: {len(result.pins)}")
        print(f"  cache hits: {result.cache_hits}  issues: {len(result.issues)}")
        print(f"  {result.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "version"):
        if args.command == "version" and getattr(args, "json", False):
            print(json.dumps({"tool": "boardmodeler", "version": __version__}, indent=2))
        else:
            print(f"boardmodeler {__version__}")
        return 0

    if args.command == "model":
        return _cmd_model(args)

    if args.command == "doctor":
        payload = doctor_payload(
            run_smoke=not getattr(args, "no_smoke", False),
            smoke_workdir=getattr(args, "smoke_workdir", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print(_render_doctor_human(payload))
        return 0

    if args.command == "ui":
        from boardmodeler.ui.app import main as ui_main

        forwarded: list[str] = []
        if args.project is not None:
            forwarded += ["--project", str(args.project)]
        if getattr(args, "installer", False):
            forwarded.append("--installer")
        return ui_main(forwarded)

    if args.command == "setup":
        from boardmodeler.ui.setup_dialog import main as setup_main

        return setup_main(["--json"] if getattr(args, "json", False) else [])

    if args.command == "run" and args.run_command == "tests":
        return _cmd_run_tests(args)

    if args.command == "run" and args.run_command == "mutations":
        return _cmd_run_mutations(args)

    if args.command == "demo" and args.demo_command == "build":
        return _cmd_demo_build(args)

    if args.command == "circuit" and args.circuit_command == "check":
        return _cmd_circuit_check(args)

    if args.command == "export":
        return _cmd_export(args)

    if args.command == "extract":
        return _cmd_extract(args)

    parser.error(f"unhandled command {args.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
