"""The deck policy: what a rogue candidate deck may not name.

Every test works on real files under ``tmp_path`` (the policy's only I/O is
``resolve()``/``exists()``, so a real folder is the only honest fixture), and
each test states one idea. The last two tests are the integration evidence: the
decks the *app itself* renders must pass the policy unchanged, so that wiring the
policy into the harness (wave 2) cannot reject the app's own runs.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from boardmodeler.authoring.deck_policy import (
    MAX_LINE_CHARS,
    VIOLATION_CODES,
    check_deck,
    safe_deck_summary,
)
from boardmodeler.authoring.probes import PROBES, ProbeError
from boardmodeler.models.regulator import write_regulator_library

SUBCKT = "BM_REG_BUCK"
REPO = Path(__file__).resolve().parents[2]

#: The I/O probe family's synthetic electrical test double (the same one the
#: I/O probe tests use): named ports, no vendor behaviour claimed.
IO_FIXTURE = """* TEST_FIXTURE: synthetic noninverting tri-state buffer
.subckt IO VCC A Y GND OE
Bdriver Y GND I=if(V(VCC,GND)>1,if(V(OE,GND)>1,(V(Y,GND)-V(A,GND))/25,0),0)
Rleak Y GND 1e12
Rinput A GND 1e12
.ends IO
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The run folder: a real directory with the model the decks include."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "model.lib").write_text("* model under author\n", encoding="utf-8", newline="\n")
    return run


def codes(verdict) -> list[str]:
    return [violation.code for violation in verdict.violations]


# --------------------------------------------------------------------------- #
# the clean case


def test_clean_deck_with_relative_include_passes(root: Path) -> None:
    text = "\n".join(
        [
            "* BoardModeler probe deck",
            ".include model.lib",
            "R1 a 0 1k",
            ".options plotwinsize=0",
            ".temp 25",
            ".tran 0 1e-3 0 1e-8",
            ".save V(a)",
            ".end",
            "",
        ]
    )
    verdict = check_deck(text, root=root)
    assert verdict.ok is True
    assert verdict.violations == ()


# --------------------------------------------------------------------------- #
# shell-escape directives


@pytest.mark.parametrize("line", ["! cmd", "!cmd", ".control", ".endc", ".exec", ".postrun"])
def test_shell_escape_directive_is_refused(root: Path, line: str) -> None:
    verdict = check_deck(f"* deck\n{line}\n.end\n", root=root)
    assert codes(verdict) == ["directive_shell_escape"]
    assert verdict.ok is False
    violation = verdict.violations[0]
    assert violation.line_no == 2
    assert violation.line == line


def test_shell_escape_refusal_does_not_depend_on_the_simulator(root: Path, tmp_path: Path) -> None:
    """The refusal is the policy's, not LTspice's.

    Measured on this machine (LTspice 26.0.0, ``LTspice.exe -b``): a deck with a
    ``!`` or ``.control`` line fails with ``Expected device instantiation or
    directive here.`` and runs no command. The policy refuses the same lines for
    defence in depth, and must not be a function of which simulator is installed
    — so the check needs no simulator, no run folder that exists, and refuses
    even a *clean* deck that only adds the one line.
    """
    clean = "* deck\n.include model.lib\nR1 a 0 1k\n.end\n"
    escaped = "* deck\n.include model.lib\nR1 a 0 1k\n! del /f /q C:\\Windows\\*.*\n.end\n"
    assert check_deck(clean, root=root).ok is True
    refused = check_deck(escaped, root=root)
    assert codes(refused) == ["directive_shell_escape"]

    absent = tmp_path / "never-created"
    assert not absent.exists()
    assert codes(check_deck("* deck\n.control\n", root=absent)) == ["directive_shell_escape"]


def test_simulator_executed_directives_are_refused_without_ltspice(root: Path) -> None:
    """The interesting half: paths LTspice *does* honour in batch mode."""
    text = "* deck\n.include C:\\Windows\\System32\\drivers\\etc\\hosts\n.end\n"
    verdict = check_deck(text, root=root)
    assert codes(verdict) == ["path_outside_root"]
    assert verdict.violations[0].line_no == 2


