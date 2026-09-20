"""Plan device-specific fixtures independently of the model-writing turn."""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from boardmodeler.authoring.backends import AuthorRequest
from boardmodeler.authoring.circuit_probe import CircuitRecipe
from boardmodeler.authoring.pin_roles import physical_terminals
from boardmodeler.domain.enums import RequirementClass
from boardmodeler.providers.http_inference import extract_json_object
from boardmodeler.requirements.model import UnknownUnitError, normalize_unit, scale_factor

VERSION = "frozen-circuit-planner-v1"


def _envelope(req):
    """An input operating envelope is not an output measurement specification."""
    text = " ".join([req.statement, *(str(e.section or "") for e in req.evidence)])
    return bool(
        re.search(
            r"recommended (?:operating )?(?:conditions|supply|input|output current)|recommended operating",
            text,
            re.I,
        )
    )


def _ac_open_loop(recipe, pin_map):
    """Compile a unity follower into a DC-stable, differential open-loop AC bench.

    Only the unambiguous direct-feedback topology is rewritten. No numeric
    acceptance target, load, supply or operating bias is changed.
    """
    from boardmodeler.authoring.pin_roles import terminal_name

    m = recipe.measurement
    if not any(
        p.get("function", "").lower().replace("-", "").startswith("noninverting")
        for p in pin_map or []
    ):
        return recipe
    if m.operation not in ("gain", "unity_frequency") or not m.reference:
        return recipe
    if "," in m.reference:
        return recipe
    if not m.signal.startswith("V(") or not m.reference.startswith("V("):
        return recipe
    output, plus = m.signal[2:-1], m.reference[2:-1]
    minus_pins = [
        terminal_name(p)
        for p in pin_map or []
        if p.get("function", "").lower().startswith("inverting")
        and recipe.terminals.get(terminal_name(p)) == output
    ]
    plus_pins = [
        p
        for p in pin_map or []
        if p.get("function", "").lower().replace("-", "").startswith("noninverting")
        and recipe.terminals.get(terminal_name(p)) == plus
    ]
    if len(minus_pins) != 1 or len(plus_pins) != 1:
        raise ValueError(
            "open_loop_fixture: gain/GBW needs the observed differential DUT input, not a follower input"
        )
    if any("bm_ac_" in line.lower() for line in recipe.components):
        raise ValueError("open_loop_fixture: reserved AC fixture nodes already in use")
    lines = [
        re.sub(r"\bAC\s+[+-]?[\d.eE]+(?:\s+[+-]?[\d.eE]+)?", "AC 0", line, flags=re.I)
        for line in recipe.components
    ]
    lines.append(f"Vbm_ac_feedback {output} bm_ac_minus DC 0 AC 1")
    return recipe.model_copy(
        update={
            "terminals": {**recipe.terminals, minus_pins[0]: "bm_ac_minus"},
            "components": lines,
            "measurement": m.model_copy(update={"reference": f"V({plus},bm_ac_minus)"}),
            "condition_evidence": recipe.condition_evidence
            + " Open-loop AC gain is output divided by differential DUT input; a series AC source preserves DC feedback.",
        }
    )


