"""The local credential file, minimum retention and failure behavior."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from boardmodeler.security import credentials as c

SECRET = "test-only-secret-not-a-real-key"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "credential_path", lambda: tmp_path / "credentials.json")
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


def test_saved_key_is_an_ordinary_local_json_file(monkeypatch):
    """No OS-held key: the file is readable by a plain JSON reader, here and elsewhere."""
    c.set_credential("fixture", SECRET)
    raw = c.credential_path().read_bytes()
    assert json.loads(raw) == {"v": 1, "name": "fixture", "key": SECRET}
    monkeypatch.setenv("BOARDMODELER_FIXTURE_API_KEY", "different-env-key")
    saved = c.get_credential("fixture")
    assert saved.value == SECRET and saved.source is c.SecretSource.LOCAL_FILE
    assert "windows" not in saved.detail.lower() and "encrypt" not in saved.detail.lower()


def test_another_process_reads_the_same_file_without_a_windows_key():
    """A second process, with no inherited state, reads the same saved key."""
    c.set_credential("fixture", SECRET)
    code = (
        "import json,sys; from pathlib import Path; "
        "print(json.loads(Path(sys.argv[1]).read_bytes())['key'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(c.credential_path())], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SECRET


def test_module_keeps_no_windows_crypto_or_vault_dependency():
    source = Path(c.__file__).read_text(encoding="utf-8")
    for forbidden in ("crypt32", "CryptProtectData", "CryptUnprotectData", "win32crypt", "keyring"):
        assert forbidden not in source, forbidden
    assert not hasattr(c, "_dpapi")


def test_only_one_key_is_retained_and_delete_is_scoped():
    c.set_credential("fixture", SECRET)
    c.set_credential("replacement", "new-test-key")
    assert c.get_credential("fixture").source is c.SecretSource.MISSING
    assert c.get_credential("replacement").value == "new-test-key"
    c.delete_credential("fixture")
    assert c.credential_path().exists()
    c.delete_credential("replacement")
    assert not c.credential_path().exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b'{"v":1,"name":"fixture"}',
        b'{"v":2,"name":"fixture","key":"' + SECRET.encode() + b'"}',
        b'{"v":1,"name":"fixture","key":"   "}',
        b'{"v":1,"name":"fixture","key":42}',
        b'[{"v":1,"name":"fixture","key":"' + SECRET.encode() + b'"}]',
    ],
)
def test_unreadable_or_unsupported_files_are_not_used(payload):
    c.credential_path().write_bytes(payload)
    result = c.get_credential("fixture")
    assert result.source is c.SecretSource.MISSING and SECRET not in repr(result)
    # The exception text is never surfaced: it can echo the file content.
    assert "Traceback" not in result.detail and "json" not in result.detail


def test_a_foreign_providers_key_is_not_used():
    c.credential_path().write_bytes(
        json.dumps({"v": 1, "name": "someone-else", "key": SECRET}).encode()
    )
    assert c.get_credential("fixture").source is c.SecretSource.MISSING
    c.delete_credential("fixture")
    assert c.credential_path().exists()


def test_failed_replace_keeps_previous_file_and_no_temporary_file(monkeypatch):
    c.set_credential("fixture", SECRET)
    original = c.credential_path().read_bytes()

    def fail(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(c.os, "replace", fail)
    with pytest.raises(OSError):
        c.set_credential("fixture", "replacement-test-key")
    assert c.credential_path().read_bytes() == original
    assert len(list(c.credential_path().parent.iterdir())) == 1


def test_a_failed_write_installs_no_file(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(c.tempfile, "mkstemp", fail)
    with pytest.raises(OSError):
        c.set_credential("fixture", SECRET)
    assert not c.credential_path().exists()


@pytest.mark.parametrize("value", ["", "  ", "x" * 16385])
def test_invalid_values_rejected(value):
    with pytest.raises(ValueError):
        c.set_credential("fixture", value)
    assert not c.credential_path().exists()


def test_the_key_size_cap_counts_bytes_not_characters():
    value = "\u00e9" * 8192  # 16384 UTF-8 bytes: accepted.
    c.set_credential("fixture", value)
    assert c.get_credential("fixture").value == value
    with pytest.raises(ValueError):
        c.set_credential("fixture", value + "\u00e9")


def test_oversized_file_is_not_used():
    c.credential_path().write_bytes(b"x" * 65537)
    assert c.get_credential("fixture").source is c.SecretSource.MISSING


def test_redact_longest_first():
    assert (
        c.redact("long-secret short", ["long", "long-secret", "short"]) == "[REDACTED] [REDACTED]"
    )
