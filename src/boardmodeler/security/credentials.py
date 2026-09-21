"""One encrypted local credential per edition and Windows user; no credential vault.

The application writes only DPAPI ciphertext beside the app in its data directory. It never
falls back to a plaintext file. Explicit environment variables remain available for
CLI automation. Saving another provider replaces the previously stored credential.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from boardmodeler.build_flavor import BOB_ONLY

REDACTED = "[REDACTED]"
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_APP_NAME = "SpiceMakerBob" if BOB_ONLY else "SpiceMaker"
_ENTROPY = f"{_APP_NAME}:credential-file:v1".encode("ascii")
_LOCK = threading.RLock()
_MAX_FILE_BYTES = 65536


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
    """Ciphertext belongs to this extracted copy, never a shared profile."""
    from boardmodeler.storage import data_dir

    return data_dir() / "credentials.bin"


def env_var_name(name: str) -> str:
    return f"BOARDMODELER_{_NON_ALNUM.sub('_', name).upper()}_API_KEY"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Current-user DPAPI, authenticated by Windows; no machine-wide flag or UI."""
    if os.name != "nt":
        raise RuntimeError(
            "Encrypted credential files require Windows; use an environment variable."
        )
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    entropy = ctypes.create_string_buffer(_ENTROPY)
    incoming = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    additional = _Blob(len(_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN=1; deliberately never CRYPTPROTECT_LOCAL_MACHINE.
        if not operation(
            ctypes.byref(incoming),
            None,
            ctypes.byref(additional),
            None,
            None,
            1,
            ctypes.byref(outgoing),
        ):
            raise RuntimeError("Windows could not protect or unlock this credential file.")
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(outgoing.data)


def _read_saved() -> dict[str, object] | None:
    path = credential_path()
    if not path.exists():
        return None
    with path.open("rb") as stream:
        encrypted = stream.read(_MAX_FILE_BYTES + 1)
    if len(encrypted) > _MAX_FILE_BYTES:
        raise ValueError("Credential file is too large.")
    payload = json.loads(_dpapi(encrypted, decrypt=True))
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
                    "encrypted local file for this Windows user",
                )
        except Exception:
            # Never include exception text: decoders and OS errors can echo input.
            problem = "saved key could not be unlocked; enter it again in SETUP"
    variable = env_var_name(name)
    if value := os.environ.get(variable):
        return Credential(name, value, SecretSource.ENV, f"environment variable {variable}")
    return Credential(name, None, SecretSource.MISSING, problem)


def set_credential(name: str, value: str) -> None:
    """Replace the single saved credential atomically, writing ciphertext only."""
    if not name.strip() or not value.strip() or len(value.encode("utf-8")) > 16384:
        raise ValueError("Enter a non-empty API key of at most 16 KiB.")
    with _LOCK:
        payload = json.dumps({"v": 1, "name": name, "key": value}).encode("utf-8")
        encrypted = _dpapi(payload)
        if _dpapi(encrypted, decrypt=True) != payload:
            raise RuntimeError("Credential encryption verification failed.")
        path = credential_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
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
