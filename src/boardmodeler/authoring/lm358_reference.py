"""Reviewed TI LM358 extraction recipe, tied to the exact datasheet.

This is a reviewed extraction cache, not a device model. The selected agent still
authors the model and the simulator judges it. An LM358B or a different source
datasheet cannot match this recipe.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from boardmodeler.authoring.opamp_probes import PORTS
from boardmodeler.domain.records import Requirement

DATASHEET_SHA256 = "58c89c68aff6b6025555f3b8bad9af6a825288cd96631deadc1c616a6062af46"
PRODUCT_URL = "https://www.ti.com/product/LM358"


def matches(part: str, file_hash: str) -> bool:
    return part.strip().upper() == "LM358" and file_hash == DATASHEET_SHA256


def available(part: str, datasheet: Path) -> bool:
    return part.strip().upper() == "LM358" and matches(
        part, hashlib.sha256(datasheet.read_bytes()).hexdigest()
    )


def records(record, page_text: str):
    """Human-reviewed table 5.7 at PDF page 9; citations still checked by the pipeline."""
    rows = []
    bindings = []

    def add(
        name,
        statement,
        anchor,
        end,
        limits=None,
        probe=None,
        params=None,
        channel=None,
        reason=None,
    ):
        start = page_text.index(anchor)
        excerpt = page_text[start : page_text.index(end, start)] if end else anchor
        if len(excerpt) > 400:
            raise ValueError(f"reference citation too long: {name}")
        rid = f"LM358_{name}" + (f"_CH{channel}" if channel else "")
        params = {"temp_c": 25, **(params or {})}
        if channel:
            params["op_channel"] = channel
            statement += f"; amplifier {channel}"
        data = {
            "req_id": rid,
            "applies_to": "LM358",
            "kind": "ELECTRICAL",
            "class": ("TYPICAL_VALUE" if limits and "typ" in limits else "DOCUMENTED_LIMIT")
            if limits
            else "UNKNOWN",
            "criticality": "IMPORTANT",
            "origin": "DOCUMENT",
            "statement": statement,
            "conditions": [
                {
                    "text": "Evaluated at 25 C only; the recorded probe parameters define the sampled operating point.",
                    "parameter_overrides": params if probe else {},
                }
            ],
            "evidence": [
                {
                    "doc_id": record.doc_id,
                    "page": {"pdf_page": 9},
                    "excerpt": excerpt,
                    "extraction": "embedded_text",
                }
            ],
        }
        if limits:
            data["limits"] = limits
        rows.append(Requirement.model_validate(data))
        bindings.append(
            {
                "req_id": rid,
                "probe": probe,
                **(
                    {"params": params}
                    if probe
                    else {
                        "not_testable_reason": reason
                        or "No qualified probe for this characteristic or its full operating range."
                    }
                ),
            }
        )

    for channel in (1, 2):
        add(
            "VOS",
            "Input offset voltage magnitude at VS=5 V, VCM near 0 V, VO=1.4 V",
            "VOS Input offset voltage",
            "dVOS/dT",
            {"max": 7e-3, "unit": "V"},
            "opamp_offset",
            channel=channel,
        )
        add(
            "IB",
            "Input bias current magnitude at VS=5 V, VO=1.4 V",
            "IB Input bias current",
            "IOS Input",
            {"max": 250e-9, "unit": "A"},
            "opamp_bias",
            channel=channel,
        )
        add(
            "IOS",
            "Input offset current magnitude at VS=5 V, VO=1.4 V",
            "IOS Input offset current",
            "dIOS/dT",
            {"max": 50e-9, "unit": "A"},
            "opamp_offset_current",
            channel=channel,
        )
        add(
            "AOL",
            "Open-loop gain at VS=15 V, VO=6 V, RL=2 kohm; one point in the specified output range",
            "AOL Open-loop voltage gain",
            "FREQUENCY RESPONSE",
            {"min": 25000, "unit": "V/V"},
            "opamp_gain",
            {"op_vcc": 15, "op_vout": 6, "op_load": 2000},
            channel,
        )
        add(
            "GBW",
            "Typical gain bandwidth product at VS=5 V",
            "GBW Gain bandwidth product",
            "SR Slew",
            {"typ": 700000, "unit": "Hz"},
            "opamp_gbw",
            channel=channel,
        )
        for edge in ("rise", "fall"):
            add(
                "SR_" + edge.upper(),
                "Typical unity-gain slew rate, " + edge + ", 1 V to 3 V step at VS=5 V",
                "SR Slew rate",
                "OUTPUT",
                {"typ": 300000, "unit": "V/s"},
                "opamp_slew_" + edge,
                channel=channel,
            )
        add(
            "VOH",
            "Positive output headroom at VS=30 V, RL=10 kohm",
            "VO Voltage output swing",
            "IO Output current",
            {"max": 3, "unit": "V"},
            "opamp_swing_high",
            {"op_vcc": 30, "op_load": 10000},
            channel,
        )
        add(
            "VOL",
            "Low output voltage at VS=5 V, RL=10 kohm; 25 C only",
            "VO Voltage output swing",
            "IO Output current",
            {"max": 0.02, "unit": "V"},
            "opamp_swing_low",
            {"op_load": 10000},
            channel,
        )
        for name, statement, anchor, end, value, unit, probe, params in (
            (
                "VOS_TYP",
                "Typical offset magnitude",
                "VOS Input offset voltage",
                "dVOS/dT",
                0.003,
                "V",
                "offset",
                {},
            ),
            (
                "IB_TYP",
                "Typical input bias current magnitude (current flows out of the input pins)",
                "IB Input bias current",
                "IOS Input",
                20e-9,
                "A",
                "bias",
                {},
            ),
            (
                "IOS_TYP",
                "Typical input offset current magnitude",
                "IOS Input offset current",
                "dIOS/dT",
                2e-9,
                "A",
                "offset_current",
                {},
            ),
            (
                "AOL_TYP",
                "Typical open-loop gain at VS=15 V, VO=6 V, RL=2 kohm",
                "AOL Open-loop voltage gain",
                "FREQUENCY RESPONSE",
                100000,
                "V/V",
                "gain",
                {"op_vcc": 15, "op_vout": 6, "op_load": 2000},
            ),
            (
                "VOH_TYP",
                "Typical high-output headroom at VS=30 V, RL=10 kohm",
                "VO Voltage output swing",
                "IO Output current",
                2,
                "V",
                "swing_high",
                {"op_vcc": 30, "op_load": 10000},
            ),
            (
                "VOL_TYP",
                "Typical low output at VS=5 V, RL=10 kohm",
                "VO Voltage output swing",
                "IO Output current",
                0.005,
                "V",
                "swing_low",
                {"op_load": 10000},
            ),
        ):
            add(
                name,
                statement,
                anchor,
                end,
                {"typ": value, "unit": unit},
                "opamp_" + probe,
                params,
                channel,
            )
    add(
        "IQ",
        "No-load supply current per amplifier (dual total divided by two), VS=5 V, VO=2.5 V; 25 C only",
        "IQ Quiescent current",
        "(1) All characteristics",
        {"max": 600e-6, "unit": "A"},
        "opamp_quiescent",
    )
    add(
        "IQ_TYP",
        "Typical no-load supply current per amplifier at VS=5 V, VO=2.5 V",
        "IQ Quiescent current",
        "(1) All characteristics",
        {"typ": 350e-6, "unit": "A"},
        "opamp_quiescent",
    )
    for name, statement, anchor, end in (
        (
            "DRIFT",
            "Offset voltage drift and temperature limits",
            "dVOS/dT Input offset voltage drift",
            "PSRR",
        ),
        (
            "PSRR",
            "Power supply rejection across the specified supply range",
            "PSRR Input offset",
            "VO1/ VO2",
        ),
        (
            "SEPARATION",
            "Channel separation from 1 kHz to 20 kHz",
            "VO1/ VO2",
            "INPUT VOLTAGE RANGE",
        ),
        (
            "VCM",
            "Full common-mode input range including temperature dependence",
            "VCM Common-mode",
            "CMRR",
        ),
        ("CMRR", "Common-mode rejection ratio", "CMRR Common-mode", "INPUT BIAS"),
        ("IOS_DRIFT", "Input offset current temperature drift", "dIOS/dT", "NOISE"),
        ("NOISE", "Input voltage noise density", "en Input voltage noise", "OPEN-LOOP"),
        (
            "IO",
            "Output sourcing and sinking current across the specified conditions",
            "IO Output current",
            "ISC Short-circuit",
        ),
        ("ISC", "Short-circuit output current", "ISC Short-circuit", "POWER SUPPLY"),
        (
            "OTHER_POINTS",
            "All supply/load/temperature corners outside the recorded nominal samples",
            "(1) All characteristics",
            None,
        ),
    ):
        add(name, statement, anchor, end)
    pins = tuple({"physical_pin": str(i + 1), "name": name} for i, name in enumerate(PORTS))
    return rows, bindings, pins
