"""Deliverables for an authored model: the card, the symbol, and the install step.

The card is the only place a claim about the model is allowed to live, and every row on
it is produced from an observed harness outcome. A characteristic with no probe is
listed as a coverage gap with its reason rather than being quietly dropped, so a
reader can see exactly which datasheet rows were tested, which were judged, and which
were never reachable by simulation.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from boardmodeler.authoring.harness import judge_characteristic
from boardmodeler.domain.enums import Status
from boardmodeler.models.library import ModelStoreError, subckt_ports
from boardmodeler.models.symbolism import symbol_text, validate_symbol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from boardmodeler.authoring.harness import HarnessReport
    from boardmodeler.authoring.spec import Characteristic, SpecSet

__all__ = [
    "DELIVERABLE_FILES",
    "InstallPlan",
    "plan_install",
    "render_card",
    "write_deliverables",
    "write_symbol_for",
]

DELIVERABLE_FILES = ("MODEL_CARD.md", "example.cir", "install.md")

_STATUS_ORDER = {"FAIL": 0, "UNKNOWN": 1, "PASS": 2, "NOT_APPLICABLE": 3}


def status_tally(statuses: Iterable[str]) -> dict[str, int]:
    """Per-row status counts in the same five-key shape as a probe tally."""
    tally = {status.value: 0 for status in Status}
    for status in statuses:
        tally[status] = tally.get(status, 0) + 1
    return tally


def _status_by_characteristic(
    spec: SpecSet, report: HarnessReport
) -> dict[str, tuple[str, str, dict[str, float | str]]]:
    """``char_id -> (status, detail, measured)``, re-judged per characteristic."""
    outcomes = {char_id: outcome for outcome in report.outcomes for char_id in outcome.char_ids}
    judged: dict[str, tuple[str, str, dict[str, float | str]]] = {}
    for characteristic in spec.characteristics:
        outcome = outcomes.get(characteristic.char_id)
        if outcome is None:
            continue
        status, detail = judge_characteristic(characteristic, outcome)
        judged[characteristic.char_id] = (status, detail, dict(outcome.measured))
    return judged


def _format_limits(characteristic: Characteristic) -> str:
    parts: list[str] = []
    if characteristic.min_value is not None:
        parts.append(f"min {characteristic.min_value:g}")
    if characteristic.typ_value is not None:
        parts.append(f"typ {characteristic.typ_value:g}")
    if characteristic.max_value is not None:
        parts.append(f"max {characteristic.max_value:g}")
    if characteristic.target is not None:
        parts.append(f"target {characteristic.target:g}")
    return " / ".join(parts) if parts else "not quantified"


def _format_measured(measured: Mapping[str, float | str]) -> str:
    shown: list[str] = []
    for key, value in measured.items():
        shown.append(f"{key}={value:g}" if isinstance(value, float) else f"{key}={value}")
    return ", ".join(shown) if shown else "-"


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_card(
    *,
    part: str,
    subckt: str,
    spec: SpecSet,
    report: HarnessReport,
    model_file: str,
    document: str | None = None,
    backend: str | None = None,
    iterations: int | None = None,
    reinforcement: object | None = None,
) -> str:
    """The model card, findings first, every number taken from an observed outcome."""
    judged = _status_by_characteristic(spec, report)
    covered = spec.covered()
    uncovered = spec.uncovered()

    rows: list[tuple[int, str, str]] = []
    row_statuses: list[str] = []
    for characteristic in covered:
        status, detail, measured = judged.get(
            characteristic.char_id, ("UNKNOWN", "no probe reported for this characteristic", {})
        )
        row_statuses.append(status)
        rows.append(
            (
                _STATUS_ORDER.get(status, 1),
                status,
                "| `{}` | {} | {} | {} | {} | {} | {} |".format(
                    characteristic.char_id,
                    _escape_cell(characteristic.statement),
                    _escape_cell(_format_limits(characteristic)),
                    _escape_cell(_format_measured(measured)),
                    status,
                    characteristic.source_page if characteristic.source_page is not None else "-",
                    _escape_cell(detail),
                ),
            )
        )
    rows.sort(key=lambda row: (row[0], row[1]))
    counts = status_tally(row_statuses)

    lines: list[str] = [
        f"# {part} — LTspice model card",
        "",
        f"Model: `{model_file}` · subcircuit `{subckt}` · sha256 `{report.model_sha256}`",
        f"Spec `{spec.digest()[:16]}` from {document or spec.doc_id}"
        + (f" · built via {backend} in {iterations} agent turn(s)" if backend else ""),
        "",
        "This model comes from the recorded authoring path and is judged by real LTspice "
        "runs against "
        "the datasheet rows listed below. A row is only `PASS` when a completed simulation "
        "produced the measured value shown; rows that could not be judged are `UNKNOWN` with "
        "the reason. Datasheet rows no probe can reach are listed as coverage gaps with the "
        "reason. Nothing else about this part is claimed.",
        "PASS applies only at the operating points recorded in harness-report.json. "
        "A nominal sample inside a datasheet range does not validate the whole range. "
        "No temperature, process distribution, protocol, or high-speed channel qualification "
        "is inferred from these behavioral probes.",
        "",
        f"**Totals:** {counts.get('PASS', 0)} pass · {counts.get('FAIL', 0)} fail · "
        f"{counts.get('UNKNOWN', 0)} unknown · {len(uncovered)} rows not testable by "
        f"simulation out of {len(spec.characteristics)} datasheet rows. The pass/fail/unknown totals here cover executed probes; the application also counts untested numeric requirements as UNKNOWN.",
        "",
        "## Judged characteristics",
        "",
        "| Requirement | Datasheet statement | Required | Measured | Status | Page | Detail |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(row[2] for row in rows)
    if not rows:
        lines.append("| — | no characteristic was bound to a probe | — | — | — | — | — |")

    if uncovered:
        lines += [
            "",
            "## Datasheet rows with no simulation probe",
            "",
            "These are declared gaps, not passes. Each needs bench measurement or a "
            "different tool to establish.",
            "",
            "| Requirement | Datasheet statement | Why no probe | Page |",
            "| --- | --- | --- | --- |",
        ]
        for characteristic in uncovered:
            lines.append(
                "| `{}` | {} | {} | {} |".format(
                    characteristic.char_id,
                    _escape_cell(characteristic.statement),
                    _escape_cell(characteristic.not_testable_reason or "no reason recorded"),
                    characteristic.source_page if characteristic.source_page is not None else "-",
                )
            )

    lines += _supporting_material_section(reinforcement)

    lines += [
        "",
        "## Scope",
        "",
        "- Behavioural model: it reproduces the judged rows above at the stated conditions "
        "and is not a transistor-level replica of the silicon.",
        "- Anything not on this card (thermal behaviour, internal oscillator artifacts, "
        "EMI, fault timing corners, absolute-maximum survival) is outside the model's "
        "claimed scope.",
        "- Exported files reference no vendor data unless the card says otherwise.",
        "",
        "## Reproduce",
        "",
        "```",
        "uv run boardmodeler model test --out <this directory>",
        "```",
        "",
        "The harness reruns every probe in a fresh LTspice batch and rewrites this card.",
        "",
    ]
    return "\n".join(lines)


def _supporting_material_section(reinforcement: object | None) -> list[str]:
    """What the web search turned up, kept strictly apart from the verdicts.

    Only text this tool actually retrieved is listed as fact; anything the agent merely
    claimed is listed as unverified with the reason we could not confirm it. Nothing here
    can change a status — that is what the judged table above is for.
    """
    if reinforcement is None:
        return []
    status = getattr(reinforcement, "status", "skipped")
    sources = tuple(getattr(reinforcement, "sources", ()) or ())
    caveats = tuple(getattr(reinforcement, "caveats", ()) or ())
    suggested = tuple(getattr(reinforcement, "suggested_probes", ()) or ())
    detail = getattr(reinforcement, "detail", "")

    lines = ["", "## Supporting material (searched, not evidence for the verdicts)", ""]
    if status != "ok":
        lines.append(
            f"The search did not produce usable sources this run (`{status}`: {detail or 'no detail'})."
        )
        lines.append("")
        lines.append(
            "This affects nothing above: the rows were judged by simulation against the "
            "datasheet as usual."
        )
        return lines

    retrieved = [source for source in sources if getattr(source, "retrieved", False)]
    unverified = [source for source in sources if not getattr(source, "retrieved", False)]
    if retrieved:
        lines += [
            "Sources retrieved at build time (text stored verbatim with its hash):",
            "",
        ]
        for source in retrieved:
            digest = str(getattr(source, "sha256", "") or "")[:12]
            lines.append(
                f"- <{getattr(source, 'url', '')}> — {_escape_cell(str(getattr(source, 'claim', '')))}"
                + (f" (sha256 {digest})" if digest else "")
            )
        lines.append("")
    if unverified:
        lines += ["Claimed but not retrievable at build time (treated as unverified):", ""]
        for source in unverified:
            reason = str(getattr(source, "reason", "") or "not retrieved")
            lines.append(
                f"- <{getattr(source, 'url', '')}> — {_escape_cell(str(getattr(source, 'claim', '')))} "
                f"({_escape_cell(reason)})"
            )
        lines.append("")
    if caveats:
        lines += ["Caveats read out of the retrieved text:", ""]
        lines += [f"- {_escape_cell(str(caveat))}" for caveat in caveats]
        lines.append("")
    if suggested:
        lines += [
            "Probes the retrieved text suggests adding (not run for this model): "
            + ", ".join(f"`{name}`" for name in suggested),
            "",
        ]
    return lines


def write_symbol_for(
    *,
    out_path: Path,
    name: str,
    ports: Sequence[str],
    model_file: str,
    model_name: str,
    description: str | None = None,
    directions: Mapping[str, str] | None = None,
) -> Path:
    """Write a symbol whose ``SpiceOrder`` bijection is validated before it is kept."""
    text = symbol_text(
        name,
        list(ports),
        model_file=model_file,
        model_name=model_name,
        description=description,
        directions=directions,
    )
    findings = validate_symbol(text, ports=list(ports), model_file=model_file)
    if findings:
        detail = "; ".join(f"{finding.code}: {finding.message}" for finding in findings)
        raise ValueError(f"generated symbol is inconsistent with the subcircuit: {detail}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8", newline="\n")
    return out_path


@dataclass(frozen=True)
class InstallPlan:
    """Where the model's files go, and the exact steps the user takes in LTspice."""

    part: str
    lib_source: Path
    asy_source: Path
    lib_target: Path | None
    asy_target: Path | None
    copied: tuple[Path, ...]
    steps: tuple[str, ...]
    detail: str


