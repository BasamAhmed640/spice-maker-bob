"""Deck containment policy: what a candidate deck is allowed to name before it runs.

The threat model, in the owner's terms: the deck text is written by a rogue
agent, so the deck may name *any* path. An ``.include``/``.lib`` line reached by
a real LTspice process makes that file part of the simulation, and ``.wave``,
``.savebias``, ``.savestate`` and ``.loadstate`` name files the simulator writes
or reads. Nothing outside the run folder may be read or written by a run, so
every path a deck names is checked *before* the simulator is started.

One half of the question is already settled by measurement on this machine
(LTspice 26.0.0, ``LTspice.exe -b``): a netlist line whose first token is ``!``,
``.control``, ``.endc``, ``.exec`` or ``.postrun`` is *rejected* as a syntax
error — the run produced ``Expected device instantiation or directive here.``
and no command ran. Batch mode has no execution point for those directives, so
they are not the containment risk. They are still refused here
(:data:`~DeckViolation.code` ``directive_shell_escape``) as defence in depth: a
future LTspice, a different invocation (``-netlist``, a schematic tool) or a
wrapping tool must not be able to turn a line that is "harmless because the
simulator ignores it" into a shell command, and a refusal that depends on the
installed simulator's error wording is not a policy. The remaining risk is
paths, which LTspice *does* honour in batch mode.

What is refused, and why each shape is refused at the text level:

* ``\\\\host\\share\\x`` (UNC) and ``//host/share/x`` — resolving such a path is
  itself the network access: Windows authenticates to the remote host before any
  file is opened. This is refused from the text, before ``Path.resolve()`` is
  ever called on it, because the resolve would be the attack.
* ``C:x`` (drive-relative) — the drive LTspice resolves it against depends on
  the process's per-drive current directory, which no text check can know.
* ``%APPDATA%``, ``$HOME`` (environment expansion) — the expansion happens
  inside the simulator/toolchain, outside everything this check can see.
* ``..`` segments — refused even when they normalise back inside the run folder
  (``a/../../b``), so containment never depends on how a resolver treats them.
* ``http://``, ``https://``, ``ftp://`` — a URL in a path argument, refused by
  scheme (a URL-looking target would otherwise be treated as a local name).
* anything else that resolves outside ``root``, and an absolute path that
  resolves outside ``root``.
* a ``.include``/``.inc``/``.lib`` whose target is inside ``root`` but does not
  exist (``model_path_reference``) — only a file this run created may be read,
  and the model under author is the one exception, because the deck that
  characterises it is written before it exists.

An absolute path that resolves *inside* ``root`` is accepted. The app's own deck
builders write exactly that shape (``probes._deck``, ``circuit_probe.make_probe``,
``pin_roles``, ``sanity.load_check``, ``simulation.deck.Include.absolute``), an
absolute in-root path grants nothing a relative one would not, and refusing it
would only push the deck builders into inventing relative paths that break as
soon as the working directory changes.

``~`` is treated as a literal directory name, never expanded. LTspice does not
expand it either, so ``~/x.lib`` means ``<root>/~/x.lib``: it stays inside the
run folder, which is the safe direction, and expanding it here would reject a
deck over a filename the simulator would never have read as the home directory.

Directories the app's own rules already govern — the read-only LTspice library
under ``%LOCALAPPDATA%\\Programs\\ADI\\LTspice\\lib`` — are *not* inside
``root``, so a deck naming a stock library file is refused here; allowing that
directory stays the business of
:func:`boardmodeler.security.subprocess_guard.resolve_include_path`, the only
place that knows the LTspice library may be read. The app's own probe decks
never name it: they ``.include`` the model under author.

Contract
--------
* :func:`check_deck` is a *report*, never an exception. It returns every
  violation with its line number and its line so the harness can record an
  honest ``BLOCKED``/``UNKNOWN`` reason instead of a stack trace.
* Pure function: no I/O beyond ``Path.resolve()``/``Path.exists()``, no
  globals, no host state. The harness may call it on every candidate revision.
* ``root`` is the folder the run may touch — pass the authoring workdir, which
  holds both the staged model and the per-probe run folders. Passing only
  ``<workdir>/probes/<probe_id>`` would reject the harness's own absolute model
  include, because the model is staged beside the run folders, not inside them.
* One violation per offending line, from the first matching rule, so the count
  in :func:`safe_deck_summary` is stable; several violations mean several
  offending lines. Continuation lines (``+``) are joined the way LTspice joins
  them before any rule runs, so a target cannot be hidden on the next line.

Reading a violation list:

* ``code`` is the stable machine-readable rule name,
* ``line_no`` is the 1-based number of the first *physical* line of the
  offending logical line,
* ``line`` is that line's text, copied out of the deck (clipped at
  :data:`_CLIP_CHARS` characters, marked when clipped),
* ``detail`` says what was wrong, including the resolved path when one could be
  built, so the operator can act on it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "MAX_LINE_CHARS",
    "VIOLATION_CODES",
    "DeckVerdict",
    "DeckViolation",
    "ViolationCode",
    "check_deck",
    "safe_deck_summary",
]

#: Stable rule names. A caller records these in reports and a model card, so
#: they are part of the contract and only ever get added to.
ViolationCode = Literal[
    "directive_shell_escape",
    "path_outside_root",
    "path_write_outside_root",
    "path_read_outside_root",
    "url_in_path",
    "model_path_reference",
    "character_garbage",
]

VIOLATION_CODES: tuple[ViolationCode, ...] = (
    "directive_shell_escape",
    "path_outside_root",
    "path_write_outside_root",
    "path_read_outside_root",
    "url_in_path",
    "model_path_reference",
    "character_garbage",
)

#: The longest single line a deck may contain. A deck is interpreted line by
#: line; a 10 MB single-line deck is a denial of service against the parser
#: (and against every reader of the deck in the report) while claiming to be one
#: card, so the length is refused rather than measured later.
MAX_LINE_CHARS = 4096

#: Directive head tokens that name a simulator-side execution point. ``!`` is a
#: shell command in the LTspice GUI; the dotted forms are the batch/control-mode
#: directives. All are rejected in batch mode (see the module docstring) and all
#: are refused here anyway.
_SHELL_DIRECTIVES = frozenset({"!", ".control", ".endc", ".exec", ".postrun"})

#: Directive head -> (code when the target escapes the run folder, whether the
#: target must already exist). ``.include``/``.inc``/``.lib`` read a file that
#: must have been created inside the run folder; ``.wave``/``.savebias``/
#: ``.savestate`` write one; ``.loadbias``/``.loadstate`` read one the run
#: wrote earlier. Only the include family needs the existence rule: a bias or
#: state file is written by an earlier run in the same folder.
_PATH_DIRECTIVES: dict[str, tuple[ViolationCode, bool]] = {
    ".include": ("path_outside_root", True),
    ".inc": ("path_outside_root", True),
    ".lib": ("path_outside_root", True),
    ".wave": ("path_write_outside_root", False),
    ".savebias": ("path_write_outside_root", False),
    ".savestate": ("path_write_outside_root", False),
    ".loadbias": ("path_read_outside_root", False),
    ".loadstate": ("path_read_outside_root", False),
}

#: A URL scheme in a path argument. Matched in the text because a URL-looking
#: target is *not* absolute for :class:`~pathlib.Path`, so a resolve would place
#: ``http://evil/x.lib`` neatly inside the run folder and call it contained.
_URL_SCHEME = re.compile(r"(?i)\b(?:https?|ftp)://")

_DRIVE_LETTER = re.compile(r"[A-Za-z]:")

#: ``%VAR%`` or ``$VAR``/``${VAR}``: expanded by cmd.exe, PowerShell or the
#: simulator, never by this check.
_ENVIRONMENT_EXPANSION = re.compile(r"%[^%\s]*%|\$\{|\$[A-Za-z_]")

#: Inline comment in SPICE text. Only honoured outside quotes, so a path that
#: contains a semicolon survives.
_COMMENT_CHAR = ";"

_QUOTE_CHARS = "\"'"

#: SPICE continuation. Joined before the rules run, so `.include` on one line
#: and its target on the next is one line to the policy, exactly as it is to the
#: simulator.
_CONTINUATION_CHAR = "+"

#: Physical line separators. Deliberately not ``str.splitlines()``: that also
#: splits on ``\\v``, ``\\f`` and the C1 range (U+0085 and friends), which would
#: silently swallow control characters this policy exists to refuse.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

#: How much of an offending line a violation copies out of the deck.
_CLIP_CHARS = 300

_PATH_SEPARATORS = re.compile(r"[\\/]")


@dataclass(frozen=True)
class DeckViolation:
    """One refused deck line.

    ``code`` is the rule, ``line_no``/``line`` locate the offending line in the
    deck the caller handed in, and ``detail`` says what was wrong in the terms
    the operator needs (the resolved path when one could be built).
    """

    code: ViolationCode
    line_no: int
    line: str
    detail: str


@dataclass(frozen=True)
class DeckVerdict:
    """The policy's report for one deck revision.

    ``ok`` is True exactly when ``violations`` is empty; ``violations`` is in
    deck order, so ``violations[0]`` is the first thing to fix.
    """

    ok: bool
    violations: tuple[DeckViolation, ...]


@dataclass(frozen=True)
class _Token:
    """One whitespace-separated token, with the quoting it arrived in."""

    raw: str
    value: str
    quoted: bool


@dataclass(frozen=True)
class _Logical:
    """One logical deck line: continuations joined, garbage noted."""

    line_no: int
    text: str
    garbage: DeckViolation | None


def _clip(text: str) -> str:
    """Bound what a report copies out of the deck."""
    return text if len(text) <= _CLIP_CHARS else text[:_CLIP_CHARS] + " (truncated)"


def _tokens(line: str) -> list[_Token]:
    """Tokens of a directive line, stopping at an unquoted ``;`` comment.

    Not ``line.split()``: a quoted target may contain spaces and a semicolon,
    and a comment has to be ignored only when it is outside the quotes.
    """
    out: list[_Token] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char.isspace():
            index += 1
            continue
        if char == _COMMENT_CHAR:
            break
        if char in _QUOTE_CHARS:
            end = line.find(char, index + 1)
            if end < 0:  # unterminated quote: keep the rest as the value
                out.append(_Token(line[index:], line[index + 1 :], True))
                break
            out.append(_Token(line[index : end + 1], line[index + 1 : end], True))
            index = end + 1
            continue
        end = index
        while end < length and not line[end].isspace() and line[end] != _COMMENT_CHAR:
            end += 1
        out.append(_Token(line[index:end], line[index:end], False))
        index = end
    return out


def _garbage(line_no: int, line: str) -> DeckViolation | None:
    """Refuse a line the parser must not be handed (control bytes, huge length)."""
    problems: list[str] = []
    controls = sorted(
        {char for char in line if unicodedata.category(char) == "Cc" and char != "\t"}
    )
    if controls:
        named = ", ".join("NUL" if char == "\x00" else f"U+{ord(char):04X}" for char in controls)
        problems.append(f"control character(s) {named}")
    if len(line) > MAX_LINE_CHARS:
        problems.append(f"{len(line)} characters on one line (limit {MAX_LINE_CHARS})")
    if not problems:
        return None
    return DeckViolation(
        code="character_garbage",
        line_no=line_no,
        line=_clip(line),
        detail="; ".join(problems) + "; a deck is read line by line and is never worth a parser",
    )


def _logical_lines(text: str) -> list[_Logical]:
    """The deck's logical lines in file order, with 1-based physical numbers."""
    out: list[_Logical] = []
    for line_no, physical in enumerate(_LINE_BREAK.split(text), start=1):
        garbage = _garbage(line_no, physical)
        stripped = physical.strip()
        if out and stripped.startswith(_CONTINUATION_CHAR):
            previous = out[-1]
            joined = f"{previous.text} {stripped[1:].strip()}"
            out[-1] = _Logical(previous.line_no, joined, previous.garbage or garbage)
            continue
        out.append(_Logical(line_no, physical, garbage))
    return out


