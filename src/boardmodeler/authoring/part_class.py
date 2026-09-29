"""Which parts this harness can honestly judge, and which it must refuse.

The model maker binds each datasheet row to a deterministic *analogue* probe --
voltage and current thresholds, regulation, the timing of analogue behaviour --
and judges the model with real LTspice runs. A firmware-defined part has no such
row: its datasheet speaks in clock trees, peripherals and instruction sets, so
every row an extraction could produce would be declared untestable and any model
that "passed" would be passing nothing. Authoring one there would spend agent
turns and simulator time on a verdict no probe can give, so the honest build
stops at the door.

The classifier is deliberately conservative, because a false refusal costs a
model that could have been built while an unrecognised digital part still fails
loudly later (extraction and binding report what they cannot do). A part is
refused only on a confident match:

* its part number starts an alphanumeric token of a written vendor family below
  (``STM32F407``, ``EP4CE6E22C8N``), or the part string names the family
  (``Cyclone V``); or
* the document's own text -- the datasheet's title -- names the class in as many
  words: "microcontroller", "FPGA", "CPLD", "SoC" or "processor core".

Everything else is ``analogue_ic`` and proceeds exactly as it does today: an
unrecognised part is never guessed at, and a weak signal is never a refusal. The
lists are closed and reviewed; adding a family means adding a line here and a
test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["PartClass", "PartKind", "classify"]

PartKind = Literal["analogue_ic", "microcontroller", "fpga", "unsupported"]
"""What a part was classified as. Only ``analogue_ic`` is supported."""

_MICROCONTROLLER: Final[PartKind] = "microcontroller"
_FPGA: Final[PartKind] = "fpga"
_UNSUPPORTED: Final[PartKind] = "unsupported"

#: Why no probe can judge each refused kind. The probes are the harness's whole
#: vocabulary, so the sentence names what they measure before what the part is.
_WHY: Final[dict[PartKind, str]] = {
    _MICROCONTROLLER: (
        "the probes measure analogue thresholds and regulation; a firmware-defined part "
        "has no datasheet row this harness can bind"
    ),
    _FPGA: (
        "the probes measure analogue thresholds and regulation; a programmable-logic part "
        "has no datasheet row this harness can bind"
    ),
    _UNSUPPORTED: (
        "the probes measure analogue thresholds and regulation; a processor-based part "
        "has no datasheet row this harness can bind"
    ),
}

_SUPPORTED_REASON: Final[str] = (
    "no microcontroller, FPGA, CPLD, SoC or processor-core family matched the part number or "
    "the document text"
)


@dataclass(frozen=True)
class _Family:
    """One written refusal rule: a vendor family and how to see it.

    ``prefixes`` are part-number prefixes matched at the start of any
    alphanumeric token of the part string, so ``stm32`` sees ``STM32F407`` and
    ``Xilinx STM32...`` alike. ``phrases`` are multi-word family names matched as
    whole words; only "MAX 10" needs one, because every other family name is also
    a single token and the token rule would match e.g. Maxim's ``MAX232``.
    """

    name: str
    kind: PartKind
    prefixes: tuple[re.Pattern[str], ...] = ()
    phrases: tuple[str, ...] = ()


def _family(
    name: str,
    kind: PartKind,
    *,
    prefixes: tuple[str, ...] = (),
    phrases: tuple[str, ...] = (),
) -> _Family:
    """One table row, with its prefix patterns compiled once at import."""
    return _Family(
        name=name, kind=kind, prefixes=tuple(re.compile(p) for p in prefixes), phrases=phrases
    )


#: The written refusal list, checked in order. Patterns are lowercase because
#: they are matched against casefolded part strings.
_FAMILIES: Final[tuple[_Family, ...]] = (
    # Microcontrollers: firmware defines the rows, so no probe can judge one.
    _family("STMicroelectronics STM32", _MICROCONTROLLER, prefixes=("stm32",)),
    _family(
        "Microchip/Atmel ATmega and ATtiny AVR", _MICROCONTROLLER, prefixes=("atmega", "attiny")
    ),
    _family("Microchip PIC", _MICROCONTROLLER, prefixes=(r"pic\d",)),
    _family("Texas Instruments MSP430", _MICROCONTROLLER, prefixes=("msp430",)),
    _family("Nordic Semiconductor nRF52", _MICROCONTROLLER, prefixes=("nrf52",)),
    _family("Espressif ESP32", _MICROCONTROLLER, prefixes=("esp32",)),
    _family("Raspberry Pi RP2040", _MICROCONTROLLER, prefixes=("rp2040",)),
    # Programmable logic: a bitstream, not a datasheet limit, defines the part.
    _family("Xilinx 7 series, Spartan and Virtex", _FPGA, prefixes=(r"xc[2-7][a-z]",)),
    _family("Xilinx UltraScale and UltraScale+", _FPGA, prefixes=(r"xc[a-z]{2}",)),
    _family(
        "Xilinx Artix, Kintex, Spartan, Virtex and Zynq",
        _FPGA,
        prefixes=("artix", "kintex", "spartan", "virtex", "zynq"),
    ),
    _family(
        "Intel/Altera Cyclone",
        _FPGA,
        prefixes=("cyclone", "ep1c", "ep2c", "ep3c", "ep4c", "5ce", "5cg", "5cs", "10cl"),
    ),
    _family(
        "Intel/Altera MAX",
        _FPGA,
        prefixes=("epm", "5m", "10m"),
        phrases=("max 10",),
    ),
    _family(
        "Intel/Altera Arria",
        _FPGA,
        prefixes=("arria", "ep1agx", "ep2ag", "5agx", "5ast", "10ax", "10as"),
    ),
    _family(
        "Intel/Altera Stratix",
        _FPGA,
        prefixes=("stratix", "ep1s", "ep2s", "ep3s", "ep4s", "5sgx", "5sgs"),
    ),
    _family("Lattice ECP5", _FPGA, prefixes=("ecp5", "lfe5u")),
    _family("Lattice iCE40", _FPGA, prefixes=("ice40",)),
    _family("Lattice MachXO", _FPGA, prefixes=("machxo", "lcmxo")),
    # Added with the support gate: more written families of parts the engine cannot represent.
    _family("NXP LPC and i.MX", _MICROCONTROLLER, prefixes=("lpc[0-9]", "imx[0-9]", "kinetis")),
    _family(
        "Microchip PIC32, dsPIC and SAM",
        _MICROCONTROLLER,
        prefixes=(
            "pic32",
            "dspic",
            "samd[0-9]",
            "same[0-9]",
            "samc[0-9]",
            "sam[3-9][0-9]",
            "atsam",
        ),
    ),
    _family("Renesas RA and RL78", _MICROCONTROLLER, prefixes=("r7fa[0-9]", "r5f[0-9]", "rl78")),
    _family(
        "Texas Instruments MCUs and wireless MCUs",
        _MICROCONTROLLER,
        prefixes=(
            "tms570",
            "tm4c[0-9]",
            "msp432",
            "cc13[0-9][0-9]",
            "cc26[0-9][0-9]",
            "cc32[0-9][0-9]",
        ),
    ),
    _family(
        "Texas Instruments DSPs and Sitara processors",
        _UNSUPPORTED,
        prefixes=("tms320", "am335", "am437", "am57[0-9][0-9]"),
    ),
    _family("STMicroelectronics STM8", _MICROCONTROLLER, prefixes=("stm8",)),
    _family("Nordic nRF51, nRF53 and nRF91", _MICROCONTROLLER, prefixes=("nrf5[13]", "nrf91")),
    _family("Silicon Labs EFM32 and EFR32", _MICROCONTROLLER, prefixes=("efm32", "efr32", "ezr32")),
    _family("Infineon XMC and PSoC", _MICROCONTROLLER, prefixes=("xmc[0-9]", "psoc", "cy8c")),
    _family("Espressif ESP8266", _MICROCONTROLLER, prefixes=("esp8266", "esp8285")),
    _family("GigaDevice GD32", _MICROCONTROLLER, prefixes=("gd32",)),
    _family("Gowin FPGAs", _FPGA, prefixes=("gw1n", "gw2a", "gw5a")),
    _family(
        "Microchip and Microsemi FPGAs",
        _FPGA,
        prefixes=("polarfire", "igloo", "smartfusion", "proasic"),
    ),
    _family("Intel Stratix, Arria and Agilex", _FPGA, prefixes=("stratix", "arria", "agilex")),
    _family("AMD Zynq", _UNSUPPORTED, prefixes=("zynq",)),
    # Added after a coverage scan of typed part numbers found these unrecognised. XC95xx is
    # written out number by number: Torex sells XC9504 and XC9104 step-down regulators, so no
    # shorter prefix is safe. The same care applies to TDA, MAX32 and CH3xx, which are left out.
    _family(
        "Xilinx XC9500 CPLDs",
        _FPGA,
        prefixes=("xc9536", "xc9572", "xc95108", "xc95144", "xc95216", "xc95288"),
    ),
    _family(
        "Lattice ECP, ECP2, ECP3, CrossLink and ispMACH",
        _FPGA,
        prefixes=("lfxp", "lfec", "lfe[23]", "lifcl", "lc4[0-9][0-9][0-9]", "ispmach"),
    ),
    _family(
        "Atmel/Microchip programmable logic",
        _FPGA,
        prefixes=(
            "atf15[0-9][0-9]",
            "atf16v8",
            "atf22v10",
            "atf750",
            "gal16v8",
            "gal22v10",
            "gal20v8",
        ),
    ),
    _family(
        "Microchip/Microsemi PolarFire, SmartFusion2, IGLOO2 and ProASIC3",
        _FPGA,
        prefixes=("mpf[0-9]", "m2s[0-9]", "m2gl[0-9]", "a3p[0-9]", "agl[0-9]"),
    ),
    _family(
        "Broadcom Raspberry Pi SoCs", _UNSUPPORTED, prefixes=("bcm27[0-9][0-9]", "bcm28[0-9][0-9]")
    ),
    _family(
        "Rockchip, Qualcomm, Samsung and NVIDIA application SoCs",
        _UNSUPPORTED,
        prefixes=(
            "rk3[0-9][0-9][0-9]",
            "rk35",
            "exynos",
            "snapdragon",
            "tegra",
            "msm8",
            "sdm[0-9]",
        ),
    ),
    _family(
        "Texas Instruments Sitara AM6 and OMAP processors",
        _UNSUPPORTED,
        prefixes=("am6[2-9]", "omap"),
    ),
    _family(
        "NXP Kinetis, S32K, i.MX RT and PowerPC MCUs",
        _MICROCONTROLLER,
        prefixes=(
            "mk[0-9][0-9][a-z]",
            "mkl[0-9]",
            "mke[0-9]",
            "mkv[0-9]",
            "mkw[0-9]",
            "mimx",
            "s32k",
            "mc9s",
            "mc56f",
            "mpc5[5-7]",
        ),
    ),
    _family(
        "Atmel AT89, AT91, AT32 and XMEGA",
        _MICROCONTROLLER,
        prefixes=("at89", "at91", "at32", "atxmega", "avr[0-9]"),
    ),
    _family(
        "WCH, Nuvoton, STC, Puya, Padauk and Silicon Labs 8051 MCUs",
        _MICROCONTROLLER,
        prefixes=(
            "ch32",
            "ch55[0-9]",
            "ch57[0-9]",
            "ch58[0-9]",
            "nuc[0-9]",
            "n76e",
            "ms51",
            "stc8",
            "stc12",
            "stc15",
            "stc89",
            "py32",
            "pms[0-9]",
            "pfs[0-9]",
            "c8051",
            "efm8",
        ),
    ),
)

#: Digital-only words in the document's own text (its title/keywords). Each is a
#: whole word -- the optional plural covers the usual "Microcontrollers" heading
#: -- and the boundaries keep e.g. "assoc" or "Socionext" out. "SoC" and
#: "processor core" are refused as plainly unsupported rather than guessed into
#: one of the two families above.
_TEXT_RULES: Final[tuple[tuple[re.Pattern[str], PartKind, str], ...]] = (
    (
        re.compile(r"(?<![a-z0-9])microcontrollers?(?![a-z0-9])"),
        _MICROCONTROLLER,
        "a microcontroller",
    ),
    (re.compile(r"(?<![a-z0-9])fpgas?(?![a-z0-9])"), _FPGA, "an FPGA"),
    (
        re.compile("(^|[^a-z0-9])field[- ]programmable gate arrays?($|[^a-z0-9])"),
        _FPGA,
        "an FPGA",
    ),
    (
        re.compile("(^|[^a-z0-9])complex programmable logic devices?($|[^a-z0-9])"),
        _FPGA,
        "a CPLD",
    ),
    (
        re.compile("(^|[^a-z0-9])programmable logic devices?($|[^a-z0-9])"),
        _FPGA,
        "a programmable logic device",
    ),
    (
        re.compile("(^|[^a-z0-9])micro-?computers?($|[^a-z0-9])"),
        _MICROCONTROLLER,
        "a microcomputer",
    ),
    (re.compile(r"(?<![a-z0-9])cplds?(?![a-z0-9])"), _FPGA, "a CPLD"),
    (re.compile(r"(?<![a-z0-9])socs?(?![a-z0-9])"), _UNSUPPORTED, "a SoC"),
    (
        re.compile(r"(?<![a-z0-9])processor cores?(?![a-z0-9])"),
        _UNSUPPORTED,
        "a processor core",
    ),
    (
        re.compile("(^|[^a-z0-9])systems?[- ]on[- ](a[- ])?(chip|module)s?($|[^a-z0-9])"),
        _UNSUPPORTED,
        "a system-on-chip or system-on-module",
    ),
    (
        re.compile(
            "(^|[^a-z0-9])(application|media|network|graphics|digital signal) processors?($|[^a-z0-9])"
        ),
        _UNSUPPORTED,
        "a processor",
    ),
    (
        re.compile("(^|[^a-z0-9])single[- ]board computers?($|[^a-z0-9])"),
        _UNSUPPORTED,
        "a single-board computer",
    ),
)


@dataclass(frozen=True)
class PartClass:
    """What the classifier decided about one part.

    ``kind`` is the family the part belongs to; it is ``analogue_ic`` when
    nothing matched, which is the default the rest of the pipeline has always
    assumed. ``supported`` says whether a model build may proceed. ``reason``
    carries the evidence and the harness's own limitation: for a refusal it
    names the match and why no probe can judge the part, and for a pass it says
    what was checked.
    """

    kind: PartKind
    reason: str
    supported: bool

    @property
    def detail(self) -> str:
        """The BLOCKED detail a refusal carries: ``unsupported_part_class: <kind>: <why>``."""
        return f"unsupported_part_class: {self.kind}: {self.reason}"


def _mentions(text: str, phrase: str) -> bool:
    """Whether ``phrase`` appears in ``text`` as a whole word (neighbours not alphanumeric)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _match_part(part: str) -> tuple[str, PartKind] | None:
    """The first written family ``part`` matches, as ``(evidence, kind)``."""
    compact = " ".join(part.split()).casefold()
    tokens = re.findall(r"[a-z0-9]+", compact)
    for family in _FAMILIES:
        hit = any(pattern.match(token) for token in tokens for pattern in family.prefixes)
        if not hit and family.phrases:
            hit = any(_mentions(compact, phrase) for phrase in family.phrases)
        if hit:
            return f"the part number matches the {family.name} family", family.kind
    return None


def _match_text(text: str) -> tuple[str, PartKind] | None:
    """The first digital-only keyword in ``text``, as ``(evidence, kind)``."""
    normalized = " ".join(text.split()).casefold()
    for pattern, kind, label in _TEXT_RULES:
        if pattern.search(normalized):
            return f"the document text describes {label}", kind
    return None


def classify(part: str, *, text: str = "") -> PartClass:
    """Classify ``part``, with the document's own ``text`` as the second signal.

    The part number decides first: one of the written families above is enough
    on its own. Only when no family matches the number is ``text`` (the
    datasheet's title, where the caller has one) consulted, and only the explicit
    keyword table counts. A part that matches neither stays ``analogue_ic`` and
    supported -- a refusal needs evidence, never a hunch.
    """
    matched = _match_part(part) or _match_text(text)
    if matched is None:
        return PartClass(kind="analogue_ic", reason=_SUPPORTED_REASON, supported=True)
    evidence, kind = matched
    return PartClass(kind=kind, reason=f"{evidence}; {_WHY[kind]}", supported=False)