def _ltspice_user_lib(home: Path | None = None) -> Path:
    """The per-user LTspice library directory (never the installation directory)."""
    from boardmodeler.storage import library_dir, portable

    if portable():
        return library_dir()
    base = home if home is not None else Path.home()
    return base / "AppData" / "Local" / "LTspice" / "lib"


def plan_install(
    *,
    part: str,
    subckt: str,
    lib: Path,
    asy: Path,
    into: Path | None = None,
    user_lib: bool = False,
    home: Path | None = None,
    apply: bool = False,
) -> InstallPlan:
    """Copy the model where the user asked, and describe the wiring in LTspice.

    Writing into the LTspice *installation* is never offered. ``user_lib`` targets the
    per-user library under ``%LOCALAPPDATA%\\LTspice\\lib``, which is the documented place
    for user models and is fully reversible by deleting the two files.
    """
    root = _ltspice_user_lib(home) if user_lib else into
    if root is None:
        return InstallPlan(
            part=part,
            lib_source=lib,
            asy_source=asy,
            lib_target=None,
            asy_target=None,
            copied=(),
            steps=(
                f"1. Copy {lib.name} anywhere LTspice can read it, or pass it with -I<dir>.",
                f"2. Copy {asy.name} into your symbol directory, or add its folder as a "
                f"symbol search path (LTspice: Tools > Settings > Sym. & Lib. Search Paths).",
                f"3. Place the {subckt} symbol on a schematic and simulate; the symbol "
                f"already points at {lib.name}.",
                "4. Re-run with --user-lib to have this command do steps 1-2 in your "
                "per-user LTspice library instead.",
            ),
            detail="no destination given; nothing was copied",
        )

    if user_lib:
        lib_target = root / "sub" / lib.name
        asy_target = root / "sym" / asy.name
    else:
        lib_target = root / lib.name
        asy_target = root / asy.name

    copied: list[Path] = []
    if apply:
        missing = [path for path in (lib, asy) if not path.is_file()]
        if missing:
            raise ValueError(
                "nothing was copied; missing source file(s): "
                + ", ".join(str(path) for path in missing)
            )
        for source, target in ((lib, lib_target), (asy, asy_target)):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target)

    listed = (lib_target, asy_target) if user_lib else (root / lib.name, root / asy.name)
    steps = (
        f"{lib.name} -> {listed[0]}",
        f"{asy.name} -> {listed[1]}",
        f"Open LTspice, place the {subckt} symbol, and simulate. The symbol's SpiceModel "
        f"attribute already names {lib.name}.",
        "Undo at any time by deleting those two files.",
    )
    return InstallPlan(
        part=part,
        lib_source=lib,
        asy_source=asy,
        lib_target=listed[0],
        asy_target=listed[1],
        copied=tuple(copied),
        steps=steps,
        detail=(
            f"copied {len(copied)} file(s) into {root}"
            if apply
            else f"would copy into {root} (pass --apply to do it)"
        ),
    )


