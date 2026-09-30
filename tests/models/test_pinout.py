"""Synthetic adversarial pinouts test the gate; these are not electrical device results."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from boardmodeler.authoring.spec import SpecSet
from boardmodeler.models.pinout import (
    PhysicalPin,
    PinoutError,
    PinoutProfile,
    SourceObservation,
    check_publication,
    freeze_pinout,
    resolve_reviewed_profile,
    reviewed_profiles,
)
from boardmodeler.models.symbolism import symbol_text


def synthetic_profile(source_hash="a" * 64):
    """An explicitly synthetic separately supplied source contract, not a product profile."""
    pins = (PhysicalPin("1", "IN", (), 1), PhysicalPin("2", "OUT", (), 2))
    sources = tuple(
        SourceObservation(
            f"synthetic-{i}",
            f"https://example.invalid/test-fixture-{i}",
            digest,
            "synthetic_fixture",
            0,
            "synthetic test table",
            str(i + 3) * 64,
            (("1", "IN"), ("2", "OUT")),
        )
        for i, digest in enumerate((source_hash, "b" * 64))
    )
    return PinoutProfile(
        "synthetic-test-only",
        ("SYNTH",),
        source_hash,
        ("TEST2",),
        "exact_package",
        "TEST2",
        pins,
        sources,
        "two_independent_sources",
        "Synthetic unit-test control only.",
        "No real device evidence.",
    )


def synthetic_spec(pins=None):
    return SpecSet(
        "SYNTH",
        "SYNTH",
        "test-fixture",
        (),
        tuple(pins or ({"physical_pin": "1", "name": "IN"}, {"physical_pin": "2", "name": "OUT"})),
    )


def library(ports=("IN", "OUT")):
    return (
        f"* TEST_FIXTURE\r\n.subckt SYNTH {' '.join(ports)}\r\nR1 IN OUT 1k\r\n.ends SYNTH\r\n"
    ).encode()


def symbol(ports=("IN", "OUT")):
    return symbol_text("SYNTH", ports, model_file="SYNTH.lib").encode()


def test_reviewed_profiles_have_two_distinct_documents_and_explicit_package_scope():
    buck, amplifier, pwm = reviewed_profiles()
    assert buck.selected_package == "DDA" and len(buck.pins) == 9
    assert buck.required_external_connections == (("9", "7"),)
    assert amplifier.selected_package is None
    assert amplifier.package_resolution == "pinout_equivalent_group"
    assert amplifier.packages == ("D", "DGK", "P", "PS", "PW")
    assert pwm.parts == ("UCC28251PW", "UCC28251PWR")
    assert pwm.selected_package == "PW" and pwm.packages == ("PW",)
    assert pwm.required_external_connections == (("1", "7"),)
    assert "SLUA673A" in pwm.confirmation_reference
    assert "substitution" in pwm.confirmation_reference
    for profile in (buck, amplifier, pwm):
        assert len({source.document_sha256 for source in profile.sources}) == 2
        assert all(source.url.startswith("https://www.ti.com/") for source in profile.sources)
        assert all(pin.spice_order == int(pin.number) for pin in profile.pins)
        pins = tuple({"physical_pin": p.number, "name": p.name} for p in profile.pins)
        spec = SpecSet(profile.parts[0], profile.parts[0], "doc", (), pins)
        assert freeze_pinout(profile, spec, profile.primary_document_sha256).pins


@pytest.mark.parametrize(
    "mutation",
    ["swap", "mapped", "missing", "extra", "duplicate", "leading_zero", "part", "package"],
)
def test_pin_map_cannot_self_certify(mutation):
    pins = [{"physical_pin": "1", "name": "IN"}, {"physical_pin": "2", "name": "OUT"}]
    if mutation == "swap":
        pins[0]["physical_pin"], pins[1]["physical_pin"] = "2", "1"
    elif mutation == "mapped":
        pins[0]["mapped_symbol_pin"] = "OUT"
    elif mutation == "missing":
        pins.pop()
    elif mutation == "extra":
        pins.append({"physical_pin": "3", "name": "PAD"})
    elif mutation == "duplicate":
        pins[1]["physical_pin"] = "1"
    elif mutation == "leading_zero":
        pins[1]["physical_pin"] = "02"
    elif mutation == "part":
        pins[0]["part_id"] = "DIFFERENT"
    else:
        pins[0]["package_resolution"] = "unresolved"
    with pytest.raises(PinoutError):
        freeze_pinout(synthetic_profile(), synthetic_spec(pins), "a" * 64)


@pytest.mark.parametrize("part", ["TPS54331", "LM358B", "LM358DDF", "LM358FK", "UNREVIEWED_MOSFET"])
def test_unreviewed_part_or_package_is_not_inferred_from_similar_pin_names(part):
    with pytest.raises(PinoutError, match="confirmation_missing"):
        resolve_reviewed_profile(part, reviewed_profiles()[1].primary_document_sha256)


def test_profile_rejects_alias_collisions_duplicate_source_and_noncanonical_numbers():
    profile = synthetic_profile()
    with pytest.raises(PinoutError, match="alias_ambiguous"):
        replace(profile, pins=(profile.pins[0], replace(profile.pins[1], aliases=("IN",))))
    with pytest.raises(PinoutError, match="independent_source_missing"):
        replace(profile, sources=(profile.sources[0], profile.sources[0]))
    with pytest.raises(PinoutError, match="not_canonical"):
        replace(profile, pins=(profile.pins[0], replace(profile.pins[1], number="02")))
    with pytest.raises(PinoutError, match="source_changed"):
        resolve_reviewed_profile("LM358", "f" * 64)


@pytest.mark.parametrize(
    "mutation",
    [
        "joint_permutation",
        "symbol_order",
        "model_name",
        "extra_attribute",
        "duplicate_model",
        "cross_block_order",
        "source",
        "spec",
        "model_ports",
    ],
)
def test_publication_compares_independent_mapping_and_exact_symbol_binding(mutation):
    spec = synthetic_spec()
    contract = freeze_pinout(synthetic_profile(), spec, "a" * 64)
    lib, asy, source_hash = library(), symbol(), "a" * 64
    if mutation == "joint_permutation":
        lib, asy = library(("OUT", "IN")), symbol(("OUT", "IN"))
    elif mutation == "symbol_order":
        asy = symbol(("OUT", "IN"))
    elif mutation == "model_name":
        asy = asy.replace(b"Value2 SYNTH", b"Value2 OTHER")
    elif mutation == "extra_attribute":
        asy += b"PINATTR PinName SURPLUS\n"
    elif mutation == "duplicate_model":
        asy += b"SYMATTR SpiceModel EVIL.lib\n"
    elif mutation == "cross_block_order":
        asy = asy.replace(
            b"PINATTR SpiceOrder 1\n", b"PINATTR SpiceOrder 1\nPINATTR SpiceOrder 2\n", 1
        )
        index = asy.rfind(b"PINATTR SpiceOrder 2\n")
        asy = asy[:index] + asy[index + len(b"PINATTR SpiceOrder 2\n") :]
    elif mutation == "source":
        source_hash = "c" * 64
    elif mutation == "spec":
        spec = replace(spec, doc_id="different")
    else:
        lib = library(("IN", "OUT", "EXTRA"))
    report = check_publication(
        contract,
        spec=spec,
        document_sha256=source_hash,
        library=lib,
        symbol=asy,
        model_file="SYNTH.lib",
    )
    assert report["status"] == "BLOCKED" and not report["publication_allowed"]
    assert report["reason"]


def test_clean_receipt_is_source_and_artifact_bound_and_has_no_footprint_claim():
    import hashlib

    spec = synthetic_spec()
    contract = freeze_pinout(synthetic_profile(), spec, "a" * 64)
    report = check_publication(
        contract,
        spec=spec,
        document_sha256="a" * 64,
        library=library(),
        symbol=symbol(),
        model_file="SYNTH.lib",
    )
    assert report["publication_allowed"] and report["status"] == "CONFIRMED"
    assert report["contract_sha256"] == contract.digest()
    assert report["library_sha256"] == hashlib.sha256(library()).hexdigest()
    assert report["symbol_sha256"] == hashlib.sha256(symbol()).hexdigest()
    assert report["physical_footprint_selected"] is False
    assert [(p["physical_pin"], p["observed_spice_order"]) for p in report["mapping"]] == [
        ("1", 1),
        ("2", 2),
    ]
    assert report["checks"]["PIN-05"] == "NOT_APPLICABLE"


def test_discrete_does_not_gain_confirmation_without_primitive_terminal_order():
    spec = synthetic_spec()
    contract = freeze_pinout(replace(synthetic_profile(), device_kind="discrete"), spec, "a" * 64)
    report = check_publication(
        contract,
        spec=spec,
        document_sha256="a" * 64,
        library=library(),
        symbol=symbol(),
        model_file="SYNTH.lib",
    )
    assert not report["publication_allowed"]
    assert "discrete_terminal_contract_missing" in report["reason"]


def test_installer_includes_reviewed_pinout_data():
    import ast

    spec = Path(__file__).resolve().parents[2] / "installer" / "SpiceMaker.spec"
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    analysis = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    )
    datas = next(keyword.value for keyword in analysis.keywords if keyword.arg == "datas")
    assert any(
        isinstance(entry, ast.Tuple)
        and isinstance(entry.elts[1], ast.Constant)
        and entry.elts[1].value == "boardmodeler/models"
        and any(
            isinstance(node, ast.Constant) and node.value == "reviewed_pinouts.json"
            for node in ast.walk(entry.elts[0])
        )
        for entry in datas.elts
    )
