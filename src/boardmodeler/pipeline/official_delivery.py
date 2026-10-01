"""Prefer a manufacturer's unchanged model; a load check is not electrical qualification.

Discovery is intentionally bounded and deterministic. No AI writes URLs, model text,
test limits, or compatibility verdicts here. Other manufacturers can add a product
page resolver without adding a part-number whitelist or bypassing containment.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from boardmodeler.authoring.sanity import load_check
from boardmodeler.documents.pdf import read_pdf
from boardmodeler.models.support import decide_support
from boardmodeler.models.symbolism import symbol_text, validate_symbol
from boardmodeler.security.network import internet_allowed
from boardmodeler.simulation.ltspice import locate

if TYPE_CHECKING:
    from boardmodeler.pipeline.make_model import MakeModelRequest, MakeModelResult


def product_page(
    part: str, head: str, *, manufacturer_context: str | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """Resolve only a manufacturer positively named by the supplied PDF.

    Match the longest part token in the document, rather than guessing an ordering
    suffix. This does not infer a physical package or a pin map.
    """
    requested = part.upper().strip()
    tokens = set(re.findall(r"\b[A-Z]{1,8}[0-9][A-Z0-9-]*\b", head.upper()))
    matches = [token for token in tokens if requested == token or requested.startswith(token)]
    if not matches:
        return None
    base = max(matches, key=len)
    if len(base) < 4:
        return None
    lower = (head if manufacturer_context is None else manufacturer_context).lower()
    if "texas instruments" in lower or "www.ti.com" in lower:
        return f"https://www.ti.com/product/{base}", ("ti.com",)
    if "analog devices" in lower or "analog.com" in lower or "linear technology" in lower:
        return f"https://www.analog.com/en/products/{base.lower()}.html", ("analog.com",)
    return None


def _json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def try_official_delivery(
    request: MakeModelRequest, record, head: str, out_dir: Path, log, cancel=None
) -> MakeModelResult | None:
    """Return a delivered official result, or record why generation may proceed.

    A native LTspice candidate that cannot be verified is withheld. An incompatible
    PSpice candidate is explicitly recorded and cannot silently become an adaptation.
    All saved originals retain their original filenames, bytes and entry points.
    """
    from boardmodeler.authoring.official_spice import (
        OfficialSpiceError,
        acquire_official_spice,
        discover_model_links,
        fetch_official_bytes,
    )
    from boardmodeler.pipeline.make_model import MakeModelResult

    if request.engine != "behavioral" or request.backend_name in ("fixture", "scripted"):
        return None
    if request.requirements_json is not None or record is None:
        return None
    identity = decide_support(request.part, title=record.title, head=head)
    if identity.state == "blocked_class":
        # The caller's normal exclusion gate will record the same refusal.
        return None
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "record_kind": "official_model_discovery",
        "part": request.part,
        "datasheet_sha256": record.file_hash,
        "electrical_accuracy_verified": False,
        "attempts": [],
    }
    report_path = out_dir / "official-model.json"
    if not internet_allowed():
        report.update(status="skipped", reason="internet_access_off")
        _json(report_path, report)
        return None
    if not head:
        report.update(status="skipped", reason="manufacturer_product_page_unresolved")
        _json(report_path, report)
        return None
    if hashlib.sha256(Path(request.datasheet).read_bytes()).hexdigest() != record.file_hash:
        raise ValueError("datasheet_changed_before_official_discovery")
    try:
        document = read_pdf(Path(request.datasheet), max_pages=3)
    except Exception as exc:
        raise ValueError("official_datasheet_unreadable: " + type(exc).__name__) from exc
    context = " ".join(page.text for page in document.pages)[:18000]
    source = product_page(request.part, head, manufacturer_context=context)
    if not internet_allowed() or source is None:
        report.update(
            status="skipped",
            reason="internet_access_off"
            if not internet_allowed()
            else "manufacturer_product_page_unresolved",
        )
        _json(report_path, report)
        return None
    product_url, hosts = source
    report["product_url"] = product_url
    log.emit("official", "running", "checking the manufacturer's model downloads")
    selected = None
    native_failure = None
    bundle = None
    entry = None
    loaded = None
    ports = ()
    prefix = "X"
    try:
        page, _media_type = fetch_official_bytes(
            product_url,
            allowed_hosts=hosts,
            timeout_s=12,
            max_bytes=2 * 1024 * 1024,
            accepted_types=frozenset({"text/html"}),
        )
        links = discover_model_links(product_url, page, allowed_hosts=hosts)
        for link in links[:3]:
            if cancel is not None and cancel.is_set():
                break
            remaining = 35 - (time.monotonic() - started)
            if remaining <= 6:
                break
            attempt = {"url": link.url, "label": link.label, "native_ltspice": link.native_ltspice}
            report["attempts"].append(attempt)
            try:
                bundle = acquire_official_spice(
                    link.url,
                    out_dir / "vendor-originals",
                    allowed_hosts=hosts,
                    timeout_s=min(12, remaining - 5),
                )
                base = product_url.rsplit("/", 1)[-1].removesuffix(".html").upper()
                matched = [
                    candidate
                    for candidate in bundle.entries
                    if candidate.name.upper() == base
                    or re.match(re.escape(base) + r"[_-]", candidate.name.upper())
                ]
                exact = [candidate for candidate in matched if candidate.name.upper() == base]
                if len(exact) == 1:
                    matched = exact
                if len(matched) != 1:
                    raise OfficialSpiceError(
                        "model_entry_ambiguous: exactly one part-matching SPICE entry is required"
                    )
                entry = matched[0]
                if entry.kind == "subckt":
                    ports, prefix = entry.ports, "X"
                else:
                    primitive = {
                        "D": (("A", "K"), "D"),
                        "NPN": (("C", "B", "E"), "Q"),
                        "PNP": (("C", "B", "E"), "Q"),
                        "NMOS": (("D", "G", "S", "B"), "M"),
                        "PMOS": (("D", "G", "S", "B"), "M"),
                        "VDMOS": (("D", "G", "S"), "M"),
                    }.get(entry.model_type)
                    if primitive is None:
                        raise OfficialSpiceError("primitive_model_type_unsupported")
                    ports, prefix = primitive
                install = locate()
                loaded = load_check(
                    entry.file,
                    entry.name,
                    bundle.bundle_dir.parent / "compatibility" / uuid.uuid4().hex,
                    None if install is None else install.path,
                    cancel,
                    ports=ports,
                    element_prefix=prefix,
                )
                attempt["load_check"] = loaded
                if loaded.get("status") != "loaded":
                    raise OfficialSpiceError(
                        "ltspice_compatibility_unverified: " + loaded.get("status", "unknown")
                    )
            except (OfficialSpiceError, OSError, ValueError) as exc:
                attempt["reason"] = str(exc)
                if link.native_ltspice:
                    native_failure = link
                    report["reason"] = str(exc)
                continue
            selected = link
            break
    except (OfficialSpiceError, OSError, ValueError) as exc:
        report.update(status="unavailable", reason=str(exc))
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    if selected is None and native_failure is not None:
        selected = native_failure
        report["status"] = "withheld"
    if selected is None:
        report.setdefault("status", "no_compatible_candidate")
        _json(report_path, report)
        log.emit(
            "official", "skipped", "official lookup recorded; continuing with the code-built engine"
        )
        return None
    if report.get("status") == "withheld":
        _json(report_path, report)
        detail = "official_model_withheld: manufacturer LTspice model could not be verified; see official-model.json"
        log.emit("official", "failed", detail)
        result = MakeModelResult(
            "BLOCKED",
            detail,
            request.part,
            out_dir,
            None,
            None,
            None,
            (),
            {"PASS": 0, "FAIL": 0, "UNKNOWN": 1, "BLOCKED": 0, "NOT_APPLICABLE": 0},
            tuple(log.events),
            request,
        )
    else:
        assert bundle is not None and entry is not None and loaded is not None
        # Rehash every delivered file after simulation: provenance describes delivered bytes.
        hashes = {
            item.relative_path: hashlib.sha256(
                (bundle.bundle_dir / item.relative_path).read_bytes()
            ).hexdigest()
            for item in bundle.files
        }
        if any(hashes[item.relative_path] != item.sha256 for item in bundle.files):
            raise ValueError("official_model_changed_during_validation")
        lib_hash = hashlib.sha256(entry.file.read_bytes()).hexdigest()
        package = bundle.bundle_dir.parent / "source.zip"
        if (
            package.is_file()
            and hashlib.sha256(package.read_bytes()).hexdigest() != bundle.source_sha256
        ):
            raise ValueError("official_source_package_changed_during_validation")
        symbol_path = out_dir / f"{request.subckt}.asy"
        relative = entry.file.relative_to(out_dir).as_posix()
        symbol = symbol_text(
            request.subckt,
            ports,
            model_file=relative,
            model_name=entry.name,
            description="Official manufacturer model; model ports, not package pin numbers",
        )
        if prefix != "X":
            symbol = symbol.replace("SYMATTR Prefix X", f"SYMATTR Prefix {prefix}")
        if validate_symbol(symbol, ports=ports, model_file=relative):
            raise ValueError("official_symbol_validation_failed")
        symbol_path.write_text(symbol, encoding="utf-8")
        report.update(
            status="delivered",
            source_url=bundle.source_url,
            final_url=bundle.final_url,
            source_package_path=package.relative_to(out_dir).as_posix()
            if package.is_file()
            else None,
            bundle_root=bundle.bundle_dir.relative_to(out_dir).as_posix(),
            source_sha256=bundle.source_sha256,
            model_path=relative,
            model_sha256=lib_hash,
            files=hashes,
            entry_point=entry.name,
            model_ports=list(ports),
            entry_kind=entry.kind,
            element_prefix=prefix,
            symbol_sha256=hashlib.sha256(symbol_path.read_bytes()).hexdigest(),
            symbol_path=symbol_path.relative_to(out_dir).as_posix(),
            load_check=loaded,
            manufacturer_label=selected.label,
            adaptation_applied=False,
            physical_package_pin_mapping="not_verified",
        )
        _json(report_path, report)
        card = out_dir / "MODEL_CARD.md"
        card.write_text(
            f"# {request.part}\n\nOfficial manufacturer model, saved byte-for-byte unchanged.\n\nSource: {bundle.source_url}\n\nManufacturer label: {selected.label}\n\nEntry point: `{entry.name}`. SHA-256: `{lib_hash}`.\n\nLTspice loaded and solved one unpowered operating point. This checks compatibility only.\n\n**UNKNOWN: electrical accuracy against the datasheet has not been measured on this route.**\n\nThe symbol follows the model's declared port order. It does not claim a physical package pin mapping. Average, transient, temperature and other scope limits remain as stated in the manufacturer's original documentation. Original files and licenses stay in the vendor-originals folder.\n",
            encoding="utf-8",
        )
        detail = "Official manufacturer model saved unchanged; LTspice load checked. Electrical accuracy remains UNKNOWN; review MODEL_CARD.md."
        log.emit("save", "ok", detail, {"files": len(bundle.files) + 3})
        result = MakeModelResult(
            "UNKNOWN",
            detail,
            request.part,
            out_dir,
            card,
            entry.file,
            symbol_path,
            (),
            {"PASS": 0, "FAIL": 0, "UNKNOWN": 1, "BLOCKED": 0, "NOT_APPLICABLE": 0},
            tuple(log.events),
            request,
        )
    _json(
        out_dir / "run-timing.json",
        {
            "schema_version": 1,
            "record_kind": "run_timing",
            "part": request.part,
            "route": "official_manufacturer_original",
            "status": result.status,
            "total_seconds": round(time.monotonic() - started, 3),
            "provider_calls": 0,
            "provider_calls_observed": 0,
            "provider_calls_complete": True,
            "simulation_seconds": None if loaded is None else loaded.get("wall_s"),
        },
    )
    (out_dir / "results.json").write_text(result.to_json(), encoding="utf-8")
    return result
