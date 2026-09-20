"""The elapsed clock must keep ticking during a silent worker and stop on every exit."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.mark.parametrize("ending", ["PASS", "BLOCKED", "cancel", "error"])
def test_go_timer_survives_silent_work_and_preserves_final_time(
    qtbot, tmp_path, monkeypatch, ending
) -> None:
    from boardmodeler import agent_providers
    from boardmodeler.pipeline import make_model as pipeline
    from boardmodeler.ui import model_maker as ui

    clock = [100.0]
    release = threading.Event()
    entered = threading.Event()
    cancelled = []

    def silent_engine(request, progress, cancel):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test worker was not released")
        cancelled.append(cancel.is_set())
        if ending == "error":
            raise RuntimeError("test failure")
        return SimpleNamespace(status="UNKNOWN" if ending == "cancel" else ending)

    monkeypatch.setattr(pipeline, "make_model", silent_engine)
    monkeypatch.setattr(ui, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ui, "_agent_availability", lambda: (True, "test"))
    monkeypatch.setattr(ui, "_configured_provider", agent_providers.default_provider)
    monkeypatch.setattr(ui.QMessageBox, "critical", lambda *args: None)
    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window.show()
    datasheet = tmp_path / "timer-test.pdf"
    datasheet.write_bytes(b"%PDF-1.4")
    window.part_edit.setText("TIMER_TEST")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path / "out"))
    assert window.elapsed_label.text() == "ELAPSED 00:00:00"
    window.go_button.click()
    worker = window._worker
    try:
        qtbot.waitUntil(entered.is_set)
        clock[0] += 3661
        # There are deliberately no progress signals: Qt must update the clock itself.
        qtbot.waitUntil(lambda: window.elapsed_label.text() == "ELAPSED 01:01:01")
        assert not window.go_button.isEnabled()
        if ending == "cancel":
            window.cancel_button.click()
            clock[0] += 2
            qtbot.waitUntil(lambda: window.elapsed_label.text() == "ELAPSED 01:01:03")
            assert window.status_label.text() == "cancelling…"
        release.set()
        qtbot.waitUntil(window.go_button.isEnabled)
        assert worker.wait(1000)
        assert cancelled == [ending == "cancel"]
        final_time = window.elapsed_label.text()
        clock[0] += 500
        qtbot.wait(350)
        assert window.elapsed_label.text() == final_time
        assert not window._elapsed_timer.isActive()
        # A second GO starts a new duration, including after a failure or cancellation.
        release.clear()
        entered.clear()
        window.go_button.click()
        worker = window._worker
        qtbot.waitUntil(entered.is_set)
        assert window.elapsed_label.text() == "ELAPSED 00:00:00"
    finally:
        release.set()
        assert worker.wait(2000)
        qtbot.waitUntil(window.go_button.isEnabled)