# --------------------------------------------------------------------------- #
# paths, by shape and by directive family


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (".include C:\\Windows\\System32\\drivers\\etc\\hosts", "path_outside_root"),
        ('.include "C:\\Windows\\System32\\drivers\\etc\\hosts"', "path_outside_root"),
        (".include \\\\evil\\share\\x.lib", "path_outside_root"),
        (".lib //evil/share/model.lib", "path_outside_root"),
        (".include ../../secrets.lib", "path_outside_root"),
        (".inc ../model.lib", "path_outside_root"),
        (".include %APPDATA%\\x.lib", "path_outside_root"),
        (".include $HOME/model.lib", "path_outside_root"),
        (".include C:x.lib", "path_outside_root"),
        (".include /etc/passwd", "path_outside_root"),
        ('.wave "D:\\out.wav"', "path_write_outside_root"),
        (".wave ../out.wav", "path_write_outside_root"),
        (".savebias C:\\x.txt", "path_write_outside_root"),
        (".savestate ..\\..\\x.state", "path_write_outside_root"),
        (".loadbias C:\\x.txt", "path_read_outside_root"),
        (".loadstate ..\\..\\x.state", "path_read_outside_root"),
        (".loadstate /x.state", "path_read_outside_root"),
        (".include http://evil/x.lib", "url_in_path"),
        (".include FTP://evil/x.lib", "url_in_path"),
        (".wave https://evil/x.wav", "url_in_path"),
    ],
)
def test_path_outside_the_run_folder_is_refused(root: Path, line: str, code: str) -> None:
    verdict = check_deck(f"* deck\n{line}\n.end\n", root=root)
    assert codes(verdict) == [code]
    violation = verdict.violations[0]
    assert violation.line_no == 2
    assert violation.line == line
    assert violation.detail  # the operator needs to be told what was wrong


def test_traversal_that_only_looks_inside_the_run_folder_is_refused(root: Path) -> None:
    """``a/../../b`` normalises outside; refusal is textual so it cannot vary."""
    verdict = check_deck(f"* deck\n.include a/../../{root.name}/model.lib\n.end\n", root=root)
    assert codes(verdict) == ["path_outside_root"]
    assert ".." in verdict.violations[0].detail


def test_missing_in_root_include_is_refused(root: Path) -> None:
    """Only a file the run itself created may be read."""
    verdict = check_deck("* deck\n.include not_written_yet.lib\n.end\n", root=root)
    assert codes(verdict) == ["model_path_reference"]
    assert "does not exist" in verdict.violations[0].detail


def test_model_under_author_may_be_included_before_it_exists(root: Path) -> None:
    pending = root / "pending.lib"
    assert not pending.exists()
    text = "* deck\n.include pending.lib\n.end\n"
    assert codes(check_deck(text, root=root)) == ["model_path_reference"]
    assert check_deck(text, root=root, model_path=pending).ok is True


def test_url_in_a_non_first_argument_is_refused(root: Path) -> None:
    verdict = check_deck(".lib model.lib https://evil/x.lib\n", root=root)
    assert codes(verdict) == ["url_in_path"]


def test_lib_with_a_section_checks_only_its_path_argument(root: Path) -> None:
    """``.lib <file> <section>``: the section names a block, not a second file."""
    assert check_deck(".lib model.lib BM_REG_BUCK\n", root=root).ok is True
    verdict = check_deck(".lib C:\\x.lib BM_REG_BUCK\n", root=root)
    assert codes(verdict) == ["path_outside_root"]
    assert "C:\\x.lib" in verdict.violations[0].detail


# --------------------------------------------------------------------------- #
# quoting, comments, and the tilde


def test_quoted_target_with_spaces_inside_the_run_folder_passes(root: Path) -> None:
    (root / "my file.lib").write_text("* quoted target\n", encoding="utf-8", newline="\n")
    assert check_deck('* deck\n.include "my file.lib"\n.end\n', root=root).ok is True


