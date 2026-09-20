"""Validation tests for ``requirements/model.py`` (Plan Phase 2 §3).

Every fixture here is synthetic: doc ids, sections and excerpts are written for
this file and are not taken from a vendor datasheet. Each test is written so a
plausible bug fails it — an ordering check that never fires, an absolute-maximum
marker that is missed, a unit prefix that converts the wrong way, a condition
regex that cannot match because the text was case-folded first.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from boardmodeler.domain.enums import (
    Criticality,
    EvidenceExtraction,
    RequirementClass,
    RequirementKind,
    RequirementOrigin,
)
from boardmodeler.domain.records import (
    Condition,
    DocumentRecord,
    EvidenceRef,
    Limit,
    PageRef,
    Requirement,
)
from boardmodeler.requirements.model import (
    ABSOLUTE_MAXIMUM_MARKERS,
    UNIT_VOCABULARY,
    UnitError,
    UnknownUnitError,
    classify_class,
    is_absolute_maximum,
    normalize_unit,
    scale_factor,
    validate_requirement,
    validate_requirements,
)

DOC_ID = "DOC_synthetic_regulator"
SECTION_OK = "Recommended Operating Conditions"
SECTION_ABS_MAX = "Absolute Maximum Ratings"
DEFAULT_EXCERPT = "The output voltage is 3.234 V to 3.366 V."
DEFAULT_LIMIT = Limit(min=3.234, typ=3.3, max=3.366, unit="V")


def make_evidence(
    *,
    excerpt: str = DEFAULT_EXCERPT,
    section: str | None = SECTION_OK,
    table: str | None = None,
    doc_id: str = DOC_ID,
    page: int = 0,
) -> EvidenceRef:
    return EvidenceRef(
        doc_id=doc_id,
        page=PageRef(pdf_page=page),
        section=section,
        table=table,
        excerpt=excerpt,
        extraction=EvidenceExtraction.EMBEDDED_TEXT,
    )


def make_requirement(
    req_id: str = "REQ_SYNTH_ELEC_001",
    *,
    req_class: RequirementClass = RequirementClass.DOCUMENTED_LIMIT,
    origin: RequirementOrigin = RequirementOrigin.DOCUMENT,
    criticality: Criticality = Criticality.CRITICAL,
    statement: str = "The 3V3 output stays within its regulation limits.",
    excerpt: str = DEFAULT_EXCERPT,
    section: str | None = SECTION_OK,
    table: str | None = None,
    page: int = 0,
    limits: Limit | None = DEFAULT_LIMIT,
    expression: dict[str, Any] | None = None,
    conditions: list[Condition] | None = None,
    signal_refs: list[str] | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> Requirement:
    return Requirement(
        req_id=req_id,
        applies_to="U1",
        kind=RequirementKind.ELECTRICAL,
        **{
            "class": req_class,
            "criticality": criticality,
            "origin": origin,
            "statement": statement,
            "limits": limits,
            "expression": expression,
            "conditions": conditions or [],
            "signal_refs": signal_refs or [],
            "evidence": evidence
            if evidence is not None
            else [make_evidence(excerpt=excerpt, section=section, table=table, page=page)],
        },
    )


def make_document(
    *,
    doc_id: str = DOC_ID,
    page_count: int = 8,
    text_extraction: str = "embedded",
) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        title="Synthetic regulator datasheet",
        doc_type="datasheet",
        provenance="synthetic_fixture",
        file_hash="0" * 64,
        page_count=page_count,
        text_extraction=text_extraction,
        path="docs/files/synthetic.pdf",
    )


# --------------------------------------------------------------------------- #
# limit sanity


def test_limit_triple_order_is_accepted() -> None:
    report = validate_requirements([make_requirement()])

    assert report.issues == []
    assert report.ok is True
    assert report.errors == []


def test_out_of_order_limit_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        Limit(min=3.5, max=3.0, unit="V")


def test_out_of_order_limit_is_detected_when_construction_validation_is_bypassed() -> None:
    requirement = make_requirement()
    assert requirement.limits is not None
    broken = requirement.limits.model_copy(update={"min": 3.5})  # model_copy skips validation
    bypassed = requirement.model_copy(update={"limits": broken})

    issues = validate_requirement(bypassed)

    assert [(issue.severity, issue.code) for issue in issues] == [("error", "limit_triple_order")]
    assert issues[0].detail["min"] == "3.5"
    assert issues[0].detail["max"] == "3.366"


@pytest.mark.parametrize(
    ("req_class", "limits", "expect_error"),
    [
        (RequirementClass.DOCUMENTED_LIMIT, None, True),
        (RequirementClass.DOCUMENTED_LIMIT, Limit(typ=3.3, unit="V"), True),
        (RequirementClass.TYPICAL_VALUE, Limit(min=3.234, unit="V"), True),
        (RequirementClass.DOCUMENTED_LIMIT, Limit(max=3.366, unit="V"), False),
        (RequirementClass.TYPICAL_VALUE, Limit(typ=3.3, unit="V"), False),
    ],
)
def test_limit_missing_value(
    req_class: RequirementClass, limits: Limit | None, expect_error: bool
) -> None:
    requirement = make_requirement(req_class=req_class, limits=limits)

    codes = [issue.code for issue in validate_requirement(requirement)]

    assert ("limit_missing_value" in codes) is expect_error


# --------------------------------------------------------------------------- #
# units


@pytest.mark.parametrize(
    ("from_unit", "to_unit", "expected"),
    [
        ("mV", "V", 0.001),
        ("V", "mV", 1000.0),
        ("kV", "V", 1000.0),
        ("kohm", "ohm", 1000.0),
        ("Mohm", "ohm", 1e6),
        ("uA", "mA", 0.001),
        ("nF", "uF", 0.001),
        ("uH", "mH", 0.001),
        ("ms", "s", 0.001),
        ("us", "ns", 1000.0),
        ("MHz", "kHz", 1000.0),
        ("mW", "W", 0.001),
        ("V", "V", 1.0),
    ],
)
def test_scale_factor_is_si_prefix_aware(from_unit: str, to_unit: str, expected: float) -> None:
    assert scale_factor(from_unit, to_unit) == pytest.approx(expected)


def test_scale_factor_refuses_mixed_dimensions_and_unknown_units() -> None:
    with pytest.raises(UnitError):
        scale_factor("V", "A")
    with pytest.raises(UnknownUnitError):
        scale_factor("furlong", "V")
    with pytest.raises(UnknownUnitError):
        scale_factor("V", "furlong")


def test_normalize_unit_folds_prefixes_and_datasheet_spellings() -> None:
    assert normalize_unit("mV") == "V"
    assert normalize_unit("kohm") == "ohm"
    assert normalize_unit("MOhm") == "ohm"
    assert normalize_unit("Ω") == "ohm"
    assert normalize_unit("uA") == "A"
    assert normalize_unit("°C") == "C"
    assert normalize_unit("degC") == "C"
    assert normalize_unit("μA") == "A"
    assert normalize_unit("percent") == "%"
    assert normalize_unit("%") == "%"


def test_normalize_unit_refuses_an_empty_or_unknown_unit() -> None:
    with pytest.raises(UnknownUnitError):
        normalize_unit("")
    with pytest.raises(UnknownUnitError):
        normalize_unit("volt")


def test_unit_vocabulary_stays_small_and_typed() -> None:
    """The vocabulary is closed on purpose; extending it is a deliberate act.

    It must contain every electrical quantity a datasheet states numerically, and
    every entry must map to one physical dimension — a unit outside the set is an
    ``unit_unknown`` error rather than a guess.
    """
    assert UNIT_VOCABULARY == {
        "V": "voltage",
        "A": "current",
        "s": "time",
        "ohm": "resistance",
        "C": "temperature",
        "W": "power",
        "Hz": "frequency",
        "F": "capacitance",
        "H": "inductance",
        "%": "ratio",
        "ratio": "ratio",
        # Added for real datasheet rows: transconductance (uMhos / A per V),
        # amplifier gain (V/V), a cycle count, and conductance (siemens).
        "S": "conductance",
        "A/V": "transconductance",
        "V/V": "gain",
        "s/V": "inverse_voltage_slew",
        "V/s": "voltage_slew",
        "A/s": "current_slew",
        "C/W": "thermal_resistance",
        "K/W": "thermal_resistance",
        "cycles": "count",
        "dB": "logarithmic_ratio",
        "J": "energy",
        "A/A": "current_gain",
        "V/C": "voltage_temperature_drift",
        "A/C": "current_temperature_drift",
        "ppm/C": "relative_temperature_drift",
        "ppm": "parts_per_million",
        "%/V": "relative_voltage_sensitivity",
        "%/A": "relative_current_sensitivity",
        "%/C": "relative_temperature_sensitivity",
        "V/sqrt(Hz)": "voltage_noise_density",
        "A/sqrt(Hz)": "current_noise_density",
    }
    # Canonical compound units retain their direction and physical dimension.
    assert normalize_unit("uS") == "S"
    assert normalize_unit("A/V") == "A/V"
    assert scale_factor("uS", "S") == pytest.approx(1e-6)
    with pytest.raises(UnitError):
        scale_factor("A/V", "V/V")


def test_unknown_limit_unit_is_retained_as_an_unconverted_warning() -> None:
    requirement = make_requirement(limits=Limit(min=3.234, max=3.366, unit="furlong"))

    issues = validate_requirement(requirement)

    assert [(i.severity, i.code, i.detail["field"], i.detail["unit"]) for i in issues] == [
        ("warning", "unit_unknown", "limits", "furlong")
    ]


@pytest.mark.parametrize(
    ("unit", "canonical", "factor"),
    [
        ("ns/V", "s/V", 1e-9),
        ("ns / mV", "s/V", 1e-6),
        ("V/ns", "V/s", 1e9),
        ("mV/µs", "V/s", 1e3),
        ("mA/ms", "A/s", 1.0),
        ("°C/W", "C/W", 1.0),
        ("℃/mW", "C/W", 1e3),
        ("deg C / W", "C/W", 1.0),
        ("K / W", "K/W", 1.0),
        ("mK/W", "K/W", 1e-3),
        ("mV/mV", "V/V", 1.0),
        ("µA/mV", "A/V", 1e-3),
    ],
)
def test_compound_units_validate_and_freeze_at_the_same_scale(
    unit: str, canonical: str, factor: float
) -> None:
    from boardmodeler.authoring.spec import normalize_unit as frozen_unit

    row = make_requirement(limits=Limit(max=20, unit=unit))
    assert not any(issue.code == "unit_unknown" for issue in validate_requirement(row))
    assert normalize_unit(unit) == canonical
    assert scale_factor(unit, canonical) == pytest.approx(factor)
    assert scale_factor(canonical, unit) == pytest.approx(1 / factor)
    base, multiplier = frozen_unit(unit)
    assert base == canonical
    assert multiplier == pytest.approx(factor)


def test_compound_units_do_not_invert_limits_or_accept_unrelated_dimensions() -> None:
    for target in ("V/ns", "ns", "V", "°C/W"):
        with pytest.raises(UnitError):
            scale_factor("ns/V", target)
    for unit in ("ns/furlong", "V/", "/V", "V/s/A", "V//s", "K"):
        with pytest.raises(UnknownUnitError):
            normalize_unit(unit)
    assert scale_factor("K/W", "°C/W") == 1.0


def test_unknown_expression_unit_is_an_error() -> None:
    requirement = make_requirement(
        expression={"op": "lt", "signal": "V(VOUT)", "value": 3.366, "unit": "parsecs"},
        signal_refs=["V(VOUT)"],
    )

    issues = validate_requirement(requirement)

    assert [(i.severity, i.code, i.detail["field"]) for i in issues] == [
        ("error", "unit_unknown", "expression")
    ]


# --------------------------------------------------------------------------- #
# absolute maximum


def test_absolute_maximum_section_rejects_an_operating_limit() -> None:
    requirement = make_requirement(
        section=SECTION_ABS_MAX,
        excerpt="Input voltage (VIN) -0.3 V to 20 V.",
    )

    assert is_absolute_maximum(requirement) is True
    issues = validate_requirement(requirement)

    assert [(issue.severity, issue.code) for issue in issues] == [
        ("error", "absolute_maximum_rejected")
    ]
    assert issues[0].detail["marker"] == "absolute maximum"
    assert issues[0].detail["matched_in"] == "section"
    assert "absolute maximum" in issues[0].message.lower()


def test_recommended_operating_conditions_are_accepted() -> None:
    requirement = make_requirement(section=SECTION_OK)

    assert is_absolute_maximum(requirement) is False
    assert validate_requirement(requirement) == []


@pytest.mark.parametrize("marker", ABSOLUTE_MAXIMUM_MARKERS)
def test_each_absolute_maximum_marker_is_rejected_and_named(marker: str) -> None:
    requirement = make_requirement(section=None, excerpt=f"Ratings text: {marker} applies here.")

    issues = [i for i in validate_requirement(requirement) if i.code == "absolute_maximum_rejected"]

    assert [issue.detail["marker"] for issue in issues] == [marker]
    assert marker in issues[0].message.lower()


def test_absolute_maximum_marker_in_a_table_or_a_statement_is_detected() -> None:
    by_table = make_requirement(
        section=None, table="Maximum Ratings", excerpt="VIN -0.3 V to 20 V."
    )
    by_statement = make_requirement(
        section=None, statement="Operation beyond the stress ratings is not permitted."
    )

    assert is_absolute_maximum(by_table) is True
    assert is_absolute_maximum(by_statement) is True
    assert [i.detail["matched_in"] for i in validate_requirement(by_table)] == ["table"]
    assert [i.detail["matched_in"] for i in validate_requirement(by_statement)] == ["statement"]


def test_absolute_maximum_is_rejected_for_a_typical_value_too() -> None:
    requirement = make_requirement(
        req_class=RequirementClass.TYPICAL_VALUE, section=SECTION_ABS_MAX
    )

    issues = validate_requirement(requirement)

    assert [issue.code for issue in issues] == ["absolute_maximum_rejected"]


def test_absolute_maximum_is_not_rejected_outside_the_limit_classes() -> None:
    requirement = make_requirement(req_class=RequirementClass.UNKNOWN, section=SECTION_ABS_MAX)

    assert is_absolute_maximum(requirement) is True
    assert validate_requirement(requirement) == []


# --------------------------------------------------------------------------- #
# typical vs limit


def test_typical_wording_classed_as_a_limit_warns() -> None:
    requirement = make_requirement(
        excerpt="The FB regulation voltage is typical 0.795 V.",
        limits=Limit(min=0.788, typ=0.795, max=0.802, unit="V"),
    )

    assert classify_class(requirement) is RequirementClass.TYPICAL_VALUE
    issues = validate_requirement(requirement)

    assert [(issue.severity, issue.code) for issue in issues] == [
        ("warning", "class_inference_mismatch")
    ]
    assert issues[0].detail == {
        "declared_class": "DOCUMENTED_LIMIT",
        "inferred_class": "TYPICAL_VALUE",
    }


def test_typical_value_from_typical_wording_does_not_warn() -> None:
    requirement = make_requirement(
        req_class=RequirementClass.TYPICAL_VALUE,
        excerpt="The FB regulation voltage is typ. 0.795 V.",
        limits=Limit(typ=0.795, unit="V"),
    )

    assert classify_class(requirement) is RequirementClass.TYPICAL_VALUE
    assert validate_requirement(requirement) == []


def test_bound_wording_classed_as_a_typical_value_warns() -> None:
    requirement = make_requirement(
        req_class=RequirementClass.TYPICAL_VALUE,
        excerpt="The output voltage minimum is 3.234 V.",
        limits=Limit(typ=3.3, unit="V"),
    )

    assert classify_class(requirement) is RequirementClass.DOCUMENTED_LIMIT
    issues = validate_requirement(requirement)

    assert [(issue.severity, issue.code) for issue in issues] == [
        ("warning", "class_inference_mismatch")
    ]


def test_class_inference_does_not_argue_with_deliberate_classes() -> None:
    requirement = make_requirement(
        req_class=RequirementClass.DERIVED_VALUE,
        excerpt="The current limit follows from the sense resistor minimum value.",
        limits=Limit(min=1.0, max=2.0, unit="V"),
    )

    assert classify_class(requirement) is RequirementClass.DOCUMENTED_LIMIT
    assert validate_requirement(requirement) == []


def test_silent_evidence_keeps_the_declared_class() -> None:
    requirement = make_requirement(
        req_class=RequirementClass.ASSUMPTION,
        excerpt="The thermal design is outside this model's scope.",
        limits=None,
    )

    assert classify_class(requirement) is RequirementClass.ASSUMPTION
    assert validate_requirement(requirement) == []


def test_absolute_maximum_evidence_is_never_classified_as_a_limit() -> None:
    requirement = make_requirement(section=SECTION_ABS_MAX)

    assert classify_class(requirement) is RequirementClass.UNKNOWN


# --------------------------------------------------------------------------- #
# conditions


def test_condition_stated_in_evidence_without_conditions_warns() -> None:
    requirement = make_requirement(excerpt="Reference voltage 0.795 V, TA = 25 C, VIN = 12 V.")

    issues = validate_requirement(requirement)

    assert [(issue.severity, issue.code) for issue in issues] == [("warning", "conditions_missing")]
    assert issues[0].detail["matched"] == "TA"


def test_conditions_present_silences_the_warning() -> None:
    requirement = make_requirement(
        excerpt="Reference voltage 0.795 V, TA = 25 C, VIN = 12 V.",
        conditions=[Condition(text="TA = 25 C"), Condition(text="VIN = 12 V")],
    )

    assert validate_requirement(requirement) == []


@pytest.mark.parametrize(
    "excerpt",
    [
        "Switching frequency is 1.2 MHz at 85 °C.",
        "The regulated output is 3.3 V at 25 C.",
        "RDS(on) is measured at IO = 2 A and VIN = 12 V.",
        "The part is specified over the full load range.",
        "Junction temperature must stay below the limit.",
    ],
)
def test_stated_conditions_are_detected(excerpt: str) -> None:
    requirement = make_requirement(excerpt=excerpt)

    assert "conditions_missing" in [i.code for i in validate_requirement(requirement)]


def test_a_section_title_is_not_itself_a_condition() -> None:
    requirement = make_requirement(
        section="Recommended Operating Conditions",
        excerpt="The output voltage is 3.234 V to 3.366 V.",
    )

    assert "conditions_missing" not in [i.code for i in validate_requirement(requirement)]


# --------------------------------------------------------------------------- #
# expressions and ids


def test_expression_op_is_checked_even_though_the_ast_is_closed() -> None:
    requirement = make_requirement(
        expression={"op": "lt", "signal": "V(VOUT)", "value": 3.366, "unit": "V"},
        signal_refs=["V(VOUT)"],
    )
    assert requirement.expression is not None
    broken = requirement.expression.model_copy(update={"op": "xor"})
    bypassed = requirement.model_copy(update={"expression": broken})

    issues = validate_requirement(bypassed)

    assert [(i.severity, i.code, i.detail["op"]) for i in issues] == [
        ("error", "expression_op_unknown", "xor")
    ]


def test_nested_expression_op_is_checked() -> None:
    requirement = make_requirement(
        expression={
            "op": "all_of",
            "items": [{"op": "lt", "signal": "V(VOUT)", "value": 3.366, "unit": "V"}],
        },
        signal_refs=["V(VOUT)"],
    )
    inner = requirement.expression.items[0].model_copy(update={"op": "nand"})
    broken = requirement.expression.model_copy(update={"items": [inner]})
    bypassed = requirement.model_copy(update={"expression": broken})

    assert [(i.code, i.detail["op"]) for i in validate_requirement(bypassed)] == [
        ("expression_op_unknown", "nand")
    ]


def test_signal_refs_missing_warns_when_the_expression_names_signals() -> None:
    requirement = make_requirement(
        expression={"op": "gt", "signal": "V(FB)", "value": 0.788, "unit": "V"},
        signal_refs=[],
    )

    issues = validate_requirement(requirement)

    assert [(i.severity, i.code, i.detail["signals"]) for i in issues] == [
        ("warning", "signal_refs_missing", "V(FB)")
    ]


def test_connectivity_expression_does_not_need_signal_refs() -> None:
    requirement = make_requirement(
        expression={"op": "pin_connected", "refdes": "U1", "pin": "FB"},
        signal_refs=[],
    )

    assert validate_requirement(requirement) == []


def test_duplicate_req_ids_are_an_error() -> None:
    first = make_requirement()
    second = make_requirement(statement="A second requirement that reuses the id.")

    report = validate_requirements([first, second])

    assert [(i.severity, i.code, i.req_id, i.detail["count"]) for i in report.issues] == [
        ("error", "duplicate_req_id", "REQ_SYNTH_ELEC_001", "2")
    ]
    assert report.ok is False
    assert report.counts[RequirementClass.DOCUMENTED_LIMIT.value] == 2


# --------------------------------------------------------------------------- #
# citations (validation half; verification lives in test_review.py)


def test_document_origin_without_evidence_is_an_error() -> None:
    requirement = make_requirement(evidence=[])

    issues = validate_requirement(requirement)

    assert [(i.severity, i.code) for i in issues] == [("error", "citation_missing")]


def test_test_fixture_origin_without_evidence_is_not_an_error() -> None:
    requirement = make_requirement(origin=RequirementOrigin.TEST_FIXTURE, evidence=[])

    assert validate_requirement(requirement) == []


def test_citation_page_is_checked_against_the_document() -> None:
    document = make_document(page_count=8)
    inside = make_requirement(req_id="REQ_INSIDE", page=7)
    outside = make_requirement(req_id="REQ_OUTSIDE", page=8)

    assert validate_requirement(inside, document=document) == []
    issues = validate_requirement(outside, document=document)

    assert [(i.severity, i.code, i.detail["pdf_page"]) for i in issues] == [
        ("error", "citation_page_out_of_range", "8")
    ]
    # Without the document record there is no page count to judge against.
    assert validate_requirement(outside) == []
    # validate_requirements resolves the cited document from the mapping.
    report = validate_requirements([outside], documents={DOC_ID: document})
    assert [i.code for i in report.issues] == ["citation_page_out_of_range"]


def test_an_unknown_page_count_is_not_treated_as_zero_pages() -> None:
    document = make_document(page_count=0)
    requirement = make_requirement(page=3)

    assert validate_requirement(requirement, document=document) == []


# --------------------------------------------------------------------------- #
# report shape and determinism


def test_report_counts_cover_every_class_and_split_errors_from_warnings() -> None:
    requirements = [
        make_requirement(req_id="REQ_ERR", limits=None),
        make_requirement(req_id="REQ_WARN", excerpt="Reference voltage 0.795 V, TA = 25 C."),
        make_requirement(
            req_id="REQ_USER",
            req_class=RequirementClass.USER_REQUIREMENT,
            origin=RequirementOrigin.USER,
            limits=None,
            excerpt="The board must reach regulation within 200 ms.",
        ),
    ]

    report = validate_requirements(requirements)

    assert [issue.code for issue in report.errors] == ["limit_missing_value"]
    assert "conditions_missing" in [issue.code for issue in report.issues]
    assert report.ok is False
    assert report.counts == {
        RequirementClass.DOCUMENTED_LIMIT.value: 2,
        RequirementClass.ABSOLUTE_MAXIMUM.value: 0,
        RequirementClass.TYPICAL_VALUE.value: 0,
        RequirementClass.DERIVED_VALUE.value: 0,
        RequirementClass.USER_REQUIREMENT.value: 1,
        RequirementClass.ASSUMPTION.value: 0,
        RequirementClass.UNKNOWN.value: 0,
    }


def test_validation_is_deterministic() -> None:
    requirements = [
        make_requirement(req_id="REQ_ERR", limits=None),
        make_requirement(req_id="REQ_WARN", excerpt="Reference voltage 0.795 V, TA = 25 C."),
        make_requirement(req_id="REQ_ERR"),
    ]

    first = validate_requirements(requirements, documents={DOC_ID: make_document()})
    second = validate_requirements(requirements, documents={DOC_ID: make_document()})

    assert first == second
    assert isinstance(first.issues[0].detail, dict)
