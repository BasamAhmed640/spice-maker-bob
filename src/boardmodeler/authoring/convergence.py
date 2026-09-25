"""Static convergence checks and simulator-log diagnosis for authored models.

Two failure classes cost whole authoring turns without telling the author what to
change: a model LTspice cannot parse (a bare internal node name inside a behavioural
expression is read as an undefined ``.param``) and a model with no operating point
(a behavioural source that reads its own output, a state node whose only path to
ground is a terabyte resistor, a floating-permitted input with no internal bias).

:func:`lint_library` finds those structures before any simulation, and
:func:`diagnose_log` turns an LTspice log into the element lines the repair has to
touch. Both only *describe* a model; neither edits it, relaxes a limit or decides a
verdict — the harness remains the only source of PASS or FAIL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Findings the author must fix before a simulation can say anything useful.
ERROR = "error"
#: Structures that often break convergence but can be valid.
WARNING = "warning"

#: Resistances at or above this value are not a usable DC path for the solver.
MAX_DC_PATH_OHMS = 1e10

_GROUND_NAMES = {"0", "gnd", "agnd", "pgnd", "vss", "ground", "gnd!", "powerpad", "ep", "pad"}
_SUPPLY_PREFIXES = ("vin", "vcc", "vdd", "v+", "vs", "pvin", "avdd", "vbat", "vsup", "vp")

#: Functions and constants LTspice accepts inside B-source expressions.
_KNOWN_WORDS = {
    "abs",
    "acos",
    "acosh",
    "arccos",
    "arccosh",
    "arcsin",
    "arcsinh",
    "arctan",
    "arctanh",
    "asin",
    "asinh",
    "atan",
    "atan2",
    "atanh",
    "buf",
    "cbrt",
    "ceil",
    "cos",
    "cosh",
    "ddt",
    "delay",
    "exp",
    "floor",
    "hypot",
    "idt",
    "idtmod",
    "if",
    "int",
    "inv",
    "limit",
    "ln",
    "log",
    "log10",
    "max",
    "min",
    "pow",
    "pwr",
    "pwrs",
    "rand",
    "random",
    "round",
    "sdt",
    "sgn",
    "sign",
    "sin",
    "sinh",
    "smooth",
    "sqrt",
    "table",
    "tan",
    "tanh",
    "u",
    "uramp",
    "white",
    "time",
    "temp",
    "pi",
    "e",
    "k",
    "q",
    "true",
    "false",
    "v",
    "i",
    "freq",
    "w",
    "s",
    "x",
    "gauss",
    "flat",
    "mc",
    "hertz",
    "tripdv",
    "tripdt",
    "laplace",
    "nfft",
    "windowtype",
    "ic",
    "tc1",
    "tc2",
    "rpar",
    "cpar",
    "noiseless",
    "vprxy",
}

_SCALE = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "µ": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}
_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmuµnpf])?", re.I)
_IDENT = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)(?![\w.]*\s*\()")
_VREF = re.compile(r"\bV\s*\(\s*([^,()\s]+)\s*(?:,\s*([^,()\s]+)\s*)?\)", re.I)


@dataclass(frozen=True)
class LintFinding:
    """One structure in the model text that the repair should address."""

    severity: str
    code: str
    line: int
    element: str
    message: str

    def text(self) -> str:
        where = f"line {self.line}" + (f" `{self.element}`" if self.element else "")
        return f"[{self.severity}:{self.code}] {where}: {self.message}"


@dataclass
class _Element:
    name: str
    line: int
    nodes: tuple[str, ...]
    text: str


def parse_value(token: str) -> float | None:
    """A SPICE number with its scale suffix, or ``None`` for an expression."""
    match = _NUMBER.match(token.strip().lower())
    if match is None:
        return None
    value = float(match[1])
    suffix = (match[2] or "").lower()
    return value * _SCALE.get(suffix, 1.0)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Joined continuation lines with their first physical line number; comments dropped."""
    joined: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        code = raw.split(";", 1)[0].rstrip()
        stripped = code.strip()
        if not stripped or stripped.startswith("*"):
            continue
        if stripped.startswith("+") and joined:
            first, previous = joined[-1]
            joined[-1] = (first, previous + " " + stripped[1:].strip())
            continue
        joined.append((number, stripped))
    return joined


