"""Click every button on every surface, offscreen, and write down what happened.

Why this exists. The owner's requirement is that "all buttons work and don't cause
errors" and that the GUI stays light. Before this tool, ``tests/gui/`` asserted pixel
colours and signal wiring and never clicked anything: a button wired to a slot that
raises on the first press would have passed the whole suite. This sweep presses every
``QAbstractButton`` on every surface a user can reach and reports, per button, whether
it was enabled, whether it was connected to anything, what changed, and what raised.

It is safe to run on a developer's machine and in CI because every side effect is
sandboxed *before* the first click and recorded instead of performed:

* ``QFileDialog`` statics return canned paths inside the sandbox, and
  ``boardmodeler.ui.file_dialogs.starting_directory`` never sees a real path;
* ``QMessageBox`` statics are recorded (title, text, method) and return immediately;
* ``WorkerClient.__init__/start/cancel`` and ``MakeModelWorker.start/run`` never spawn
  a child process or a worker thread;
* ``boardmodeler.ui.model_maker``'s one child-process seam (``_run_command``, which
  calls ``security.execution.run``) is replaced by a shim that records the sanctioned
  command and answers with a canned ``doctor --json`` payload, so the real ``_run_cli``
  code path is exercised and nothing is ever launched;
* the LTspice probes (``locate``/``locate_outcome``/``smoke_test``) and the
  API-key connection check are stubs, so nothing is executed and no network is used;
* ``app_root``/``data_dir``/``config_path``/``credential_path``/``model_dir``/
  ``library_dir`` and ``tempfile.tempdir`` all point inside one sandbox directory, and
  the credential environment variables are removed for the duration;
* a modal dialog opened by a click is recorded (class, title, button texts) and
  rejected by a timer, so a blocking ``exec()`` can never hang the sweep.

Honest limitations, also written into the report as ``limitations``:

* Connectivity is read with ``QObject.isSignalConnected``/``QObject.receivers``, which
  PySide6 does expose, so a button wired to *nothing* is detected rather than guessed.
  What is *not* observable is which handler runs; the report names the handler it found
  in the surface's source as supporting evidence only, and says so.
* Qt's own chrome buttons (table corner buttons, tab-bar scroll buttons, the menu/tool
  bar extension buttons) are recorded but never clicked: they are not user actions and
  two of them open blocking menus.
* ``QComboBox``/``QLineEdit``/``QSpinBox``/``QAction`` are not ``QAbstractButton``s and
  are not swept.
* Every dialog interaction is canned, so the sweep proves a button *asks* for a dialog,
  not that the dialog itself behaves — that is what the rest of ``tests/gui`` is for.

Run it directly::

    uv run python tools/gui_sweep.py --json build/gui-sweep.json

or import :func:`run_sweep` (the test wrapper in ``tests/gui/test_button_sweep.py``
does that). The exit code is non-zero when a click raised, a traceback was captured, a
button was wired to nothing, or a checkbox did not return to where it started.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # shipped with PySide6; the fallback is a guarded try/except on the wrapper
    import shiboken6
except ImportError:  # pragma: no cover - only when PySide6 is absent entirely
    shiboken6 = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # a plain ``python tools/gui_sweep.py`` still works
    sys.path.insert(0, str(SRC))

#: Offscreen before the first Qt import: the sweep must never need a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

__all__ = ["SURFACE_NAMES", "SweepReport", "ensure_application", "main", "run_sweep"]

#: Qt's own internal buttons. They are ``QAbstractButton``s but no user ever presses
#: them, and the menu/tool bar extension buttons open a blocking popup menu.
INTERNAL_OBJECT_NAMES = frozenset(
    {
        "qt_tableview_cornerbutton",
        "qt_menubar_ext_button",
        "qt_toolbar_ext_button",
        "ScrollLeftButton",
        "ScrollRightButton",
    }
)

#: The signals a button can carry a click through. ``receivers()`` needs Qt's old
#: ``SIGNAL()`` spelling, hence the ``2`` code prefix in :func:`_connections`.
_BUTTON_SIGNALS = ("clicked(bool)", "toggled(bool)", "pressed()", "released()")

#: Qt's message levels by value, for a handler that is given the enum as an integer.
_QT_LEVELS = {
    0: "QtDebugMsg",
    1: "QtWarningMsg",
    2: "QtCriticalMsg",
    3: "QtFatalMsg",
    4: "QtInfoMsg",
}

#: How many changed state entries one click reports before it summarises.
_MAX_EFFECTS = 6

LIMITATIONS = (
    "connectivity is read from QObject.isSignalConnected/receivers (PySide6 exposes "
    "both), so a button wired to nothing is detected; *which* handler runs is not "
    "observable, so the handler named here was found in the surface's source and is "
    "supporting evidence only, never proof that it ran",
    "Qt's own chrome buttons (qt_tableview_cornerbutton, ScrollLeft/RightButton, "
    "qt_menubar_ext_button, qt_toolbar_ext_button) are recorded but never clicked: they "
    "are not user actions and the extension buttons open a blocking menu",
    "QComboBox, QLineEdit, QSpinBox and menu/toolbar QActions are not QAbstractButtons "
    "and are not swept; their own tests live elsewhere",
    "file dialogs return canned paths inside the sandbox and message boxes are recorded "
    "instead of shown, so the sweep proves a button *asks* for a dialog, not that the "
    "dialog's own behaviour is right",
    "a QCheckBox is pressed on its indicator (Qt's own SE_CheckBoxClickRect), because Qt "
    "ignores a press in the empty area to the right of a stretched checkbox's label; the "
    "click position is recorded per control, and the round trip is what is asserted",
    "each surface is built without a project or a finished run, so controls that only "
    "become enabled after a successful build (Open model folder, Install into LTspice, "
    "Run tests again) are reported as disabled rather than clicked",
    "the sweep always runs in the installed, non-portable layout: SPICE_MAKER_ROOT is "
    "removed for the duration, so a portable copy's layout is not what is swept",
    "Qt messages are recorded rather than forwarded to the previously installed handler "
    "(the handler is restored afterwards), and every click's exceptions are read from "
    "sys.excepthook/threading.excepthook, which is where PySide6 reports a slot error",
)


def _size(size: QtCore.QSize) -> list[int]:
    return [size.width(), size.height()]


def _alive(widget: Any) -> bool:
    """Whether the C++ object behind a Python wrapper still exists.

    A slot is free to ``deleteLater()`` the widget that was clicked, and touching a
    deleted wrapper afterwards raises ``RuntimeError`` — so every post-click check goes
    through this, with shiboken when it is importable and a guarded call otherwise.
    """
    shiboken = getattr(shiboken6, "Shiboken", None) if shiboken6 is not None else None
    if shiboken is not None:
        try:
            return bool(shiboken.isValid(widget))
        except Exception:  # pragma: no cover - only for exotic wrappers
            pass
    try:
        widget.objectName()
    except RuntimeError:
        return False
    return True


def _connections(button: QtWidgets.QAbstractButton) -> dict[str, dict[str, Any]]:
    """Per-signal proof of wiring: ``isSignalConnected`` plus Qt's own receiver count."""
    counts: dict[str, dict[str, Any]] = {}
    meta = button.metaObject()
    for signature in _BUTTON_SIGNALS:
        index = meta.indexOfSignal(signature)
        if index < 0:
            continue
        try:
            connected = bool(button.isSignalConnected(meta.method(index)))
        except Exception:  # pragma: no cover - defensive: the API is stable
            connected = False
        try:
            receivers = int(button.receivers(f"2{signature}"))
        except Exception:  # pragma: no cover - defensive
            receivers = 0
        counts[signature] = {"connected": connected, "receivers": receivers}
    return counts


