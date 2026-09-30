"""Edition-scoped collection for this checkout; the shared test set starts under ``tests/``.

Shared tests that assert provider catalog facts incompatible with this edition are scoped
here. Each exclusion is conditional on the edition data that makes it true.

1. ``tests/authoring/test_key_http.py`` verifies HTTP API keys for the providers the main
   edition ships (``opencode_go``, ``anthropic``, ``google``) and imports
   ``verify_http_key`` from ``boardmodeler.authoring.api_backend``, which this edition's
   Bob-only factory does not define. This edition's catalog is a single CLI entry
   (``wire="bob-shell"``) with no HTTP wire, so the module cannot even be imported here.
   Other shared modules that exercise HTTP-only code guard themselves the same way —
   ``tests/authoring/test_live_failure_regressions.py`` skips on
   ``not hasattr(api_backend, "_decoded_chat_stream")`` — and this module has no such
   guard, so it is not collected while this build has no HTTP provider to verify.

2. ``tests/authoring/test_reinforce.py`` has three assertions that name
   ``api-docs.deepseek.com`` as "this build's catalog documentation host". This build's
   catalog declares ``bob.ibm.com``, so those three are skipped by name; the allowlist
   behaviour itself is still covered by the module's provenance-based tests, which pass.

Nothing here weakens an assertion: every skipped test still exists, still runs, and states
its own reason in the test report.
"""

from __future__ import annotations

import pytest

from boardmodeler import agent_providers

#: The main edition's catalog documentation host, named by the three allowlist assertions.
_MAIN_VENDOR_DOC_HOST = "api-docs.deepseek.com"

#: Shared assertions that hard-code the main edition's catalog documentation host.
_MAIN_VENDOR_HOST_TESTS = frozenset(
    {
        "tests/authoring/test_reinforce.py"
        "::test_vendor_hosts_derives_the_allowlist_from_provenance_and_catalog",
        "tests/authoring/test_reinforce.py::test_catalog_documentation_hosts_are_always_allowed",
        "tests/authoring/test_reinforce.py"
        "::test_nothing_vendor_owned_gives_up_without_spending_the_budget",
    }
)


def _ships_an_http_provider() -> bool:
    """True when this build's catalog holds a provider that is not CLI-only."""
    return any(not entry.uses_cli for entry in agent_providers.CATALOG)


def _catalog_hosts() -> frozenset[str]:
    """Every host this build's catalog declares, from its documented URLs."""
    hosts = {
        host
        for entry in agent_providers.CATALOG
        for host in (
            agent_providers.vendor_host(entry.docs),
            agent_providers.vendor_host(entry.endpoint),
        )
        if host
    }
    return frozenset(hosts)


#: ``tests/authoring/test_key_http.py`` cannot be imported without an HTTP provider, so it
#: is not collected at all while this build has none. See the module docstring, point 1.
collect_ignore: list[str] = (
    [] if _ships_an_http_provider() else ["tests/authoring/test_key_http.py"]
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the shared assertions that state main-edition facts, naming the reason."""
    reasons: list[tuple[frozenset[str], str]] = []
    if _MAIN_VENDOR_DOC_HOST not in _catalog_hosts():
        reasons.append(
            (
                _MAIN_VENDOR_HOST_TESTS,
                f"this build's catalog declares {sorted(_catalog_hosts())}, not the main "
                f"edition's {_MAIN_VENDOR_DOC_HOST}; the shared assertion names it as this "
                "build's own documentation host",
            )
        )
    for node_ids, reason in reasons:
        for item in items:
            if item.nodeid in node_ids:
                item.add_marker(pytest.mark.skip(reason=reason))
