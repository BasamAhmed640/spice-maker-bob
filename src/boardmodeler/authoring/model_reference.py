"""Make a standalone, ground-referenced subcircuit use its declared ground pin.

This local rewrite preserves the zero-ground circuit. The simulator must judge
the rewritten bytes again. Nested cells are left alone unless each declares GND.
"""

from __future__ import annotations

import re
from pathlib import Path


def normalize_ground_reference(path: Path, pin_map, evidence: Path, *, spec=None) -> bool:
    from boardmodeler.authoring.model_syntax import (
        add_regulator_operating_hint,
        archive_original,
        normalize_behavioral_sources,
        normalize_library_end,
        write_library,
    )

    syntax_changed = normalize_library_end(path, evidence / "syntax")
    syntax_changed = (
        normalize_behavioral_sources(path, evidence / "behavioral-sources") or syntax_changed
    )
    syntax_changed = (
        add_regulator_operating_hint(path, spec, evidence / "operating-point") or syntax_changed
    )
    if not path.is_file() or not any(
        str(p.get("mapped_symbol_pin") or p.get("name", "")).upper() == "GND"
        and re.search(r"\b(?:ground|gnd)\b", p.get("function", ""), re.I)
        for p in pin_map
    ):
        return syntax_changed
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    declarations = re.findall(r"(?im)^\s*\.subckt\s+\S+\s+([^\r\n]+)", original)
    if not declarations or any("GND" not in d.upper().split() for d in declarations):
        return syntax_changed
    lines = []
    for line in original.splitlines():
        if not line.strip() or line.lstrip().startswith("*"):
            lines.append(line)
            continue

        def voltage(match):
            nodes = ["GND" if n.strip() == "0" else n.strip() for n in match[1].split(",")]
            if len(nodes) == 1:
                nodes.append("GND")
            return "V(" + ",".join(nodes) + ")"

        line = re.sub(
            r"\bV\(\s*([A-Za-z0-9_]+(?:\s*,\s*[A-Za-z0-9_]+)?)\s*\)", voltage, line, flags=re.I
        )
        tokens = line.split()
        arity = {
            "R": 2,
            "C": 2,
            "L": 2,
            "V": 2,
            "I": 2,
            "B": 2,
            "E": 4,
            "G": 4,
            "F": 2,
            "H": 2,
            "D": 2,
            "M": 4,
            "Q": 3,
        }
        count = arity.get(tokens[0][0].upper(), 0)
        # E/G behavioural forms have only two nodes before VALUE/TABLE/POLY.
        if (
            count == 4
            and tokens[0][0].upper() in "EG"
            and len(tokens) > 3
            and re.match(r"(?i)(?:value|table|poly|laplace)", tokens[3])
        ):
            count = 2
        if count:
            for i in range(1, min(count + 1, len(tokens))):
                if tokens[i] == "0":
                    tokens[i] = "GND"
            line = " ".join(tokens)
        lines.append(line)
    newline = "\r\n" if "\r\n" in original else "\n"
    result = newline.join(lines) + newline
    if result == original:
        return syntax_changed
    archive_original(evidence, original_bytes)
    write_library(path, result)
    return True
