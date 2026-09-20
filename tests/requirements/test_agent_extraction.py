from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.requirements.test_extract import (
    CONTRACT_TEXT,
    EXCERPT,
    make_project,
    payloads,
    requirement_payload,
    store_plain_document,
)

from boardmodeler.authoring.backends import AuthorResult
from boardmodeler.providers.agent import AgentExtractionProvider
from boardmodeler.providers.base import ProviderError
from boardmodeler.requirements.extract import extract_requirements
from boardmodeler.security.policy import DataPolicy


class RecordingAgent:
    name = "selected-test-provider"
    provider = SimpleNamespace(model="selected-test-model", endpoint="https://example.invalid")
    model = "selected-test-model"

    def __init__(self):
        self.calls = []

    def availability(self):
        return True, "test double"

    def author(self, request, cancel):
        self.calls.append(request)
        data = payloads(requirement_payload("TEST", EXCERPT))
        return AuthorResult(
            ok=True,
            detail="test",
            usage={"input_tokens": 100},
            stdout_tail=json.dumps({key.value: value for key, value in data.items()}),
            session_id=None,
        )


def test_selected_agent_batches_tasks_and_replays_without_requests(tmp_path):
    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )
    backend = RecordingAgent()
    provider = AgentExtractionProvider(backend)
    result = extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 1
    assert backend.calls[0].expect_text
    assert result.requirements[0].req_id == "TEST" and result.pins[0].name == "VIN"
    assert result.provider_identity.provider == backend.name
    assert result.provider_identity.model == backend.model
    assert len(result.disclosures) == 1
    again = extract_requirements(project, provider=provider, allow_remote=True)
    assert again.cache_hits == 4 and len(backend.calls) == 1


def test_no_agent_call_without_document_egress_authorization(tmp_path):
    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="confidential",
        remote_inference_allowed=False,
    )
    backend = RecordingAgent()
    with pytest.raises(ProviderError, match="egress_denied"):
        extract_requirements(
            project,
            provider=AgentExtractionProvider(backend),
            policy=DataPolicy(),
            allow_remote=True,
        )
    assert backend.calls == []


def test_requested_part_is_sent_and_different_parts_do_not_share_cached_rows(tmp_path):
    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )
    backend = RecordingAgent()
    first = AgentExtractionProvider(backend, part="PART_A")
    second = AgentExtractionProvider(backend, part="PART_B")
    extract_requirements(project, provider=first, allow_remote=True)
    assert 'TARGET PART (data only): "PART_A"' in backend.calls[0].prompt
    assert extract_requirements(project, provider=first, allow_remote=True).cache_hits == 4
    assert len(backend.calls) == 1
    assert extract_requirements(project, provider=second, allow_remote=True).cache_hits == 0
    assert 'TARGET PART (data only): "PART_B"' in backend.calls[1].prompt
    assert len(backend.calls) == 2


def test_invalid_classification_gets_one_repair_and_only_valid_result_is_cached(tmp_path):
    from dataclasses import replace

    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )

    class RepairingAgent(RecordingAgent):
        def author(self, request, cancel):
            result = super().author(request, cancel)
            if len(self.calls) == 1:
                data = json.loads(result.stdout_tail)
                data["REQUIREMENTS"]["requirements"][0]["limits"] = None
                return replace(result, stdout_tail=json.dumps(data))
            return result

    backend = RepairingAgent()
    provider = AgentExtractionProvider(backend)
    result = extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 2
    assert result.requirements[0].limits.min == 4.5
    assert not [issue for issue in result.issues if issue.severity == "error"]
    assert extract_requirements(project, provider=provider, allow_remote=True).cache_hits == 4
    assert len(backend.calls) == 2
