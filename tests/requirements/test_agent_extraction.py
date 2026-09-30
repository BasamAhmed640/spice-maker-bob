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

    def author(self, request, cancel, *, timeout_s=None):
        assert timeout_s is not None and 0 < timeout_s <= 150
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
        def author(self, request, cancel, *, timeout_s=None):
            result = super().author(request, cancel, timeout_s=timeout_s)
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


def test_datasheet_compound_units_do_not_trigger_a_paid_correction(tmp_path):
    """Valid but unprobed quantities must survive extraction without losing rows."""
    from dataclasses import replace

    from boardmodeler.pipeline.make_model import bind_requirements

    project = make_project(tmp_path)
    text = "Input transition rate is at most 20 ns/V. Thermal resistance is 165 °C/W."
    store_plain_document(
        project, text, doc_id="DOC_1", classification="public", remote_inference_allowed=True
    )

    class CompoundAgent(RecordingAgent):
        def author(self, request, cancel, *, timeout_s=None):
            result = super().author(request, cancel, timeout_s=timeout_s)
            data = json.loads(result.stdout_tail)
            rows = []
            for req_id, unit, value, statement in (
                ("EDGE", "ns/V", 20, "Input transition rate"),
                ("THERMAL", "°C/W", 165, "Thermal resistance"),
            ):
                row = requirement_payload(req_id, text)
                row.update(statement=statement, limits={"max": value, "unit": unit})
                rows.append(row)
            data["REQUIREMENTS"]["requirements"] = rows
            return replace(result, stdout_tail=json.dumps(data))

    backend = CompoundAgent()
    provider = AgentExtractionProvider(backend)
    result = extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 1
    assert [row.limits.unit for row in result.requirements] == ["ns/V", "°C/W"]
    assert not [issue for issue in result.issues if issue.severity == "error"]
    bindings = bind_requirements(result.requirements)
    assert len(bindings) == 2
    assert all(row["probe"] is None and row["not_testable_reason"] for row in bindings)
    assert extract_requirements(project, provider=provider, allow_remote=True).cache_hits == 4
    assert len(backend.calls) == 1


def test_validation_repairs_share_one_call_limit_and_never_cache_bad_rows(tmp_path):
    from dataclasses import replace

    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )

    class InvalidAgent(RecordingAgent):
        def author(self, request, cancel, *, timeout_s=None):
            result = super().author(request, cancel, timeout_s=timeout_s)
            return replace(result, stdout_tail="{cut off")

    backend = InvalidAgent()
    provider = AgentExtractionProvider(backend, max_calls=1)
    with pytest.raises(ProviderError, match="extraction_budget_exhausted"):
        extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 1
    assert not list(project.path("evidence/cache").glob("*.json"))


def test_elapsed_extraction_budget_prevents_a_new_validation_repair(tmp_path, monkeypatch):
    from dataclasses import replace

    from boardmodeler.providers import agent

    clock = [100.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )

    class SlowInvalidAgent(RecordingAgent):
        def author(self, request, cancel, *, timeout_s=None):
            assert timeout_s == 7.0
            result = super().author(request, cancel, timeout_s=timeout_s)
            clock[0] += 8.0
            return replace(result, stdout_tail="{cut off")

    backend = SlowInvalidAgent()
    provider = AgentExtractionProvider(backend, timeout_s=7)
    with pytest.raises(ProviderError, match="extraction_budget_exhausted"):
        extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 1


def test_parallel_batches_and_split_retries_cannot_multiply_the_request_limit(tmp_path):
    from dataclasses import replace

    project = make_project(tmp_path)
    store_plain_document(
        project,
        CONTRACT_TEXT * 300,
        doc_id="DOC_1",
        classification="public",
        remote_inference_allowed=True,
    )

    class InvalidAgent(RecordingAgent):
        def author(self, request, cancel, *, timeout_s=None):
            result = super().author(request, cancel, timeout_s=timeout_s)
            return replace(result, stdout_tail="{cut off")

    backend = InvalidAgent()
    provider = AgentExtractionProvider(backend, max_calls=2)
    with pytest.raises(ProviderError, match="extraction_budget_exhausted"):
        extract_requirements(project, provider=provider, allow_remote=True)
    assert len(backend.calls) == 2
