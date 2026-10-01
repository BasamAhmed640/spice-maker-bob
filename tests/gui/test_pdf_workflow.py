"""The simple input workflow preserves identity, local files and explicit legacy choices."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    from boardmodeler.config import AppConfig
    from boardmodeler.ui import model_maker as ui
    from boardmodeler.ui import setup_dialog

    config = AppConfig()
    monkeypatch.setattr("boardmodeler.config.load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr("boardmodeler.config.save_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_dialog, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(ui, "_default_model_dir", lambda: str(tmp_path / "models"))
    monkeypatch.setattr(ui, "_configured_provider", lambda: None)
    monkeypatch.setattr(ui, "_configured_provider_id", lambda: "")
    monkeypatch.setattr(ui.QMessageBox, "warning", lambda *args: pytest.fail(str(args[1:])))

    widget = ui.ModelMakerWindow()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_normal_inputs_hide_engine_decisions_and_keep_explicit_legacy(window):
    assert window.part_edit.isVisible() and window.datasheet_edit.isVisible()
    for control in (window.engine_combo, window.family_combo, window.out_edit, window.full_check):
        assert not control.isVisible()
    assert not hasattr(window, "package_combo")
    assert window.engine_combo.currentData() == "behavioral"
    assert window.family_combo.currentData() is None
    window.advanced_button.click()
    assert window.engine_combo.isVisible() and window.out_edit.isVisible()
    assert not window.full_check.isVisible()
    window.engine_combo.setCurrentIndex(window.engine_combo.findData("legacy_ai"))
    assert window.full_check.isVisible() and window.full_check.isEnabled()
    assert "legacy" in window.engine_combo.currentText().lower()


def test_picker_accepts_quoted_path_and_all_files_without_inventing_part(
    window, tmp_path, monkeypatch
):
    from boardmodeler.ui import model_maker as ui

    pdf = tmp_path / "UCC28251 Advanced PWM Controller long title.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic input fixture")
    window.datasheet_edit.setText(f'"{pdf}"')
    seen = []
    monkeypatch.setattr(
        ui.QFileDialog, "getOpenFileName", lambda *args: (seen.append(args) or str(pdf), "PDF")
    )
    window._choose_datasheet()
    assert seen[0][2] == str(pdf.parent)
    assert "All files (*)" in seen[0][3] and "PDF" in seen[0][3]
    assert window.datasheet_edit.text() == str(pdf)
    assert window.part_edit.text() == "", "a document title is not an ordering code"
    window.part_edit.setText("LM358")
    window._choose_datasheet()
    assert window.part_edit.text() == "LM358"


def test_full_ordering_code_and_quoted_pdf_start_exact_request(window, tmp_path, monkeypatch):
    from boardmodeler.ui import model_maker as ui

    pdf = tmp_path / "datasheet.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic input fixture")
    output = tmp_path / "chosen exact folder"
    window.part_edit.setText("UCC28251PW")
    window.datasheet_edit.setText(f'  "{pdf}"  ')
    window.out_edit.setText(f'"{output}"')
    monkeypatch.setattr(
        ui.QInputDialog, "getText", lambda *args: pytest.fail("full code is explicit")
    )
    requests = []
    monkeypatch.setattr(window, "_start", requests.append)
    window._make_model()
    assert len(requests) == 1
    request = requests[0]
    assert request.part == "UCC28251PW" and request.datasheet == pdf and request.out_dir == output
    assert request.engine == "behavioral" and request.family is None
    assert request.verification == "full" and not request.plan_tests


def test_local_pdf_drop_populates_only_datasheet_and_never_moves_or_builds(
    window, tmp_path, monkeypatch
):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    from boardmodeler.ui.pdf_input import dropped_pdf

    pdf = tmp_path / "a long document title.pdf"
    original = b"%PDF-1.4\nsynthetic drop fixture"
    pdf.write_bytes(original)
    window.part_edit.setText("LM358")
    monkeypatch.setattr(
        window, "_start", lambda *args: pytest.fail("a drop must not start generation")
    )
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(pdf))])
    event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.datasheet_edit.dropEvent(event)
    assert event.isAccepted() and event.dropAction() == Qt.CopyAction
    assert window.datasheet_edit.text() == str(pdf) and window.part_edit.text() == "LM358"
    assert pdf.read_bytes() == original
    window.datasheet_edit.clear()
    move_only = QDropEvent(QPointF(10, 10), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.datasheet_edit.dropEvent(move_only)
    assert not move_only.isAccepted() and window.datasheet_edit.text() == ""
    window_drop = QDropEvent(
        QPointF(10, 10), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier
    )
    window.dropEvent(window_drop)
    assert window_drop.isAccepted() and window_drop.dropAction() == Qt.CopyAction
    assert window.datasheet_edit.text() == str(pdf) and pdf.read_bytes() == original
    mime.setUrls([QUrl("https://example.invalid/datasheet.pdf")])
    assert dropped_pdf(mime) is None
    mime.setUrls([QUrl.fromLocalFile(str(pdf)), QUrl.fromLocalFile(str(pdf))])
    assert dropped_pdf(mime) is None
    other = tmp_path / "pretend.pdf"
    other.write_bytes(b"not a PDF")
    mime.setUrls([QUrl.fromLocalFile(str(other))])
    assert dropped_pdf(mime) is None
    mime.setUrls([QUrl.fromLocalFile(str(pdf))])
    window._set_busy(True)
    blocked = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.datasheet_edit.dropEvent(blocked)
    assert not blocked.isAccepted()
    window._set_busy(False)


def test_setup_new_root_refreshes_automatic_target_and_preserves_manual(
    window, tmp_path, monkeypatch
):
    from boardmodeler.ui import model_maker as ui
    from boardmodeler.ui.setup_dialog import SetupDialog

    root = tmp_path / "new root"
    monkeypatch.setattr(ui, "_default_model_dir", lambda: str(root))
    monkeypatch.setattr(SetupDialog, "exec", lambda self: 0)
    monkeypatch.setattr(window.readiness, "refresh_local", lambda: None)
    window.part_edit.setText("LM358")
    window._open_setup()
    assert Path(window.out_edit.text()) == root / "LM358"
    explicit = tmp_path / "chosen exact target"
    window.out_edit.setText(str(explicit))
    monkeypatch.setattr(ui, "_default_model_dir", lambda: str(tmp_path / "third root"))
    window._open_setup()
    assert Path(window.out_edit.text()) == explicit


def test_combo_popup_paints_dark_labels_on_white_rows(window, qtbot):
    window.advanced_button.click()
    combo = window.engine_combo
    combo.showPopup()
    qtbot.wait(50)
    view = combo.view()
    rect = view.visualRect(view.model().index(1, 0))
    image = view.viewport().grab().toImage()
    pixels = [
        image.pixelColor(x, y)
        for y in range(max(0, rect.top() + 3), min(image.height(), rect.bottom() - 2))
        for x in range(max(0, rect.left() + 5), min(image.width(), rect.right() - 5))
    ]
    combo.hidePopup()
    assert pixels and any(max(p.red(), p.green(), p.blue()) < 90 for p in pixels), (
        "label must render"
    )
    assert any(min(p.red(), p.green(), p.blue()) > 240 for p in pixels), (
        "unselected row stays white"
    )


def test_official_original_exposes_saved_files_without_broken_generated_actions(
    window, tmp_path, monkeypatch
):
    model = tmp_path / "vendor-originals" / "source" / "original" / "main.lib"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"* synthetic manufacturer-route UI fixture\n")
    window._out_dir = tmp_path
    window._on_result(
        SimpleNamespace(
            status="UNKNOWN",
            detail="Compatibility only",
            counts={"UNKNOWN": 1},
            out_dir=tmp_path,
            lib_path=model,
            rows=(),
        )
    )
    assert window.open_button.isEnabled() and window.saved_location.isVisible()
    assert not window.install_button.isEnabled() and not window.again_button.isEnabled()
    assert "supporting files" in window.install_button.toolTip()
    assert "compatibility only" in window.again_button.toolTip()
    assert "UNKNOWN" in window.status_label.text()
    monkeypatch.setattr(window, "_start", lambda *args: pytest.fail("no new build"))
    window._rerun_tests()
    window._install()
    assert model.read_bytes() == b"* synthetic manufacturer-route UI fixture\n"


def test_native_picker_is_enabled_by_application(qapp):
    from PySide6.QtCore import Qt

    from boardmodeler.ui.app import build_application

    assert not build_application([]).testAttribute(Qt.AA_DontUseNativeDialogs)
