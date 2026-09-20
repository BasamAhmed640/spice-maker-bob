"""Datasheet characteristics bound to deterministic probes.

Honesty rule this module implements: every requirement of the extracted
datasheet fixture is either **bound** to a probe that measures it as a single
number, or declared ``not_testable`` with a concrete reason. There is no third
state: a requirement a probe cannot answer is never dropped silently and never
reported as PASS.

The characteristics are *frozen*: :meth:`SpecSet.to_json` is byte-stable and
:meth:`SpecSet.digest` is stable across processes, so the author loop can
recompute the digest from ``<workdir>/spec/characteristics.json`` and detect
tampering (a relaxed tolerance changes the digest).

Limits are normalised to SI base units when a fixture writes SPICE-style
suffixed units (``uA`` -> ``A`` with a 1e-6 scale, ``mV`` -> ``V``, ``kHz``/
``MHz`` -> ``Hz``), so a measured value and a datasheet limit are always
compared in the same unit. Statements and citations keep the datasheet wording.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardmodeler.authoring.probes import PROBES
from boardmodeler.domain.enums import RequirementClass
from boardmodeler.domain.hashing import canonical_json_bytes, sha256_bytes
from boardmodeler.models.library import subckt_ports
from boardmodeler.requirements.model import UnknownUnitError, scale_factor
from boardmodeler.requirements.model import normalize_unit as canonical_unit

__all__ = [
    "Characteristic",
    "SpecSet",
    "load_tps54320_spec",
    "normalize_unit",
    "parse_subckt_ports",
]

#: Decimal prefixes accepted in a fixture unit string. ``m`` is milli, ``M`` is
#: mega (SPICE's ``meg`` is listed separately because ``m`` and ``M`` are only
#: distinguished by case in this vocabulary).
_PREFIX_SCALE: dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "meg": 1e6,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "g": 1e9,
}

#: Units that are already base units (or dimensionless) and never take a prefix.
_BASE_UNITS: tuple[str, ...] = (
    "V",
    "A",
    "S",
    "F",
    "H",
    "Hz",
    "s",
    "ohm",
    "W",
    "J",
    "C",
    "K",
    "V/V",
    "A/A",
    "ratio",
    "cycles",
    "%",
    "dB",
)

_UNIT_RE = re.compile(r"^(?P<prefix>[A-Za-zµμ]*)(?P<base>.*)$")


def normalize_unit(unit: str) -> tuple[str, float]:
    """``(base unit, scale factor)`` for a fixture unit string.

    ``("uA") -> ("A", 1e-6)``; ``("mV") -> ("V", 1e-3)``; ``("kHz") ->
    ("Hz", 1e3)``; ``("ratio") -> ("ratio", 1.0)``. An unrecognised string is
    returned unchanged with scale 1.0 so that an unusual unit is compared
    consistently *within* its own requirement instead of being silently
    rescaled.
    """
    text = unit.strip()
    if not text:
        return "", 1.0
    try:
        base = canonical_unit(text)
        return base, scale_factor(text, base)
    except UnknownUnitError:
        # Preserve the legacy fixture vocabulary and explicit unsupported units.
        # Production extraction has already validated units before this stage.
        pass
    for base in sorted(_BASE_UNITS, key=len, reverse=True):
        if text == base:
            return base, 1.0
        if text.endswith(base) and len(text) > len(base):
            prefix = text[: -len(base)]
            if prefix in _PREFIX_SCALE:
                return base, _PREFIX_SCALE[prefix]
    return text, 1.0


@dataclass(frozen=True)
class Characteristic:
    """One datasheet characteristic, bound to a probe or declared untestable."""

    char_id: str
    statement: str
    unit: str
    min_value: float | None
    max_value: float | None
    typ_value: float | None
    target: float | None
    source_page: int | None
    excerpt: str
    req_class: str
    probe: str | None
    probe_params: dict[str, float]
    not_testable_reason: str | None
    conditions: tuple[dict[str, Any], ...] = ()

    @property
    def has_limits(self) -> bool:
        return self.min_value is not None or self.max_value is not None

    def limits_text(self) -> str:
        """Human description of what the datasheet requires."""
        if self.min_value is not None and self.max_value is not None:
            return f"{self.min_value:g} .. {self.max_value:g} {self.unit}".strip()
        if self.min_value is not None:
            return f">= {self.min_value:g} {self.unit}".strip()
        if self.max_value is not None:
            return f"<= {self.max_value:g} {self.unit}".strip()
        if self.typ_value is not None:
            return f"typ {self.typ_value:g} {self.unit} (+/-10 %)".strip()
        return "no numeric limit"

    def payload(self) -> dict[str, Any]:
        return {
            "char_id": self.char_id,
            "statement": self.statement,
            "unit": self.unit,
            "min_value": _opt_float(self.min_value),
            "max_value": _opt_float(self.max_value),
            "typ_value": _opt_float(self.typ_value),
            "target": _opt_float(self.target),
            "source_page": self.source_page,
            "excerpt": self.excerpt,
            "req_class": self.req_class,
            "probe": self.probe,
            "probe_params": {key: float(value) for key, value in sorted(self.probe_params.items())},
            "not_testable_reason": self.not_testable_reason,
            "conditions": list(self.conditions),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Characteristic:
        params = payload.get("probe_params") or {}
        return cls(
            char_id=str(payload["char_id"]),
            statement=str(payload["statement"]),
            unit=str(payload["unit"]),
            min_value=_opt_float(payload.get("min_value")),
            max_value=_opt_float(payload.get("max_value")),
            typ_value=_opt_float(payload.get("typ_value")),
            target=_opt_float(payload.get("target")),
            source_page=None if payload.get("source_page") is None else int(payload["source_page"]),
            excerpt=str(payload["excerpt"]),
            req_class=str(payload["req_class"]),
            probe=None if payload.get("probe") is None else str(payload["probe"]),
            probe_params={str(k): float(v) for k, v in params.items()},
            not_testable_reason=(
                None
                if payload.get("not_testable_reason") is None
                else str(payload["not_testable_reason"])
            ),
            conditions=tuple(payload.get("conditions") or ()),
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _conditions(requirement: dict) -> tuple[dict[str, Any], ...]:
    return tuple(
        {key: value for key, value in condition.items() if key != "schema_version"}
        for condition in requirement.get("conditions") or ()
    )


@dataclass(frozen=True)
class SpecSet:
    """The frozen characteristic set for one part, plus its doc identity.

    ``digest()`` is computed from the canonical JSON payload, so it changes when
    *anything* the harness judges changes: limits, bindings, deck parameters,
    even the subcircuit name. The author loop uses it to detect a tampered
    specification.
    """

    part: str
    subckt: str
    doc_id: str
    characteristics: tuple[Characteristic, ...]
    pin_map: tuple[dict[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "subckt": self.subckt,
            "doc_id": self.doc_id,
            "characteristics": [char.payload() for char in self.characteristics],
            "pin_map": list(self.pin_map),
        }

    def digest(self) -> str:
        """SHA-256 of the canonical JSON payload (key-sorted, no whitespace)."""
        return sha256_bytes(canonical_json_bytes(self.payload()))

    def to_json(self) -> str:
        """Indented JSON, byte-stable across runs and processes."""
        return json.dumps(self.payload(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SpecSet:
        payload = json.loads(text)
        return cls(
            part=str(payload["part"]),
            subckt=str(payload["subckt"]),
            doc_id=str(payload["doc_id"]),
            characteristics=tuple(
                Characteristic.from_payload(entry) for entry in payload["characteristics"]
            ),
            pin_map=tuple(payload.get("pin_map") or ()),
        )

    def by_probe(self) -> dict[str, tuple[Characteristic, ...]]:
        """Covered characteristics grouped by probe, in first-appearance order."""
        grouped: dict[str, list[Characteristic]] = {}
        for char in self.characteristics:
            if char.probe is None:
                continue
            grouped.setdefault(char.probe, []).append(char)
        return {probe: tuple(chars) for probe, chars in grouped.items()}

    def covered(self) -> tuple[Characteristic, ...]:
        """Characteristics a probe can judge."""
        return tuple(char for char in self.characteristics if char.probe is not None)

    def cases(self) -> tuple[tuple[str, str, tuple[Characteristic, ...]], ...]:
        """Group measurements by probe AND operating point, never discard a corner."""
        cases = []
        for probe_id, chars in self.by_probe().items():
            groups: dict[str, list[Characteristic]] = {}
            for char in chars:
                fingerprint = sha256_bytes(canonical_json_bytes(char.payload()["probe_params"]))[
                    :12
                ]
                groups.setdefault(fingerprint, []).append(char)
            for fingerprint, group in groups.items():
                name = probe_id if len(groups) == 1 else f"{probe_id}-{fingerprint}"
                cases.append((name, probe_id, tuple(group)))
        return tuple(cases)

    def uncovered(self) -> tuple[Characteristic, ...]:
        """Characteristics reported ``not_testable`` with a reason."""
        return tuple(char for char in self.characteristics if char.probe is None)

    def by_id(self, char_id: str) -> Characteristic:
        for char in self.characteristics:
            if char.char_id == char_id:
                return char
        raise KeyError(char_id)


def parse_subckt_ports(text: str, *, name: str | None = None) -> list[str]:
    """Port names of a ``.subckt`` declaration, in order.

    Without ``name`` the first declaration in the file is used (the contract
    shape); with ``name`` the named subcircuit is selected, which is what the
    harness needs when a library holds primitives before the model under test.
    Continuation lines and a ``params:`` clause are handled.

    Raises :class:`boardmodeler.models.library.ModelStoreError` when the
    subcircuit is not declared; an empty port list would otherwise be
    indistinguishable from a declaration with no ports.
    """
    return list(subckt_ports(text, name))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _req_class(raw: Any) -> str:
    text = str(raw)
    for member in RequirementClass:
        if text == member.value or text == member.name:
            return member.value
    return text


def load_tps54320_spec(
    requirements_path: Path, bindings_path: Path, *, part: str, subckt: str
) -> SpecSet:
    """Build the frozen characteristic set from the fixture pair.

    ``requirements_path`` is the extracted requirement list (the datasheet
    oracle, one entry per requirement id). ``bindings_path`` is this harness's
    binding file: for every requirement id exactly one entry, either
    ``{"req_id": ..., "probe": <probe id>, "params": {...}}`` or
    ``{"req_id": ..., "probe": null, "not_testable_reason": "..."}``.

    Validation is part of the honesty rule: a bound requirement must declare a
    numeric limit (otherwise the harness would have nothing to judge), an
    unbound requirement must carry a reason, and the two files must agree on the
    requirement id set. Any violation raises :class:`ValueError` instead of
    producing a spec the harness cannot defend.
    """
    requirements = _read_json(Path(requirements_path))
    bindings = _read_json(Path(bindings_path))
    if not isinstance(requirements, dict) or "requirements" not in requirements:
        raise ValueError(f"{requirements_path} has no 'requirements' list")
    if not isinstance(bindings, dict) or "bindings" not in bindings:
        raise ValueError(f"{bindings_path} has no 'bindings' list")

    doc_id = str(requirements.get("document", {}).get("doc_id", ""))

    binding_by_id: dict[str, dict[str, Any]] = {}
    for entry in bindings["bindings"]:
        req_id = str(entry.get("req_id", ""))
        if not req_id:
            raise ValueError(f"binding entry without req_id: {entry!r}")
        if req_id in binding_by_id:
            raise ValueError(f"duplicate binding for {req_id}")
        binding_by_id[req_id] = entry

    characteristics: list[Characteristic] = []
    seen: set[str] = set()
    for requirement in requirements["requirements"]:
        req_id = str(requirement["req_id"])
        seen.add(req_id)
        if req_id not in binding_by_id:
            raise ValueError(f"{req_id} has no entry in {bindings_path}")
        binding = binding_by_id[req_id]
        limits = requirement.get("limits")
        evidence = requirement.get("evidence") or []
        page = None
        excerpt = ""
        if evidence:
            excerpt = str(evidence[0].get("excerpt", ""))
            page_ref = evidence[0].get("page") or {}
            if page_ref.get("pdf_page") is not None:
                page = int(page_ref["pdf_page"])

        unit = ""
        min_value = max_value = typ_value = None
        if isinstance(limits, dict):
            base, scale = normalize_unit(str(limits.get("unit", "")))
            unit = base
            min_value = _scaled(limits.get("min"), scale)
            max_value = _scaled(limits.get("max"), scale)
            typ_value = _scaled(limits.get("typ"), scale)

        probe = binding.get("probe")
        if probe is None:
            reason = str(binding.get("not_testable_reason") or "").strip()
            if not reason:
                raise ValueError(f"{req_id} binds no probe but declares no not_testable_reason")
            characteristics.append(
                Characteristic(
                    char_id=req_id,
                    statement=str(requirement["statement"]),
                    unit=unit,
                    min_value=min_value,
                    max_value=max_value,
                    typ_value=typ_value,
                    target=typ_value,
                    source_page=page,
                    excerpt=excerpt,
                    req_class=_req_class(requirement.get("class")),
                    probe=None,
                    probe_params={},
                    not_testable_reason=reason,
                    conditions=_conditions(requirement),
                )
            )
            continue

        probe_id = str(probe)
        if probe_id not in PROBES:
            raise ValueError(f"{req_id} binds unknown probe {probe_id!r}")
        if not isinstance(limits, dict) or (
            min_value is None and max_value is None and typ_value is None
        ):
            raise ValueError(
                f"{req_id} binds probe {probe_id!r} but declares no numeric limit; "
                "a bound characteristic must be judgeable"
            )
        params = binding.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"{req_id} params must be an object")
        characteristics.append(
            Characteristic(
                char_id=req_id,
                statement=str(requirement["statement"]),
                unit=unit,
                min_value=min_value,
                max_value=max_value,
                typ_value=typ_value,
                target=typ_value,
                source_page=page,
                excerpt=excerpt,
                req_class=_req_class(requirement.get("class")),
                probe=probe_id,
                probe_params={str(k): float(v) for k, v in params.items()},
                not_testable_reason=None,
                conditions=_conditions(requirement),
            )
        )

    unknown = sorted(set(binding_by_id) - seen)
    if unknown:
        raise ValueError(f"{bindings_path} binds requirements not in the fixture: {unknown}")
    return SpecSet(
        part=part,
        subckt=subckt,
        doc_id=doc_id,
        characteristics=tuple(characteristics),
        pin_map=tuple(requirements.get("pin_map") or ()),
    )


def _scaled(value: Any, scale: float) -> float | None:
    return None if value is None else float(value) * scale
