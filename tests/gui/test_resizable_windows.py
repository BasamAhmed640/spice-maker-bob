"""Both windows must be draggable and maximisable; both used to be hard-fixed.

The assertion is the geometry a user feels, not the absence of a particular call: a window
whose minimum size equals its maximum size cannot be dragged at all, and that was the state
of ``ModelMakerWindow.setFixedSize(900, 600)`` and ``SetupDialog.setFixedSize(self.size())``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSize

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

#: Qt's default maximum for a widget that has never been constrained; anything near it
#: means "the window may be dragged to any size the screen allows".
UNCONSTRAINED = 5000


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The setup page reads and writes the real config otherwise."""
    target = tmp_path / "config.json"
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.ui.setup_dialog.config_path", lambda: target)
    return target


@pytest.fixture
def window(qtbot):
    from boardmodeler.ui.model_maker import ModelMakerWindow

    maker = ModelMakerWindow()
    qtbot.addWidget(maker)
    maker.show()
    qtbot.waitExposed(maker)
    return maker


def _assert_room_to_move(widget) -> None:
    assert widget.minimumSize() != widget.maximumSize(), (
        "minimum == maximum is a fixed size: the window cannot be dragged"
    )
    assert widget.maximumSize().width() >= UNCONSTRAINED, "it must be maximisable"
    assert widget.maximumSize().height() >= UNCONSTRAINED, "it must be maximisable"


def test_the_model_window_can_be_resized(qtbot, window) -> None:
    """It used to be locked at 900x600; now that size is where it opens, not where it stays."""
    _assert_room_to_move(window)
    starts_at = window.size()
    assert starts_at == QSize(900, 600), "the working size the owner knows"
    assert window.minimumSize().width() < starts_at.width()
    assert window.minimumSize().height() < starts_at.height()

    window.resize(1180, 780)
    assert window.size() == QSize(1180, 780), "enlarging must take effect"

    window.resize(600, 300)  # smaller than the content floor
    assert window.size().height() < starts_at.height(), "shrinking must take effect too"
    floor = window.minimumSize()
    assert window.width() >= floor.width() and window.height() >= floor.height(), (
        "Qt clamps to the content floor, never past it"
    )


def test_the_model_window_keeps_every_control_when_shrunk_to_its_minimum(qtbot, window) -> None:
    """A resizable window may not become a window with unreachable controls."""
    window.resize(window.minimumSize())
    for name in ("part_edit", "datasheet_edit", "out_edit", "go_button", "cancel_button"):
        widget = getattr(window, name)
        assert widget.isVisible(), f"{name} must stay reachable at the minimum size"
        assert widget.width() > 0 and widget.height() > 0, f"{name} must have real room"
    assert window.elapsed_label.isVisible()
    assert window.hourglass.isVisible()
    assert window.stages.height() > 0 and window.rows.height() > 0


def test_the_setup_page_can_be_resized(qtbot, isolated_config: Path) -> None:
    """Setup was fixed to its own content size, so it could not be enlarged at all."""
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)

    _assert_room_to_move(page)
    assert page.minimumSize().height() < page.height()
    assert page.size() == page.content_size(), "it still opens showing all of its content"

    page.resize(1240, 820)
    assert page.size() == QSize(1240, 820), "enlarging must take effect"

    page.resize(600, 420)
    assert page.size().width() < 1240, "the page must be shrinkable for a small screen"
    assert page.size().width() >= page.minimumSize().width()


def test_the_settings_stay_scrollable_when_the_page_is_small(qtbot, isolated_config: Path) -> None:
    """Shrinking the page must scroll the settings, not clip them away."""
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)
    assert not page.scroll_area.horizontalScrollBar().isVisible(), (
        "at the content size nothing needs scrolling"
    )

    page.resize(page.minimumSize())
    viewport = page.scroll_area.viewport()
    content = page.scroll_area.widget()
    assert content is not None, "the scroll area must hold the settings page"
    assert content.minimumSizeHint().width() > viewport.width(), (
        "the probe only means something if the page is too wide"
    )
    qtbot.waitUntil(lambda: page.scroll_area.horizontalScrollBar().isVisible(), timeout=2_000)


def test_the_scroll_area_rule_does_not_repaint_the_settings_buttons(
    qtbot, isolated_config: Path
) -> None:
    """The regression the model window already guards: a background rule over the controls.

    The scroll area added for resizing carries a background rule of its own, so the buttons
    inside it are sampled from the rendered pixels: an enabled button must not be black.
    """
    from PySide6.QtWidgets import QPushButton

    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)

    image = page.grab().toImage()
    save = next(button for button in page.findChildren(QPushButton) if button.text() == "SAVE")
    assert save.isEnabled()
    top_left = save.mapTo(page, save.rect().topLeft())
    # Inside the button's own padding, clear of the black glyphs of its label.
    background = image.pixelColor(top_left.x() + 4, top_left.y() + save.height() // 2)
    sampled = (background.red(), background.green(), background.blue())
    assert sampled != (0, 0, 0), f"SAVE renders black ({sampled}); it would be invisible"
    assert sampled in {(170, 170, 170), (255, 255, 255)}, sampled
