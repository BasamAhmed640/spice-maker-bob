"""One local credential file per edition, inside this copy's data directory.

The application writes the key as an ordinary local JSON file beside the app in its
data directory. Nothing is bound to Windows: no DPAPI ciphertext, no registry entry,
no Credential Manager entry, no user-profile location and no machine-held key. The
file is protected only by the folder it lives in, so keep this copy private and do
not share its ``data`` directory. Explicit environment variables remain available
for CLI automation. Saving another provider replaces the previously stored
credential.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from boardmodeler.build_flavor import BOB_ONLY

REDACTED = "[REDACTED]"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_APP_NAME = "SpiceMakerBob" if BOB_ONLY else "SpiceMaker"
#: Each edition keeps its own file, so unpacking both into one folder shares no key.
_CREDENTIAL_FILE = "credentials.bob.json" if BOB_ONLY else "credentials.json"
_LOCK = threading.RLock()
_MAX_FILE_BYTES = 65536
_MAX_KEY_BYTES = 16384


class SecretSource(StrEnum):
    LOCAL_FILE = "LOCAL_FILE"
    ENV = "ENV"
    MISSING = "MISSING"


@dataclass(frozen=True)
class Credential:
    name: str
    value: str | None = field(repr=False)
    source: SecretSource
    detail: str


def credential_path() -> Path:
    """This extracted copy's own credential file, never a shared profile."""
    from boardmodeler.storage import state_file

    return state_file(_CREDENTIAL_FILE)


def env_var_name(name: str) -> str:
    return f"BOARDMODELER_{_NON_ALNUM.sub('_', name).upper()}_API_KEY"


def _read_saved() -> dict[str, object] | None:
    path = credential_path()
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("Credential file is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # The text is fixed: a decoder message can quote the file's own content.
        raise ValueError("Credential file has an unsupported format.") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "name", "key"}
        or payload["v"] != 1
        or not isinstance(payload["name"], str)
        or not isinstance(payload["key"], str)
        or not payload["key"].strip()
    ):
        raise ValueError("Credential file has an unsupported format.")
    return payload


def get_credential(name: str) -> Credential:
    problem = "no saved key for this provider"
    with _LOCK:
        try:
            saved = _read_saved()
            if saved and saved["name"] == name:
                return Credential(
                    name,
                    str(saved["key"]),
                    SecretSource.LOCAL_FILE,
                    "local file in this copy's data directory",
                )
        except Exception:
            # Never include exception text: a decoder error can echo file content.
            problem = "saved key could not be read; enter it again in SETUP"
    variable = env_var_name(name)
    if value := os.environ.get(variable):
        return Credential(name, value, SecretSource.ENV, f"environment variable {variable}")
    return Credential(name, None, SecretSource.MISSING, problem)


def set_credential(name: str, value: str) -> None:
    """Replace the single saved credential atomically, writing the local file only."""
    if not name.strip() or not value.strip() or len(value.encode("utf-8")) > _MAX_KEY_BYTES:
        raise ValueError("Enter a non-empty API key of at most 16 KiB.")
    with _LOCK:
        payload = json.dumps({"v": 1, "name": name, "key": value}).encode("utf-8")
        path = credential_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if Path(temporary).read_bytes() != payload:
                raise RuntimeError("Credential file verification failed.")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def delete_credential(name: str) -> None:
    """Forget the selected local credential; never delete a different provider's key."""
    with _LOCK:
        saved = _read_saved()
        if saved and saved["name"] == name:
            credential_path().unlink(missing_ok=True)


def describe_credential(name: str) -> str:
    credential = get_credential(name)
    return f"credential {name!r}: source={credential.source.value.lower()}"


def redact(text: str, secrets: Iterable[str]) -> str:
    result = text
    for secret in sorted((s for s in secrets if s and s.strip()), key=len, reverse=True):
        result = result.replace(secret, REDACTED)
    return result
