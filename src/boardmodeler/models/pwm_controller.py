"""First-order, resistor-timed UCC28251 primary-side PWM controller.

Verified evidence owns device values; this module owns reviewed equations. The
model is intentionally limited to level enable and primary-side control. It does
not reproduce the secondary prebias servo, pulse enable, synchronization, full
hiccup timing, temperature dependence, or transistor-level switching edges.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardmodeler.authoring.pin_roles import physical_terminals
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.authoring.ucc28251_reference import PART_PACKAGES, PW_NAMES, RGP_NAMES
from boardmodeler.models.buck_switching import ParameterOrigin

RENDERER_VERSION = "ucc28251_primary_pwm_v1"
PARAMETERS = (
    ("VREF", "V"),
    ("UVLO_RISE", "V"),
    ("UVLO_FALL", "V"),
    ("EN_THRESHOLD", "V"),
    ("ISS", "A"),
    ("SS_CLAMP", "V"),
    ("ILIM", "V"),
    ("ILIM_DELAY", "s"),
    ("BLANK", "s"),
    ("OVP", "V"),
    ("RSRC", "ohm"),
    ("RSNK", "ohm"),
    ("SP_DELAY", "s"),
    ("PS_DELAY", "s"),
    ("COMP_HIGH", "V"),
    ("COMP_LOW", "V"),
    ("COMP_START", "V"),
    ("EA_GAIN", "dB"),
    ("EA_SOURCE", "A"),
    ("EA_SINK", "A"),
    ("IQ", "A"),
    ("IQ_STANDBY", "A"),
    ("IQ_START", "A"),
    ("REF_LIMIT", "A"),
    ("RT_C", "F"),
    ("START_DELAY", "s"),
    ("REF_READY", "V"),
    ("SS_DISCHARGE", "ohm"),
    ("RAMP_RESET", "ohm"),
    ("RAMP_BLANK", "s"),
    ("RAMP_CLAMP", "V"),
    ("OUTPUT_LIMIT", "A"),
)
ESSENTIAL_INPUTS = tuple(name for name, _ in PARAMETERS)
BEHAVIOURS = (
    ("reference voltage", ("pwm_vref",)),
    ("resistor-timed switching frequency", ("pwm_frequency",)),
    ("undervoltage lockout", ("pwm_uvlo_rise", "pwm_uvlo_fall")),
    ("level enable", ("pwm_enable",)),
    ("soft-start charge current", ("pwm_ss_charge",)),
    ("current sense threshold and propagation", ("pwm_ilim", "pwm_ilim_delay")),
    ("OVP shutdown", ("pwm_ovp",)),
    ("alternating primary outputs", ("pwm_alternating",)),
    ("COMP-controlled duty cycle", ("pwm_duty_response",)),
    ("finite output stages", ("pwm_source_resistance", "pwm_sink_resistance")),
    ("primary/synchronous dead time", ("pwm_sp_delay", "pwm_ps_delay")),
)
LIMITATIONS = (
    "Primary-side voltage/current-mode comparator core, 25 C and continuous level enable only.",
    "RT uses equation 3; frequency is qualified only at RT=75 kohm and SP=20 kohm. The numerical RT sensor clamps to 12.5-to-200 kohm; other settings are unqualified and external synchronization is omitted.",
    "VSENSE must be connected to VREF for primary-side operation; secondary prebias servo is omitted.",
    "Primary-side SR startup ramp and SS=2.9-V handover are omitted; synchronous outputs are qualified only after startup settles.",
    "The 420-mV COMP start offset follows typical application prose, not a guaranteed component specification; typical 100-ns minimum pulse width is not reproduced.",
    "HICC timing and duty-match current-limit recovery are omitted; repeated overcurrent accuracy is unknown.",
    "ILIM pin/filter-capacitor discharge is omitted; the first-order comparator expects an externally generated sense voltage.",
    "PS/SP resistor scaling is first-order through the measured 27-kohm/20-kohm points; other resistance values are not qualified.",
    "No thermal, process, statistical, transistor-edge, or complete converter accuracy claim.",
)
NUMERICAL_ASSUMPTIONS = (
    {
        "name": "timing_pin_bias",
        "value": 1.0,
        "unit": "V",
        "origin": "template_default",
        "reason": "Powered numerical RT resistance sensing; this pin DC voltage is not predicted device data.",
    },
    {
        "name": "dead_time_pin_bias",
        "value": 40e-6,
        "unit": "A",
        "origin": "template_default",
        "reason": "Powered numerical resistance sensing at PS/SP; these pin currents are not predicted device data.",
    },
    {
        "name": "reference_output_resistance",
        "value": 0.1,
        "unit": "ohm",
        "origin": "template_default",
        "reason": "Finite reference source regularization, not a datasheet output impedance claim.",
    },
    {
        "name": "state_time_constant",
        "value": 1e-9,
        "unit": "s",
        "origin": "template_default",
        "reason": "Numerical comparator memory regularization; no propagation-delay claim.",
    },
    {
        "name": "error_amplifier_bandwidth",
        "value": 1e6,
        "unit": "Hz",
        "origin": "template_default",
        "reason": "First-order stable amplifier pole; device loop dynamics are not qualified.",
    },
    {
        "name": "error_amplifier_transconductance",
        "value": 1e-3,
        "unit": "S",
        "origin": "template_default",
        "reason": "Numerical amplifier scale; cited open-loop gain fixes output resistance.",
    },
    {
        "name": "soft_start_zero_detection",
        "value": 1e-6,
        "unit": "V",
        "origin": "template_default",
        "reason": "Numerical detection of externally grounded SS, not a device threshold claim.",
    },
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PwmControllerDesignError(ValueError):
    """The source, package or validated design cannot represent this PWM implementation."""


def _terminal(name: str) -> str:
    return name.replace("+", "P").replace("-", "M").replace("/", "_")


def package_ports(package: str) -> tuple[str, ...]:
    return tuple(_terminal(name) for name in (PW_NAMES if package == "PW" else RGP_NAMES))


def untested(spec: SpecSet) -> tuple[str, ...]:
    bound = {
        row.probe
        for row in spec.characteristics
        if row.has_limits
        and row.excerpt
        and row.source_page is not None
        and row.req_class != "ABSOLUTE_MAXIMUM"
    }
    return tuple(
        label for label, probes in BEHAVIOURS if not all(probe in bound for probe in probes)
    )


@dataclass(frozen=True)
class PwmControllerDesign:
    renderer_version: str
    spec_digest: str
    part: str
    subckt: str
    package: str
    ports: tuple[str, ...]
    parameters: tuple[ParameterOrigin, ...]

    def __post_init__(self):
        if self.renderer_version != RENDERER_VERSION:
            raise PwmControllerDesignError("pwm_renderer_version")
        if not re.fullmatch(r"[0-9a-f]{64}", self.spec_digest):
            raise PwmControllerDesignError("pwm_design_spec_digest")
        if PART_PACKAGES.get(self.part.upper()) != self.package or not _NAME.fullmatch(self.subckt):
            raise PwmControllerDesignError("pwm_design_identity")
        if tuple(port.upper() for port in self.ports) != package_ports(self.package):
            raise PwmControllerDesignError("pwm_design_physical_pin_order")
        if tuple(item.name for item in self.parameters) != ESSENTIAL_INPUTS:
            raise PwmControllerDesignError("pwm_design_parameter_set")
        units = dict(PARAMETERS)
        for item in self.parameters:
            if (
                isinstance(item.value, bool)
                or not isinstance(item.value, int | float)
                or not math.isfinite(item.value)
                or item.value <= 0
                or item.unit != units[item.name]
            ):
                raise PwmControllerDesignError("pwm_design_parameter_invalid:" + item.name)
            if (
                item.origin != "cited_row"
                or item.row_id != "UCC28251_" + item.name
                or isinstance(item.page, bool)
                or not isinstance(item.page, int)
                or item.page < 0
                or not item.excerpt
                or item.transform != "none"
            ):
                raise PwmControllerDesignError("pwm_design_uncited_input:" + item.name)
        v = {item.name: item.value for item in self.parameters}
        if v["UVLO_FALL"] >= v["UVLO_RISE"] or v["COMP_LOW"] >= v["COMP_HIGH"]:
            raise PwmControllerDesignError("pwm_design_threshold_order")
        if v["UVLO_RISE"] > 17 or v["ILIM"] > 3.3 or v["VREF"] > 3.6:
            raise PwmControllerDesignError("pwm_design_operating_domain")

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "record_kind": "pwm_controller_design",
            "family": "alternating_pwm_controller",
            "renderer_version": self.renderer_version,
            "spec_digest": self.spec_digest,
            "part": self.part,
            "subckt": self.subckt,
            "package": self.package,
            "ports": list(self.ports),
            "pin_roles": {
                port: "supply"
                if port == "VDD"
                else "ground"
                if port == "GND"
                else "output"
                if port in {"OUTA", "OUTB", "SRA", "SRB", "VREF", "COMP"}
                else "input"
                for port in self.ports
            },
            "external_connections": [
                {
                    "from_pin": "VSENSE",
                    "to_pin": "VREF",
                    "reason": "Reviewed primary-side configuration",
                }
            ],
            "parameters": [item.payload() for item in self.parameters],
            "numerical_assumptions": [dict(item) for item in NUMERICAL_ASSUMPTIONS],
            "limitations": list(LIMITATIONS),
            "footprint_claim": False,
        }

    def to_json(self):
        return json.dumps(self.payload(), indent=2, sort_keys=True, allow_nan=False) + "\n"

    @property
    def sha256(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def record(self, delivered: bytes | None):
        rendered = render_library(self).encode()
        return {
            "schema_version": 1,
            "record_kind": "model_design_record",
            "design_sha256": self.sha256,
            "design": self.payload(),
            "rendered_library_sha256": hashlib.sha256(rendered).hexdigest(),
            "delivered_library_sha256": None
            if delivered is None
            else hashlib.sha256(delivered).hexdigest(),
            "association": "not_delivered"
            if delivered is None
            else "exact"
            if delivered == rendered
            else "invalid_after_change",
            "association_note": "Exact rendered bytes are checked; changed legacy bytes retain seed-only design provenance.",
            "verdict": "UNJUDGED",
            "verdict_note": "Design evidence is not electrical qualification.",
        }

    @classmethod
    def from_json(cls, text: str):
        data = json.loads(text)
        try:
            design = cls(
                data["renderer_version"],
                data["spec_digest"],
                data["part"],
                data["subckt"],
                data["package"],
                tuple(data["ports"]),
                tuple(ParameterOrigin(**item) for item in data["parameters"]),
            )
        except (KeyError, TypeError) as exc:
            raise PwmControllerDesignError("pwm_design_record_incomplete") from exc
        if design.payload() != data:
            raise PwmControllerDesignError("pwm_design_record_altered")
        return design

    @classmethod
    def from_payload(cls, payload):
        return cls.from_json(json.dumps(payload))


def design_from_spec(spec: SpecSet, *, unverified: Collection[str] = ()):
    part = spec.part.strip().upper()
    if part not in PART_PACKAGES:
        return None
    package = PART_PACKAGES[part]
    if physical_terminals(spec.pin_map) != package_ports(package):
        raise PwmControllerDesignError("pwm_design_physical_pin_order")
    rows = {row.char_id: row for row in spec.characteristics}
    parameters = []
    for name, unit in PARAMETERS:
        row = rows.get("UCC28251_" + name)
        if (
            row is None
            or row.char_id in unverified
            or row.typ_value is None
            or row.unit != unit
            or row.source_page is None
            or not row.excerpt
            or row.req_class == "ABSOLUTE_MAXIMUM"
        ):
            raise PwmControllerDesignError("pwm_design_missing_cited_input:" + name)
        parameters.append(
            ParameterOrigin(
                name,
                row.typ_value,
                unit,
                "cited_row",
                row.char_id,
                row.source_page,
                row.excerpt,
                "none",
            )
        )
    return PwmControllerDesign(
        RENDERER_VERSION,
        spec.digest(),
        part,
        spec.subckt,
        package,
        physical_terminals(spec.pin_map),
        tuple(parameters),
    )


def render_library(design: PwmControllerDesign):
    """Generate exact deterministic SPICE bytes; no external state or inference calls."""
    v = {item.name: item.value for item in design.parameters}
    p = "\n".join(f".param {name}={value:.12g}" for name, value in v.items())
    core = f"""* {design.part}: first-order primary-side alternating PWM, 25 C
