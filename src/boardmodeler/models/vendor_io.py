"""Preserve supplied high-speed models without inventing electrical validation.

This is an acquisition manifest, not an IBIS/AMI or channel simulator. No supplied
binary is loaded. Original bytes, attribution, and missing validation travel together.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from boardmodeler.domain.hashing import sha256_bytes


def import_io_source(
    source: Path, out: Path, *, part: str, source_url: str, license_note: str
) -> dict:
    source = Path(source)
    if not part.strip() or not license_note.strip():
        raise ValueError("part and license_note are required; record unknown licensing explicitly")
    url = urlparse(source_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("source_url must be an HTTPS attribution URL without credentials")
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("source exceeds the 64 MiB import limit")
    data = source.read_bytes()
    text = data.decode("utf-8-sig", errors="replace")
    suffix = source.suffix.lower()
    metadata = {}
    if suffix == ".ibs":
        version = re.search(r"(?im)^\s*\[IBIS Ver\]\s*(\S+)", text)
        models = re.findall(r"(?im)^\s*\[Model\]\s+(\S+)", text)
        if not version or not models or not re.search(r"(?im)^\s*\[End\]", text):
            raise ValueError("IBIS header, model declarations or end marker missing")
        kind = "ibis"
        metadata = {"declared_version": version[1], "model_names": models}
        validation = "Run an IBIS standards checker and buffer/package/board tests in an IBIS-capable simulator."
    elif re.fullmatch(r"\.s[1-9]\d*p", suffix):
        option = next(
            (
                line.split("!", 1)[0].strip()
                for line in text.splitlines()
                if line.lstrip().startswith("#")
            ),
            "",
        )
        if not option:
            raise ValueError("Touchstone option line is missing")
        kind = "touchstone"
        metadata = {"declared_ports": int(suffix[2:-1]), "option_line": option}
        validation = "Check port mapping, frequency coverage, passivity and causality; validate the complete channel in a channel simulator."
    elif suffix == ".ami":
        if not text.strip().startswith("(") or "Reserved_Parameters" not in text:
            raise ValueError("AMI parameter tree or Reserved_Parameters missing")
        kind = "ibis_ami_parameters"
        validation = "Obtain the matching vendor binary and IBIS model, verify platform compatibility, then run AMI statistical/time-domain tests externally."
    else:
        raise ValueError(
            "supported source types: .ibs, .ami and .sNp; executable binaries are not imported"
        )
    digest = sha256_bytes(data)
    directory = Path(out) / "vendor-io" / digest
    directory.mkdir(parents=True, exist_ok=True)
    stored = directory / ("original" + suffix)
    if stored.exists() and stored.read_bytes() != data:
        raise ValueError("stored original was changed; refusing to overwrite evidence")
    stored.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "part": part,
        "kind": kind,
        "original_filename": source.name,
        "path": stored.name,
        "sha256": digest,
        "size": len(data),
        "source_url": source_url,
        "source_url_verified": False,
        "license_note": license_note,
        "metadata": metadata,
        "format_validation": "NOT_RUN",
        "electrical_validation": "UNKNOWN",
        "next_step": validation,
        "scope": "Source preserved only; no protocol, eye-diagram, BER or device-accuracy claim.",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {**manifest, "manifest": str(directory / "manifest.json")}
