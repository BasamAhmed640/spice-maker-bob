"""The text menu preserves the CLI's build and reopen paths."""

from __future__ import annotations

import json

from boardmodeler import cli, terminal_menu
from boardmodeler.config import AppConfig
from boardmodeler.pipeline.make_model import MakeModelResult, RowOutcome
from boardmodeler.settings_summary import describe_settings


def _reader(*answers: str):
    values = iter(answers)
    return lambda _prompt: next(values)


def test_no_arguments_open_the_menu(monkeypatch):
    monkeypatch.setattr("boardmodeler.storage.initialize", lambda: None)
    monkeypatch.setattr("boardmodeler.storage.install_write_guard", lambda: None)
    monkeypatch.setattr(terminal_menu, "run_menu", lambda: 23)

    assert cli.main([]) == 23


def test_menu_refuses_a_build_before_setup(monkeypatch, capsys):
    monkeypatch.setattr(terminal_menu, "load_config", lambda: AppConfig(setup_complete=False))

    def forbidden(_arguments):
        raise AssertionError("the engine must not run before setup")

    assert terminal_menu.run_menu(reader=_reader("1", "5"), command=forbidden) == 1
    assert "Setup is unfinished" in capsys.readouterr().out


def test_menu_build_accepts_dragged_quoted_pdf_and_uses_existing_handler(monkeypatch, tmp_path):
    datasheet = tmp_path / "a datasheet.pdf"
    datasheet.write_bytes(b"%PDF-1.4\n")
    base = tmp_path / "finished models"
    monkeypatch.setattr(
        terminal_menu,
        "load_config",
        lambda: AppConfig(setup_complete=True, default_model_dir=str(base), internet_access=True),
    )
    commands = []

    def command(arguments):
        commands.append(arguments)
        return 0

    assert (
        terminal_menu.run_menu(reader=_reader("1", "LM358", f'"{datasheet}"', "5"), command=command)
        == 0
    )
    assert commands == [
        [
            "model",
            "build",
            "--part",
            "LM358",
            "--datasheet",
            str(datasheet),
            "--out",
            str(base / "LM358"),
            "--allow-remote",
        ]
    ]


def test_menu_does_not_allow_remote_when_internet_is_off(monkeypatch, tmp_path):
    datasheet = tmp_path / "local.pdf"
    datasheet.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        terminal_menu,
        "load_config",
        lambda: AppConfig(setup_complete=True, internet_access=False),
    )
    commands = []
    terminal_menu.run_menu(
        reader=_reader("1", "LM358", str(datasheet), "5"),
        command=lambda arguments: commands.append(arguments) or 0,
    )
    assert "--allow-remote" not in commands[0]


def test_menu_retest_uses_model_open_verification(monkeypatch, tmp_path):
    base = tmp_path / "models"
    saved = base / "LM358"
    saved.mkdir(parents=True)
    monkeypatch.setattr(
        terminal_menu,
        "load_config",
        lambda: AppConfig(default_model_dir=str(base)),
    )
    commands = []
    assert (
        terminal_menu.run_menu(
            reader=_reader("2", "1", "t", "5"),
            command=lambda arguments: commands.append(arguments) or 0,
        )
        == 0
    )
    assert commands == [["model", "open", "--out", str(saved), "--verify"]]


def test_menu_check_setup_reports_an_unready_doctor(monkeypatch, capsys):
    monkeypatch.setattr(cli, "doctor_payload", lambda: {"ok": False})
    monkeypatch.setattr(cli, "_render_doctor_human", lambda _payload: "LTspice is not configured")

    assert terminal_menu.run_menu(reader=_reader("4", "5"), command=lambda _args: 0) == 1
    assert "Setup check: needs attention" in capsys.readouterr().out


def test_menu_check_setup_requires_a_completed_wizard(monkeypatch, capsys):
    monkeypatch.setattr(cli, "doctor_payload", lambda: {"ok": True})
    monkeypatch.setattr(cli, "_render_doctor_human", lambda _payload: "LTspice smoke test passed")
    monkeypatch.setattr(terminal_menu, "load_config", lambda: AppConfig(setup_complete=False))

    assert terminal_menu.run_menu(reader=_reader("4", "5"), command=lambda _args: 0) == 1
    assert "wizard has not been completed" in capsys.readouterr().out


