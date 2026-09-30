"""Source-led PWM design checks and explicitly synthetic instrument controls."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from boardmodeler.authoring import pwm_probes as pwm
from boardmodeler.authoring import ucc28251_reference as reference
from boardmodeler.authoring.harness import _judge
from boardmodeler.authoring.probes import PROBES, ProbeError
from boardmodeler.authoring.spec import load_tps54320_spec
from boardmodeler.domain.records import DocumentRecord
from boardmodeler.models.pwm_controller import (
    PwmControllerDesign,
    PwmControllerDesignError,
    design_from_spec,
    render_library,
)
from boardmodeler.requirements.review import verify_citations
from boardmodeler.simulation.ltspice import run_batch

FIXTURE = Path(__file__).parents[2] / "fixtures/pwm/ucc28251/reviewed-pages.json"


def source(part="UCC28251PW"):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record = DocumentRecord(
        doc_id="TI_UCC28251_REVE",
        title="UCC28251",
        doc_type="datasheet",
        file_hash=payload["source_sha256"],
        provenance="user_supplied",
        page_count=58,
        text_extraction="embedded",
    )
    pages = {int(key): text for key, text in payload["pages"].items()}
    return record, pages, reference.records(record, pages, part=part)


def spec_at(tmp_path, part="UCC28251PW"):
    record, _pages, (rows, bindings, pins, _metadata) = source(part)
    req, bind = tmp_path / "requirements.json", tmp_path / "bindings.json"
    req.write_text(
        json.dumps(
            {
                "document": record.model_dump(mode="json"),
                "requirements": [row.model_dump(mode="json", by_alias=True) for row in rows],
                "pin_map": pins,
            }
        ),
        encoding="utf-8",
    )
    bind.write_text(
        json.dumps({"part": part, "subckt": part, "doc_id": record.doc_id, "bindings": bindings}),
        encoding="utf-8",
    )
    return load_tps54320_spec(req, bind, part=part, subckt=part)


def test_reader_requires_exact_pdf_and_explicit_package():
    assert reference.matches(" ucc28251pw ", reference.DATASHEET_SHA256)
    assert not reference.matches("UCC28251", reference.DATASHEET_SHA256)
    assert not reference.matches("UCC28251PW", "0" * 64)
    record, pages, _result = source()
    pages[6] = pages[6].replace("0.497 0.505 0.513", "0.497 0.605 0.613")
    with pytest.raises(ValueError, match="citation_missing"):
        reference.records(record, pages, part="UCC28251PW")


def test_source_columns_and_scope_are_independent_of_candidate():
    record, pages, (rows, _bindings, pins, metadata) = source()
    by_id = {row.req_id: row for row in rows}
    for name, expected in {
        "ILIM": (0.497, 0.505, 0.513),
        "VREF": (3.17, 3.25, 3.33),
        "UVLO_RISE": (4.0, 4.3, 4.65),
        "UVLO_FALL": (3.8, 4.1, 4.4),
        "ISS": (26e-6, 28e-6, 30e-6),
        "FREQUENCY": (90e3, 98e3, 106e3),
    }.items():
        limits = by_id["UCC28251_" + name].limits
        assert (limits.min, limits.typ, limits.max) == expected
    verified = verify_citations(
        rows, {record.doc_id: record}, excerpt_lookup=lambda _doc, page: pages[page]
    )
    assert all(verified.values())
    assert len(by_id["UCC28251_ALTERNATING"].evidence) == 3
    assert [pin["name"] for pin in pins] == [
        "VSENSE",
        "RT",
        "RAMP/CS",
        "ILIM",
        "EN",
        "OVP/OTP",
        "VREF",
        "REF/EA+",
        "FB/EA-",
        "COMP",
        "GND",
        "VDD",
        "SRB",
        "SRA",
        "OUTB",
        "OUTA",
        "HICC",
        "PS",
        "SP",
        "SS",
    ]
    assert not metadata["complete_datasheet_extraction"]
    assert metadata["qualified_probes_added"] == 15
    for omitted in (
        "SYNC",
        "PULSE_ENABLE",
        "PREBIAS",
        "HICCUP",
        "THERMAL",
        "SR_STARTUP",
        "MINIMUM_PULSE",
    ):
        assert by_id["UCC28251_" + omitted].req_class.value == "UNKNOWN"
    assert by_id["UCC28251_COMP_START"].req_class.value == "TYPICAL_VALUE"


def test_rgps_numeric_pinout_is_distinct_and_propagation_row_package_specific():
    _record, _pages, (rows, _bindings, pins, _meta) = source("UCC28251RGP")
    assert pins[0]["name"] == "REF/EA+" and pins[13]["name"] == "VSENSE"
    delay = next(row for row in rows if row.req_id == "UCC28251_ILIM_DELAY")
    assert (delay.limits.min, delay.limits.typ, delay.limits.max) == (15e-9, 25e-9, 36e-9)
    assert "UCC28251RGP" in delay.evidence[0].excerpt


def test_design_roundtrip_mutation_isolation_and_delivered_byte_identity(tmp_path):
    design = design_from_spec(spec_at(tmp_path))
    assert design is not None
    text = render_library(design)
    assert render_library(PwmControllerDesign.from_payload(design.payload())) == text
    assert PwmControllerDesign.from_json(design.to_json()) == design
    altered = design.payload()
    altered["numerical_assumptions"][0]["value"] = 999
    assert design.payload()["numerical_assumptions"][0]["value"] == 1.0
    assert design.record(text.encode())["association"] == "exact"
    assert design.record((text + "* repair\n").encode())["association"] == "invalid_after_change"
    assert design.record(None)["association"] == "not_delivered"


@pytest.mark.parametrize(
    "changes",
    [
        {"page": -1},
        {"page": True},
        {"origin": "template_default"},
        {"row_id": "OTHER_ILIM"},
        {"transform": "guess"},
        {"value": float("nan")},
    ],
)
def test_design_refuses_invalid_or_uncited_value_origins(tmp_path, changes):
    design = design_from_spec(spec_at(tmp_path))
    parameters = tuple(
        dataclasses.replace(item, **changes) if item.name == "ILIM" else item
        for item in design.parameters
    )
    with pytest.raises(PwmControllerDesignError):
        dataclasses.replace(design, parameters=parameters)


def test_uncited_input_and_reordered_physical_ports_block_design(tmp_path):
    spec = spec_at(tmp_path)
    with pytest.raises(PwmControllerDesignError, match="missing_cited_input:ILIM"):
        design_from_spec(spec, unverified={"UCC28251_ILIM"})
    design = design_from_spec(spec)
    with pytest.raises(PwmControllerDesignError, match="physical_pin_order"):
        dataclasses.replace(design, ports=tuple(reversed(design.ports)))


def test_frozen_fixture_does_not_change_with_candidate_parameters(tmp_path):
    design = design_from_spec(spec_at(tmp_path))
    lib = tmp_path / "candidate.lib"
    lib.write_text(render_library(design), encoding="utf-8")
    before = {
        name: PROBES["pwm_" + name].render(model_lib=lib, subckt=design.subckt, params={})
        for name, _unit in pwm.KINDS
    }
    changed = dataclasses.replace(
        design,
        parameters=tuple(
            dataclasses.replace(item, value=0.496) if item.name == "ILIM" else item
            for item in design.parameters
        ),
    )
    lib.write_text(render_library(changed), encoding="utf-8")
    for name, fixture in before.items():
        assert (
            PROBES["pwm_" + name].render(model_lib=lib, subckt=design.subckt, params={}) == fixture
        )
    assert "Vramp ramp_cs 0 0.1" in before["ilim"]
    assert "3.995" in before["uvlo_rise"]


class SyntheticWave:
    """A synthetic instrument control, never presented as device evidence."""

    def __init__(self, time, nodes):
        self.t, self.nodes = time, nodes

    def y(self, name):
        return self.nodes[name]


def synthetic_ilim(threshold, width=4.90e-6, delay=25e-9):
    levels = pwm.THRESHOLD_LEVELS["ilim"]
    time = np.arange(0, len(levels) * pwm.UVLO_HOLD + 4e-9, 4e-9)
    index = np.minimum((time / pwm.UVLO_HOLD).astype(int), len(levels) - 1)
    ilim = np.asarray(levels)[index]
    delayed = np.interp(time - delay, time, ilim, left=ilim[0])
    phase = np.mod(time, 5.023e-6)
    channel = np.floor(time / 5.023e-6).astype(int) % 2
    pulse = (phase >= 43e-9) & (phase < 43e-9 + np.where(delayed >= threshold, 60e-9, width))
    return SyntheticWave(
        time,
        {
            "V(ilim)": ilim,
            "V(outa)": 12 * pulse * (channel == 0),
            "V(outb)": 12 * pulse * (channel == 1),
        },
    )


def synthetic_plateaus(kind, threshold, stimulus_offset=0.0):
    levels = pwm.THRESHOLD_LEVELS[kind]
    time = np.arange(0, len(levels) * pwm.UVLO_HOLD + 1e-8, 1e-8)
    indices = np.minimum((time / pwm.UVLO_HOLD).astype(int), len(levels) - 1)
    actual = np.asarray(levels)[indices] + stimulus_offset
    on = (
        actual >= threshold
        if kind in {"uvlo_rise", "enable"}
        else actual > threshold
        if kind == "uvlo_fall"
        else actual < threshold
    )
    phase = np.mod(time, 5.023e-6)
    channel = np.floor(time / 5.023e-6).astype(int) % 2
    # Fixed35us startup latency is intentionally present. Each75us source
    # plateau settles before the measuring window, so it cannot hide a fault.
    settled = time - indices * pwm.UVLO_HOLD > 35e-6
    pulse = (phase > 50e-9) & (phase < 1e-6) & on & settled
    return SyntheticWave(
        time,
        {
            pwm.THRESHOLD_INPUTS[kind]: actual,
            "V(outa)": 12 * pulse * (channel == 0),
            "V(outb)": 12 * pulse * (channel == 1),
        },
    )


@pytest.mark.parametrize(
    "kind,threshold",
    [
        ("uvlo_rise", 3.99),
        ("uvlo_rise", 4.66),
        ("uvlo_fall", 3.79),
        ("uvlo_fall", 4.41),
        ("enable", 1.49),
        ("enable", 2.26),
        ("ovp", 0.659),
        ("ovp", 0.741),
    ],
)
def test_synthetic_plateau_threshold_faults_fail_unchanged_bounds(monkeypatch, kind, threshold):
    monkeypatch.setattr(
        "boardmodeler.authoring.probes._load", lambda *_: synthetic_plateaus(kind, threshold)
    )
    result = pwm.measure(kind, Path("synthetic-only.raw"), {})
    minimum, maximum = pwm.THRESHOLD_BOUNDS[kind]
    assert (
        result["observed_interval_high_v"] < minimum or result["observed_interval_low_v"] > maximum
    )


@pytest.mark.parametrize(
    "kind,threshold,offset",
    [
        ("enable", 1.49999, -90e-6),
        ("ovp", 0.74001, 90e-6),
        ("enable", 1.499, 0),
        ("ovp", 0.74001, 0),
    ],
)
def test_synthetic_plateau_offset_cannot_hide_near_bound_fault(
    monkeypatch, kind, threshold, offset
):
    monkeypatch.setattr(
        "boardmodeler.authoring.probes._load",
        lambda *_: synthetic_plateaus(kind, threshold, offset),
    )
    with pytest.raises(ProbeError, match="threshold_interval_straddles_limit"):
        pwm.measure(kind, Path("synthetic-only.raw"), {})


@pytest.mark.parametrize("threshold", [0.496, 0.514])
@pytest.mark.parametrize("width", [1.1e-6, 2.5e-6, 4.9e-6])
def test_synthetic_ilim_wrong_value_never_passes(monkeypatch, threshold, width):
    monkeypatch.setattr(
        "boardmodeler.authoring.probes._load", lambda *_: synthetic_ilim(threshold, width)
    )
    try:
        result = pwm.measure("ilim", Path("synthetic-only.raw"), {})
    except ProbeError as exc:
        assert "interval_straddles" in exc.reason
    else:
        assert result["pwm_value"] < 0.497 or result["pwm_value"] > 0.513


@pytest.mark.parametrize("threshold", [0.49697, 0.51301])
def test_synthetic_ilim_boundary_uncertainty_is_unknown(monkeypatch, threshold):
    monkeypatch.setattr("boardmodeler.authoring.probes._load", lambda *_: synthetic_ilim(threshold))
    with pytest.raises(ProbeError, match="threshold_interval_straddles_limit"):
        pwm.measure("ilim", Path("synthetic-only.raw"), {})


def test_static_ilim_boundary_and_combined_delay_never_false_pass(monkeypatch):
    monkeypatch.setattr("boardmodeler.authoring.probes._load", lambda *_: synthetic_ilim(0.513))
    result = pwm.measure("ilim", Path("synthetic-only.raw"), {})
    assert 0.497 <= result["pwm_value"] <= 0.513
    monkeypatch.setattr(
        "boardmodeler.authoring.probes._load", lambda *_: synthetic_ilim(0.496, delay=2e-6)
    )
    result = pwm.measure("ilim", Path("synthetic-only.raw"), {})
    assert result["observed_interval_high_v"] < 0.497


@pytest.mark.ltspice
def test_actual_pwm_and_wrong_candidates_keep_the_same_frozen_limits(tmp_path, ltspice_exe):
    """All fifteen actual checks, then near-bound, long-delay and overlap controls."""
    spec = spec_at(tmp_path)
    design = design_from_spec(spec)
    baseline = render_library(design)
    rows = {row.probe: row for row in spec.covered()}

    def run(name, kind, text):
        work = tmp_path / name
        work.mkdir()
        lib, deck = work / "candidate.lib", work / "bench.cir"
        lib.write_text(text, encoding="utf-8")
        probe = PROBES["pwm_" + kind]
        deck.write_text(
            probe.render(model_lib=lib, subckt=design.subckt, params={}), encoding="utf-8"
        )
        outcome = run_batch(ltspice_exe, deck, work, timeout_s=120)
        assert outcome.exit_code == 0 and outcome.raw_path and not outcome.timed_out
        measured = probe.measure(outcome.raw_path, probe.merged_params({}))
        return _judge(rows[probe.probe_id], "pwm_value", measured["pwm_value"])[0]

    for kind, _unit in pwm.KINDS:
        assert run("nominal_" + kind, kind, baseline) == "PASS"
    for name, kind, substitutions in (
        (
            "rise_below",
            "uvlo_rise",
            {"UVLO_RISE=4.3": "UVLO_RISE=3.99", "UVLO_FALL=4.1": "UVLO_FALL=3.7"},
        ),
        (
            "fall_above",
            "uvlo_fall",
            {"UVLO_RISE=4.3": "UVLO_RISE=4.7", "UVLO_FALL=4.1": "UVLO_FALL=4.41"},
        ),
        ("ilim_below", "ilim", {"ILIM=0.505": "ILIM=0.496"}),
        ("ilim_above", "ilim", {"ILIM=0.505": "ILIM=0.514"}),
        ("enable_below", "enable", {"EN_THRESHOLD=2": "EN_THRESHOLD=1.49"}),
        ("enable_above", "enable", {"EN_THRESHOLD=2": "EN_THRESHOLD=2.26"}),
        ("ovp_below", "ovp", {"OVP=0.7": "OVP=0.659"}),
        ("ovp_above", "ovp", {"OVP=0.7": "OVP=0.741"}),
        (
            "ilim_long_delay",
            "ilim",
            {"ILIM=0.505": "ILIM=0.496", "ILIM_DELAY=2.5e-08": "ILIM_DELAY=2e-06"},
        ),
        (
            "ilim_long_delay_response",
            "ilim_delay",
            {"ILIM=0.505": "ILIM=0.496", "ILIM_DELAY=2.5e-08": "ILIM_DELAY=2e-06"},
        ),
        (
            "sr_overlap",
            "alternating",
            {
                "(V(ga,GND)>0.5) | (delay(V(ga,GND),V(dtps,GND))>0.5)": "(delay(V(ga,GND),V(dtps,GND))>0.5)",
                "(V(gb,GND)>0.5) | (delay(V(gb,GND),V(dtps,GND))>0.5)": "(delay(V(gb,GND),V(dtps,GND))>0.5)",
            },
        ),
    ):
        wrong = baseline
        for old, new in substitutions.items():
            assert old in wrong
            wrong = wrong.replace(old, new)
        assert run(name, kind, wrong) == "FAIL"
