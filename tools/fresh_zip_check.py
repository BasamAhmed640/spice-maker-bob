"""Fresh-ZIP check: a downloaded copy installs, starts without searching, and simulates.

Network::

    python tools/fresh_zip_check.py --repo owner/name --ref main --work C:\\fz ^
        --ltspice C:\\...\\LTspice.exe --out C:\\fz\\verdict.json

Offline (any local archive, e.g. ``git archive --format=zip HEAD -o head.zip``)::

    python tools/fresh_zip_check.py --zip head.zip --work C:\\fz --ltspice ... --out ...

Steps, each recorded as PASS/FAIL/SKIP with its wall time in the verdict JSON:

1. fetch ``https://github.com/<repo>/archive/<ref>.zip`` (or take ``--zip``) and hash it;
2. extract it into a new folder under ``--work``, refusing any member that would land
   outside that folder;
3. ``py -3.14 -m venv .venv`` inside the extracted copy;
4. ``pip install -r requirements.txt`` (the pinned list);
5. ``doctor --json`` under the startup probe below: an audit hook plus stat-level
   wrappers record every file, directory, glob, registry or process access that names
   LTspice or starts under a known LTspice install root. It must record nothing, and
   doctor must report LTspice as not configured;
6. save the LTspice path exactly as SETUP's Save does (``config.ltspice.path`` then
   ``save_config``) and run a trivial ``.op`` deck through ``simulation.ltspice.run_batch``;
7. ``tools/scan_credentials.py`` over the extracted tree.

Child processes of steps 3-6 get the parent environment minus anything that looks like a
credential, so a developer's own keys never reach the fresh copy. The scanner (step 7)
is the one exception: it needs those values in memory to look for them, and it prints
only names.

``tests/security/test_startup_no_ltspice_search.py`` runs this file's ``--internal``
modes against the working tree, so the probe the tool trusts is the probe the tests run.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import ntpath
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

RESULT_MARKER = "FRESH_ZIP_CHECK_RESULT "

#: Roots an LTspice installer uses on Windows. ``%VAR%`` entries follow the machine.
INSTALL_ROOT_TEMPLATES: tuple[str, ...] = (
    r"C:\Program Files\ADI",
    r"C:\Program Files\LTC",
    r"C:\Program Files (x86)\ADI",
    r"C:\Program Files (x86)\LTC",
    r"%ProgramFiles%\ADI",
    r"%ProgramFiles%\LTC",
    r"%ProgramFiles(x86)%\ADI",
    r"%ProgramFiles(x86)%\LTC",
    r"%LOCALAPPDATA%\Programs\ADI",
)

#: Registry sub-keys that would mean "look the simulator up in the registry".
_REGISTRY_MARKERS = ("ltspice", "analog devices", "linear technology")

OP_DECK = "* fresh-zip-check divider\nV1 in 0 5\nR1 in out 1k\nR2 out 0 1k\n.op\n.end\n"
OP_EXPECTED_V = 2.5
OP_TOLERANCE_V = 1e-3

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
_DROPPED_NAMES = frozenset(
    {"SPICE_MAKER_ROOT", "BOARDMODELER_CONFIG", "LTSPICE_EXE", "PYTHONPATH", "PYTHONHOME"}
    | {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
)
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


# --------------------------------------------------------------------------- #
# the startup probe (runs inside the interpreter under test)


def _normalize(text: str) -> str:
    value = text.replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normpath(value).casefold()


def install_roots(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """The known install roots, expanded for this machine and normalized for matching."""
    env = dict(os.environ if environ is None else environ)
    roots: set[str] = set()
    for template in INSTALL_ROOT_TEMPLATES:
        expanded = re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1), m.group(0)), template)
        if "%" not in expanded:
            roots.add(_normalize(expanded))
    return tuple(sorted(roots))


class _Recorder:
    """Collects LTspice-looking accesses; never raises into the code under test."""

    def __init__(self, own_dirs: tuple[str, ...]) -> None:
        self.roots = install_roots()
        self.own_dirs = tuple(_normalize(d) for d in own_dirs)
        self.events: list[dict[str, str]] = []

    def why(self, text: str, *, is_path: bool) -> str | None:
        norm = _normalize(text)
        if is_path and not ntpath.isabs(norm):
            norm = _normalize(ntpath.join(os.getcwd(), text))
        bounded = norm + "\\"
        for root in self.roots:
            if root + "\\" in bounded:
                return "install_root"
        if "ltspice" in norm and not any(bounded.startswith(own + "\\") for own in self.own_dirs):
            return "name"
        return None

    def check(self, event: str, values: list[Any], *, is_path: bool = True) -> None:
        for value in values:
            text = _text(value)
            if not text:
                continue
            reason = self.why(text, is_path=is_path)
            if reason and len(self.events) < 200:
                self.events.append({"event": event, "path": text, "why": reason})

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        # An audit hook must never break the program it watches.
        with contextlib.suppress(Exception):
            self._audit(event, args)

    def _audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event in {"open", "os.listdir", "os.scandir", "os.stat", "os.chdir", "os.startfile"}:
            self.check(event, [args[0]] if args else [])
        elif event == "os.startfile/2":
            self.check(event, [args[0], args[3]])
            self.check(event, [args[2]], is_path=False)
        elif event in {"glob.glob", "glob.glob/2"}:
            root_dir = args[2] if event == "glob.glob/2" else None
            pattern = _text(args[0]) or ""
            self.check(event, [pattern, root_dir, ntpath.join(_text(root_dir) or "", pattern)])
        elif event in {"pathlib.Path.glob", "pathlib.Path.rglob"}:
            base = _text(args[0]) or ""
            self.check(event, [base, ntpath.join(base, _text(args[1]) or "")])
        elif event == "subprocess.Popen":
            executable, argv, cwd = args[0], args[1], args[2]
            self.check(event, [executable, cwd])
            items = [argv] if isinstance(argv, (str, bytes)) else list(argv or [])
            self.check(event, items, is_path=False)
        elif event == "_winapi.CreateProcess":
            self.check(event, [args[0], args[2]])
            self.check(event, [args[1]], is_path=False)
        elif event == "os.system":
            self.check(event, [args[0]], is_path=False)
        elif event in {"os.spawn", "os.exec", "os.posix_spawn"}:
            path, argv = (args[1], args[2]) if event == "os.spawn" else (args[0], args[1])
            self.check(event, [path])
            self.check(event, list(argv or []), is_path=False)
        elif event.startswith("winreg.") and len(args) >= 2:
            sub_key = (_text(args[1]) or "").casefold()
            if any(marker in sub_key for marker in _REGISTRY_MARKERS):
                self.events.append({"event": event, "path": str(args[1]), "why": "registry"})

    def wrap_stat_probes(self) -> None:
        """``os.stat``/``is_file``/``exists`` raise no audit event; watch them here."""
        targets = [(os, name) for name in ("stat", "lstat", "access")]
        targets += [(os.path, name) for name in ("exists", "lexists", "isfile", "isdir", "islink")]
        for owner, name in targets:
            original = getattr(owner, name)

            def probe(
                path: Any, *args: Any, _original: Any = original, _name: str = name, **kwargs: Any
            ) -> Any:
                with contextlib.suppress(Exception):
                    self.check(f"py:{_name}", [path])
                return _original(path, *args, **kwargs)

            setattr(owner, name, probe)


def _text(value: Any) -> str | None:
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.fsdecode(value)
        except TypeError, ValueError:
            return None
    return None


def _emit(result: dict[str, Any]) -> None:
    stream = sys.__stdout__ or sys.stdout
    stream.write("\n" + RESULT_MARKER + json.dumps(result) + "\n")
    stream.flush()


def startup_probe() -> int:
    """Import the app, run ``doctor --json``, read settings, import the UI; report accesses."""
    import importlib
    import importlib.util
    import pkgutil

    spec = importlib.util.find_spec("boardmodeler")
    own = tuple(spec.submodule_search_locations or ()) if spec else ()
    recorder = _Recorder(own)
    recorder.wrap_stat_probes()
    sys.addaudithook(recorder.audit)

    result: dict[str, Any] = {"python": platform.python_version()}
    cli = importlib.import_module("boardmodeler.cli")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result["doctor_exit"] = cli.main(["doctor", "--json"])
    payload = json.loads(captured.getvalue())
    result["doctor_ltspice"] = {
        key: payload["ltspice"].get(key)
        for key in ("found", "path", "reason", "setup_required", "searched", "probed")
    }

    config = importlib.import_module("boardmodeler.config")
    loaded = config.load_config()
    result["config_ltspice_path"] = loaded.ltspice.path
    for name in ("boardmodeler.settings_summary", "boardmodeler.ui.setup_dialog"):
        with contextlib.suppress(ImportError):
            describe = getattr(importlib.import_module(name), "describe_settings", None)
            if describe is not None:
                result["settings_ltspice_path"] = describe(loaded).get("ltspice_path")
                break
    ltspice = importlib.import_module("boardmodeler.simulation.ltspice")
    result["locate_reason"] = ltspice.locate_outcome().reason

    ui = importlib.import_module("boardmodeler.ui")
    imported: list[str] = []
    errors: dict[str, str] = {}
    for module in sorted(m.name for m in pkgutil.iter_modules(ui.__path__)):
        try:
            importlib.import_module(f"boardmodeler.ui.{module}")
            imported.append(module)
        except Exception as exc:
            errors[module] = f"{type(exc).__name__}: {exc}"
    result["ui_imported"] = imported
    result["ui_import_errors"] = errors
    try:
        from PySide6.QtWidgets import QApplication

        result["qt_application_created"] = QApplication.instance() is not None
    except ImportError:
        result["qt_application_created"] = None
    result["events"] = list(recorder.events)
    _emit(result)
    return 0


def configured_run(exe: str) -> int:
    """Save the LTspice path the way SETUP does, then run one ``.op`` deck through the app."""
    from boardmodeler.storage import data_dir, initialize, install_write_guard

    initialize()
    install_write_guard()
    from boardmodeler.config import load_config, save_config
    from boardmodeler.simulation import ltspice
    from boardmodeler.simulation.raw import read_raw

    config = load_config()
    config.ltspice.path = exe  # ui/setup_dialog.py's Save: the field, then save_config()
    saved = save_config(config)
    outcome = ltspice.locate_outcome()
    result: dict[str, Any] = {
        "config_file": str(saved),
        "reason": outcome.reason,
        "resolved": str(outcome.install.path) if outcome.install else None,
        "status": "FAIL",
    }
    if outcome.install is None:
        result["detail"] = f"the saved path did not resolve ({outcome.reason})"
        _emit(result)
        return 1
    run_dir = data_dir() / "fresh-zip-check"
    run_dir.mkdir(parents=True, exist_ok=True)
    deck = run_dir / "divider_op.cir"
    deck.write_text(OP_DECK, encoding="utf-8")
    batch = ltspice.run_batch(
        outcome.install.path, deck, run_dir, timeout_s=float(config.ltspice.timeout_s)
    )
    result["observed"] = batch.observed()
    raw_path = batch.raw_path or batch.op_raw_path
    if batch.ok and raw_path is not None:
        v_out = float(read_raw(raw_path).column("V(out)")[0])
        result["v_out"] = v_out
        if abs(v_out - OP_EXPECTED_V) <= OP_TOLERANCE_V:
            result["status"] = "PASS"
    _emit(result)
    return 0 if result["status"] == "PASS" else 1


def parse_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    return None


def probe_verdict(result: dict[str, Any] | None) -> tuple[bool, str]:
    """PASS only for zero LTspice accesses and a doctor that says 'not configured'."""
    if result is None:
        return False, "the probe produced no result"
    section = result.get("doctor_ltspice") or {}
    problems = []
    if result.get("events"):
        problems.append(f"{len(result['events'])} LTspice-looking access(es)")
    if section.get("found") or section.get("reason") != "unset" or section.get("probed"):
        problems.append(f"doctor did not report 'not configured': {section}")
    if not section.get("setup_required"):
        problems.append("doctor did not ask for SETUP")
    if result.get("qt_application_created"):
        problems.append("importing the UI created a QApplication")
    return (not problems), "; ".join(problems) or "no LTspice access; doctor: not configured"


# --------------------------------------------------------------------------- #
# the outer check


def child_environment(root: Path | None = None) -> dict[str, str]:
    """The parent environment without credentials or settings that point elsewhere."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in _DROPPED_NAMES and not _SECRET_NAME.search(name)
    }
    env["PYTHONUTF8"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    if root is not None:
        env["SPICE_MAKER_ROOT"] = str(root)
    return env


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(repo: str, ref: str, target: Path) -> str:
    if not _REPO_RE.match(repo) or not _REF_RE.match(ref) or ".." in ref:
        raise ValueError(f"refusing repo/ref {repo!r}@{ref!r}")
    url = f"https://github.com/{repo}/archive/{ref}.zip"
    request = urllib.request.Request(url, headers={"User-Agent": "fresh-zip-check"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    return url


def safe_extract(archive_path: Path, dest: Path) -> Path:
    """Extract, refusing absolute/drive/parent paths; return the project folder."""
    base = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if name.startswith(("/", "\\")) or ":" in name or ".." in re.split(r"[\\/]", name):
                raise ValueError(f"unsafe archive member: {name!r}")
            if not (base / name).resolve().is_relative_to(base):
                raise ValueError(f"archive member escapes the work folder: {name!r}")
        archive.extractall(base)
    entries = list(base.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else base


class Verdict:
    def __init__(self) -> None:
        self.checks: dict[str, dict[str, Any]] = {}

    def record(self, name: str, status: str, seconds: float, detail: str, **extra: Any) -> bool:
        self.checks[name] = {"status": status, "seconds": round(seconds, 2), "detail": detail}
        self.checks[name].update(extra)
        print(f"[{status}] {name} ({seconds:.1f}s): {detail}")
        return status == "PASS"

    def skip(self, name: str, reason: str) -> None:
        self.record(name, "SKIP", 0.0, reason)


def fresh_check(args: argparse.Namespace) -> int:
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out = Path(args.out).resolve() if args.out else work / "verdict.json"
    verdict = Verdict()
    report: dict[str, Any] = {
        "tool": "fresh_zip_check",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host_python": platform.python_version(),
        "source": {"repo": args.repo, "ref": args.ref, "zip": args.zip},
    }
    steps = ["acquire", "extract", "venv", "pip_install", "startup_no_ltspice_search"]
    steps += ["configured_op_run", "credential_scan"]
    blocked: str | None = None

    def block_rest(from_step: str) -> None:
        for step in steps[steps.index(from_step) + 1 :]:
            if step not in verdict.checks:
                verdict.skip(step, f"blocked: {from_step} failed")

    started = time.perf_counter()
    try:
        if args.zip:
            archive = Path(args.zip).resolve()
            source = str(archive)
        else:
            archive = work / f"{args.repo.replace('/', '-')}-{args.ref.replace('/', '-')}.zip"
            source = download(args.repo, args.ref, archive)
        report["zip_sha256"] = _sha256(archive)
        verdict.record("acquire", "PASS", time.perf_counter() - started, source)
    except Exception as exc:
        verdict.record("acquire", "FAIL", time.perf_counter() - started, repr(exc))
        blocked = "acquire"

    root: Path | None = None
    if blocked is None:
        started = time.perf_counter()
        try:
            root = safe_extract(archive, Path(tempfile.mkdtemp(prefix="tree-", dir=work)))
            missing = [
                n for n in ("requirements.txt", "src/boardmodeler") if not (root / n).exists()
            ]
            if missing:
                raise FileNotFoundError(f"not a Spice Maker tree, missing {missing}")
            verdict.record("extract", "PASS", time.perf_counter() - started, str(root))
        except Exception as exc:
            verdict.record("extract", "FAIL", time.perf_counter() - started, repr(exc))
            blocked = "extract"

    venv_python: Path | None = None
    if blocked is None and root is not None:
        started = time.perf_counter()
        launcher = shutil.which("py")
        argv = [launcher, "-3.14", "-m", "venv", ".venv"] if launcher else None
        if argv is None and sys.version_info[:2] == (3, 14):
            argv = [sys.executable, "-m", "venv", ".venv"]
        if argv is None:
            verdict.record("venv", "FAIL", 0.0, "no 'py' launcher and this is not Python 3.14")
            blocked = "venv"
        else:
            code, output = _run(argv, cwd=root, env=child_environment(), timeout=600)
            scripts = "Scripts/python.exe" if os.name == "nt" else "bin/python"
            venv_python = root / ".venv" / scripts
            if code == 0 and venv_python.is_file():
                code, version = _run(
                    [str(venv_python), "-c", "import platform;print(platform.python_version())"],
                    cwd=root,
                    env=child_environment(),
                    timeout=60,
                )
                report["venv_python"] = version.strip()
                verdict.record(
                    "venv",
                    "PASS",
                    time.perf_counter() - started,
                    f"{' '.join(argv[:2])} -> Python {version.strip()}",
                )
            else:
                verdict.record("venv", "FAIL", time.perf_counter() - started, output[-2000:])
                blocked = "venv"

    if blocked is None and root is not None and venv_python is not None:
        started = time.perf_counter()
        code, output = _run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                "requirements.txt",
            ],
            cwd=root,
            env=child_environment(),
            timeout=1800,
        )
        if code == 0:
            verdict.record(
                "pip_install",
                "PASS",
                time.perf_counter() - started,
                "pip install -r requirements.txt",
            )
        else:
            verdict.record("pip_install", "FAIL", time.perf_counter() - started, output[-2000:])
            blocked = "pip_install"

    if blocked is None and root is not None and venv_python is not None:
        started = time.perf_counter()
        code, output = _run(
            [str(venv_python), str(Path(__file__).resolve()), "--internal", "startup-probe"],
            cwd=root,
            env=child_environment(root),
            timeout=600,
        )
        probe = parse_result(output)
        ok, detail = probe_verdict(probe)
        if code != 0:
            ok, detail = False, f"probe exited {code}: {output[-1500:]}"
        verdict.record(
            "startup_no_ltspice_search",
            "PASS" if ok else "FAIL",
            time.perf_counter() - started,
            detail,
            events=(probe or {}).get("events", []),
            ui_import_errors=(probe or {}).get("ui_import_errors", {}),
        )

        if args.ltspice:
            started = time.perf_counter()
            code, output = _run(
                [
                    str(venv_python),
                    str(Path(__file__).resolve()),
                    "--internal",
                    "configured-run",
                    "--internal-arg",
                    str(Path(args.ltspice)),
                ],
                cwd=root,
                env=child_environment(root),
                timeout=600,
            )
            run = parse_result(output) or {"status": "FAIL", "detail": output[-1500:]}
            verdict.record(
                "configured_op_run",
                run.get("status", "FAIL"),
                time.perf_counter() - started,
                f"V(out)={run.get('v_out')} expected {OP_EXPECTED_V} "
                f"({run.get('observed') or run.get('detail')})",
                reason=run.get("reason"),
                resolved=run.get("resolved"),
            )
        else:
            verdict.skip("configured_op_run", "no --ltspice path given")

    if (
        root is not None
        and "extract" in verdict.checks
        and verdict.checks["extract"]["status"] == "PASS"
    ):
        started = time.perf_counter()
        scanner = root / "tools" / "scan_credentials.py"
        if not scanner.is_file():
            scanner = Path(__file__).resolve().with_name("scan_credentials.py")
        completed = subprocess.run(
            [sys.executable, str(scanner), str(root), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        try:
            scan = json.loads(completed.stdout)
        except json.JSONDecodeError:
            scan = {"findings": None, "error": completed.stderr[-1500:]}
        status = "PASS" if completed.returncode == 0 else "FAIL"
        verdict.record(
            "credential_scan",
            status,
            time.perf_counter() - started,
            f"{scanner} -> {len(scan.get('findings') or [])} finding(s)",
            findings=scan.get("findings"),
        )
    if blocked is not None:
        block_rest(blocked)

    report["checks"] = verdict.checks
    statuses = [check["status"] for check in verdict.checks.values()]
    report["overall"] = (
        "FAIL" if "FAIL" in statuses else "INCOMPLETE" if "SKIP" in statuses else "PASS"
    )
    report["seconds_total"] = round(sum(c["seconds"] for c in verdict.checks.values()), 2)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"overall: {report['overall']}  verdict: {out}")
    return 0 if report["overall"] == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--repo", help="GitHub owner/name")
    source.add_argument("--zip", help="a local ZIP instead of a download (offline)")
    parser.add_argument("--ref", default="main", help="branch, tag or commit (with --repo)")
    parser.add_argument("--work", help="work folder (a new tree-* folder is made inside)")
    parser.add_argument("--ltspice", help="LTspice.exe to configure for the .op run")
    parser.add_argument("--out", help="verdict JSON path (default <work>/verdict.json)")
    parser.add_argument(
        "--internal", choices=("startup-probe", "configured-run"), help=argparse.SUPPRESS
    )
    parser.add_argument("--internal-arg", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.internal == "startup-probe":
        return startup_probe()
    if args.internal == "configured-run":
        return configured_run(args.internal_arg)
    if not (args.repo or args.zip) or not args.work:
        build_parser().error("give --repo (with --ref) or --zip, and --work")
    return fresh_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
