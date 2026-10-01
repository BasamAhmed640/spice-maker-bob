"""Classic desktop controls with readable type and restrained modern spacing.

Kept separate from any one window so the model maker and the setup page style themselves
from the same source, and so the stylesheet cannot be accidentally shadowed by a window
that appends its own rules.
"""

from __future__ import annotations

__all__ = ["CGA", "DESKTOP", "RETRO_STYLESHEET"]

CGA: dict[str, str] = {
    "black": "#000000",
    "blue": "#0000aa",
    "green": "#00aa00",
    "cyan": "#00aaaa",
    "red": "#aa0000",
    "magenta": "#aa00aa",
    "brown": "#aa5500",
    "grey": "#aaaaaa",
    "dark_grey": "#555555",
    "bright_blue": "#5555ff",
    "bright_green": "#55ff55",
    "bright_cyan": "#55ffff",
    "bright_red": "#ff5555",
    "bright_magenta": "#ff55ff",
    "yellow": "#ffff55",
    "white": "#ffffff",
}

DESKTOP: dict[str, str] = {
    "face": "#d4d0c8",
    "text": "#202020",
    "navy": "#000080",
    "blue": "#164e9a",
    "white": "#ffffff",
    "shadow": "#808080",
    "dark_shadow": "#404040",
    "muted": "#555555",
    "pass": "#146c38",
    "fail": "#a32121",
    "unknown": "#805b00",
}

#: Scope window backgrounds by object name, so they can never repaint a button.
RETRO_STYLESHEET = f"""
QPushButton {{
    background: {DESKTOP["face"]}; color: {DESKTOP["text"]};
    border-top: 2px solid white; border-left: 2px solid white;
    border-bottom: 2px solid {DESKTOP["dark_shadow"]};
    border-right: 2px solid {DESKTOP["dark_shadow"]};
    padding: 6px 12px; font-family: "Segoe UI"; font-size: 10pt;
}}
QPushButton:hover {{ background: #e6e3dc; }}
QPushButton:default {{ font-weight: 600; }}
QPushButton:pressed {{ border-top-color: {DESKTOP["dark_shadow"]};
    border-left-color: {DESKTOP["dark_shadow"]}; border-bottom-color: white;
    border-right-color: white; padding-top: 7px; padding-bottom: 5px; }}
QPushButton:disabled {{ color: {DESKTOP["shadow"]}; }}
QLineEdit, QComboBox, QPlainTextEdit {{
    background: white; color: {DESKTOP["text"]};
    border-top: 2px solid {DESKTOP["shadow"]};
    border-left: 2px solid {DESKTOP["shadow"]};
    border-bottom: 2px solid white; border-right: 2px solid white;
    padding: 5px; font-family: "Segoe UI"; font-size: 10pt;
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {DESKTOP["blue"]}; }}
QComboBox QAbstractItemView {{ background: white; color: {DESKTOP["text"]};
    selection-background-color: {DESKTOP["navy"]}; selection-color: white;
    border: 1px solid {DESKTOP["dark_shadow"]}; outline: 0;
    font-family: "Segoe UI"; font-size: 10pt; }}
QComboBox QAbstractItemView::item {{ min-height: 24px; color: {DESKTOP["text"]}; }}
QComboBox QAbstractItemView::item:selected {{ background: {DESKTOP["navy"]}; color: white; }}
QCheckBox {{ color: {DESKTOP["text"]}; font-family: "Segoe UI"; font-size: 10pt; spacing: 7px; }}
QCheckBox:disabled {{ color: {DESKTOP["muted"]}; }}
QProgressBar {{ background: white; color: {DESKTOP["text"]};
    border-top: 2px solid {DESKTOP["shadow"]}; border-left: 2px solid {DESKTOP["shadow"]};
    border-bottom: 2px solid white; border-right: 2px solid white;
    min-height: 22px; text-align: center; font-family: "Segoe UI"; }}
QProgressBar::chunk {{ background: {DESKTOP["navy"]}; width: 12px; margin: 1px; }}
QLabel {{ color: {DESKTOP["text"]}; font-family: "Segoe UI"; font-size: 10pt; }}
QLabel#windowBanner {{ background: {DESKTOP["navy"]}; color: white;
    padding: 8px 10px; font-weight: 600; font-size: 12pt; }}
QDialog {{ background: {DESKTOP["face"]}; }}
QToolTip {{ background: #ffffe1; color: {DESKTOP["text"]}; border: 1px solid black; }}
"""
