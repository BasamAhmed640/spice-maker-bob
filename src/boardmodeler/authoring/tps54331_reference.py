"""Reviewed TPS54331 table columns for exactly TI SLVS839H, October 2023.

This is a partial extraction profile, not a model or a set of qualified probes.
The PDF's blank MIN/TYP/MAX cells stay blank. In particular, the 3.5-V VIN UVLO
maximum is not a nominal model parameter. TPS54331 alone also does not choose
between the D and DDA packages; common pins are insufficient for publication.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from boardmodeler.documents.pdf import normalize_for_citation
from boardmodeler.domain.records import DocumentRecord, Requirement

DATASHEET_SHA256 = "cf72dfd0ac69eec645b7b493de628dc1c3aa66f5f925a2ea9be6bb5c38260730"
SOURCE_URL = "https://www.ti.com/lit/ds/symlink/tps54331.pdf"
PROFILE_ID = "ti-tps54331-slvs839h-tables-v1"
# Page references in the records are zero-based; printed pages are one-based.
PAGES = (2, 3, 4, 5, 12)
TABLE_CONDITIONS = "TJ = -40 C to 150 C; VIN = 3.5 V to 28 V unless otherwise noted."
PACKAGE_GAP = "resolved package (D or DDA; DDA requires PowerPAD pin 9)"
_QUANTITIES = {
    "VIN_UVLO": "vin_uvlo_threshold",
    "IQ_SHUTDOWN": "vin_supply_current",
    "IQ_NOSWITCH": "vin_supply_current",
    "EN_THRESHOLD": "enable_voltage_threshold",
    "EN_CURRENT_BELOW": "enable_input_current",
    "EN_CURRENT_ABOVE": "enable_input_current",
    "VREF": "voltage_reference",
    "RON_LOW": "high_side_on_resistance",
    "RON_NOMINAL": "high_side_on_resistance",
    "ILIM": "switch_peak_current_limit",
}

# Values retain the source table's unit and column; spec loading handles SI scaling.
# (id, statement, exact normalized citation, min, typ, max, unit, conditions)
ELECTRICAL_ROWS = (
    (
        "VIN_UVLO",
        "Internal undervoltage lockout threshold",
        "Internal undervoltage lockout threshold Rising and falling 3.5 V",
        None,
        None,
        3.5,
        "V",
        "Rising and falling; typical threshold not specified.",
    ),
    (
        "IQ_SHUTDOWN",
        "Shutdown supply current",
        "Shutdown supply current EN = 0 V, VIN = 12 V, -40°C to 85°C 1 4 μA",
        None,
        1,
        4,
        "uA",
        "EN = 0 V; VIN = 12 V; TJ = -40 C to 85 C.",
    ),
    (
        "IQ_NOSWITCH",
        "Operating non-switching supply current",
        "Operating - non-switching supply current VSENSE = 0.85 V 110 190 μA",
        None,
        110,
        190,
        "uA",
        "VSENSE = 0.85 V.",
    ),
    (
        "EN_THRESHOLD",
        "Enable threshold",
        "Enable threshold Rising and falling 1.25 1.35 V",
        None,
        1.25,
        1.35,
        "V",
        "Rising and falling.",
    ),
    (
        "EN_CURRENT_BELOW",
        "Enable input current below threshold",
        "Input current Enable threshold - 50 mV -1 μA",
        None,
        -1,
        None,
        "uA",
        "EN = enable threshold - 50 mV.",
    ),
    (
        "EN_CURRENT_ABOVE",
        "Enable input current above threshold",
        "Input current Enable threshold + 50 mV -4 μA",
        None,
        -4,
        None,
        "uA",
        "EN = enable threshold + 50 mV.",
    ),
    (
        "VREF",
        "Voltage reference",
        "Voltage reference 0.772 0.8 0.828 V",
        0.772,
        0.8,
        0.828,
        "V",
        "",
    ),
    (
        "RON_LOW",
        "High-side MOSFET on resistance at VIN = 3.5 V",
        "BOOT-PH = 3 V, VIN = 3.5 V 115 200 mΩ",
        None,
        115,
        200,
        "mohm",
        "BOOT-PH = 3 V; VIN = 3.5 V.",
    ),
    (
        "RON_NOMINAL",
        "High-side MOSFET on resistance at VIN = 12 V",
        "BOOT-PH = 6 V, VIN = 12 V 80 150",
        None,
        80,
        150,
        "mohm",
        "BOOT-PH = 6 V; VIN = 12 V. Unit shared with preceding on-resistance row.",
    ),
    (
        "EA_GM",
        "Error amplifier transconductance",
        "Error amplifier transconductance (gm) -2 μA < I(COMP) < 2 μA, V(COMP) = 1 V 92 μmhos",
        None,
        92,
        None,
        "uS",
        "-2 uA < I(COMP) < 2 uA; V(COMP) = 1 V. Source unit micromhos equals uS.",
    ),
    (
        "EA_GAIN",
        "Error amplifier DC gain",
        "Error amplifier DC gain(1) VSENSE = 0.8 V 800 V/V",
        None,
        800,
        None,
        "V/V",
        "VSENSE = 0.8 V; specified by design (footnote 1).",
    ),
    (
        "EA_GBW",
        "Error amplifier unity gain bandwidth",
        "Error amplifier unity gain bandwidth(1) 5-pF capacitance from COMP to GND pins 2.7 MHz",
        None,
        2.7,
        None,
        "MHz",
        "5-pF capacitance COMP to GND; specified by design (footnote 1).",
    ),
    (
        "EA_CURRENT_MAGNITUDE",
        "Error amplifier source and sink current magnitude",
        "Error amplifier source and sink current V(COMP) = 1 V, 100-mV overdrive ±7 μA",
        None,
        7,
        None,
        "uA",
        "V(COMP) = 1 V; 100-mV overdrive. Source table gives signed +/-7 uA; the recorded typical value is the magnitude, not a min/max interval.",
    ),
    (
        "SW_CURRENT_TO_COMP",
        "Switch current to COMP transconductance",
        "Switch current to COMP transconductance VIN = 12 V 12 A/V",
        None,
        12,
        None,
        "A/V",
        "VIN = 12 V.",
    ),
    (
        "ECO_CURRENT",
        "Pulse skipping Eco-mode switch current threshold",
        "Pulse skipping Eco-mode switch current threshold 160 mA",
        None,
        160,
        None,
        "mA",
        "",
    ),
    (
        "ILIM",
        "Current-limit threshold",
        "Current-limit threshold VIN = 12 V 3.5 5.8 A",
        3.5,
        5.8,
        None,
        "A",
        "VIN = 12 V; the MAX column is blank.",
    ),
    (
        "THERMAL_SHUTDOWN",
        "Thermal shutdown temperature",
        "Thermal shutdown 165 °C",
        None,
        165,
        None,
        "C",
        "No temperature-dependent model or thermal validation is implied.",
    ),
    (
        "SS_CHARGE",
        "Slow-start charge current",
        "Charge current V(SS) = 0.4 V 2 μA",
        None,
        2,
        None,
        "uA",
        "V(SS) = 0.4 V.",
    ),
    (
        "SS_MATCH",
        "SS to VSENSE matching",
        "SS to VSENSE matching V(SS) = 0.4 V 10 mV",
        None,
        10,
        None,
        "mV",
        "V(SS) = 0.4 V.",
    ),
)
SWITCHING_ROWS = (
    (
        "FSW",
        "Switching frequency",
        "Switching frequency VIN = 12 V, 25°C 456 570 684 kHz",
        456,
        570,
        684,
        "kHz",
        "VIN = 12 V; TJ = 25 C.",
    ),
    (
        "TONMIN",
        "Minimum controllable on time",
        "Minimum controllable on time VIN = 12 V, 25°C 105 130 ns",
        None,
        105,
        130,
        "ns",
        "VIN = 12 V; TJ = 25 C.",
    ),
    (
        "DMAX",
        "Maximum controllable duty ratio",
        "Maximum controllable duty ratio(1) BOOT-PH = 6 V 90% 93%",
        90,
        93,
        None,
        "%",
        "BOOT-PH = 6 V; footnote marker (1) as printed.",
    ),
)
PIN_NAMES = ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH")
PIN_ANCHORS = (
    "1 BOOT O",
    "2 VIN I This pin is the 3.5-V to 28-V input supply voltage.",
    "3 EN I",
    "4 SS I This pin is slow-start pin. An external capacitor connected to this pin sets the output rise time.",
    "5 VSENSE I This pin is the inverting node of the transconductance (gm) error amplifier.",
    "6 COMP O",
    "7 GND - Ground pin",
    "8 PH O The PH pin is the source of the internal high-side power MOSFET.",
)
PAD_CITATION = "The PowerPAD is only available on the DDA package. For proper operation, the GND pin must be connected to the exposed pad."
UVLO_CITATION = "The typical VIN UVLO threshold is not specified and the device can operate at input voltages down to the UVLO voltage."


def matches(part: str, file_hash: str) -> bool:
    return part.strip().upper() == "TPS54331" and file_hash == DATASHEET_SHA256


def records(
    record: DocumentRecord, pages: Mapping[int, str]
) -> tuple[list[Requirement], tuple[dict[str, Any], ...], dict[str, Any]]:
    """Return reviewed rows, unresolved common pins, and machine-readable provenance.

    Every citation must match readable text from the exact document. Missing text
    or a changed hash refuses the profile, rather than invoking a fallback extractor.
    """
    if record.file_hash != DATASHEET_SHA256:
        raise ValueError("reviewed_extraction_hash_mismatch: TPS54331 profile needs exact SLVS839H")
    texts = {page: normalize_for_citation(pages.get(page, "")) for page in PAGES}
    if missing := [page + 1 for page, text in texts.items() if not text]:
        raise ValueError(f"datasheet_page_unreadable: TPS54331 reviewed printed pages {missing}")
    rows: list[Requirement] = []
    source_columns: dict[str, Any] = {}

    def evidence(page: int, excerpt: str, section: str) -> dict[str, Any]:
        excerpt = normalize_for_citation(excerpt)
        if excerpt not in texts[page]:
            raise ValueError(
                f"reviewed_extraction_citation_missing: printed page {page + 1}: {excerpt}"
            )
        return {
            "doc_id": record.doc_id,
            "page": {"pdf_page": page, "printed_label": str(page + 1)},
            "section": section,
            "table": section,
            "excerpt": excerpt,
            "extraction": "embedded_text",
        }

    def add(name, statement, page, excerpt, section, limits=None, condition="", **extra):
        rid = f"TPS54331_{name}"
        rows.append(
            Requirement.model_validate(
                {
                    "req_id": rid,
                    "applies_to": "TPS54331",
                    "kind": "ELECTRICAL",
                    "class": (
                        "DOCUMENTED_LIMIT"
                        if limits.get("min") is not None or limits.get("max") is not None
                        else "TYPICAL_VALUE"
                    )
                    if limits
                    else "UNKNOWN",
                    "criticality": "IMPORTANT",
                    "origin": "DOCUMENT",
                    "statement": statement,
                    "signal_refs": [_QUANTITIES.get(name, name.lower())],
                    "configuration": condition or None,
                    "limits": limits,
                    "conditions": [{"text": condition}] if condition else [],
                    "evidence": [evidence(page, excerpt, section)],
                    "citation_verified": True,
                    **extra,
                }
            )
        )
        if limits:
            source_columns[rid] = dict(limits)

    for page, section, table in (
        (4, "6.5 Electrical Characteristics", ELECTRICAL_ROWS),
        (5, "6.6 Switching Characteristics", SWITCHING_ROWS),
    ):
        for name, statement, excerpt, minimum, typical, maximum, unit, condition in table:
            add(
                name,
                statement,
                page,
                excerpt,
                section,
                {"min": minimum, "typ": typical, "max": maximum, "unit": unit},
                f"{TABLE_CONDITIONS} {condition}".strip(),
            )
    add(
        "VIN_RANGE",
        "Recommended operating input voltage",
        3,
        "Operating input voltage on (VIN pin) 3.5 28 V",
        "6.3 Recommended Operating Conditions",
        {"min": 3.5, "typ": None, "max": 28, "unit": "V"},
    )
    add(
        "TJ_RANGE",
        "Recommended operating junction temperature",
        3,
        "TJ Operating junction temperature -40 150 °C",
        "6.3 Recommended Operating Conditions",
        {"min": -40, "typ": None, "max": 150, "unit": "C"},
    )
    add(
        "VIN_UVLO_TYP_UNSPECIFIED",
        "Typical VIN UVLO threshold is not specified",
        12,
        UVLO_CITATION,
        "7.4.2 Operation With VIN < 3.5 V",
    )
    add(
        "DDA_POWERPAD",
        "DDA package requires PowerPAD pin 9 connected to GND",
        2,
        PAD_CITATION,
        "5 Pin Configuration and Functions",
        kind="CONNECTIVITY",
        configuration="DDA",
    )
    pins = tuple(
        {
            "physical_pin": str(index),
            "name": name,
            "package_resolution": "unresolved",
            "package_options": ["D", "DDA"],
            "evidence": [evidence(2, excerpt, "5 Pin Configuration and Functions")],
        }
        for index, (name, excerpt) in enumerate(zip(PIN_NAMES, PIN_ANCHORS, strict=True), 1)
    )
    provenance = {
        "schema_version": 1,
        "record_kind": "reviewed_extraction",
        "profile_id": PROFILE_ID,
        "source_url": SOURCE_URL,
        "document_sha256": record.file_hash,
        "document_revision": "SLVS839H; revised October 2023",
        "pdf_pages_zero_based": list(PAGES),
        "source_columns": source_columns,
        "package_resolution": "unresolved",
        "package_options": {
            "D": {"pins": list(PIN_NAMES), "powerpad": False},
            "DDA": {
                "pins": [*PIN_NAMES, "PowerPAD"],
                "powerpad": True,
                "required_connection": "pin 9 PowerPAD to pin 7 GND",
            },
        },
        "complete_datasheet_extraction": False,
        "scope": "Tables 6.5 and 6.6; recommended operating conditions; common pins; DDA pad; explicit unspecified typical VIN UVLO.",
        "unreviewed_scope": "Absolute stress/ESD/thermal tables, curves, remaining functional prose and application/packaging sections are not fully extracted by this profile.",
        "qualified_probes_added": 0,
    }
    # These two representations require interpretation; retain the printed cells
    # alongside the normalized numeric quantity rather than hiding the conversion.
    source_columns["TPS54331_EA_GM"]["printed_unit"] = "μmhos"
    source_columns["TPS54331_EA_CURRENT_MAGNITUDE"].update(
        printed_typ="±7", interpreted_quantity="magnitude"
    )
    return rows, pins, provenance
