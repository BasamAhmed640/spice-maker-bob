"""The public CLI exposes model work, not dormant board workflows."""

import pytest

from boardmodeler.cli import build_parser


@pytest.mark.parametrize(
    "argv",
    [
        ["demo", "build", "--out", "test-output"],
        ["circuit", "check", "--project", "test-project"],
        ["run", "mutations", "--project", "test-project", "--report", "test-report.json"],
    ],
)
def test_board_workflows_are_not_public_commands(argv, capsys):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_model_generation_and_verification_commands_remain_available():
    parser = build_parser()
    model = parser.parse_args(["model", "build", "--part", "TPS54332DDA", "--out", "model-out"])
    tests = parser.parse_args(["run", "tests", "--project", "model-project"])
    assert (model.command, model.model_command) == ("model", "build")
    assert (tests.command, tests.run_command) == ("run", "tests")


def test_board_project_is_not_a_model_maker_argument(capsys):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["ui", "--project", "old-board"])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--board-ui"], ["--project", "old-board"]])
def test_legacy_board_window_is_not_public(argv, capsys):
    pytest.importorskip("PySide6")
    from boardmodeler.ui.app import main

    with pytest.raises(SystemExit) as error:
        main(argv, exec_app=False)
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
