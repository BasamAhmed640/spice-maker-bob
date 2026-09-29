"""The pin shell: every package pin of a part as a plain-SPICE port that is ready to go.

One mechanism for any IC. Given a confirmed pin table, :func:`render_shell` writes a
``.subcircuit`` in which

* every package pin is a port, in the order of the pin table (so the symbol's
  ``SpiceOrder`` is that order);
* every current returns through pins, never through the simulator's node 0, so the
  package obeys Kirchhoff's current law and a system simulation can add up its rails;
* each pin behaves by its *kind*: supplies draw their quiescent current, inputs are high
  impedance with a default and clamps, outputs have finite drive, take their current from
  the supply pin and current limit, an exposed pad is its own pin, a no-connect pin is
  inert;
* the part's function, if it has one, is a short ``core`` of plain SPICE placed inside,
  and it steers each output through a *desired level* expression (``drives``).

The shell decides nothing about what the part does and cites nothing itself: every number
comes in through :class:`ShellPin` from a cited datasheet row or is a labelled default.
:mod:`boardmodeler.authoring.viability` is the judge that the result sits correctly in a
circuit; this module never judges itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "ALARM_KINDS",
    "PIN_KINDS",
    "ShellError",
    "ShellPin",
    "alarm_node",
    "level_param",
    "render_shell",
    "shell_alarms",
]

PIN_KINDS = ("supply", "ground", "input", "output", "io", "analog", "nc", "pad")

#: Convergence leak: a helper resistance, never a datasheet value.
_LEAK = "1G"


class ShellError(ValueError):
    """The pin table cannot be turned into a model without guessing."""


@dataclass(frozen=True)
class ShellPin:
    """One package pin and the cited numbers its kind needs."""

    port: str
    kind: str
    number: str = ""
    #: supply: quiescent current drawn by this pin when powered (A), fully on above ``v_on``
    iq: float = 1e-6
    v_on: float = 1.0
    #: input / io: resistance to the ground pin, and what a floating pin defaults to
    r_in: float = 1e9
    default: str = "none"  # "none" | "pullup" | "pulldown"
    r_default: float = 100e3
    #: input / io: "both" clamps to each rail, "gnd" clamps only below ground, "none" neither
    clamp: str = "both"
    #: input / io: bias current in amperes, positive flowing OUT of the pin; it fades out as
    #: the pin leaves the input stage's range (``ib_headroom`` volts below the rail)
    ibias: float = 0.0
    ib_headroom: float = 1.0
    #: output / io: "push_pull" or "open_drain", output resistance and current limits (A)
    topology: str = "push_pull"
    r_out: float = 50.0
    i_source: float = 20e-3
    i_sink: float = 20e-3
    #: absolute limits, volts above the ground pin, beyond which the datasheet warns of damage;
    #: ``vmax_over_rail`` is the upper limit relative to the supply pin (a CMOS input: 0.3)
    vmax: float | None = None
    vmin: float | None = None
    vmax_over_rail: float | None = None
    #: digital input thresholds; a level between them is "undefined" (floating, or too slow)
    vil: float | None = None
    vih: float | None = None
    #: a pin the board must connect (exposed pad, second ground): an open one is detected
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in PIN_KINDS:
            raise ShellError(f"pin {self.port!r}: kind {self.kind!r} is not one of {PIN_KINDS}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.port):
            raise ShellError(f"pin {self.port!r}: a port name is letters, digits and underscores")
        if self.default not in ("none", "pullup", "pulldown"):
            raise ShellError(
                f"pin {self.port!r}: default {self.default!r} is not none/pullup/pulldown"
            )
        if self.clamp not in ("both", "gnd", "none"):
            raise ShellError(f"pin {self.port!r}: clamp {self.clamp!r} is not both/gnd/none")
        if self.topology not in ("push_pull", "open_drain"):
            raise ShellError(
                f"pin {self.port!r}: topology {self.topology!r} is not push_pull/open_drain"
            )


def level_param(port: str) -> str:
    """The instance parameter that commands an output no core drives.

    ``-1`` (the default) leaves the pin high impedance; ``0`` to ``1`` drives that
    fraction of the supply rail through the pin's output stage (open-drain: ``1`` pulls
    low). It lets a user, or the gate, exercise any output of a part with no function core.
    """
    return f"LEVEL_{port}"


ALARM_KINDS = ("abs", "ovl", "flt", "tie")


def alarm_node(kind: str, port: str) -> str:
    """The internal node that reads 1 when alarm ``kind`` fires on ``port`` and 0 when quiet.

    Plot it from the user's schematic as ``V(x1:chk_abs_VCC)`` (``x1`` is the instance).
    """
    return f"chk_{kind}_{port}"


def shell_alarms(
    pins: Sequence[ShellPin], drives: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """``(kind, port, node)`` for every alarm :func:`render_shell` writes for ``pins``."""
    drives = drives or {}
    out: list[tuple[str, str, str]] = []
    first_ground = next((p.port for p in pins if p.kind == "ground"), None)
    for pin in pins:
        if pin.vmax is not None or pin.vmin is not None or pin.vmax_over_rail is not None:
            out.append(("abs", pin.port, alarm_node("abs", pin.port)))
        if pin.kind in ("output", "io"):
            out.append(("ovl", pin.port, alarm_node("ovl", pin.port)))
        if pin.kind in ("input", "io") and pin.vil is not None and pin.vih is not None:
            out.append(("flt", pin.port, alarm_node("flt", pin.port)))
        if pin.required and pin.port != first_ground:
            out.append(("tie", pin.port, alarm_node("tie", pin.port)))
    return out


def _n(value: float) -> str:
    return f"{value:.6g}"


def render_shell(
    name: str,
    pins: Sequence[ShellPin],
    *,
    core: str = "",
    drives: Mapping[str, str] | None = None,
    title: str = "",
    rail: str | None = None,
) -> str:
    """The model text for ``pins`` (in that port order) with an optional function ``core``.

    ``drives`` maps an output/io port to an expression for the *level it should drive*
    (volts above the ground pin; for an open-drain pin, 0..1 where 1 pulls low). The shell
    clamps the level to the rails and wraps it in the pin's finite-drive output stage. ``rail``
    names the supply pin every clamp, default and output refers to (default: the first supply
    pin), for a part whose first supply pin is not its main rail. An
    output with no entry stays high impedance.
    """
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise ShellError(f"subcircuit name {name!r} is letters, digits and underscores")
    drives = dict(drives or {})
    ports = [pin.port for pin in pins]
    if len({p.lower() for p in ports}) != len(ports):
        raise ShellError("a port name appears twice in the pin table")
    ground = next((p.port for p in pins if p.kind == "ground"), None)
    if ground is None:
        raise ShellError("the pin table has no ground pin to reference the model to")
    supplies = [p.port for p in pins if p.kind == "supply"]
    if rail is not None and rail not in supplies:
        raise ShellError(f"rail {rail!r} is not a supply pin of this part")
    rail = rail or next(iter(supplies), None)
    by_port = {p.port: p for p in pins}
    for port in drives:
        if port not in by_port or by_port[port].kind not in ("output", "io"):
            raise ShellError(f"drive for {port!r}: it is not an output or io pin of this part")
    needs_rail = any(
        (p.kind in ("output", "io") and p.port in drives)
        or (p.kind in ("input", "io") and (p.clamp == "both" or p.default == "pullup"))
        or (p.kind in ("input", "io") and p.vil is not None)
        or p.vmax_over_rail is not None
        or p.ibias > 0
        for p in pins
    )
    if needs_rail and rail is None:
        raise ShellError("the pin table has no supply pin for the rail these pins refer to")

    g, r = ground, rail
    commanded = [p.port for p in pins if p.kind in ("output", "io") and p.port not in drives]
    header = ".subckt " + name + " " + " ".join(ports)
    if commanded:
        header += " params: " + " ".join(f"{level_param(port)}=-1" for port in commanded)
    lines = [f"* {title or name}", "* pin shell: every current returns through a pin", header]
    for pin in pins:
        p = pin.port
        if pin.kind == "supply":
            lines.append(f"* supply {p}")
            lines.append(f"Bq_{p} {p} {g} I={_n(pin.iq)}*limit(V({p},{g})/{_n(pin.v_on)},0,1)")
        elif pin.kind == "pad":
            lines.append(f"* exposed pad {p}: its own pin, convergence leak only")
            lines.append(f"Rpad_{p} {p} {g} {_LEAK}")
        elif pin.kind == "analog":
            lines.append(f"* analog {p}: high impedance, driven by the core")
            lines.append(f"Rin_{p} {p} {g} {_LEAK}")
        elif pin.kind in ("input", "io"):
            lines.append(f"* {pin.kind} {p}")
            if pin.default == "pullup":
                lines.append(f"Rdef_{p} {p} {r} {_n(pin.r_default)}")
            elif pin.default == "pulldown":
                lines.append(f"Rdef_{p} {p} {g} {_n(pin.r_default)}")
            if pin.vil is not None and pin.default == "none":
                # a floating CMOS input drifts between the rails: model the worst case, so the
                # undefined-level alarm has something to see
                lines.append(f"Rin_hi_{p} {p} {r} {_n(2 * pin.r_in)}")
                lines.append(f"Rin_lo_{p} {p} {g} {_n(2 * pin.r_in)}")
            else:
                lines.append(f"Rin_{p} {p} {g} {_n(pin.r_in)}")
            if pin.clamp == "both":
                lines.append(f"Dh_{p} {p} {r} Dpin")
            if pin.clamp in ("both", "gnd"):
                lines.append(f"Dl_{p} {g} {p} Dpin")
            if pin.ibias > 0:
                fade = f"limit((V({r},{g})-{_n(pin.ib_headroom)}-V({p},{g}))/0.2,0,1)"
                lines.append(f"Bib_{p} {g} {p} I={_n(pin.ibias)}*{fade}")
            elif pin.ibias < 0:
                fade = f"limit((V({p},{g})-0.5)/0.2,0,1)"
                lines.append(f"Bib_{p} {p} {g} I={_n(-pin.ibias)}*{fade}")
        if pin.kind in ("output", "io"):
            if pin.kind == "output":
                lines.append(f"* output {p} ({pin.topology})")
                lines.append(f"Rlk_{p} {p} {g} {_LEAK}")
            level_name = level_param(p)
            if p in drives:
                level, gate = drives[p], ""
            else:
                level = f"{level_name}*V({r},{g})"
                gate = f"*u({level_name}+0.5)"
            if pin.topology == "push_pull":
                lines.append(f"Bdrv_{p} drv_{p} {g} V=limit({level},0,max(V({r},{g}),0))")
                lines.append(
                    f"Bso_{p} {r} {p} I=limit((V(drv_{p},{g})-V({p},{g}))/{_n(pin.r_out)},0,{_n(pin.i_source)}){gate}"
                )
                lines.append(
                    f"Bsk_{p} {p} {g} I=limit((V({p},{g})-V(drv_{p},{g}))/{_n(pin.r_out)},0,{_n(pin.i_sink)}){gate}"
                )
            else:
                pull = drives.get(p, level_name)
                lines.append(f"Bdrv_{p} drv_{p} {g} V=limit({pull},0,1)")
                lines.append(
                    f"Bsk_{p} {p} {g} I=limit(V({p},{g})/{_n(pin.r_out)},0,{_n(pin.i_sink)})*V(drv_{p},{g})"
                )
    if core.strip():
        lines.append("* function core")
        lines.extend(core.strip().splitlines())
    alarms = shell_alarms(pins, drives)
    if alarms:
        lines.append(
            "* alarms: 1 when a datasheet condition is violated, else 0 (plot V(x1:<node>))"
        )
    for kind, port, node in alarms:
        pin = by_port[port]
        if kind == "abs":
            terms = []
            if pin.vmax is not None:
                terms.append(f"u(V({port},{g})-{_n(pin.vmax)})")
            if pin.vmin is not None:
                terms.append(f"u({_n(pin.vmin)}-V({port},{g}))")
            if pin.vmax_over_rail is not None:
                terms.append(f"u(V({port},{g})-V({r},{g})-{_n(pin.vmax_over_rail)})")
            lines.append(f"B{node} {node} {g} V=limit({'+'.join(terms)},0,1)")
        elif kind == "ovl":
            gate = "" if port in drives else f"*u({level_param(port)}+0.5)"
            if pin.topology == "push_pull":
                demand = (
                    f"u((V(drv_{port},{g})-V({port},{g}))/{_n(pin.r_out)}-{_n(pin.i_source)})"
                    f"+u((V({port},{g})-V(drv_{port},{g}))/{_n(pin.r_out)}-{_n(pin.i_sink)})"
                )
            else:
                demand = f"u(V({port},{g})/{_n(pin.r_out)}-{_n(pin.i_sink)})*V(drv_{port},{g})"
            lines.append(f"B{node} {node} {g} V=limit({demand},0,1){gate}")
        elif kind == "flt":
            raw = f"{node}_raw"
            lines.append(
                f"B{raw} {raw} {g} V=u(V({port},{g})-{_n(pin.vil)})*u({_n(pin.vih)}-V({port},{g}))"
            )
            lines.append(f"R{node} {raw} {node} 1k")
            lines.append(f"C{node} {node} {g} 10n")
        else:  # "tie": a 1 nA probe current lifts a pin nothing on the board holds
            lines.append(f"Binj_{port} {g} {port} I=1n")
            lines.append(f"B{node} {node} {g} V=u(V({port},{g})-0.1)")
    if alarms:
        total = "+".join(f"V({node},{g})" for _kind, _port, node in alarms)
        lines.append(f"Bchk_any chk_any {g} V=limit({total},0,1)")
    lines.append(".model Dpin D(Is=1e-14 N=1 Rs=10)")
    lines.append(f".ends {name}")
    return "\n".join(lines) + "\n"
