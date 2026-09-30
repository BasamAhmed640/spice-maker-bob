"""Synthetic harness controls test saved-file identity; no device result is fabricated."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from boardmodeler import cli
from boardmodeler.authoring import harness
from boardmodeler.authoring.card import write_symbol_for
from boardmodeler.authoring.probes import PROBES
from boardmodeler.authoring.retest import saved_design_is_exact
from boardmodeler.models import pinout, pwm_controller
from tests.models.test_pwm_controller import source, spec_at


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def saved(tmp_path, monkeypatch):
    spec = spec_at(tmp_path)
    design = pwm_controller.design_from_spec(spec)
    out = tmp_path / "saved"
    (out / "spec").mkdir(parents=True)
    (out / "spec/characteristics.json").write_text(spec.to_json(), encoding="utf-8")
    lib = out / f"{spec.subckt}.lib"
    lib.write_bytes(pwm_controller.render_library(design).encode())
    symbol = lib.with_suffix(".asy")
    write_symbol_for(
        out_path=symbol,
        name=spec.subckt,
        ports=design.ports,
        model_file=lib.name,
        model_name=spec.subckt,
    )
    contract = pinout.freeze_pinout(
        pinout.resolve_reviewed_profile(spec.part, source()[0].file_hash),
        spec,
        source()[0].file_hash,
    )
    (out / "spec/pinout-contract.json").write_text(contract.to_json(), encoding="utf-8")
    write_json(
        out / "pinout-report.json",
        pinout.check_publication(
            contract,
            spec=spec,
            document_sha256=contract.document_sha256,
            library=lib.read_bytes(),
            symbol=symbol.read_bytes(),
            model_file=lib.name,
        ),
    )
    write_json(out / "model-design.json", design.record(lib.read_bytes()))
    write_json(
        out / "template-parameters.json",
        {
            "seed_sha256": hashlib.sha256(lib.read_bytes()).hexdigest(),
            "parameters": design.payload()["parameters"],
        },
    )
    write_json(
        out / "results.json",
        {
            "status": "UNKNOWN",
            "part": spec.part,
            "out_dir": str(out),
            "request": None,
            "rows": [],
            "stages": [],
        },
    )
    write_json(
        out / "reviewed-extraction.json",
        {
            "complete_datasheet_extraction": False,
            "scope": "Reviewed first-order PWM subset",
            "unreviewed_scope": "Prebias servo and temperature corners",
        },
    )
    (out / "MODEL_CARD.md").write_text(
        "## Modeled scope and limitations\n\nPrimary-side controller only; prebias servo unqualified.\n\n## Numerical assumptions\n\nFinite switch edges are numerical regularization.\n\n## Reviewed evidence scope\n\nPartial reviewed extraction.\n",
        encoding="utf-8",
    )
    calls = []

    def fake_harness(**kwargs):
        calls.append(kwargs)
        outcomes = []
        for row in spec.covered():
            value = (
                row.typ_value
                if row.typ_value is not None
                else row.min_value
                if row.min_value is not None
                else row.max_value
            )
            outcomes.append(
                harness.ProbeOutcome(
                    row.probe,
                    "PASS",
                    {PROBES[row.probe].judge_key: value},
                    "TEST_FIXTURE: synthetic observed-value control",
                    None,
                    "synthetic_fixture",
                    (row.char_id,),
                )
            )
        return harness.HarnessReport(
            spec.part, hashlib.sha256(lib.read_bytes()).hexdigest(), spec.digest(), tuple(outcomes)
        )

    monkeypatch.setattr(harness, "run_harness", fake_harness)
    monkeypatch.setattr(
        "boardmodeler.simulation.ltspice.locate",
        lambda: SimpleNamespace(path=tmp_path / "test-fixture.exe"),
    )
    return out, spec, design, lib, symbol, calls


@pytest.mark.parametrize("changed", [False, True])
def test_retest_keeps_numeric_gaps_scope_and_exact_current_receipts(saved, changed):
    out, spec, _design, lib, symbol, calls = saved
    if changed:
        lib.write_bytes(lib.read_bytes() + b"\n* User-edited candidate\n")
    before, symbol_before = lib.read_bytes(), symbol.read_bytes()
    payload, ran = cli._run_model_test(out, timeout_s=2)
    assert ran and len(calls) == 1
    assert payload["status"] == "UNKNOWN" and payload["counts"]["UNKNOWN"] > 0
    assert payload["counts"]["PASS"] == len(spec.covered())
    assert lib.read_bytes() == before and symbol.read_bytes() == symbol_before
    design_record = json.loads((out / "model-design.json").read_text())
    assert design_record["association"] == ("invalid_after_change" if changed else "exact")
    assert design_record["delivered_library_sha256"] == hashlib.sha256(before).hexdigest()
    assert design_record["delivered_symbol_sha256"] == hashlib.sha256(symbol_before).hexdigest()
    receipt = json.loads((out / "pinout-report.json").read_text())
    assert (
        receipt["status"] == "CONFIRMED"
        and receipt["library_sha256"] == hashlib.sha256(before).hexdigest()
    )
    card = (out / "MODEL_CARD.md").read_text()
    assert "prebias servo unqualified" in card and "numerical regularization" in card
    assert "Partial reviewed extraction" in card and "**UNKNOWN**" in card
    results = json.loads((out / "results.json").read_text())
    assert results["status"] == "UNKNOWN" and results["counts"] == payload["counts"]
    assert (out / "retest-history").is_dir()


@pytest.mark.parametrize("mutation", ["limit", "pin_map", "ports", "symbol", "contract", "receipt"])
def test_changed_fixed_inputs_or_pin_confirmation_block_before_simulation(saved, mutation):
    out, spec, _design, lib, symbol, calls = saved
    if mutation in ("limit", "pin_map"):
        data = spec.payload()
        if mutation == "limit":
            data["characteristics"][0]["max_value"] = 999
        else:
            data["pin_map"][0]["physical_pin"] = "99"
        write_json(out / "spec/characteristics.json", data)
    elif mutation == "ports":
        lib.write_bytes(lib.read_bytes().replace(b" VSENSE RT ", b" RT VSENSE ", 1))
    elif mutation == "symbol":
        symbol.write_bytes(symbol.read_bytes().replace(b"SpiceOrder 1\n", b"SpiceOrder 2\n", 1))
    else:
        path = out / (
            "spec/pinout-contract.json" if mutation == "contract" else "pinout-report.json"
        )
        data = json.loads(path.read_text())
        (data["profile"] if mutation == "contract" else data["contract"]["profile"])["scope"] = (
            "User altered source approval"
        )
        write_json(path, data)
    before, symbol_before = lib.read_bytes(), symbol.read_bytes()
    payload, ran = cli._run_model_test(out, timeout_s=2)
    assert not ran and not calls and payload["status"] == "BLOCKED", payload
    assert lib.read_bytes() == before and symbol.read_bytes() == symbol_before
    assert json.loads((out / "pinout-report.json").read_text())["status"] == "BLOCKED"
    assert json.loads((out / "results.json").read_text())["status"] == "BLOCKED"


def test_coedited_design_library_and_hashes_cannot_self_certify_source_values(saved):
    out, spec, design, lib, _symbol, _calls = saved
    payload = design.payload()
    parameter = next(item for item in payload["parameters"] if item["name"] == "VREF")
    parameter["value"] += 0.01
    changed = pwm_controller.PwmControllerDesign.from_payload(payload)
    lib.write_bytes(pwm_controller.render_library(changed).encode())
    record = changed.record(lib.read_bytes())
    assert record["association"] == "exact"
    write_json(out / "model-design.json", record)
    assert not saved_design_is_exact(record, lib.read_bytes(), spec)
    result, ran = cli._run_model_test(out, timeout_s=2)
    assert ran and result["design_association"] == "invalid_after_change"


def test_a_measured_failure_stays_fail_even_with_remaining_unknown_rows(saved, monkeypatch):
    out, _spec, _design, lib, _symbol, calls = saved
    original = harness.run_harness

    def failing(**kwargs):
        report = original(**kwargs)
        first = report.outcomes[0]
        return replace(
            report,
            outcomes=(
                replace(first, status="FAIL", measured={PROBES[first.probe_id].judge_key: 9999.0}),
                *report.outcomes[1:],
            ),
        )

    monkeypatch.setattr(harness, "run_harness", failing)
    payload, ran = cli._run_model_test(out, timeout_s=2)
    assert ran and calls and payload["status"] == "FAIL"
    assert payload["counts"]["FAIL"] > 0 and payload["counts"]["UNKNOWN"] > 0
    assert payload["model_sha256"] == hashlib.sha256(lib.read_bytes()).hexdigest()


def test_legacy_retest_can_repeat_without_inventing_pinout_or_design_provenance(saved):
    out, _spec, _design, _lib, _symbol, calls = saved
    for name in ("model-design.json", "spec/pinout-contract.json", "pinout-report.json"):
        (out / name).unlink()
    for _ in range(2):
        payload, ran = cli._run_model_test(out, timeout_s=2)
        assert (
            ran
            and payload["status"] == "UNKNOWN"
            and payload["design_association"] == "unavailable"
        )
        receipt = json.loads((out / "pinout-report.json").read_text())
        assert not receipt["publication_allowed"] and receipt["contract"] is None
    assert len(calls) == 2


@pytest.mark.parametrize("bad", ["request", "design", "stages", "rows"])
def test_malformed_saved_nested_record_blocks_cleanly(saved, bad):
    out, _spec, _design, _lib, _symbol, calls = saved
    path = out / ("model-design.json" if bad == "design" else "results.json")
    payload = json.loads(path.read_text())
    payload[bad] = None if bad == "stages" else ["malformed fixture"]
    write_json(path, payload)
    result, ran = cli._run_model_test(out, timeout_s=2)
    assert not ran and not calls and result["status"] == "BLOCKED"


def test_pin_only_retest_never_claims_function_even_when_mock_bound_values_pass(saved):
    out, _spec, _design, _lib, _symbol, _calls = saved
    write_json(out / "pin-only-report.json", {"scope": "TEST_FIXTURE: shell control"})
    payload, ran = cli._run_model_test(out, timeout_s=2)
    assert ran and payload["status"] == "UNKNOWN" and payload["counts"]["PASS"] == 0
    assert "no function" in payload["detail"]
    assert "| PASS |" not in (out / "MODEL_CARD.md").read_text()


def test_unverified_citation_cannot_be_laundered_by_repeated_retests(saved):
    out, spec, _design, _lib, _symbol, _calls = saved
    row = spec.covered()[0]
    prior = json.loads((out / "results.json").read_text())
    prior["rows"] = [
        {
            "req_id": row.char_id,
            "required": "citation unverified: synthetic control without verified excerpt",
            "status": "UNKNOWN",
        }
    ]
    write_json(out / "results.json", prior)
    for _ in range(2):
        payload, ran = cli._run_model_test(out, timeout_s=2)
        current = next(item for item in payload["rows"] if item["req_id"] == row.char_id)
        assert ran and current["status"] == "UNKNOWN"
        assert current["required"].startswith("citation unverified:")
        card = (out / "MODEL_CARD.md").read_text()
        card_row = next(line for line in card.splitlines() if line.startswith(f"| `{row.char_id}`"))
        assert "| UNKNOWN |" in card_row and "| PASS |" not in card_row
