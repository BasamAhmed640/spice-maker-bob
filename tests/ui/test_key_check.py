from __future__ import annotations

import threading

import pytest

from boardmodeler.security.key_verification import KeyVerification
from boardmodeler.ui.setup_dialog import SetupDialog


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BOARDMODELER_CONFIG", str(path))
    return path


def test_check_runs_off_ui_thread_and_shows_result(qtbot, isolated_config, monkeypatch):
    ready, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def verify(provider, key, **kw):
        assert threading.get_ident() != main_thread
        assert key == "fake-check-secret"
        ready.set()
        release.wait(3)
        return KeyVerification("verified", "Provider accepted the key.")

    monkeypatch.setattr("boardmodeler.ui.setup_dialog.verify_key", verify)
    monkeypatch.setattr("boardmodeler.security.credentials.set_credential", lambda *a: None)
    page = SetupDialog()
    qtbot.addWidget(page)
    page.key_edit.setText("fake-check-secret")
    page.save_key_button.click()
    qtbot.waitUntil(ready.is_set)
    assert not page.save_key_button.isEnabled()
    assert page.key_edit.text() == ""
    assert "fake-check-secret" not in isolated_config.read_text()
    release.set()
    qtbot.waitUntil(lambda: page._key_check is None)
    assert "VERIFIED" in page.key_status.text()
    assert page.save_key_button.isEnabled()


def test_timeout_and_late_completion_cannot_show_success(qtbot, isolated_config, monkeypatch):
    release = threading.Event()

    def verify(*args, **kw):
        release.wait(3)
        return KeyVerification("verified", "late")

    monkeypatch.setattr("boardmodeler.ui.setup_dialog.verify_key", verify)
    page = SetupDialog()
    qtbot.addWidget(page)
    page._start_key_check("fake-check-secret")
    cancel, results, started = page._key_check
    page._key_check = (cancel, results, started - 20)
    page._poll_key_check()
    assert cancel.is_set()
    assert "UNVERIFIED" in page.key_status.text()
    release.set()
    qtbot.waitUntil(lambda: bool(results))
    page._poll_key_check()
    assert "UNVERIFIED" in page.key_status.text()
