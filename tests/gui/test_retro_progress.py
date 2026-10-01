"""The compact screen keeps diagnostics reachable and progress honest."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


def test_progress_stays_visible_without_inventing_a_percentage(qtbot):
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.progress.isVisible()
    assert not window.details_panel.isVisible()
    window._set_busy(True)
    assert window.progress.minimum() == window.progress.maximum() == 0
    assert not window.progress.isTextVisible()
    window._on_stage(SimpleNamespace(stage="extract", status="running", detail="Reading datasheet"))
    window.details_button.click()
    assert window.stages.isVisible() and window.readiness.isVisible()
    assert window.stages.item(0, 2).text() == "Reading datasheet"
    window._on_result(
        SimpleNamespace(status="BLOCKED", detail="No supported function", counts={"UNKNOWN": 1})
    )
    assert window.progress.maximum() == 1
    assert window.progress.isTextVisible()
    assert window.status_label.text() == "BLOCKED — No supported function"
    assert window.counts_label.text() == "1 unknown"
    assert not window.install_button.isEnabled()


@pytest.mark.parametrize("package", ["UCC28251PW", "UCC28251RGP"])
def test_ambiguous_ucc_part_requires_the_user_package(qtbot, tmp_path, monkeypatch, package):
    from boardmodeler.ui import model_maker as ui

    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window.show()
    window.part_edit.setText("UCC28251")
    assert not hasattr(window, "package_combo")
    pdf = tmp_path / "datasheet.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    window.datasheet_edit.setText(str(pdf))
    window.out_edit.setText(str(tmp_path / "model"))
    started, warned = [], []
    monkeypatch.setattr(window, "_start", started.append)
    monkeypatch.setattr(ui.QMessageBox, "warning", lambda *args: warned.append(args))
    monkeypatch.setattr(ui.QInputDialog, "getText", lambda *args: ("", False))
    window.go_button.click()
    assert not started
    assert not (tmp_path / "model").exists(), "no build may begin before package selection"
    monkeypatch.setattr(ui.QInputDialog, "getText", lambda *args: (package, True))
    window.go_button.click()
    assert len(started) == 1
    assert started[0].part == package
    assert started[0].engine == "behavioral"
    assert window.part_edit.text() == package, "the resolved ordering code remains visible"
    window._set_busy(True)
    assert not window.part_edit.isEnabled()
    window._set_busy(False)


def test_default_save_folder_follows_part_and_package_but_preserves_an_exact_choice(
    qtbot, tmp_path, monkeypatch
):
    from boardmodeler.ui import model_maker as ui

    monkeypatch.setattr(ui, "_default_model_dir", lambda: str(tmp_path / "models"))
    window = ui.ModelMakerWindow()
    qtbot.addWidget(window)
    window.part_edit.setText("LM358")
    assert window.out_edit.text() == str(tmp_path / "models" / "LM358")
    window.part_edit.setText("UCC28251")
    window.part_edit.setText("UCC28251PW")
    assert window.out_edit.text() == str(tmp_path / "models" / "UCC28251PW")
    explicit = tmp_path / "my chosen model folder"
    window.out_edit.setText(str(explicit))
    window.part_edit.setText("UCC28251RGP")
    window.part_edit.setText("LM358")
    assert window.out_edit.text() == str(explicit)


@pytest.mark.parametrize("published", [False, True])
def test_unknown_saved_model_is_distinguished_from_withheld_model(qtbot, tmp_path, published):
    from boardmodeler.ui.model_maker import ModelMakerWindow
    from boardmodeler.ui.theme import DESKTOP

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    model = tmp_path / "synthetic-ui-result.lib"
    if published:
        model.write_text("* Synthetic GUI publication fixture\n", encoding="utf-8")
    detail = "Some datasheet behaviors are unmeasured"
    window._on_result(
        SimpleNamespace(
            status="UNKNOWN",
            detail=detail,
            counts={"UNKNOWN": 2},
            lib_path=model if published else None,
        )
    )
    headline = "Model saved — UNKNOWN (limited coverage)" if published else "UNKNOWN"
    assert window.status_label.text() == f"{headline} — {detail}"
    assert DESKTOP["unknown"] in window.status_label.styleSheet()
    assert window.counts_label.text() == "2 unknown"
    assert window.install_button.isEnabled() is published
    assert window.open_button.isEnabled() and window.again_button.isEnabled()
