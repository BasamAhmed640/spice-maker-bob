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
#: Room inside the body for the labels of top/bottom pins.
_EDGE_LABEL_BAND = 32

#: Pin roles, read from the (sanitized) pin name first and the pin map's direction second.
#: The side follows the schematic convention readers expect: positive supplies on top,
#: grounds, negative supplies and exposed pads at the bottom, inputs and controls on the
#: left, outputs and the feedback/compensation network on the right.
_SUPPLY_RE = re.compile(
    r"^(P|A|D|S)?(VIN|VCC|VDD|VBAT|VBUS|VSUP|VSYS|VPWR|VBB|VCP|VPOS|VS|VSP|VP|IN_?SUPPLY)"
    r"(A|D|IO|Q)?\d*$"
)
_GROUND_RE = re.compile(
    r"^(P|A|D|S|C)?(GND|VSS|VEE|VNEG|VSM)\d*$|^(E?PAD|POWERPAD|PWRPAD|THERMAL_?PAD|EXPOSED_?PAD|"
    r"PAD_?GND|DAP|EP)\d*$"
)
_OUTPUT_RE = re.compile(
    r"^(V?OUT|SW|PH|LX|BOOT|BST|CB|CBOOT|PG|PGOOD|PWRGD|POK|FLT|N?FAULT|ALERT|RDY|FB|VSENSE|"
    r"VFB|ADJ|SENSE|COMP|VC|ITH|BYP|NR|REF(OUT)?)"
    r"(_?\w*)?$"
)
_INPUT_RE = re.compile(r"^(P_)?\d*(IN|INP|INM|IN_?P|IN_?M|EN|SS|TR|RT|CLK|SYNC|MODE|ILIM|UVLO)")
_CHANNEL_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class SymbolPin:
    """One symbol pin: its name, body side, and SpiceOrder."""

    name: str
    side: str
    order: int
    x: int
    y: int


def _side_for(name: str, direction: str | None, ports: Sequence[str] = ()) -> str:
    upper = name.upper()
    if direction == "nc" or upper.startswith("NC"):
        return "right"
    if upper == "VM":  # V- when there is a V+, otherwise a motor/module supply
        return "bottom" if "VP" in {port.upper() for port in ports} else "top"
    if _GROUND_RE.match(upper):
        return "bottom"
    if _SUPPLY_RE.match(upper):
        return "top"
    if _OUTPUT_RE.match(upper):
        return "right"
    if _INPUT_RE.match(upper):
        return "left"
    if direction == "ground":
        return "bottom"
    if direction == "power":
        return "top"
    if direction == "output":
        return "right"
    return "left"


def _channel(name: str, multi: bool) -> int:
    if not multi:
        return 0
    match = _CHANNEL_RE.search(name)
    return int(match.group(1)) if match else 0


def _polarity_rank(name: str) -> int:
    """Within a channel: + input before - input before anything else."""
    upper = name.upper()
    if re.search(r"(INP|IN_?P|\+)$|P$", upper) and "IN" in upper:
        return 0
    if re.search(r"(INM|IN_?M|-)$|M$", upper) and "IN" in upper:
        return 1
    return 2


#: Right-side order: power outputs (the switch node beside its boot capacitor), then
#: status flags, then the feedback/compensation pins, then no-connects.
_FEEDBACK_RE = re.compile(r"^(FB|VFB|VSENSE|ADJ|SENSE|COMP|VC|ITH|BYP|NR|REF)")
_STATUS_RE = re.compile(r"^(PG|PGOOD|PWRGD|POK|FLT|N?FAULT|ALERT|RDY)")


def _right_rank(name: str) -> int:
    upper = name.upper()
    if upper.startswith("NC"):
        return 3
    if _FEEDBACK_RE.match(upper):
        return 2
    if _STATUS_RE.match(upper):
        return 1
    return 0


def _sides(
    ports: Sequence[str], directions: Mapping[str, str] | None = None
) -> dict[str, list[tuple[int, str]]]:
    directions = dict(directions or {})
    sides: dict[str, list[tuple[int, str]]] = {"left": [], "right": [], "top": [], "bottom": []}
    for index, name in enumerate(ports):
        sides[_side_for(name, directions.get(name), ports)].append((index, name))
    return sides


def _side_rows(
    left: list[tuple[int, str]], right: list[tuple[int, str]]
) -> tuple[dict[int, int], dict[int, int], int]:
    """Row numbers for the side pins, grouping numbered channels (IN1P, IN1M, OUT1 ...).

    A channel with two inputs and one output gets the op-amp shape: + input, output one
    row lower, - input below it. Channels are separated by one empty row.
    """
    channels = {
        match.group(1)
        for _, name in left + right
        if (match := _CHANNEL_RE.search(name)) is not None
    }
    multi = len(channels) > 1
    blocks: dict[int, tuple[list, list]] = {}
    for column, target in ((left, 0), (right, 1)):
        for index, name in column:
            blocks.setdefault(_channel(name, multi), ([], []))[target].append((index, name))
    # Channels only get their own blocks (and a blank row between them) when a channel
    # has more than one pin a side needs grouping for; INPUT_n/OUTPUT_n just share rows.
    grouped = multi and any(len(a) + len(b) > 2 for a, b in blocks.values())
    left_rows: dict[int, int] = {}
    right_rows: dict[int, int] = {}
    row = 0
    for position, key in enumerate(sorted(blocks)):
        block_left, block_right = blocks[key]
        block_left.sort(key=lambda pin: (_polarity_rank(pin[1]), pin[0]))
        block_right.sort(key=lambda pin: (_right_rank(pin[1]), pin[0]))
        if position and grouped:
            row += 1  # a blank row between channels
        if grouped and len(block_left) == 2 and len(block_right) == 1:
            left_rows[block_left[0][0]] = row
            right_rows[block_right[0][0]] = row + 1
            left_rows[block_left[1][0]] = row + 2
            row += 3
            continue
        for offset, (index, _name) in enumerate(block_left):
            left_rows[index] = row + offset
        for offset, (index, _name) in enumerate(block_right):
            right_rows[index] = row + offset
        row += max(len(block_left), len(block_right), 1)
    return left_rows, right_rows, row


