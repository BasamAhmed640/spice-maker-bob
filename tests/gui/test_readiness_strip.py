"""The build window shows key/tool readiness and verifies it on request."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from boardmodeler.security.readiness import Check
from boardmodeler.ui.readiness_strip import STATE_COLOURS, ReadinessStrip


def _checks(key_state: str, model_state: str) -> list[Check]:
    return [
        Check("key", "API KEY", key_state, "key detail"),
        Check("model", "MODEL", model_state, "model detail"),
        Check("ltspice", "LTSPICE", "ok", "ran"),
        Check("pdf", "PDF", "ok", "ok"),
        Check("ocr", "OCR", "warn", "no tesseract"),
        Check("internet", "INTERNET", "ok", "on"),
    ]


def test_lights_colour_by_state_and_verify_repaints_them(qtbot):
    strip = ReadinessStrip(
        local=lambda: _checks("unchecked", "unchecked"),
        verify=lambda cancel=None: _checks("ok", "fail"),
    )
    qtbot.addWidget(strip)
    strip.refresh_local()
    assert strip.state_of("key") == "unchecked"
    assert STATE_COLOURS["unchecked"] in strip.lights["key"].styleSheet()
    assert "key detail" in strip.lights["key"].toolTip()

    strip.start_verify()
    assert not strip.verify_button.isEnabled()
    qtbot.waitUntil(lambda: strip.verify_button.isEnabled(), timeout=5000)
    assert strip.state_of("key") == "ok"
    assert STATE_COLOURS["ok"] in strip.lights["key"].styleSheet()
    assert strip.state_of("model") == "fail"
    assert STATE_COLOURS["fail"] in strip.lights["model"].styleSheet()
    assert "problem" in strip.verify_button.toolTip()


def test_a_verification_that_raises_is_reported_not_swallowed(qtbot):
    def boom(cancel=None):
        raise RuntimeError("network gone")

    strip = ReadinessStrip(local=lambda: _checks("unchecked", "unchecked"), verify=boom)
    qtbot.addWidget(strip)
    strip.start_verify()
    qtbot.waitUntil(lambda: strip.verify_button.isEnabled(), timeout=5000)
    assert strip.state_of("key") == "warn"
    assert "did not finish" in strip.lights["key"].toolTip()


def test_the_build_window_carries_the_strip(qtbot, monkeypatch):
    from boardmodeler.ui import model_maker

    window = model_maker.ModelMakerWindow()
    qtbot.addWidget(window)
    assert window.readiness.verify_button.text() == "VERIFY KEY && TOOLS"
    assert set(window.readiness.lights) == {"key", "model", "ltspice", "pdf", "ocr", "internet"}
