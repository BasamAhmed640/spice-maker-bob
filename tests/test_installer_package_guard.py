"""Keep the Bob edition's early stale-wheel refusal during installer mirroring."""

import importlib.util
import zipfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("condition", ["missing_wheel", "missing_module", "stale_module"])
def test_bad_application_wheel_refused_before_payload_packaging(tmp_path, monkeypatch, condition):
    script = Path(__file__).parents[1] / "installer/package_portable.py"
    spec = importlib.util.spec_from_file_location("bob_package_guard", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="1.8.2"\n')
    source = tmp_path / "src/boardmodeler/module.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"current source\n")
    wheels = tmp_path / "build/portable/env/wheels"
    wheels.mkdir(parents=True)
    if condition != "missing_wheel":
        with zipfile.ZipFile(wheels / "boardmodeler-1.8.2-py3-none-any.whl", "w") as wheel:
            if condition == "stale_module":
                wheel.writestr("boardmodeler/module.py", b"stale source\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("Refuse before packaging any payload or launching a compiler")

    monkeypatch.setattr(module, "_zip_tree", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    message = {
        "missing_wheel": "wheel missing",
        "missing_module": "wheel lacks",
        "stale_module": "wheel is stale",
    }[condition]
    with pytest.raises(RuntimeError, match=message):
        module.package(tmp_path, "SpiceMakerBob")
