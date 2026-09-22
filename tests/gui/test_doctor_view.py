"""The environment report must be readable in full — the old surface showed only its tail.

``_run_doctor`` ran ``doctor --json`` and pushed ``message[-4000:]`` into a ``QMessageBox``,
so on a long report the head (version, config path, LTspice) was gone and the rest had to be
read a few lines at a time in a box that could not be resized. These tests pin the whole
report, the readable rendering, the copy action and the resizability of the page that
replaced it. The CLI invocation itself stays ``["doctor", "--json"]``, which
``tests/gui/test_window_contract.py`` asserts separately.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSize

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

#: The old surface kept exactly this much of the report; a report longer than this is what
#: made the head unreachable, so every probe here is built to exceed it.
OLD_SURFACE_CHARACTERS = 4000


def _report(detail_repeat: int = 120) -> str:
    """A doctor payload shaped like the real ``doctor --json``."""
    payload = {
        "tool": "boardmodeler",
        "version": "1.3.0",
        "python": "3.14.0",
        "platform": "win32",
        "executable": r"C:\app\python.exe",
        "config": {"path": r"C:\Users\a\config.json", "exists": True},
        "ltspice": {
            "found": True,
            "path": r"C:\tools\LTspice.exe",
            "version": "26.0.0",
            "source": "env",
            "lib_dir": r"C:\Users\a\AppData\Local\LTspice\lib",
            "smoke_test": "pass",
            "smoke_detail": "RC step measured 0.632119 V at 1 ms " * detail_repeat,
            "exit_code": 0,
        },
        "reader_backend": {
            "reader_backend": "native",
            "spicelib_version": None,
            "max_deviation": 0.0,
            "agreement_tolerance": 1e-9,
            "detail": "native reader",
        },
        "ocr": {"available": True, "reason": "", "detail": "tesseract"},
        "credentials": {"bob_shell": "missing"},
        "telemetry": "none",
        "ok": True,
    }
    raw = json.dumps(payload, indent=2)
    return raw


def _long_report() -> str:
    """A report longer than the 4000 characters the old surface could show at all."""
    raw = _report()
    assert len(raw) > OLD_SURFACE_CHARACTERS, "the probe is pointless under the old cap"
    return raw


@pytest.fixture
def window(qtbot):
    from boardmodeler.ui.model_maker import ModelMakerWindow

    maker = ModelMakerWindow()
    qtbot.addWidget(maker)
    maker.show()
    qtbot.waitExposed(maker)
    return maker


def _click_check_environment(window) -> None:
    for button in window.findChildren(type(window.go_button)):
        if button.text() == "CHECK ENVIRONMENT":
            button.click()
            return
    raise AssertionError("no CHECK ENVIRONMENT button")  # pragma: no cover - it must exist


def _stub_cli(monkeypatch, raw: str, returncode: int = 0) -> None:
    monkeypatch.setattr(
        "boardmodeler.ui.model_maker.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=raw, stderr="", returncode=returncode),
    )


def test_the_whole_report_survives_the_check_environment_button(qtbot, window, monkeypatch) -> None:
    """The actual bug: a report longer than 4000 characters lost its head and was truncated."""
    raw = _long_report()
    _stub_cli(monkeypatch, raw)
    shown: list[object] = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: shown.append(args)
    )

    _click_check_environment(window)

    view = window.doctor_view
    assert view is not None, "CHECK ENVIRONMENT must open the report page"
    assert shown == [], "the report must not go back through a message box"
    assert view.raw_json == raw, "nothing may be trimmed on the way in"
    assert view.raw_json[:OLD_SURFACE_CHARACTERS] == raw[:OLD_SURFACE_CHARACTERS], (
        "the head of the report — the part the old [-4000:] threw away — must be present"
    )
    assert "exit code 0" in view.summary_label.text()
    assert str(len(raw)) in view.summary_label.text()

    view.show_raw()
    assert view.report_text == raw, "the raw view holds the payload character for character"


def test_the_report_opens_readable_with_the_raw_json_one_click_away(
    qtbot, window, monkeypatch
) -> None:
    raw = _long_report()
    _stub_cli(monkeypatch, raw)
    _click_check_environment(window)
    view = window.doctor_view
    assert view is not None
    qtbot.addWidget(view)
    view.show()
    qtbot.waitExposed(view)

    readable = view.readable_report
    assert "boardmodeler 1.3.0" in readable, "the readable form names the version"
    assert "ltspice: " in readable and "smoke test: pass" in readable
    assert "reader backend: native" in readable
    assert view.report_text == readable, "a person reads the rendered report first"

    view.toggle_raw()
    assert view.report_text == raw
    view.toggle_raw()
    assert view.report_text == readable

    view.show_raw()
    view.copy_report()
    from PySide6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == raw, "COPY takes what is shown, in full"


def test_the_report_page_is_readable_software_not_a_fixed_box(qtbot, window) -> None:
    from PySide6.QtWidgets import QPlainTextEdit

    view = window._show_doctor(_long_report(), exit_code=7)
    qtbot.addWidget(view)
    view.show()
    qtbot.waitExposed(view)

    assert isinstance(view.report_view, QPlainTextEdit)
    assert view.report_view.isReadOnly() is True
    assert view.report_view.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap
    assert view.copy_button.isEnabled() and view.raw_button.isEnabled()

    assert view.minimumSize() != view.maximumSize(), "the report page must be resizable"
    view.resize(1000, 700)
    assert view.size() == QSize(1000, 700)
    view.resize(200, 150)
    assert view.size().height() < 700, "it must be shrinkable as well as enlargable"


def test_the_readable_form_is_the_commands_own_rendering() -> None:
    """The window must describe the report the way ``boardmodeler doctor`` does."""
    from boardmodeler.cli import _render_doctor_human
    from boardmodeler.ui.model_maker import readable_doctor_report

    raw = _report(detail_repeat=1)
    assert readable_doctor_report(raw) == _render_doctor_human(json.loads(raw))


def test_a_failed_check_still_shows_everything_it_printed(window) -> None:
    """A non-JSON report (a traceback on stderr) must be shown whole, not parsed away."""
    text = "doctor failed: " + "x" * 9000
    view = window._show_doctor(text, exit_code=2)
    assert view.raw_json == text
    assert view.report_text == text, "an unparsable report is still the report"
    assert len(view.report_text) > OLD_SURFACE_CHARACTERS