def plan_bindings(
    requirements,
    pin_map,
    backend,
    folder: Path,
    *,
    part,
    unverified,
    cancel=None,
    progress=None,
    _context=None,
    _fill_missing=True,
):
    terminals = physical_terminals(pin_map)
    source = {
        "part": part,
        "pin_map": pin_map,
        "terminals": terminals,
        "requirements": [json.loads(r.model_dump_json(by_alias=True)) for r in requirements],
        "unverified": dict(unverified),
        "version": VERSION,
        **({"context": _context} if _context is not None else {}),
    }
    encoded = json.dumps(source, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / f"{digest}.json"

    def complete(entries):
        from boardmodeler.authoring.standard_fixtures import opamp_output_swing

        requirements_by_id = {r.req_id: r for r in requirements}
        entries = [dict(entry) for entry in entries]
        for entry in entries:
            req = requirements_by_id[entry["req_id"]]
            if (
                entry.get("probe") is None
                and req.req_id not in unverified
                and req.req_class
                not in (RequirementClass.UNKNOWN, RequirementClass.ABSOLUTE_MAXIMUM)
            ):
                recipe = opamp_output_swing(req, pin_map, requirements)
                if recipe:
                    entry.clear()
                    entry.update(
                        req_id=req.req_id, probe="circuit_measurement", recipe=recipe, params={}
                    )
        if not _fill_missing:
            return entries
        missing = {
            e["req_id"]
            for e in entries
            if e.get("not_testable_reason", "").startswith("test planner did not supply")
        }
        pending = [
            r
            for r in requirements
            if r.req_id in missing
            and r.limits is not None
            and r.req_class not in (RequirementClass.UNKNOWN, RequirementClass.ABSOLUTE_MAXIMUM)
            and r.req_id not in unverified
            and not _envelope(r)
        ]
        if not pending:
            return entries
        if progress:
            progress(f"completing {len(pending)} omitted test fixtures in smaller requests")
        replacements = []
        for start in range(0, len(pending), 8):
            replacements.extend(
                plan_bindings(
                    pending[start : start + 8],
                    pin_map,
                    backend,
                    folder,
                    part=part,
                    unverified=unverified,
                    cancel=cancel,
                    progress=progress,
                    _context={"scope": "complete missing rows", "records": source["requirements"]},
                    _fill_missing=False,
                )
            )
        by_id = {e["req_id"]: e for e in replacements}
        result = [by_id.get(e["req_id"], e) for e in entries]
        cache.write_text(json.dumps({"bindings": result}), encoding="utf-8")
        return result

    # Older punctuation normalization removed Unicode minus signs. Migrate a
    # cache only when its complete source hash matches, and remap physical pins
    # one-to-one using the same cited pin map.
    legacy = []
    for pin in pin_map:
        name = str(pin.get("mapped_symbol_pin") or pin["name"]).upper()
        name = name.replace("+", "P").replace("\u2212", "M").replace("-", "M")
        name = re.sub(r"[^A-Z0-9_]", "_", name).strip("_")
        if not name or name[0].isdigit():
            name = "P_" + name
        if pin.get("direction") == "nc":
            name += "_" + re.sub(r"[^A-Za-z0-9]", "_", str(pin["physical_pin"]))
        legacy.append(name)
    if tuple(legacy) != terminals and len(set(legacy)) == len(legacy) and not cache.exists():
        old_digest = hashlib.sha256(
            json.dumps({**source, "terminals": legacy}, sort_keys=True).encode()
        ).hexdigest()
        old_cache = folder / f"{old_digest}.json"
        if old_cache.is_file():
            payload = json.loads(old_cache.read_text(encoding="utf-8"))
            aliases = dict(zip(legacy, terminals, strict=True))
            for entry in payload["bindings"]:
                if entry.get("recipe"):
                    entry["recipe"]["terminals"] = {
                        aliases.get(k, k): v for k, v in entry["recipe"]["terminals"].items()
                    }
            entries = validate_plan(payload, requirements, terminals, unverified, pin_map)
            cache.write_text(json.dumps({"bindings": entries}), encoding="utf-8")
            return complete(entries)
    if cache.is_file():
        return complete(
            validate_partial_plan(
                json.loads(cache.read_text(encoding="utf-8")),
                requirements,
                terminals,
                unverified,
                pin_map,
            )
        )
    # A locally unsupported row must not force another paid rewrite of valid
    # circuits. Revalidate saved, credential-redacted replies against this code.
    for saved in sorted(folder.glob(f"{digest}-attempt-*.json"), reverse=True):
        try:
            payload = extract_json_object(
                json.loads(saved.read_text(encoding="utf-8"))["response"], secrets=()
            )
            entries = validate_plan(payload, requirements, terminals, unverified, pin_map)
            cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return complete(entries)
        except ValueError, TypeError, KeyError:
            continue
    if len(requirements) > 16:
        excluded, eligible = [], []
        for req in requirements:
            reason = None
            if req.req_id in unverified:
                reason = "citation_unverified: test circuit cannot be grounded in the cited page"
            elif req.req_class in (RequirementClass.UNKNOWN, RequirementClass.ABSOLUTE_MAXIMUM):
                reason = "unknown classification or absolute stress rating, not an operating target"
            elif req.limits is None:
                reason = (
                    "no numeric limit; this fixture interpreter measures numeric characteristics"
                )
            elif _envelope(req):
                reason = "operating envelope defines fixture conditions; it is not a measured DUT response"
            else:
                try:
                    normalize_unit(req.limits.unit)
                except UnknownUnitError:
                    reason = "unsupported unit preserved without conversion"
            if reason:
                excluded.append(
                    {"req_id": req.req_id, "probe": None, "not_testable_reason": reason}
                )
            else:
                eligible.append(req)
        context = [
            {
                "statement": r.statement,
                "limits": r.limits.model_dump() if r.limits else None,
                "conditions": [c.model_dump() for c in r.conditions],
            }
            for r in requirements
            if r.req_class != RequirementClass.ABSOLUTE_MAXIMUM
        ]
        groups = [eligible[i : i + 12] for i in range(0, len(eligible), 12)]
        if progress:
            progress(f"planning {len(groups)} smaller cached fixture batches")

        def batch(group):
            return plan_bindings(
                group,
                pin_map,
                backend,
                folder,
                part=part,
                unverified=unverified,
                cancel=cancel,
                progress=progress,
                _context=context,
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            result = excluded + [entry for group in pool.map(batch, groups) for entry in group]
        cache.write_text(json.dumps({"bindings": result}, indent=2), encoding="utf-8")
        return result
    prompt_source = {
        **source,
        "pin_map": [
            {
                k: v
                for k, v in p.items()
                if v not in (None, [], {}) and k not in ("schema_version", "evidence")
            }
            for p in pin_map
        ],
        "requirements": [
            r.model_dump(mode="json", by_alias=True, exclude_defaults=True, exclude_none=True)
            for r in requirements
        ],
    }
    if isinstance(_context, dict) and "records" in _context:
        prompt_source["context"] = [
            {k: r[k] for k in ("statement", "limits", "conditions") if r.get(k)}
            for r in _context["records"]
        ]
    prompt = (
        "You are designing independent LTspice measurement fixtures from datasheet evidence. "
        "No device model exists yet. Do not write or assume a model, do not change any limit. "
        'Return JSON {"bindings":[...]} with exactly one entry per req_id, no omitted rows. '
        "An entry is {req_id,probe:null,not_testable_reason:string}, or "
        "{req_id,probe:'circuit_measurement',recipe:<CircuitRecipe>}. Use JSON double quotes. "
        "Cover meaningful core function and electrical characteristics, not just leakage or supply "
        "current. Preserve every distinct load, rail, signal polarity, direction and channel. "
        "Only choose a fixture when its quantity, operating point and signal-to-pin mapping are "
        "established by the supplied evidence. Unverified citations, absolute maximum ratings, "
        "thermal/statistical/packaging requirements and unsupported measurements stay explicit gaps. "
        "Temperature is nominal 25 C only; a wider datasheet range is not a temperature verification. "
        "For range-qualified limits choose a stated in-range point and name that limited scope. "
        "Never replace a recommended input-voltage range with a switching threshold test. "
        "Select the requested exact variant; other voltages, channel counts or packages are gaps. "
        "All primitive values, measurements and operating_point entries use SI units. recipe.unit "
        "is the normalized requirement unit. For percent output tolerance, measure actual output "
        "voltage with scale=100/Vnom and offset=-100. For relative bounds record the named variable "
        "(e.g. VCC) at the exact tested point in operating_point, in the limit's unit. "
        "Use terminals to connect EVERY listed physical terminal to a fixture node (NC may have "
        "a unique floating node). Do not invent terminals. Components may only be R,C,L,V,I,B,E,F,G,H "
        "primitive lines, no directives or model instances. The program inserts the DUT and analysis. "
        "Include ALL supplies, enable/direction controls and loads required by this device. Terminate "
        "unused amplifier channels in stable followers; do not short outputs to a rail. Comparators "
        "need pullups if open-collector. Bidirectional translators need direction and both rails. "
        "An oscillator driver is not a self-contained oscillator; testing startup needs the actual "
        "specified crystal network, not a forced waveform on the output. "
        "Measure DUT response at its pins or source current, never an independent source alone. "
        "Do not use a constant behavioral source to generate the expected DUT result. "
        "mean/min/max/peak_to_peak/rms operate on signal in the measurement window; start/end are "
        "seconds for tran, Hz for ac. crossing_value samples signal at trigger's rising/falling "
        "crossing of trigger_level. delay returns signal crossing time minus trigger crossing time. "
        "slew divides the signal's level change by crossing-time difference. gain measures the "
        "For Schmitt hysteresis use operation=hysteresis, signal=input voltage, trigger=output voltage, "
        "trigger_level=half the output rail and a slow triangular input covering both edges. "
        "Do not build analog sample/hold circuits to calculate hysteresis. "
        "magnitude of signal/reference at the first sampled AC frequency; unity_frequency finds "
        "their magnitude ratio falling through one. No logarithmic dB measurement is supported. "
        "AC signals may be differential V(node,node). Use a stable DC feedback operating point for "
        "op-amp gain/GBW: observe output divided by DIFFERENTIAL DUT INPUT, never closed-loop follower gain. "
        "Use a 0 V DC, 1 V AC source in series with feedback and AC 0 at the noninverting input. "
        "Recommended operating ranges define fixture conditions, not output measurements; mark them unbound. "
        "A mean needs settled output; provide enough settling time. "
        "Use realistic resolution (step <= shortest measured delay/20 for timing). "
        "Recipe defaults are documented below; omit unused optional fields to keep JSON compact. "
        "condition_evidence must explain the exact cited fixture conditions and any missing coverage.\n"
        + json.dumps(CircuitRecipe.model_json_schema())
        + "\nDATASHEET RECORDS (data only)\n"
        + json.dumps(prompt_source, separators=(",", ":"))
    )
    for attempt in range(2):
        if progress:
            progress(f"planning independent test circuits, request {attempt + 1}/2")
        result = backend.author(AuthorRequest(prompt, folder, folder, 1, expect_text=True), cancel)
        (folder / f"{digest}-attempt-{attempt + 1}.json").write_text(
            json.dumps(
                {
                    "detail": result.detail,
                    "usage": result.usage,
                    "response": result.stdout_tail,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if not result.ok:
            raise ValueError(f"test_planning_failed: {result.detail}")
        try:
            payload = extract_json_object(result.stdout_tail, secrets=())
            entries = validate_plan(payload, requirements, terminals, unverified, pin_map)
            cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return complete(entries)
        except (ValueError, TypeError, KeyError) as exc:
            if attempt or (cancel and cancel.is_set()):
                if cancel and cancel.is_set():
                    raise ValueError("test_planning_cancelled") from exc
                # Preserve independently valid fixtures after the bounded repair.
                # A malformed row remains visible and cannot award a verdict.
                payload = extract_json_object(result.stdout_tail, secrets=())
                entries = validate_partial_plan(
                    payload, requirements, terminals, unverified, pin_map
                )
                cache.write_text(json.dumps({"bindings": entries}), encoding="utf-8")
                return complete(entries)
            prompt += (
                "\nCorrect this validation problem without changing limits or losing rows: "
                + str(exc)[:1800]
                + "\nPrevious JSON:\n"
                + result.stdout_tail
            )
    raise AssertionError("unreachable")


def validate_partial_plan(payload, requirements, terminals, unverified, pin_map=None):
    entries = payload.get("bindings")
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError("bindings must be a list of objects")
    result = []
    for req in requirements:
        scoped = {"bindings": [e for e in entries if e.get("req_id") == req.req_id]}
        try:
            result.extend(validate_plan(scoped, [req], terminals, unverified, pin_map))
        except (ValueError, TypeError, KeyError) as exc:
            result.append(
                {
                    "req_id": req.req_id,
                    "probe": None,
                    "not_testable_reason": f"invalid test fixture: {str(exc)[:300]}; no measurement accepted",
                }
            )
    return result


def validate_plan(payload, requirements, terminals, unverified, pin_map=None):
    entries = payload["bindings"]
    if not isinstance(entries, list):
        raise ValueError("bindings must be a list")
    by_id = {r.req_id: r for r in requirements}
    # Discard out-of-scope proposals, never source requirements. Conflicting
    # duplicates become a visible gap; one malformed row cannot erase the
    # independently valid fixtures for every other row.
    unique = {}
    for entry in entries:
        key = entry.get("req_id")
        if key not in by_id:
            continue
        if key in unique and unique[key] != entry:
            unique[key] = {
                "req_id": key,
                "probe": None,
                "not_testable_reason": "conflicting duplicate fixture proposals; no measurement accepted",
            }
        else:
            unique[key] = entry
    entries = list(unique.values())
    ids = set(unique)
    entries = [
        *entries,
        *(
            {
                "req_id": key,
                "probe": None,
                "not_testable_reason": "test planner did not supply a fixture; coverage remains unknown",
            }
            for key in by_id
            if key not in ids
        ),
    ]
    result = []
    for entry in entries:
        req = by_id[entry["req_id"]]

        def decline(reason, req_id=req.req_id):
            result.append({"req_id": req_id, "probe": None, "not_testable_reason": reason})

        if entry.get("probe") is None:
            if not entry.get("not_testable_reason"):
                raise ValueError(f"{req.req_id}: an unbound row needs its reason")
            result.append(
                {
                    "req_id": req.req_id,
                    "probe": None,
                    "not_testable_reason": entry["not_testable_reason"],
                }
            )
            continue
        if entry["probe"] != "circuit_measurement":
            raise ValueError(f"{req.req_id}: unsupported probe")
        if req.req_id in unverified or req.req_class in (
            RequirementClass.UNKNOWN,
            RequirementClass.ABSOLUTE_MAXIMUM,
        ):
            decline("citation_unverified or unknown/stress classification; no operating verdict")
            continue
        if req.limits is None:
            raise ValueError(f"{req.req_id}: a numeric measurement needs a numeric limit")
        if _envelope(req):
            decline(
                "operating envelope defines fixture conditions; it is not a measured DUT response"
            )
            continue
        from boardmodeler.authoring.standard_fixtures import declared_delay_threshold

        recipe = CircuitRecipe.model_validate(declared_delay_threshold(entry["recipe"], req))
        recipe = _ac_open_loop(recipe, pin_map)
        m = recipe.measurement
        if m.operation in ("mean", "min", "max", "peak_to_peak", "rms"):
            if m.signal.upper() == "V(0)":
                raise ValueError(
                    f"{req.req_id}: fixture measures forced ground, not a DUT response"
                )
            if re.fullmatch(r"I\(I[^)]+\)", m.signal, re.I):
                raise ValueError(
                    f"{req.req_id}: fixture measures a forced independent current, not a DUT response"
                )
            simple_voltage = re.fullmatch(r"V\((\w+)\)", m.signal, re.I)
            if simple_voltage and any(
                re.match(rf"V\w*\s+{re.escape(simple_voltage[1])}\s+0\s", line, re.I)
                for line in recipe.components
            ):
                raise ValueError(
                    f"{req.req_id}: fixture measures a forced independent voltage, not a DUT response"
                )
        if set(recipe.terminals) != set(terminals):
            raise ValueError(f"{req.req_id}: fixture must connect exactly {terminals}")
        try:
            expected = normalize_unit(req.limits.unit)
        except UnknownUnitError as exc:
            decline(f"unsupported_unit: {exc}; raw datasheet unit retained without conversion")
            continue
        if recipe.unit != expected:
            try:
                factor = scale_factor(recipe.unit, expected)
            except ValueError:
                decline(
                    f"fixture_quantity_mismatch: {recipe.unit} cannot be compared with {expected}"
                )
                continue
            recipe = recipe.model_copy(
                update={
                    "unit": expected,
                    "measurement": recipe.measurement.model_copy(
                        update={
                            "scale": recipe.measurement.scale * factor,
                            "offset": recipe.measurement.offset * factor,
                        }
                    ),
                }
            )
        if expected == "dB":
            decline("logarithmic measurements are unsupported by this fixture interpreter")
            continue
        # Relative formulas may only become numbers at an explicit, finite point.
        for side in ("min", "typ", "max"):
            bound = getattr(req.limits, side + "_relative")
            if bound is not None and (
                bound.parameter not in recipe.operating_point
                or not math.isfinite(recipe.operating_point[bound.parameter])
            ):
                raise ValueError(
                    f"{req.req_id}: relative bound requires explicit {bound.parameter}"
                )
        result.append(
            {
                "req_id": req.req_id,
                "probe": "circuit_measurement",
                "params": {},
                "recipe": recipe.model_dump(mode="json"),
            }
        )
    return result