def test_single_quoted_target_is_accepted(root: Path) -> None:
    (root / "other file.lib").write_text("* quoted target\n", encoding="utf-8", newline="\n")
    assert check_deck("* deck\n.include 'other file.lib'\n.end\n", root=root).ok is True


def test_quoting_does_not_hide_an_outside_target(root: Path) -> None:
    verdict = check_deck('* deck\n.include "\\\\evil\\share\\x.lib"\n.end\n', root=root)
    assert codes(verdict) == ["path_outside_root"]


def test_trailing_comment_is_not_part_of_the_path(root: Path) -> None:
    """A comment that looks like a directive is a comment; the path still counts."""
    assert check_deck("* deck\n.include model.lib ; from the authoring run\n", root=root).ok is True
    assert check_deck("* deck\n.include model.lib ; ! rm -rf /\n", root=root).ok is True


def test_semicolon_inside_quotes_is_part_of_the_path(root: Path) -> None:
    (root / "a;b.lib").write_text("* odd but legal name\n", encoding="utf-8", newline="\n")
    assert check_deck('* deck\n.include "a;b.lib"\n', root=root).ok is True


def test_tilde_is_a_literal_directory_name(root: Path) -> None:
    """``~`` is not expanded, so it cannot escape; ``expanduser`` would be wrong."""
    home = root / "~"
    home.mkdir()
    (home / "x.lib").write_text("* literal tilde\n", encoding="utf-8", newline="\n")
    assert check_deck("* deck\n.include ~/x.lib\n", root=root).ok is True


def test_continuation_line_cannot_hide_a_target(root: Path) -> None:
    """``+`` joins, so ``.include`` and its target are one line to the policy too."""
    hidden = "* deck\n.include\n+ \\\\evil\\share\\x.lib\n.end\n"
    assert codes(check_deck(hidden, root=root)) == ["path_outside_root"]
    joined = check_deck("* deck\n.include\n+ model.lib\n.end\n", root=root)
    assert joined.ok is True


# --------------------------------------------------------------------------- #
# links


