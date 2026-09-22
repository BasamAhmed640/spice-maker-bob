"""Run named datasheets through the real product path and write the numbers down.

```powershell
uv run python tools/verify_datasheet_suite.py                    # the default suite
uv run python tools/verify_datasheet_suite.py --suite suite.json
uv run python tools/verify_datasheet_suite.py --only ucc28251 --mode full
```

Why this exists: ``tools/measure_models.py`` measures the *scripted* offline
author against a committed fixture, and ``tools/benchmark_authoring.py`` measures
a synthetic author. Neither exercises the thing the product actually ships — a
real datasheet going through a real provider to a published ``.lib``. This tool
does that, and it exists to answer two graded questions with evidence rather than
assertion: how long a model takes to appear, and whether the result is honest.

Per datasheet it records:

* wall clock for the whole build, and the per-stage wall clock the CLI reports;
* the CLI's exit code, final status, and detail;
* every published artefact with its sha256 and size;
* the LTspice load verdict the sanity report recorded.

It then checks the repository's own honesty rules rather than trusting the run:

* the final status is one of the declared enum values;
* a ``PASS`` is only accepted when an observed simulator artefact is named, and
  a build with no published model is reported as a failure, never as a soft pass;
* a missing datasheet is a recorded ``skipped`` row, never a silent omission.

Nothing here fabricates a result. A build that fails is reported as a failure
with its own reason, and the exit code is non-zero when any datasheet failed, so
this can gate CI.

Writes ``build/datasheet-suite.json`` and ``build/datasheet-suite.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = Path.home() / "Downloads"

#: Statuses the domain declares. Anything else is a defect in the producer.
STATUSES = ("PASS", "FAIL", "UNKNOWN", "BLOCKED", "NOT_APPLICABLE")


def count_value(value: object) -> int | None:
    """A reported count as an ``int``, or ``None`` when it is not a usable number.

    The CLI emits integers here. This tool exists to report on that producer, so a
    payload whose shape changed must be recorded as a missing count rather than
    taken down the tool that was measuring it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except TypeError, ValueError, OverflowError:
        return None


@dataclass
class Case:
    """One datasheet to build a model from."""

    name: str
    part: str
    datasheet: Path
    provider: str
    mode: str = "sanity"
    note: str = ""


def default_suite() -> list[Case]:
    """The owner's datasheets, resolved against this machine's Downloads folder."""
    return [
        Case(
            name="lm358",
            part="LM358",
            datasheet=DOWNLOADS
            / "Industry-Standard Dual Operational Amplifiers datasheet (Rev. AB) - lm358.pdf",
            provider="opencode_go",
            note="dual op-amp; 68 pages; the fastest of the set",
        ),
        Case(
            name="sn74lvc1gx04",
            part="SN74LVC1GX04",
            datasheet=DOWNLOADS
            / "SN74LVC1GX04 Crystal Oscillator Driver datasheet (Rev. D) - sn74lvc1gx04.pdf",
            provider="opencode_go",
            note="crystal oscillator driver; genuinely simulatable",
        ),
        Case(
            name="ucc28251",
            part="UCC28251",
            datasheet=DOWNLOADS
            / "UCC28251 Advanced PWM Controller With Prebias Operation datasheet (Rev. E) - ucc28251.pdf",
            provider="opencode_go",
            note="PWM controller; the run that failed on the turn budget",
        ),
        Case(
            name="chargepump",
            part="CHARGE_PUMP",
            datasheet=DOWNLOADS / "datasheetchargepump.pdf",
            provider="opencode_go",
            note="charge pump; part number is not in the file name",
        ),
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cli_command(root: Path, args: list[str]) -> list[str]:
    """The console script is not always runnable, so drive the module entry directly.

    On a machine with Windows Smart App Control enabled the installed
    ``.venv\\Scripts\\boardmodeler.exe`` shim is refused with ``os error 4551``
    before it starts, which has nothing to do with the product. Going through the
    interpreter keeps this tool usable there.
    """
    del root
    return [sys.executable, "-c", "from boardmodeler.cli import main; main()", *args]


@dataclass
class Run:
    """What one datasheet actually did."""

    case: Case
    status: str = "NOT_RUN"
    detail: str = ""
    exit_code: int | None = None
    wall_s: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    ltspice_load: str | None = None
    datasheet_sha256: str | None = None
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.case.name,
            "part": self.case.part,
            "mode": self.case.mode,
            "provider": self.case.provider,
            "note": self.case.note,
            "datasheet": str(self.case.datasheet),
            "datasheet_sha256": self.datasheet_sha256,
            "status": self.status,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "wall_s": None if self.wall_s is None else round(self.wall_s, 3),
            "counts": self.counts,
            "ltspice_load": self.ltspice_load,
            "artifacts": self.artifacts,
            "stages": self.stages,
            "honesty_problems": self.failures,
        }


