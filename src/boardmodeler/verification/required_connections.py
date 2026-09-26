"""Cited external pin connections and a synthetic pad-connection diagnostic.

The static rule inspects a neutral card (and optionally its built netlist).
An electrically separate diagnostic injects 1 nA into the exposed pad and
observes its voltage against the GND pin and the model's internal alarm node.
The injection and 0.1 V alarm threshold are synthetic test aids, not device
datasheet limits.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from boardmodeler.authoring.pin_roles import physical_terminals
from boardmodeler.domain.enums import Status
from boardmodeler.domain.records import Finding
from boardmodeler.models.buck_switching import PAD_DIAGNOSTIC_THRESHOLD_V
from boardmodeler.models.library import subckt_ports
from boardmodeler.schematic.netlist import NC_NODE_RE, NetMap
from boardmodeler.schematic.neutral import ConnectionRow, NeutralProject
from boardmodeler.simulation.raw import RawFile

__all__ = [
    "PadAlarmObservation",
    "RequiredConnectionRule",
    "StaticConnectionResult",
    "check_required_connection",
    "evaluate_pad_alarm_waveform",
    "load_required_connection_rule",
    "render_pad_diagnostic_deck",
]

_CHECK_CODE = "RC001_required_pin_connection"
_NETLIST_CODE = "RC002_card_netlist_disagreement"
_NC_NAMES = frozenset({"", "NC", "N/C", "UNCONNECTED"})
_SPICE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class RequiredConnectionRule:
    """The exact part, pin pair, and verified source of a required PCB tie."""

    req_id: str
    part_number: str
    subckt: str
    ground_name: str
    ground_physical_pin: str
    pad_name: str
    pad_physical_pin: str
    ports: tuple[str, ...]
    doc_id: str
    pdf_page_index: int
    printed_page_label: str | None
    excerpt: str
    source_sha256: str

    @property
    def citation(self) -> str:
        page = f"PDF page {self.pdf_page_index + 1}"
        if self.printed_page_label:
            page += f", printed p. {self.printed_page_label}"
        return f"{self.req_id}, {page}: {self.excerpt}"


@dataclass(frozen=True)
class StaticConnectionResult:
    """A clean inspected card has PASS and zero findings."""

    status: Status
    inspected_refdes: tuple[str, ...]
    findings: tuple[Finding, ...]
    requirement_id: str


@dataclass(frozen=True)
class PadAlarmObservation:
    """Waveform measurements only; artifact provenance is supplied by the runner."""

    status: Literal["MEASURED", "UNKNOWN"]
    alarm_active: bool | None
    pad_mean_abs_v: float | None
    pad_peak_abs_v: float | None
    alarm_mean_v: float | None
    half_window_drift_v: float | None
    reason: str | None
    requirement_id: str
    threshold_v: float
    window_s: tuple[float, float]


def _single(items: list[dict], description: str) -> dict:
    if len(items) != 1:
        raise ValueError(f"expected exactly one {description}, found {len(items)}")
    return items[0]


def load_required_connection_rule(
    requirements_path: Path, *, req_id: str = "B001_PIN_POWERPAD"
) -> RequiredConnectionRule:
    """Load B001 from the raw extraction, preserving citation_verified."""
    source = Path(requirements_path).read_bytes()
    payload = json.loads(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        raise ValueError("required-connection source has no requirements list")
    document = payload.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("doc_id"), str):
        raise ValueError("required-connection source has no document identity")
    rows = [
        item
        for item in payload["requirements"]
        if isinstance(item, dict) and item.get("req_id") == req_id
    ]
    requirement = _single(rows, f"requirement {req_id}")
    if (
        requirement.get("citation_verified") is not True
        or requirement.get("kind") != "CONNECTIVITY"
        or requirement.get("origin") != "DOCUMENT"
        or requirement.get("status") != "active"
    ):
        raise ValueError(f"{req_id}: connection row is not a verified active document citation")
    statement = str(requirement.get("statement", ""))
    if not (
        re.search(r"\bGND\b", statement, re.I)
        and re.search(r"exposed pad|PowerPAD", statement, re.I)
        and re.search(r"connect", statement, re.I)
    ):
        raise ValueError(f"{req_id}: statement does not require a GND-to-pad tie")
    evidence = requirement.get("evidence") or []
    cited = _single([item for item in evidence if isinstance(item, dict)], f"citation for {req_id}")
    doc_id = document["doc_id"]
    page = cited.get("page")
    if (
        cited.get("doc_id") != doc_id
        or not isinstance(page, dict)
        or not isinstance(page.get("pdf_page"), int)
        or page["pdf_page"] < 0
        or not str(cited.get("excerpt", "")).strip()
    ):
        raise ValueError(f"{req_id}: citation page/excerpt does not match the source document")
    part = requirement.get("applies_to")
    if not isinstance(part, str) or not _SPICE_ID.fullmatch(part):
        raise ValueError(f"{req_id}: invalid part identity")
    pin_map = payload.get("pin_map")
    if not isinstance(pin_map, list):
        raise ValueError(f"{req_id}: no physical pin map")
    pins = [pin for pin in pin_map if isinstance(pin, dict) and pin.get("part_id") == part]
    ground = _single([pin for pin in pins if str(pin.get("name", "")).upper() == "GND"], "GND pin")
    pad = _single(
        [pin for pin in pins if str(pin.get("name", "")).upper() == "POWERPAD"], "PowerPAD pin"
    )
    if (
        ground.get("direction") != "ground"
        or pad.get("direction") != "ground"
        or ground.get("connection_requirement") != "required"
        or pad.get("connection_requirement") != "required"
        or ground.get("physical_pin") == pad.get("physical_pin")
    ):
        raise ValueError(f"{req_id}: GND/PowerPAD physical pins are not distinct required grounds")
    pad_evidence = pad.get("evidence") or []
    pad_cited = _single(
        [item for item in pad_evidence if isinstance(item, dict)], "PowerPAD pin citation"
    )
    pad_page = pad_cited.get("page")
    if (
        pad_cited.get("doc_id") != doc_id
        or pad_cited.get("excerpt") != cited["excerpt"]
        or not isinstance(pad_page, dict)
        or pad_page.get("pdf_page") != page["pdf_page"]
    ):
        raise ValueError(f"{req_id}: PowerPAD pin and requirement citations differ")
    ports = physical_terminals(pins)
    if len(ports) != len(pins) or not {"GND", "POWERPAD"}.issubset(set(ports)):
        raise ValueError(f"{req_id}: physical port map is incomplete")
    return RequiredConnectionRule(
        req_id=req_id,
        part_number=part,
        subckt=part,
        ground_name=str(ground["name"]),
        ground_physical_pin=str(ground["physical_pin"]),
        pad_name=str(pad["name"]),
        pad_physical_pin=str(pad["physical_pin"]),
        ports=ports,
        doc_id=doc_id,
        pdf_page_index=page["pdf_page"],
        printed_page_label=(
            str(pad_page["printed_label"]) if pad_page.get("printed_label") is not None else None
        ),
        excerpt=str(cited["excerpt"]),
        source_sha256=hashlib.sha256(source).hexdigest(),
    )


def _rows_for_pin(rows: list[ConnectionRow], pin_number: str, pin_name: str) -> list[ConnectionRow]:
    aliases = {pin_number.casefold(), pin_name.casefold()}
    return [row for row in rows if row.physical_pin.casefold() in aliases]


def _connected(net: str | None) -> bool:
    return bool(net and net.upper() not in _NC_NAMES and not NC_NODE_RE.fullmatch(net))


def _detail(
    rule: RequiredConnectionRule, ground_net: str | None, pad_net: str | None, reason: str
) -> dict[str, str]:
    return {
        "reason": reason,
        "part_number": rule.part_number,
        "ground_pin": rule.ground_name,
        "ground_physical_pin": rule.ground_physical_pin,
        "ground_net": ground_net or "<missing>",
        "pad_pin": rule.pad_name,
        "pad_physical_pin": rule.pad_physical_pin,
        "pad_net": pad_net or "<missing>",
        "requirement_id": rule.req_id,
        "document_id": rule.doc_id,
        "pdf_page_index": str(rule.pdf_page_index),
        "printed_page": rule.printed_page_label or "",
        "excerpt": rule.excerpt,
    }


def check_required_connection(
    project: NeutralProject, rule: RequiredConnectionRule, *, netmap: NetMap | None = None
) -> StaticConnectionResult:
    """Require each matching part's distinct pad pin to share its GND net."""
    targets = [
        component
        for component in project.components
        if component.part_number.casefold() == rule.part_number.casefold()
    ]
    findings: list[Finding] = []
    for component in targets:
        rows = project.connections_of(component.refdes)
        ground_rows = _rows_for_pin(rows, rule.ground_physical_pin, rule.ground_name)
        pad_rows = _rows_for_pin(rows, rule.pad_physical_pin, rule.pad_name)
        ground_net = ground_rows[0].net_name if len(ground_rows) == 1 else None
        pad_net = pad_rows[0].net_name if len(pad_rows) == 1 else None
        if len(ground_rows) != 1:
            reason = "ground_pin_missing" if not ground_rows else "ground_pin_ambiguous"
        elif len(pad_rows) != 1:
            reason = "pad_pin_missing" if not pad_rows else "pad_pin_ambiguous"
        elif not _connected(ground_net):
            reason = "ground_pin_disconnected"
        elif not _connected(pad_net):
            reason = "pad_pin_disconnected"
        elif pad_net != ground_net:
            reason = "pad_wrong_net"
        else:
            reason = ""
        if reason:
            findings.append(
                Finding(
                    code=_CHECK_CODE,
                    status=Status.FAIL,
                    refdes=component.refdes,
                    nets=[net for net in (ground_net, pad_net) if net],
                    message=(
                        f"{component.refdes} {rule.part_number} {rule.pad_name} pin {rule.pad_physical_pin} "
                        f"on {pad_net or '<missing>'} must connect to {rule.ground_name} "
                        f"pin {rule.ground_physical_pin} on {ground_net or '<missing>'} "
                        f"({rule.req_id}, PDF page {rule.pdf_page_index + 1})"
                    ),
                    detail=_detail(rule, ground_net, pad_net, reason),
                )
            )
        if netmap is not None:
            for pin_number, pin_name, declared in (
                (rule.ground_physical_pin, rule.ground_name, ground_net),
                (rule.pad_physical_pin, rule.pad_name, pad_net),
            ):
                actual = netmap.node_of(component.refdes, pin_number)
                if actual is None:
                    actual = netmap.node_of(component.refdes, pin_name)
                if actual != declared or (_connected(declared) and not _connected(actual)):
                    findings.append(
                        Finding(
                            code=_NETLIST_CODE,
                            status=Status.UNKNOWN if actual is None else Status.FAIL,
                            refdes=component.refdes,
                            nets=[net for net in (declared, actual) if net],
                            message=f"{component.refdes}.{pin_name} pin {pin_number}: card net {declared or '<missing>'}, netlist net {actual or '<missing>'}",
                            detail={
                                **_detail(rule, ground_net, pad_net, "card_netlist_disagreement"),
                                "checked_pin": pin_number,
                                "netlist_net": actual or "<missing>",
                            },
                        )
                    )
    status = (
        Status.FAIL
        if any(item.status == Status.FAIL for item in findings)
        else Status.UNKNOWN
        if findings
        else Status.PASS
        if targets
        else Status.NOT_APPLICABLE
    )
    return StaticConnectionResult(
        status, tuple(component.refdes for component in targets), tuple(findings), rule.req_id
    )


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("nonfinite diagnostic value")
    return format(value, ".12g")


