"""Publishing a resumed candidate must not leave the previous design marked exact."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from tests.models.op_amp_spec import synthetic_dual_op_amp_spec
from tests.models.test_peak_current_buck import _basis, _spec

from boardmodeler.authoring.harness import HarnessReport
from boardmodeler.models import op_amp
from boardmodeler.models.buck_switching import seed_from_spec
from boardmodeler.pipeline import make_model as engine


def publish_run(tmp_path, family="buck"):
    spec = _spec(*_basis()) if family == "buck" else synthetic_dual_op_amp_spec()
    request = engine.MakeModelRequest(
        spec.part, spec.subckt, tmp_path / "unused.pdf", tmp_path / "out"
    )
    run = engine._Run(request, engine._StageLog(None))
    run.spec = spec
    seed = seed_from_spec(spec) if family == "buck" else op_amp.seed_from_spec(spec)
    assert seed is not None
    source = run.workdir / "model" / f"{spec.subckt}.lib"
    seed.write(source)
    run.report = HarnessReport(
        part=spec.part,
        model_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        spec_digest=spec.digest(),
        outcomes=(),
    )
    return run, seed, source


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("family", ["buck", "op_amp"])
def test_resumed_publication_refreshes_library_symbol_and_parameter_provenance(
    tmp_path, changed, family
):
    run, seed, source = publish_run(tmp_path, family)
    initial = seed.library_text.encode("utf-8")
    run._write_json(run.out_dir / engine.DESIGN_RECORD_NAME, seed.design.record(initial))
    run._write_json(
        run.out_dir / "template-parameters.json",
        {
            "seed_sha256": hashlib.sha256(initial).hexdigest(),
            "final_model_sha256": hashlib.sha256(initial).hexdigest(),
            "final_model_matches_seed": True,
        },
    )
    if changed:
        source.write_text(seed.library_text + "\n* resumed legacy repair\n", encoding="utf-8")
        run.report = HarnessReport(
            part=run.spec.part,
            model_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            spec_digest=run.spec.digest(),
            outcomes=(),
        )
    assert run.template_seed is None and run.template_design is None
    run._publish(source, [])
    record = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert record["association"] == ("invalid_after_change" if changed else "exact")
    assert (
        record["delivered_library_sha256"] == hashlib.sha256(run.lib_path.read_bytes()).hexdigest()
    )
    assert (
        record["delivered_symbol_sha256"] == hashlib.sha256(run.asy_path.read_bytes()).hexdigest()
    )
    assert record["delivered_pin_order"] == list(seed.design.ports)
    assert record["design"] == seed.design.payload()
    assert record["verdict"] == "UNJUDGED"
    parameters = json.loads((run.out_dir / "template-parameters.json").read_text())
    assert parameters["final_model_matches_seed"] is not changed
    assert parameters["final_model_sha256"] == record["delivered_library_sha256"]


@pytest.mark.parametrize("family", ["buck", "op_amp"])
@pytest.mark.parametrize(
    "mutation",
    ["value", "value_rehashed", "digest", "renderer", "shape", "pins", "record_version", "spec"],
)
def test_resumed_publication_rejects_altered_saved_design(tmp_path, family, mutation):
    run, seed, source = publish_run(tmp_path, family)
    delivered = source.read_bytes()
    previous = seed.design.record(delivered)
    payload = previous["design"]
    if mutation in ("value", "value_rehashed"):
        name = "VREF" if family == "buck" else "VOS"
        next(item for item in payload["parameters"] if item["name"] == name)["value"] *= 0.9
        if mutation == "value_rehashed":
            previous["design_sha256"] = type(seed.design).from_payload(payload).sha256
    elif mutation == "digest":
        previous["design_sha256"] = "0" * 64
    elif mutation == "renderer":
        payload["renderer_version"] = "unsupported-old-renderer"
    elif mutation == "shape":
        payload["parameters"] = [None]
    elif mutation == "pins":
        payload["ports"].reverse()
    elif mutation == "record_version":
        previous["schema_version"] = 999
    else:
        run.spec = replace(run.spec, doc_id="a-different-frozen-spec")
        run.report = replace(run.report, spec_digest=run.spec.digest())
    run._write_json(run.out_dir / engine.DESIGN_RECORD_NAME, previous)

    run._publish(source, [])

    record = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert record["association"] == "invalid_after_change"
    assert record["design"] == payload
    assert run.lib_path.read_bytes() == delivered
    assert record["delivered_library_sha256"] == hashlib.sha256(delivered).hexdigest()


@pytest.mark.parametrize("previous", [None, "bad json", "[]"])
def test_publication_without_usable_design_records_unavailable(tmp_path, previous):
    run, _seed, source = publish_run(tmp_path)
    if previous is not None:
        run._write_text(run.out_dir / engine.DESIGN_RECORD_NAME, previous)
    run._publish(source, [])
    record = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert record["association"] == "unavailable"
    assert record["design"] is None
    assert (
        record["delivered_library_sha256"] == hashlib.sha256(run.lib_path.read_bytes()).hexdigest()
    )


def test_new_seed_record_binds_delivered_symbol_and_pin_order(tmp_path):
    run, seed, source = publish_run(tmp_path)
    run.template_design = seed.design
    run.template_seed = seed.payload()
    run.template_seed_bytes = seed.library_text.encode("utf-8")
    run._publish(source, [])
    record = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert record["association"] == "exact"
    assert record["delivered_pin_order"] == list(seed.ports)
    assert (
        record["delivered_symbol_sha256"] == hashlib.sha256(run.asy_path.read_bytes()).hexdigest()
    )