def _make_directory_link(link: Path, target: Path) -> str | None:
    """Create a directory link/junction; the reason string when it cannot."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return None
    except (OSError, NotImplementedError) as exc:
        first = f"os.symlink: {exc}"
    if os.name != "nt":
        return first
    # A junction needs no developer mode or elevation, which is why it is the
    # fallback rather than the reason to give up.
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and link.is_dir():
        return None
    said = (result.stdout + result.stderr).strip()
    return f"{first}; mklink /J: {said or result.returncode}"


def test_link_that_escapes_the_run_folder_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.lib").write_text("* outside the run folder\n", encoding="utf-8", newline="\n")
    link = root / "escape"
    reason = _make_directory_link(link, outside)
    if reason is not None:
        pytest.skip(
            f"this host will not create a directory link, so the escape cannot be tested: {reason}"
        )
    assert not (link / "evil.lib").resolve().is_relative_to(root.resolve())
    verdict = check_deck("* deck\n.include escape/evil.lib\n.end\n", root=root)
    assert codes(verdict) == ["path_outside_root"]


def test_link_that_stays_inside_the_run_folder_is_accepted(tmp_path: Path, root: Path) -> None:
    inside = root / "shared"
    inside.mkdir()
    (inside / "real.lib").write_text("* inside\n", encoding="utf-8", newline="\n")
    link = root / "alias"
    reason = _make_directory_link(link, inside)
    if reason is not None:
        pytest.skip(f"this host will not create a directory link: {reason}")
    assert check_deck("* deck\n.include alias/real.lib\n", root=root).ok is True


# --------------------------------------------------------------------------- #
# character garbage and line numbers


def test_long_line_is_refused(root: Path) -> None:
    at_limit = "R" + "a" * (MAX_LINE_CHARS - 1 - len(" 0 1k")) + " 0 1k"
    assert len(at_limit) == MAX_LINE_CHARS
    assert check_deck(f"* deck\n{at_limit}\n", root=root).ok is True
    over = ".include " + "a" * 5000 + ".lib\n"
    verdict = check_deck(f"* deck\n{over}\n", root=root)
    assert codes(verdict) == ["character_garbage"]
    assert verdict.violations[0].line_no == 2
    assert str(MAX_LINE_CHARS) in verdict.violations[0].detail


def test_nul_byte_is_refused(root: Path) -> None:
    verdict = check_deck("* deck\n.include model\x00.lib\n.end\n", root=root)
    assert codes(verdict) == ["character_garbage"]
    assert verdict.violations[0].line_no == 2
    assert "NUL" in verdict.violations[0].detail


def test_control_character_outside_ascii_is_refused(root: Path) -> None:
    """U+0085 is where a mis-decoded byte lands, and ``splitlines`` would eat it."""
    verdict = check_deck("* deck\n.include x\x85y.lib\n.end\n", root=root)
    assert codes(verdict) == ["character_garbage"]
    assert "U+0085" in verdict.violations[0].detail


def test_crlf_deck_reports_physical_line_numbers(root: Path) -> None:
    clean = "* deck\r\n.include model.lib\r\n.end\r\n"
    assert check_deck(clean, root=root).ok is True
    bad = "* deck\r\n.include model.lib\r\n.wave D:\\out.wav\r\n.end\r\n"
    verdict = check_deck(bad, root=root)
    assert codes(verdict) == ["path_write_outside_root"]
    assert verdict.violations[0].line_no == 3
    assert verdict.violations[0].line == ".wave D:\\out.wav"


def test_comment_lines_are_not_directives(root: Path) -> None:
    text = "* a comment naming C:\\Windows\\System32\\drivers\\etc\\hosts\n* ! and a command\n"
    assert check_deck(text, root=root).ok is True


# --------------------------------------------------------------------------- #
# the report shape


def test_every_violation_code_has_a_reachable_trigger(root: Path) -> None:
    decks = {
        "directive_shell_escape": "* deck\n! cmd\n",
        "path_outside_root": "* deck\n.include \\\\evil\\s\\x.lib\n",
        "path_write_outside_root": "* deck\n.wave D:\\out.wav\n",
        "path_read_outside_root": "* deck\n.loadstate D:\\x.state\n",
        "url_in_path": "* deck\n.include http://evil/x.lib\n",
        "model_path_reference": "* deck\n.include absent.lib\n",
        "character_garbage": "* deck\nR1 a b 1k \x00\n",
    }
    assert tuple(decks) == VIOLATION_CODES
    for expected, text in decks.items():
        assert codes(check_deck(text, root=root)) == [expected]


def test_verdict_reports_every_offending_line_in_deck_order(root: Path) -> None:
    text = "* deck\n.wave D:\\out.wav\n.include model.lib\n! cmd\n.include \\\\evil\\s\\x.lib\n"
    verdict = check_deck(text, root=root)
    assert verdict.ok is False
    assert codes(verdict) == [
        "path_write_outside_root",
        "directive_shell_escape",
        "path_outside_root",
    ]
    assert [violation.line_no for violation in verdict.violations] == [2, 4, 5]


def test_verdict_is_frozen(root: Path) -> None:
    verdict = check_deck("* deck\n! cmd\n", root=root)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.ok = True  # type: ignore[misc]


def test_safe_deck_summary_names_the_count_and_the_first_code(root: Path) -> None:
    verdict = check_deck(
        "* deck\n.wave D:\\out.wav\n! cmd\n.include \\\\evil\\s\\x.lib\n", root=root
    )
    summary = safe_deck_summary(verdict)
    assert summary.count("\n") == 0
    assert "3" in summary
    assert "path_write_outside_root" in summary
    assert safe_deck_summary(check_deck("* deck\n.include model.lib\n", root=root)) == (
        "deck policy ok: 0 violations"
    )


def test_safe_deck_summary_never_echoes_an_outside_path(root: Path) -> None:
    text = "* deck\n.include C:\\Windows\\System32\\drivers\\etc\\hosts\n.include \\\\evil\\share\\x.lib\n"
    verdict = check_deck(text, root=root)
    assert "System32" in verdict.violations[0].detail  # the report itself stays honest
    summary = safe_deck_summary(verdict)
    for fragment in ("C:\\", "System32", "hosts", "evil", "share", str(root)):
        assert fragment not in summary


# --------------------------------------------------------------------------- #
# integration: the app's own decks


def _staged(root: Path, name: str, text: str) -> Path:
    """Write a model into the run folder, the way the harness stages one."""
    path = root / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_the_apps_own_probe_deck_passes_unchanged(tmp_path: Path) -> None:
    """The real deck ``probes._deck`` renders, checked as the harness would.

    ``root`` is the authoring workdir, holding the model beside the run folders,
    which is the layout the harness stages — that is what makes the app's
    absolute ``.include`` a contained path rather than an escape.
    """
    model = write_regulator_library(tmp_path / "model.lib", [SUBCKT])
    spec = PROBES["vref"]
    deck = spec.render(model_lib=model, subckt=SUBCKT, params={})
    included = [line for line in deck.splitlines() if line.strip().startswith(".include")]
    assert included == [f".include {model.resolve().as_posix()}"]  # absolute, as designed
    verdict = check_deck(deck, root=tmp_path, model_path=model)
    assert verdict.violations == ()
    assert safe_deck_summary(verdict) == "deck policy ok: 0 violations"


def test_every_probe_deck_the_app_renders_passes_unchanged(tmp_path: Path) -> None:
    """All three probe families, against the fixtures the app ships for them.

    The model is staged inside the run folder first, which is what the harness
    does when it writes a candidate: the decks themselves then pass untouched.
    Measured on this machine: 11 regulator, 11 op-amp and 9 I/O decks, 31 in
    total; the floors below are lower on purpose, so adding a probe keeps this
    test green while losing a family does not.
    """
    fixtures = [
        (write_regulator_library(tmp_path / "regulator.lib", [SUBCKT]), SUBCKT),
        (
            _staged(
                tmp_path,
                "opamp.lib",
                (REPO / "fixtures" / "opamp" / "synthetic.lib").read_text(encoding="utf-8"),
            ),
            "ORACLE",
        ),
        (_staged(tmp_path, "io.lib", IO_FIXTURE), "IO"),
    ]
    rendered = 0
    for model, subckt in fixtures:
        family = 0
        for probe_id, spec in sorted(PROBES.items()):
            try:
                deck = spec.render(model_lib=model, subckt=subckt, params={})
            except ProbeError, KeyError, ValueError:
                # A probe that needs a port (or a recipe) this library does not
                # declare renders no deck at all; that is the registry's business.
                continue
            family += 1
            rendered += 1
            verdict = check_deck(deck, root=tmp_path, model_path=model)
            assert verdict.violations == (), (
                subckt,
                probe_id,
                [violation.detail for violation in verdict.violations],
            )
        assert family >= 5, (subckt, family)
    assert rendered >= 25


def test_the_apps_own_circuit_probe_deck_passes_unchanged(tmp_path: Path) -> None:
    """``circuit_probe`` quotes its absolute include; quoting must not matter."""
    model = write_regulator_library(tmp_path / "model.lib", [SUBCKT])
    deck = "\n".join(
        [
            "* Frozen fixture: soft-start shape",
            f'.include "{model.resolve().as_posix()}"',
            "V_vin vin 0 5",
            f"Xdut vin vout 0 {SUBCKT}",
            ".temp 25",
            ".options plotwinsize=0 numdgt=15",
            ".tran 0 1e-3 0 1e-8",
            ".save V(vout)",
            ".end",
            "",
        ]
    )
    assert check_deck(deck, root=tmp_path, model_path=model).violations == ()
