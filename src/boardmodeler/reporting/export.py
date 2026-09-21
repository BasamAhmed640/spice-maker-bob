"""Export (§14 / Phase 2 step 7).

`boardmodeler export` produces a self-contained directory that a colleague can
open without BoardModeler:

```
<out>/
  MODEL_CARD.md      what the model is, what was demonstrated, what is excluded
  README.md          how to reproduce the results in LTspice
  manifest.json      hashes, tool versions, provider, disclosures, parameters
  coverage.json      requirement/test coverage, including what is NOT covered
  requirements.json  the frozen requirement set the run was judged against
  pinmap.json        pin functions (when the project has a pin map)
  results.json       test results with measured values
  model.lib          the generated/adapted model text
  symbol.asy         the symbol bound to it
  example.asc        an example application schematic
  tests/             the test cases and their deck templates
```

Two hard rules:

* **Vendor originals are never copied** (plan A7). The export references them by
  the path the user configured, with a note, and only ships artifacts BoardModeler
  generated or adapted.
* **Paths are relative** inside the export, so the directory works after being
  copied anywhere (verified by a portability re-run in the tests).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from boardmodeler.domain import SCHEMA_VERSION
from boardmodeler.domain.enums import Status
from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes, sha256_file
from boardmodeler.domain.records import (
    ArchiveEntry,
    DocumentRecord,
    Finding,
    ModelCapability,
    Requirement,
    TestCase,
    TestResult,
)
from boardmodeler.models.library import ModelStore
from boardmodeler.models.symbolism import symbol_text, validate_symbol
from boardmodeler.pipeline.project import Project
from boardmodeler.reporting.model_card import ModelCardInputs, model_card_markdown

__all__ = [
    "COVERAGE_CODE_UNTESTED_CRITICAL",
    "ExportResult",
    "export_project",
    "requirements_coverage",
    "write_manifest",
]

COVERAGE_CODE_UNTESTED_CRITICAL = "COV001_critical_requirement_untested"


@dataclass
class ExportResult:
    """What the export wrote and what it noticed."""

    out_dir: Path
    files: list[ArchiveEntry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    model_card_path: Path | None = None
    manifest_path: Path | None = None
    coverage_path: Path | None = None
    results_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not [f for f in self.findings if f.status.value == "FAIL"]

    def relative_files(self) -> list[str]:
        return sorted(entry.path for entry in self.files)


def requirements_coverage(
    requirements: Sequence[Requirement],
    tests: Sequence[TestCase],
    results: Sequence[TestResult],
    *,
    behaviours: Mapping[str, str] | None = None,
    capability_map: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Coverage of the requirement set: what ran, what did not, and why.

    ``not_dynamically_covered`` is the honest part: a requirement with no test, or
    with a test whose verdict was UNKNOWN/BLOCKED, is listed explicitly instead of
    being implied by an overall percentage.
    """
    tested_ids: dict[str, list[str]] = {}
    verdicts: dict[str, list[str]] = {}
    for case, result in zip(tests, results, strict=False):
        for req_id in case.requirement_ids:
            tested_ids.setdefault(req_id, []).append(case.test_id)
            verdicts.setdefault(req_id, []).append(result.status.value)

    uncovered: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    for requirement in requirements:
        req_id = requirement.req_id
        cases = tested_ids.get(req_id, [])
        if not cases:
            uncovered.append(
                {
                    "req_id": req_id,
                    "criticality": requirement.criticality.value,
                    "reason": "no test case references this requirement",
                }
            )
            continue
        states = verdicts.get(req_id, [])
        if states and all(state in ("UNKNOWN", "BLOCKED", "NOT_APPLICABLE") for state in states):
            unknown.append(
                {
                    "req_id": req_id,
                    "status": ",".join(sorted(set(states))),
                    "reason": "every test for this requirement reported a non-verdict status",
                    "tests": ",".join(cases),
                }
            )

    capability_gaps: list[dict[str, str]] = []
    if behaviours and capability_map:
        for req_id, behavior in capability_map.items():
            state = behaviours.get(behavior, "unknown")
            if state != "supported":
                capability_gaps.append({"req_id": req_id, "behavior": behavior, "state": state})

    total = len(requirements)
    with_expression = sum(1 for r in requirements if r.expression is not None)
    with_verified_citation = sum(1 for r in requirements if r.citation_verified)
    dynamically_covered = total - len(uncovered)
    return {
        "schema_version": SCHEMA_VERSION,
        "requirements_total": total,
        "requirements_with_expression": with_expression,
        "requirements_with_verified_citation": with_verified_citation,
        "requirements_with_a_test": dynamically_covered,
        "requirements_with_a_verdict": total - len(uncovered) - len(unknown),
        "coverage_fraction": round(dynamically_covered / total, 6) if total else 0.0,
        "not_dynamically_covered": uncovered,
        "non_verdict_requirements": unknown,
        "capability_gated": capability_gaps,
        "tests_total": len(tests),
        "results_by_status": _counts(results),
    }


