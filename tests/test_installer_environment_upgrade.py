"""Run the real C# provisioning code against small, local synthetic wheel fixtures.

These fixtures exercise installer transactions, not electrical device behavior.
No payload, user credential or network request is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from boardmodeler.engine_identity import source_contract

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows C# installer")
REPO = Path(__file__).resolve().parents[1]
NEW_VERSION = "1.8.0"


def _run(argv, *, root, check=True):
    environment = os.environ.copy()
    for name in list(environment):
        if name.upper().startswith(("PYTHON", "VIRTUAL_ENV", "PIP_")):
            environment.pop(name)
    return subprocess.run(
        [str(value) for value in argv],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=check,
        timeout=120,
    )


@pytest.fixture(scope="module")
def provisioner(tmp_path_factory):
    compiler = (
        Path(os.environ.get("WINDIR", "C:/Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    if not compiler.is_file():
        pytest.skip("Framework C# compiler unavailable")
    directory = tmp_path_factory.mktemp("installer-provisioner")
    harness = directory / "Harness.cs"
    harness.write_text(
        "using System; using System.Reflection;\n"
        "internal static class ProvisionHarness {\n"
        " public static int Main(string[] args) {\n"
        "  var flags=BindingFlags.Static|BindingFlags.NonPublic;\n"
        "  var type=typeof(PortableInstaller);\n"
        '  type.GetField("Root",flags).SetValue(null,args[0]);\n'
        '  type.GetField("Version",flags).SetValue(null,args[1]);\n'
        '  type.GetMethod("ProvisionEnvironment",flags).Invoke(null,null);\n'
        '  var warning=(string)type.GetField("EnvironmentWarning",flags).GetValue(null);\n'
        "  if(warning!=null) {Console.WriteLine(warning); return 1;} return 0;\n"
        " }\n}\n",
        encoding="utf-8",
    )
    executable = directory / "Provision.exe"
    _run(
        [
            compiler,
            "/nologo",
            "/target:exe",
            "/main:ProvisionHarness",
            f"/out:{executable}",
            "/r:System.Windows.Forms.dll",
            "/r:System.Drawing.dll",
            "/r:System.IO.Compression.dll",
            "/r:System.IO.Compression.FileSystem.dll",
            REPO / "installer/PortableInstaller.cs",
            harness,
        ],
        root=directory,
    )
    return executable


def _wheel(directory, version, *, stale=False, activation_failure=False):
    source = directory / ("source-" + version + ("-stale" if stale else ""))
    package = source / "boardmodeler"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"__version__ = {version!r}\n", encoding="utf-8")
    if stale:
        with (package / "__init__.py").open("a", encoding="utf-8") as stream:
            stream.write("# Synthetic old engine with the same declared version\n")
    identity = (REPO / "src/boardmodeler/engine_identity.py").read_text(encoding="utf-8")
    if activation_failure:
        identity += (
            "\n_original_contract = engine_contract\n"
            "def engine_contract():\n"
            "    if '.venv-upgrade-' not in str(Path(__file__)):\n"
            "        raise ValueError('test_injected_activation_failure')\n"
            "    return _original_contract()\n"
        )
    (package / "engine_identity.py").write_text(identity, encoding="utf-8")
    if stale:
        (package / "old_engine.py").write_text(
            "# Synthetic stale engine sentinel\n", encoding="utf-8"
        )
    expected = source_contract(package)
    metadata = f"boardmodeler-{version}.dist-info"
    wheel = directory / f"boardmodeler-{version}-py3-none-any.whl"
    contents = {
        path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*.py")
    }
    # Import-only dependency fixtures keep this transaction test small and entirely local.
    contents.update(
        {
            f"{name}.py": b"# Synthetic installer dependency fixture\n"
            for name in ("numpy", "pydantic", "pypdf", "pypdfium2")
        }
    )
    contents[f"{metadata}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: boardmodeler\nVersion: {version}\n".encode()
    )
    contents[f"{metadata}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    contents[f"{metadata}/RECORD"] = "".join(
        f"{name},,\n" for name in [*contents, f"{metadata}/RECORD"]
    ).encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return wheel, expected


def _setup(root, old_version, *, stale=False, failure=None):
    old_dir, new_dir = root / "old", root / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_wheel, _ = _wheel(old_dir, old_version, stale=stale)
    new_wheel, expected = _wheel(new_dir, NEW_VERSION, activation_failure=failure == "activation")
    _run([sys.executable, "-m", "venv", root / ".venv"], root=root)
    python = root / ".venv/Scripts/python.exe"
    _run([python, "-m", "pip", "install", "--no-index", "--no-deps", old_wheel], root=root)
    wheels = root / "env/wheels"
    wheels.mkdir(parents=True)
    bundled = wheels / new_wheel.name
    shutil.copyfile(new_wheel, bundled)
    digest = hashlib.sha256(bundled.read_bytes()).hexdigest()
    (root / "env/wheels.sha256").write_text(f"{digest}  {bundled.name}\n", encoding="ascii")
    (root / "env/requirements.txt").write_text(f"boardmodeler=={NEW_VERSION}\n", encoding="ascii")
    snapshot = root / "app/_internal/boardmodeler/engine-identity.json"
    snapshot.parent.mkdir(parents=True)
    if failure == "staged_identity":
        expected["source_sha256"] = "0" * 64
    snapshot.write_text(json.dumps(expected), encoding="utf-8")
    if failure == "checksum":
        bundled.write_bytes(bundled.read_bytes() + b"tampered")
    sentinels = (
        "data/config.json",
        "data/credentials-test-sentinel.json",
        "models/test-model.lib",
        ".venv/Lib/site-packages/extra_user_package.py",
    )
    for name in sentinels:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic preservation sentinel\n")
    _package_report(root)  # Warm imports before the byte/mtime snapshot.
    return python, expected, sentinels


def _package_report(root):
    result = _run(
        [
            root / ".venv/Scripts/python.exe",
            "-c",
            "import json,importlib.metadata,boardmodeler,numpy,pydantic,pypdf,pypdfium2;"
            " from boardmodeler.engine_identity import engine_contract;"
            " print(json.dumps({'version':boardmodeler.__version__,'engine':engine_contract(),"
            " 'metadata_version':importlib.metadata.version('boardmodeler')}))",
        ],
        root=root,
    )
    return json.loads(result.stdout)


def _snapshot(path):
    return {
        file.relative_to(path).as_posix(): (
            hashlib.sha256(file.read_bytes()).hexdigest(),
            file.stat().st_mtime_ns,
        )
        for file in path.rglob("*")
        if file.is_file()
    }


def test_matching_environment_is_a_byte_and_mtime_no_op(tmp_path, provisioner):
    _setup(tmp_path, NEW_VERSION)
    before = _snapshot(tmp_path / ".venv")
    _run([provisioner, tmp_path, NEW_VERSION], root=tmp_path)
    assert _snapshot(tmp_path / ".venv") == before


@pytest.mark.parametrize("old_version", ["1.7.0", NEW_VERSION])
def test_old_version_or_same_version_old_engine_upgrades_locally(
    tmp_path, provisioner, old_version
):
    python, expected, sentinels = _setup(tmp_path, old_version, stale=True)
    interpreter = (python.read_bytes(), python.stat().st_mtime_ns)
    configuration = (tmp_path / ".venv/pyvenv.cfg").read_bytes()
    _run([provisioner, tmp_path, NEW_VERSION], root=tmp_path)
    report = _package_report(tmp_path)
    assert report == {"version": NEW_VERSION, "engine": expected, "metadata_version": NEW_VERSION}
    assert (python.read_bytes(), python.stat().st_mtime_ns) == interpreter
    assert (tmp_path / ".venv/pyvenv.cfg").read_bytes() == configuration
    assert not (tmp_path / ".venv/Lib/site-packages/boardmodeler/old_engine.py").exists()
    for name in sentinels:
        assert (tmp_path / name).read_bytes() == b"synthetic preservation sentinel\n"
    assert not list(tmp_path.glob(".venv-upgrade-*"))
    assert not list(tmp_path.glob(".venv-packages-previous-*"))


@pytest.mark.parametrize("failure", ["checksum", "staged_identity", "activation"])
def test_failed_upgrade_retains_a_usable_old_environment(tmp_path, provisioner, failure):
    _setup(tmp_path, "1.7.0", stale=True, failure=failure)
    before = _package_report(tmp_path)
    result = _run([provisioner, tmp_path, NEW_VERSION], root=tmp_path, check=False)
    assert result.returncode == 1
    assert _package_report(tmp_path) == before
    for name in (
        "data/config.json",
        "data/credentials-test-sentinel.json",
        "models/test-model.lib",
    ):
        assert (tmp_path / name).read_bytes() == b"synthetic preservation sentinel\n"
    assert not list(tmp_path.glob(".venv-upgrade-*"))
    assert not list(tmp_path.glob(".venv-packages-previous-*"))


def test_linked_environment_is_rejected_without_touching_its_target(tmp_path, provisioner):
    root = tmp_path / "copy"
    root.mkdir()
    _setup(root, "1.7.0", stale=True)
    original = root / ".venv"
    external = tmp_path / "external-environment"
    # Both absolute move targets are explicitly bounded to this test's temporary workspace.
    assert original.resolve().is_relative_to(tmp_path.resolve())
    assert external.resolve().is_relative_to(tmp_path.resolve())
    original.rename(external)
    link = _run(["cmd", "/c", "mklink", "/J", original, external], root=tmp_path, check=False)
    if link.returncode != 0:
        pytest.skip("directory junctions unavailable")
    before = _snapshot(external)
    result = _run([provisioner, root, NEW_VERSION], root=root, check=False)
    assert result.returncode == 1
    assert "linked path" in result.stdout
    assert _snapshot(external) == before