def _violation(code: ViolationCode, line_no: int, line: str, detail: str) -> DeckViolation:
    return DeckViolation(code=code, line_no=line_no, line=_clip(line), detail=detail)


def _absolute_by_text(target: str) -> bool:
    """Whether LTspice would treat ``target`` as rooted, drives included.

    ``Path("/etc/passwd").is_absolute()`` is False on Windows, and
    ``Path("C:/x").is_absolute()`` is True, so this is decided from the text on
    purpose: a driveless rooted target must not be joined to ``root``.
    """
    if target.startswith(("\\", "/")):
        return True
    return bool(_DRIVE_LETTER.match(target)) and len(target) > 2 and target[2] in "\\/"


def _path_detail(
    target: str,
    directive: str,
    *,
    code: ViolationCode,
    root: Path,
    model: Path | None,
    must_exist: bool,
) -> tuple[ViolationCode, str] | None:
    """``(code, why)`` for an unacceptable ``target``, or ``None`` when it is fine.

    The code is the caller's (the directive family's escape code) except for the
    existence rule, which reports :data:`~DeckViolation.code`
    ``model_path_reference``: a missing file inside the run folder is not an
    escape, it is a read of something the run never wrote.

    Order matters: UNC and the other text-level shapes are refused before any
    ``resolve()``, because for a UNC path the resolve is the network access.
    """
    if not target:
        # Nothing to resolve; the simulator's own syntax error covers it.
        return None
    if target.startswith(("\\\\", "//")):
        return code, (
            f"{directive} target {target!r} is a UNC or device path; LTspice would make "
            "Windows authenticate to a remote host before opening any file"
        )
    if _DRIVE_LETTER.match(target) and not _absolute_by_text(target):
        return code, (
            f"{directive} target {target!r} is drive-relative; the drive LTspice uses "
            "depends on the process's per-drive directory"
        )
    if _ENVIRONMENT_EXPANSION.search(target):
        return code, (
            f"{directive} target {target!r} expands an environment variable, which happens "
            "inside the simulator and outside this check"
        )
    if ".." in _PATH_SEPARATORS.split(target):
        return code, (
            f"{directive} target {target!r} contains a '..' segment, refused even when it "
            f"normalises back inside {root}"
        )
    joined = Path(target) if _absolute_by_text(target) else root / target
    try:
        resolved = joined.resolve()
    except (OSError, ValueError) as exc:
        return code, (
            f"{directive} target {target!r} could not be resolved ({type(exc).__name__}: {exc})"
        )
    if not resolved.is_relative_to(root):
        return code, (
            f"{directive} target {target!r} resolves to {resolved}, outside the run folder {root}"
        )
    if must_exist and resolved != model and not resolved.exists():
        return "model_path_reference", (
            f"{directive} target {target!r} resolves to {resolved}, which does not exist inside "
            f"{root}; only a file this run created may be read (the model under author is exempt)"
        )
    return None