def _stage_times(stages: list[dict[str, Any]]) -> dict[str, float]:
    """Per-stage wall clock is not stamped, so derive ordering facts instead.

    The CLI reports stage transitions but not their durations, so this returns the
    count of transitions per stage. Wall clock is measured here, around the whole
    process, and the progress log keeps the detail for a human.
    """
    counts: dict[str, float] = {}
    for stage in stages:
        name = str(stage.get("stage", "?"))
        counts[name] = counts.get(name, 0.0) + 1.0
    return counts


def run_case(case: Case, root: Path, build_root: Path, *, timeout_s: float) -> Run:
    run = Run(case)
    if not case.datasheet.is_file():
        run.status = "SKIPPED"
        run.detail = f"datasheet not present: {case.datasheet}"
        return run
    run.datasheet_sha256 = sha256(case.datasheet)

    out_dir = build_root / case.name
    args = [
        "model",
        "build",
        "--part",
        case.part,
        "--datasheet",
        str(case.datasheet),
        "--out",
        str(out_dir),
        "--allow-remote",
        "--provider",
        case.provider,
        "--json",
    ]
    if case.mode == "sanity":
        args.append("--sanity")

    log_dir = build_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{case.name}.log"

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cli_command(root, args),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as exc:
        run.wall_s = time.perf_counter() - started
        run.exit_code = None
        run.status = "TIMEOUT"
        run.detail = f"the build did not finish within {timeout_s:g} s"
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        log_path.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8", newline="\n")
        run.failures.append(f"wall clock exceeded {timeout_s:g} s")
        return run
    run.wall_s = time.perf_counter() - started
    run.exit_code = code
    log_path.write_text(stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8", newline="\n")

    payload: dict[str, Any] | None = None
    start = stdout.find("{")
    if start >= 0:
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            payload = None

    if payload is None:
        run.status = "NO_PAYLOAD"
        run.detail = (
            (stderr.strip().splitlines() or ["no payload emitted"])[-1][:400]
            if stderr.strip()
            else "the CLI emitted no JSON payload"
        )
        run.failures.append("no machine-readable outcome; treat every number here as unknown")
        return run

    run.status = str(payload.get("status", "UNKNOWN"))
    run.detail = str(payload.get("detail", ""))[:600]
    counts = payload.get("counts") or {}
    run.counts = {
        str(key): total
        for key, value in counts.items()
        if (total := count_value(value)) is not None
    }
    run.stages = list(payload.get("stages") or [])

    for label, key in (("lib", "lib_path"), ("asy", "asy_path"), ("card", "card_path")):
        value = payload.get(key)
        if not value:
            continue
        path = Path(str(value))
        if path.is_file():
            run.artifacts[label] = {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        else:
            run.artifacts[label] = {"path": str(path), "missing": True}

    report_path = out_dir / "sanity-report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            load = report.get("load") or {}
            run.ltspice_load = str(load.get("status") or report.get("ltspice_load") or "") or None
        except OSError, json.JSONDecodeError:
            run.failures.append("sanity-report.json could not be read")

    # --- the repository's own honesty rules, checked rather than assumed -------
    if run.status not in STATUSES:
        run.failures.append(f"status {run.status!r} is not one of {STATUSES}")
    if run.status == "PASS" and "lib" not in run.artifacts:
        run.failures.append("reported PASS with no published .lib")
    if counts.get("PASS", 0) and not payload.get("lib_path"):
        run.failures.append("reported PASS rows with no published model")
    if run.case.mode == "sanity" and run.status == "PASS":
        run.failures.append(
            "a --sanity build must not report PASS: electrical accuracy is unverified in that mode"
        )
    if "lib" not in run.artifacts and run.status in {"PASS", "UNKNOWN"}:
        # UNKNOWN with no model is legitimate, but it must be visible as "no model",
        # because the graded question is whether a model appeared at all.
        run.failures.append("no .lib was published, so no model was produced")
    if code != 0 and run.status in {"PASS", "UNKNOWN"}:
        run.failures.append(f"the CLI exited {code} while reporting {run.status}")
    return run


def summarise(runs: list[Run]) -> dict[str, Any]:
    produced = [r for r in runs if "lib" in r.artifacts]
    times = [r.wall_s for r in runs if r.wall_s is not None and r.status != "SKIPPED"]
    return {
        "datasheets": len(runs),
        "models_produced": len(produced),
        "failed": len([r for r in runs if r.status in {"NO_PAYLOAD", "TIMEOUT"}]),
        "honesty_problems": sum(len(r.failures) for r in runs),
        "wall_s": {
            "min": round(min(times), 3) if times else None,
            "max": round(max(times), 3) if times else None,
            "total": round(sum(times), 3) if times else None,
        },
        "per_datasheet": {r.case.name: r.wall_s for r in runs if r.wall_s is not None},
    }


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Datasheet suite",
        "",
        "Real datasheets through the real product path (`model build`), timed here.",
        "A row with no `.lib` produced no model, whatever its status says.",
        "",
        "| datasheet | part | mode | status | model? | wall s | PASS | UNKNOWN | load | honesty |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in payload["runs"]:
        model = "yes" if "lib" in row["artifacts"] else "**no**"
        wall = "" if row["wall_s"] is None else f"{row['wall_s']:.1f}"
        problems = len(row["honesty_problems"])
        lines.append(
            f"| {row['name']} | {row['part']} | {row['mode']} | {row['status']} | {model} | "
            f"{wall} | {row['counts'].get('PASS', 0)} | {row['counts'].get('UNKNOWN', 0)} | "
            f"{row['ltspice_load'] or ''} | {problems or ''} |"
        )
    lines += ["", "## Summary", "", "```json", json.dumps(payload["summary"], indent=2), "```", ""]
    problems = [
        (row["name"], problem) for row in payload["runs"] for problem in row["honesty_problems"]
    ]
    lines += ["## Honesty checks", ""]
    if problems:
        lines += [f"* `{name}`: {problem}" for name, problem in problems]
    else:
        lines.append("* none: every published status was consistent with its artefacts")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=None, help="JSON list of cases")
    parser.add_argument("--only", action="append", default=[], help="run just these names")
    parser.add_argument("--mode", default=None, choices=["sanity", "full"])
    parser.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds per datasheet")
    args = parser.parse_args(argv)

    cases = default_suite()
    if args.suite:
        try:
            raw = json.loads(args.suite.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--suite {args.suite} could not be read: {exc}") from exc
        if not isinstance(raw, list):
            raise SystemExit(f"--suite {args.suite} must hold a JSON list of cases")
        try:
            cases = [
                Case(
                    name=item["name"],
                    part=item["part"],
                    datasheet=Path(item["datasheet"]),
                    provider=item.get("provider", "deepseek"),
                    mode=item.get("mode", "sanity"),
                    note=item.get("note", ""),
                )
                for item in raw
            ]
        except (KeyError, AttributeError, TypeError) as exc:
            raise SystemExit(f"--suite {args.suite} entry is missing {exc}") from exc
    if args.only:
        wanted = set(args.only)
        cases = [c for c in cases if c.name in wanted]
    if args.mode:
        cases = [Case(**{**c.__dict__, "mode": args.mode}) for c in cases]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    build_root = args.build_dir / "datasheet-suite"
    build_root.mkdir(parents=True, exist_ok=True)

    runs: list[Run] = []
    for case in cases:
        print(f"==> {case.name} ({case.part}, {case.mode}, {case.provider})", flush=True)
        run = run_case(case, REPO_ROOT, build_root, timeout_s=args.timeout)
        runs.append(run)
        model = "model published" if "lib" in run.artifacts else "NO MODEL"
        wall = "n/a" if run.wall_s is None else f"{run.wall_s:.1f}s"
        print(f"    {run.status} in {wall} — {model}", flush=True)
        if run.failures:
            for problem in run.failures:
                print(f"    !! {problem}", flush=True)

    payload = {
        "tool": "verify_datasheet_suite",
        "summary": summarise(runs),
        "runs": [r.as_dict() for r in runs],
    }
    out_json = args.build_dir / "datasheet-suite.json"
    out_md = args.build_dir / "datasheet-suite.md"
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
    out_md.write_text(markdown(payload), encoding="utf-8", newline="\n")
    print(f"\nwrote {out_json}\nwrote {out_md}")
    print(json.dumps(payload["summary"], indent=2))

    # A build that produced no model is a failure, and a dishonest status is worse.
    bad = [r for r in runs if "lib" not in r.artifacts or r.failures]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