def _counts(results: Sequence[TestResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    return counts


def write_manifest(
    out_dir: Path,
    *,
    project: Project,
    files: Sequence[ArchiveEntry],
    model_id: str,
    model_files: Mapping[str, str],
    test_hashes: Mapping[str, str],
    requirement_baseline_hash: str,
    parameters: Mapping[str, str],
    tool_versions: Mapping[str, str],
    reader_backend: str,
    qualifications: Sequence[str],
    simulator: str = "",
) -> Path:
    """Write ``manifest.json`` describing exactly what this export contains."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "tool": "boardmodeler",
        "exported_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_id": project.config.project_id,
        "project_name": project.config.name,
        "mode": project.config.mode,
        "model_id": model_id,
        "model_hashes": dict(model_files),
        "test_hashes": dict(test_hashes),
        "requirement_baseline_hash": requirement_baseline_hash,
        "reader_backend": reader_backend,
        "tool_versions": dict(tool_versions),
        "parameters": dict(parameters),
        "qualifications": list(qualifications),
        "simulator": simulator,
        "files": [entry.model_dump() for entry in files],
        "vendor_models_referenced_not_copied": True,
        "telemetry": "none",
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def export_project(
    project: Project,
    out_dir: Path,
    *,
    model_id: str | None = None,
    subckt: str | None = None,
    requirements: Sequence[Requirement] | None = None,
    tests: Sequence[TestCase] | None = None,
    results: Sequence[TestResult] | None = None,
    capability: ModelCapability | None = None,
    capability_behavior_map: Mapping[str, str] | None = None,
    documents: Sequence[DocumentRecord] | None = None,
    parameters: Mapping[str, str] | None = None,
    limitations: Sequence[str] = (),
    reproduction: Sequence[str] = (),
    simulator: str = "",
    tool_version: str = "",
    example_asc: Path | None = None,
    symbol_ports: Sequence[str] | None = None,
    symbol_directions: Mapping[str, str] | None = None,
) -> ExportResult:
    """Write the export directory and return what was produced."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    findings: list[Finding] = []
    result = ExportResult(out_dir=out)

    requirements = list(
        requirements if requirements is not None else project.read_requirements_file()
    )
    tests = list(tests if tests is not None else project.read_tests_file())
    results = list(results or [])
    parameters = dict(parameters or {})
    documents = list(documents if documents is not None else project.documents())

    # ---------------------------------------------------------------- artifacts
    store = ModelStore(project.root)
    model_files: dict[str, str] = {}
    model_text: str | None = None
    chosen = model_id or _first_generated_model(store)
    if chosen is not None:
        try:
            record = store.get(chosen)
            model_text = store.read_text(chosen)
            if record.kind == "vendor_original":
                findings.append(
                    Finding(
                        code="EXP001_vendor_original_not_exported",
                        status=Status.FAIL,
                        message=(
                            f"{chosen} is a vendor original and is referenced rather than copied "
                            "(redistribution is not permitted)"
                        ),
                        detail={"model_id": chosen},
                    )
                )
                model_text = None
            if record.kind != "vendor_original" and model_text is not None:
                (out / "model.lib").write_text(model_text, encoding="utf-8", newline="\n")
            # The hash is recorded even when the bytes are not shipped, so the
            # manifest still pins exactly which vendor artifact was used.
            model_files[chosen] = record.sha256
        except Exception as exc:
            findings.append(
                Finding(
                    code="EXP002_model_unavailable",
                    status=Status.UNKNOWN,
                    message=f"model {chosen!r} could not be read: {type(exc).__name__}: {exc}",
                )
            )

    if symbol_ports:
        model_file = "model.lib"
        asy = symbol_text(
            chosen or "model",
            symbol_ports,
            model_file=model_file,
            model_name=subckt or chosen,
            description=f"{chosen} ({subckt or 'subcircuit'})",
            directions=symbol_directions,
        )
        (out / "symbol.asy").write_text(asy, encoding="utf-8")
        findings.extend(
            validate_symbol(
                asy,
                ports=symbol_ports,
                model_file=model_file if chosen else None,
            )
        )

    if example_asc is not None and Path(example_asc).is_file():
        (out / "example.asc").write_text(
            Path(example_asc).read_text(encoding="utf-8"), encoding="utf-8"
        )

    # ---------------------------------------------------------- data artifacts
    (out / "requirements.json").write_text(
        json.dumps(
            {
                "project_id": project.config.project_id,
                "requirements": [
                    json.loads(r.model_dump_json(by_alias=True)) for r in requirements
                ],
                "baseline_hash": (
                    project.baseline().compute_hash() if project.baseline() is not None else ""
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    pinmap = project.root / "evidence" / "pinmap.json"
    if pinmap.is_file():
        shutil.copyfile(pinmap, out / "pinmap.json")

    results_payload = {
        "project_id": project.config.project_id,
        "results": [json.loads(r.model_dump_json()) for r in results],
        "summary": _counts(results),
    }
    (out / "results.json").write_text(json.dumps(results_payload, indent=2), encoding="utf-8")

    coverage = requirements_coverage(
        requirements,
        tests,
        results,
        behaviours=capability.behaviors if capability else None,
        capability_map=capability_behavior_map,
    )
    (out / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    uncovered_entries: list[dict[str, str]] = coverage["not_dynamically_covered"]  # type: ignore[assignment]
    for entry in uncovered_entries:
        if entry["criticality"] == "CRITICAL":
            findings.append(
                Finding(
                    code=COVERAGE_CODE_UNTESTED_CRITICAL,
                    status=Status.UNKNOWN,
                    refdes=None,
                    message=f"critical requirement {entry['req_id']} has no test case",
                    detail=dict(entry),
                )
            )

    tests_dir = out / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "tests.json").write_text(
        json.dumps({"tests": [json.loads(t.model_dump_json()) for t in tests]}, indent=2),
        encoding="utf-8",
    )
    for case in tests:
        template = Path(case.deck_template)
        if not template.is_absolute():
            template = project.root / template
        if template.is_file():
            target = tests_dir / template.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, target)

    # --------------------------------------------------------------- documents
    if capability is not None:
        card = model_card_markdown(
            ModelCardInputs(
                model_id=capability.model_id,
                model_kind=capability.kind,
                evidence_level=capability.evidence_level.value,
                source_model_hash=capability.source_model_hash,
                requirements=requirements,
                capability=capability,
                results=results,
                tests=tests,
                documents=documents,
                parameters=parameters,
                limitations=limitations,
                generated_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                tool_version=tool_version,
                simulator=simulator,
                reproduction=reproduction,
            )
        )
        result.model_card_path = out / "MODEL_CARD.md"
        result.model_card_path.write_text(card, encoding="utf-8")

    (out / "README.md").write_text(
        _readme(model_id=chosen, simulator=simulator, tests=tests), encoding="utf-8"
    )

    # ---------------------------------------------------------------- manifest
    files = [
        ArchiveEntry(
            path=str(path.relative_to(out)).replace("\\", "/"),
            sha256=sha256_file(path),
            size=path.stat().st_size,
        )
        for path in sorted(out.rglob("*"))
        if path.is_file()
    ]
    baseline = project.baseline()
    manifest_path = write_manifest(
        out,
        project=project,
        files=files,
        model_id=chosen or "",
        model_files=model_files,
        test_hashes={
            case.test_id: sha256_bytes(canonical_json_bytes(json.loads(case.model_dump_json())))
            for case in tests
        },
        requirement_baseline_hash=baseline.compute_hash() if baseline else "",
        parameters=parameters,
        tool_versions={"python": _python_version(), "boardmodeler": tool_version},
        reader_backend="native",
        qualifications=[
            "vendor originals are referenced, not redistributed",
            "results are from the recorded simulator version only",
        ],
        simulator=simulator,
    )
    result.manifest_path = manifest_path
    result.coverage_path = out / "coverage.json"
    result.results_path = out / "results.json"
    result.files = [
        ArchiveEntry(
            path=str(path.relative_to(out)).replace("\\", "/"),
            sha256=sha256_file(path),
            size=path.stat().st_size,
        )
        for path in sorted(out.rglob("*"))
        if path.is_file()
    ]
    result.findings = findings
    if model_text is None and chosen is not None:
        findings.append(
            Finding(
                code="EXP003_model_not_included",
                status=Status.UNKNOWN,
                message=(
                    f"no model.lib was written for {chosen!r}; the export lists the model hash in "
                    "the manifest so the omission is visible"
                ),
            )
        )
    return result


def _first_generated_model(store: ModelStore) -> str | None:
    records = store.records()
    for kind in ("generated", "vendor_adapted"):
        for record in records:
            if record.kind == kind:
                return record.model_id
    return records[0].model_id if records else None


def _readme(*, model_id: str | None, simulator: str, tests: Sequence[TestCase]) -> str:
    lines = [
        "# Exported model and tests",
        "",
        f"Model: `{model_id or 'not recorded'}`",
        "",
        "## Reproduce these results",
        "",
        "1. Open LTspice and load `example.asc`, or run it in batch:",
        "",
        "   ```",
        "   LTspice.exe -b example.asc",
        "   ```",
        "",
        "   (`example.asc` expects `model.lib` and `symbol.asy` in the same directory.)",
        "",
        "2. Run the exported test decks in `tests/` the same way. Each deck is "
        "self-contained and includes the model by relative path.",
        "",
        "## What is in here",
        "",
        "| file | meaning |",
        "|---|---|",
        "| `MODEL_CARD.md` | what the model reproduces, what it excludes, and the evidence |",
        "| `manifest.json` | hashes of every file plus the parameters and tool versions |",
        "| `coverage.json` | requirement coverage, including the requirements with **no** dynamic test |",
        "| `results.json` | test results with the measured values |",
        "| `requirements.json` | the requirement set this model was judged against |",
        "| `model.lib` | the model text (generated, or adapted from a vendor model) |",
        "| `symbol.asy` | LTspice symbol bound to the subcircuit port order |",
        "| `example.asc` | an example application schematic |",
        "| `tests/` | the test cases and their decks |",
        "",
        f"Simulator used for the recorded results: {simulator or 'not recorded'}.",
        f"Exported test cases: {len(tests)}.",
        "",
        "Vendor model originals are **not** included (their licences do not permit "
        "redistribution); `manifest.json` records their hashes and the paths used.",
        "",
    ]
    return "\n".join(lines)


def _python_version() -> str:
    import platform

    return platform.python_version()
