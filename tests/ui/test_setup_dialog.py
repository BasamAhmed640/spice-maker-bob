"""The setup page: persistent settings only, and none of them leak.

These tests drive the real widgets and check what the page actually persists — the
config file it writes and the credential store it talks to — plus the property the owner
asked for explicitly: the page is sized to its content, with no dead space under it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

SECRET = "sk-bob-sentinel-0123456789"


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    """Point every config read/write at a throwaway file."""
    target = tmp_path / "config.json"
    monkeypatch.setattr("boardmodeler.config.config_path", lambda: target)
    monkeypatch.setattr("boardmodeler.ui.setup_dialog.config_path", lambda: target)
    return target


@pytest.fixture
def dialog(qtbot, isolated_config):
    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)
    return page


def test_the_page_is_sized_to_its_content(dialog) -> None:
    """A page taller than its content is the dead space the owner objected to."""
    assert dialog.height() == dialog.sizeHint().height()
    assert dialog.width() == dialog.sizeHint().width()


def test_saving_persists_only_the_declared_settings(dialog, isolated_config: Path) -> None:
    dialog.model_dir_edit.setText(str(isolated_config.parent / "models"))
    dialog.ltspice_edit.setText(r"C:\tools\LTspice.exe")
    dialog.reinforce_check.setChecked(False)

    dialog._save()

    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["default_model_dir"] == str(isolated_config.parent / "models")
    assert saved["ltspice"]["path"] == r"C:\tools\LTspice.exe"
    assert saved["web_reinforcement"] is False


def test_the_api_key_goes_to_the_credential_store_and_never_to_the_config(
    dialog, isolated_config: Path, monkeypatch
) -> None:
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "boardmodeler.security.credentials.set_credential",
        lambda name, value: stored.append((name, value)),
    )
    monkeypatch.setattr(
        "boardmodeler.security.credentials.describe_credential",
        lambda name: "sourced from the keyring",
    )

    dialog.key_edit.setText(SECRET)
    dialog._save_key()
    dialog._save()

    assert stored == [("bob_shell", SECRET)]
    assert dialog.key_edit.text() == "", "the field must not keep the secret on screen"
    assert SECRET not in isolated_config.read_text(encoding="utf-8")


def test_settings_round_trip_through_the_config(dialog, isolated_config: Path) -> None:
    dialog.reinforce_check.setChecked(False)
    dialog._save()

    from boardmodeler.config import load_config

    reloaded = load_config(isolated_config)
    assert reloaded.web_reinforcement is False


def test_the_smoke_test_is_run_and_reported(dialog, monkeypatch, tmp_path: Path) -> None:
    """RUN SMOKE TEST must call the real smoke test and show the measured value."""

    class _Install:
        path = tmp_path / "LTspice.exe"

    class _Result:
        status = "pass"
        measured_v = 0.6321192595944498
        detail = "RC step measured at 1 ms"

    calls: list[Path] = []

    def fake_smoke(exe: Path, workdir: Path):
        calls.append(Path(exe))
        return _Result()

    monkeypatch.setattr("boardmodeler.simulation.ltspice.locate", lambda explicit=None: _Install())
    monkeypatch.setattr("boardmodeler.simulation.ltspice.smoke_test", fake_smoke)

    dialog._run_smoke()

    assert calls, "the smoke test must actually be invoked"
    assert "0.6321" in dialog.ltspice_status.text()
    assert "pass" in dialog.ltspice_status.text().lower()


def test_a_missing_ltspice_is_reported_not_raised(dialog, monkeypatch) -> None:
    monkeypatch.setattr("boardmodeler.simulation.ltspice.locate", lambda explicit=None: None)
    dialog._run_smoke()
    assert "not found" in dialog.ltspice_status.text().lower()


def test_json_mode_describes_the_settings_without_a_window(isolated_config: Path) -> None:
    from boardmodeler.ui.setup_dialog import main

    assert main(["--json"]) == 0


def test_the_page_carries_only_persistent_settings(dialog) -> None:
    """No project directory, no provider selection, no policy acknowledgement."""
    labels = {
        child.text().strip()
        for child in dialog.findChildren(type(dialog.key_status))
        if isinstance(child.text(), str)
    }
    for gone in ("PROJECT", "PROVIDER", "DATA POLICY", "PRIVACY"):
        assert gone not in labels, f"{gone} does not belong on the settings page"


def test_the_ltspice_library_is_shown_read_only(dialog) -> None:
    from boardmodeler.ui.setup_dialog import ltspice_user_lib

    shown = [
        child.text()
        for child in dialog.findChildren(type(dialog.key_status))
        if str(ltspice_user_lib()) in child.text()
    ]
    assert shown, "the LTspice user library path must be visible"


def test_choosing_a_provider_points_the_key_row_at_its_own_credential(
    dialog, isolated_config: Path, monkeypatch
) -> None:
    """The key row follows the provider row: label, model default and stored name."""
    from boardmodeler import agent_providers

    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "boardmodeler.security.credentials.set_credential",
        lambda name, value: stored.append((name, value)),
    )
    entries = list(agent_providers.CATALOG)
    combo = dialog.provider_combo
    if combo is None:
        # A one-entry build has no row to choose from: that page is the sole entry's own.
        assert dialog.restricted_note is not None, "a single-provider build must say so"
        chosen = entries[0]
        assert dialog.key_label.text() == chosen.key_label
        assert dialog.model_edit.isVisible() is chosen.model_editable
    else:
        assert dialog.restricted_note is None, "a build with a choice must not claim restriction"
        ids = [combo.itemData(index) for index in range(combo.count())]
        assert ids == [entry.id for entry in entries], "one row per provider this build accepts"
        assert dialog.key_label.text() == entries[0].key_label
        assert dialog.model_edit.isVisible() is entries[0].model_editable

        chosen = entries[1]
        combo.setCurrentIndex(ids.index(chosen.id))
        assert dialog.key_label.text() == chosen.key_label
        assert dialog.model_edit.isVisible() is chosen.model_editable
        assert dialog.model_edit.text() == (chosen.model or ""), "the provider's own default"

    dialog.key_edit.setText(SECRET)
    dialog._save_key()
    dialog._save()

    assert stored == [(chosen.credential, SECRET)], "the key is stored under the chosen provider"
    saved = json.loads(isolated_config.read_text(encoding="utf-8"))
    assert saved["agent_provider"] == chosen.id
    if chosen.model_editable:
        assert saved["agent_model"] == dialog.model_edit.text()
    assert SECRET not in isolated_config.read_text(encoding="utf-8")


def test_a_single_provider_catalog_keeps_the_bob_only_page(
    qtbot, isolated_config: Path, monkeypatch
) -> None:
    """The Bob-only build is one catalog entry away: no provider row, Bob's own label."""
    from boardmodeler import agent_providers

    bob = agent_providers.by_id("bob")
    assert bob is not None
    monkeypatch.setattr(agent_providers, "CATALOG", (bob,))

    from boardmodeler.ui.setup_dialog import SetupDialog

    page = SetupDialog()
    qtbot.addWidget(page)
    page.show()
    qtbot.waitExposed(page)

    assert page.provider_combo is None
    assert page.key_label.text() == "BOB API KEY"
    assert page.model_edit.isVisible() is False
    assert page.restricted_note is not None
    assert "IBM Bob API only" in page.restricted_note.text()
    assert page.height() == page.sizeHint().height()
    assert page.width() == page.sizeHint().width()

    from boardmodeler.ui.model_maker import ModelMakerWindow

    window = ModelMakerWindow()
    qtbot.addWidget(window)
    assert window.windowTitle().endswith("· IBM Bob only")


def test_an_unknown_provider_in_the_config_is_reported_never_replaced(
    isolated_config: Path,
) -> None:
    """A hand-edited config naming a provider this build lacks is refused, not laundered."""
    from boardmodeler import agent_providers
    from boardmodeler.config import load_config
    from boardmodeler.ui.setup_dialog import configured_provider, describe_settings

    config = load_config(isolated_config)
    config.agent_provider = "not-a-provider"

    provider, reason = configured_provider(config)

    assert provider is None, "a provider this build lacks must never be swapped for another"
    assert reason.startswith("api_provider_unavailable:") and "not-a-provider" in reason
    assert all(f"'{name}'" in reason for name in agent_providers.ids())

    described = describe_settings(config)
    assert described["agent_provider"] == "not-a-provider", "the configured id is kept as written"
    assert described["agent_provider_accepted"] is False
