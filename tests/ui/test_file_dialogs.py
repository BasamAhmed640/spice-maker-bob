"""Every chooser hands Qt a starting *directory* that exists — never the file it picks.

Qt's third argument to ``getOpenFileName``/``getExistingDirectory`` is the folder the
dialog opens at. Handed a path that is not an existing directory — the PDF a model is
built from, a stale LTspice path, a model folder that has been deleted — the native
dialog picks a place of its own: in practice the process's current drive root,
"Look in: C:\\", with nothing the user wants in view and Open that cannot succeed.

These tests record exactly what each handler passes for the four shapes a path field can
hold, and assert the observable the owner asked for: an existing folder, never a drive
root. The file is *shared* — ``tests/ui/test_setup_dialog.py`` is flavour-specific and
excluded from ``tools/sync_shared_core.py``, so the contract both editions must keep is
pinned here, where the sync copies it to the Bob edition.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    """Point every config read/write at a throwaway file, as the setup page expects."""
    target = tmp_path / "config.json"
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.ui.setup_dialog.config_path", lambda: target)
    return target


def _field_cases(tmp_path: Path) -> list[tuple[str, str, str]]:
    """Four shapes a path field can hold: (what it is, what it holds, where it must open)."""
    pdf = tmp_path / "datasheets" / "tps54320.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 test fixture")
    folder = tmp_path / "models"
    folder.mkdir(exist_ok=True)
    return [
        ("a file", str(pdf), str(pdf.parent)),
        ("a path that does not exist", str(tmp_path / "gone" / "nothing.pdf"), str(Path.home())),
        ("an empty value", "", str(Path.home())),
        ("a folder with a trailing separator", str(folder) + os.sep, str(folder)),
    ]


def _assert_usable(seen: list[str], case: str, expected: str) -> None:
    """The one property the user cares about, for whichever handler just ran."""
    assert seen[-1] == expected, f"{case}: must open at {expected!r}, not {seen[-1]!r}"
    assert Path(seen[-1]).is_dir(), f"{case}: {seen[-1]!r} is not a folder that exists"
    assert seen[-1] != Path(seen[-1]).anchor, f"{case}: must never open at the drive root"


def _record_open(monkeypatch, module: str, seen: list[str]) -> None:
    """Stand in for the Open dialog, recording the starting directory it was handed."""

    def record(parent, caption, directory, *rest):
        seen.append(directory)
        return "", ""

    monkeypatch.setattr(f"{module}.QFileDialog.getOpenFileName", record)


def _record_dir(monkeypatch, module: str, seen: list[str]) -> None:
    """Stand in for the folder picker, recording the starting directory it was handed."""

    def record(parent, caption, directory, *rest):
        seen.append(directory)
        return ""

    monkeypatch.setattr(f"{module}.QFileDialog.getExistingDirectory", record)


def test_the_starting_directory_is_always_an_existing_folder(tmp_path: Path) -> None:
    """The shared rule itself, before any widget is involved."""
    from boardmodeler.ui.file_dialogs import starting_directory

    for case, value, expected in _field_cases(tmp_path):
        chosen = starting_directory(value)
        assert chosen == expected, f"{case}: {value!r} must open at {expected!r}"
        assert Path(chosen).is_dir()
        assert chosen != Path(chosen).anchor, f"{case}: never the drive root"

    assert starting_directory("  ") == str(Path.home()), "blank is as empty as empty"


def test_the_setup_page_choosers_hand_qt_an_existing_folder(
    qtbot, isolated_config: Path, tmp_path: Path, monkeypatch
) -> None:
    """LTspice.exe and the model folder: the two fields the owner's report named."""
    monkeypatch.delenv("LTSPICE_EXE", raising=False)
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    cases = _field_cases(tmp_path)

    open_seen: list[str] = []
    _record_open(monkeypatch, "boardmodeler.ui.setup_dialog", open_seen)
    for case, value, expected in cases:
        page.ltspice_edit.setText(value)
        page.browse_ltspice_button.click()
        _assert_usable(open_seen, f"LTspice, {case}", expected)

    dir_seen: list[str] = []
    _record_dir(monkeypatch, "boardmodeler.ui.setup_dialog", dir_seen)
    for case, value, expected in cases:
        page.model_dir_edit.setText(value)
        page._choose_model_dir()
        _assert_usable(dir_seen, f"model folder, {case}", expected)


def test_the_datasheet_chooser_hands_qt_an_existing_folder(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """The datasheet field holds a PDF path, which Qt must never be handed as a folder."""
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    seen: list[str] = []
    _record_open(monkeypatch, "boardmodeler.ui.model_maker", seen)

    for case, value, expected in _field_cases(tmp_path):
        window.datasheet_edit.setText(value)
        window._choose_datasheet()
        _assert_usable(seen, f"datasheet, {case}", expected)


def test_the_model_folder_chooser_hands_qt_an_existing_folder(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """A configured save folder can also be a file, stale, or cleared to nothing."""
    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    seen: list[str] = []
    _record_dir(monkeypatch, "boardmodeler.ui.model_maker", seen)

    for case, value, expected in _field_cases(tmp_path):
        window.out_edit.setText(value)
        window._choose_out()
        _assert_usable(seen, f"save location, {case}", expected)