def _expression(text: str) -> str:
    """The behavioural expression of a B/E/G line (after ``V=``/``I=``), or ``""``."""
    match = re.search(r"(?i)\b[VI]\s*=\s*(.*)$", text)
    return match[1] if match else ""


def _subckt_blocks(text: str) -> list[tuple[list[str], list[tuple[int, str]], set[str]]]:
    """(ports, body lines, parameter names) for each top-level ``.subckt``."""
    blocks: list[tuple[list[str], list[tuple[int, str]], set[str]]] = []
    current: tuple[list[str], list[tuple[int, str]], set[str]] | None = None
    for number, line in _logical_lines(text):
        words = line.split()
        head = words[0].lower()
        if head == ".subckt" and current is None:
            ports = [w for w in words[2:] if "=" not in w and w.lower() not in {"params:"}]
            params = {w.split("=", 1)[0].lower() for w in words[2:] if "=" in w}
            current = (ports, [], params)
            continue
        if head == ".ends" and current is not None:
            blocks.append(current)
            current = None
            continue
        if current is not None:
            if head == ".param":
                for assignment in re.finditer(r"([A-Za-z_]\w*)\s*=", line[6:]):
                    current[2].add(assignment[1].lower())
            current[1].append((number, line))
    if current is not None:
        blocks.append(current)
    return blocks


def _node_count(name: str) -> int:
    """How many node fields an element of this type has before its value/model."""
    kind = name[0].upper()
    return {
        "R": 2,
        "C": 2,
        "L": 2,
        "V": 2,
        "I": 2,
        "B": 2,
        "D": 2,
        "E": 4,
        "G": 4,
        "F": 2,
        "H": 2,
        "S": 4,
        "W": 2,
        "M": 4,
        "J": 3,
        "Z": 3,
        "T": 4,
    }.get(kind, 0)


def _elements(body: list[tuple[int, str]]) -> list[_Element]:
    elements: list[_Element] = []
    for number, line in body:
        words = line.split()
        name = words[0]
        if name.startswith("."):
            continue
        kind = name[0].upper()
        if kind == "X":
            nodes = tuple(w for w in words[1:-1] if "=" not in w and w.lower() != "params:")
        elif kind == "Q":
            nodes = tuple(words[1:4])
        elif kind == "A":
            nodes = tuple(words[1:9])
        else:
            nodes = tuple(words[1 : 1 + _node_count(name)])
        elements.append(_Element(name=name, line=number, nodes=nodes, text=line))
    return elements


#: LTspice's special-function (A-device) types, as the model keyword after the 8 nodes.
_A_DEVICE_TYPES = frozenset(
    {
        "and",
        "or",
        "xor",
        "inv",
        "buf",
        "dflop",
        "srflop",
        "schmitt",
        "schmtbuf",
        "schmtinv",
        "diffschmitt",
        "diffschmtbuf",
        "diffschmtinv",
        "phidet",
        "samplehold",
        "counter",
        "modulate",
        "modulate2",
        "varistor",
        "ota",
        "dlatch",
        "srlatch",
    }
)


def _a_device_arity(line: str) -> tuple[int, str] | None:
    """``(nodes before the type keyword, keyword)`` for an A-device line, if it names one."""
    words = line.split()
    for index, word in enumerate(words[1:], start=1):
        if word.lower() in _A_DEVICE_TYPES:
            return index - 1, word
    return None


def _is_ground(node: str) -> bool:
    return node.lower() in _GROUND_NAMES