* RT/PS/SP DC bias is a numerical resistance sensor, not a predicted pin voltage.
* No sync, pulse enable, secondary prebias servo, thermal or complete hiccup model.
.subckt {design.subckt} {" ".join(design.ports)}
{p}
Vlogic logic GND 1
Suvlo logic uvlo VDD GND SUV
Ruvlo uvlo GND 1k
.model SUV SW(Ron=1 Roff=1e12 Vt={{(UVLO_RISE+UVLO_FALL)/2}} Vh={{(UVLO_RISE-UVLO_FALL)/2}})
Bready ready GND V=if((V(uvlo,GND)>0.5) & (V(VREF,GND)>REF_READY),1,0)
* Startup timer is a resettable integration, with 1 nF only a numerical state scale.
Btimer timer GND V=idt(if(V(ready,GND)>0.5,1,0),0,V(ready,GND)<0.5)
Bactive active GND V=if((V(ready,GND)>0.5) & (V(timer,GND)>=START_DELAY*0.999) & (V(EN,GND)>EN_THRESHOLD) & (V(OVP_OTP,GND)<OVP),1,0)
Bvref VREF GND I=limit((V(VREF,GND)-min(VREF,max(V(VDD,GND),0)))/0.1,-REF_LIMIT,REF_LIMIT)
Vrtbias rtbias GND 0
Brt RT rtbias V=limit(V(VDD,GND),0,1)
Bps GND PS I=if(V(VDD,GND)>1,40u,0)
Bsp GND SP I=if(V(VDD,GND)>1,40u,0)
Bdtsp dtsp GND V=if(V(SP,GND)>=V(VREF,GND)-0.1,0,SP_DELAY*limit(V(SP,GND)/(40u*20k),0.25,12.5))
Bdtps dtps GND V=if(V(PS,GND)>=V(VREF,GND)-0.1,0,PS_DELAY*limit(V(PS,GND)/(40u*27k),0.185185,9.25926))
Bperiod period GND V=max(RT_C*limit(V(RT,GND)/max(abs(I(Vrtbias)),1e-12),12.5k,200k)+V(dtsp,GND),1n)
Bphase phase GND V=time-V(period,GND)*floor(time/V(period,GND))
Bchannel channel GND V=floor(time/V(period,GND))-2*floor(time/(2*V(period,GND)))
Bss SS GND I=if(V(active,GND)>0.5,-ISS+max(V(SS,GND)-SS_CLAMP+ISS*SS_DISCHARGE,0)/SS_DISCHARGE,V(SS,GND)/SS_DISCHARGE)
* The amplifier pole is a labelled 1 MHz numerical approximation; gain/current clamps are cited.
Bea COMP GND I=if(V(uvlo,GND)>0.5,limit(-1m*(min(V(REF_EAP,GND),V(SS,GND))-V(FB_EAM,GND)),-EA_SOURCE,EA_SINK)+V(COMP,GND)/(10**(EA_GAIN/20)/1m)+max(V(COMP,GND)-COMP_HIGH,0)/100+min(V(COMP,GND)-COMP_LOW,0)/100,V(COMP,GND)/100)
Cea COMP GND {{1m/(2*pi*1Meg)}}
Bramp RAMP_CS GND I=if(V(phase,GND)<V(dtsp,GND)+RAMP_BLANK,V(RAMP_CS,GND)/RAMP_RESET,if(V(RAMP_CS,GND)>RAMP_CLAMP,(V(RAMP_CS,GND)-RAMP_CLAMP)/RAMP_RESET,0))
* A per-half-cycle latch prevents a falling sense/ramp signal from re-enabling a pulse.
Blimdelayed limdelayed GND V=delay(V(ILIM,GND),ILIM_DELAY)
Blatch latch GND I=if((V(active,GND)<0.5) | (V(SS,GND)<1u),V(latch,GND)/1k,if(V(phase,GND)<V(dtsp,GND)+BLANK,(V(latch,GND)-1)/1k,if((V(RAMP_CS,GND)>=V(COMP,GND)) | (V(limdelayed,GND)>=ILIM),V(latch,GND)/1k,0)))
Clatch latch GND 1p
Bgating gating GND V=if((V(active,GND)>0.5) & (V(SS,GND)>=1u) & (V(COMP,GND)>COMP_START) & (V(phase,GND)>=V(dtsp,GND)) & (V(latch,GND)>0.5),1,0)
Bga ga GND V=if(V(channel,GND)<0.5,V(gating,GND),0)
Bgb gb GND V=if(V(channel,GND)>0.5,V(gating,GND),0)
* Synchronous outputs complement their same-channel primary and preserve independent delays.
Bsa sa GND V=if((V(active,GND)<0.5) | (V(SS,GND)<1u),0,if(V(channel,GND)>0.5,1,if(V(phase,GND)<V(dtsp,GND),0,if((V(ga,GND)>0.5) | (delay(V(ga,GND),V(dtps,GND))>0.5),0,1))))
Bsb sb GND V=if((V(active,GND)<0.5) | (V(SS,GND)<1u),0,if(V(channel,GND)<0.5,1,if(V(phase,GND)<V(dtsp,GND),0,if((V(gb,GND)>0.5) | (delay(V(gb,GND),V(dtps,GND))>0.5),0,1))))
"""
    for name, gate in (("OUTA", "ga"), ("OUTB", "gb"), ("SRA", "sa"), ("SRB", "sb")):
        core += f"Bdrive_{name} {name} GND I=limit((V({name},GND)-V({gate},GND)*max(V(VDD,GND),0))/if(V({gate},GND)>0.5,RSRC,RSNK),-OUTPUT_LIMIT,OUTPUT_LIMIT)\n"
    driver_current = "+".join(f"max(-I(Bdrive_{pin}),0)" for pin in ("OUTA", "OUTB", "SRA", "SRB"))
    core += (
        "Biq VDD GND I=if(V(uvlo,GND)<0.5,IQ_START,if(V(active,GND)>0.5,IQ,IQ_STANDBY))+max(-I(Bvref),0)+max(-I(Bea),0)+max(-I(Vrtbias),0)+max(I(Bps),0)+max(I(Bsp),0)+"
        + driver_current
        + "\n"
    )
    for pin in (
        "VSENSE",
        "REF_EAP",
        "FB_EAM",
        "EN",
        "OVP_OTP",
        "ILIM",
        "HICC",
        "SS",
        "OUTA",
        "OUTB",
        "SRA",
        "SRB",
        "latch",
    ):
        core += f"Rinput_{pin} {pin} GND 1T\n"
    return core + f".ends {design.subckt}\n"


@dataclass(frozen=True)
class PwmControllerSeed:
    design: PwmControllerDesign
    library_text: str

    def payload(self):
        return {
            "schema_version": 1,
            "template_kind": "alternating_pwm_controller",
            "renderer_version": self.design.renderer_version,
            "spec_digest": self.design.spec_digest,
            "part": self.design.part,
            "subckt": self.design.subckt,
            "ports": list(self.design.ports),
            "parameters": [x.payload() for x in self.design.parameters],
            "limitations": list(LIMITATIONS),
            "numerical_assumptions": [dict(item) for item in NUMERICAL_ASSUMPTIONS],
            "verdict": "UNJUDGED",
            "verdict_note": "Independent LTspice probes decide observed behavior.",
        }

    def write(self, path: Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.library_text, encoding="utf-8", newline="\n")
        return target


def seed_from_spec(spec: SpecSet, *, unverified: Collection[str] = ()):
    design = design_from_spec(spec, unverified=unverified)
    return None if design is None else PwmControllerSeed(design, render_library(design))
