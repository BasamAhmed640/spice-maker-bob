"""The buck design record: what to build as data, rendered by a pure function of it.

The golden hashes were taken from the unchanged baseline (b1ced1c) before the design was
split from the rendering, so they prove the refactor changed no byte of any library.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from tests.models import test_buck_template_slice as slice_tests
from tests.models import test_peak_current_buck as peak_tests

from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.models.buck_switching import (
    RENDERER_VERSION,
    BuckDesign,
    ParameterOrigin,
    TemplateSeedError,
    design_from_spec,
    design_record_payload,
    render_library,
    seed_from_spec,
)

GOLDEN = {
    "slice_pad_SW": "80f045a3fb55c3614ec1e0069857c1893aa523b852d3d003129ab1196f381875",
    "slice_pad_AVG": "0712ac2e32a17bf06093d748fe5a31c649945cd65c3a4d9074feea9b1e32b735",
    "slice_nopad_SW": "33e443216ba11692020fa0efbf55b4cca3878aee3d8141ebfa5cb74505365a40",
    "slice_nopad_AVG": "583a9c16a165ffef8e4f35877f4ba0967328a056ffb1466ceb8e1655f56e87cf",
    "slice_alias_SW": "483c915758da461697e9ec790466151c5e99ebccb009747566f0fe463682eead",
    "slice_alias_AVG": "a59ccfd5474c4e97aca3e1cd6449e95e4329fe22a71669909dfdea82fa85a07b",
    "peak_basis_SW": "018a1884cf4bd6c7409bfe8d667c1f08cdea2334819d03a2117ed61647717991",
    "peak_basis_AVG": "48a5ec4e1d0932916ffce2e0374fe9db2d37fdb423fd370bb8d5ec72fa68026f",
}
TPS_GOLDEN = {
    "SW": "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2",
    "AVG": "adce082491d0035c6514aaa9b3ada916f75bcd998da4731caebe58fa2294d0dc",
}
# The parameter values of the first real TPS54332DDA seed (numbers only, no datasheet text).
TPS_PORTS = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "POWERPAD")
TPS_VALUES = {
    "VREF": 0.8,
    "FSW": 1000000.0,
    "TONMIN": 1.1e-07,
    "DMAX": 0.91,
    "ILIM": 5.35,
    "GMCS": 12.0,
    "VECO": 0.5,
    "ECO_I": 0.16,
    "EAGM": 9.2e-05,
    "EAI": 7e-06,
    "ISS": 2e-06,
    "SSOFS": 0.01,
    "ENTH": 1.25,
    "ENHYS": 0.05,
    "UVTH": 3.5,
    "UVHYS": 0.2,
    "IQOP": 8.2e-05,
    "IQSD_BASE": 0.0,
    "EN_PULLUP": 1e-06,
    "RON": 0.115,
    "FOLD6": 0.6,
    "FOLD4": 0.4,
    "FOLD2": 0.2,
}
TPS_UNITS = {
    "VREF": "V",
    "FSW": "Hz",
    "TONMIN": "s",
    "DMAX": "ratio",
    "ILIM": "A",
    "GMCS": "A/V",
    "VECO": "V",
    "ECO_I": "A",
    "EAGM": "S",
    "EAI": "A",
    "ISS": "A",
    "SSOFS": "V",
    "ENTH": "V",
    "ENHYS": "V",
    "UVTH": "V",
    "UVHYS": "V",
    "IQOP": "A",
    "IQSD_BASE": "A",
    "EN_PULLUP": "A",
    "RON": "ohm",
    "FOLD6": "V",
    "FOLD4": "V",
    "FOLD2": "V",
}


def _specs() -> dict:
    return {
        "slice_pad": slice_tests._spec((*slice_tests.ROLES, "POWERPAD")),
        "slice_nopad": slice_tests._spec(slice_tests.ROLES),
        "slice_alias": slice_tests._spec(
            ("BST", "VIN", "EN", "SS_TR", "FB", "COMP", "AGND", "SW", "PGND")
        ),
        "peak_basis": peak_tests._spec(*peak_tests._basis()),
    }


def _design(mode: str = "SW") -> BuckDesign:
    design = design_from_spec(_specs()["peak_basis"], mode=mode)
    assert design is not None
    return design


def _changed(design: BuckDesign, name: str, **changes) -> BuckDesign:
    parameters = tuple(
        dataclasses.replace(item, **changes) if item.name == name else item
        for item in design.parameters
    )
    return dataclasses.replace(design, parameters=parameters)


def _tps_design(mode: str) -> BuckDesign:
    parameters = tuple(
        ParameterOrigin(name, value, TPS_UNITS[name], "template_default")
        for name, value in TPS_VALUES.items()
    )
    return BuckDesign(
        contract_id="peak_current_buck_v1",
        contract_sha256=_design().contract_sha256,
        renderer_version=RENDERER_VERSION,
        spec_digest="0" * 64,
        part="TPS54332DDA",
        subckt="TPS54332DDA",
        ports=TPS_PORTS,
        parameters=parameters,
        mode=mode,
    )


@pytest.mark.parametrize("key", list(GOLDEN))
def test_rendering_is_byte_identical_to_the_baseline(key: str) -> None:
    name, mode = key.rsplit("_", 1)
    spec = _specs()[name]
    design = design_from_spec(spec, mode=mode)
    assert design is not None
    text = render_library(design)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == GOLDEN[key]
    seed = seed_from_spec(spec, mode=mode)
    assert seed is not None
    assert seed.library_text == text
    assert seed.design == design


@pytest.mark.parametrize("mode", ["SW", "AVG"])
def test_real_tps54332_values_render_the_baseline_bytes(mode: str) -> None:
    text = render_library(_tps_design(mode))
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == TPS_GOLDEN[mode]
    assert ".subckt TPS54332DDA " + " ".join(TPS_PORTS) in text


def _first_default(design: BuckDesign) -> str:
    return next(item.name for item in design.parameters if item.origin == "template_default")


INVALID = {
    "mode": (lambda d: dataclasses.replace(d, mode="WRONG"), "invalid_mode"),
    "subckt": (lambda d: dataclasses.replace(d, subckt="bad name"), "invalid_subckt"),
    "duplicate port": (
        lambda d: dataclasses.replace(d, ports=(*d.ports, "vin")),
        "duplicate_ports",
    ),
    "unmodelled pin": (
        lambda d: dataclasses.replace(d, ports=(*d.ports, "SYNC")),
        "buck_design_pins",
    ),
    "missing parameter": (
        lambda d: dataclasses.replace(d, parameters=d.parameters[:-1]),
        "parameter_set",
    ),
    "not finite": (lambda d: _changed(d, "VREF", value=float("nan")), "nonfinite"),
    "bool value": (lambda d: _changed(d, "VREF", value=True), "value_type"),
    "unknown origin": (lambda d: _changed(d, "VREF", origin="guessed"), "origin"),
    "cited without a source": (
        lambda d: _changed(d, "VREF", origin="cited_row", row_id=None),
        "provenance",
    ),
    "default with a source": (
        lambda d: _changed(d, _first_default(d), row_id="R_X", page=3, excerpt="x"),
        "provenance",
    ),
    "duty above one": (lambda d: _changed(d, "DMAX", value=1.5), "invalid_duty"),
    "zero frequency": (lambda d: _changed(d, "FSW", value=0.0), "invalid_parameter: FSW"),
}


@pytest.mark.parametrize("case", list(INVALID))
def test_an_invalid_design_is_rejected(case: str) -> None:
    make, reason = INVALID[case]
    with pytest.raises(TemplateSeedError, match=reason):
        make(_design())


def test_serialization_is_canonical_and_a_saved_design_reloads_identically() -> None:
    design = _design()
    text = design.to_json()
    assert text == _design().to_json()
    assert text.endswith(chr(10)) and not text.endswith(chr(10) * 2)
    assert json.loads(text) == design.payload()
    again = BuckDesign.from_payload(json.loads(text))
    assert again == design
    assert again.sha256 == design.sha256
    assert render_library(again) == render_library(design)


def test_a_saved_design_that_was_altered_or_is_incomplete_is_refused() -> None:
    good = json.loads(_design().to_json())
    tampered = {**good, "pin_roles": {**good["pin_roles"], "VIN": "EN"}}
    with pytest.raises(TemplateSeedError, match="altered"):
        BuckDesign.from_payload(tampered)
    for broken in ({**good, "record_kind": "other"}, {**good, "schema_version": 2}, "text", None):
        with pytest.raises(TemplateSeedError, match="record_kind"):
            BuckDesign.from_payload(broken)
    with pytest.raises(TemplateSeedError, match="incomplete"):
        BuckDesign.from_payload({k: v for k, v in good.items() if k != "ports"})


@pytest.mark.parametrize(
    "edit, reason",
    [
        (lambda payload: payload["parameters"][0].update(unit="not-a-unit"), "unit"),
        (lambda payload: payload.update(part=""), "part"),
        (lambda payload: payload.update(spec_digest="not-a-digest"), "spec_digest"),
    ],
)
def test_design_metadata_has_canonical_units_and_identity(edit, reason: str) -> None:
    payload = json.loads(_design().to_json())
    edit(payload)
    with pytest.raises(TemplateSeedError, match=reason):
        BuckDesign.from_payload(payload)


def test_a_design_from_another_renderer_or_contract_is_not_rendered() -> None:
    design = _design()
    other = dataclasses.replace(design, renderer_version="peak_current_buck_render_v0")
    with pytest.raises(TemplateSeedError, match="buck_renderer_version"):
        render_library(other)
    with pytest.raises(TemplateSeedError, match="buck_contract_changed"):
        render_library(dataclasses.replace(design, contract_sha256="0" * 64))


def test_cited_and_default_parameters_are_told_apart() -> None:
    design = _design()
    cited = [item for item in design.parameters if item.origin != "template_default"]
    defaults = [item for item in design.parameters if item.origin == "template_default"]
    assert cited and defaults
    assert all(item.row_id and item.page is not None and item.excerpt for item in cited)
    assert all(item.row_id is None and item.page is None and not item.excerpt for item in defaults)
    assert {item.row_id for item in cited} >= {"R_FSW", "R_VREF"}
    dropped = design_from_spec(_specs()["peak_basis"], unverified={"R_VREF"})
    assert dropped is not None
    vref = next(item for item in dropped.parameters if item.name == "VREF")
    assert vref.origin == "template_default" and vref.row_id is None


def test_sw_and_avg_share_pins_and_parameters() -> None:
    sw, avg = _design("SW"), _design("AVG")
    assert sw.ports == avg.ports
    assert sw.parameters == avg.parameters
    assert sw.payload()["pin_roles"] == avg.payload()["pin_roles"]
    assert sw.payload()["external_bench_parameters"] == []
    assert [item["name"] for item in avg.payload()["external_bench_parameters"]] == ["L_EXT"]
    assert sw.sha256 != avg.sha256
    assert "L_EXT" not in render_library(sw)
    assert ".param L_EXT" in render_library(avg)


def test_a_different_topology_or_no_buck_evidence_gives_no_design() -> None:
    other_pins = ("VIN", "EN", "GND", "VOUT")
    assert design_from_spec(peak_tests._spec(*peak_tests._basis(), ports=other_pins)) is None
    assert design_from_spec(peak_tests._spec(peak_tests._row("R", "A voltage reference"))) is None
    with pytest.raises(TemplateSeedError, match="invalid_mode"):
        design_from_spec(_specs()["peak_basis"], mode="WRONG")


def test_the_record_ties_the_design_to_the_delivered_bytes() -> None:
    design = _design()
    rendered = render_library(design).encode("utf-8")
    exact = design_record_payload(design, rendered)
    assert exact["association"] == "exact"
    digest = hashlib.sha256(rendered).hexdigest()
    assert exact["delivered_library_sha256"] == exact["rendered_library_sha256"] == digest
    assert exact["design_sha256"] == design.sha256
    assert exact["design"] == design.payload()
    assert exact["verdict"] == "UNJUDGED"
    repaired = design_record_payload(design, rendered + b"* an agent edit" + bytes([10]))
    assert repaired["association"] == "invalid_after_change"
    assert repaired["delivered_library_sha256"] != repaired["rendered_library_sha256"]
    assert repaired["design"] == exact["design"]
    assert design_record_payload(design, None)["association"] == "not_delivered"


def test_the_seed_keeps_its_public_payload_and_carries_its_design() -> None:
    seed = seed_from_spec(_specs()["peak_basis"])
    assert seed is not None
    assert seed.design is not None
    assert seed.payload()["parameters"] == [item.payload() for item in seed.design.parameters]
    assert seed.payload()["mode"] == "SW"
    assert seed.payload()["verdict"] == "UNJUDGED"
    assert seed.library_text == render_library(seed.design)


def test_the_ledger_adds_stage_seconds_from_its_clock() -> None:
    from boardmodeler.pipeline.make_model import _Ledger

    ticks = iter([0.0, 1.0, 3.5, 4.0, 5.0, 5.0, 10.0, 12.0])
    ledger = _Ledger(clock=lambda: next(ticks))
    with ledger.stage("read"):
        pass
    with ledger.stage("read"):
        pass
    with ledger.stage("author"):
        pass
    payload = ledger.payload(part="X")
    assert payload["stage_seconds"] == {"read": 3.5, "author": 5.0}
    assert payload["total_seconds"] == 12.0
    assert payload["part"] == "X"


def test_the_ledger_still_records_a_stage_that_raised() -> None:
    from boardmodeler.pipeline.make_model import _Ledger

    ticks = iter([0.0, 1.0, 4.0, 9.0])
    ledger = _Ledger(clock=lambda: next(ticks))
    with pytest.raises(RuntimeError), ledger.stage("extract"):
        raise RuntimeError("boom")
    assert ledger.payload()["stage_seconds"] == {"extract": 3.0}


FROZEN_DIR = Path(__file__).resolve().parents[2] / "models" / "T1-tps54332" / "spec"
FROZEN_SPEC_DIGEST = "499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7"
FROZEN_SW_LIBRARY = "21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2"


@pytest.mark.skipif(
    not (FROZEN_DIR / "requirements.json").is_file(),
    reason="the frozen TPS54332 spec is a local, untracked input (models/T1-tps54332/spec)",
)
def test_the_frozen_tps54332_spec_still_renders_the_frozen_library() -> None:
    spec = load_tps54320_spec(
        FROZEN_DIR / "requirements.json",
        FROZEN_DIR / "bindings.json",
        part="TPS54332DDA",
        subckt="TPS54332DDA",
    )
    assert spec.digest() == FROZEN_SPEC_DIGEST
    design = design_from_spec(spec)
    assert design is not None
    assert design.spec_digest == FROZEN_SPEC_DIGEST
    text = render_library(design)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == FROZEN_SW_LIBRARY
    record = design_record_payload(design, text.encode("utf-8"))
    assert record["association"] == "exact"