def _dc_links(element: _Element) -> list[tuple[str, str]]:
    """Node pairs this element connects with a finite DC conductance or a driven voltage."""
    kind = element.name[0].upper()
    words = element.text.split()
    nodes = element.nodes
    if kind == "R" and len(nodes) == 2:
        value = parse_value(words[3]) if len(words) > 3 else None
        if value is not None and value >= MAX_DC_PATH_OHMS:
            return []
        return [(nodes[0], nodes[1])]
    if kind in {"L", "V", "D", "H"} and len(nodes) == 2:
        return [(nodes[0], nodes[1])]
    if kind == "E" and len(nodes) >= 2:
        return [(nodes[0], nodes[1])]
    if kind == "B" and len(nodes) == 2 and re.search(r"(?i)\bV\s*=", element.text):
        return [(nodes[0], nodes[1])]
    if kind in {"S", "W"} and len(nodes) >= 2:
        return [(nodes[0], nodes[1])]
    if kind in {"M", "J", "Z"} and len(nodes) >= 3:
        return [(nodes[0], nodes[2]), (nodes[1], nodes[2])]
    if kind == "Q" and len(nodes) == 3:
        return [(nodes[0], nodes[2]), (nodes[1], nodes[2])]
    if kind == "X" and len(nodes) >= 2:
        return [(nodes[0], other) for other in nodes[1:]]
    if kind == "A" and len(nodes) >= 8:
        # A-device outputs are driven; tie the output pins to the device's common node.
        common = nodes[5] if len(nodes) > 5 else "0"
        return [(nodes[6], common), (nodes[7], common)]
    return []


def _find(parent: dict[str, str], node: str) -> str:
    parent.setdefault(node, node)
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def lint_library(text: str, *, floating_ok: tuple[str, ...] = ()) -> list[LintFinding]:
    """Structural convergence findings for every ``.subckt`` in ``text``.

    ``floating_ok`` names ports the datasheet allows to be left unconnected (for
    example an enable pin documented as "float to enable"); such a port must be
    biased by the model itself, so a missing internal DC path is an error there.
    """
    findings: list[LintFinding] = []
    for ports, body, params in _subckt_blocks(text):
        elements = _elements(body)
        node_names = {n.lower() for element in elements for n in element.nodes}
        node_names |= {p.lower() for p in ports}
        for element in elements:
            kind = element.name[0].upper()
            if kind == "A":
                arity = _a_device_arity(element.text)
                if arity is not None and arity[0] != 8:
                    findings.append(
                        LintFinding(
                            ERROR,
                            "a_device_node_count",
                            element.line,
                            element.name,
                            f"`{element.name}` lists {arity[0]} node(s) before `{arity[1]}`; an "
                            "LTspice A-device takes exactly 8: five inputs, then QB (inverted "
                            "output), Q (output) and the common node, e.g. "
                            "`A1 S R 0 0 0 QB Q 0 SRFLOP Vhigh=1 Vlow=0`. Use 0 for unused inputs.",
                        )
                    )
                continue
            if kind not in {"B", "E", "G"}:
                continue
            expression = _expression(element.text)
            if not expression:
                continue
            referenced = {m[1].lower() for m in _VREF.finditer(expression)}
            if kind == "B" and re.search(r"(?i)\bV\s*=", element.text):
                own = element.nodes[0].lower() if element.nodes else ""
                if own and own in referenced and not _is_ground(own):
                    findings.append(
                        LintFinding(
                            ERROR,
                            "self_referencing_source",
                            element.line,
                            element.name,
                            f"the voltage source drives node `{element.nodes[0]}` and also reads "
                            f"V({element.nodes[0]}); this algebraic loop has no unique operating "
                            "point. Hold state on a capacitor with a finite-tau update and a ≤1 GΩ "
                            "DC path, or use an LTspice A-device (SRFLOP/DFLOP) for a latch.",
                        )
                    )
            stripped = _VREF.sub(" ", expression)
            stripped = re.sub(r"\bI\s*\(\s*[^()]*\)", " ", stripped, flags=re.I)
            stripped = re.sub(r"\{[^}]*\}", " ", stripped)
            for match in _IDENT.finditer(stripped):
                word = match[1]
                lower = word.lower()
                if lower in _KNOWN_WORDS or lower in params or parse_value(word) is not None:
                    continue
                if lower in node_names:
                    findings.append(
                        LintFinding(
                            ERROR,
                            "bare_node_in_expression",
                            element.line,
                            element.name,
                            f"`{word}` is a node, but a bare name in an expression is read as a "
                            f".param (LTspice: 'No such parameter defined'). Write "
                            f"V({word},GND) instead.",
                        )
                    )
                else:
                    findings.append(
                        LintFinding(
                            ERROR,
                            "undefined_identifier",
                            element.line,
                            element.name,
                            f"`{word}` is neither a node, a .param nor an LTspice function; "
                            "define it with .param inside the subcircuit or replace it with a number.",
                        )
                    )
            hard_steps = len(re.findall(r"(?i)\bif\s*\(", expression))
            if kind == "B" and re.search(r"(?i)\bI\s*=", element.text) and hard_steps >= 2:
                findings.append(
                    LintFinding(
                        WARNING,
                        "hard_switching_current",
                        element.line,
                        element.name,
                        f"{hard_steps} nested if() steps in a current source; a discontinuous "
                        "current is a common operating-point failure. Use limit()/tanh() with a "
                        "finite transition width (e.g. 10 mV) or a switch with hysteresis.",
                    )
                )
        for element in elements:
            if element.name[0].upper() != "R" or len(element.text.split()) < 4:
                continue
            value = parse_value(element.text.split()[3])
            if value is not None and value >= MAX_DC_PATH_OHMS:
                findings.append(
                    LintFinding(
                        WARNING,
                        "resistor_not_a_dc_path",
                        element.line,
                        element.name,
                        f"{element.text.split()[3]} Ω is too large to anchor a node for the "
                        "solver; use ≤1 GΩ (e.g. 100Meg) for a leakage/DC path.",
                    )
                )
        parent: dict[str, str] = {}
        for element in elements:
            for a, b in _dc_links(element):
                parent[_find(parent, a.lower())] = _find(parent, b.lower())
        anchors = {
            _find(parent, p.lower())
            for p in ports
            if _is_ground(p) or p.lower().startswith(_SUPPLY_PREFIXES)
        }
        anchors.add(_find(parent, "0"))
        floating = {name.lower() for name in floating_ok}
        port_set = {p.lower() for p in ports}
        first_line: dict[str, _Element] = {}
        for element in elements:
            for node in element.nodes:
                first_line.setdefault(node.lower(), element)
        for node in sorted(node_names):
            if _is_ground(node) or _find(parent, node) in anchors:
                continue
            if node in port_set and node not in floating:
                continue  # the fixture drives or loads ordinary ports
            element = first_line.get(node)
            findings.append(
                LintFinding(
                    ERROR,
                    "no_dc_path",
                    element.line if element else 0,
                    element.name if element else "",
                    (
                        f"port `{node}` may be left floating (datasheet) but has no internal DC "
                        "path to GND or a supply: bias it inside the model (e.g. a pull-up current "
                        "with a voltage compliance limit plus a finite resistor)."
                        if node in floating
                        else f"internal node `{node}` has no DC path to GND or a supply pin "
                        "(only capacitors/current sources/≥10 GΩ): add a ≤1 GΩ resistor to GND."
                    ),
                )
            )
    return findings


