"""Qt test configuration: every GUI test runs offscreen, without a display."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def no_live_key_checks(monkeypatch):
    from boardmodeler.security.key_verification import KeyVerification

    monkeypatch.setattr(
        "boardmodeler.ui.setup_dialog.verify_key",
        lambda *args, **kwargs: KeyVerification("unverified", "Offline test."),
    )
