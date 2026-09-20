"""Reply paths: every write lands inside the model directory as the OS resolves it.

Two families of reply name used to reach the filesystem and must not any more: a
component Windows canonicalizes differently from ``Path.resolve()`` (Win32 strips a
trailing dot or space, so ``'.. '`` is ``..``, and ``resolve()`` keeps it literal
while the containment check passes) and a component carrying a control character
(an embedded NUL cannot be written at all). Both are refused before any file of the
reply is written, so a bad name can never leave the earlier files of the same reply
on disk.

The second half covers the write itself: a path that passes validation but cannot be
written is returned as ``api_write_failed: ...`` — never raised, whatever exception
the OS layer raises — and the note states honestly that earlier files may already be
on disk. Everything runs offline: the transport and the credential lookup are
injected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardmodeler import agent_providers
from boardmodeler.agent_providers import AgentProvider, by_id
from boardmodeler.authoring import api_backend
from boardmodeler.authoring.api_backend import ApiKeyBackend
from boardmodeler.authoring.backends import AuthorRequest, AuthorResult
from boardmodeler.providers.http_inference import HttpRequest, HttpResponse
from boardmodeler.security.credentials import Credential, SecretSource

SECRET = "sk-paths-test-7b21"
LIB_TEXT = ".subckt BM_PATHS VIN GND\nR1 VIN GND 1k\n.ends BM_PATHS\n"

#: The wire shape these tests speak through in a build that ships no HTTP provider,
#: so the path refusals are exercised in every build — they are not wire-specific.
DECLARED = AgentProvider(
    id="openai",
    label="OpenAI",
    wire="openai",
    credential="openai",
    key_label="OPENAI API KEY",
    key_hint="platform.openai.com → API keys",
    docs="https://developers.openai.com/api/docs/guides/text",
    endpoint="https://api.openai.com/v1",
    model="gpt-4o",
    env_aliases=("OPENAI_API_KEY",),
)


def provider() -> AgentProvider:
    """This build's OpenAI entry, or the declared shape for that wire."""
    return by_id(DECLARED.id) or DECLARED


@pytest.fixture(autouse=True)
def accepts_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build without the entry still runs these tests through the declared shape."""
    if by_id(DECLARED.id) is None:
        monkeypatch.setattr(agent_providers, "CATALOG", (*agent_providers.CATALOG, DECLARED))


def with_key():
    def lookup(name: str) -> Credential:
        return Credential(
            name=name, value=SECRET, source=SecretSource.KEYRING, detail="test keyring"
        )

    return lookup


class Recorder:
    """An injected transport: records every request, answers with one canned body."""

    def __init__(self, body: str) -> None:
        self.payload = body.encode("utf-8")
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(status=200, headers={}, body=self.payload)


def reply(files: dict[str, str]) -> str:
    """One OpenAI-shaped response whose assistant message is the files JSON."""
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"files": files}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


def request_for(tmp_path: Path) -> AuthorRequest:
    workdir = tmp_path / "build"
    model_dir = workdir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    return AuthorRequest(
        prompt="author the model", workdir=workdir, model_dir=model_dir, max_turns=12
    )


def written_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def author(tmp_path: Path, files: dict[str, str]) -> tuple[AuthorResult, AuthorRequest]:
    request = request_for(tmp_path)
    backend = ApiKeyBackend(
        provider(), transport=Recorder(reply(files)), credential_lookup=with_key()
    )
    return backend.author(request), request


# --------------------------------------------------------------------------- #
# names the Win32 layer would rewrite


@pytest.mark.parametrize(
    "name",
    [
        ".. /escape.lib",  # first component: Win32 reads it as .. → workdir/escape.lib
        "model/.. /escape.lib",  # the same after the model-directory prefix is dropped
        "sub/.. /escape.lib",
        "x.lib.",  # a trailing dot is stripped, so the file is not the name asked for
        "sub /x.lib",
    ],
)
def test_a_component_windows_rewrites_is_refused_and_writes_nothing(
    name: str, tmp_path: Path
) -> None:
    result, _ = author(tmp_path, {name: LIB_TEXT})

    assert result.ok is False
    assert result.detail.startswith("api_write_refused:"), result.detail
    assert "Windows rewrites" in result.detail
    assert written_files(tmp_path) == [], "a refused name must write nothing at all"


def test_the_audited_pair_writes_neither_file(tmp_path: Path) -> None:
    """The audited pair: a name Windows normalizes beside a good one, refused up front."""
    result, _ = author(tmp_path, {".. ": "x", "good.lib": "y"})

    assert result.ok is False
    assert result.detail.startswith("api_write_refused:"), result.detail
    assert written_files(tmp_path) == [], "nothing is half-applied, good.lib included"


def test_an_ordinary_name_still_writes(tmp_path: Path) -> None:
    """The refusals must not cost the normal case: a plain relative name still lands."""
    result, request = author(tmp_path, {"BM_PATHS.lib": LIB_TEXT})

    assert result.ok is True, result.detail
    assert (request.model_dir / "BM_PATHS.lib").read_text(encoding="utf-8") == LIB_TEXT


# --------------------------------------------------------------------------- #
# names that cannot be written at all


def test_a_nul_in_a_reply_name_is_refused_and_the_reply_is_not_half_applied(
    tmp_path: Path,
) -> None:
    """The audit's reply: a good file followed by a NUL name."""
    result, _ = author(tmp_path, {"good.lib": "y", "bad\u0000.lib": "x"})

    assert result.ok is False
    assert result.detail.startswith("api_write_refused:"), result.detail
    assert "control character" in result.detail
    assert written_files(tmp_path) == [], "the NUL is refused before good.lib is written"


# --------------------------------------------------------------------------- #
# a write that cannot be performed


def test_a_value_error_from_the_write_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that raises ``ValueError`` (the NUL class) is a returned note, never a raise."""
    write = api_backend._write_atomically

    def failing(target: Path, content: str) -> None:
        if target.name == "second.lib":
            raise ValueError("embedded null byte")
        write(target, content)

    monkeypatch.setattr(api_backend, "_write_atomically", failing)

    result, request = author(tmp_path, {"first.lib": "x", "second.lib": "y"})

    assert result.ok is False
    assert result.detail.startswith("api_write_failed: ValueError:"), result.detail
    assert "may be incomplete" in result.detail
    assert (request.model_dir / "first.lib").is_file(), "the earlier file is the honest state"


def test_an_unwritable_target_is_reported_with_the_incomplete_note(tmp_path: Path) -> None:
    """A real failure (``os.replace`` onto a directory) is returned, not raised."""
    request = request_for(tmp_path)
    (request.model_dir / "blocked.lib").mkdir()  # a directory where the file must go
    backend = ApiKeyBackend(
        provider(),
        transport=Recorder(reply({"first.lib": "x", "blocked.lib": "y"})),
        credential_lookup=with_key(),
    )

    result = backend.author(request)

    assert result.ok is False
    assert result.detail.startswith("api_write_failed:"), result.detail
    assert "may be incomplete" in result.detail
    assert (request.model_dir / "first.lib").is_file(), "the earlier file is the honest state"
