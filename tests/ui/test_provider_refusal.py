"""A provider this build does not accept: named, refused, never substituted.

The finding this covers was a silent substitution — a config naming an id outside
``agent_providers.CATALOG`` ran the build's default provider (Bob) with Bob's key
instead. So here the page names the configured id in its own line, SAVE leaves the id
alone until the user picks a provider, ``describe_settings`` reports the raw id with an
acceptance flag, and the window hands the raw id to the request so the engine's own
``api_provider_unavailable`` refusal is what the user sees.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

#: One letter away from the catalog's ``deepseek``: the typo the page must not launder.
UNACCEPTED = "deepsek"


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway config file that names a provider this build does not accept."""
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"agent_provider": UNACCEPTED}), encoding="utf-8")
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.ui.setup_dialog.config_path", lambda: target)
    return target


def _page(qtbot):
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)
    return page


# --------------------------------------------------------------------------- #
# the settings as data


def test_the_configured_id_is_reported_raw_with_an_acceptance_flag(
    isolated_config: Path,
) -> None:
    from boardmodeler import agent_providers
    from boardmodeler.config import load_config
    from boardmodeler.ui.setup_dialog import describe_settings

    assert UNACCEPTED not in agent_providers.ids(), "the fixture id must be foreign"
    described = describe_settings(load_config())

    assert described["agent_provider"] == UNACCEPTED
    assert described["agent_provider_accepted"] is False
    assert described["accepted_providers"] == list(agent_providers.ids())
    assert "api_provider_unavailable" in str(described["agent_provider_problem"])


def test_nothing_configured_reports_the_build_default_as_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from boardmodeler import agent_providers
    from boardmodeler.config import load_config
    from boardmodeler.ui.setup_dialog import describe_settings

    monkeypatch.setattr("boardmodeler.config.config_path", lambda: tmp_path / "config.json")

    described = describe_settings(load_config())

    assert described["agent_provider"] == agent_providers.default_provider().id
    assert described["agent_provider_accepted"] is True


# --------------------------------------------------------------------------- #
# the setup page


def test_the_page_names_the_unaccepted_provider_and_save_keeps_it(
    qtbot, isolated_config: Path
) -> None:
    page = _page(qtbot)

    assert page.provider_status is not None, "the refusal must be visible on the page"
    shown = page.provider_status.text()
    assert UNACCEPTED in shown
    assert "does not accept" in shown
    assert page.height() == page.sizeHint().height()
    assert page.width() == page.sizeHint().width()

    page._save()

    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["agent_provider"] == UNACCEPTED, "SAVE must not rewrite the id to the default"


def test_choosing_a_provider_is_what_replaces_the_unaccepted_id(
    qtbot, isolated_config: Path
) -> None:
    from boardmodeler import agent_providers

    page = _page(qtbot)
    combo = page.provider_combo
    if combo is None:
        # A build with one provider has no row to choose from: it says so, and SAVE
        # leaves the stored id alone rather than rewriting it behind the user's back.
        assert agent_providers.only_provider() is not None
        assert page.restricted_note is not None, "a single-provider build says which one"
        page._save()

        saved = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert saved["agent_provider"] == UNACCEPTED
        return

    choices = [combo.itemData(index) for index in range(combo.count())]
    assert choices == list(agent_providers.ids()), "the row never invents an entry"

    chosen = choices[-1]
    combo.setCurrentIndex(choices.index(chosen))
    page._save()

    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["agent_provider"] == chosen


# --------------------------------------------------------------------------- #
# the window


def test_the_window_pre_flight_reports_the_engines_own_refusal(isolated_config: Path) -> None:
    from boardmodeler.ui.model_maker import _agent_availability

    usable, reason = _agent_availability()

    assert usable is False
    assert reason.startswith("api_provider_unavailable:"), reason
    assert UNACCEPTED in reason


def test_the_window_refuses_before_any_run_and_says_why(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_config: Path
) -> None:
    """The pre-flight keeps the same words as the engine: nothing runs, the reason shows."""
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda parent, title, text, *a, **k: shown.append((title, text)),
    )

    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "tps54320.pdf"
    datasheet.write_bytes(b"%PDF-1.4 fake")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path / "out"))

    window.go_button.click()

    assert window._result is None and window._worker is None, "nothing may be started"
    assert shown and shown[0][0] == "No agent available"
    assert "api_provider_unavailable" in shown[0][1]
    assert UNACCEPTED in shown[0][1]


@dataclass(frozen=True)
class _Result:
    status: str = "BLOCKED"
    detail: str = "api_provider_unavailable: the engine refused"
    counts: dict = field(default_factory=dict)
    rows: tuple = ()
    lib_path: Path | None = None


def _engine_module(calls: list[object]) -> ModuleType:
    """A stand-in engine that records the request instead of building a model."""
    module = ModuleType("boardmodeler.pipeline.make_model")

    @dataclass(frozen=True)
    class _Request:
        part: str
        subckt: str
        datasheet: Path
        out_dir: Path
        backend_name: str
        provider: str | None = None
        allow_remote: bool = False

    def make_model(request: object, progress: object = None, cancel: object = None) -> _Result:
        calls.append(request)
        return _Result()

    module.make_model = make_model  # type: ignore[attr-defined]
    module.MakeModelRequest = _Request  # type: ignore[attr-defined]
    return module


def test_the_window_hands_the_raw_id_to_the_request(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_config: Path
) -> None:
    calls: list[object] = []
    monkeypatch.setitem(sys.modules, "boardmodeler.pipeline.make_model", _engine_module(calls))
    monkeypatch.setattr(
        "boardmodeler.ui.model_maker._agent_availability", lambda: (True, "test agent")
    )

    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    datasheet = tmp_path / "tps54320.pdf"
    datasheet.write_bytes(b"%PDF-1.4 fake")
    window.part_edit.setText("TPS54320")
    window.datasheet_edit.setText(str(datasheet))
    window.out_edit.setText(str(tmp_path / "out"))

    window.go_button.click()
    qtbot.waitUntil(lambda: window._result is not None, timeout=10_000)

    assert calls, "the window must start the run"
    assert calls[0].provider == UNACCEPTED, (
        "the request must carry the configured id so the engine refuses it"
    )
