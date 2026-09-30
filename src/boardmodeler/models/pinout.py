"""Source-confirmed physical pin mappings, independent of a model's own ports.

The checker is generic. The bundled reviewed profiles are the first accepted
instances; extracted names, a successful simulation, and a symbol generated from
the same model cannot confirm a physical pinout. This is not a footprint claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from boardmodeler.authoring.pin_roles import terminal_name
from boardmodeler.authoring.spec import SpecSet
from boardmodeler.models.library import subckt_ports
from boardmodeler.models.symbolism import symbol_pin_orders, validate_symbol

PROFILE_PATH = Path(__file__).with_name("reviewed_pinouts.json")
_HASH = re.compile(r"[0-9a-f]{64}")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _name(value: str) -> str:
    return (
        value.strip()
        .upper()
        .translate(str.maketrans({"\u2212": "-", "\u2013": "-", "\u2014": "-"}))
    )


class PinoutError(ValueError):
    """A source, selected package, or physical-to-electrical mapping is unconfirmed."""


@dataclass(frozen=True)
class SourceObservation:
    source_id: str
    url: str
    document_sha256: str
    revision: str
    pdf_page: int
    location: str
    page_image_sha256: str
    pins: tuple[tuple[str, str], ...]
    method: str = "reviewed_figure"

    def __post_init__(self) -> None:
        if not _HASH.fullmatch(self.document_sha256) or not _HASH.fullmatch(self.page_image_sha256):
            raise PinoutError("pinout_source_identity_missing")
        if not self.url or not self.location or self.pdf_page < 0:
            raise PinoutError("pinout_source_location_missing")
        if not self.pins or len(dict(self.pins)) != len(self.pins):
            raise PinoutError("pinout_source_pins_ambiguous")


@dataclass(frozen=True)
class PhysicalPin:
    number: str
    name: str
    aliases: tuple[str, ...]
    spice_order: int

    def accepts(self, name: str) -> bool:
        return _name(name) in {_name(self.name), *(_name(alias) for alias in self.aliases)}


@dataclass(frozen=True)
class PinoutProfile:
    """An independently reviewed package map; callers must not build this from AI output."""

    profile_id: str
    parts: tuple[str, ...]
    primary_document_sha256: str
    packages: tuple[str, ...]
    package_resolution: Literal["exact_package", "pinout_equivalent_group"]
    selected_package: str | None
    pins: tuple[PhysicalPin, ...]
    sources: tuple[SourceObservation, ...]
    confirmation_method: Literal["two_independent_sources", "owner_confirmed"]
    confirmation_reference: str
    scope: str
    device_kind: Literal["integrated_circuit", "discrete"] = "integrated_circuit"
    required_external_connections: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id or not self.parts or not self.packages or not self.pins:
            raise PinoutError("pinout_profile_incomplete")
        expected = {pin.number: pin for pin in self.pins}
        if len(expected) != len(self.pins) or any(not pin.number.strip() for pin in self.pins):
            raise PinoutError("pinout_profile_numbers_ambiguous")
        for pin in self.pins:
            if pin.number != pin.number.strip() or (
                pin.number.isdigit() and str(int(pin.number)) != pin.number
            ):
                raise PinoutError("pinout_profile_number_not_canonical")
        if sorted(pin.spice_order for pin in self.pins) != list(range(1, len(self.pins) + 1)):
            raise PinoutError("pinout_profile_spice_order_ambiguous")
        owners: dict[str, str] = {}
        for pin in self.pins:
            for name in (pin.name, *pin.aliases):
                normalized = _name(name)
                if not normalized or owners.setdefault(normalized, pin.number) != pin.number:
                    raise PinoutError("pinout_profile_alias_ambiguous")
        if (
            self.package_resolution == "exact_package"
            and self.selected_package not in self.packages
        ):
            raise PinoutError("pinout_selected_package_missing")
        if (
            self.package_resolution == "pinout_equivalent_group"
            and self.selected_package is not None
        ):
            raise PinoutError("pinout_group_cannot_select_a_footprint")
        if not self.confirmation_reference or not self.sources:
            raise PinoutError("pinout_confirmation_missing")
        hashes = {source.document_sha256 for source in self.sources}
        if self.primary_document_sha256 not in hashes:
            raise PinoutError("pinout_primary_source_missing")
        if self.confirmation_method == "two_independent_sources" and len(hashes) < 2:
            raise PinoutError("pinout_independent_source_missing")
        for source in self.sources:
            if set(dict(source.pins)) != set(expected):
                raise PinoutError("pinout_sources_disagree: physical pin set")
            for number, name in source.pins:
                if not expected[number].accepts(name):
                    raise PinoutError(f"pinout_sources_disagree: pin {number} {name}")


@dataclass(frozen=True)
class ConfirmedPin:
    physical_pin: str
    source_name: str
    extracted_name: str
    terminal: str
    spice_order: int


@dataclass(frozen=True)
class PinoutContract:
    profile: PinoutProfile
    part: str
    subckt: str
    document_sha256: str
    spec_digest: str
    pins: tuple[ConfirmedPin, ...]

    def payload(self) -> dict[str, Any]:
        return json.loads(
            json.dumps({"schema_version": 1, "record_kind": "pinout_contract", **asdict(self)})
        )

    def digest(self) -> str:
        return _digest(self.payload())

    def to_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def reviewed_profiles() -> tuple[PinoutProfile, ...]:
    """Load only application-owned, reviewed facts; no runtime network or guessed fallback."""
    profiles = []
    for item in json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]:
        values = dict(item)
        values["parts"] = tuple(values["parts"])
        values["packages"] = tuple(values["packages"])
        values["required_external_connections"] = tuple(
            map(tuple, values.get("required_external_connections", ()))
        )
        values["pins"] = tuple(
            PhysicalPin(pin["number"], pin["name"], tuple(pin["aliases"]), pin["spice_order"])
            for pin in values["pins"]
        )
        values["sources"] = tuple(
            SourceObservation(**{**source, "pins": tuple(map(tuple, source["pins"]))})
            for source in values["sources"]
        )
        profiles.append(PinoutProfile(**values))
    return tuple(profiles)


def resolve_reviewed_profile(part: str, document_sha256: str) -> PinoutProfile:
    matches = [profile for profile in reviewed_profiles() if _name(part) in profile.parts]
    if not matches:
        raise PinoutError(
            "pinout_confirmation_missing: no separately confirmed package pinout for this part"
        )
    for profile in matches:
        if profile.primary_document_sha256 == document_sha256:
            return profile
    raise PinoutError("pinout_source_changed: this document revision has no reviewed pinout")


def freeze_pinout(profile: PinoutProfile, spec: SpecSet, document_sha256: str) -> PinoutContract:
    """Bind a confirmed source map to the exact frozen inputs, preserving port spelling/order."""
    if _name(spec.part) not in profile.parts:
        raise PinoutError("pinout_part_mismatch")
    if document_sha256 != profile.primary_document_sha256:
        raise PinoutError("pinout_source_changed")
    expected = {pin.number: pin for pin in profile.pins}
    numbers = [str(pin.get("physical_pin", "")).strip() for pin in spec.pin_map]
    if len(set(numbers)) != len(numbers) or set(numbers) != set(expected):
        raise PinoutError("pinout_physical_pin_set_mismatch: missing, extra, or duplicated pins")
    bound = []
    for raw, number in zip(spec.pin_map, numbers, strict=True):
        pin = expected[number]
        if raw.get("package_resolution") == "unresolved":
            raise PinoutError("pinout_package_unresolved")
        if raw.get("part_id") and _name(str(raw["part_id"])) not in profile.parts:
            raise PinoutError("pinout_pin_part_mismatch")
        if not pin.accepts(str(raw.get("name", ""))):
            raise PinoutError(
                f"pinout_number_name_mismatch: physical pin {number} must be {pin.name}"
            )
        mapped = raw.get("mapped_symbol_pin")
        if mapped and not pin.accepts(str(mapped)):
            raise PinoutError(f"pinout_mapped_terminal_mismatch: physical pin {number}")
        terminal = terminal_name(raw)
        bound.append(ConfirmedPin(number, pin.name, str(raw["name"]), terminal, pin.spice_order))
    if len({pin.terminal for pin in bound}) != len(bound):
        raise PinoutError("pinout_terminal_ambiguous")
    return PinoutContract(
        profile, spec.part, spec.subckt, document_sha256, spec.digest(), tuple(bound)
    )


def pinout_report(
    *,
    part: str,
    document_sha256: str,
    spec_digest: str,
    contract: PinoutContract | None = None,
    reason: str = "",
    library: bytes | None = None,
    symbol: bytes | None = None,
) -> dict[str, Any]:
    """A diagnostic remains useful even when a source or a candidate is unavailable."""
    return {
        "schema_version": 1,
        "record_kind": "pinout_report",
        "status": "BLOCKED"
        if reason
        else ("CONFIRMED" if library is not None and symbol is not None else "SOURCE_CONFIRMED"),
        "publication_allowed": not reason and library is not None and symbol is not None,
        "part": part,
        "document_sha256": document_sha256,
        "spec_digest": spec_digest,
        "contract_sha256": None if contract is None else contract.digest(),
        "contract": None if contract is None else contract.payload(),
        "library_sha256": None if library is None else hashlib.sha256(library).hexdigest(),
        "symbol_sha256": None if symbol is None else hashlib.sha256(symbol).hexdigest(),
        "physical_footprint_selected": False,
        "reason": reason,
        "mapping": [],
        "checks": {"PIN-01": "UNKNOWN", "PIN-02": "UNKNOWN", "PIN-05": "UNKNOWN"},
        "scope": "Static package-to-model/symbol pinout confirmation; no electrical verdict or PCB footprint claim.",
    }


def check_publication(
    contract: PinoutContract,
    *,
    spec: SpecSet,
    document_sha256: str,
    library: bytes,
    symbol: bytes,
    model_file: str,
) -> dict[str, Any]:
    """Check the independently confirmed map against the exact bytes to be published."""
    reason = ""
    mapping: list[dict[str, Any]] = []
    try:
        # Re-derive the complete binding: a replaced profile/map/contract cannot self-certify.
        current = freeze_pinout(contract.profile, spec, document_sha256)
        if current != contract:
            raise PinoutError("pinout_contract_changed: frozen source/spec binding differs")
        ports = subckt_ports(library.decode("utf-8"), contract.subckt)
        expected = {pin.terminal for pin in contract.pins}
        if len(ports) != len(expected) or {port.upper() for port in ports} != expected:
            raise PinoutError("pinout_model_ports_mismatch")
        text = symbol.decode("utf-8")
        for pattern in (r"^PIN\s", r"^PINATTR\s+PinName\s", r"^PINATTR\s+SpiceOrder\s"):
            if len(re.findall(pattern, text, flags=re.MULTILINE)) != len(contract.pins):
                raise PinoutError("pinout_symbol_pin_attributes_malformed")
        for attribute, value in (
            ("Prefix", "X"),
            ("Value", contract.subckt),
            ("Value2", contract.subckt),
        ):
            values = re.findall(rf"^SYMATTR\s+{attribute}\s+(\S+)\s*$", text, flags=re.MULTILINE)
            if values != [value]:
                raise PinoutError(f"pinout_symbol_model_binding_mismatch: {attribute}")
        problems = validate_symbol(text, ports=ports, model_file=model_file)
        if problems:
            raise PinoutError("pinout_symbol_mismatch: " + "; ".join(p.code for p in problems))
        orders = {name.upper(): order for name, order in symbol_pin_orders(text)}
        if any(orders[pin.terminal] != pin.spice_order for pin in contract.pins):
            raise PinoutError("pinout_physical_spice_order_mismatch")
        mapping = [
            {**asdict(pin), "observed_spice_order": orders[pin.terminal]} for pin in contract.pins
        ]
    except (ValueError, KeyError, UnicodeError) as exc:
        reason = str(exc)
    report = pinout_report(
        part=spec.part,
        document_sha256=document_sha256,
        spec_digest=spec.digest(),
        contract=contract,
        reason=reason,
        library=library,
        symbol=symbol,
    )
    report["mapping"] = mapping
    report["checks"] = {
        "PIN-01": "BLOCKED" if reason else "CONFIRMED",
        "PIN-02": "BLOCKED" if reason else "CONFIRMED",
        "PIN-05": (
            "NOT_APPLICABLE" if contract.profile.device_kind == "integrated_circuit" else "BLOCKED"
        ),
    }
    if contract.profile.device_kind == "discrete":
        report.update(
            status="BLOCKED",
            publication_allowed=False,
            reason="pinout_discrete_terminal_contract_missing: primitive/wrapper terminal order is not qualified",
        )
    return report
