"""Release identity changes with real engine code and reviewed policy."""

from __future__ import annotations

from boardmodeler.engine_identity import engine_contract, source_contract
from boardmodeler.pipeline.make_model import ENGINES, MakeModelRequest


def test_reported_contract_matches_default_public_request(tmp_path):
    contract = engine_contract()
    request = MakeModelRequest("TEST", "TEST", tmp_path / "source.pdf", tmp_path / "out")
    assert contract["default_engine"] == request.engine == "behavioral"
    assert set(contract["engines"]) == set(ENGINES)
    assert len(contract["source_sha256"]) == 64


def test_engine_changes_and_policy_changes_invalidate_identity(tmp_path):
    code = tmp_path / "models" / "renderer.py"
    code.parent.mkdir()
    code.write_bytes(b"value = 1\n")
    policy = code.with_name("policy.json")
    policy.write_bytes(b'{"pins": [1, 2]}\n')
    before = source_contract(tmp_path)
    code.write_bytes(b"value = 1\r\n")
    assert source_contract(tmp_path) == before
    code.write_bytes(b"value = 2\n")
    changed_code = source_contract(tmp_path)
    assert changed_code != before
    policy.write_bytes(b'{"pins": [2, 1]}\n')
    assert source_contract(tmp_path) != changed_code