def render_pad_diagnostic_deck(
    rule: RequiredConnectionRule,
    model_library: Path,
    *,
    clean: bool,
    injection_a: float = 1e-9,
) -> str:
    """Inject 1 nA to observe the model's internal clean/open pad alarm."""
    if not (math.isfinite(injection_a) and 0 < injection_a < 1e-6):
        raise ValueError("diagnostic injection must be finite and bounded")
    model_path = Path(model_library).resolve(strict=True)
    if any(char in str(model_path) for char in ('"', "\r", "\n")):
        raise ValueError("model library path cannot be quoted safely")
    model_text = model_path.read_text(encoding="utf-8")
    if tuple(port.upper() for port in subckt_ports(model_text, rule.subckt)) != rule.ports:
        raise ValueError("pad diagnostic library physical port order differs from verified pin map")
    role_nodes = {
        "BOOT": "boot",
        "VIN": "vin",
        "EN": "en",
        "SS": "ss",
        "VSENSE": "vsense",
        "COMP": "comp",
        "GND": "0",
        "PH": "ph",
        "POWERPAD": "pad",
    }
    if any(port not in role_nodes for port in rule.ports):
        raise ValueError("pad diagnostic does not support extra physical ports")
    nodes = " ".join(role_nodes[port] for port in rule.ports)
    alarm_signal = f"V(xu1:chk_{rule.pad_name.lower()})"
    excerpt = (
        " ".join(rule.excerpt.split()).encode("ascii", "backslashreplace").decode("ascii")[:180]
    )
    lines = [
        "* Synthetic exposed-pad connection diagnostic: 1 nA injection, disabled device",
        f"* cited {rule.req_id} PDF-page {rule.pdf_page_index + 1}: {excerpt}",
        f'.include "{model_path.as_posix()}"',
        "Vvin vin 0 12",
        "Ven en 0 0",
        "Rboot boot ph 1Meg",
        "Rss ss 0 1Meg",
        "Rvsense vsense 0 1Meg",
        "Rcomp comp 0 1Meg",
        "Rph ph 0 1Meg",
        f"Ipad_diag 0 pad {_number(injection_a)}",
    ]
    if clean:
        lines.append("Rpcb pad 0 1m")
    lines.extend(
        (
            f"XU1 {nodes} {rule.subckt}",
            ".temp 25",
            ".tran 0 100u 0 100n",
            f".save V(pad) {alarm_signal}",
            ".meas tran pad_abs MAX V(pad) FROM=80u TO=100u",
            f".meas tran alarm_hi MAX {alarm_signal} FROM=80u TO=100u",
            ".end",
            "",
        )
    )
    return "\n".join(lines)


