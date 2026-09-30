"""What a chooser must hand Qt: a starting *directory*, never the file being chosen.

``QFileDialog.getOpenFileName``/``getExistingDirectory`` take a starting folder as their
third argument, not the item to select. A path that is not an existing directory leaves
the native dialog to pick a place of its own — in practice the process's current drive
root, ``Look in: C:\\`` — where nothing the user wants is selectable and Open cannot
succeed. The LTspice picker hit exactly that, so both editions share this one rule.

This module lives under ``src/`` and is absent from the exclusion list in
``tools/sync_shared_core.py``, so the sync copies it to the Bob edition verbatim: the two
builds cannot drift apart on it even though their ``ui/setup_dialog.py`` files, which
call it, deliberately do.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["starting_directory"]


def starting_directory(value: str) -> str:
    """The folder a file dialog must open in, given what its field currently holds.

    Qt's third argument is the *starting directory*, never the file to select. Handing it
    a path that is not an existing directory — the executable itself, or an LTspice that
    has since been uninstalled — leaves the native dialog to choose a place of its own:
    in practice the process's current drive root, ``C:\\``, where nothing is selectable and
    Open cannot succeed. That is the reported "Look in: C:\\" bug. So open at the value
    itself when it is a folder, at the folder holding it when it is a file, and at home
    when there is nothing usable to open at.
    """
    text = value.strip()  # a pasted path often carries spaces; the dialog rejects those too
    if not text:
        return str(Path.home())
    candidate = Path(text)
    if candidate.is_dir():
        return str(candidate)
    parent = candidate.parent
    if parent.is_dir():  # the folder of a file, or of a path whose file has gone
        return str(parent)
    return str(Path.home())
