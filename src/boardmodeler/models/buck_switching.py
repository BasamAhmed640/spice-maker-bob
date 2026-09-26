"""Deterministic first candidate for a recognised peak-current-mode buck.

The JSON contract specifies the topology's parameters and the reviewed ways to
read them from the already frozen, citation-checked :class:`SpecSet`.  A value
with no matching cited row is a *template default*, recorded as such in the
seed's provenance.  This module does not judge a model or confer a PASS: the
normal LTspice harness remains the sole electrical authority.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardmodeler.authoring.pin_roles import physical_terminals
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.domain.hashing import sha256_file

__all__ = [
    "BuckSeed",
    "ParameterOrigin",
    "PinMatch",
    "TemplateSeedError",
    "match_pins",
    "seed_from_spec",
]

_CONTRACT = Path(__file__).with_name("peak_current_buck.json")
_SUBCKT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PAD_DIAGNOSTIC_THRESHOLD_V = 0.1  # Synthetic alarm threshold, not a device limit.
_PARAMETER_NAMES = (
    "VREF",
    "FSW",
    "TONMIN",
    "DMAX",
    "ILIM",
    "GMCS",
    "VECO",
    "ECO_I",
    "EAGM",
    "EAI",
    "ISS",
    "SSOFS",
    "ENTH",
    "ENHYS",
    "UVTH",
    "UVHYS",
    "IQOP",
    "IQSD_BASE",
    "EN_PULLUP",
    "RON",
    "FOLD6",
    "FOLD4",
    "FOLD2",
)


class TemplateSeedError(ValueError):
    """A recognised template cannot be rendered faithfully."""


@dataclass(frozen=True)
class ParameterOrigin:
    name: str
    value: float
    unit: str
    origin: str
    row_id: str | None = None
    page: int | None = None
    excerpt: str | None = None
    transform: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "origin": self.origin,
            "row_id": self.row_id,
            "page": self.page,
            "excerpt": self.excerpt,
            "transform": self.transform,
        }


@dataclass(frozen=True)
class BuckSeed:
    contract_id: str
    contract_sha256: str
    spec_digest: str
    part: str
    subckt: str
    ports: tuple[str, ...]
    parameters: tuple[ParameterOrigin, ...]
    library_text: str

    def payload(self) -> dict[str, Any]:
        """Evidence for a card or a neighbouring ``template-parameters.json``."""
        return {
            "schema_version": 1,
            "template_kind": "peak_current_buck",
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "spec_digest": self.spec_digest,
            "part": self.part,
            "subckt": self.subckt,
            "ports": list(self.ports),
            "parameters": [parameter.payload() for parameter in self.parameters],
            "verdict": "UNJUDGED",
            "verdict_note": "Run the LTspice harness; parameter provenance is not electrical proof.",
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def write(self, path: Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.library_text, encoding="utf-8", newline="\n")
        return target


def _load_contract() -> dict[str, Any]:
    try:
        contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateSeedError(f"buck_template_contract_unavailable: {exc}") from exc
    if contract.get("schema_version") != 1 or contract.get("id") != "peak_current_buck_v1":
        raise TemplateSeedError("buck_template_contract_version")
    if tuple(entry.get("name") for entry in contract.get("parameters", ())) != _PARAMETER_NAMES:
        raise TemplateSeedError("buck_template_contract_parameters")
    if tuple(contract.get("required_ports", ())) != (
        "BOOT",
        "VIN",
        "EN",
        "SS",
        "VSENSE",
        "COMP",
        "GND",
        "PH",
    ) or tuple(contract.get("optional_ports", ())) != ("POWERPAD",):
        raise TemplateSeedError("buck_template_contract_ports")
    roles = contract.get("pin_roles")
    if not isinstance(roles, dict) or set(roles) != set(contract["required_ports"]):
        raise TemplateSeedError("buck_template_contract_pin_roles")
    for key in ("supported_behaviors", "unsupported_behaviors"):
        if not isinstance(contract.get(key), list) or not contract[key]:
            raise TemplateSeedError(f"buck_template_contract_{key}")
    if "ground_tie_pins" in contract:
        raise TemplateSeedError("buck_template_contract_legacy_ground_ties")
    connections = contract.get("required_connections")
    if not isinstance(connections, list) or len(connections) != 1:
        raise TemplateSeedError("buck_template_contract_required_connections")
    pad_rule = connections[0]
    if (
        not isinstance(pad_rule, dict)
        or pad_rule.get("id") != "exposed_pad_to_gnd"
        or pad_rule.get("to_role") != "GND"
        or pad_rule.get("location") != "PCB"
        or pad_rule.get("applies_when") != "pin_present"
        or pad_rule.get("citation_required") is not True
        or not isinstance(pad_rule.get("statement"), str)
        or not pad_rule["statement"].strip()
        or not isinstance(pad_rule.get("pin_aliases"), list)
        or not pad_rule["pin_aliases"]
        or not all(isinstance(alias, str) and alias for alias in pad_rule["pin_aliases"])
        or "source" in pad_rule
    ):
        raise TemplateSeedError("buck_template_contract_required_connections")
    for entry in contract["parameters"]:
        if not math.isfinite(float(entry["default"])):
            raise TemplateSeedError(f"buck_template_contract_nonfinite: {entry['name']}")
        if not isinstance(entry.get("sources"), list):
            raise TemplateSeedError(f"buck_template_contract_sources: {entry['name']}")
    return contract


def _number_for(characteristic: Characteristic, source: dict[str, Any]) -> tuple[float, str] | None:
    field = source["field"]
    transform = "none"
    if field == "bounds_midpoint":
        if characteristic.min_value is None or characteristic.max_value is None:
            return None
        if characteristic.min_value > characteristic.max_value:
            return None
        value = (characteristic.min_value + characteristic.max_value) / 2
        transform = "midpoint of cited minimum and maximum"
    elif field == "literal":
        value = float(source["literal"])
        # The number must actually occur in the cited excerpt.  A contract may
        # nominate a row; it cannot manufacture a cited parameter from its id.
        token = f"{value:g}"
        if re.search(rf"(?<![\d.]){re.escape(token)}(?![\d.])", characteristic.excerpt) is None:
            return None
        transform = "literal value in cited excerpt"
    elif field in {"typ_value", "min_value", "max_value"}:
        raw = getattr(characteristic, field)
        if raw is None:
            return None
        value = float(raw)
    else:
        raise TemplateSeedError(f"buck_template_contract_field: {field}")
    if source.get("absolute"):
        value = abs(value)
        transform = "magnitude of cited signed current"
    scale = float(source.get("scale", 1.0))
    if scale != 1.0:
        value *= scale
        transform = (
            f"{transform}; scaled by {scale:g}" if transform != "none" else f"scaled by {scale:g}"
        )
    if not math.isfinite(value):
        return None
    return value, transform


def _source_for(
    spec: SpecSet, entry: dict[str, Any], unverified: Collection[str]
) -> tuple[Characteristic, float, str, str] | None:
    for source in entry["sources"]:
        suffix = source.get("suffix")
        pattern = source.get("statement")
        for characteristic in spec.characteristics:
            if characteristic.char_id in unverified:
                continue
            if suffix is not None and not characteristic.char_id.endswith(suffix):
                continue
            if pattern is not None and not re.search(pattern, characteristic.statement, re.I):
                continue
            if suffix is None and pattern is None:
                raise TemplateSeedError(f"buck_template_contract_source: {entry['name']}")
            if characteristic.req_class == "ABSOLUTE_MAXIMUM":
                continue
            if characteristic.source_page is None or not characteristic.excerpt.strip():
                continue
            expected_unit = source.get("source_unit", entry["unit"])
            if characteristic.unit != expected_unit:
                continue
            numeric = _number_for(characteristic, source)
            if numeric is None:
                continue
            value, transform = numeric
            origin = "derived_from_bounds" if source["field"] == "bounds_midpoint" else "cited_row"
            return characteristic, value, origin, transform
    return None


def _parameters(
    spec: SpecSet, contract: dict[str, Any], unverified: Collection[str]
) -> tuple[ParameterOrigin, ...]:
    chosen: list[ParameterOrigin] = []
    for entry in contract["parameters"]:
        source = _source_for(spec, entry, unverified)
        if source is None:
            chosen.append(
                ParameterOrigin(
                    name=entry["name"],
                    value=float(entry["default"]),
                    unit=entry["unit"],
                    origin="template_default",
                )
            )
            continue
        characteristic, value, origin, transform = source
        chosen.append(
            ParameterOrigin(
                name=entry["name"],
                value=value,
                unit=entry["unit"],
                origin=origin,
                row_id=characteristic.char_id,
                page=characteristic.source_page,
                excerpt=characteristic.excerpt,
                transform=transform,
            )
        )
    values = {item.name: item.value for item in chosen}
    if not (0 < values["DMAX"] <= 1):
        raise TemplateSeedError("buck_template_invalid_duty")
    if not (0 < values["FOLD2"] < values["FOLD4"] < values["FOLD6"] < values["VREF"]):
        raise TemplateSeedError("buck_template_invalid_foldback")
    if not (values["TONMIN"] < values["DMAX"] / values["FSW"]):
        raise TemplateSeedError("buck_template_invalid_minimum_on_time")
    for name in ("VREF", "FSW", "ILIM", "GMCS", "VECO", "EAGM", "EAI", "ISS", "UVTH", "RON"):
        if values[name] <= 0:
            raise TemplateSeedError(f"buck_template_invalid_parameter: {name}")
    return tuple(chosen)


def _spice(value: float) -> str:
    return format(value, ".12g")


def _render(
    subckt: str,
    ports: tuple[str, ...],
    parameters: tuple[ParameterOrigin, ...],
    pins: PinMatch | None = None,
) -> str:
    """The template in the part's own terminal names, with pads left external."""
    pins = pins or match_pins(ports)
    body = _BODY
    for role, name in pins.roles.items():
        if name != role:
            body = re.sub(rf"\b{role}\b", name, body)
    ground = pins.roles.get("GND", "GND")
    declarations = [f"{parameter.name}={_spice(parameter.value)}" for parameter in parameters]
    lines = [
        "* Reduced peak-current-mode buck template; electrical verdict comes from LTspice.",
        "* Parameter origins are recorded in template-parameters.json beside the delivered model.",
        f".subckt {subckt} {' '.join(ports)}",
    ]
    for start in range(0, len(declarations), 5):
        lines.append(".param " + " ".join(declarations[start : start + 5]))
    if pins.ground_ties:
        lines.append(
            "* Pad leak is for convergence only; the PCB tie needs separate cited verification."
        )
    for pad in pins.ground_ties:
        lines.append(f"R{pad}_leak {pad} {ground} 1G")
        lines.append(
            f"Bchk_{pad.lower()} chk_{pad.lower()} {ground} "
            f"V=if(abs(V({pad},{ground}))>{_spice(PAD_DIAGNOSTIC_THRESHOLD_V)},1,0)"
        )
    if pins.ground_ties:
        lines.append(
            "* chk_* is a synthetic test alarm at 0.1 V, not an IC limit; inject only in a diagnostic deck."
        )
    lines.extend(body.splitlines())
    lines.append(f".ends {subckt}")
    return "\n".join(lines) + "\n"


