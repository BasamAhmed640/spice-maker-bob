"""Cheap structural checks before starting a simulator process."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def normalize_library_end(path: Path, evidence: Path) -> bool:
    """Repair a final .end used to close the sole unclosed subcircuit.

    This is a syntax correction, not a change to component values. Preserve the
    original and require a new simulation of the resulting library.
    """
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
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
    lines[meaningful[-1]] = ".ends " + opened[0]
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / (hashlib.sha256(original.encode()).hexdigest() + ".lib")).write_text(
        original, encoding="utf-8"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def validate_library(path: Path) -> None:
    from boardmodeler.authoring.probes import ProbeError

    stack = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        words = line.strip().split()
        if not words or words[0].startswith(("*", "+", ";")):
            continue
        first = words[0].lower()
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
    original = path.read_text(encoding="utf-8")
    if len(re.findall(r"(?im)^\s*\.subckt\b", original)) != 1 or re.search(
        r"(?im)^\s*\.nodeset\b", original
    ):
        return False
    hint = f"* Documented output seeds DC iteration; it does not force the solution.\n.nodeset V({outputs[0]})={values.pop():g}\n"
    updated, count = re.subn(r"(?im)^(\s*\.ends\b)", lambda m: hint + m[1], original)
    if count != 1:
        return False
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / (hashlib.sha256(original.encode()).hexdigest() + ".lib")).write_text(
        original, encoding="utf-8"
    )
    path.write_text(updated, encoding="utf-8")
    return True
