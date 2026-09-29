"""A synthetic dual op amp spec: eight named pins and probe-bound typical rows.

The frozen LM358 rows are local and git-ignored, so the offline tests of the op amp implementation
run on this stand-in instead. Every row says it is synthetic; nothing here is a datasheet number
for a real part except that the values are round figures of the LM358 class.
"""

from __future__ import annotations

from boardmodeler.authoring.spec import Characteristic, SpecSet

PIN_MAP = tuple(
    {"name": name, "physical_pin": str(number)}
    for number, name in enumerate(
        ("OUT1", "IN1M", "IN1P", "VEE", "IN2P", "IN2M", "OUT2", "VCC"), start=1
    )
)

#: (id, probe, unit, typical value, extra probe parameters)
_ROWS = (
    ("VOS", "opamp_offset", "V", 3e-3, {}),
    ("IB", "opamp_bias", "A", 20e-9, {}),
    ("IOS", "opamp_offset_current", "A", 2e-9, {}),
    ("AOL", "opamp_gain", "V/V", 1e5, {"op_load": 2000.0, "op_vcc": 15.0, "op_vout": 6.0}),
    ("GBW", "opamp_gbw", "Hz", 700e3, {}),
    ("SR_RISE", "opamp_slew_rise", "V/s", 300e3, {}),
    ("SR_FALL", "opamp_slew_fall", "V/s", 300e3, {}),
    ("VOH", "opamp_swing_high", "V", 2.0, {"op_load": 10000.0, "op_vcc": 30.0}),
    ("VOL", "opamp_swing_low", "V", 5e-3, {"op_load": 10000.0}),
    ("IQ", "opamp_quiescent", "A", 350e-6, {}),
)


_WORDS = {
    "VOS": "Input offset voltage",
    "IB": "Input bias current",
    "IOS": "Input offset current",
    "AOL": "Open-loop gain",
    "GBW": "Gain bandwidth product",
    "SR_RISE": "Slew rate, rising output",
    "SR_FALL": "Slew rate, falling output",
    "VOH": "Output voltage swing, high headroom",
    "VOL": "Output voltage swing, low",
    "IQ": "Supply current per amplifier",
}


def _row(name: str, probe: str, unit: str, typical: float, params: dict[str, float]):
    return Characteristic(
        char_id=f"SYN_{name}_TYP_CH1",
        statement=f"{_WORDS[name]}, typical, amplifier 1 (synthetic fixture row)",
        unit=unit,
        min_value=None,
        max_value=None,
        typ_value=typical,
        target=typical,
        source_page=1,
        excerpt=f"SYNTHETIC FIXTURE ROW {name} typ {typical:g} {unit}",
        req_class="TYPICAL_VALUE",
        probe=probe,
        probe_params={"op_channel": 1.0, "temp_c": 25.0, **params},
        not_testable_reason=None,
    )


def synthetic_dual_op_amp_spec(part: str = "SYNOPA") -> SpecSet:
    return SpecSet(
        part=part,
        subckt=part,
        doc_id="DOC_SYNTHETIC_OPAMP",
        characteristics=tuple(_row(*row) for row in _ROWS),
        pin_map=PIN_MAP,
    )