def _edge_pitch(names: Sequence[str]) -> int:
    """Horizontal pitch for top/bottom pins: the widest label plus a gap, on the 32 grid."""
    widest = max((len(name) for name in names), default=0)
    return max(64, ((widest * 16 + 24 + 31) // 32) * 32)


def symbol_pins(
    ports: Sequence[str], directions: Mapping[str, str] | None = None
) -> list[SymbolPin]:
    """Place pins on a grid; their electrical order stays the declaration order."""
    sides = _sides(ports, directions)
    left_rows, right_rows, rows = _side_rows(sides["left"], sides["right"])
    rows = max(rows, 1)
    top = -((rows - 1) * _ROW_PITCH // 2 // _GRID) * _GRID
    width = body_width(ports, directions)
    body_top, body_bottom = _body_edges(ports, directions)
    pins: list[SymbolPin] = []
    for index, name in sides["left"] + sides["right"]:
        side = "left" if index in left_rows else "right"
        row = left_rows.get(index, right_rows.get(index, 0))
        y = top + row * _ROW_PITCH
        x = -(width // 2 + _PIN_STUB) if side == "left" else width // 2 + _PIN_STUB
        pins.append(SymbolPin(name=name, side=side, order=index + 1, x=x, y=y))
    for edge in ("top", "bottom"):
        members = sorted(sides[edge], key=lambda pin: pin[0])
        pitch = _edge_pitch([name for _, name in members])
        for position, (index, name) in enumerate(members):
            x = int((position - (len(members) - 1) / 2) * pitch)
            y = body_top - _PIN_STUB if edge == "top" else body_bottom + _PIN_STUB
            pins.append(SymbolPin(name=name, side=edge, order=index + 1, x=x, y=y))
    pins.sort(key=lambda pin: pin.order)
    return pins


def _body_edges(
    ports: Sequence[str], directions: Mapping[str, str] | None = None
) -> tuple[int, int]:
    """Body top/bottom: room for the side rows, plus a label band for top/bottom pins."""
    sides = _sides(ports, directions)
    _left, _right, rows = _side_rows(sides["left"], sides["right"])
    rows = max(rows, 1)
    first = -((rows - 1) * _ROW_PITCH // 2 // _GRID) * _GRID
    last = first + (rows - 1) * _ROW_PITCH
    top = first - _BODY_PADDING - (_EDGE_LABEL_BAND if sides["top"] else 0)
    bottom = last + _BODY_PADDING + (_EDGE_LABEL_BAND if sides["bottom"] else 0)
    return top, bottom


def body_width(ports: Sequence[str], directions: Mapping[str, str] | None = None) -> int:
    """Reserve room for both opposing labels, with a central gap and grid edges.

    Font size 2 pin labels get a conservative 16 units per character. The final
    PIN number is an offset from the connection point, not a font size. Top and
    bottom pins need their own labels side by side, one pitch apart.
    """
    sides = _sides(ports, directions)
    text_width = sum(
        max((len(name) for _, name in sides[column]), default=0) for column in ("left", "right")
    )
    needed = max(_DEFAULT_BODY_WIDTH, text_width * 16 + 64)
    for edge in ("top", "bottom"):
        names = [name for _, name in sides[edge]]
        if names:
            needed = max(needed, _edge_pitch(names) * len(names) + 32)
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
    top, bottom = _body_edges(ports, directions)
    has_top = any(pin.side == "top" for pin in pins)
    has_bottom = any(pin.side == "bottom" for pin in pins)
    lines = [
        "Version 4",
        "SymbolType CELL",
        f"RECTANGLE Normal {-width // 2} {top} {width // 2} {bottom}",
        f"WINDOW 0 0 {top - (64 if has_top else 32)} Center 2",
        f"WINDOW 3 0 {bottom + (64 if has_bottom else 32)} Center 2",
        "SYMATTR Prefix X",
        f"SYMATTR Value {model_name or name}",
        f"SYMATTR SpiceModel {model_file}",
        f"SYMATTR Value2 {model_name or name}",
    ]
    if description:
        lines.append(f"SYMATTR Description {description}")
    for pin in pins:
        if pin.side in ("left", "right"):
            edge = -width // 2 if pin.side == "left" else width // 2
            lines.append(f"LINE Normal {pin.x} {pin.y} {edge} {pin.y}")
        else:
            edge = top if pin.side == "top" else bottom
            lines.append(f"LINE Normal {pin.x} {pin.y} {pin.x} {edge}")
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