def evaluate_pad_alarm_waveform(
    raw: RawFile,
    rule: RequiredConnectionRule,
    *,
    threshold_v: float = PAD_DIAGNOSTIC_THRESHOLD_V,
    window_s: tuple[float, float] = (80e-6, 100e-6),
) -> PadAlarmObservation:
    """Observe the injected pad voltage and model-internal alarm; never award PASS."""
    if not (threshold_v == PAD_DIAGNOSTIC_THRESHOLD_V and 0 <= window_s[0] < window_s[1]):
        raise ValueError("invalid diagnostic threshold or window")
    try:
        axis = raw.time_column()
        if axis is None:
            raise ValueError("raw has no transient time axis")
        t = np.asarray(axis, dtype=float)
        pad = np.asarray(raw.column("V(pad)"), dtype=float)
        alarm = np.asarray(raw.column(f"V(xu1:chk_{rule.pad_name.lower()})"), dtype=float)
        if (
            len(t) < 3
            or len(pad) != len(t)
            or len(alarm) != len(t)
            or not all(np.all(np.isfinite(item)) for item in (t, pad, alarm))
            or np.any(np.diff(t) < 0)
        ):
            raise ValueError("pad diagnostic waveform is incomplete or nonfinite")
        left = int(np.searchsorted(t, window_s[0]))
        right = int(np.searchsorted(t, window_s[1], side="right"))
        if right - left < 10:
            raise ValueError("pad diagnostic steady window has fewer than ten samples")
        region = np.abs(pad[left:right])
        monitor = alarm[left:right]
        mean_abs = float(np.mean(region))
        peak_abs = float(np.max(region))
        alarm_mean = float(np.mean(monitor))
        half = len(region) // 2
        drift = abs(float(np.mean(region[:half]) - np.mean(region[half:])))
        if drift > max(0.01, 0.1 * mean_abs):
            raise ValueError("pad diagnostic voltage did not settle")
        if 0.1 < alarm_mean < 0.9:
            raise ValueError("pad alarm did not settle to a digital state")
        active = mean_abs > threshold_v
        monitor_active = alarm_mean >= 0.9
        if active != monitor_active:
            raise ValueError("pad voltage and internal alarm disagree")
        return PadAlarmObservation(
            "MEASURED",
            active,
            mean_abs,
            peak_abs,
            alarm_mean,
            drift,
            None,
            rule.req_id,
            threshold_v,
            window_s,
        )
    except (KeyError, ValueError, IndexError) as exc:
        return PadAlarmObservation(
            "UNKNOWN",
            None,
            None,
            None,
            None,
            None,
            f"{type(exc).__name__}: {exc}",
            rule.req_id,
            threshold_v,
            window_s,
        )
