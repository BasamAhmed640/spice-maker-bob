"""Keep physical device terminals separate from laboratory fixture roles."""

from __future__ import annotations

import re
from pathlib import Path


def terminal_name(pin: dict) -> str:
    """A stable SPICE identifier; preserve +/- polarity rather than deleting it."""
    name = str(pin.get("mapped_symbol_pin") or pin["name"]).upper()
    name = name.replace("+", "P")
    for minus in ("-", "\u2212", "\u2013", "\u2014"):
        name = name.replace(minus, "M")
    name = re.sub(r"[^A-Z0-9_]", "_", name).strip("_")
    if not name or name[0].isdigit():
        name = "P_" + name
    if pin.get("direction") == "nc":
        name += "_" + re.sub(r"[^A-Za-z0-9]", "_", str(pin["physical_pin"]))
    return name


def physical_terminals(pin_map) -> tuple[str, ...]:
    names = tuple(terminal_name(pin) for pin in pin_map)
    if len(set(names)) != len(names):
        raise ValueError("physical_pin_ambiguous: repeated terminal names in the selected package")
    numbers = [str(pin["physical_pin"]) for pin in pin_map]
    if len(set(numbers)) != len(numbers):
        raise ValueError("physical_pin_ambiguous: mixed packages or duplicate physical pins")
    return names


def write_probe_adapter(
    model: Path, subckt: str, roles: dict[str, str], pin_map, destination: Path
) -> tuple[Path, str]:
    """Wire an unchanged model into a named fixture; no device terminals are invented."""
    from boardmodeler.authoring.probes import ProbeError, model_ports

    ports = model_ports(model, subckt)
    expected = set(physical_terminals(pin_map))
    if {p.upper() for p in ports} != expected:
        raise ProbeError("physical_pin_contract", f"expected {sorted(expected)}, got {ports}")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", role) for role in roles):
        raise ProbeError("probe_role_invalid")
    if any(pin not in expected for pin in roles.values()):
        raise ProbeError("probe_pin_missing")
    if len(set(roles.values())) != len(roles):
        raise ProbeError("probe_pin_short", "two fixture roles cannot short one terminal")
    reverse = {pin: role for role, pin in roles.items()}
    nc = {terminal_name(pin) for pin in pin_map if pin.get("direction") == "nc"}
    missing = expected - set(reverse) - nc
    if missing:
        raise ProbeError(
            "probe_pin_unconnected", f"unassigned physical terminals: {sorted(missing)}"
        )
    nodes = [reverse.get(pin.upper(), f"unused_{pin}") for pin in ports]
    name = "BM_FIXTURE_DUT"
    lines = [
        f'.include "{model.resolve().as_posix()}"',
        f".subckt {name} {' '.join(roles)}",
        f"Xdut {' '.join(nodes)} {subckt}",
        f".ends {name}",
        "",
    ]
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination, name
