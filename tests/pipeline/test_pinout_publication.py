"""Real pipeline pinout boundary with explicitly synthetic independent source contracts."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace

import pytest
from tests.models.test_pinout import library, synthetic_profile, synthetic_spec

from boardmodeler.authoring.harness import HarnessReport
from boardmodeler.domain.records import DocumentRecord
from boardmodeler.models import pinout
from boardmodeler.pipeline import make_model as engine


def test_concurrent_build_cannot_archive_or_rewrite_existing_outputs(tmp_path):
    """The public entry point refuses before any output mutation on every route."""
    entered = threading.Event()
    release = threading.Event()

    def hold_build_lock():
        with engine._BUILD_LOCK:
            entered.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold_build_lock)
    worker.start()
    assert entered.wait(10)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"TEST_FIXTURE")
    output = tmp_path / "out"
    output.mkdir()
    old_library = output / "SYNTH.lib"
    old_library.write_bytes(b"previous delivered file")
    try:
        for route in ("behavioral", "pin_only", "legacy_ai"):
            result = engine.make_model(
                engine.MakeModelRequest("SYNTH", "SYNTH", source, output, engine=route)
            )
            assert result.status == "BLOCKED"
            assert "build_in_progress" in result.detail
            assert old_library.read_bytes() == b"previous delivered file"
            assert list(output.iterdir()) == [old_library]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def _run(tmp_path, monkeypatch):
    datasheet = tmp_path / "synthetic-source.pdf"
    datasheet.write_bytes(b"TEST_FIXTURE: synthetic source; not a device PDF")
    digest = hashlib.sha256(datasheet.read_bytes()).hexdigest()
    profile = synthetic_profile(digest)
    # Supply only the independently confirmed test profile. The complete production
    # source/map/freeze/publication logic runs; no gate method is mocked here.
    monkeypatch.setattr(pinout, "resolve_reviewed_profile", lambda part, doc: profile)
    run = engine._Run(
        engine.MakeModelRequest("SYNTH", "SYNTH", datasheet, tmp_path / "out"),
        engine._StageLog(None),
    )
    run.record = DocumentRecord(
        doc_id="test-fixture",
        title="Synthetic fixture",
        doc_type="datasheet",
        file_hash=digest,
        provenance="synthetic_fixture",
    )
    run.spec = synthetic_spec()
    run.pin_map = run.spec.pin_map
    source = run.workdir / "model" / "SYNTH.lib"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(library())
    run.report = HarnessReport(
        "SYNTH", hashlib.sha256(source.read_bytes()).hexdigest(), run.spec.digest(), ()
    )
    return run, source


def test_confirmed_publication_keeps_exact_crlf_bytes_and_reports_sources(tmp_path, monkeypatch):
    run, source = _run(tmp_path, monkeypatch)
    run.freeze_pinout()
    assert run.pinout_report["status"] == "SOURCE_CONFIRMED"
    assert not run.pinout_report["publication_allowed"]
    before = source.read_bytes()
    measured = run.report
    run.save()
    assert run.lib_path is not None, run.detail
    assert run.lib_path.read_bytes() == before
    assert run.report == measured
    receipt = json.loads((run.out_dir / engine.PINOUT_REPORT_NAME).read_text(encoding="utf-8"))
    assert receipt["status"] == "CONFIRMED" and receipt["publication_allowed"]
    assert receipt["library_sha256"] == measured.model_sha256
    assert receipt["symbol_sha256"] == hashlib.sha256(run.asy_path.read_bytes()).hexdigest()
    assert receipt["contract_sha256"] == run.pinout_contract.digest()
    assert len(receipt["contract"]["profile"]["sources"]) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "live_map",
        "spec_map",
        "source",
        "saved_contract",
        "contract_profile",
        "symbol",
        "late_symbol",
    ],
)
def test_changed_identity_is_withheld_with_diagnostic_receipt(tmp_path, monkeypatch, mutation):
    run, source = _run(tmp_path, monkeypatch)
    run.freeze_pinout()
    if mutation == "live_map":
        run.pin_map = ({"physical_pin": "2", "name": "IN"}, {"physical_pin": "1", "name": "OUT"})
    elif mutation == "spec_map":
        run.spec = replace(run.spec, pin_map=tuple(reversed(run.spec.pin_map)))
        run.pin_map = run.spec.pin_map
        run.report = replace(run.report, spec_digest=run.spec.digest())
    elif mutation == "source":
        run.request.datasheet.write_bytes(b"different source")
    elif mutation == "saved_contract":
        (run.spec_dir / engine.PINOUT_CONTRACT_NAME).write_text("{}", encoding="utf-8")
    elif mutation == "contract_profile":
        changed = replace(run.pinout_contract.profile, scope="Unapproved new claims")
        run.pinout_contract = replace(run.pinout_contract, profile=changed)
        (run.spec_dir / engine.PINOUT_CONTRACT_NAME).write_text(
            run.pinout_contract.to_json(), encoding="utf-8"
        )
    elif mutation == "symbol":
        original = run._publish_symbol

        def bad_symbol(*args, **kwargs):
            path, note = original(*args, **kwargs)
            path.write_bytes(path.read_bytes().replace(b"Value2 SYNTH", b"Value2 DIFFERENT"))
            return path, note

        monkeypatch.setattr(run, "_publish_symbol", bad_symbol)
    else:
        original = run._publish_design_record

        def late_change(*args, **kwargs):
            original(*args, **kwargs)
            run.asy_path.write_bytes(run.asy_path.read_bytes() + b"* changed after confirmation\n")

        monkeypatch.setattr(run, "_publish_design_record", late_change)
    run.save()
    assert run.lib_path is None and not (run.out_dir / "SYNTH.lib").exists()
    assert not (run.out_dir / "SYNTH.asy").exists()
    receipt = json.loads((run.out_dir / engine.PINOUT_REPORT_NAME).read_text(encoding="utf-8"))
    assert receipt["status"] == "BLOCKED" and not receipt["publication_allowed"]
    assert receipt["reason"]
    assert source.exists(), "diagnostic candidate is retained"


@pytest.mark.parametrize("route", ["behavioral", "legacy_ai", "pin_only"])
@pytest.mark.parametrize("verification", ["full", "sanity"])
def test_no_route_can_publish_without_a_frozen_pinout(tmp_path, monkeypatch, route, verification):
    run, _source = _run(tmp_path, monkeypatch)
    run.request = replace(run.request, engine=route, verification=verification)
    run.sanity_ok = True
    run.save()
    assert run.lib_path is None
    receipt = json.loads((run.out_dir / engine.PINOUT_REPORT_NAME).read_text(encoding="utf-8"))
    assert receipt["status"] == "BLOCKED" and not receipt["publication_allowed"]
    assert "confirmation_missing" in receipt["reason"]


def test_refused_rerun_archives_old_outputs_and_pinout_receipts(tmp_path, monkeypatch):
    prior, _source = _run(tmp_path, monkeypatch)
    prior.freeze_pinout()
    prior.save()
    assert prior.lib_path
    old_receipt = (prior.out_dir / engine.PINOUT_REPORT_NAME).read_bytes()
    old_contract = (prior.spec_dir / engine.PINOUT_CONTRACT_NAME).read_bytes()

    def read(self):
        self.record, self.spec = prior.record, prior.spec
        self.pin_map = ({"physical_pin": "2", "name": "IN"}, {"physical_pin": "1", "name": "OUT"})
        self.spec = replace(self.spec, pin_map=self.pin_map)

    monkeypatch.setattr(engine._Run, "read", read)
    monkeypatch.setattr(engine._Run, "extract", lambda *_: None)
    monkeypatch.setattr(engine._Run, "bind", lambda *_: None)
    monkeypatch.setattr(engine._Run, "support_gate", lambda *_: None)
    monkeypatch.setattr(
        engine._Run, "author", lambda *_: pytest.fail("unconfirmed pinout reached author")
    )
    result = engine.make_model(prior.request)
    assert result.status == "BLOCKED" and result.lib_path is None
    assert not (prior.out_dir / "SYNTH.lib").exists()
    receipt = json.loads((prior.out_dir / engine.PINOUT_REPORT_NAME).read_text(encoding="utf-8"))
    assert not receipt["publication_allowed"] and "number_name_mismatch" in receipt["reason"]
    histories = list((prior.workdir / "publication-history").glob("*"))
    assert any(
        (path / engine.PINOUT_REPORT_NAME).is_file()
        and (path / engine.PINOUT_REPORT_NAME).read_bytes() == old_receipt
        for path in histories
    )
    assert any(
        (path / "spec" / engine.PINOUT_CONTRACT_NAME).is_file()
        and (path / "spec" / engine.PINOUT_CONTRACT_NAME).read_bytes() == old_contract
        for path in histories
    )
    assert not (prior.spec_dir / engine.PINOUT_CONTRACT_NAME).exists()


def test_real_reviewed_lm358_minimal_saved_map_replays_without_invented_fields(
    tmp_path, monkeypatch
):
    """Synthetic source substitutes at profile lookup; replay keeps the exact minimal map."""
    from reportlab.pdfgen import canvas

    datasheet = tmp_path / "reviewed-source.pdf"
    pdf = canvas.Canvas(str(datasheet))
    pdf.drawString(30, 700, "TEST_FIXTURE synthetic minimal pin replay")
    pdf.save()
    saved = tmp_path / "requirements.json"
    pins = [
        {"physical_pin": str(i), "name": name}
        for i, name in enumerate(("OUT1", "IN1M", "IN1P", "VEE", "IN2P", "IN2M", "OUT2", "VCC"), 1)
    ]
    saved.write_text(json.dumps({"requirements": [], "pin_map": pins}), encoding="utf-8")
    profile = next(p for p in pinout.reviewed_profiles() if p.profile_id == "ti-lm358-pinout-v1")
    monkeypatch.setattr(pinout, "resolve_reviewed_profile", lambda *_: profile)
    run = engine._Run(
        engine.MakeModelRequest(
            "LM358", "LM358", datasheet, tmp_path / "out", requirements_json=saved
        ),
        engine._StageLog(None),
    )
    run.read()
    assert list(run.pin_map) == pins
    assert all(set(pin) == {"physical_pin", "name"} for pin in run.pin_map)