def floating_permitted(pin_map: list[dict] | None) -> tuple[str, ...]:
    """Ports the extracted pin table documents as allowed to float."""
    names: list[str] = []
    for pin in pin_map or []:
        words = " ".join(
            [str(pin.get("function") or "")] + [str(b) for b in (pin.get("behavior") or [])]
        ).lower()
        floats = re.search(
            r"\b(float|floating|left open|leave open|unconnected|no connect)\b", words
        )
        if floats and pin.get("name"):
            names.append(str(pin["name"]))
    return tuple(names)


def fix_bare_nodes(text: str) -> tuple[str, list[str]]:
    """Rewrite bare node names inside B-source expressions as ``V(node,GND)``.

    This is the one purely syntactic repair made without the author: LTspice reads a
    bare name as an undefined ``.param`` and refuses the whole file. The reference is
    taken against the subcircuit's own ground port when it declares one. Nothing
    else changes; the caller archives the original bytes as evidence.
    """
    changes: list[str] = []
    blocks = _subckt_blocks(text)
    if not blocks:
        return text, changes
    ports, body, params = blocks[0]
    ground = next((p for p in ports if _is_ground(p) and p != "0"), None)
    nodes = {n.lower() for element in _elements(body) for n in element.nodes}
    nodes |= {p.lower() for p in ports}
    protect = re.compile(r"(\bV\s*\([^()]*\)|\bI\s*\([^()]*\)|\{[^}]*\})", re.I)
    out: list[str] = []
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        code, separator, comment = raw.partition(";")
        stripped = code.strip()
        split = re.split(r"(?i)(\b[VI]\s*=)", code, maxsplit=1)
        if not stripped or stripped[0].upper() != "B" or len(split) != 3:
            out.append(raw)
            continue
        head, equals, expression = split

        def rewrite(match: re.Match, number: int = number) -> str:
            word = match[1]
            lower = word.lower()
            if lower in _KNOWN_WORDS or lower in params or lower not in nodes:
                return match[0]
            reference = f"V({word},{ground})" if ground else f"V({word})"
            changes.append(f"line {number}: {word} -> {reference}")
            return reference

        parts = protect.split(expression)
        rebuilt = "".join(
            part if index % 2 else _IDENT.sub(rewrite, part) for index, part in enumerate(parts)
        )
        out.append(head + equals + rebuilt + separator + comment)
    return "".join(out), changes


