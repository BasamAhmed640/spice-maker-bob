"""Synthetic publication receipts guard identity; no device accuracy is claimed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from tests.pipeline.test_publication_provenance import publish_run

from boardmodeler.authoring.harness import HarnessReport
from boardmodeler.authoring.loop import BuildOutcome, model_file
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.pipeline import make_model as engine

LIBRARY = b"* TEST_FIXTURE\r\n.subckt TEST IN OUT\r\nR1 IN OUT 1k\r\n.ends TEST\r\n"


def _run(tmp_path):
    request = engine.MakeModelRequest("TEST", "TEST", tmp_path / "unused.pdf", tmp_path / "out")
    run = engine._Run(request, engine._StageLog(None))
    # TEST_FIXTURE: this test isolates publication identity, not source pinout approval.
    run._check_pinout_publication = lambda library, symbol, model_file: None
    run.spec = SpecSet("TEST", "TEST", "synthetic-doc", (), ())
    source = model_file(run.workdir, "TEST")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(LIBRARY)
    run.report = HarnessReport("TEST", hashlib.sha256(LIBRARY).hexdigest(), run.spec.digest(), ())
    return run, source


def _stale_outputs(run):
    payload = {
        "TEST.lib": LIBRARY,
        "TEST.asy": b"old symbol",
        "MODEL_CARD.md": b"old PASS",
        "EXAMPLE.cir": b"old example",
        "install.md": b"old install instruction",
        engine.HARNESS_REPORT_NAME: b"old measured report",
        engine.DESIGN_RECORD_NAME: b"old design",
        "template-parameters.json": b"old parameters",
        "sanity-report.json": b"old sanity",
        "pin-only-report.json": b"old gate",
        engine.QUALIFICATION_REPORT_NAME: b"old qualification",
        engine.RESULTS_NAME: b"{}",
        engine.TIMING_NAME: b"old timing",
    }
    run.out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        (run.out_dir / name).write_bytes(content)
    run.spec_dir.mkdir(parents=True, exist_ok=True)
    (run.spec_dir / engine.QUALIFICATION_PLAN_NAME).write_bytes(b"old qualification plan")
    (run.out_dir / "user-note.txt").write_text("preserve this unrelated file")
    return payload


@pytest.mark.parametrize("blocked_at", ["gate", "author", "author_outcome", "network"])
def test_blocked_rerun_with_old_candidate_withdraws_all_old_deliverables(
    tmp_path, monkeypatch, blocked_at
):
    previous, source = _run(tmp_path)
    old = _stale_outputs(previous)
    request = replace(
        previous.request, engine="legacy_ai" if blocked_at == "network" else "behavioral"
    )
    monkeypatch.setattr(engine._Run, "read", lambda self: None)
    monkeypatch.setattr(engine._Run, "extract", lambda self, cancel: None)
    # Isolate the selected refusal stage from package evidence for this synthetic part.
    monkeypatch.setattr(engine._Run, "freeze_pinout", lambda self: None)

    def bind(self, cancel):
        self.spec, self.report = previous.spec, previous.report

    monkeypatch.setattr(engine._Run, "bind", bind)

    def gate(self):
        if blocked_at == "gate":
            raise engine._Stop("author", "BLOCKED", "unsupported_part: synthetic refusal")

    monkeypatch.setattr(engine._Run, "support_gate", gate)

    def author(self, cancel):
        if blocked_at == "author_outcome":
            self.outcome = BuildOutcome("BLOCKED", 0, self.report, (), "synthetic author refused")
        else:
            self.status, self.detail = "BLOCKED", "synthetic author unavailable"

    monkeypatch.setattr(engine._Run, "author", author)
    monkeypatch.setattr(engine, "internet_allowed", lambda: blocked_at != "network")
    result = engine.make_model(request)
    assert result.status == "BLOCKED"
    assert result.lib_path is result.asy_path is result.card_path is None
    assert source.read_bytes() == LIBRARY
    assert not (previous.spec_dir / engine.QUALIFICATION_PLAN_NAME).exists()
    assert (previous.out_dir / "user-note.txt").read_text() == "preserve this unrelated file"
    for name, data in old.items():
        if name not in (engine.RESULTS_NAME, engine.TIMING_NAME):
            assert not (previous.out_dir / name).exists(), name
        archived = list((previous.workdir / "publication-history").glob(f"*/{name}"))
        assert archived and archived[0].read_bytes() == data
    assert json.loads((previous.out_dir / engine.RESULTS_NAME).read_text())["status"] == "BLOCKED"


@pytest.mark.parametrize("mutation", ["model", "spec", "frozen_file", "line_endings"])
def test_post_judge_mutation_withholds_candidate_and_old_root_outputs(tmp_path, mutation):
    run, source = _run(tmp_path)
    _stale_outputs(run)
    if mutation == "model":
        source.write_bytes(LIBRARY.replace(b"1k", b"2k"))
    elif mutation == "line_endings":
        source.write_bytes(LIBRARY.replace(b"\r\n", b"\n"))
    elif mutation == "frozen_file":
        (run.spec_dir / engine.CHARACTERISTICS_NAME).write_text(
            replace(run.spec, doc_id="changed-after-judge").to_json(), encoding="utf-8"
        )
    else:
        run.spec = replace(run.spec, doc_id="changed-after-judge")
    run.save()
    assert run.lib_path is run.asy_path is run.card_path is None
    assert not (run.out_dir / "TEST.lib").exists()
    assert not (run.out_dir / "MODEL_CARD.md").exists()
    assert "publication_" in run.detail and "mismatch" in run.detail
    assert run.decide(())[0] == "UNKNOWN"


def test_successful_publication_preserves_exact_measured_CRLF_bytes(tmp_path):
    run, source = _run(tmp_path)
    run.save()
    assert run.lib_path.read_bytes() == source.read_bytes() == LIBRARY
    assert hashlib.sha256(run.lib_path.read_bytes()).hexdigest() == run.report.model_sha256
    design = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert design["delivered_library_sha256"] == run.report.model_sha256


def test_candidate_mutated_during_publish_check_is_not_written(tmp_path, monkeypatch):
    run, source = _run(tmp_path)
    ports = engine.subckt_ports

    def changed(text, name):
        source.write_bytes(LIBRARY.replace(b"1k", b"3k"))
        return ports(text, name)

    monkeypatch.setattr(engine, "subckt_ports", changed)
    run.save()
    assert run.lib_path is None and not (run.out_dir / "TEST.lib").exists()
    assert "changed during publication" in run.detail


def test_partial_publication_failure_withdraws_new_library_and_old_success(tmp_path, monkeypatch):
    run, _source = _run(tmp_path)
    _stale_outputs(run)

    def failed(ports, lib_name, **kwargs):
        raise OSError("synthetic symbol writer failure")

    monkeypatch.setattr(run, "_publish_symbol", failed)
    run.save()
    assert run.lib_path is run.card_path is None
    assert not (run.out_dir / "TEST.lib").exists()
    assert not (run.out_dir / "MODEL_CARD.md").exists()
    assert "model_not_published" in run.detail


def test_successful_save_reuses_archived_provenance_without_old_root_claims(tmp_path):
    run, seed, source = publish_run(tmp_path)
    old = seed.design.record(source.read_bytes())
    run._write_json(run.out_dir / engine.DESIGN_RECORD_NAME, old)
    run._write_json(
        run.out_dir / "template-parameters.json",
        {
            "seed_sha256": run.report.model_sha256,
            "final_model_sha256": run.report.model_sha256,
        },
    )
    run.save()
    assert run.lib_path is not None
    assert run.previous_publication_dir is not None
    assert json.loads((run.previous_publication_dir / engine.DESIGN_RECORD_NAME).read_text()) == old
    current = json.loads((run.out_dir / engine.DESIGN_RECORD_NAME).read_text())
    assert current["association"] == "exact"
    assert current["delivered_library_sha256"] == run.report.model_sha256
    parameters = json.loads((run.out_dir / "template-parameters.json").read_text())
    assert parameters["final_model_matches_seed"]
