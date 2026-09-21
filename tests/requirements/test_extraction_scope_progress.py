from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from tests.requirements.test_agent_extraction import RecordingAgent
from tests.requirements.test_extract import CONTRACT_TEXT, make_project, store_plain_document

from boardmodeler.providers.agent import AgentExtractionProvider, _run_batches
from boardmodeler.providers.base import ProviderError
from boardmodeler.requirements.extract import extract_requirements


def test_selected_document_is_the_only_input_and_unrelated_changes_keep_cache(tmp_path):
    project = make_project(tmp_path)
    for doc_id, text in [("DOC_1", CONTRACT_TEXT), ("OLD_DOC", "UNRELATED_OLD_DATASHEET")]:
        store_plain_document(
            project, text, doc_id=doc_id, classification="public", remote_inference_allowed=True
        )
    backend = RecordingAgent()
    provider = AgentExtractionProvider(backend)
    result = extract_requirements(
        project, provider=provider, document_ids=["DOC_1"], allow_remote=True
    )
    assert len(backend.calls) == 1
    assert "UNRELATED_OLD_DATASHEET" not in backend.calls[0].prompt
    assert "OLD_DOC" not in backend.calls[0].prompt
    assert len(result.disclosures) == 1
    store_plain_document(
        project,
        "CHANGED_UNRELATED_DATASHEET",
        doc_id="OLD_DOC",
        classification="public",
        remote_inference_allowed=True,
    )
    assert (
        extract_requirements(
            project, provider=provider, document_ids=["DOC_1"], allow_remote=True
        ).cache_hits
        == 4
    )
    assert len(backend.calls) == 1
    with pytest.raises(ProviderError, match="document_not_found"):
        extract_requirements(
            project, provider=provider, document_ids=["MISSING"], allow_remote=True
        )
    assert len(backend.calls) == 1


def test_progress_advances_while_another_request_is_waiting_and_keeps_result_order(monkeypatch):
    from boardmodeler.providers import agent

    monkeypatch.setattr(agent, "PROGRESS_INTERVAL_S", 0.01)
    waiting = threading.Event()
    release = threading.Event()
    messages = []
    jobs = [[SimpleNamespace(snippets=[], slow=True)], [SimpleNamespace(snippets=[], slow=False)]]

    def run(job, report):
        if job[0].slow:
            report("API request 1/2, waiting for response")
            waiting.set()
            assert release.wait(2)
            return "first"
        assert waiting.wait(2)
        return "second"

    def progress(message):
        messages.append(message)
        if sum("1/2 batches complete" in item for item in messages) >= 2:
            release.set()

    assert _run_batches(jobs, run, progress, None) == ["first", "second"]
    assert any("1/2 batches complete; 1 active" in item for item in messages)
    assert any("API request 1/2" in item for item in messages)
    assert messages[-1].startswith("2/2 batches complete; 0 active")


def test_failed_batch_is_reported_as_failed_not_completed():
    messages = []

    def fail(job, progress):
        raise ProviderError("fixture_failure", "synthetic failure")

    with pytest.raises(ProviderError, match="fixture_failure"):
        _run_batches([[SimpleNamespace(snippets=[])]], fail, messages.append, None)
    assert "0/1 batches complete" in messages[-1] and "1 failed" in messages[-1]


def test_model_pipeline_selects_current_pdf_in_a_reused_output_folder(tmp_path, monkeypatch):
    from tests.pipeline.test_make_model import CountingProvider, _synthesize_datasheet, last_stage

    from boardmodeler.config import AppConfig
    from boardmodeler.domain.enums import ProviderKind
    from boardmodeler.pipeline import make_model as engine
    from boardmodeler.pipeline.make_model import MakeModelRequest, make_model
    from boardmodeler.pipeline.project import create_project
    from boardmodeler.providers.registry import ProviderSelection

    out = tmp_path / "out"
    project = create_project(out / "build", project_id="OLD_MODEL", name="Old model")
    store_plain_document(
        project,
        "UNRELATED_OLD_DATASHEET",
        doc_id="OLD_DOC",
        classification="public",
        remote_inference_allowed=True,
    )
    provider = CountingProvider()
    original = provider.extract

    def capture(request, cancel=None):
        assert {doc.doc_id for doc in request.documents} != {"OLD_DOC"}
        assert len(request.documents) == 1
        assert all(s.doc_id != "OLD_DOC" for s in request.snippets)
        return original(request, cancel)

    provider.extract = capture
    monkeypatch.setattr(engine, "load_config", lambda path=None: AppConfig())
    monkeypatch.setattr(
        engine,
        "select_provider",
        lambda *args, **kwargs: ProviderSelection(
            provider=provider, kind=ProviderKind.FIXTURE, detail="fixture"
        ),
    )
    cancel = threading.Event()
    cancel.set()
    result = make_model(
        MakeModelRequest(
            part="SYNTH",
            subckt="BM_TEST",
            datasheet=_synthesize_datasheet(tmp_path / "selected.pdf"),
            out_dir=out,
            backend_name="scripted",
        ),
        cancel=cancel,
    )
    assert provider.calls == 4
    assert last_stage(result, "extract").status == "ok"


def test_flat_evidence_page_is_normalized_without_changing_the_citation():
    from boardmodeler.providers.agent import _normalize_evidence_pages

    payload = {
        "part": {"evidence": [{"doc_id": "datasheet", "pdf_page": 0, "excerpt": "Source words"}]}
    }
    _normalize_evidence_pages(payload)
    assert payload["part"]["evidence"] == [
        {"doc_id": "datasheet", "page": {"pdf_page": 0}, "excerpt": "Source words"}
    ]
    _normalize_evidence_pages(payload)
    assert payload["part"]["evidence"][0]["page"] == {"pdf_page": 0}


def test_conflicting_or_invalid_page_numbers_are_not_silently_repaired():
    from boardmodeler.providers.agent import _normalize_evidence_pages

    for number in (True, -1, "0", 2):
        ref = {"doc_id": "datasheet", "pdf_page": number, "page": {"pdf_page": 0}}
        _normalize_evidence_pages({"evidence": [ref]})
        assert "pdf_page" in ref