_LOG_LOCATION = re.compile(r"^(?P<file>.+?)\((?P<line>\d+)\):\s*(?P<message>.+)$")
_TROUBLE_NODE = re.compile(r'trouble with (?:node|instance)\s+"?([^"\s]+)"?', re.I)
_SINGULAR = re.compile(r"singular matrix:?\s*check node\s+(\S+)", re.I)


def read_log(path: Path) -> str:
    """LTspice logs are UTF-16LE or UTF-8, sometimes one after the other in one file."""
    data = Path(path).read_bytes()
    if data[:2] == b"\xff\xfe" or (len(data) > 1 and data[1:2] == b"\x00"):
        text = data.decode("utf-16-le", errors="replace")
        if "\x00" not in text[:200]:
            return text.replace("\r", "")
    return data.decode("utf-8", errors="replace").replace("\x00", "").replace("\r", "")


def diagnose_log(log_text: str, library_text: str, *, subckt_instance: str = "xdut") -> list[str]:
    """The model lines an LTspice log implicates, with the specific repair each needs."""
    lines = library_text.splitlines()
    hints: list[str] = []
    seen: set[str] = set()

    def add(hint: str) -> None:
        if hint not in seen:
            seen.add(hint)
            hints.append(hint)

    log_lines = log_text.splitlines()
    for index, raw in enumerate(log_lines):
        match = _LOG_LOCATION.match(raw.strip())
        if match is None:
            continue
        number = int(match["line"])
        message = match["message"].strip()
        source = lines[number - 1].strip() if 0 < number <= len(lines) else ""
        marker = log_lines[index + 2] if index + 2 < len(log_lines) else ""
        focus = ""
        if "^" in marker and index + 1 < len(log_lines):
            start = marker.index("^")
            focus = log_lines[index + 1][start : start + marker.count("^")].strip()
        detail = f" (at `{focus}`)" if focus else ""
        advice = ""
        if "no such parameter" in message.lower():
            advice = " — a bare name is read as a .param; reference nodes as V(node,GND)."
        add(f"LTspice rejected model line {number} `{source[:160]}`: {message}{detail}{advice}")

    lowered = log_text.lower()
    nodes = {m[1] for m in _TROUBLE_NODE.finditer(log_text)} | {
        m[1] for m in _SINGULAR.finditer(log_text)
    }
    for node in sorted(nodes):
        local = node.split(":")[-1]
        if node.lower().startswith(subckt_instance.lower() + ":"):
            local = node.split(":", 1)[1]
        touching = [
            line.strip()
            for line in lines
            if line.strip()
            and not line.strip().startswith(("*", ";", "."))
            and re.search(rf"(?i)(?<![\w]){re.escape(local)}(?![\w])", line)
        ]
        shown = "; ".join(f"`{t[:120]}`" for t in touching[:6]) or "(no model line names it)"
        add(
            f"LTspice could not converge at node `{node}`. Model lines touching `{local}`: {shown}. "
            "Give that node a finite DC path (≤1 GΩ) and replace hard if() steps or "
            "self-referencing sources that drive it with continuous expressions."
        )
    if "gmin stepping failed" in lowered or "source stepping failed" in lowered:
        add(
            "No DC operating point: every LTspice homotopy failed. Typical causes in behavioural "
            "models: a voltage source that reads its own output, a latch built from B sources, a "
            "state capacitor with no DC path, or discontinuous if() thresholds on a fed-back node."
        )
    if "time step too small" in lowered:
        add(
            "Transient stalled (time step too small): slow any edge faster than ~1 ns, bound "
            "every exp() argument, and give discontinuities a finite transition width."
        )
    return hints
