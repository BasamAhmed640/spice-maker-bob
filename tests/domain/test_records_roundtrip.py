"""Record round-trip and strictness tests (Phase 0 step 4).

The point of these tests is the contract other modules depend on: a record can
be dumped, hashed, reloaded and compared, and a malformed record is an error
rather than a silently ignored field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from boardmodeler.domain import SCHEMA_VERSION
from boardmodeler.domain.enums import (
    Criticality,
    EvidenceExtraction,
    EvidenceLevel,
    ModelKind,
    ProviderKind,
    ReaderBackend,
    RequirementClass,
    RequirementKind,
    RequirementOrigin,
    Status,
)
from boardmodeler.domain.expressions import ALL_OPS, parse_expr
from boardmodeler.domain.records import (
    AbstractionBoundary,
    ArchiveEntry,
    CircuitMapping,
    Condition,
    DataDisclosure,
    DocumentRecord,
    EvidenceRef,
    ExpectationSpec,
    Finding,
    HierarchyBlock,
    Limit,
    MappingEntry,
    ModelCapability,
    PageRef,
    PartIdentity,
    PinDefinition,
    ProviderIdentity,
    Requirement,
    ReviewItem,
    RunManifest,
    Stimulus,
    TestCase,
    TestResult,
)

EXCERPT = "FB pin regulation voltage is 0.795 V typical at 25 C."

EVIDENCE: dict[str, Any] = {
    "doc_id": "doc_tps54320",
    "page": {"pdf_page": 7, "printed_label": "7"},
    "section": "Electrical Characteristics",
    "table": "Electrical Characteristics",
    "figure": None,
    "excerpt": EXCERPT,
    "extraction": EvidenceExtraction.EMBEDDED_TEXT,
}

LIMIT: dict[str, Any] = {"min": 0.7875, "typ": 0.795, "max": 0.8025, "unit": "V"}


def _cases() -> list[tuple[str, type[BaseModel], dict[str, Any]]]:
    return [
        ("PageRef", PageRef, {"pdf_page": 0, "printed_label": "1"}),
        ("EvidenceRef", EvidenceRef, EVIDENCE),
        (
            "DocumentRecord",
            DocumentRecord,
            {
                "doc_id": "doc_tps54320",
                "title": "TPS54320 4.5-V to 17-V Input, 3-A Synchronous Step-Down Converter",
                "manufacturer": "Texas Instruments",
                "doc_type": "datasheet",
                "revision": "SLVS982C",
                "issued_date": "2018-05",
                "file_hash": "0" * 64,
                "source_url": "https://www.ti.com/lit/ds/symlink/tps54320.pdf",
                "provenance": "downloaded_public",
                "classification": "public",
                "remote_inference_allowed": True,
                "page_count": 40,
                "page_labels": {0: "1", 7: "8"},
                "text_extraction": "embedded",
                "path": "docs/files/doc_tps54320-tps54320.pdf",
                "redistribution_allowed": False,
            },
        ),
        (
            "PartIdentity",
            PartIdentity,
            {
                "manufacturer": "Texas Instruments",
                "family": "TPS5432x",
                "ordering_code": "TPS54320RHLT",
                "base_part": "TPS54320",
                "package": "VQFN-14",
                "revision": "C",
                "ambiguities": [],
                "confidence": "partial",
                "evidence": [EVIDENCE],
            },
        ),
        (
            "PinDefinition",
            PinDefinition,
            {
                "part_id": "TPS54320",
                "physical_pin": "7",
                "name": "FB",
                "function": "Feedback input for the regulation loop",
                "polarity": "not_applicable",
                "direction": "input",
                "supply_domain": None,
                "output_topology": "input_only",
                "connection_requirement": "required",
                "unused_pin_treatment": None,
                "behavior": ["regulates V(FB) to VREF when EN is above its threshold"],
                "evidence": [EVIDENCE],
                "mapped_symbol_pin": "FB",
            },
        ),
        ("Limit", Limit, LIMIT),
        ("Condition", Condition, {"text": "VIN = 12 V", "parameter_overrides": {"VIN": 12.0}}),
        (
            "Requirement",
            Requirement,
            {
                "req_id": "REQ_TPS54320_ELEC_001",
                "applies_to": "TPS54320",
                "configuration": "FPWM, ILIM_MODE=retry_hiccup",
                "kind": RequirementKind.ELECTRICAL,
                "class": RequirementClass.DOCUMENTED_LIMIT,
                "criticality": Criticality.CRITICAL,
                "origin": RequirementOrigin.DOCUMENT,
                "statement": "V(FB) is regulated within the feedback regulation limits.",
                "limits": LIMIT,
                "expression": {
                    "op": "between",
                    "signal": "V(FB)",
                    "low": 0.7875,
                    "high": 0.8025,
                    "unit": "V",
                    "interval": {"start_s": 5e-3, "end_s": 6e-3},
                },
                "conditions": [{"text": "TA = 25 C", "parameter_overrides": {}}],
                "signal_refs": ["V(FB)"],
                "evidence": [EVIDENCE],
                "conflicts": [],
                "citation_verified": True,
                "status": "active",
            },
        ),
        (
            "ModelCapability",
            ModelCapability,
            {
                "model_id": "bm_reg_buck_tps54320",
                "kind": ModelKind.REDUCED_BEHAVIORAL,
                "behaviors": {
                    "startup": "supported",
                    "shutdown": "supported",
                    "dc_regulation": "supported",
                    "load_transients": "unsupported",
                    "input_current": "unknown",
                    "current_limit_recovery": "supported",
                    "switching_waveforms": "unsupported",
                    "compensation_loop": "unsupported",
                    "thermal_dependence": "unsupported",
                    "reverse_current_prebias": "not_tested",
                },
                "valid_domain": {"VIN": LIMIT, "note": "12 V nominal"},
                "exclusions": ["switch-node ripple is not modeled"],
                "evidence_level": EvidenceLevel.SYNTHETIC_ANALYTICAL,
                "probe_results": ["T_capability_startup_001"],
                "source_model_hash": "1" * 64,
            },
        ),
        (
            "Stimulus",
            Stimulus,
            {"kind": "dc_ramp", "target": "V1", "params": {"v0": 0.0, "v1": 12.0, "t_r": 2e-3}},
        ),
        ("ExpectationSpec", ExpectationSpec, {"kind": "satisfy", "detail": "rail reaches 3.3 V"}),
        (
            "TestCase",
            TestCase,
            {
                "test_id": "T_nominal_startup_001",
                "requirement_ids": ["REQ_TPS54320_ELEC_001"],
                "scenario_id": "nominal_startup",
                "scope": "circuit_compliance",
                "stimulus": [{"kind": "dc_ramp", "target": "V1", "params": {"t_r": 2e-3}}],
                "deck_template": "tests/decks/regulator_nominal.cir",
                "expected": {"kind": "satisfy", "detail": "V(3V3) within limits by 5 ms"},
                "measurement": ["V(3V3)", "V(PG)"],
                "max_timestep_s": 1e-6,
                "tolerance": {"v_pct": 2.0},
            },
        ),
        (
            "TestResult",
            TestResult,
            {
                "test_id": "T_nominal_startup_001",
                "status": Status.PASS,
                "requirement_ids": ["REQ_TPS54320_ELEC_001"],
                "measured": {"V(3V3)@5ms": 3.3021, "PG_high_at_s": 0.0031},
                "expected": "V(3V3) in [3.234, 3.366] by 5 ms",
                "detail": "rail settled",
                "waveform_refs": ["runs/run_x/deck.raw:V(3V3)"],
                "log_ref": "runs/run_x/deck.log",
                "run_id": "run_x",
                "duration_s": 1.25,
                "unknown_reason": None,
                "blocked_reason": None,
            },
        ),
        (
            "MappingEntry",
            MappingEntry,
            {
                "refdes": "U1",
                "model_id": "bm_reg_buck_tps54320",
                "manufacturer": "Texas Instruments",
                "part_number": "TPS54320",
                "package": "VQFN-14",
                "value": "TPS54320",
                "symbol_pin": {"VIN": "1", "SW": "2"},
            },
        ),
        (
            "HierarchyBlock",
            HierarchyBlock,
            {
                "instance_refdes": "XU1",
                "subckt": "BM_REG_BUCK",
                "node_order": ["VIN", "EN", "FB", "PG"],
                "parent_nodes": ["12V", "EN_U1", "FB_U1", "PG_U1"],
            },
        ),
        (
            "AbstractionBoundary",
            AbstractionBoundary,
            {
                "refdes": "U2",
                "omitted_pins": ["SW", "BOOT"],
                "preserved_connectivity": True,
                "reason": "LDO template has no switching node",
            },
        ),
        (
            "CircuitMapping",
            CircuitMapping,
            {
                "source": "ltspice_asc",
                "entries": [
                    {
                        "refdes": "U1",
                        "model_id": "bm_reg_buck_tps54320",
                        "manufacturer": "Texas Instruments",
                        "part_number": "TPS54320",
                        "package": "VQFN-14",
                        "value": "TPS54320",
                        "symbol_pin": {"VIN": "1"},
                    }
                ],
                "hierarchy": [
                    {
                        "instance_refdes": "XU1",
                        "subckt": "BM_REG_BUCK",
                        "node_order": ["VIN"],
                        "parent_nodes": ["12V"],
                    }
                ],
                "abstractions": [
                    {
                        "refdes": "U2",
                        "omitted_pins": ["SW"],
                        "preserved_connectivity": True,
                        "reason": "LDO template",
                    }
                ],
                "unresolved": ["U9: no model assigned"],
            },
        ),
        ("ArchiveEntry", ArchiveEntry, {"path": "deck.cir", "sha256": "a" * 64, "size": 412}),
        (
            "ProviderIdentity",
            ProviderIdentity,
            {
                "provider": "http_inference",
                "kind": ProviderKind.HTTP_INFERENCE,
                "model": "bob-test-model",
                "endpoint": "https://bob.example.invalid/v1/chat/completions",
                "usage_units": "tokens",
                "usage": {"prompt_tokens": 8123.0, "completion_tokens": 940.0},
                "detail": {"policy": "public-only"},
            },
        ),
        (
            "DataDisclosure",
            DataDisclosure,
            {
                "provider": "http_inference",
                "endpoint": "https://bob.example.invalid/v1/chat/completions",
                "doc_ids": ["doc_tps54320"],
                "page_ranges": {"doc_tps54320": [6, 7, 8]},
                "chars": 24180,
                "policy": "classification=public, --allow-remote",
            },
        ),
        (
            "RunManifest",
            RunManifest,
            {
                "run_id": "run_20260101T000000Z_ab12",
                "created_utc": "2026-01-01T00:00:00Z",
                "mode": "circuit",
                "inputs": [{"path": "deck.cir", "sha256": "a" * 64, "size": 412}],
                "model_hashes": {"bm_reg_buck_tps54320": "b" * 64},
                "test_hashes": {"T_nominal_startup_001": "c" * 64},
                "requirement_baseline_hash": "d" * 64,
                "tool_versions": {"ltspice": "26.0.0.3", "python": "3.14.2"},
                "parameters": {"max_repair_iterations": "3"},
                "provider": None,
                "reader_backend": ReaderBackend.NATIVE,
                "resource_usage": {"wall_s": 1.25, "peak_rss_mb": 88.5},
                "results_summary": {"PASS": 4, "UNKNOWN": 1},
                "qualifications": ["type-B behavioral model, not silicon-accurate"],
                "disclosures": [],
            },
        ),
        (
            "Finding",
            Finding,
            {
                "code": "SC009_supply_domain_assignment",
                "status": Status.FAIL,
                "refdes": "U3",
                "nets": ["1V8_PG"],
                "message": "PG pull-up is on 3V3 while the pin's domain is 1V8",
                "detail": {"pin": "PG", "domain": "1V8", "net": "3V3"},
            },
        ),
        (
            "ReviewItem",
            ReviewItem,
            {
                "id": "RV001",
                "kind": "ambiguity",
                "question": "Which package variant is on the board?",
                "options": ["VQFN-14", "HTSSOP-14"],
                "recommended": "VQFN-14",
                "blocking": False,
                "affected_requirement_ids": ["REQ_TPS54320_ELEC_001"],
                "resolution": None,
            },
        ),
    ]


CASES = _cases()
IDS = [name for name, _, _ in CASES]


@pytest.mark.parametrize(("name", "model", "literal"), CASES, ids=IDS)
def test_record_roundtrip_is_lossless(name: str, model: type[BaseModel], literal: dict) -> None:
    obj = model.model_validate(literal)
    dumped = obj.model_dump_json(indent=2)
    assert json.loads(dumped)["schema_version"] == SCHEMA_VERSION
    reloaded = model.model_validate_json(dumped)
    assert reloaded == obj
    # Re-dumping must be byte-identical: baselines are compared by hash.
    assert reloaded.model_dump_json(indent=2) == dumped


@pytest.mark.parametrize(("name", "model", "literal"), CASES, ids=IDS)
def test_record_rejects_unknown_fields(name: str, model: type[BaseModel], literal: dict) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**literal, "not_a_real_field": 1})


def test_requirement_serializes_class_key() -> None:
    payload = json.loads(Requirement.model_validate(REQUIREMENT_LITERAL).model_dump_json())
    assert payload["class"] == RequirementClass.DOCUMENTED_LIMIT.value
    assert "req_class" not in payload
    # The alias is also accepted for input, so hand-written fixtures work.
    assert (
        Requirement.model_validate(REQUIREMENT_LITERAL).req_class
        is RequirementClass.DOCUMENTED_LIMIT
    )


REQUIREMENT_LITERAL = next(lit for name, _, lit in CASES if name == "Requirement")


def test_limit_rejects_out_of_order_triple() -> None:
    with pytest.raises(ValidationError, match="out of order"):
        Limit(min=1.0, typ=0.5, max=2.0, unit="V")


def test_interval_requires_positive_width() -> None:
    with pytest.raises(ValidationError, match="must be <"):
        parse_expr(
            {
                "op": "gt",
                "signal": "V(x)",
                "value": 1.0,
                "unit": "V",
                "interval": {"start_s": 1.0, "end_s": 1.0},
            }
        )


def test_event_ref_requires_value_for_crossing_kinds() -> None:
    with pytest.raises(ValidationError, match="requires a value"):
        parse_expr(
            {
                "op": "ordering",
                "first": {"signal": "V(x)", "kind": "rise_above", "unit": "V"},
                "then": {"signal": "V(y)", "kind": "fall_below", "value": 1.0, "unit": "V"},
            }
        )


def test_model_capability_requires_every_behavior_key() -> None:
    literal = next(lit for name, _, lit in CASES if name == "ModelCapability")
    broken = dict(
        literal,
        behaviors={k: v for k, v in literal["behaviors"].items() if k != "thermal_dependence"},
    )
    with pytest.raises(ValidationError, match="thermal_dependence"):
        ModelCapability.model_validate(broken)


def test_test_result_unknown_requires_reason() -> None:
    literal = next(lit for name, _, lit in CASES if name == "TestResult")
    with pytest.raises(ValidationError, match="unknown_reason"):
        TestResult.model_validate(dict(literal, status=Status.UNKNOWN, unknown_reason=None))
    with pytest.raises(ValidationError, match="blocked_reason"):
        TestResult.model_validate(dict(literal, status=Status.BLOCKED, blocked_reason=None))


def test_hierarchy_block_requires_matching_arity() -> None:
    with pytest.raises(ValidationError, match="equal length"):
        HierarchyBlock(
            instance_refdes="XU1", subckt="BM_REG_BUCK", node_order=["A"], parent_nodes=[]
        )


# --------------------------------------------------------------------------- #
# the expression AST is closed


_MINIMAL_EXPRS: dict[str, dict[str, Any]] = {
    "lt": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "le": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "gt": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "ge": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "between": {"signal": "V(out)", "low": 0.0, "high": 1.0, "unit": "V"},
    "rise_above": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "fall_below": {"signal": "V(out)", "value": 1.0, "unit": "V"},
    "event_delay": {
        "start": {"signal": "V(in)", "kind": "rise_above", "value": 0.5, "unit": "V"},
        "end": {"signal": "V(out)", "kind": "rise_above", "value": 0.5, "unit": "V"},
        "min_s": 1e-6,
        "max_s": 1e-3,
    },
    "pulse_width": {
        "start": {"signal": "V(pg)", "kind": "fall_below", "value": 0.4, "unit": "V"},
        "end": {"signal": "V(pg)", "kind": "rise_above", "value": 2.0, "unit": "V"},
        "min_s": 1e-3,
        "max_s": 0.1,
    },
    "ordering": {
        "first": {"signal": "V(3V3)", "kind": "rise_above", "value": 3.0, "unit": "V"},
        "then": {"signal": "V(1V8)", "kind": "rise_above", "value": 1.6, "unit": "V"},
    },
    "hold": {"signal": "V(pg)", "value": 3.3, "unit": "V", "stable": True},
    "state_dependent": {
        "when": {"signal": "V(en)", "kind": "rise_above", "value": 1.2, "unit": "V"},
        "then": {"op": "gt", "signal": "V(3V3)", "value": 3.0, "unit": "V"},
    },
    "all_of": {"items": [{"op": "pin_connected", "refdes": "U1", "pin": "1"}]},
    "any_of": {"items": [{"op": "pin_open", "refdes": "U1", "pin": "2"}]},
    "not": {"item": {"op": "pin_connected", "refdes": "U1", "pin": "1"}},
    "net_equals": {"refdes": "U1", "pin": "PERST", "net": "PERST_N"},
    "net_not_equals": {"refdes": "U1", "pin": "PERST", "net": "GND"},
    "pullup_domain": {"refdes": "U1", "pin": "PG", "net": "3V3", "domain": "3V3"},
    "pin_connected": {"refdes": "U1", "pin": "1"},
    "pin_open": {"refdes": "U1", "pin": "2"},
}


def test_all_ops_are_implemented_and_no_extra_ops_exist() -> None:
    assert set(_MINIMAL_EXPRS) == set(ALL_OPS)
    for op, fields in _MINIMAL_EXPRS.items():
        node = parse_expr({"op": op, **fields})
        assert node.op == op


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "exec", "code": "import os; os.system('echo pwned')"},
        {"op": "eval", "expression": "__import__('os').system('echo pwned')"},
        {"op": "gt", "signal": "V(out)", "value": 1.0, "unit": "V", "python": "print(1)"},
    ],
    ids=["unknown-op-exec", "unknown-op-eval", "extra-field"],
)
def test_expr_rejects_ops_outside_the_allowed_set(payload: dict) -> None:
    with pytest.raises(ValidationError):
        parse_expr(payload)


@pytest.mark.parametrize(
    "payload",
    ["os.system('echo pwned')", "V(out) > 1.0", 3.14, None, ["gt"]],
    ids=["code-string", "spice-string", "number", "null", "list"],
)
def test_expr_rejects_non_object_payloads(payload: object) -> None:
    with pytest.raises(ValidationError):
        parse_expr(payload)


def test_expr_dump_is_canonical_and_hashable() -> None:
    node = parse_expr({"op": "not", "item": {"op": "pin_open", "refdes": "U1", "pin": "2"}})
    payload = node.model_dump_json()
    assert (
        hashlib.sha256(payload.encode()).hexdigest()
        == hashlib.sha256(node.model_dump_json().encode()).hexdigest()
    )
    assert '"op":"not"' in payload