def write_deliverables(
    *,
    out_dir: Path,
    part: str,
    subckt: str,
    spec: SpecSet,
    report: HarnessReport,
    document: str | None = None,
    backend: str | None = None,
    iterations: int | None = None,
    reinforcement: object | None = None,
) -> tuple[Path, ...]:
    """Write the card and a runnable example to ``out_dir``; return what was written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    model_file = f"{subckt}.lib"
    written: list[Path] = []

    card = out_dir / "MODEL_CARD.md"
    card.write_text(
        render_card(
            part=part,
            subckt=subckt,
            spec=spec,
            report=report,
            model_file=model_file,
            document=document,
            backend=backend,
            iterations=iterations,
            reinforcement=reinforcement,
        ),
        encoding="utf-8",
        newline="\n",
    )
    written.append(card)

    example = out_dir / "example.cir"
    library = out_dir / model_file
    # The physical examples use the model's declared port order, not a package's
    # assumed pin order. A BLOCKED or model-less build still writes its card.
    if library.is_file() and any((row.probe or "").startswith("opamp_") for row in spec.covered()):
        from boardmodeler.authoring.probes import PROBES

        text = PROBES["opamp_slew_rise"].render(model_lib=library, subckt=subckt, params={})
        text = text.replace(library.resolve().as_posix(), model_file)
    else:
        ports: tuple[str, ...] = ()
        if library.is_file():
            with suppress(OSError, UnicodeError, ModelStoreError):
                ports = subckt_ports(library.read_text(encoding="utf-8"), subckt)
        text = (
            _physical_buck_example_deck(subckt=subckt, model_file=model_file, ports=ports)
            if _is_physical_buck(ports)
            else _example_deck(subckt=subckt, model_file=model_file)
        )
    example.write_text(text, encoding="utf-8", newline="\n")
    written.append(example)

    for name, text in (("install.md", _install_text(part=part, subckt=subckt, lib=model_file)),):
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return tuple(written)


def _example_deck(*, subckt: str, model_file: str) -> str:
    """A minimal deck the user can open in LTspice to see the part regulate."""
    return "\n".join(
        [
            f"* {subckt}: example operating point and a load step",
            "* The model file sits beside this deck; the symbol points at the same name.",
            f".include {model_file}",
            "",
            "VIN   vin  0  12",
            "VEN   en   0  12",
            "REN   en   0  100k",
            "RFB1  vout fb 100k",
            "RFB2  fb   0  32.4k",
            "CPG   pg   0  10p",
            "RPG   pgv  0  0",
            "RPG2  pg   pgv 100k",
            "VPG   pgv  0  3.3",
            "L1    sw   vout 4.7u",
            "COUT  vout 0  47u",
            f"XU1   vin en fb pg vout 0 sw 0 {subckt}",
            "ILOAD vout 0 PWL(0 0.1 1m 0.1 1.01m 1.5 5m 1.5)",
            "",
            ".tran 10u 5m",
            ".options maxstep=1u",
            ".probe V(vout) I(L1) V(sw)",
            ".end",
            "",
        ]
    )


_BUCK_PORTS = frozenset({"BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH"})
_BUCK_NODES = {
    "BOOT": "boot",
    "VIN": "vin",
    "EN": "en",
    "SS": "ss",
    "VSENSE": "vsense",
    "COMP": "comp",
    "GND": "0",
    "PH": "ph",
    "POWERPAD": "0",
}


def _is_physical_buck(ports: tuple[str, ...]) -> bool:
    named = {port.upper() for port in ports}
    return len(named) == len(ports) and _BUCK_PORTS <= named <= _BUCK_PORTS | {"POWERPAD"}


def _physical_buck_example_deck(*, subckt: str, model_file: str, ports: tuple[str, ...]) -> str:
    """One illustrative closed-loop power stage, wired in the model's own order."""
    nodes = " ".join(_BUCK_NODES[port.upper()] for port in ports)
    return "\n".join(
        [
            f"* {subckt}: physical buck example with a 12 V input and a 3.3 V target",
            "* Example component values are illustrative; see MODEL_CARD.md for tested limits.",
            f'.include "{model_file}"',
            "VIN vin 0 12",
            "CIN vin 0 10u",
            "VEN en 0 12",
            "CSS ss 0 10n",
            "CBOOT boot ph 100n",
            "RFB1 out vsense 15k",
            "RFB2 vsense 0 4.75k",
            "RCOMP comp comp_mid 75k",
            "CCOMP comp_mid 0 180p",
            "CHF comp 0 10p",
            "Dcatch 0 ph DCATCH",
            ".model DCATCH D(Is=1e-8 N=1.1 Rs=0.04 Cjo=300p Bv=40 Ibv=1m)",
            "LOUT ph out 4.7u Rser=10m",
            "COUT out 0 94u Rser=3m",
            "RLOAD out 0 10",
            f"XU1 {nodes} {subckt}",
            ".tran 0 12m 0 200n",
            ".meas tran vout_avg AVG V(out) FROM=10m TO=12m",
            ".save V(out) V(ph) I(LOUT)",
            ".end",
            "",
        ]
    )


def _install_text(*, part: str, subckt: str, lib: str) -> str:
    return "\n".join(
        [
            f"# Using the {part} model in LTspice",
            "",
            f"Files in this directory: `{lib}` (the subcircuit), `{subckt}.asy` (the symbol), "
            "`example.cir` (a runnable schematic netlist), `MODEL_CARD.md` (what was tested).",
            "",
            "## Quickest path",
            "",
            "1. Open `example.cir` in LTspice and press Run.",
            f"2. To use the part elsewhere, copy `{lib}` and `{subckt}.asy` into your "
            "per-user library:",
            "",
            "```",
            "uv run boardmodeler model install --out . --user-lib --apply",
            "```",
            "",
            "That writes the model into `%LOCALAPPDATA%\\LTspice\\lib\\sub` and the symbol "
            "into `%LOCALAPPDATA%\\LTspice\\lib\\sym`. Delete both files to undo. This "
            "command never writes into the LTspice installation directory.",
            "",
            "## Manual path",
            "",
            f"- Symbol search path: LTspice -> Tools -> Settings -> Sym. & Lib. Search Paths; "
            f"add the folder holding `{subckt}.asy`.",
            f"- Or keep both files next to your schematic and `.include {lib}`.",
            "",
        ]
    )