_BODY = """\
* EN bias permits a floating EN; the externally held-low pull-up draw is part of IQSD.
Bpu VIN EN I={EN_PULLUP}*limit((5-V(EN,GND))/0.2,0,1)
Ren EN GND 100Meg
* Unused logic inputs sit on each gate's own common node (GND). LTspice ignores an
* input only there; an input on global node 0 counts as logic low whenever the
* fixture's ground is a different node, which held every AND gate off.
Aen EN GND GND GND GND nc_en en_ok GND SCHMITT Vt={ENTH-ENHYS/2} Vh={ENHYS/2} Vhigh=1 Vlow=0
Auv VIN GND GND GND GND nc_uv uv_ok GND SCHMITT Vt={UVTH-UVHYS/2} Vh={UVHYS/2} Vhigh=1 Vlow=0
Aon en_ok uv_ok GND GND GND nc_on run GND AND Vhigh=1 Vlow=0
Rrun run GND 1Meg
Bq VIN GND I={IQSD_BASE}+({IQOP}-{IQSD_BASE})*V(run,GND)
* SS and error-amplifier current are bounded by cited rows when present.
Bss VIN SS I={ISS}*V(run,GND)*limit((3-V(SS,GND))/0.1,0,1)
Sss SS GND nrun GND SSOFF
Rss SS GND 1G
Bref ref GND V=min(V(SS,GND)+{SSOFS},{VREF})
Rref ref GND 1Meg
Bea GND COMP I=limit({EAGM}*(V(ref,GND)-V(VSENSE,GND)),-{EAI},{EAI})*V(run,GND)
Rvs VSENSE GND 100Meg
Dchi COMP chi DCL
Vchi chi GND 2.5
Dclo clo COMP DCL
Vclo clo GND {VECO}
Rcomp COMP GND 100Meg
* Peak switch-current command. Eco clamp permits pulse skipping at light load.
Bipk ipk GND V=limit({GMCS}*(V(COMP,GND)-{VECO}),0,{ILIM})
Ripk ipk GND 1Meg
Aeco COMP GND GND GND GND nc_eco eco_ok GND SCHMITT Vt={VECO} Vh=10m Vhigh=1 Vlow=0
Becomand eco_demand GND V=limit((V(ipk,GND)-{ECO_I})/20m,0,1)
* At enable, soft-start allows minimum-width pulses before COMP has charged;
* after SS reaches its reference region, Eco-mode suppresses unwanted pulses.
Bstartup startup GND V=limit(({VREF}/2-V(SS,GND))/10m,0,1)
Ballow allow GND V=max(V(startup,GND),V(eco_ok,GND)*V(eco_demand,GND))
* Four synchronized clocks implement the documented 1, 1/2, 1/4, 1/8 foldback.
Vclk1 clk1 GND PULSE(0 1 0 2n 2n 20n {1/FSW})
Vclk2 clk2 GND PULSE(0 1 0 2n 2n 20n {2/FSW})
Vclk4 clk4 GND PULSE(0 1 0 2n 2n 20n {4/FSW})
Vclk8 clk8 GND PULSE(0 1 0 2n 2n 20n {8/FSW})
Bfold1 f1 GND V=limit((V(VSENSE,GND)-{FOLD6})/5m,0,1)
Bfold2 f2 GND V=limit((V(VSENSE,GND)-{FOLD4})/5m,0,1)*(1-V(f1,GND))
Bfold4 f4 GND V=limit((V(VSENSE,GND)-{FOLD2})/5m,0,1)*(1-V(f1,GND))*(1-V(f2,GND))
Bfold8 f8 GND V=(1-V(f1,GND))*(1-V(f2,GND))*(1-V(f4,GND))
Bclk clk GND V=V(clk1,GND)*V(f1,GND)+V(clk2,GND)*V(f2,GND)+V(clk4,GND)*V(f4,GND)+V(clk8,GND)*V(f8,GND)
Vmaxd1 maxd1 GND PULSE(0 1 {DMAX/FSW} 2n 2n 20n {1/FSW})
Vmaxd2 maxd2 GND PULSE(0 1 {2*DMAX/FSW} 2n 2n 20n {2/FSW})
Vmaxd4 maxd4 GND PULSE(0 1 {4*DMAX/FSW} 2n 2n 20n {4/FSW})
Vmaxd8 maxd8 GND PULSE(0 1 {8*DMAX/FSW} 2n 2n 20n {8/FSW})
Bmaxd maxd GND V=V(maxd1,GND)*V(f1,GND)+V(maxd2,GND)*V(f2,GND)+V(maxd4,GND)*V(f4,GND)+V(maxd8,GND)*V(f8,GND)
Aset clk run allow GND GND nc_set set GND AND Vhigh=1 Vlow=0
Ablank q GND GND GND GND nc_bl blank_ok GND BUF Vhigh=1 Vlow=0 Td={TONMIN}
Bcmp cmp GND V=0.5*(1+tanh((I(Vsns)-V(ipk,GND))/0.02))
Rcmp cmp GND 1Meg
Areset maxd pk nrun GND GND nc_rst rst GND OR Vhigh=1 Vlow=0
Apk cmp blank_ok GND GND GND nc_pk pk GND AND Vhigh=1 Vlow=0
Anrun run GND GND GND GND nrun nc_nrun GND BUF Vhigh=1 Vlow=0
Alatch set rst GND GND GND qb q GND SRFLOP Vhigh=1 Vlow=0 Trise=2n Tfall=2n
Rq q GND 1Meg
Rqb qb GND 1Meg
Vsns VIN swin 0
Shs swin PH q GND SWHS
Rboot BOOT PH 10Meg
.model SWHS SW(Ron={RON} Roff=10G Vt=0.5 Vh=-0.1)
.model SSOFF SW(Ron=100 Roff=1G Vt=0.5 Vh=-0.1)
.model DCL D(Is=1e-14 N=0.05)"""


