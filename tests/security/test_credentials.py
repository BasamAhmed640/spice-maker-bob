"""Real current-user encryption, minimum retention and failure behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from boardmodeler.security import credentials as c

SECRET = "test-only-secret-not-a-real-key"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "credential_path", lambda: tmp_path / "credentials.bin")
    monkeypatch.delenv("BOARDMODELER_FIXTURE_API_KEY", raising=False)


def test_missing_and_environment_fallback(monkeypatch):
    assert c.get_credential("fixture").source is c.SecretSource.MISSING
    monkeypatch.setenv("BOARDMODELER_FIXTURE_API_KEY", SECRET)
    result = c.get_credential("fixture")
    assert result.source is c.SecretSource.ENV and result.value == SECRET
    assert SECRET not in repr(result) and SECRET not in c.describe_credential("fixture")
    assert not c.credential_path().exists()


def test_env_name_normalizes_non_alphanumeric(monkeypatch):
    monkeypatch.setenv("BOARDMODELER_BOB_DIRECT_API_KEY", SECRET)
    assert c.get_credential("bob-direct").value == SECRET


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_real_encrypted_roundtrip_and_new_process(monkeypatch):
    c.set_credential("fixture", SECRET)
    raw = c.credential_path().read_bytes()
    assert SECRET.encode() not in raw and b'"key"' not in raw
    monkeypatch.setenv("BOARDMODELER_FIXTURE_API_KEY", "different-env-key")
    assert c.get_credential("fixture").value == SECRET
    assert c.get_credential("fixture").source is c.SecretSource.LOCAL_FILE
    code = (
        "import sys; from pathlib import Path; "
        "from boardmodeler.security.credentials import _dpapi; "
        "sys.stdout.buffer.write(_dpapi(Path(sys.argv[1]).read_bytes(), decrypt=True))"
    )
    # The subprocess reads ciphertext; neither argv nor its environment contains the saved key.
    result = subprocess.run(
        [sys.executable, "-c", code, str(c.credential_path())], capture_output=True, check=True
    )
    assert json.loads(result.stdout)["key"] == SECRET
    assert list(c.credential_path().parent.iterdir()) == [c.credential_path()]


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_only_one_key_is_retained_and_delete_is_scoped():
    c.set_credential("fixture", SECRET)
    c.set_credential("replacement", "new-test-key")
    assert c.get_credential("fixture").source is c.SecretSource.MISSING
    assert c.get_credential("replacement").value == "new-test-key"
    c.delete_credential("fixture")
    assert c.credential_path().exists()
    c.delete_credential("replacement")
    assert not c.credential_path().exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_tampering_or_different_edition_cannot_unlock(monkeypatch):
    c.set_credential("fixture", SECRET)
    raw = c.credential_path().read_bytes()
    c.credential_path().write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))
    result = c.get_credential("fixture")
    assert result.source is c.SecretSource.MISSING and SECRET not in repr(result)
    c.credential_path().write_bytes(raw)
    monkeypatch.setattr(c, "_ENTROPY", b"another-edition")
    assert c.get_credential("fixture").source is c.SecretSource.MISSING


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_failed_replace_keeps_previous_ciphertext_and_no_temporary_file(monkeypatch):
    c.set_credential("fixture", SECRET)
    original = c.credential_path().read_bytes()

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(c.os, "replace", fail)
    with pytest.raises(OSError):
        c.set_credential("fixture", "replacement-test-key")
    assert c.credential_path().read_bytes() == original
    assert len(list(c.credential_path().parent.iterdir())) == 1


def test_encryption_failure_never_writes_plaintext(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated encryption failure")

    monkeypatch.setattr(c, "_dpapi", fail)
    with pytest.raises(RuntimeError):
        c.set_credential("fixture", SECRET)
    assert not c.credential_path().exists()


@pytest.mark.parametrize("value", ["", "  ", "x" * 16385])
def test_invalid_values_rejected(value):
    with pytest.raises(ValueError):
        c.set_credential("fixture", value)
    assert not c.credential_path().exists()


def test_corrupt_and_oversized_file_are_not_used():
    c.credential_path().write_bytes(b"not encrypted")
    assert c.get_credential("fixture").source is c.SecretSource.MISSING
    c.credential_path().write_bytes(b"x" * 65537)
    assert c.get_credential("fixture").source is c.SecretSource.MISSING


def test_redact_longest_first():
    assert (
        c.redact("long-secret short", ["long", "long-secret", "short"]) == "[REDACTED] [REDACTED]"
    )
