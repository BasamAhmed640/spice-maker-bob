"""Per-build route choices must reach the pipeline without unrelated provider gates."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    from boardmodeler.config import AppConfig
    from boardmodeler.ui import model_maker as ui
    from boardmodeler.ui import setup_dialog

    config = AppConfig(full_verification=False)
    # Import consumers before patching so a deferred readiness refresh cannot retain
    # our temporary load_config function after this fixture is torn down.
    monkeypatch.setattr(setup_dialog, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("boardmodeler.config.load_config", lambda *a, **k: config)
    monkeypatch.setattr("boardmodeler.config.save_config", lambda *a, **k: None)
    monkeypatch.setattr(ui, "_configured_provider", lambda: None)
    monkeypatch.setattr(ui, "_configured_provider_id", lambda: "unavailable-provider")
    maker = ui.ModelMakerWindow()
    qtbot.addWidget(maker)
    datasheet = tmp_path / "part.pdf"
    datasheet.write_bytes(b"%PDF-1.4\n")
    maker.part_edit.setText("TPS54331")
    maker.datasheet_edit.setText(str(datasheet))
    maker.out_edit.setText(str(tmp_path / "model"))
    return maker


def _select(window, engine):
    window.engine_combo.setCurrentIndex(window.engine_combo.findData(engine))


def test_code_built_is_the_default_and_family_is_optional(window):
    assert window.engine_combo.currentData() == "behavioral"
    assert window.family_combo.currentData() is None
    assert not window.full_check.isEnabled()
    assert window.full_check.isChecked()


@pytest.mark.parametrize("engine", ["behavioral", "pin_only"])
@pytest.mark.parametrize("family", [None, "switching_regulator"])
def test_code_built_request_does_not_preflight_a_provider(window, monkeypatch, engine, family):
    from boardmodeler.ui import model_maker as ui

    def unrelated_preflight():
        pytest.fail("The pipeline decides whether extraction needs a provider")

    monkeypatch.setattr(ui, "_agent_availability", unrelated_preflight)
    started = []
    monkeypatch.setattr(window, "_start", started.append)
    _select(window, engine)
    window.family_combo.setCurrentIndex(window.family_combo.findData(family))

    window.go_button.click()

    assert len(started) == 1
    request = started[0]
    assert request.engine == engine
    assert request.family == family
    assert request.verification == "full"
    assert request.plan_tests is False
    assert request.provider == "unavailable-provider"
    assert window.full_check.isChecked() and not window.full_check.isEnabled()
    assert "Extraction may still need" in window.engine_hint.text()
    if engine == "pin_only":
        assert "no functional behavior" in window.engine_combo.currentText()
        assert "without functional behavior" in window.engine_hint.text()


def test_required_verification_does_not_overwrite_the_legacy_preference(window, monkeypatch):
    saved = []
    monkeypatch.setattr(
        "boardmodeler.config.save_config",
        lambda value, path=None: saved.append(value.full_verification),
    )
    _select(window, "behavioral")
    _select(window, "pin_only")
    _select(window, "legacy_ai")
    assert not window.full_check.isChecked()
    assert saved == []

    window.full_check.setChecked(True)
    _select(window, "behavioral")
    _select(window, "legacy_ai")
    assert window.full_check.isChecked()
    assert saved == [True]


@pytest.mark.parametrize("engine", ["legacy_ai", "behavioral", "pin_only"])
def test_build_choices_are_locked_until_the_run_finishes(window, engine):
    _select(window, engine)
    window._set_busy(True)
    assert not window.engine_combo.isEnabled()
    assert not window.family_combo.isEnabled()
    assert not window.full_check.isEnabled()

    window._set_busy(False)
    assert window.engine_combo.isEnabled()
    assert window.family_combo.isEnabled()
    assert window.full_check.isEnabled() == (engine == "legacy_ai")


def test_route_explanations_remain_readable_at_the_minimum_window_size(window, qtbot):
    window.show()
    qtbot.waitExposed(window)
    window.advanced_button.click()
    for engine in ("legacy_ai", "behavioral", "pin_only"):
        _select(window, engine)
        window.resize(window.minimumSize())
        qtbot.wait(10)
        hint = window.engine_hint
        assert hint.height() >= hint.heightForWidth(hint.width())
        assert (
            hint.mapTo(window.advanced_panel, hint.rect().bottomLeft()).y()
            < window.advanced_panel.height()
        )
