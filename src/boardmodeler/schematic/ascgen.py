"""Deterministic LTspice ``.asc`` generation (D9).

Two invariants drive this module:

* **The same spec always produces byte-identical text.**  Wires and flags are
  normalised, de-duplicated and sorted; components keep spec order.  Nothing
  depends on dict iteration order, time, or the filesystem.
* **Geometry follows the measured transforms.**  A pin sits at
  ``anchor + rotate(local_pin, rotation)`` (:mod:`boardmodeler.schematic.asc`),
  every wire is axis-aligned, and every placed component anchor is on the
  16-unit grid.  Mirrored rotations are rejected rather than emitted, because
  only ``R0``/``R90``/``R180``/``R270`` were verified against the simulator.

Symbols the schematic needs are copied **beside** the ``.asc`` (LTspice resolves
a symbol from the schematic's own directory - verified), together with the model
file their ``SYMATTR SpiceModel`` names, but only when they come from outside the
LTspice installation: vendor symbols and models are never copied out of it.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from boardmodeler.schematic.asc import (
    ROTATIONS,
    AsyPin,
    asy_attributes,
    asy_pins,
    is_rotation,
    pin_position,
    read_text,
    rotate_point,
)

__all__ = [
    "GRID",
    "CircuitSpec",
    "PlacedComponent",
    "SchematicBuilder",
    "generate_asc",
    "layout_series",
    "snap",
    "wire_segments",
    "write_asc",
]

#: LTspice's placement grid.
GRID = 16
#: Default sheet size used by the GUI.
DEFAULT_SHEET: tuple[int, int] = (880, 680)
#: Where directive text is placed when the spec does not say.
_DIRECTIVE_ORIGIN = (32, 48)
#: Escape stub length for a pin that is auto-routed.
PIN_STUB = 16

_MIRRORS = ("M0", "M90", "M180", "M270")
_ORIENTATION_DIRECTIONS: dict[str, tuple[int, int]] = {
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
    "UP": (0, -1),
    "TOP": (0, -1),
    "DOWN": (0, 1),
    "BOTTOM": (0, 1),
}


def snap(value: int) -> int:
    """Round ``value`` to the nearest grid point."""
    return round(value / GRID) * GRID


@dataclass(frozen=True)
class PlacedComponent:
    """One ``SYMBOL`` line of a schematic."""

    refdes: str
    symbol: str
    value: str = ""
    anchor: tuple[int, int] = (0, 0)
    rotation: str = "R0"
    #: Explicit ``.asy`` to read geometry from (defaults to ``<symbol_dir>/<symbol>.asy``).
    symbol_path: Path | None = None
    #: Extra ``SYMATTR`` lines, in order (e.g. ``("SpiceLine", "T=1m")``).
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass
class CircuitSpec:
    """A schematic to emit: components, wires, flags, directives, texts."""

    components: list[PlacedComponent] = field(default_factory=list)
    wires: list[tuple[int, int, int, int]] = field(default_factory=list)
    flags: list[tuple[int, int, str]] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    texts: list[tuple[int, int, str]] = field(default_factory=list)

    def component(self, refdes: str) -> PlacedComponent | None:
        for component in self.components:
            if component.refdes == refdes:
                return component
        return None

    def nets(self) -> list[str]:
        """Flag names in emission order."""
        return [name for _x, _y, name in _normalise_flags(self.flags)]

    def non_ground_nets(self) -> list[str]:
        return [net for net in self.nets() if net != "0"]


def layout_series(
    items: Sequence[object],
    *,
    start: tuple[int, int],
    spacing: int,
    axis: str = "x",
) -> list[tuple[int, int]]:
    """Grid anchors for ``items`` laid out from ``start`` along ``axis``.

    ``start`` is the anchor of the first item; every subsequent item advances by
    ``spacing`` (which must be a whole number of grid steps) on ``axis``.
    """
    if axis not in {"x", "y"}:
        raise ValueError(f"axis must be 'x' or 'y', not {axis!r}")
    if spacing <= 0 or spacing % GRID:
        raise ValueError(f"spacing must be a positive multiple of {GRID}, not {spacing}")
    origin_x, origin_y = snap(start[0]), snap(start[1])
    anchors: list[tuple[int, int]] = []
    for index in range(len(items)):
        offset = index * spacing
        anchors.append(
            (origin_x + offset, origin_y) if axis == "x" else (origin_x, origin_y + offset)
        )
    return anchors


def wire_segments(points: Sequence[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """Axis-aligned segments between consecutive ``points`` (no diagonals)."""
    segments: list[tuple[int, int, int, int]] = []
    for (x1, y1), (x2, y2) in pairwise(points):
        if x1 != x2 and y1 != y2:
            raise ValueError(f"wire from {(x1, y1)} to {(x2, y2)} is diagonal")
        if x1 == x2 and y1 == y2:
            continue
        segments.append((x1, y1, x2, y2))
    return segments


def _normalise_wires(
    wires: Sequence[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    seen: set[tuple[int, int, int, int]] = set()
    for x1, y1, x2, y2 in wires:
        if x1 != x2 and y1 != y2:
            raise ValueError(f"wire {(x1, y1)}-{(x2, y2)} is diagonal; only orthogonal wires")
        if (x1, y1) == (x2, y2):
            raise ValueError(f"wire at {(x1, y1)} has zero length")
        if (x2, y2) < (x1, y1):
            x1, y1, x2, y2 = x2, y2, x1, y1
        seen.add((x1, y1, x2, y2))
    return sorted(seen)


def _normalise_flags(flags: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    normalised: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int, str]] = set()
    for x, y, name in flags:
        entry = (snap(x), snap(y), name.strip())
        if not entry[2]:
            raise ValueError(f"flag at {(x, y)} has no net name")
        if " " in entry[2] or "," in entry[2]:
            raise ValueError(f"illegal net name {entry[2]!r} on a flag")
        if entry in seen:
            continue
        seen.add(entry)
        normalised.append(entry)
    return sorted(normalised, key=lambda item: (item[1], item[0], item[2]))


def _symbol_source(component: PlacedComponent, symbol_dir: Path) -> Path:
    if component.symbol_path is not None:
        return Path(component.symbol_path)
    return symbol_dir / f"{component.symbol}.asy"


def _validate_component(component: PlacedComponent, symbol_dir: Path) -> Path:
    if not component.refdes:
        raise ValueError("a placed component needs a refdes")
    rotation = component.rotation.upper()
    if rotation in _MIRRORS:
        raise ValueError(
            f"{component.refdes}: mirrored rotation {component.rotation!r} is refused - only "
            f"{', '.join(ROTATIONS)} were verified against LTspice (use a rotated symbol instead)"
        )
    if not is_rotation(rotation) or rotation not in ROTATIONS:
        raise ValueError(
            f"{component.refdes}: unknown rotation {component.rotation!r}; "
            f"expected one of {', '.join(ROTATIONS)}"
        )
    if component.anchor[0] % GRID or component.anchor[1] % GRID:
        raise ValueError(
            f"{component.refdes}: anchor {component.anchor} is off the {GRID}-unit grid"
        )
    source = _symbol_source(component, symbol_dir)
    if not source.is_file():
        raise FileNotFoundError(
            f"{component.refdes}: symbol {component.symbol!r} not found at {source}"
        )
    return source


def generate_asc(spec: CircuitSpec, *, symbol_dir: Path) -> str:
    """Emit ``.asc`` text for ``spec``.

    Raises ``ValueError`` for a mirrored/unknown rotation, an off-grid anchor, a
    diagonal or zero-length wire, an illegal net name, or a missing symbol file
    (which would otherwise make LTspice open a modal dialog and hang a batch
    invocation).
    """
    directory = Path(symbol_dir)
    lines = ["Version 4", f"SHEET 1 {DEFAULT_SHEET[0]} {DEFAULT_SHEET[1]}"]
    for x1, y1, x2, y2 in _normalise_wires(spec.wires):
        lines.append(f"WIRE {x1} {y1} {x2} {y2}")
    for x, y, name in _normalise_flags(spec.flags):
        lines.append(f"FLAG {x} {y} {name}")
    for component in spec.components:
        _validate_component(component, directory)
        x, y = component.anchor
        lines.append(f"SYMBOL {component.symbol} {x} {y} {component.rotation.upper()}")
        lines.append(f"SYMATTR InstName {component.refdes}")
        if component.value:
            lines.append(f"SYMATTR Value {component.value}")
        for key, value in component.attributes:
            lines.append(f"SYMATTR {key} {value}")
    for index, directive in enumerate(spec.directives):
        text = " ".join(str(directive).split())
        if not text:
            raise ValueError(f"directive {index} is empty")
        text = text.lstrip("!")
        x = _DIRECTIVE_ORIGIN[0]
        y = _DIRECTIVE_ORIGIN[1] + index * GRID
        lines.append(f"TEXT {x} {y} Left 2 !{text}")
    for x, y, text in spec.texts:
        lines.append(f"TEXT {snap(x)} {snap(y)} Left 2 {text}")
    return "\n".join(lines) + "\n"


def write_asc(spec: CircuitSpec, path: Path, *, symbol_dir: Path) -> Path:
    """Write ``spec`` to ``path`` and make its symbols resolvable beside it."""
    target = Path(path)
    directory = Path(symbol_dir)
    text = generate_asc(spec, symbol_dir=directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    for component in spec.components:
        source = _symbol_source(component, directory)
        _ensure_symbol_beside(component.symbol, source, target.parent)
    return target


def _ensure_symbol_beside(symbol: str, source: Path, dest_dir: Path) -> None:
    """Copy a non-installation ``.asy`` (and its model file) beside the schematic."""
    if _inside_ltspice_lib(source):
        return
    dest = dest_dir / f"{symbol}.asy"
    if not dest.exists():
        shutil.copyfile(source, dest)
    attributes = asy_attributes(read_text(source))
    model = attributes.get("SpiceModel", "").strip()
    if not model or _is_absolute(model):
        return
    for base in (source.parent, dest_dir):
        candidate = base / model
        if candidate.is_file():
            if _inside_ltspice_lib(candidate):
                return
            model_dest = dest_dir / model
            if not model_dest.exists():
                model_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, model_dest)
            return


def _is_absolute(value: str) -> bool:
    return Path(value).is_absolute() or (len(value) > 1 and value[1] == ":")


def _inside_ltspice_lib(path: Path) -> bool:
    from boardmodeler.simulation.ltspice import default_lib_dir

    lib = default_lib_dir()
    if lib is None:
        return False
    try:
        resolved = Path(path).resolve()
        return resolved.is_relative_to(Path(lib).resolve())
    except OSError:  # pragma: no cover - resolve() on a vanished path
        return False


# --------------------------------------------------------------------------- #
# builder


class SchematicBuilder:
    """Place parts and connect named nets; wires are routed orthogonally.

    Every connection gets a ``FLAG`` naming its net (ground is the flag ``0``,
    which is how LTspice represents the global reference), so the generated
    netlist carries the same net names the caller declared.
    """

    def __init__(
        self,
        symbol_dir: str | Path,
        *,
        origin: tuple[int, int] = (112, 96),
        spacing: int = 96,
    ) -> None:
        self.symbol_dir = Path(symbol_dir)
        self.origin = (snap(origin[0]), snap(origin[1]))
        self.spacing = spacing
        self.components: list[PlacedComponent] = []
        self.wires: list[tuple[int, int, int, int]] = []
        self.flags: list[tuple[int, int, str]] = []
        self.directives: list[str] = []
        self.texts: list[tuple[int, int, str]] = []
        self._geometry: dict[str, dict[str, AsyPin]] = {}
        self._wire_nets: list[str] = []

    # -- placement ----------------------------------------------------------

    def add(
        self,
        refdes: str,
        symbol: str,
        value: str = "",
        *,
        anchor: tuple[int, int] | None = None,
        rotation: str = "R0",
        attributes: Sequence[tuple[str, str]] = (),
        symbol_path: str | Path | None = None,
    ) -> PlacedComponent:
        """Place ``refdes``; ``anchor=None`` auto-places it on the origin row."""
        if self.component(refdes) is not None:
            raise ValueError(f"{refdes} is already placed")
        if anchor is None:
            column = len(self.components)
            anchor = (self.origin[0] + column * self.spacing, self.origin[1])
        component = PlacedComponent(
            refdes=refdes,
            symbol=symbol,
            value=value,
            anchor=(snap(anchor[0]), snap(anchor[1])),
            rotation=rotation.upper(),
            symbol_path=Path(symbol_path) if symbol_path else None,
            attributes=tuple(attributes),
        )
        _validate_component(component, self.symbol_dir)
        self._load(component)
        self.components.append(component)
        return component

    def component(self, refdes: str) -> PlacedComponent | None:
        for component in self.components:
            if component.refdes == refdes:
                return component
        return None

    def _load(self, component: PlacedComponent) -> dict[str, AsyPin]:
        if component.symbol not in self._geometry:
            source = _symbol_source(component, self.symbol_dir)
            self._geometry[component.symbol] = asy_pins(read_text(source))
        return self._geometry[component.symbol]

    def _require(self, refdes: str) -> tuple[PlacedComponent, dict[str, AsyPin]]:
        component = self.component(refdes)
        if component is None:
            raise KeyError(f"{refdes} is not placed")
        return component, self._geometry[component.symbol]

    def pin(self, refdes: str, pin_name: str) -> tuple[int, int]:
        """Absolute sheet position of ``refdes``'s ``pin_name``."""
        component, geometry = self._require(refdes)
        if pin_name not in geometry:
            raise KeyError(
                f"{refdes}: symbol {component.symbol!r} has no pin {pin_name!r} "
                f"(pins: {', '.join(geometry) or 'none'})"
            )
        return pin_position(component.anchor, geometry[pin_name].offset, component.rotation)

    def pins(self, refdes: str) -> dict[str, tuple[int, int]]:
        """Absolute positions of every pin of ``refdes``."""
        component, geometry = self._require(refdes)
        return {
            name: pin_position(component.anchor, pin.offset, component.rotation)
            for name, pin in geometry.items()
        }

    def pin_order(self, refdes: str) -> tuple[str, ...]:
        """Pin names in ``SpiceOrder`` order (as read from the ``.asy``)."""
        _component, geometry = self._require(refdes)
        ordered = sorted(geometry.values(), key=lambda pin: (pin.order or 0, pin.name))
        return tuple(pin.name for pin in ordered)

    def symbols(self) -> dict[str, tuple[str, ...]]:
        """refdes -> pin names in ``SpiceOrder`` order (for netlist pin lookups)."""
        return {component.refdes: self.pin_order(component.refdes) for component in self.components}

    # -- wiring -------------------------------------------------------------

    def escape_direction(self, refdes: str, pin_name: str) -> tuple[int, int]:
        """Axis direction a wire must leave ``refdes.pin_name`` in."""
        component, geometry = self._require(refdes)
        if pin_name not in geometry:
            raise KeyError(f"{refdes}: symbol {component.symbol!r} has no pin {pin_name!r}")
        pin = geometry[pin_name]
        if pin.orientation in _ORIENTATION_DIRECTIONS:
            local = _ORIENTATION_DIRECTIONS[pin.orientation]
        else:
            local = _centroid_direction(
                pin.offset, {name: p.offset for name, p in geometry.items()}
            )
        return rotate_point(local, component.rotation)

    def connect(
        self,
        net: str,
        *pins: tuple[str, str],
        via: Sequence[tuple[int, int]] | None = None,
    ) -> None:
        """Connect ``pins`` to ``net`` with orthogonal wires and a flag.

        A route may not cross a pin of another part or a wire already belonging
        to another net - in LTspice a coincident point *is* a junction, so a
        crossing would silently short two nets.  When no candidate route is
        clear the call raises rather than emitting a short; pass ``via``
        waypoints or move the parts.
        """
        if not net.strip() or " " in net or "," in net:
            raise ValueError(f"illegal net name {net!r}")
        if not pins:
            raise ValueError(f"net {net!r} has no pins to connect")
        first = self.pin(*pins[0])
        placements = [
            (self.pin(refdes, pin_name), self.escape_direction(refdes, pin_name))
            for refdes, pin_name in pins
        ]
        blocked = self._occupied(net)
        for position, _direction in placements:
            blocked.discard(position)
        stub_ends: list[tuple[int, int]] = []
        for (refdes, pin_name), (position, direction) in zip(pins, placements, strict=True):
            stub_end = (
                position[0] + direction[0] * PIN_STUB,
                position[1] + direction[1] * PIN_STUB,
            )
            clash = next(
                (point for point in _segment_points(position, stub_end) if point in blocked),
                None,
            )
            if clash is None and stub_end in blocked:
                clash = stub_end
            if clash is not None:
                raise ValueError(
                    f"cannot attach a wire to {refdes}.{pin_name} at {stub_end}: {clash} "
                    "already belongs to another net"
                )
            self._add_wires(net, wire_segments([position, stub_end]))
            blocked.discard(stub_end)
            stub_ends.append(stub_end)
        hub = stub_ends[0]
        for endpoint in stub_ends[1:]:
            path = self._choose_route(net, hub, endpoint, via, blocked)
            self._add_wires(net, wire_segments(path))
        self.flags.append((first[0], first[1], net))

    def _add_wires(self, net: str, segments: Sequence[tuple[int, int, int, int]]) -> None:
        for segment in segments:
            self.wires.append(segment)
            self._wire_nets.append(net)

    def _occupied(self, net: str) -> set[tuple[int, int]]:
        blocked: set[tuple[int, int]] = set()
        for component in self.components:
            blocked.update(self.pins(component.refdes).values())
        for wire_net, (x1, y1, x2, y2) in zip(self._wire_nets, self.wires, strict=True):
            if wire_net == net:
                continue
            blocked.update(_polyline_points([(x1, y1), (x2, y2)]))
        return blocked

    def _choose_route(
        self,
        net: str,
        start: tuple[int, int],
        end: tuple[int, int],
        via: Sequence[tuple[int, int]] | None,
        blocked: set[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        if via:
            return [start, *via, end]
        for candidate in _candidate_routes(start, end):
            points = [point for point in _polyline_points(candidate) if point not in (start, end)]
            if all(point not in blocked for point in points):
                return candidate
        raise ValueError(
            f"cannot route net {net!r} from {start} to {end} without crossing "
            "another net's pin or wire; move the parts or pass explicit 'via' waypoints"
        )

    def wire(self, *points: tuple[int, int]) -> None:
        """Add an explicit orthogonal polyline (belongs to no net)."""
        self._add_wires("", wire_segments(list(points)))

    def directive(self, text: str) -> None:
        """Add a SPICE directive (emitted as ``TEXT ... !<text>``)."""
        if not text.strip():
            raise ValueError("empty directive")
        self.directives.append(text.strip())

    def text(self, x: int, y: int, text: str) -> None:
        self.texts.append((x, y, text))

    def build(self) -> CircuitSpec:
        return CircuitSpec(
            components=list(self.components),
            wires=list(self.wires),
            flags=list(self.flags),
            directives=list(self.directives),
            texts=list(self.texts),
        )


def _centroid_direction(
    local: tuple[int, int], offsets: Mapping[str, tuple[int, int]]
) -> tuple[int, int]:
    """Escape direction inferred from where the pin sits relative to the body."""
    if not offsets:
        return (0, 1)
    centre_x = sum(point[0] for point in offsets.values()) / len(offsets)
    centre_y = sum(point[1] for point in offsets.values()) / len(offsets)
    dx = local[0] - centre_x
    dy = local[1] - centre_y
    if abs(dx) >= abs(dy) and dx:
        return (1 if dx > 0 else -1, 0)
    if dy:
        return (0, 1 if dy > 0 else -1)
    return (0, 1)


def _candidate_routes(start: tuple[int, int], end: tuple[int, int]) -> list[list[tuple[int, int]]]:
    """Deterministic orthogonal routes from ``start`` to ``end``, best first."""
    if start == end:
        return [[start, end]]
    if start[0] == end[0] or start[1] == end[1]:
        return [[start, end]]
    horizontal_first = [start, (end[0], start[1]), end]
    vertical_first = [start, (start[0], end[1]), end]
    return [
        horizontal_first,
        vertical_first,
        [start, (start[0] + PIN_STUB, start[1]), (start[0] + PIN_STUB, end[1]), end],
        [start, (start[0] - PIN_STUB, start[1]), (start[0] - PIN_STUB, end[1]), end],
        [start, (start[0], end[1] + PIN_STUB), (end[0], end[1] + PIN_STUB), end],
        [start, (start[0], end[1] - PIN_STUB), (end[0], end[1] - PIN_STUB), end],
    ]


def _polyline_points(points: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = [points[0]]
    for start, end in pairwise(points):
        out.extend(_segment_points(start, end))
        out.append(end)
    return out


def _segment_points(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Every point strictly between ``start`` and ``end`` (grid step when aligned)."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx and dy:
        raise ValueError(f"segment {start}-{end} is diagonal")
    length = max(abs(dx), abs(dy))
    if length <= 1:
        return []
    step = GRID if length % GRID == 0 else 1
    unit_x = (dx > 0) - (dx < 0)
    unit_y = (dy > 0) - (dy < 0)
    return [
        (start[0] + unit_x * offset, start[1] + unit_y * offset)
        for offset in range(step, length, step)
    ]