def test_setup_flags_forward_to_the_wizard(monkeypatch):
    from boardmodeler import setup_wizard

    monkeypatch.setattr("boardmodeler.storage.initialize", lambda: None)
    monkeypatch.setattr("boardmodeler.storage.install_write_guard", lambda: None)
    forwarded = []

    def wizard(arguments):
        forwarded.append(arguments)
        return 0

    monkeypatch.setattr(setup_wizard, "main", wizard)
    assert (
        cli.main(
            [
                "setup",
                "--ltspice",
                "C:/LTspice.exe",
                "--model-dir",
                "models",
                "--provider",
                "bob",
                "--internet",
                "off",
                "--key-env",
                "TEST_KEY",
                "--yes",
            ]
        )
        == 0
    )
    assert forwarded == [
        [
            "--ltspice",
            "C:/LTspice.exe",
            "--model-dir",
            "models",
            "--provider",
            "bob",
            "--internet",
            "off",
            "--key-env",
            "TEST_KEY",
            "--yes",
        ]
    ]


def test_bob_settings_report_keeps_the_bob_only_refusal():
    settings = describe_settings(AppConfig(agent_provider="deepseek"))

    assert settings["agent_provider"] is None
    assert settings["agent_provider_accepted"] is False
    assert settings["agent_model"] is None
    assert settings["accepted_providers"] == ["bob"]
    assert "IBM Bob only" in settings["agent_provider_problem"]


def test_bob_model_open_reads_saved_result_without_running_the_engine(
    monkeypatch, tmp_path, capsys
):
    out = tmp_path / "LM358"
    out.mkdir()
    result = MakeModelResult(
        status="UNKNOWN",
        detail="one row was not testable",
        part="LM358",
        out_dir=tmp_path / "old location",
        card_path=tmp_path / "old location" / "MODEL_CARD.md",
        lib_path=tmp_path / "old location" / "LM358.lib",
        asy_path=tmp_path / "old location" / "LM358.asy",
        rows=(RowOutcome("VOS", "Input offset voltage", "2mV", "unknown", "UNKNOWN", 6),),
        counts={"UNKNOWN": 1},
        stages=(),
    )
    (out / "results.json").write_text(result.to_json(), encoding="utf-8")
    (out / "MODEL_CARD.md").write_text("# LM358", encoding="utf-8")
    (out / "LM358.lib").write_text(".subckt LM358 IN OUT\n.ends", encoding="utf-8")
    (out / "LM358.asy").write_text("Version 4", encoding="utf-8")
    monkeypatch.setattr("boardmodeler.storage.initialize", lambda: None)
    monkeypatch.setattr("boardmodeler.storage.install_write_guard", lambda: None)

    assert cli.main(["model", "open", "--out", str(out), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["part"] == "LM358"
    assert payload["status"] == "UNKNOWN"
    assert payload["counts"] == {"UNKNOWN": 1}
    assert payload["lib_exists"] is True
    assert payload["asy_exists"] is True
    assert payload["rows"][0]["req_id"] == "VOS"
    assert payload["lib_path"] == str(out / "LM358.lib")


def test_bob_model_open_verify_uses_model_test_path(monkeypatch, tmp_path, capsys):
    out = tmp_path / "LM358"
    out.mkdir()
    result = MakeModelResult(
        status="UNKNOWN",
        detail="recorded result",
        part="LM358",
        out_dir=out,
        card_path=None,
        lib_path=out / "LM358.lib",
        asy_path=None,
        rows=(),
        counts={"UNKNOWN": 0},
        stages=(),
    )
    (out / "results.json").write_text(result.to_json(), encoding="utf-8")
    monkeypatch.setattr("boardmodeler.storage.initialize", lambda: None)
    monkeypatch.setattr("boardmodeler.storage.install_write_guard", lambda: None)
    called = []

    def fake_test(folder, *, timeout_s):
        called.append((folder, timeout_s))
        return {"tool": "boardmodeler", "command": "model test", "status": "PASS"}, True, "abc"

    monkeypatch.setattr(cli, "_run_model_test", fake_test)
    assert cli.main(["model", "open", "--out", str(out), "--verify", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNKNOWN"
    assert payload["verification"]["status"] == "PASS"
    assert payload["verified"] is True
    assert called == [(out, 120.0)]


def test_bob_model_open_reports_damaged_results_plainly(monkeypatch, tmp_path, capsys):
    out = tmp_path / "broken"
    out.mkdir()
    (out / "results.json").write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr("boardmodeler.storage.initialize", lambda: None)
    monkeypatch.setattr("boardmodeler.storage.install_write_guard", lambda: None)

    assert cli.main(["model", "open", "--out", str(out)]) == 1
    output = capsys.readouterr().out
    assert "could not read the saved model" in output
    assert "Traceback" not in output
