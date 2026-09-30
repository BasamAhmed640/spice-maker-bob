"""Retest saved candidates without changing their model, symbol, or fixed requirements.

Prior receipts describe a previous observation. They are archived before replacement;
only a newly executed harness and a newly checked pinout describe the current bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from boardmodeler.authoring.card import status_tally, write_deliverables
from boardmodeler.authoring.harness import HarnessReport, judge_characteristic
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.models.library import subckt_ports
from boardmodeler.models.pinout import (
    check_publication,
    freeze_pinout,
    pinout_report,
    resolve_reviewed_profile,
)

_RECEIPTS = (
    "harness-report.json",
    "model-design.json",
    "pinout-report.json",
    "MODEL_CARD.md",
    "template-parameters.json",
    "results.json",
    "qualification-report.json",
    "pin-only-report.json",
    "sanity-report.json",
    "reviewed-extraction.json",
    "spec/pinout-contract.json",
    "spec/characteristics.json",
    "retest-report.json",
)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class Retest:
    directory: Path
    spec: SpecSet
    library_path: Path
    spec_path: Path
    library: bytes
    symbol: bytes | None
    prior: dict[str, bytes]
    pinout: dict[str, Any] | None = None
    problem: str = ""
    notes: list[str] = field(default_factory=list)

    def data(self, name: str) -> dict[str, Any] | None:
        raw = self.prior.get(name)
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"retest_record_invalid: {name} must contain an object")
        return value

    def safe_data(self, name: str) -> dict[str, Any] | None:
        try:
            return self.data(name)
        except ValueError, TypeError:
            return None

    @property
    def pin_only(self) -> bool:
        results = self.safe_data("results.json") or {}
        request = results.get("request")
        return "pin-only-report.json" in self.prior or (
            isinstance(request, dict) and request.get("engine") == "pin_only"
        )

    @property
    def unverified(self) -> set[str]:
        rows = (self.safe_data("results.json") or {}).get("rows", ())
        if not isinstance(rows, (list, tuple)):
            return set()
        return {
            str(row.get("req_id"))
            for row in rows
            if isinstance(row, dict)
            and str(row.get("required", "")).startswith("citation unverified:")
        }

    def unchanged(self) -> bool:
        symbol_path = self.library_path.with_suffix(".asy")
        return (
            self.library_path.read_bytes() == self.library
            and (symbol_path.read_bytes() if symbol_path.is_file() else None) == self.symbol
            and SpecSet.from_json(self.spec_path.read_text(encoding="utf-8")).digest()
            == self.spec.digest()
            and all(
                (self.directory / name).is_file() and (self.directory / name).read_bytes() == raw
                for name, raw in self.prior.items()
            )
        )


def prepare_retest(
    directory: Path, spec: SpecSet, library_path: Path, spec_path: Path | None = None
) -> Retest:
    """Check saved identities before any simulation. No input artifact is rewritten."""
    symbol_path = library_path.with_suffix(".asy")
    run = Retest(
        directory,
        spec,
        library_path,
        spec_path or directory / "spec/characteristics.json",
        library_path.read_bytes(),
        symbol_path.read_bytes() if symbol_path.is_file() else None,
        {
            name: (directory / name).read_bytes()
            for name in _RECEIPTS
            if (directory / name).is_file()
        },
    )
    try:
        results = run.data("results.json") or {}
        if results.get("request") is not None and not isinstance(results["request"], dict):
            raise ValueError("retest_record_invalid: results.request must be an object")
        for key in ("rows", "stages"):
            if key in results and (
                not isinstance(results[key], list)
                or any(not isinstance(item, dict) for item in results[key])
            ):
                raise ValueError(f"retest_record_invalid: results.{key} must be a list of objects")
        design = run.data("model-design.json") or {}
        if design.get("design") is not None and not isinstance(design["design"], dict):
            raise ValueError("retest_record_invalid: model-design.design must be an object")
        expected = []
        for name in ("harness-report.json", "pinout-report.json", "spec/pinout-contract.json"):
            payload = run.data(name)
            if payload is not None and payload.get("spec_digest"):
                expected.append((name, payload["spec_digest"]))
        if isinstance(design.get("design"), dict):
            expected.append(("model-design.json", design["design"].get("spec_digest")))
        if any(digest != spec.digest() for _name, digest in expected):
            raise ValueError(
                "spec_tampered: fixed requirements differ from saved verification/design/pinout identity"
            )
        if not expected:
            run.notes.append(
                "No prior fixed-spec digest is available; this legacy retest establishes a new baseline, not prior provenance."
            )
        saved = run.data("spec/pinout-contract.json")
        prior_pinout = run.data("pinout-report.json")
        unavailable_legacy = prior_pinout is None or (
            not prior_pinout.get("publication_allowed")
            and prior_pinout.get("contract") is None
            and str(prior_pinout.get("reason", "")).startswith("pinout_confirmation_unavailable:")
        )
        if saved is None and unavailable_legacy and not isinstance(design.get("design"), dict):
            run.notes.append(
                "No saved source-confirmed pinout contract is available; package pinout remains unconfirmed."
            )
        else:
            if saved is None:
                raise ValueError(
                    "pinout_contract_missing: prior receipt cannot substitute for the frozen contract"
                )
            source_hash = str(saved.get("document_sha256", ""))
            profile = resolve_reviewed_profile(spec.part, source_hash)
            current = freeze_pinout(profile, spec, source_hash)
            if saved != current.payload():
                raise ValueError(
                    "pinout_contract_changed: saved contract differs from application-owned approval"
                )
            if prior_pinout is not None and (
                prior_pinout.get("contract") != current.payload()
                or prior_pinout.get("contract_sha256") != current.digest()
            ):
                raise ValueError(
                    "pinout_receipt_changed: saved receipt differs from application-owned approval"
                )
            source_path = (results.get("request") or {}).get("datasheet")
            if (
                source_path
                and Path(source_path).is_file()
                and _hash(Path(source_path).read_bytes()) != source_hash
            ):
                raise ValueError("pinout_source_changed: saved datasheet bytes changed")
            if run.symbol is None:
                raise ValueError("pinout_symbol_missing: no delivered symbol to recheck")
            run.pinout = check_publication(
                current,
                spec=spec,
                document_sha256=source_hash,
                library=run.library,
                symbol=run.symbol,
                model_file=library_path.name,
            )
            if not run.pinout["publication_allowed"]:
                raise ValueError(f"pinout_not_confirmed: {run.pinout['reason']}")
        # Do not permit malformed saved state to become a provenance claim later.
        for name in _RECEIPTS:
            if name.endswith(".json"):
                run.data(name)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        run.problem = str(exc)
        run.pinout = pinout_report(
            part=spec.part,
            document_sha256="",
            spec_digest=spec.digest(),
            reason=run.problem,
            library=run.library,
            symbol=run.symbol,
        )
    return run


def saved_design_is_exact(record: Any, delivered: bytes, spec: SpecSet, *, unverified=()) -> bool:
    """Both rendering and cited parameter selection must agree with the frozen spec."""
    if not isinstance(record, dict) or record.get("association") != "exact":
        return False
    payload = record.get("design")
    if isinstance(payload, dict):
        from boardmodeler.models.buck_switching import BuckDesign
        from boardmodeler.models.buck_switching import design_from_spec as buck_design
        from boardmodeler.models.op_amp import OpAmpDesign
        from boardmodeler.models.op_amp import design_from_spec as op_amp_design
        from boardmodeler.models.pwm_controller import PwmControllerDesign
        from boardmodeler.models.pwm_controller import design_from_spec as pwm_design

        design_type = {
            "buck_design": BuckDesign,
            "op_amp_design": OpAmpDesign,
            "pwm_controller_design": PwmControllerDesign,
        }.get(payload.get("record_kind"))
        try:
            design = design_type.from_payload(payload) if design_type else None
            builders = {
                BuckDesign: buck_design,
                OpAmpDesign: op_amp_design,
                PwmControllerDesign: pwm_design,
            }
            canonical = (
                builders[design_type](
                    spec,
                    unverified=unverified,
                    **({"mode": design.mode} if design_type is BuckDesign else {}),
                )
                if design_type
                else None
            )
            refreshed = design.record(delivered) if design else {}
            return bool(design and canonical) and (
                canonical.payload() == design.payload()
                and design.spec_digest == spec.digest()
                and design.part.strip().upper() == spec.part.strip().upper()
                and design.subckt == spec.subckt
                and refreshed.get("association") == "exact"
                and all(
                    record.get(key) == refreshed.get(key)
                    for key in (
                        "schema_version",
                        "record_kind",
                        "design_sha256",
                        "rendered_library_sha256",
                        "delivered_library_sha256",
                    )
                )
            )
        except AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError:
            return False
    return False


def _design_record(run: Retest) -> dict[str, Any]:
    previous = run.safe_data("model-design.json") or {}
    payload = previous.get("design")
    exact = not run.problem and saved_design_is_exact(
        previous, run.library, run.spec, unverified=run.unverified
    )
    record = {
        "schema_version": 1,
        "record_kind": "model_design_record",
        "design": None,
        "design_sha256": None,
        "rendered_library_sha256": None,
        **previous,
        "delivered_library_sha256": _hash(run.library),
        "delivered_symbol_sha256": None if run.symbol is None else _hash(run.symbol),
        "association": "exact" if exact else "invalid_after_change" if payload else "unavailable",
        "association_note": "Saved typed design strictly reloaded and rendered against the exact current library and fixed spec."
        if exact
        else "Prior design retained only as historical provenance; exact current parameters were not reconstructed from SPICE text.",
        "verdict": "UNJUDGED",
    }
    try:
        record["delivered_pin_order"] = list(subckt_ports(run.library.decode(), run.spec.subckt))
    except ValueError, UnicodeError:
        record["delivered_pin_order"] = []
    return record


def _scope_sections(run: Retest, design: dict[str, Any]) -> str:
    """Retain trusted renderer scope and clearly label historical seed origins."""
    sections = []
    # The published card's appendices are copied as historical scope, never as current measurements.
    card = run.prior.get("MODEL_CARD.md", b"").decode("utf-8", errors="replace")
    headings = (
        "Reviewed evidence scope",
        "Modeled scope and limitations",
        "Numerical assumptions",
        "Pin-only model (limited)",
    )
    for heading in headings:
        match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)", card)
        if match:
            sections.append(match.group().rstrip())
    parameters = run.safe_data("template-parameters.json") or {}
    if parameters.get("parameters"):
        sections.append(
            "## Historical parameter origins\n\nStarting-seed parameter origins remain in `template-parameters.json`; they do not establish current device values. The exact association is recorded in `model-design.json`."
        )
    evidence = run.safe_data("reviewed-extraction.json") or {}
    if (
        evidence.get("complete_datasheet_extraction") is False
        and "## Reviewed evidence scope" not in card
    ):
        sections.append(
            "## Reviewed evidence scope\n\nThis uses a partial reviewed extraction, not every datasheet statement. "
            + str(evidence.get("scope", ""))
            + "\nUnreviewed scope: "
            + str(evidence.get("unreviewed_scope", "not qualified"))
        )
    payload = design.get("design") if isinstance(design.get("design"), dict) else {}
    if payload.get("limitations") and "## Modeled scope and limitations" not in card:
        sections.append(
            "## Modeled scope and limitations\n\n"
            + "\n".join(f"- {item}" for item in payload["limitations"])
        )
    if payload.get("numerical_assumptions") and "## Numerical assumptions" not in card:
        sections.append(
            "## Numerical assumptions\n\nThese regularize the first-order implementation; they are not measured device characteristics.\n\n"
            + "\n".join(
                f"- `{item['name']}` = {item['value']} {item['unit']}: {item['reason']}"
                for item in payload["numerical_assumptions"]
                if isinstance(item, dict) and {"name", "value", "unit", "reason"} <= item.keys()
            )
        )
    return "\n\n".join(sections)


def row_results(
    run: Retest, report: HarnessReport
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    outcomes = {char_id: outcome for outcome in report.outcomes for char_id in outcome.char_ids}
    rows = []
    for characteristic in run.spec.characteristics:
        status, measured = "UNKNOWN", "-"
        required = characteristic.limits_text()
        if characteristic.char_id in run.unverified:
            required = next(
                (
                    str(row.get("required"))
                    for row in (run.safe_data("results.json") or {}).get("rows", ())
                    if isinstance(row, dict) and row.get("req_id") == characteristic.char_id
                ),
                "citation unverified: prior source citation was not verified",
            )
        elif characteristic.probe is None:
            numeric = (
                characteristic.req_class in ("DOCUMENTED_LIMIT", "TYPICAL_VALUE")
                and any(
                    value is not None
                    for value in (
                        characteristic.min_value,
                        characteristic.max_value,
                        characteristic.typ_value,
                    )
                )
                and not re.search(
                    r"recommended|operating (?:range|envelope)", characteristic.statement, re.I
                )
            )
            status = "UNKNOWN" if numeric else "NOT_APPLICABLE"
            required = characteristic.not_testable_reason or "no simulation probe"
        elif (
            characteristic.char_id in outcomes
            and not run.pin_only
            and characteristic.char_id not in run.unverified
        ):
            outcome = outcomes[characteristic.char_id]
            status, _detail = judge_characteristic(characteristic, outcome)
            measured = outcome.judged or "-"
        rows.append(
            {
                "req_id": characteristic.char_id,
                "statement": characteristic.statement,
                "required": required,
                "measured": measured,
                "status": status,
                "page": characteristic.source_page,
            }
        )
    counts = status_tally(row["status"] for row in rows)
    status = (
        "FAIL"
        if counts["FAIL"] or report.failing()
        else "UNKNOWN"
        if (counts["UNKNOWN"] or run.pin_only or not counts["PASS"] or not report.passed())
        else "PASS"
    )
    return rows, counts, status


def save_retest(run: Retest, report: HarnessReport | None = None) -> dict[str, Any]:
    """Archive prior receipts, refresh current associations, and retain scope on the card."""
    snapshot = hashlib.sha256()
    for name, raw in sorted(run.prior.items()):
        snapshot.update(name.encode() + b"\0" + raw + b"\0")
    history = run.directory / "retest-history" / snapshot.hexdigest()[:24]
    history.mkdir(parents=True, exist_ok=True)
    for name, raw in run.prior.items():
        target = history / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    _write(
        history / "snapshot.json",
        {
            "scope": "Receipts present before retesting; they may describe earlier library/symbol bytes. No current verification is inferred."
        },
    )
    design = _design_record(run)
    _write(run.directory / "model-design.json", design)
    if run.pinout is not None:
        _write(run.directory / "pinout-report.json", run.pinout)
    else:
        _write(
            run.directory / "pinout-report.json",
            pinout_report(
                part=run.spec.part,
                document_sha256="",
                spec_digest=run.spec.digest(),
                reason="pinout_confirmation_unavailable: legacy retest has no saved source contract",
                library=run.library,
                symbol=run.symbol,
            ),
        )
    parameters = run.safe_data("template-parameters.json")
    if parameters is not None:
        parameters.update(
            final_model_sha256=_hash(run.library),
            final_model_matches_seed=parameters.get("seed_sha256") == _hash(run.library),
            provenance_note="Historical starting-seed origins; exact current association is in model-design.json.",
        )
        _write(run.directory / "template-parameters.json", parameters)
    for name in ("qualification-report.json", "pin-only-report.json", "sanity-report.json"):
        prior = run.safe_data(name)
        if prior is not None:
            prior.update(
                current_library_association="historical_only",
                retest_note="Not rerun by model test; prior observations are retained under retest-history.",
            )
            _write(run.directory / name, prior)
    rows, counts, status = row_results(run, report) if report else ([], status_tally(()), "BLOCKED")
    detail = run.problem or (
        "Retested the exact saved library against unchanged requirements; remaining numeric coverage gaps stay UNKNOWN."
        if status == "UNKNOWN"
        else "Measured model values failed the unchanged fixed requirements."
        if status == "FAIL"
        else "All bound measurements passed; declared nonnumeric gaps remain outside tested scope."
    )
    if run.pin_only:
        detail += " Pin-only models provide no function or electrical qualification."
    detail += " " + " ".join(run.notes)
    files = []
    if report is not None:
        _write(run.directory / "harness-report.json", report.payload())
        card_outcomes = []
        for outcome in report.outcomes:
            verified = tuple(
                char_id for char_id in outcome.char_ids if char_id not in run.unverified
            )
            unverified = tuple(char_id for char_id in outcome.char_ids if char_id in run.unverified)
            if run.pin_only:
                card_outcomes.append(
                    replace(
                        outcome,
                        status="UNKNOWN",
                        unknown_reason="pin_only_no_function",
                        detail="Pin-only shell: no functional device behavior is qualified.",
                    )
                )
            else:
                if verified:
                    card_outcomes.append(replace(outcome, char_ids=verified))
                if unverified:
                    card_outcomes.append(
                        replace(
                            outcome,
                            char_ids=unverified,
                            status="UNKNOWN",
                            unknown_reason="citation_unverified",
                            detail="Source citation remains unverified; a simulator result cannot verify the citation.",
                        )
                    )
        card_report = replace(report, outcomes=tuple(card_outcomes))
        files.extend(
            write_deliverables(
                out_dir=run.directory,
                part=run.spec.part,
                subckt=run.spec.subckt,
                spec=run.spec,
                report=card_report,
                document=run.spec.doc_id,
            )
        )
    else:
        (run.directory / "MODEL_CARD.md").write_text(
            f"# {run.spec.part} — retest BLOCKED\n\n{detail}\n", encoding="utf-8"
        )
    card = run.directory / "MODEL_CARD.md"
    appendix = _scope_sections(run, design)
    with card.open("a", encoding="utf-8") as stream:
        stream.write(
            f"\n## Current saved-model retest\n\n**{status}** · {counts['PASS']} pass · {counts['FAIL']} fail · {counts['UNKNOWN']} unknown.\n\n{detail}\n\nCurrent library sha256 `{_hash(run.library)}`; current symbol sha256 `{None if run.symbol is None else _hash(run.symbol)}`.\nTyped design association: `{design['association']}`. Package confirmation: `{(run.pinout or {}).get('status', 'unavailable')}`. Previous receipts are in `{history.relative_to(run.directory).as_posix()}` and describe prior observations only.\n\n"
        )
        if appendix:
            stream.write(
                "Historical modeled scope follows; it is not expanded by this retest.\n\n"
                + appendix
                + "\n"
            )
    payload = {
        "tool": "boardmodeler",
        "command": "model test",
        "part": run.spec.part,
        "subckt": run.spec.subckt,
        "status": status,
        "detail": detail.strip(),
        "counts": counts,
        "rows": rows,
        "probes": [] if report is None else [outcome.to_json() for outcome in report.outcomes],
        "model_sha256": _hash(run.library),
        "symbol_sha256": None if run.symbol is None else _hash(run.symbol),
        "spec_digest": run.spec.digest(),
        "design_association": design["association"],
        "prior_receipts": str(history),
        "files": [str(path) for path in files],
    }
    _write(run.directory / "retest-report.json", payload)
    previous = run.safe_data("results.json") or {"request": None, "stages": []}
    previous.update(
        status=status,
        detail=payload["detail"],
        rows=rows,
        counts=counts,
        part=run.spec.part,
        out_dir=str(run.directory),
        card_path=str(card),
        lib_path=None if status == "BLOCKED" else str(run.library_path),
        asy_path=None
        if status == "BLOCKED" or run.symbol is None
        else str(run.library_path.with_suffix(".asy")),
    )
    if not isinstance(previous.get("stages"), list):
        previous["stages"] = []
    previous["stages"].append(
        {
            "stage": "judge",
            "status": status,
            "detail": "Saved-model retest: " + payload["detail"],
            "counts": counts,
        }
    )
    _write(run.directory / "results.json", previous)
    return payload