@dataclass(frozen=True)
class PinMatch:
    """How a part's physical terminals fill the template's roles, or why they cannot."""

    roles: dict[str, str]  # template role -> the part's terminal name
    # Historical field name retained for fixture callers. The fixture supplies
    # an external ground connection; a requirement alarm needs part-specific evidence.
    ground_ties: tuple[str, ...]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def required_ground_connections(self) -> tuple[str, ...]:
        """Present pad terminals whose PCB requirement needs part-specific evidence."""
        return self.ground_ties


def match_pins(ports: Collection[str], contract: dict[str, Any] | None = None) -> PinMatch:
    """Map terminal names onto ``peak_current_buck_v1`` roles through the contract's aliases.

    Every terminal must be used exactly once: a role pin or a recognized pad. The
    model leaves pad connection external; any requirement needs cited part evidence.
    A terminal the template does not model (RT, SYNC, PG, ...) refuses the match with
    its name, rather than leaving a datasheet pin silently unconnected.
    """
    contract = contract or _load_contract()
    alias_to_role = {
        alias.upper(): role for role, aliases in contract["pin_roles"].items() for alias in aliases
    }
    ties = {
        name.upper() for rule in contract["required_connections"] for name in rule["pin_aliases"]
    }
    roles: dict[str, str] = {}
    ground_ties: list[str] = []
    unmodelled: list[str] = []
    for port in ports:
        role = alias_to_role.get(port.upper())
        if role is not None:
            if role in roles:
                return PinMatch(
                    {}, (), f"pin_role_duplicate: {roles[role]} and {port} are both {role}"
                )
            roles[role] = port
        elif port.upper() in ties:
            ground_ties.append(port)
        else:
            unmodelled.append(port)
    if unmodelled:
        return PinMatch({}, (), "pin_role_unsupported: " + ", ".join(unmodelled))
    missing = [role for role in contract["required_ports"] if role not in roles]
    if missing:
        return PinMatch({}, (), "pin_role_missing: " + ", ".join(missing))
    return PinMatch(roles, tuple(ground_ties))