def _check_line(line_no: int, line: str, *, root: Path, model: Path | None) -> DeckViolation | None:
    """The first rule ``line`` breaks, or ``None``."""
    tokens = _tokens(line)
    if not tokens:
        return None  # blank, or a line that is only a comment
    head = tokens[0].value.lower()
    if head in _SHELL_DIRECTIVES or head.startswith("!"):
        return _violation(
            "directive_shell_escape",
            line_no,
            line,
            f"{head!r} names a simulator execution point; batch mode rejects it "
            "('Expected device instantiation or directive here.') and this policy refuses it "
            "without relying on that",
        )
    rule = _PATH_DIRECTIVES.get(head)
    if rule is None or len(tokens) < 2:
        return None
    code, must_exist = rule
    for token in tokens[1:]:
        found = _URL_SCHEME.search(token.value)
        if found is not None:
            return _violation(
                "url_in_path",
                line_no,
                line,
                f"{head} argument {token.value!r} is a URL ({found.group(0)}); a deck reaches "
                "the network through no argument at all",
            )
    problem = _path_detail(
        tokens[1].value, head, code=code, root=root, model=model, must_exist=must_exist
    )
    if problem is None:
        return None
    found_code, detail = problem
    return _violation(found_code, line_no, line, detail)


def check_deck(text: str, *, root: Path, model_path: Path | None = None) -> DeckVerdict:
    """Report every line of ``text`` that names something outside ``root``.

    ``root`` is the run folder the deck may read and write inside. ``model_path``
    is the model under author: it may be ``.include``\\ d before it exists, so it
    is exempt from the existence rule (a path that escapes ``root`` is still
    refused — the model is staged inside the run folder).

    Never raises for a bad deck: the returned :class:`DeckVerdict` carries the
    violations, so the caller can decide between refusing the run and reporting
    it. Text-level rules run first (see the module docstring), then containment,
    then existence, and each offending line yields exactly one violation.
    """
    root_path = Path(root).resolve()
    model = Path(model_path).resolve() if model_path is not None else None
    violations: list[DeckViolation] = []
    for logical in _logical_lines(text):
        if logical.garbage is not None:
            # A line the parser must not see is refused on its own terms; the
            # path rules are not asked to read it.
            violations.append(logical.garbage)
            continue
        found = _check_line(logical.line_no, logical.text, root=root_path, model=model)
        if found is not None:
            violations.append(found)
    return DeckVerdict(ok=not violations, violations=tuple(violations))


def safe_deck_summary(verdict: DeckVerdict) -> str:
    """One line for the log and the model card: the count and the first code.

    The offending line and target are deliberately not echoed. A rogue deck's
    target is a machine path (``\\\\host\\share``, ``%APPDATA%``, a user name),
    and this string is what the log record and the model card keep forever; the
    full report stays with the harness, which prints it to the operator who
    asked for the run.
    """
    if verdict.ok:
        return "deck policy ok: 0 violations"
    first = verdict.violations[0]
    return (
        f"deck policy refused: {len(verdict.violations)} violation(s), "
        f"first {first.code} at line {first.line_no}"
    )
