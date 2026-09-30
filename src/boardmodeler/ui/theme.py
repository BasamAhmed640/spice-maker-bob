"""The shared retro look: the CGA palette and the control stylesheet.

Kept separate from any one window so the model maker and the setup page style themselves
from the same source, and so the stylesheet cannot be accidentally shadowed by a window
that appends its own rules.
"""

from __future__ import annotations

__all__ = ["CGA", "RETRO_STYLESHEET"]

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

#: Controls and dialogs only. A window adding its own rules must scope its background by
#: object name (a bare ``QWidget`` rule ties with ``QPushButton`` on specificity and, being
#: later, repaints every button).
RETRO_STYLESHEET = f"""
QPushButton {{
    background: {CGA["grey"]}; color: {CGA["black"]}; border: 2px solid {CGA["white"]};
    padding: 5px 10px; font-family: Consolas; font-weight: bold;
}}
QPushButton:hover, QPushButton:default {{ background: {CGA["white"]}; color: {CGA["black"]}; }}
QPushButton:pressed {{ background: {CGA["dark_grey"]}; color: {CGA["white"]}; }}
QPushButton:disabled {{ background: {CGA["dark_grey"]}; color: {CGA["grey"]};
                        border-color: {CGA["dark_grey"]}; }}
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {CGA["black"]}; color: {CGA["bright_green"]};
    border: 2px solid {CGA["bright_blue"]}; padding: 3px;
    font-family: Consolas; font-size: 10pt;
}}
QCheckBox {{ color: {CGA["grey"]}; font-family: Consolas; font-size: 10pt; spacing: 6px; }}
QCheckBox::indicator {{ width: 12px; height: 12px; border: 2px solid {CGA["bright_green"]};
                        background: {CGA["black"]}; }}
QCheckBox::indicator:checked {{ background: {CGA["bright_green"]}; }}
QProgressBar {{ background: {CGA["black"]}; color: {CGA["bright_green"]};
                border: 2px solid {CGA["bright_blue"]}; text-align: center; }}
QProgressBar::chunk {{ background: {CGA["blue"]}; }}
QLabel {{ color: {CGA["grey"]}; font-family: Consolas; font-size: 10pt; }}
QDialog {{ background: {CGA["black"]}; }}
"""
