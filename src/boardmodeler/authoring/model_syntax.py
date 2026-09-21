"""Cheap structural checks before starting a simulator process."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def archive_original(evidence: Path, original: bytes) -> None:
    """Keep the exact bytes that were replaced, named by their own sha256.

    Archives are evidence, so they are written as bytes: text mode would translate
    newlines and store something other than what was replaced.
    """
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / (hashlib.sha256(original).hexdigest() + ".lib")).write_bytes(original)


def write_library(path: Path, text: str) -> None:
    """Rewrite a repaired library without re-encoding bytes it did not touch.

    ``Path.write_text`` translates ``\\n`` to ``os.linesep``, so on Windows a one-line
    repair rewrote every terminator in the file, and the archive held CRLF bytes that were
    not the bytes it replaced. The pipeline hashes model bytes and ``.gitattributes`` marks
    ``*.lib`` binary, so a repair must change only what it repairs. ``text`` keeps the
    endings that were read from the file, because it was decoded from bytes rather than
    opened in text mode.
    """
    path.write_bytes(text.encode("utf-8"))


def normalize_library_end(path: Path, evidence: Path) -> bool:
    """Repair a final .end used to close the sole unclosed subcircuit.

    This is a syntax correction, not a change to component values. Preserve the
    original and require a new simulation of the resulting library.
    """
    if not path.is_file():
        return False
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    lines = original.splitlines()
    meaningful = [
        i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("*")
    ]
    if not meaningful or lines[meaningful[-1]].strip().lower() != ".end":
        return False
    opened = []
    for line in lines[: meaningful[-1]]:
        words = line.split()
        if not words:
            continue
        if words[0].lower() == ".subckt" and len(words) > 1:
            opened.append(words[1])
        elif words[0].lower() == ".ends" and opened:
            opened.pop()
    if len(opened) != 1:
        return False
    newline = "\r\n" if "\r\n" in original else "\n"
    lines[meaningful[-1]] = ".ends " + opened[0]
    archive_original(evidence, original_bytes)
    write_library(path, newline.join(lines) + newline)
    return True


def normalize_behavioral_sources(path: Path, evidence: Path) -> bool:
    """Repair E/G sources and their current references within the owning subcircuit.

    Component names are local to each subcircuit, including nested declarations.
    Collect names before rewriting so forward references and collisions are handled
    without changing unrelated scopes, comments, whitespace or line endings.
    """
    if not path.is_file():
        return False
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    source = re.compile(r"(?i)^[ \t]*([EG]\S*)[ \t]+\S+[ \t]+\S+[ \t]+([IV])[ \t]*=")
    lines = original.splitlines(keepends=True)
    scopes: list[int] = []
    stack = [0]
    names: list[dict[str, int]] = [{}]
    for line in lines:
        fields = line.partition(";")[0].split()
        first = fields[0].upper() if fields else ""
        if first == ".SUBCKT":
            stack.append(len(names))
            names.append({})
        scopes.append(stack[-1])
        if first and first[0].isalpha():
            counts = names[stack[-1]]
            counts[first] = counts.get(first, 0) + 1
        if first == ".ENDS":
            if len(stack) == 1:
                return False  # Let the validator reject malformed scope boundaries.
            stack.pop()
    if len(stack) != 1:
        return False

    replacements: list[dict[str, str]] = [{} for _ in names]
    for index, line in enumerate(lines):
        code, separator, comment = line.partition(";")
        match = source.match(code)
        if match is None:
            continue
        old, quantity = match[1], match[2]
        if (old[0].upper(), quantity.upper()) not in {("G", "I"), ("E", "V")}:
            continue
        scope = scopes[index]
        if names[scope][old.upper()] != 1:
            continue  # Do not hide an existing duplicate by giving it another name.
        new = "B_" + old
        while new.upper() in names[scope]:
            new += "_FIX"
        names[scope][new.upper()] = 1
        replacements[scope][old.upper()] = new
        lines[index] = code[: match.start(1)] + new + code[match.end(1) :] + separator + comment

    current = re.compile(r"(?i)\bI[ \t]*\([ \t]*([^()\s]+)[ \t]*\)")
    for index, line in enumerate(lines):
        if line.lstrip().lower().startswith(("*", ";", ".subckt", ".ends")):
            continue
        local = replacements[scopes[index]]
        code, separator, comment = line.partition(";")

        def reference(match, local=local):
            new = local.get(match[1].upper())
            if new is None:
                return match[0]
            start, end = match.start(1) - match.start(), match.end(1) - match.start()
            return match[0][:start] + new + match[0][end:]

        lines[index] = current.sub(reference, code) + separator + comment
    text = "".join(lines)
    if text == original:
        return False
    archive_original(evidence, original_bytes)
    write_library(path, text)
    return True


def validate_library(path: Path) -> None:
    from boardmodeler.authoring.probes import ProbeError

    stack = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        words = line.strip().split()
        if not words or words[0].startswith(("*", "+", ";")):
            continue
        first = words[0].lower()
        if re.match(r"(?i)^[EG]\S*\s+\S+\s+\S+\s+[IV]\s*=", line.strip()):
            raise ProbeError(
                "model_syntax_invalid", f"behavioral I=/V= requires a B source at line {number}"
            )
        if first == ".subckt":
            if len(words) < 3:
                raise ProbeError("model_syntax_invalid", f"incomplete .subckt at line {number}")
            stack.append((words[1].lower(), set()))
        elif first == ".ends":
            if not stack or (len(words) > 1 and words[1].lower() != stack[-1][0]):
                raise ProbeError("model_syntax_invalid", f"unmatched .ends at line {number}")
            stack.pop()
        elif first == ".end" and stack:
            raise ProbeError("model_syntax_invalid", f".end inside {stack[-1][0]}: use .ends")
        elif stack and re.match(r"^[a-z]", first):
            if first in stack[-1][1]:
                raise ProbeError(
                    "model_syntax_invalid",
                    f"duplicate component {words[0]} in {stack[-1][0]} at line {number}",
                )
            stack[-1][1].add(first)
    if stack:
        raise ProbeError("model_syntax_invalid", f"missing .ends for {stack[-1][0]}")


def add_regulator_operating_hint(path: Path, spec, evidence: Path) -> bool:
    """Seed the DC solver from the documented regulated output, not from zero.

    NODESET is an initial guess, not a voltage constraint or a passing verdict.
    This avoids the nonphysical negative equilibrium of some foldback macromodels.
    Only a single regulated output with one unambiguous nominal fixture value is eligible.
    """
    from boardmodeler.authoring.pin_roles import terminal_name

    if spec is None or not path.is_file():
        return False
    outputs = [
        terminal_name(p)
        for p in spec.pin_map
        if re.search(r"regulated output", p.get("function", ""), re.I)
    ]
    values = {
        float(value)
        for char in spec.covered()
        for name, value in (char.probe_recipe or {}).get("operating_point", {}).items()
        if re.sub(r"[^a-z]", "", name.lower()) in ("voutnom", "voutnominal")
        and isinstance(value, (int, float))
        and 0 < value < 1000
    }
    if len(outputs) != 1 or len(values) != 1:
        return False
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    if len(re.findall(r"(?im)^\s*\.subckt\b", original)) != 1 or re.search(
        r"(?im)^\s*\.nodeset\b", original
    ):
        return False
    hint = f"* Documented output seeds DC iteration; it does not force the solution.\n.nodeset V({outputs[0]})={values.pop():g}\n"
    updated, count = re.subn(r"(?im)^(\s*\.ends\b)", lambda m: hint + m[1], original)
    if count != 1:
        return False
    archive_original(evidence, original_bytes)
    newline = "\r\n" if "\r\n" in original else "\n"
    write_library(path, updated.replace("\r\n", "\n").replace("\n", newline))
    return True
