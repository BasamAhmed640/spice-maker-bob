from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardmodeler.cli import main
from boardmodeler.domain.hashing import sha256_bytes
from boardmodeler.models.vendor_io import import_io_source


@pytest.mark.parametrize(
    "suffix,data,kind",
    [
        (".ibs", b"[IBIS Ver] 7.2\n[Model] TEST_FIXTURE\n[End]\n", "ibis"),
        (".s2p", b"! TEST_FIXTURE\n# GHz S RI R 50\n1 0 0 1 0 1 0 0 0\n", "touchstone"),
        (
            ".ami",
            b"(TEST_FIXTURE (Reserved_Parameters (AMI_Version (Value 7.2))))",
            "ibis_ami_parameters",
        ),
    ],
)
def test_cli_import_preserves_source_and_does_not_claim_validation(
    tmp_path, capsys, suffix, data, kind
):
    source = tmp_path / ("fixture" + suffix)
    source.write_bytes(data)
    assert (
        main(
            [
                "model",
                "import",
                "--file",
                str(source),
                "--out",
                str(tmp_path / "library"),
                "--part",
                "TEST_FIXTURE",
                "--source-url",
                "https://example.invalid/model",
                "--license-note",
                "synthetic",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    stored = Path(result["manifest"]).parent / result["path"]
    assert stored.read_bytes() == data
    assert result["sha256"] == sha256_bytes(data)
    assert result["kind"] == kind
    assert result["electrical_validation"] == "UNKNOWN"
    assert result["format_validation"] == "NOT_RUN"
    assert result["source_url_verified"] is False


def test_import_rejects_executable_and_changed_original(tmp_path):
    source = tmp_path / "binary.dll"
    source.write_bytes(b"MZ")
    kwargs = {
        "part": "TEST",
        "source_url": "https://example.invalid/model",
        "license_note": "unknown",
    }
    with pytest.raises(ValueError, match="executable"):
        import_io_source(source, tmp_path, **kwargs)
    source = tmp_path / "test.ibs"
    source.write_text("[IBIS Ver] 7.2\n[Model] TEST\n[End]")
    result = import_io_source(source, tmp_path, **kwargs)
    (Path(result["manifest"]).parent / result["path"]).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        import_io_source(source, tmp_path, **kwargs)