def seed_from_spec(spec: SpecSet, *, unverified: Collection[str] = ()) -> BuckSeed | None:
    """Return a template candidate for a matching physical buck pinout, else ``None``.

    Recognition is deliberately narrow.  A generic analogue part or a buck
    without the exposed compensation/PH/SS pins follows the existing author path.
    The spec's order is retained exactly, and a malformed recognised contract
    raises instead of quietly substituting a different topology.
    """
    if not spec.pin_map:
        return None
    try:
        ports = physical_terminals(spec.pin_map)
    except KeyError, ValueError:
        return None
    pins = match_pins(ports)
    if not pins.ok:
        return None
    if not _SUBCKT_NAME.fullmatch(spec.subckt):
        raise TemplateSeedError("buck_template_invalid_subckt")
    # A matching pinout alone can occur on other ICs.  Demand cited buck-specific
    # rows as well, so a different part is never silently given this topology.
    evidence = " ".join(
        characteristic.statement.casefold()
        for characteristic in spec.characteristics
        if characteristic.char_id not in unverified
    )
    if "switching frequency" not in evidence or not any(
        term in evidence for term in ("buck", "current limit", "switch current")
    ):
        return None
    contract = _load_contract()
    parameters = _parameters(spec, contract, unverified)
    return BuckSeed(
        contract_id=contract["id"],
        contract_sha256=sha256_file(_CONTRACT),
        spec_digest=spec.digest(),
        part=spec.part,
        subckt=spec.subckt,
        ports=ports,
        parameters=parameters,
        library_text=_render(spec.subckt, ports, parameters, pins),
    )
