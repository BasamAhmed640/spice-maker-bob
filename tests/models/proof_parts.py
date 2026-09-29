"""Pin tables and function cores for the parts that prove the pin shell.

Data, not code: each part is a pin table (from the datasheet's pin table and cited
electrical rows) plus, where the part has a function, a short core of plain SPICE.
Nothing here is specific to the shell or the gate; the pipeline will read the same
tables from the datasheet digest.
"""

from __future__ import annotations

from boardmodeler.authoring.viability import GatePin, GateSpec
from boardmodeler.models.pin_shell import ShellPin, level_param, render_shell

# --------------------------------------------------------------------------- #
# LM358 dual op amp (TI SLOS068). Values are the ones the committed model was judged with.


def _opamp_channel(n: int, inp: str, inm: str) -> tuple[str, str]:
    """One op-amp channel: offset, a transconductance stage into a compensation node.

    DC gain 1e5 (gm 1 mS into 100 Meg), GBW ~0.7 MHz (227 pF), slew 0.3 V/us (68 uA), input
    offset 3 mV. Returns the core text and the level expression the output stage follows.
    """
    core = "\n".join(
        [
            f"Bvos{n} d{n} VEE V = V({inp},VEE) - V({inm},VEE) - 3m",
            f"Bgm{n} VEE n{n} I = 68u*tanh(1m*V(d{n},VEE)/68u)",
            f"R{n} n{n} VEE 100Meg",
            f"C{n} n{n} VEE 227p",
            f"Dclp{n}a n{n} VCC Dpin",
            f"Dclp{n}b VEE n{n} Dpin",
        ]
    )
    return core, f"limit(V(n{n},VEE),5m,max(V(VCC,VEE)-2,5m))"


LM358_PINS = (
    ShellPin("VCC", "supply", "8", iq=700e-6, vmax=32.0),
    ShellPin("OUT1", "output", "1", r_out=10, i_source=40e-3, i_sink=40e-3),
    ShellPin("IN1M", "input", "2", clamp="gnd", ibias=21e-9, vmax=32.0, vmin=-0.3),
    ShellPin("IN1P", "input", "3", clamp="gnd", ibias=19e-9, vmax=32.0, vmin=-0.3),
    ShellPin("VEE", "ground", "4"),
    ShellPin("IN2P", "input", "5", clamp="gnd", ibias=19e-9, vmax=32.0, vmin=-0.3),
    ShellPin("IN2M", "input", "6", clamp="gnd", ibias=21e-9, vmax=32.0, vmin=-0.3),
    ShellPin("OUT2", "output", "7", r_out=10, i_source=40e-3, i_sink=40e-3),
)


def lm358_text() -> str:
    core1, level1 = _opamp_channel(1, "IN1P", "IN1M")
    core2, level2 = _opamp_channel(2, "IN2P", "IN2M")
    return render_shell(
        "LM358",
        LM358_PINS,
        core=core1 + "\n" + core2,
        drives={"OUT1": level1, "OUT2": level2},
        title="LM358 dual op amp: pin shell + op-amp core",
    )


LM358_GATE = GateSpec(
    "LM358",
    "LM358",
    (
        GatePin("VCC", "supply", "8", vtest=5.0, alarms=("abs",), abs_fault_v=33.0),
        GatePin("OUT1", "output", "1", isc_max=0.060, alarms=("ovl",)),  # +-40 typ, +-60 mA max
        GatePin("IN1M", "input", "2", alarms=("abs",), abs_fault_v=33.0),
        GatePin("IN1P", "input", "3", alarms=("abs",), abs_fault_v=33.0),
        GatePin("VEE", "ground", "4"),
        GatePin("IN2P", "input", "5", alarms=("abs",), abs_fault_v=33.0),
        GatePin("IN2M", "input", "6", alarms=("abs",), abs_fault_v=33.0),
        GatePin("OUT2", "output", "7", isc_max=0.060, alarms=("ovl",)),
    ),
)

# --------------------------------------------------------------------------- #
# A microcontroller-class part with no function core: pins only. The numbers are round
# stand-ins for the shape of the problem, not a real device.

MCU8_PINS = (
    ShellPin("VDD", "supply", "1", iq=2e-3, vmax=4.0),
    ShellPin("GND", "ground", "2"),
    ShellPin("RESET", "input", "3", default="pullup", vmax_over_rail=0.3),
    ShellPin(
        "PA0", "io", "4", r_out=40, i_source=8e-3, i_sink=8e-3, vil=0.8, vih=2.0, vmax_over_rail=0.3
    ),
    ShellPin("PB0", "output", "5", r_out=40, i_source=8e-3, i_sink=8e-3),
    ShellPin("NC1", "nc", "6"),
    ShellPin("EP", "pad", "7", required=True),
)


def mcu8_text() -> str:
    return render_shell("MCU8", MCU8_PINS, title="MCU8: pin shell only")


MCU8_GATE = GateSpec(
    "MCU8",
    "MCU8",
    (
        GatePin("VDD", "supply", "1", vtest=3.3, alarms=("abs",), abs_fault_v=4.5),
        GatePin("GND", "ground", "2"),
        GatePin("RESET", "input", "3", alarms=("abs",), abs_fault_v=6.0),
        GatePin(
            "PA0",
            "io",
            "4",
            alarms=("abs", "flt", "ovl"),
            abs_fault_v=6.0,
            isc_max=8e-3,
            force=level_param("PA0"),
        ),
        GatePin("PB0", "output", "5", isc_max=8e-3, force=level_param("PB0"), alarms=("ovl",)),
        GatePin("NC1", "nc", "6"),
        GatePin("EP", "pad", "7", alarms=("tie",)),
    ),
)
