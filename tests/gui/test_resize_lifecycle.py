"""Resizing cannot replace the restore geometry or scatter the compact input rows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module", autouse=True)
def system_fonts(qapp):
    from PySide6.QtGui import QFontDatabase

    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    ids = []
    for name in ("segoeui.ttf", "segoeuib.ttf", "consola.ttf"):
        if (fonts / name).is_file():
            ids.append(QFontDatabase.addApplicationFont(str(fonts / name)))
    yield
    for font_id in ids:
        QFontDatabase.removeApplicationFont(font_id)


@pytest.fixture
def windows(qtbot, monkeypatch):
    from boardmodeler.config import AppConfig
    from boardmodeler.ui import setup_dialog
    from boardmodeler.ui.model_maker import ModelMakerWindow

    config = AppConfig()
    monkeypatch.setattr("boardmodeler.config.load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr("boardmodeler.config.save_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_dialog, "load_config", lambda *args, **kwargs: config)
    maker, setup = ModelMakerWindow(), setup_dialog.SetupDialog()
    for widget in (maker, setup):
        qtbot.addWidget(widget)
        widget.show()
        qtbot.wait(10)
    return maker, setup


def test_maximized_panel_changes_preserve_maximized_state_and_restore_geometry(windows, qtbot):
    maker, _setup = windows
    original = maker.normalGeometry()
    for _ in range(3):
        maker.showMaximized()
        qtbot.wait(10)
        for button in (maker.advanced_button, maker.details_button):
            button.click()
            qtbot.wait(10)
            assert maker.isMaximized(), "expansion must not resize a maximized window"
            assert maker.normalGeometry() == original, "restore size belongs to the user"
        for button in (maker.details_button, maker.advanced_button):
            button.click()
        maker.showNormal()
        qtbot.wait(10)
        assert maker.normalGeometry() == original and maker.size() == original.size()


def test_collapsed_rows_remain_compact_when_the_window_is_enlarged(windows, qtbot):
    maker, _setup = windows
    initial_part_y = maker.part_edit.geometry().top()
    initial_progress_y = maker.progress.geometry().top()
    for change in (lambda: maker.resize(1240, 900), maker.showMaximized, maker.showNormal):
        change()
        qtbot.wait(10)
        assert maker.part_edit.geometry().top() == initial_part_y
        assert maker.progress.geometry().top() == initial_progress_y
        assert maker.elapsed_label.height() == maker.go_button.height()


def test_expanded_controls_are_scrollable_at_the_compact_window_size(windows, qtbot):
    maker, _setup = windows
    maker.advanced_button.click()
    maker.details_button.click()
    maker.resize(maker.minimumSize())
    qtbot.wait(20)
    assert maker.scroll_area.verticalScrollBar().maximum() > 0
    for control in (maker.out_edit, maker.engine_combo, maker.environment_button, maker.rows):
        maker.scroll_area.ensureWidgetVisible(control, 0, 0)
        qtbot.wait(5)
        assert control.isVisible() and control.width() > 0 and control.height() > 0
        top = control.mapTo(maker.scroll_area.viewport(), control.rect().topLeft())
        assert top.y() < maker.scroll_area.viewport().height()
        assert top.y() + control.height() > 0


def test_setup_repeated_maximize_restore_and_narrow_wide_keep_wrapped_rows(windows, qtbot):
    _maker, setup = windows
    original = setup.normalGeometry()
    for _ in range(3):
        setup.showMaximized()
        qtbot.wait(10)
        setup.showNormal()
        qtbot.wait(10)
        assert setup.normalGeometry() == original
        for size in (setup.minimumSize(), original.size()):
            setup.resize(size)
            qtbot.wait(10)
            hint = setup.key_hint
            next_row = setup.model_edit if setup.model_edit.isVisible() else setup.model_dir_edit
            assert hint.height() >= hint.heightForWidth(hint.width())
            assert hint.mapTo(setup.page, hint.rect().bottomLeft()).y() < next_row.geometry().top()
