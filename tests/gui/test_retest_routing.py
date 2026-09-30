"""Full verification retests saved bytes; quick mode explicitly requests a full build."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest
from tests.authoring.test_retest import saved as _saved_fixture

saved = _saved_fixture

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


def test_full_retest_uses_saved_bytes_in_background_and_updates_unknown_rows(
    qtbot, saved, monkeypatch
):
    from PySide6.QtCore import QTimer

    from boardmodeler.authoring import harness
    from boardmodeler.pipeline import make_model as engine
    from boardmodeler.ui import model_maker as ui

    out, spec, _design, lib, symbol, calls = saved
    entered, release = threading.Event(), threading.Event()
    original = harness.run_harness

    def holding_harness(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    monkeypatch.setattr(harness, "run_harness", holding_harness)
    monkeypatch.setattr(
        engine,
        "make_model",
        lambda *args, **kwargs: pytest.fail("Retesting must not regenerate the model"),
    )
    errors = []
    monkeypatch.setattr(ui.QMessageBox, "critical", lambda *args: errors.append(args))
    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window._out_dir = out
    request = engine.MakeModelRequest(spec.part, spec.subckt, out / "unused.pdf", out)
    window._result = SimpleNamespace(request=request)
    before, symbol_before = lib.read_bytes(), symbol.read_bytes()
    window._rerun_tests()
    worker = window._worker
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        assert window._elapsed_timer.isActive() and not window.go_button.isEnabled()
        responsive = []
        QTimer.singleShot(0, lambda: responsive.append(True))
        qtbot.waitUntil(lambda: bool(responsive), timeout=1000)
    finally:
        release.set()
    qtbot.waitUntil(lambda: window._result is not None or bool(errors), timeout=5000)
    assert not errors, errors
    assert worker.wait(5000)
    assert calls and window._result.status == "UNKNOWN"
    assert window._result.counts["UNKNOWN"] > 0 and window.rows.rowCount() == len(
        spec.characteristics
    )
    assert lib.read_bytes() == before and symbol.read_bytes() == symbol_before
    assert not window._elapsed_timer.isActive() and window.install_button.isEnabled()


def test_quick_mode_full_verification_explicitly_builds_again(qtbot, tmp_path, monkeypatch):
    from boardmodeler.pipeline.make_model import MakeModelRequest
    from boardmodeler.ui import model_maker as ui

    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window._out_dir = tmp_path
    request = MakeModelRequest(
        "SYNTH",
        "SYNTH",
        tmp_path / "synthetic.pdf",
        tmp_path,
        verification="sanity",
        engine="legacy_ai",
    )
    window._result = SimpleNamespace(request=request)
    builds = []
    monkeypatch.setattr(window, "_start", builds.append)
    window._rerun_tests()
    assert len(builds) == 1 and builds[0].verification == "full"
    assert builds[0].engine == "legacy_ai" and builds[0].out_dir == tmp_path
