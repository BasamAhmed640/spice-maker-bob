"""LTspice symbol (``.asy``) generation and validation (D8).

The one invariant that matters: the symbol's ``PINATTR SpiceOrder`` values must be
a **bijection onto the subcircuit's port positions**, because LTspice maps the
n-th node of the instance to the pin with ``SpiceOrder n``. A duplicated or
missing order silently connects the wrong net — so :func:`validate_symbol` checks
the bijection against the real ``.subckt`` line and reports findings instead of
trusting the generator.

The emitted shape follows the bundled vendor symbols (e.g. ``LT8609S.asy``):
``SYMATTR Prefix X``, ``SYMATTR SpiceModel <file>``, ``SYMATTR Value2 <subckt>``,
pins with visible leads, and one ``PINATTR`` pair per pin. Geometry is computed
locally; a language model is never responsible for the published layout.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import Finding
from boardmodeler.models.library import subckt_ports

__all__ = [
    "SYMBOL_CODE_ORDER_MISMATCH",
    "SYMBOL_CODE_PORTS_MISMATCH",
    "SYMBOL_CODE_SPICEMODEL",
    "symbol_pins",
    "symbol_text",
    "validate_symbol",
    "write_symbol",
]

SYMBOL_CODE_PORTS_MISMATCH = "SYM001_port_set_mismatch"
SYMBOL_CODE_ORDER_MISMATCH = "SYM002_spice_order_not_a_bijection"
SYMBOL_CODE_SPICEMODEL = "SYM003_spicemodel_attribute_missing"

_GRID = 16
_PIN_STUB = 32
_LABEL_INSET = 8
_ROW_PITCH = 48
_BODY_PADDING = 32
_DEFAULT_BODY_WIDTH = 192

#: Names that belong on the left of the body (control/supply inputs).
_LEFT_HINTS = (
    "vin",
    "vdd",
    "vcc",
    "pvin",
    "en",
    "in",
    "clk",
    "rt",
    "ss",
    "tr",
    "comp",
    "fb",
    "vsense",
    "ctrl",
    "set",
    "inp",
    "inm",
    "ref",
    "a",
)


@dataclass(frozen=True)
class SymbolPin:
    """One symbol pin: its name, body side, and SpiceOrder."""

    name: str
    side: str
    order: int
    x: int
    y: int


def _side_for(name: str, direction: str | None) -> str:
    if direction in ("output",):
        return "right"
    if direction in ("ground", "power"):
        return "right" if name.lower() in ("pg", "pwrgd", "gnd", "vss", "vout") else "left"
    lowered = name.lower()
    if direction == "input":
        return "left"
    return "left" if any(hint in lowered for hint in _LEFT_HINTS) else "right"


def _columns(
    ports: Sequence[str], directions: Mapping[str, str] | None = None
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    directions = dict(directions or {})
    left = [
        (index, name)
        for index, name in enumerate(ports)
        if _side_for(name, directions.get(name)) == "left"
    ]
    right = [
        (index, name)
        for index, name in enumerate(ports)
        if _side_for(name, directions.get(name)) == "right"
    ]

    # Keep supply pins together after the signals. Layout never changes SpiceOrder.
    def group(pin: tuple[int, str]) -> tuple[int, int]:
        index, name = pin
        supply = directions.get(name) in ("power", "ground") or name.upper() in {
            "VCC",
            "VDD",
            "VSS",
            "VEE",
            "GND",
            "AGND",
            "DGND",
            "V+",
            "V-",
        }
        return int(supply), index

    return sorted(left, key=group), sorted(right, key=group)


def symbol_pins(
    ports: Sequence[str], directions: Mapping[str, str] | None = None
) -> list[SymbolPin]:
    """Place pins on a grid; their electrical order stays the declaration order."""
    left, right = _columns(ports, directions)
    rows = max(len(left), len(right), 1)
    top = -((rows - 1) * _ROW_PITCH // 2 // _GRID) * _GRID
    width = body_width(ports, directions)
    pins: list[SymbolPin] = []
    for column, side in ((left, "left"), (right, "right")):
        for row, (index, name) in enumerate(column):
            y = top + row * _ROW_PITCH
            x = -(width // 2 + _PIN_STUB) if side == "left" else width // 2 + _PIN_STUB
            pins.append(SymbolPin(name=name, side=side, order=index + 1, x=x, y=y))
    pins.sort(key=lambda pin: pin.order)
    return pins


def body_width(ports: Sequence[str], directions: Mapping[str, str] | None = None) -> int:
    """Reserve room for both opposing labels, with a central gap and grid edges.

    Font size 2 pin labels get a conservative 16 units per character. The final
    PIN number is an offset from the connection point, not a font size.
    """
    columns = _columns(ports, directions)
    text_width = sum(max((len(name) for _, name in column), default=0) for column in columns)
    needed = max(_DEFAULT_BODY_WIDTH, text_width * 16 + 64)
    return ((needed + 2 * _GRID - 1) // (2 * _GRID)) * (2 * _GRID)


def symbol_text(
    name: str,
    ports: Sequence[str],
    *,
    model_file: str,
    model_name: str | None = None,
    description: str | None = None,
    directions: Mapping[str, str] | None = None,
) -> str:
    """Emit an ``.asy`` for ``ports`` bound to ``model_file``/``model_name``.

    ``ports`` order is the subcircuit's declaration order; the generated
    ``SpiceOrder`` values follow it exactly.
    """
    width = body_width(ports, directions)
    pins = symbol_pins(ports, directions)
    top = min((pin.y for pin in pins), default=0) - _BODY_PADDING
    bottom = max((pin.y for pin in pins), default=0) + _BODY_PADDING
    lines = [
        "Version 4",
        "SymbolType CELL",
        f"RECTANGLE Normal {-width // 2} {top} {width // 2} {bottom}",
        f"WINDOW 0 0 {top - 32} Center 2",
        f"WINDOW 3 0 {bottom + 32} Center 2",
        "SYMATTR Prefix X",
        f"SYMATTR Value {model_name or name}",
        f"SYMATTR SpiceModel {model_file}",
        f"SYMATTR Value2 {model_name or name}",
    ]
    if description:
        lines.append(f"SYMATTR Description {description}")
    for pin in pins:
        edge = -width // 2 if pin.side == "left" else width // 2
        lines.append(f"LINE Normal {pin.x} {pin.y} {edge} {pin.y}")
        orientation = pin.side.upper()
        lines.append(f"PIN {pin.x} {pin.y} {orientation} {_PIN_STUB + _LABEL_INSET}")
        lines.append(f"PINATTR PinName {pin.name}")
        lines.append(f"PINATTR SpiceOrder {pin.order}")
    return "\n".join(lines) + "\n"


def write_symbol(path: object, *args: object, **kwargs: object) -> object:
    """Write :func:`symbol_text` output to ``path`` and return the path."""
    from pathlib import Path

    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(symbol_text(*args, **kwargs), encoding="utf-8")  # type: ignore[arg-type]
    return target


_PIN_NAME_RE = re.compile(r"^\s*PINATTR\s+PinName\s+(\S+)\s*$", re.MULTILINE)
_PIN_ORDER_RE = re.compile(r"^\s*PINATTR\s+SpiceOrder\s+(\d+)\s*$", re.MULTILINE)
_SPICEMODEL_RE = re.compile(r"^\s*SYMATTR\s+SpiceModel\s+(\S+)\s*$", re.MULTILINE)


def symbol_pin_orders(asy_text: str) -> list[tuple[str, int]]:
    """``(pin name, SpiceOrder)`` pairs in file order."""
    names = _PIN_NAME_RE.findall(asy_text)
    orders = [int(value) for value in _PIN_ORDER_RE.findall(asy_text)]
    if len(names) != len(orders):
        return list(zip(names, orders, strict=False))
    return list(zip(names, orders, strict=True))


def validate_symbol(
    asy_text: str,
    *,
    subckt_text: str | None = None,
    ports: Sequence[str] | None = None,
    model_file: str | None = None,
) -> list[Finding]:
    """Check the symbol against the subcircuit it claims to represent.

    Reports ``SYM001`` when the pin-name sets differ, ``SYM002`` when the
    ``SpiceOrder`` values are not a bijection onto the port positions, and
    ``SYM003`` when no ``SpiceModel`` attribute is present.
    """
    findings: list[Finding] = []
    if ports is None:
        if subckt_text is None:
            raise ValueError("validate_symbol needs either subckt_text or ports")
        ports = subckt_ports(subckt_text)
    expected = list(ports)
    pairs = symbol_pin_orders(asy_text)
    names = [name for name, _order in pairs]
    orders = [order for _name, order in pairs]

    if set(names) != set(expected) or len(names) != len(expected):
        findings.append(
            Finding(
                code=SYMBOL_CODE_PORTS_MISMATCH,
                status=Status.FAIL,
                message=(
                    "the symbol's pin names do not match the subcircuit ports: "
                    f"symbol={names} subcircuit={expected}"
                ),
                detail={"symbol_pins": ",".join(names), "subckt_ports": ",".join(expected)},
            )
        )
    if sorted(orders) != list(range(1, len(expected) + 1)):
        findings.append(
            Finding(
                code=SYMBOL_CODE_ORDER_MISMATCH,
                status=Status.FAIL,
                message=(
                    "SpiceOrder values must be exactly 1..N with no duplicates: "
                    f"observed {sorted(orders)} for {len(expected)} ports"
                ),
                detail={"orders": ",".join(str(o) for o in sorted(orders))},
            )
        )
    elif sorted(pairs, key=lambda pair: pair[1]) != [
        (name, index + 1) for index, name in enumerate(expected)
    ]:
        findings.append(
            Finding(
                code="SYM004_spice_order_is_not_the_subcircuit_port_order",
                status=Status.FAIL,
                message="Each pin's SpiceOrder must match that pin's position in the .subckt",
                detail={"expected": ",".join(expected)},
            )
        )
    if model_file is not None:
        match = _SPICEMODEL_RE.search(asy_text)
        if match is None or match.group(1) != model_file:
            findings.append(
                Finding(
                    code=SYMBOL_CODE_SPICEMODEL,
                    status=Status.FAIL,
                    message=(
                        f"the symbol must reference model file {model_file!r}; observed "
                        f"{match.group(1) if match else None!r}"
                    ),
                    detail={"expected": model_file},
                )
            )
    return findings
