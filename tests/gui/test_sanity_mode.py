import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


def test_full_verification_is_opt_in_and_saved(qtbot, tmp_path, monkeypatch):
    from boardmodeler.config import AppConfig
    from boardmodeler.ui import setup_dialog as setup

    config = AppConfig()
    saved = []
    monkeypatch.setattr(setup, "load_config", lambda: config)
    monkeypatch.setattr(setup, "portable", lambda: False)
    monkeypatch.setattr(
        setup, "save_config", lambda value: saved.append(value) or tmp_path / "config.json"
    )
    dialog = setup.SetupDialog()
    qtbot.addWidget(dialog)
    assert not dialog.full_verification_check.isChecked()
    dialog.full_verification_check.setChecked(True)
    dialog._save()
    assert saved[0].full_verification is True


def test_quick_result_is_unverified_and_full_action_uses_worker(qtbot, tmp_path, monkeypatch):
    from boardmodeler.pipeline.make_model import MakeModelRequest, MakeModelResult
    from boardmodeler.ui import model_maker as ui

    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    request = MakeModelRequest(
        "TEST", "TEST", tmp_path / "test.pdf", tmp_path, verification="sanity"
    )
    result = SimpleNamespace(
        status="UNKNOWN",
        detail="Sanity checked; electrical accuracy unverified",
        request=request,
        lib_path=tmp_path / "TEST.lib",
    )
    window._out_dir = tmp_path
    window._on_result(result)
    assert "accuracy unverified" in window.status_label.text()
    assert window.again_button.text() == "Run full verification"
    started = []
    monkeypatch.setattr(window, "_start", started.append)
    window.again_button.click()
    assert started[0].verification == "full"
    assert started[0].datasheet == request.datasheet
    persisted = MakeModelResult(
        "UNKNOWN", "quick", "TEST", tmp_path, None, None, None, (), {}, (), request
    )
    assert MakeModelResult.from_json(persisted.to_json()).request.verification == "sanity"