def _is_wired(counts: dict[str, dict[str, Any]]) -> bool:
    return any(item["connected"] or item["receivers"] for item in counts.values())


def _pump(iterations: int = 3, wait_ms: int = 5) -> None:
    """Deliver pending events for a bounded number of turns — never an unbounded wait.

    A click can queue a signal, start a timer or post a deferred delete; a fixed, small
    budget is what keeps the sweep deterministic and fast instead of hoping a single
    ``processEvents()`` was enough.
    """
    app = QtWidgets.QApplication.instance()
    for _ in range(max(1, iterations)):
        if app is not None:
            app.processEvents()
        QtTest.QTest.qWait(max(1, wait_ms))


def _flush_deletes() -> None:
    """Run the deferred deletes a closed surface posted, so windows do not pile up."""
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def _close(widget: Any) -> None:
    """Hide and schedule a widget for deletion; a failure here is never the finding."""
    for method in ("close", "deleteLater"):
        try:
            getattr(widget, method)()
        except Exception:
            continue


def _fingerprint(surface: QtWidgets.QWidget) -> dict[str, str]:
    """Everything about a surface a click could visibly change, as flat text.

    Compared before and after each click, this is what turns "the click returned" into
    "the click did something": a label, a field, a checkbox, a table's row count, the
    window title, or the clipboard a COPY REPORT button writes to.
    """
    data: dict[str, str] = {
        "window.title": surface.windowTitle(),
        "window.visible": str(surface.isVisible()),
    }
    clipboard = QtWidgets.QApplication.clipboard()
    if clipboard is not None:
        data["clipboard.characters"] = str(len(clipboard.text()))
    for index, widget in enumerate(surface.findChildren(QtWidgets.QWidget)):
        name = widget.objectName() or f"{type(widget).__name__}#{index}"
        try:
            if isinstance(widget, QtWidgets.QAbstractButton):
                data[f"{name}.text"] = widget.text()[:60]
                data[f"{name}.checked"] = str(widget.isChecked())
            elif isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QLabel)):
                data[f"{name}.text"] = widget.text()[:80]
            elif isinstance(widget, QtWidgets.QComboBox):
                data[f"{name}.current"] = widget.currentText()[:60]
            elif isinstance(widget, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
                data[f"{name}.characters"] = str(len(widget.toPlainText()))
            elif isinstance(widget, QtWidgets.QTableWidget):
                data[f"{name}.rows"] = str(widget.rowCount())
            elif isinstance(widget, QtWidgets.QListWidget):
                data[f"{name}.items"] = str(widget.count())
            data[f"{name}.enabled"] = str(widget.isEnabled())
        except RuntimeError:  # a widget deleted while the snapshot was being taken
            continue
    return data


def _changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The human-readable diff of two fingerprints, capped so the report stays small."""
    changed = [
        f"{key}: {before.get(key)!r} -> {after.get(key)!r}"
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]
    if len(changed) > _MAX_EFFECTS:
        extra = len(changed) - _MAX_EFFECTS
        return [*changed[:_MAX_EFFECTS], f"... and {extra} more change(s)"]
    return changed


def _click_position(button: QtWidgets.QAbstractButton) -> QtCore.QPoint:
    """Where a user's press actually lands, not the geometric centre of the widget.

    ``QCheckBox``/``QRadioButton`` accept a press only inside the style's click rect —
    the indicator plus the label — so a checkbox that a form layout stretched across the
    row has a large dead area to the right of its text where Qt itself ignores the press.
    Clicking the geometric centre there would report a working checkbox as broken, which
    is the kind of false finding this sweep must not produce; the indicator is pressed
    instead and the position is recorded in the report.
    """
    if isinstance(button, (QtWidgets.QCheckBox, QtWidgets.QRadioButton)):
        option = QtWidgets.QStyleOptionButton()
        button.initStyleOption(option)
        sub = (
            QtWidgets.QStyle.SubElement.SE_CheckBoxClickRect
            if isinstance(button, QtWidgets.QCheckBox)
            else QtWidgets.QStyle.SubElement.SE_RadioButtonClickRect
        )
        rect = button.style().subElementRect(sub, option, button)
        if rect.isValid() and not rect.isEmpty():
            return rect.center()
    return button.rect().center()


def _click(button: QtWidgets.QAbstractButton) -> tuple[str, QtCore.QPoint]:
    """Click the way a user would when the button is visible, directly when it is not.

    ``QTest.mouseClick`` is the honest path for a visible button (it goes through the
    widget's own hit-testing and mouse handling); a button on an inactive tab or behind a
    toolbar overflow is not reachable by a pointer, but its slot still has to work, so it
    gets ``click()`` instead and the report says which was used.
    """
    position = _click_position(button)
    if button.isVisible():
        QtTest.QTest.mouseClick(
            button,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier,
            position,
        )
        return "QTest.mouseClick", position
    button.click()
    return "click()", position


@dataclass(frozen=True)
class _Connect:
    """One ``X.signal.connect(handler)`` call found in a surface's source."""

    receiver: str
    signal: str
    handler: str
    line: int


_SOURCE_CACHE: dict[str, list[_Connect]] = {}


def _module_connects(module_name: str) -> list[_Connect]:
    """Every ``connect`` call in a module, parsed once and cached.

    Only the *first* argument's text is kept: the sweep needs to name a handler it can
    point at, not to understand it. Lambdas are named as ``lambda: ...`` on purpose —
    that is exactly what the source says.
    """
    if module_name in _SOURCE_CACHE:
        return _SOURCE_CACHE[module_name]
    connects: list[_Connect] = []
    module = sys.modules.get(module_name)
    path = Path(str(getattr(module, "__file__", "") or ""))
    if path.is_file():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError:  # pragma: no cover - a source file we cannot read
            tree = None
        for node in ast.walk(tree) if tree is not None else ():
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "connect":
                continue
            if not isinstance(func.value, ast.Attribute) or not node.args:
                continue
            connects.append(
                _Connect(
                    receiver=ast.unparse(func.value.value),
                    signal=func.value.attr,
                    handler=ast.unparse(node.args[0]),
                    line=node.lineno,
                )
            )
    _SOURCE_CACHE[module_name] = connects
    return connects


def _attribute_names(root: QtWidgets.QWidget) -> dict[int, str]:
    """Map ``id(widget)`` to the attribute the surface holds it in, when it holds one.

    ``self.go_button`` is attributable; a local ``browse = QPushButton(...)`` is not, and
    saying so is more honest than inventing an attribution from widget order.
    """
    names: dict[int, str] = {}
    holders: list[Any] = [root, *root.findChildren(QtCore.QObject)]
    for holder in holders:
        try:
            members = vars(holder)
        except TypeError:  # pragma: no cover - every PySide6 object has __dict__
            continue
        for name, value in members.items():
            if isinstance(value, QtCore.QObject):
                names.setdefault(id(value), name)
    return names


def _source_wiring(
    surface: QtWidgets.QWidget, button: QtWidgets.QAbstractButton, attributes: dict[int, str]
) -> tuple[str, str]:
    """``(attribute, handler)`` the surface's source shows for this button, best effort."""
    attribute = attributes.get(id(button), "")
    if not attribute:
        return "", ""
    matches = [
        connect
        for connect in _module_connects(type(surface).__module__)
        if connect.receiver.split(".")[-1] == attribute and connect.signal in {"clicked", "toggled"}
    ]
    if not matches:
        return attribute, ""
    return attribute, "; ".join(sorted({match.handler for match in matches}))


@dataclass
class _Watch:
    """Exceptions and Qt messages, captured instead of swallowed by Qt.

    PySide6 reports an exception raised inside a slot by calling ``sys.excepthook`` and
    then carrying on: the click returns normally and nothing else would notice. Marking
    before a click and reading what arrived after it is what turns that silence into a
    finding.
    """

    tracebacks: list[dict[str, str]]
    qt_messages: list[dict[str, str]]
    _previous_sys: Callable[..., Any] | None = None
    _previous_threading: Callable[..., Any] | None = None
    _previous_handler: Any = None

    @classmethod
    def start(cls) -> _Watch:
        watch = cls(tracebacks=[], qt_messages=[])
        watch._previous_sys = sys.excepthook
        watch._previous_threading = threading.excepthook
        sys.excepthook = watch._on_sys
        threading.excepthook = watch._on_thread
        watch._previous_handler = QtCore.qInstallMessageHandler(watch._on_qt)
        return watch

    def stop(self) -> None:
        if self._previous_sys is not None:
            sys.excepthook = self._previous_sys
        if self._previous_threading is not None:
            threading.excepthook = self._previous_threading
        QtCore.qInstallMessageHandler(self._previous_handler)

    def mark(self) -> tuple[int, int]:
        return len(self.tracebacks), len(self.qt_messages)

    def since(self, mark: tuple[int, int]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return self.tracebacks[mark[0] :], self.qt_messages[mark[1] :]

    def _record(self, source: str, kind: str, value: BaseException) -> None:
        self.tracebacks.append(
            {
                "source": source,
                "type": type(value).__name__,
                "message": str(value),
                "traceback": "".join(
                    traceback.format_exception(type(value), value, value.__traceback__)
                ),
            }
        )

    def _on_sys(self, kind: type[BaseException], value: BaseException, tb: Any) -> None:
        self._record("sys.excepthook", kind.__name__, value)
        if self._previous_sys is not None:  # nothing is hidden from the console either
            self._previous_sys(kind, value, tb)

    def _on_thread(self, args: Any) -> None:
        self._record("threading.excepthook", "ThreadError", args.exc_value or RuntimeError("?"))
        if self._previous_threading is not None:
            self._previous_threading(args)

    def _on_qt(self, mode: Any, context: Any, message: str) -> None:
        # PySide6 hands the level over as an enum whose ``str()`` is its number, so the
        # name is preferred and the number is mapped, never printed raw.
        level = str(getattr(mode, "name", "") or _QT_LEVELS.get(getattr(mode, "value", mode), mode))
        self.qt_messages.append({"level": level, "message": str(message)[:400]})
        if level in {"QtCriticalMsg", "QtFatalMsg"}:  # pragma: no cover - environment noise
            print(f"qt {level}: {message}", file=sys.stderr)


@dataclass
class _Patch:
    target: Any
    name: str
    original: Any
    existed: bool


class _CliShim:
    """``model_maker._run_command``: records the sanctioned command, launches nothing.

    ``model_maker._run_cli`` runs the CLI through ``security.execution``; the sweep wants
    the real ``_run_cli`` code path (it feeds the doctor page) without ever starting a
    process, so this answers with a canned payload and records what would have run —
    including the spec name, so the record says which template was used.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def __call__(
        self,
        spec: Any,
        argv: Sequence[Any] = (),
        *,
        cwd: Any = None,
        root: Any = None,
    ) -> Any:
        from boardmodeler.security.execution import CompletedCommand

        command = [str(spec.name), *(str(item) for item in argv)]
        self._sandbox.record(
            "cli", command=command, spec=str(spec.name), cwd=str(cwd), root=str(root)
        )
        return CompletedCommand(
            returncode=0,
            stdout=json.dumps(_doctor_payload()),
            stderr="",
            duration_s=0.0,
            timed_out=False,
            truncated=False,
        )


class Sandbox:
    """Every side effect a click can have, recorded instead of performed.

    ``install`` patches the classes and module attributes that the surfaces reach for;
    ``restore`` puts them back. The records are what the report shows as a click's
    "effect", and they are also the proof that nothing real happened.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.events: list[dict[str, Any]] = []
        self.removed_environment: list[str] = []
        self._patches: list[_Patch] = []
        self._env_removed: dict[str, str] = {}
        self._mark = 0
        self._modal_ticks: dict[int, int] = {}

    @property
    def env_removed(self) -> dict[str, str]:
        """The environment variables the sandbox took away for the sweep's duration."""
        return dict(self._env_removed)

    # ------------------------------------------------------------------ records
    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        event = {"kind": kind, **fields}
        self.events.append(event)
        return event

    def mark(self) -> None:
        self._mark = len(self.events)

    def effects(self) -> list[str]:
        """The recorded events since :meth:`mark`, as the lines a report can show."""
        lines: list[str] = []
        for event in self.events[self._mark :]:
            kind = event["kind"]
            if kind == "messagebox":
                lines.append(f"{event['method']} box: {event['title']!r} — {event['text'][:80]!r}")
            elif kind == "file_dialog":
                lines.append(f"{event['method']}: {event['caption']!r} -> {event['returned']!r}")
            elif kind == "starting_directory":
                lines.append(f"starting_directory({event['value']!r}) -> {event['returned']!r}")
            elif kind == "modal":
                buttons = ", ".join(event["buttons"][:4]) or "no buttons"
                lines.append(f"modal {event['class']} {event['title']!r} [{buttons}] rejected")
            elif kind == "worker_start":
                lines.append(f"worker start requested (sandboxed): {event['project']}")
            elif kind == "worker_cancel":
                lines.append("worker cancel requested (sandboxed)")
            elif kind == "worker_construct":
                lines.append("WorkerClient constructed (sandboxed: no child process)")
            elif kind == "thread_start":
                lines.append(f"{event['worker']}.start requested (sandboxed: no thread runs)")
            elif kind == "launch":
                lines.append(f"{event['how']}({event['target']}) recorded, not launched")
            elif kind == "cli":
                lines.append(f"cli {event['command']!r} recorded, not run")
            elif kind == "credential_write":
                lines.append(f"credential write for {event['name']!r} recorded, not written")
            elif kind == "launch_refused":
                lines.append(event["detail"])
        return lines

    # ------------------------------------------------------------------ patching
    def _patch(self, target: Any, name: str, value: Any) -> None:
        existed = hasattr(target, name)
        original = getattr(target, name, None)
        self._patches.append(_Patch(target, name, original, existed))
        setattr(target, name, value)

    def _patch_everywhere(self, module: Any, name: str, value: Any) -> None:
        """Patch ``module.name`` and every already-imported copy of that name.

        ``from x import y`` copies the reference, so patching only the defining module
        would leave e.g. ``pipeline.make_model.locate`` calling the real function — and
        worse, a module imported *while the sweep runs* (the window imports the engine
        lazily, so a click on GO does exactly that) would bind the stub and keep it for
        the rest of the process. Both directions are handled here: the copies that exist
        already are patched too, and :meth:`restore` repairs any binding of a stub that
        was made during the sweep.
        """
        original = getattr(module, name, None)
        self._patch(module, name, value)
        for other in list(sys.modules.values()):
            if other is module:
                continue
            try:
                namespace = vars(other)
            except TypeError:  # pragma: no cover - a module without __dict__
                continue
            if not str(namespace.get("__name__", "")).startswith("boardmodeler"):
                continue
            if original is not None and namespace.get(name) is original:
                self._patch(other, name, value)

    def restore(self) -> None:
        # Every stub currently installed, by identity: a *copy* of it bound elsewhere has
        # to be repaired too, and a module imported during the sweep would otherwise keep
        # the sandbox's function as its own for the rest of the process.
        stubs: dict[int, tuple[Any, Any]] = {}
        for patch in self._patches:
            try:
                current = getattr(patch.target, patch.name)
            except AttributeError:  # pragma: no cover - the attribute is already gone
                continue
            if current is not patch.original:
                stubs.setdefault(id(current), (current, patch.original))
        for patch in reversed(self._patches):
            try:
                if patch.existed:
                    setattr(patch.target, patch.name, patch.original)
                else:
                    delattr(patch.target, patch.name)
            except Exception:  # pragma: no cover - restoring must never mask a finding
                continue
        self._patches.clear()
        for module in list(sys.modules.values()):
            try:
                namespace = vars(module)
            except TypeError:  # pragma: no cover - a module without __dict__
                continue
            if not str(namespace.get("__name__", "")).startswith("boardmodeler"):
                continue
            for name, value in list(namespace.items()):
                found = stubs.get(id(value))
                if found is not None and found[0] is value:
                    namespace[name] = found[1]
        for name, value in self._env_removed.items():
            os.environ[name] = value
        self._env_removed.clear()

    def install(self) -> None:
        """Point every path at the sandbox and stub every outward call."""
        from boardmodeler import agent_providers, storage
        from boardmodeler import config as config_module
        from boardmodeler.authoring.api_backend import env_sources
        from boardmodeler.security import credentials, key_verification
        from boardmodeler.simulation import ltspice
        from boardmodeler.ui import file_dialogs, model_maker, worker_client

        root = self.root
        for folder in ("data", "models", "library", "temp"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        # The window checks that the chosen datasheet is a file before it does anything
        # else, so the canned dialog answer has to be a real (if empty) file for a click
        # on GO to reach the next check instead of stopping at "Datasheet needed".
        (root / "datasheet.pdf").write_bytes(b"%PDF-1.4\ngui sweep placeholder\n")
        (root / "notes.txt").write_text("gui sweep placeholder\n", encoding="utf-8")

        # --- environment --------------------------------------------------
        # Every variable the credential walk can read is taken away, so no click can reach
        # a provider even if the developer's shell has a real key in it. The names are kept
        # separately: ``restore`` puts the values back and empties ``_env_removed``.
        for provider in agent_providers.CATALOG:
            for name in env_sources(provider):
                if name in os.environ:
                    self._env_removed[name] = os.environ.pop(name)
                    self.removed_environment.append(name)
        for name in ("BOARDMODELER_CONFIG", "LTSPICE_EXE", "SPICE_MAKER_ROOT"):
            if name in os.environ:
                self._env_removed[name] = os.environ.pop(name)
                self.removed_environment.append(name)

        # --- where state lives --------------------------------------------
        # ``_patch_everywhere`` because these names are imported by value all over the UI:
        # a patch on the defining module alone would not reach ``setup_dialog.config_path``
        # (which the house style patches separately) nor a module imported mid-sweep.
        self._patch(tempfile, "tempdir", str(root / "temp"))
        for module, name, value in (
            (storage, "app_root", lambda: root),
            (storage, "data_dir", lambda: root / "data"),
            (storage, "state_file", lambda name: root / "data" / Path(name).name),
            (storage, "model_dir", lambda configured=None: root / "models"),
            (storage, "library_dir", lambda: root / "library"),
            (storage, "portable", lambda: False),
            (config_module, "config_dir", lambda: root / "data"),
            (config_module, "config_path", lambda: root / "data" / "config.json"),
            (credentials, "credential_path", lambda: root / "data" / "credentials.json"),
            (credentials, "get_credential", self._sandbox_credential),
            (credentials, "set_credential", self._set_credential),
            (key_verification, "verify_key", self._verify_key),
            (file_dialogs, "starting_directory", self._starting_directory),
            (ltspice, "locate", self._locate),
            (ltspice, "locate_outcome", self._locate_outcome),
            (ltspice, "smoke_test", self._smoke_test),
            (ltspice, "version", lambda *args, **kwargs: "0.0.0-sweep"),
        ):
            self._patch_everywhere(module, name, value)

        # --- dialogs, launches, processes ----------------------------------
        dialogs = QtWidgets.QFileDialog
        self._patch(dialogs, "getOpenFileName", staticmethod(self._get_open_file_name))
        self._patch(dialogs, "getOpenFileNames", staticmethod(self._get_open_file_names))
        self._patch(dialogs, "getExistingDirectory", staticmethod(self._get_existing_directory))
        self._patch(dialogs, "getSaveFileName", staticmethod(self._get_save_file_name))
        boxes = QtWidgets.QMessageBox
        for method, answer in (
            ("information", QtWidgets.QMessageBox.StandardButton.Ok),
            ("warning", QtWidgets.QMessageBox.StandardButton.Ok),
            ("critical", QtWidgets.QMessageBox.StandardButton.Ok),
            ("about", None),
        ):
            self._patch(boxes, method, staticmethod(self._message_box(method, answer)))
        self._patch(boxes, "question", staticmethod(self._question_box))
        self._patch(boxes, "exec", self._message_box_exec)
        self._patch(QtGui.QDesktopServices, "openUrl", staticmethod(self._open_url))
        self._patch(QtCore.QProcess, "startDetached", staticmethod(self._start_detached))
        self._patch(os, "startfile", self._startfile)

        # --- the worker child process and the authoring thread --------------
        original_init = worker_client.WorkerClient.__init__

        def _worker_init(instance: Any, *args: Any, **kwargs: Any) -> None:
            self.record("worker_construct")
            original_init(instance, *args, **kwargs)
            proc = getattr(instance, "_proc", None)
            if proc is not None:  # a future __init__ that spawns must not be trusted
                self.record("launch_refused", detail="WorkerClient.__init__ spawned a process")
                with suppress(Exception):  # pragma: no cover - already gone
                    proc.kill()

        self._patch(worker_client.WorkerClient, "__init__", _worker_init)
        self._patch(worker_client.WorkerClient, "start", self._worker_start)
        self._patch(worker_client.WorkerClient, "cancel", self._worker_cancel)
        # Install functions so Python binds the worker instance before forwarding to
        # the sandbox's bound method; assigning the bound method loses that argument.
        self._patch(
            model_maker.MakeModelWorker, "start", lambda instance: self._thread_start(instance)
        )
        self._patch(model_maker.MakeModelWorker, "run", lambda instance: self._thread_run(instance))

        # --- the CLI runner the window shells out to ------------------------
        self._patch(model_maker, "_run_command", _CliShim(self))
        self.record("sandbox_ready", root=str(root))

    # ------------------------------------------------------------------ stubs
    def _sandbox_credential(self, name: str) -> Any:
        from boardmodeler.security.credentials import Credential, SecretSource

        return Credential(
            name=name,
            value=None,
            source=SecretSource.MISSING,
            detail="gui sweep sandbox: no credential is read",
        )

    def _set_credential(self, name: str, value: str) -> None:
        self.record("credential_write", name=name, characters=len(value))

    def _starting_directory(self, value: str, *, fallback=None) -> str:
        returned = str(self.root)
        self.record("starting_directory", value=str(value), returned=returned)
        return returned

    def _get_open_file_name(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
        target = self.root / "datasheet.pdf"
        self.record(
            "file_dialog",
            method="getOpenFileName",
            caption=str(args[1] if len(args) > 1 else kwargs.get("caption", "")),
            started_at=str(args[2] if len(args) > 2 else kwargs.get("dir", "")),
            returned=str(target),
        )
        return str(target), "PDF (*.pdf)"

    def _get_open_file_names(self, *args: Any, **kwargs: Any) -> tuple[list[str], str]:
        targets = [self.root / "datasheet.pdf", self.root / "notes.txt"]
        self.record(
            "file_dialog",
            method="getOpenFileNames",
            caption=str(args[1] if len(args) > 1 else kwargs.get("caption", "")),
            started_at=str(args[2] if len(args) > 2 else kwargs.get("dir", "")),
            returned=", ".join(str(item) for item in targets),
        )
        return [str(item) for item in targets], "Documents (*.pdf *.txt *.md)"

    def _get_existing_directory(self, *args: Any, **kwargs: Any) -> str:
        target = self.root / "models"
        self.record(
            "file_dialog",
            method="getExistingDirectory",
            caption=str(args[1] if len(args) > 1 else kwargs.get("caption", "")),
            started_at=str(args[2] if len(args) > 2 else kwargs.get("dir", "")),
            returned=str(target),
        )
        return str(target)

    def _get_save_file_name(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
        target = self.root / "models" / "saved.lib"
        self.record(
            "file_dialog",
            method="getSaveFileName",
            caption=str(args[1] if len(args) > 1 else kwargs.get("caption", "")),
            started_at=str(args[2] if len(args) > 2 else kwargs.get("dir", "")),
            returned=str(target),
        )
        return str(target), "All files (*)"

    def _message_box(self, method: str, answer: Any) -> Callable[..., Any]:
        def _box(*args: Any, **kwargs: Any) -> Any:
            self.record(
                "messagebox",
                method=method,
                title=str(args[1] if len(args) > 1 else kwargs.get("title", "")),
                text=str(args[2] if len(args) > 2 else kwargs.get("text", "")),
            )
            return answer

        return _box

    def _question_box(self, *args: Any, **kwargs: Any) -> Any:
        self.record(
            "messagebox",
            method="question",
            title=str(args[1] if len(args) > 1 else kwargs.get("title", "")),
            text=str(args[2] if len(args) > 2 else kwargs.get("text", "")),
        )
        return QtWidgets.QMessageBox.StandardButton.No

    def _message_box_exec(self, instance: Any) -> int:
        self.record("messagebox", method="exec", title=instance.windowTitle(), text=instance.text())
        return 0

    def _open_url(self, url: Any) -> bool:
        self.record("launch", how="QDesktopServices.openUrl", target=str(url.toString()))
        return True

    def _start_detached(self, *args: Any, **kwargs: Any) -> bool:
        self.record("launch", how="QProcess.startDetached", target=str(args[:2]))
        return True

    def _startfile(self, path: Any, *args: Any, **kwargs: Any) -> None:
        self.record("launch", how="os.startfile", target=str(path))

    def _worker_start(self, instance: Any, request: Any, **kwargs: Any) -> Path:
        self.record("worker_start", project=str(kwargs.get("project_dir", "")))
        return self.root / "request-sandbox.json"

    def _worker_cancel(self, instance: Any) -> bool:
        self.record("worker_cancel")
        return False

    def _thread_start(self, instance: Any) -> None:
        self.record("thread_start", worker=type(instance).__name__)

    def _thread_run(self, instance: Any) -> None:
        self.record("thread_start", worker=f"{type(instance).__name__}.run")

    def _fake_install(self) -> Any:
        from boardmodeler.simulation.ltspice import LtspiceInstall

        return LtspiceInstall(path=self.root / "LTspice.exe", source="sweep")

    def _discover(self, explicit: Any = None) -> Any:
        from boardmodeler.simulation.ltspice import LocateOutcome

        return LocateOutcome(
            install=None,
            probed=[(self.root / "LTspice.exe", "sweep")],
            reason="not_installed",
        )

    def _locate(self, explicit: Any = None) -> Any:
        return self._fake_install()

    def _locate_outcome(self, explicit: Any = None) -> Any:
        from boardmodeler.simulation.ltspice import LocateOutcome

        return LocateOutcome(
            install=self._fake_install(),
            probed=[(self.root / "LTspice.exe", "sweep")],
            reason="env",
        )

    def _smoke_test(self, exe: Any, workdir: Any, *, timeout_s: float = 60.0) -> Any:
        from boardmodeler.simulation.ltspice import SmokeResult

        return SmokeResult(
            status="pass",
            detail="gui sweep: no simulator was run",
            measured_v=0.6321,
            expected_v=0.6321,
            tolerance_pct=5.0,
        )

    def _verify_key(self, provider: Any, key: str, **kwargs: Any) -> Any:
        from boardmodeler.security.key_verification import KeyVerification

        return KeyVerification("unverified", "gui sweep: the connection check is sandboxed")

    # ------------------------------------------------------------------ modals
    def tick_modals(self) -> None:
        """Record and reject whatever modal a click opened, so nothing can block."""
        app = QtWidgets.QApplication.instance()
        if not isinstance(app, QtWidgets.QApplication):
            return
        modal = app.activeModalWidget()
        if modal is None or not _alive(modal):
            return
        ticks = self._modal_ticks.get(id(modal), 0) + 1
        self._modal_ticks[id(modal)] = ticks
        if ticks == 1:
            buttons = [
                button.text() or button.objectName()
                for button in modal.findChildren(QtWidgets.QAbstractButton)
            ]
            self.record(
                "modal",
                **{
                    "class": type(modal).__name__,
                    "title": modal.windowTitle(),
                    "buttons": buttons,
                },
            )
        for method in ("reject", "close", "hide"):
            try:
                getattr(modal, method)()
                break
            except Exception:  # pragma: no cover - a modal without reject/close
                continue
        if ticks > 10:  # a modal that ignores all three is deleted rather than waited on
            _close(modal)


class _ModalDismisser(QtCore.QObject):
    """A timer that keeps the sandbox's modal handling running inside nested loops."""

    def __init__(self, sandbox: Sandbox, interval_ms: int = 15) -> None:
        super().__init__()
        self._sandbox = sandbox
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._sandbox.tick_modals)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()


def _doctor_payload() -> dict[str, Any]:
    """A ``doctor --json`` payload shaped like the real one, for the sandboxed CLI."""
    return {
        "tool": "boardmodeler",
        "version": "0.0.0-sweep",
        "python": "3.14",
        "platform": "offscreen",
        "executable": "python",
        "config": {"path": "sandbox/config.json", "exists": False},
        "ltspice": {
            "found": True,
            "path": "sandbox/LTspice.exe",
            "version": "0.0.0-sweep",
            "source": "sweep",
            "lib_dir": "sandbox/library",
            "smoke_test": "pass",
            "smoke_detail": "gui sweep: no simulator was run",
            "exit_code": 0,
        },
        "reader_backend": {
            "reader_backend": "native",
            "spicelib_version": None,
            "max_deviation": 0.0,
            "agreement_tolerance": 1e-9,
            "detail": "native reader",
        },
        "ocr": {"available": False, "reason": "sandbox", "detail": "not probed"},
        "credentials": {},
        "telemetry": "none",
        "ok": True,
    }


@dataclass(frozen=True)
class SurfaceSpec:
    """One top-level surface the sweep opens, and how a user reaches it."""

    name: str
    factory: Callable[[], QtWidgets.QWidget]
    reachable: str
    prepare: Callable[[QtWidgets.QWidget], None] | None = None


def _fill_doctor(view: QtWidgets.QWidget) -> None:
    """DoctorView needs a report before its buttons mean anything; this is the input."""
    view.set_report(json.dumps(_doctor_payload(), indent=2), exit_code=0)  # type: ignore[attr-defined]


def surface_specs() -> list[SurfaceSpec]:
    """The surfaces, imported lazily so the module can be imported without Qt widgets."""
    from boardmodeler.ui.main_window import MainWindow
    from boardmodeler.ui.model_maker import DoctorView, ModelMakerWindow
    from boardmodeler.ui.settings import SettingsDialog
    from boardmodeler.ui.setup_dialog import SetupDialog

    def prepare_model_maker(surface) -> None:
        # The normal picker deliberately does not infer an ordering code from a title.
        surface.part_edit.setText("SWEEP_TEST")

    return [
        SurfaceSpec(
            name="ModelMakerWindow",
            factory=ModelMakerWindow,
            reachable="the product: `boardmodeler ui`",
            prepare=prepare_model_maker,
        ),
        SurfaceSpec(
            name="SetupDialog",
            factory=SetupDialog,
            reachable="SETUP in the model window; `boardmodeler ui --installer`",
        ),
        SurfaceSpec(
            name="DoctorView",
            factory=DoctorView,
            reachable="CHECK ENVIRONMENT in the model window",
            prepare=_fill_doctor,
        ),
        SurfaceSpec(
            name="SettingsDialog",
            factory=SettingsDialog,
            reachable="dormant: Tools > Settings in `boardmodeler ui --board-ui`",
        ),
        SurfaceSpec(
            name="MainWindow",
            factory=MainWindow,
            reachable="dormant: `boardmodeler ui --board-ui`, no project loaded",
        ),
    ]


SURFACE_NAMES = (
    "ModelMakerWindow",
    "SetupDialog",
    "DoctorView",
    "SettingsDialog",
    "MainWindow",
)
"""The surfaces the sweep opens, in order; the report is required to name all of them."""


@dataclass
class SweepReport:
    """The machine-readable result of one sweep."""

    invocation: str
    cwd: str
    qt_platform: str
    started_at: str
    seconds: float
    sandbox: dict[str, Any]
    surfaces: list[dict[str, Any]]
    totals: dict[str, int]
    failures: list[dict[str, str]]
    captured_tracebacks: list[dict[str, str]]
    qt_messages: list[dict[str, str]]
    limitations: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures

    def surface(self, name: str) -> dict[str, Any] | None:
        return next((item for item in self.surfaces if item["name"] == name), None)

    def to_json(self) -> dict[str, Any]:
        """The report as the exact payload written to ``--json``."""
        return {
            "invocation": self.invocation,
            "cwd": self.cwd,
            "qt_platform": self.qt_platform,
            "started_at": self.started_at,
            "seconds": round(self.seconds, 3),
            "sandbox": self.sandbox,
            "surfaces": self.surfaces,
            "totals": self.totals,
            "failures": self.failures,
            "captured_tracebacks": self.captured_tracebacks,
            "qt_messages": self.qt_messages,
            "limitations": self.limitations,
        }

    def summary(self) -> str:
        """A short human summary: the totals, then one line per surface."""
        lines = [
            f"gui sweep: {self.qt_platform} | {self.totals['surfaces']} surfaces | "
            f"{self.totals['controls']} controls | {self.totals['clicked']} clicked | "
            f"{self.totals['disabled']} disabled | {self.totals['internal']} Qt-internal | "
            f"{self.totals['click_errors']} errors | {self.totals['unhandled']} unhandled | "
            f"{self.seconds:.1f}s",
            f"totals: {json.dumps(self.totals, sort_keys=True)}",
        ]
        for surface in self.surfaces:
            lines.append(
                f"  {surface['name']:<18} {len(surface['controls']):>3} controls  "
                f"{surface['clicked']:>3} clicked  {(surface['errors'] and len(surface['errors'])) or 0} errors  "
                f"min={surface['minimum_size']}"
            )
        if self.failures:
            lines.append(f"FAILURES ({len(self.failures)}):")
            lines.extend(
                f"  {failure['surface']}.{failure['control']}: {failure['kind']} — {failure['detail']}"
                for failure in self.failures
            )
        else:
            lines.append("no failures")
        return "\n".join(lines)


def ensure_application() -> QtWidgets.QApplication:
    """The one ``QApplication`` for this process, always on the offscreen platform."""
    existing = QtWidgets.QApplication.instance()
    if isinstance(existing, QtWidgets.QApplication):
        return existing
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication([sys.argv[0] if sys.argv else "gui_sweep"])
    app.setApplicationName("Spice Maker gui sweep")
    app.setAttribute(QtCore.Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    return app


def _new_top_levels(app: QtWidgets.QApplication, before: set[int]) -> list[QtWidgets.QWidget]:
    return [widget for widget in app.topLevelWidgets() if id(widget) not in before]


def _sweep_control(
    surface: QtWidgets.QWidget,
    surface_name: str,
    button: QtWidgets.QAbstractButton,
    attributes: dict[int, str],
    sandbox: Sandbox,
    watch: _Watch,
    pump: Callable[[], None],
) -> dict[str, Any]:
    """Click one button, capture what happened, and report it as one record."""
    connections = _connections(button)
    attribute, handler = _source_wiring(surface, button, attributes)
    record: dict[str, Any] = {
        "surface": surface_name,
        "widget_class": type(button).__name__,
        "label": button.text().strip() or button.objectName(),
        "object_name": button.objectName(),
        "enabled": bool(button.isEnabled()),
        "visible": bool(button.isVisible()),
        "checkable": bool(button.isCheckable()),
        "attribute": attribute,
        "handler": handler,
        "connections": connections,
        "connected": _is_wired(connections),
        "click_method": None,
        "click_position": None,
        "clicked": False,
        "clicks": 0,
        "checked_before": bool(button.isChecked()) if button.isCheckable() else None,
        "checked_after_first": None,
        "round_trip": None,
        "effects": [],
        "error": None,
        "button_deleted": False,
        "surface_deleted": False,
        "skipped": None,
        "unhandled": False,
        "note": "",
    }
    if record["object_name"] in INTERNAL_OBJECT_NAMES:
        record["skipped"] = f"Qt-internal chrome ({record['object_name']}): not a user action"
        return record
    if not record["enabled"]:
        record["skipped"] = "disabled when the sweep reached it"
        return record

    if not surface.isVisible():  # an earlier click (CLOSE) hid the surface; keep going
        surface.show()
        pump()

    sandbox.mark()
    mark = watch.mark()
    before = _fingerprint(surface)
    effects: list[str] = []
    try:
        method, position = _click(button)
        record["click_method"] = method
        record["click_position"] = [position.x(), position.y()]
        record["clicked"] = True
        record["clicks"] = 1
    except Exception:
        record["error"] = traceback.format_exc(limit=8)
    pump()
    effects.extend(_changes(before, _fingerprint(surface)))
    effects.extend(sandbox.effects())

    if record["checkable"] and record["clicked"]:
        # A checkbox is a switch: a second press must land back where the first started.
        record["checked_after_first"] = bool(button.isChecked()) if _alive(button) else None
        second_before = _fingerprint(surface)
        sandbox.mark()
        try:
            _click(button)
            record["clicks"] = 2
        except Exception:
            record["error"] = record["error"] or traceback.format_exc(limit=8)
        pump()
        effects.extend(_changes(second_before, _fingerprint(surface)))
        effects.extend(sandbox.effects())
        record["round_trip"] = (
            bool(button.isChecked()) == record["checked_before"] if _alive(button) else False
        )

    tracebacks, qt_messages = watch.since(mark)
    if tracebacks and record["error"] is None:
        # PySide6 reports a slot's exception through sys.excepthook and lets the click
        # return normally; without this the sweep would call that click "clean".
        record["error"] = tracebacks[0]["traceback"]
    record["qt_messages"] = qt_messages
    record["effects"] = effects
    record["button_deleted"] = not _alive(button)
    record["surface_deleted"] = not _alive(surface)
    if not effects and not record["connected"] and not handler:
        record["unhandled"] = True
        record["note"] = "nothing observed: no effect, no signal connection, no named handler"
    elif record["clicked"] and not effects:
        record["note"] = (
            "no visible change: legitimate when the control has nothing to act on "
            "(the wiring is proven, the handler named in the source is not)"
        )
    return record


def _sweep_surface(
    spec: SurfaceSpec,
    app: QtWidgets.QApplication,
    sandbox: Sandbox,
    watch: _Watch,
    pump: Callable[[], None],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Open one surface, sweep its buttons, close it, and report the surface."""
    started = time.monotonic()
    entry: dict[str, Any] = {
        "name": spec.name,
        "class": "",
        "reachable": spec.reachable,
        "opened": False,
        "skip_reason": None,
        "size_hint": None,
        "minimum_size": None,
        "minimum_size_hint": None,
        "controls": [],
        "clicked": 0,
        "errors": [],
        "unhandled": [],
        "opened_windows": [],
        "seconds": 0.0,
    }
    failures: list[dict[str, str]] = []
    try:
        surface = spec.factory()
    except Exception:
        detail = traceback.format_exc(limit=8).strip().splitlines()[-1]
        entry["skip_reason"] = f"construction failed: {detail}"
        entry["errors"].append(f"construction failed: {detail}")
        entry["seconds"] = round(time.monotonic() - started, 3)
        failures.append(
            {
                "surface": spec.name,
                "control": "<surface>",
                "kind": "surface-construction",
                "detail": detail,
            }
        )
        return entry, failures

    entry["class"] = type(surface).__name__
    entry["minimum_size_hint"] = _size(surface.minimumSizeHint())
    if spec.prepare is not None:
        spec.prepare(surface)
    top_levels_before = {id(widget) for widget in app.topLevelWidgets()}
    surface.show()
    pump()
    # Read the floor *after* showing: Qt applies a layout's minimum size to the widget
    # when the layout activates, so before ``show()`` every dialog reports (0, 0).
    entry["size_hint"] = _size(surface.sizeHint())
    entry["minimum_size"] = _size(surface.minimumSize())
    entry["opened"] = True

    attributes = _attribute_names(surface)
    for button in list(surface.findChildren(QtWidgets.QAbstractButton)):
        record = _sweep_control(surface, spec.name, button, attributes, sandbox, watch, pump)
        entry["controls"].append(record)
        label = f"{record['widget_class']} {record['label']!r}"
        if record["clicked"]:
            entry["clicked"] += 1
        if record["error"]:
            entry["errors"].append(f"{label}: {record['error'].strip().splitlines()[-1]}")
            failures.append(
                {
                    "surface": spec.name,
                    "control": label,
                    "kind": "click-error",
                    "detail": record["error"].strip().splitlines()[-1],
                }
            )
        if record["unhandled"]:
            entry["unhandled"].append(label)
            failures.append(
                {
                    "surface": spec.name,
                    "control": label,
                    "kind": "unhandled",
                    "detail": record["note"],
                }
            )
        # ``round_trip`` is True/False/None, so truthiness is exactly "it did not hold".
        if record["checkable"] and record["clicked"] and not record["round_trip"]:
            failures.append(
                {
                    "surface": spec.name,
                    "control": label,
                    "kind": "checkbox-round-trip",
                    "detail": (
                        f"checked {record['checked_before']} -> "
                        f"{record['checked_after_first']} -> "
                        f"{button.isChecked() if _alive(button) else 'deleted'}"
                    ),
                }
            )
        if record["button_deleted"] or record["surface_deleted"]:
            failures.append(
                {
                    "surface": spec.name,
                    "control": label,
                    "kind": "widget-deleted",
                    "detail": "the click deleted the widget it was pressed on",
                }
            )
            break
        if QtWidgets.QApplication.instance() is None:  # pragma: no cover - fatal teardown
            entry["errors"].append("the QApplication went away during the sweep")
            break

    for widget in _new_top_levels(app, top_levels_before):
        if widget is surface or not widget.isVisible():
            # Qt's own machinery (menus, popup frames) is not a surface the sweep opened,
            # and deleting it behind Qt's back would be worse than leaving it alone.
            continue
        entry["opened_windows"].append(type(widget).__name__)
        _close(widget)
    _close(surface)
    pump()
    _flush_deletes()
    entry["seconds"] = round(time.monotonic() - started, 3)
    return entry, failures


def _totals(surfaces: list[dict[str, Any]], failures: list[dict[str, str]]) -> dict[str, int]:
    controls = [control for surface in surfaces for control in surface["controls"]]
    checkable = [control for control in controls if control["checkable"] and control["clicked"]]
    return {
        "surfaces": len(surfaces),
        "surfaces_opened": sum(1 for surface in surfaces if surface["opened"]),
        "surfaces_skipped": sum(1 for surface in surfaces if surface["skip_reason"]),
        "controls": len(controls),
        "clicked": sum(1 for control in controls if control["clicked"]),
        "disabled": sum(1 for control in controls if not control["enabled"]),
        "internal": sum(
            1 for control in controls if control["object_name"] in INTERNAL_OBJECT_NAMES
        ),
        "checkable_clicked": len(checkable),
        "round_trips": sum(1 for control in checkable if control["round_trip"]),
        "click_errors": sum(1 for control in controls if control["error"]),
        "unhandled": sum(1 for control in controls if control["unhandled"]),
        "failures": len(failures),
    }


def run_sweep(
    *,
    sandbox_root: Path | None = None,
    pump_iterations: int = 3,
    pump_ms: int = 5,
    surfaces: Sequence[str] | None = None,
    application: QtWidgets.QApplication | None = None,
    invocation: str | None = None,
) -> SweepReport:
    """Sweep every surface once and return the report.

    ``sandbox_root`` is where all the state the surfaces would touch is redirected;
    ``surfaces`` restricts the sweep to a subset of :data:`SURFACE_NAMES`. The caller
    owns the ``QApplication`` (this function reuses one when it exists), so the tool and
    the pytest wrapper can both call it in one process.
    """
    started = time.monotonic()
    app = application or ensure_application()
    root = (
        Path(sandbox_root)
        if sandbox_root is not None
        else Path(tempfile.mkdtemp(prefix="gui-sweep-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    wanted = list(surfaces) if surfaces is not None else list(SURFACE_NAMES)
    specs = [spec for spec in surface_specs() if spec.name in wanted]

    def pump() -> None:
        _pump(pump_iterations, pump_ms)

    sandbox = Sandbox(root)
    dismisser = _ModalDismisser(sandbox)
    watch = _Watch.start()
    entries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    try:
        sandbox.install()
        dismisser.start()
        for spec in specs:
            entry, surface_failures = _sweep_surface(spec, app, sandbox, watch, pump)
            entries.append(entry)
            failures.extend(surface_failures)
        dismisser.stop()
    finally:
        dismisser.stop()
        sandbox.restore()
        watch.stop()
        _flush_deletes()
        pump()

    # A traceback captured outside any click (a timer slot, a thread) is still a defect.
    click_tracebacks = {
        control["error"] for entry in entries for control in entry["controls"] if control["error"]
    }
    for record in watch.tracebacks:
        if record["traceback"] not in click_tracebacks:
            failures.append(
                {
                    "surface": "<sweep>",
                    "control": record["source"],
                    "kind": "captured-traceback",
                    "detail": f"{record['type']}: {record['message']}",
                }
            )
    for message in watch.qt_messages:
        if message["level"] in {"QtCriticalMsg", "QtFatalMsg"}:
            failures.append(
                {
                    "surface": "<sweep>",
                    "control": "qt",
                    "kind": "qt-critical",
                    "detail": message["message"][:160],
                }
            )

    totals = _totals(entries, failures)
    totals["captured_tracebacks"] = len(watch.tracebacks)
    totals["qt_messages"] = len(watch.qt_messages)
    totals["qt_critical"] = sum(
        1 for message in watch.qt_messages if message["level"] in {"QtCriticalMsg", "QtFatalMsg"}
    )

    return SweepReport(
        invocation=invocation or _invocation(),
        cwd=str(Path.cwd()),
        qt_platform=str(app.platformName()),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        seconds=time.monotonic() - started,
        sandbox={
            "root": str(root),
            "events": len(sandbox.events),
            "kinds": _event_kinds(sandbox),
            "environment_variables_removed": sorted(sandbox.removed_environment),
        },
        surfaces=entries,
        totals=totals,
        failures=failures,
        captured_tracebacks=watch.tracebacks,
        qt_messages=watch.qt_messages,
        limitations=list(LIMITATIONS),
    )


def _event_kinds(sandbox: Sandbox) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for event in sandbox.events:
        kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
    return kinds


def _invocation() -> str:
    """How this process was started, so a report can be reproduced."""
    parts = [Path(sys.executable).name, *(shlex.quote(str(item)) for item in sys.argv)]
    return " ".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep, print the summary, write ``--json``, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(
        prog="gui_sweep",
        description="Click every button on every surface, offscreen, and report what happened.",
    )
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    parser.add_argument(
        "--surfaces",
        default=None,
        help=f"comma-separated subset of {', '.join(SURFACE_NAMES)}",
    )
    parser.add_argument("--sandbox", type=Path, default=None, help="where sandbox state lives")
    parser.add_argument("--pump-iterations", type=int, default=3, help="event turns per click")
    parser.add_argument("--pump-ms", type=int, default=5, help="milliseconds per event turn")
    args = parser.parse_args(list(argv) if argv is not None else None)

    wanted = None
    if args.surfaces:
        wanted = [name.strip() for name in args.surfaces.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in SURFACE_NAMES]
        if unknown:
            parser.error(f"unknown surface(s): {', '.join(unknown)}")

    report = run_sweep(
        sandbox_root=args.sandbox,
        pump_iterations=args.pump_iterations,
        pump_ms=args.pump_ms,
        surfaces=wanted,
    )
    print(report.summary())
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
        print(f"report: {args.json}")
    for failure in report.failures:
        print(
            f"FAIL {failure['surface']}.{failure['control']}: "
            f"{failure['kind']} — {failure['detail']}",
            file=sys.stderr,
        )
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(main())
