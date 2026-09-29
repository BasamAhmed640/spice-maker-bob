"""Pin-only mode: the pins of a part on the pin shell, with no function.

A separately requested, deliberately limited kind of model for ordinary components. It answers
"is every pin there, does the part sit correctly in a circuit, does it draw its quiescent current,
does a wiring mistake raise an alarm" and nothing else. It models no behaviour: outputs are
inert until commanded, no datasheet row is judged, and the result is never a pass. It is not what a
missing behavioural implementation falls back to; the caller asks for it by name.

The pin table comes in as extracted pin records (the same records the rest of the pipeline reads),
so a wrong pin table is the one thing this mode cannot detect: the card says where the table came
from and asks for it to be confirmed against the datasheet pinout.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from boardmodeler.authoring.pin_roles import terminal_name
from boardmodeler.models.pin_shell import ShellError, ShellPin, render_shell, shell_alarms

__all__ = ["PinOnlyModel", "PinOnlyRefusal", "build_pin_only"]

_PAD = re.compile(r"^(ep|epad|pad|powerpad|pwrpad|tab|thermal|expad|exposed)([^a-z].*)?$", re.I)
_OUTPUT_NAME = re.compile(r"^(vout|vo|out|sw|ph|lx|vreg|reg_out)(\d*)$", re.I)
_RAIL_NAME = re.compile(r"^(vin|vcc|vdd|vs|vsup|vbat|vbus|vpwr|vp|v\+)", re.I)


class PinOnlyRefusal(ValueError):
    """The pin table cannot become a pin-only model without guessing; the message names why."""


@dataclass(frozen=True)
class PinOnlyModel:
    """A pin-only library and what it was built from."""

    part: str
    subckt: str
    ports: tuple[str, ...]
    pins: tuple[ShellPin, ...]
    rail: str
    ground: str
    library_text: str
    #: plain sentences about choices the pin table did not settle
    notes: tuple[str, ...]

    def claimed_alarms(self) -> dict[str, tuple[str, ...]]:
        """Per port, the alarm kinds the model writes (each is proven by the gate)."""
        claimed: dict[str, list[str]] = {}
        for kind, port, _node in shell_alarms(self.pins):
            claimed.setdefault(port, []).append(kind)
        return {port: tuple(kinds) for port, kinds in claimed.items()}


def _kind(pin: dict[str, Any]) -> str:
    direction = str(pin.get("direction"))
    name = str(pin.get("name", ""))
    if direction == "nc":
        return "nc"
    if direction == "ground":
        return "pad" if _PAD.match(name) else "ground"
    if direction == "power":
        return "output" if _OUTPUT_NAME.match(name) else "supply"
    if direction == "input":
        return "pad" if _PAD.match(name) else "input"
    if direction == "output":
        return "output"
    if direction == "bidir":
        return "io"
    raise PinOnlyRefusal(f"pin_only_unknown_direction: pin {name!r} has direction {direction!r}")


def build_pin_only(part: str, subckt: str, pin_map: Sequence[dict[str, Any]]) -> PinOnlyModel:
    """Build the pin-only model for an extracted pin table, or refuse with the reason."""
    if not pin_map:
        raise PinOnlyRefusal(
            "pin_only_no_pin_table: no pin table is available; supply a reviewed extraction "
            "(--requirements) or extract one first"
        )
    names = [terminal_name(pin) for pin in pin_map]
    if len({name.lower() for name in names}) != len(names):
        raise PinOnlyRefusal("pin_only_ambiguous_pins: a terminal name appears twice")
    kinds = [_kind(pin) for pin in pin_map]
    notes: list[str] = []
    supplies = [n for n, k in zip(names, kinds, strict=True) if k == "supply"]
    grounds = [n for n, k in zip(names, kinds, strict=True) if k == "ground"]
    if not grounds:
        raise PinOnlyRefusal("pin_only_no_ground: the pin table has no ground pin to refer to")
    if not supplies:
        raise PinOnlyRefusal(
            "pin_only_no_supply: the pin table has no supply pin, so clamps and outputs have no "
            "rail to refer to"
        )
    domains = {
        str(pin.get("supply_domain")).strip().casefold()
        for pin, kind in zip(pin_map, kinds, strict=True)
        if kind == "supply" and pin.get("supply_domain")
    }
    if len(domains) > 1:
        raise PinOnlyRefusal(
            "pin_only_multiple_rails: the pin table names more than one supply domain "
            f"({', '.join(sorted(domains))}); this limited mode models one rail and one ground"
        )
    rail = next((n for n in supplies if _RAIL_NAME.match(n)), supplies[0])
    required_secondary = [
        name
        for pin, name, kind in zip(pin_map, names, kinds, strict=True)
        if kind == "supply"
        and name != rail
        and str(pin.get("connection_requirement")) == "required"
    ]
    if required_secondary:
        # The shell's required-connection alarm is referenced to ground. It cannot
        # distinguish a correctly powered extra supply from a floating connection,
        # even when extraction omits the domain or labels both supplies alike.
        raise PinOnlyRefusal(
            "pin_only_required_secondary_supply: required supply pins "
            f"{', '.join(required_secondary)} are additional to the selected rail {rail}; "
            "this limited mode cannot represent their supply connections and alarms"
        )
    if len(supplies) > 1:
        notes.append(f"{rail} is the rail; the other supply pins draw nothing and refer to it")
    pins: list[ShellPin] = []
    for number, (pin, name, kind) in enumerate(zip(pin_map, names, kinds, strict=True), start=1):
        physical = str(pin.get("physical_pin") or number)
        required = str(pin.get("connection_requirement")) == "required"
        if kind == "supply":
            pins.append(
                ShellPin(
                    name,
                    "supply",
                    physical,
                    iq=1e-6 if name == rail else 0.0,
                    required=required and name != rail,
                )
            )
        elif kind == "ground":
            pins.append(
                ShellPin(name, "ground", physical, required=required and name != grounds[0])
            )
        elif kind == "pad":
            pins.append(ShellPin(name, "pad", physical, required=True))
        elif kind == "input":
            pins.append(ShellPin(name, "input", physical))
        elif kind == "io":
            topology = "open_drain" if pin.get("output_topology") == "open_drain" else "push_pull"
            pins.append(ShellPin(name, "io", physical, topology=topology))
        elif kind == "output":
            topology = "open_drain" if pin.get("output_topology") == "open_drain" else "push_pull"
            pins.append(ShellPin(name, "output", physical, topology=topology))
        else:
            pins.append(ShellPin(name, "nc", physical))
    if any(
        str(p.get("direction")) == "power" and k == "output"
        for p, k in zip(pin_map, kinds, strict=True)
    ):
        notes.append("power pins named like an output (VOUT, SW, PH) are modelled as inert outputs")
    notes.append(
        "no drive strength, quiescent current or input leakage is cited in this mode: the shell "
        "defaults apply (1 uA on the rail, 50 ohm and 20 mA on outputs)"
    )
    try:
        text = render_shell(
            subckt,
            pins,
            title=f"{part} pin-only model: pins, supply draw, clamps and alarms; no function",
            rail=rail,
        )
    except ShellError as exc:
        raise PinOnlyRefusal(f"pin_only_not_buildable: {exc}") from exc
    return PinOnlyModel(
        part=part,
        subckt=subckt,
        ports=tuple(names),
        pins=tuple(pins),
        rail=rail,
        ground=grounds[0],
        library_text=text,
        notes=tuple(notes),
    )
