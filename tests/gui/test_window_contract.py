"""The two things the owner asked the window to get right, checked on rendered output.

1. Button colours. The earlier window appended a bare ``QWidget`` background rule that
   tied with ``QPushButton`` on specificity and, being later, won: every enabled button
   was painted black while its text stayed black, so MAKE MODEL rendered as an empty box.
   These tests sample the actual rendered pixels, which is the only way to catch that.
2. The build inputs. The window holds a part number, a datasheet, a save location, GO and
   the progress detail — and nothing that belongs in setup.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot):
    from boardmodeler.ui.model_maker import ModelMakerWindow

    maker = ModelMakerWindow()
    qtbot.addWidget(maker)
    maker.show()
    qtbot.waitExposed(maker)
    return maker


def _sample(window, widget, *, dx: int = 12) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Rendered (background, centre) colours of a button, straight from the pixels."""
    image = window.grab().toImage()
    rect = widget.geometry()
    # geometry() is relative to the central widget; map to the window
    top_left = widget.mapTo(window, widget.rect().topLeft())
    background = image.pixelColor(top_left.x() + dx, top_left.y() + rect.height() // 2)
    centre = image.pixelColor(top_left.x() + rect.width() // 2, top_left.y() + rect.height() // 2)
    return (
        (background.red(), background.green(), background.blue()),
        (centre.red(), centre.green(), centre.blue()),
    )


def test_an_enabled_button_is_not_black_on_black(window) -> None:
    """The regression: an enabled button must be legible, not black-on-black."""
    background, _ = _sample(window, window.go_button)
    assert background != (0, 0, 0), (
        f"GO renders black ({background}); button text is black, so it would be invisible"
    )
    # the palette the theme declares for an enabled button
    assert background in {(170, 170, 170), (255, 255, 255)}, background


def test_a_disabled_button_is_still_distinguishable(window) -> None:
    """CANCEL starts disabled; it must render as a disabled button, not as nothing."""
    assert window.cancel_button.isEnabled() is False
    background, _ = _sample(window, window.cancel_button)
    assert background != (0, 0, 0), background


def test_the_window_holds_the_build_inputs_and_nothing_from_setup(window) -> None:
    assert window.part_edit.isEnabled()
    assert window.datasheet_edit.isEnabled()
    assert window.out_edit.isEnabled()
    assert window.go_button.text() == "GO"
    assert window.out_edit.text(), "a default save location must be offered"
    for gone in ("key_edit", "team_edit", "credential_label"):
        assert not hasattr(window, gone), f"{gone} belongs to the setup page, not here"
    assert window.menuBar() is None or window.menuBar().actions() == [], (
        "the menu bar was replaced by the SETUP / CHECK ENVIRONMENT buttons"
    )


def test_the_environment_check_button_runs_the_cli_check(qtbot, window, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(window, "_run_cli", lambda argv: calls.append(list(argv)))
    for button in window.findChildren(type(window.go_button)):
        if button.text() == "CHECK ENVIRONMENT":
            button.click()
            break
    else:  # pragma: no cover - the button must exist
        raise AssertionError("no CHECK ENVIRONMENT button")
    assert calls == [["doctor", "--json"]]


def test_setup_is_reachable_from_the_window(qtbot, window, monkeypatch) -> None:
    """The SETUP button must open the setup page (spied on, so nothing blocks)."""
    opened: list[str] = []
    monkeypatch.setattr(
        "boardmodeler.ui.setup_dialog.SetupDialog.exec", lambda self: opened.append("exec") or 0
    )
    for button in window.findChildren(type(window.go_button)):
        if button.text() == "SETUP":
            button.click()
            break
    else:  # pragma: no cover - the button must exist
        raise AssertionError("no SETUP button")
    assert opened == ["exec"]


@pytest.fixture
def setup_page(qtbot, tmp_path, monkeypatch):
    """The setup page, on a throwaway config, for the same rendered-pixel check."""
    target = tmp_path / "config.json"
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.ui.setup_dialog.config_path", lambda: target)
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)
    return page


def test_the_setup_browse_button_renders_legibly(setup_page) -> None:
    """SETUP's LTspice buttons must not repeat the black-on-black regression.

    The window contract above is about the main window; this is the same property on
    the page that grew two new buttons next to the LTspice path, checked on pixels so
    a background rule that repaints a button cannot pass unnoticed.
    """
    for button in (setup_page.browse_ltspice_button,):
        assert button.isEnabled(), f"{button.text()} must be pressable"
        # Sample the button's own padding, never its centre: the centre can be glyph
        # ink (black text), which says nothing about the button's background.
        for dx in (3, button.width() - 3):
            background, _ = _sample(setup_page, button, dx=dx)
            assert background != (0, 0, 0), f"{button.text()} renders black ({background})"
            assert background in {(170, 170, 170), (255, 255, 255)}, background
