"""Reviewed, partial evidence for the exact TI UCC28251 SLUSBD8E PDF.

The physical package must be named. Table columns and functional statements are
reviewed evidence, not candidate-derived limits. No provider or OCR runs here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from boardmodeler.documents.pdf import normalize_for_citation
from boardmodeler.domain.records import DocumentRecord, Requirement

DATASHEET_SHA256 = "81a4414e6f2d7de81398bba5f7ceffb2f4f117dbc005a8c9e157b372646dc602"
SOURCE_URL = "https://www.ti.com/lit/ds/symlink/ucc28251.pdf"
PROFILE_ID = "ti-ucc28251-slusbd8e-first-order-v1"
PAGES = (3, 4, 5, 6, 7, 12, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 25, 28)
PW_NAMES = (
    "VSENSE",
    "RT",
    "RAMP/CS",
    "ILIM",
    "EN",
    "OVP/OTP",
    "VREF",
    "REF/EA+",
    "FB/EA-",
    "COMP",
    "GND",
    "VDD",
    "SRB",
    "SRA",
    "OUTB",
    "OUTA",
    "HICC",
    "PS",
    "SP",
    "SS",
)
RGP_NAMES = (
    "REF/EA+",
    "FB/EA-",
    "COMP",
    "GND",
    "VDD",
    "SRB",
    "SRA",
    "OUTB",
    "OUTA",
    "HICC",
    "PS",
    "SP",
    "SS",
    "VSENSE",
    "RT",
    "RAMP/CS",
    "ILIM",
    "EN",
    "OVP/OTP",
    "VREF",
)
PART_PACKAGES = {
    "UCC28251PW": "PW",
    "UCC28251PWR": "PW",
    "UCC28251RGP": "RGP",
    "UCC28251RGPR": "RGP",
    "UCC28251RGPT": "RGP",
}

# name, page (zero based), statement, exact normalized source excerpt,
# minimum/typical/maximum in SI units, unit, probe, additional bench parameters.
ROWS = (
    (
        "VREF",
        6,
        "Reference output voltage at VDD=12 V within the cited 0-to-10-mA load range",
        "VVREF Output voltage VDD = from 7 V to 17 V, I VREF = 2 mA 3.17 3.25 3.33 V",
        3.17,
        3.25,
        3.33,
        "V",
        "vref",
        {},
    ),
    (
        "FREQUENCY",
        6,
        "Switching frequency at each primary output, RT=75 kohm, SP=20 kohm",
        "RT/SYNC = 75 kΩ, R SP = 20 kΩ 90 98 106 kHz",
        90e3,
        98e3,
        106e3,
        "Hz",
        "frequency",
        {},
    ),
    (
        "UVLO_RISE",
        5,
        "VDD undervoltage lockout start threshold",
        "VUVLOR Start threshold 4.00 4.30 4.65",
        4.0,
        4.3,
        4.65,
        "V",
        "uvlo_rise",
        {},
    ),
    (
        "UVLO_FALL",
        5,
        "VDD undervoltage lockout falling threshold",
        "VVUVLOF Minimum operating voltage after start 3.8 4.1 4.4",
        3.8,
        4.1,
        4.4,
        "V",
        "uvlo_fall",
        {},
    ),
    (
        "EN_THRESHOLD",
        5,
        "Level enable trigger threshold",
        "Trigger threshold 1.5 2.0 2.25 V",
        1.5,
        2.0,
        2.25,
        "V",
        "enable",
        {},
    ),
    (
        "ISS",
        5,
        "Soft-start charge current at SS=0 V; electrical table nominal",
        "ISS Soft-start charge current V SS = 0 V 26 28 30 µA",
        26e-6,
        28e-6,
        30e-6,
        "A",
        "ss_charge",
        {},
    ),
    (
        "SS_CLAMP",
        5,
        "Soft-start clamp voltage",
        "VSS(max) Clamp voltage 3.3 3.6 4.0 V",
        3.3,
        3.6,
        4.0,
        "V",
        None,
        {},
    ),
    (
        "ILIM",
        6,
        "ILIM cycle-by-cycle current sense voltage threshold",
        "VILIM ILIM cycle-by-cycle threshold 0.497 0.505 0.513 V",
        0.497,
        0.505,
        0.513,
        "V",
        "ilim",
        {},
    ),
    (
        "ILIM_DELAY",
        6,
        "Current limit propagation delay excluding leading-edge blanking",
        "Exclude leading edge blanking (UCC28251PW) 12.3 25.0 38.7",
        12.3e-9,
        25e-9,
        38.7e-9,
        "s",
        "ilim_delay",
        {"delay_min_s": 12.3e-9, "delay_max_s": 38.7e-9},
    ),
    (
        "BLANK",
        6,
        "Current limit leading-edge blanking",
        "TBLANK Leading edge blanking 35 60 90",
        35e-9,
        60e-9,
        90e-9,
        "s",
        None,
        {},
    ),
    (
        "OVP",
        7,
        "OVP/OTP comparator shutdown threshold",
        "VOVP Internal reference 0.66 0.70 0.74 V",
        0.66,
        0.70,
        0.74,
        "V",
        "ovp",
        {},
    ),
    (
        "OVP_CURRENT",
        7,
        "OVP/OTP internal switched current",
        "IOVP Internal current 6.0 8.5 11.0 µA",
        6e-6,
        8.5e-6,
        11e-6,
        "A",
        None,
        {},
    ),
    (
        "RSRC",
        7,
        "Primary output source resistance at 20-mA load and VDD=12 V",
        "RSRC Output source resistance I OUT = 20 mA 12 20 35 Ω",
        12,
        20,
        35,
        "ohm",
        "source_resistance",
        {},
    ),
    (
        "RSNK",
        7,
        "Primary output sink resistance at 20-mA load and VDD=12 V",
        "RSNK Output sink resistance I OUT = 20 mA 4 12 30",
        4,
        12,
        30,
        "ohm",
        "sink_resistance",
        {},
    ),
    (
        "SP_DELAY",
        7,
        "Synchronous rectifier off to primary on delay at SP=20 kohm, 25 C",
        "SP = 20 kΩ, 25 °C 39 43 48",
        39e-9,
        43e-9,
        48e-9,
        "s",
        "sp_delay",
        {},
    ),
    (
        "PS_DELAY",
        7,
        "Primary off to synchronous rectifier on delay at PS=27 kohm, 25 C",
        "PS = 27 kΩ, 25 °C 32 38 43",
        32e-9,
        38e-9,
        43e-9,
        "s",
        "ps_delay",
        {},
    ),
    (
        "COMP_HIGH",
        5,
        "Error amplifier high-level COMP voltage",
        "High-level COMP voltage 2.8 3 V",
        2.8,
        3.0,
        None,
        "V",
        None,
        {},
    ),
    (
        "COMP_LOW",
        5,
        "Error amplifier low-level COMP voltage",
        "Low-level COMP voltage 0.3 0.4",
        None,
        0.3,
        0.4,
        "V",
        None,
        {},
    ),
    (
        "COMP_START",
        28,
        "Typical primary PWM start offset from application prose; not a guaranteed component specification",
        "The primary outputs begin to switch when COMP pin voltage is above the 420 mV internal offset.",
        None,
        0.420,
        None,
        "V",
        None,
        {},
    ),
    (
        "EA_GAIN",
        5,
        "Error amplifier open-loop gain",
        "Open loop gain 70 100 dB",
        70,
        100,
        None,
        "dB",
        None,
        {},
    ),
    (
        "EA_SOURCE",
        5,
        "COMP source current",
        "ICOMP(src) COMP source current 2.0 4.5 8.0",
        2e-3,
        4.5e-3,
        8e-3,
        "A",
        None,
        {},
    ),
    (
        "EA_SINK",
        5,
        "COMP sink current",
        "ICOMP(snk) COMP sink current 3.0 6.5 9.0 mA",
        3e-3,
        6.5e-3,
        9e-3,
        "A",
        None,
        {},
    ),
    (
        "IQ",
        5,
        "Operating supply current with 100-pF load on all four outputs",
        "IDD Operating supply current 100-pF capacitor on OUTA, OUTB, SRA and SRB 1.2 2.0 2.5 mA",
        1.2e-3,
        2e-3,
        2.5e-3,
        "A",
        None,
        {},
    ),
    (
        "IQ_STANDBY",
        5,
        "Standby supply current with EN=0 V",
        "IDD(dis) Standby current EN = 0 V 250 425 600 µA",
        250e-6,
        425e-6,
        600e-6,
        "A",
        None,
        {},
    ),
    (
        "IQ_START",
        5,
        "Startup supply current at VDD=3.6 V",
        "IDD(off) Startup current VDD = 3.6 V 150 275 µA",
        None,
        150e-6,
        275e-6,
        "A",
        None,
        {},
    ),
    (
        "REF_LIMIT",
        6,
        "Reference source short-circuit current at VREF=3 V",
        "Short circuit current V REF = 3 V, T J = 25 °C 12 25 40 mA",
        12e-3,
        25e-3,
        40e-3,
        "A",
        None,
        {},
    ),
    (
        "RT_C",
        16,
        "Timing coefficient in oscillator programming equation 3",
        "66.4pF",
        None,
        66.4e-12,
        None,
        "F",
        None,
        {},
    ),
    (
        "START_DELAY",
        12,
        "Typical startup delay after VDD reaches UVLO",
        "UCC28251 startup is delayed by 20 µs typical after",
        None,
        20e-6,
        None,
        "s",
        None,
        {},
    ),
    (
        "REF_READY",
        14,
        "Reference ready level before enable initiation",
        "at the VREF pin is 2.4 V (typical) for at least 20 us",
        None,
        2.4,
        None,
        "V",
        None,
        {},
    ),
    (
        "SS_DISCHARGE",
        23,
        "Internal soft-start discharge switch on resistance",
        "an internal switch with 2 kΩon resistance",
        None,
        2e3,
        None,
        "ohm",
        None,
        {},
    ),
    (
        "RAMP_RESET",
        19,
        "Internal ramp reset switch on resistance",
        "approximately 40-Ωon-resistance",
        None,
        40,
        None,
        "ohm",
        None,
        {},
    ),
    (
        "RAMP_BLANK",
        19,
        "Ramp discharge pulse adds 70 ns to SP dead time",
        "(T D(sp) + 70 ns)",
        None,
        70e-9,
        None,
        "s",
        None,
        {},
    ),
    (
        "RAMP_CLAMP",
        6,
        "RAMP/CS clamp voltage",
        "10-V ramp charging voltage source with 40-kΩ current limiting resistor 3.5 4.0 4.5",
        3.5,
        4.0,
        4.5,
        "V",
        None,
        {},
    ),
    (
        "OUTPUT_LIMIT",
        4,
        "Primary switching outputs source and sink 0.2 A",
        "OUTA O 0.2-A sink/source primary switching output.",
        None,
        0.2,
        None,
        "A",
        None,
        {},
    ),
)

GAPS = (
    (
        "SYNC",
        16,
        "The UCC28251 can be synchronized to an external clock",
        "External clock synchronization is outside this first-order resistor-timed model.",
    ),
    (
        "PULSE_ENABLE",
        15,
        "A pulse signal may also be applied to the EN pin.",
        "Pulse-enable toggling is outside the level-enable operating domain.",
    ),
    (
        "PREBIAS",
        22,
        "optimal pre-biased start up performance.",
        "The secondary-side VSENSE prebias servo is not represented; use primary-side VSENSE-to-VREF configuration.",
    ),
    (
        "HICCUP",
        24,
        "controller goes into hiccup cycle",
        "HICC timing and matched duty-cycle recovery are not represented by this first-order controller.",
    ),
    (
        "THERMAL",
        14,
        "Junction temperature is below the thermal shutdown threshold",
        "No junction-temperature state or temperature-dependent model is implemented.",
    ),
    (
        "EDGE_TIME",
        7,
        "Rise/fall time C LOAD = 100 pF 8 ns",
        "Finite driver resistance is represented; transistor switching edge fidelity is not qualified.",
    ),
    (
        "SR_STARTUP",
        28,
        "an internal ramp is a fixed ramp with 3-V peak voltage.",
        "Primary-side SR startup ramp and SS=2.9-V handover are omitted; complementary SR outputs are qualified only after settled startup.",
    ),
    (
        "MINIMUM_PULSE",
        28,
        "the minimum pulse width for the primary-side OUTA and OUTB is typically 100 ns.",
        "Typical 100-ns primary minimum pulse width is not reproduced; blanking and finite driver effects are represented separately.",
    ),
)


def matches(part: str, file_hash: str) -> bool:
    return part.strip().upper() in PART_PACKAGES and file_hash == DATASHEET_SHA256


def records(record: DocumentRecord, pages: Mapping[int, str], *, part: str):
    """Return cited rows, frozen probe bindings and the explicitly selected package pins."""
    name = part.strip().upper()
    if not matches(name, record.file_hash):
        raise ValueError("ucc28251_reference_mismatch: exact reviewed PDF and package required")
    texts = {page: normalize_for_citation(pages.get(page, "")) for page in PAGES}

    def citation(page: int, excerpt: str) -> dict[str, Any]:
        normalized = normalize_for_citation(excerpt)
        if normalized not in texts[page]:
            raise ValueError(f"ucc28251_citation_missing: printed page {page + 1}: {excerpt}")
        return {
            "doc_id": record.doc_id,
            "page": {"pdf_page": page, "printed_label": str(page + 1)},
            "excerpt": normalized,
            "extraction": "embedded_text",
        }

    requirements = []
    bindings = []
    source_columns = {}
    for rid, page, statement, excerpt, minimum, typical, maximum, unit, probe, params in ROWS:
        if rid == "ILIM_DELAY" and PART_PACKAGES[name] == "RGP":
            excerpt = "Exclude leading edge blanking (UCC28251RGP) 15 25 36 ns"
            minimum, maximum = 15e-9, 36e-9
            params = {"delay_min_s": 15e-9, "delay_max_s": 36e-9}
        identifier = f"UCC28251_{rid}"
        limits = {"min": minimum, "typ": typical, "max": maximum, "unit": unit}
        row_evidence = [citation(page, excerpt)]
        if rid == "VREF":
            row_evidence.append(citation(6, "0 < I REF < 10 mA 3.17 3.25 3.33"))
        requirements.append(
            Requirement.model_validate(
                {
                    "req_id": identifier,
                    "applies_to": name,
                    "kind": "ELECTRICAL",
                    "class": "DOCUMENTED_LIMIT"
                    if minimum is not None or maximum is not None
                    else "TYPICAL_VALUE",
                    "criticality": "IMPORTANT",
                    "origin": "DOCUMENT",
                    "statement": statement,
                    "conditions": [
                        {
                            "text": "25 C, primary-side, level enable; VDD=12 V, RT=75 kohm, SP=20 kohm, PS=27 kohm unless the statement specifies a swept stimulus."
                        }
                    ],
                    "limits": limits,
                    "evidence": row_evidence,
                }
            )
        )
        source_columns[identifier] = limits
        bindings.append(
            {
                "req_id": identifier,
                "probe": f"pwm_{probe}" if probe else None,
                **(
                    {"params": {"temp_c": 25, **params}}
                    if probe
                    else {
                        "not_testable_reason": "Used as cited design evidence; no independently qualified measurement of this row in the first-order domain."
                    }
                ),
            }
        )

    for rid, page, excerpt, reason in GAPS:
        identifier = f"UCC28251_{rid}"
        requirements.append(
            Requirement.model_validate(
                {
                    "req_id": identifier,
                    "applies_to": name,
                    "kind": "ELECTRICAL",
                    "class": "UNKNOWN",
                    "criticality": "IMPORTANT",
                    "origin": "DOCUMENT",
                    "statement": reason,
                    "evidence": [citation(page, excerpt)],
                }
            )
        )
        bindings.append({"req_id": identifier, "probe": None, "not_testable_reason": reason})

    # These assertions encode documented relationships, never a candidate parameter.
    for rid, probe, page, excerpt, statement in (
        (
            "ALTERNATING",
            "alternating",
            20,
            "OUTA and OUTB are turned on by the internal clock signal",
            "Primary pulses alternate without overlap, including same-channel primary/SR interlocks; observed violation count must be zero.",
        ),
        (
            "DUTY_RESPONSE",
            "duty_response",
            21,
            "a higher COMP pin voltage results in a larger duty cycle",
            "Higher COMP gives greater primary duty; observed violation count must be zero.",
        ),
    ):
        identifier = f"UCC28251_{rid}"
        assertion_evidence = [citation(page, excerpt)]
        if rid == "ALTERNATING":
            assertion_evidence.extend(
                [
                    citation(
                        16,
                        "Each output (OUTA, OUTB, SRA, SRB) switches at half the oscillator frequency",
                    ),
                    citation(
                        28,
                        "complementary to the primary-side duty cycle, without considering the dead time between primary-side switch and secondary-side SR.",
                    ),
                ]
            )
        requirements.append(
            Requirement.model_validate(
                {
                    "req_id": identifier,
                    "applies_to": name,
                    "kind": "ELECTRICAL",
                    "class": "DOCUMENTED_LIMIT",
                    "criticality": "IMPORTANT",
                    "origin": "DOCUMENT",
                    "statement": statement,
                    "limits": {"max": 0.0, "unit": "ratio"},
                    "evidence": assertion_evidence,
                    "conditions": [
                        {
                            "text": "Reviewed behavioral assertion, not a manufacturer numeric tolerance; 25 C, primary-side, level-enable controller fixture."
                        }
                    ],
                }
            )
        )
        bindings.append({"req_id": identifier, "probe": f"pwm_{probe}", "params": {"temp_c": 25}})

    package = PART_PACKAGES[name]
    pin_names = PW_NAMES if package == "PW" else RGP_NAMES
    pins = tuple(
        {
            "physical_pin": str(index),
            "name": pin_name,
            "part_id": name,
            "function": pin_name + " terminal in selected " + package + " package",
            "polarity": "active_high" if pin_name == "EN" else "not_applicable",
            "direction": "ground"
            if pin_name == "GND"
            else "power"
            if pin_name == "VDD"
            else "output"
            if pin_name in {"VREF", "OUTA", "OUTB", "SRA", "SRB"}
            else "bidir"
            if pin_name in {"COMP", "SS", "HICC"}
            else "input",
            "supply_domain": None if pin_name in {"VDD", "GND"} else "VDD",
            "output_topology": "push_pull"
            if pin_name in {"OUTA", "OUTB", "SRA", "SRB"}
            else "power"
            if pin_name in {"VDD", "VREF"}
            else "input_only"
            if pin_name not in {"COMP", "SS", "HICC", "GND"}
            else "unknown",
            "connection_requirement": "required",
            "evidence": [
                citation(
                    3 if pin_name not in {"OUTA", "OUTB", "SRA", "SRB", "GND"} else 4,
                    pin_name
                    + (
                        " I"
                        if pin_name
                        in {
                            "RT",
                            "EN",
                            "ILIM",
                            "PS",
                            "SP",
                            "VSENSE",
                            "RAMP/CS",
                            "REF/EA+",
                            "FB/EA-",
                            "OVP/OTP",
                            "HICC",
                        }
                        else ""
                    ),
                )
            ],
        }
        for index, pin_name in enumerate(pin_names, 1)
    )
    provenance = {
        "schema_version": 1,
        "record_kind": "reviewed_extraction",
        "profile_id": PROFILE_ID,
        "source_url": SOURCE_URL,
        "document_sha256": record.file_hash,
        "document_revision": "SLUSBD8E; December 2014",
        "selected_package": package,
        "source_columns": source_columns,
        "complete_datasheet_extraction": False,
        "scope": "Selected electrical table rows, primary-side PWM relationships, exact package pin map and explicit omitted behavior.",
        "unreviewed_scope": "Absolute stress, ESD, thermal characteristics, application power stages and full datasheet corners are not qualified.",
        "source_discrepancies": [
            "Table SS charge=28 uA; prose says27 uA. Table used.",
            "Table VREF=3.25 V; prose says3.3 V. Table used.",
            "RevE startup20 us; older SLUA673A says10 us. RevE used.",
        ],
        "qualified_probes_added": sum(row[8] is not None for row in ROWS) + 2,
    }
    return requirements, bindings, pins, provenance
