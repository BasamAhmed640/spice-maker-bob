"""Independent synthetic controls for imported-file identity and safe reopening."""

# ruff: noqa: F811
# Pytest fixture arguments deliberately name the imported fixture providers.

from __future__ import annotations

import json
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from tests.pipeline.test_official_delivery import (  # noqa: F401 — synthetic fixtures
    ORIGINAL,
    build_request,
    official_route,
)

from boardmodeler.authoring import official_spice
from boardmodeler.pipeline import official_delivery
from boardmodeler.pipeline.make_model import load_model_summary, make_model


def write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_official_nested_library_reopens_after_moving_the_whole_folder(
    build_request, official_route, tmp_path
):
    built = make_model(build_request)
    moved = tmp_path / "relocated-model"
    shutil.copytree(build_request.out_dir, moved)
    summary = load_model_summary(moved)
    assert summary.ok and summary.status == "UNKNOWN" and summary.counts["PASS"] == 0
    assert summary.lib_path.is_relative_to(moved) and summary.lib_path.read_bytes() == ORIGINAL
    assert summary.asy_path.is_relative_to(moved)
    assert summary.result.lib_path == summary.lib_path
    assert summary.result.asy_path == summary.asy_path
    assert built.lib_path != summary.lib_path


@pytest.mark.parametrize("changed", ["model", "symbol", "dependency"])
def test_reopen_with_changed_imported_artifacts_withholds_current_paths(
    build_request, official_route, changed
):
    result = make_model(build_request)
    receipt_path = build_request.out_dir / "official-model.json"
    receipt = json.loads(receipt_path.read_text())
    if changed == "model":
        result.lib_path.write_bytes(ORIGINAL + b"* changed\n")
    elif changed == "symbol":
        result.asy_path.write_text("* changed symbol\n")
    else:
        # A synthetic declared dependency demonstrates that its bytes are checked too.
        root = build_request.out_dir / receipt["bundle_root"]
        (root / "extra.lib").write_bytes(b"changed dependency")
        receipt["files"]["extra.lib"] = "0" * 64
        write_json(receipt_path, receipt)
    summary = load_model_summary(build_request.out_dir)
    assert summary.ok and summary.status == "BLOCKED"
    assert summary.lib_path is None and summary.asy_path is None
    assert summary.result.lib_path is None and summary.result.asy_path is None
    assert "could not be verified" in summary.results_problem


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_path", r"\\server\share\model.lib"),
        ("model_path", "../outside.lib"),
        ("bundle_root", "https://example.invalid/bundle"),
        ("symbol_path", r"\\server\share\model.asy"),
    ],
)
def test_recorded_network_or_escaping_import_paths_are_refused_before_resolution(
    build_request, official_route, monkeypatch, field, value
):
    make_model(build_request)
    path = build_request.out_dir / "official-model.json"
    receipt = json.loads(path.read_text())
    receipt[field] = value
    write_json(path, receipt)
    original_resolve = Path.resolve

    def guard_resolve(self, *args, **kwargs):
        assert not str(self).replace("\\", "/").startswith("//"), "UNC resolution attempted"
        assert "://" not in str(self), "URL resolution attempted"
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guard_resolve)
    summary = load_model_summary(build_request.out_dir)
    assert summary.status == "BLOCKED" and summary.lib_path is None


def test_selected_unc_folder_is_refused_before_any_filesystem_lookup(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network folder must be refused before filesystem lookup")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "is_dir", forbidden)
    summary = load_model_summary(r"\\server\share\model")
    assert not summary.ok and "refused" in summary.reason


@pytest.mark.parametrize("changed", ["model", "dependency"])
def test_post_load_model_or_dependency_mutation_cannot_be_delivered(
    build_request, official_route, monkeypatch, changed
):
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(
            "AD999.lib", ORIGINAL.replace(b".SUBCKT", b'.include "helper.lib"\r\n.SUBCKT', 1)
        )
        archive.writestr("helper.lib", b"* TEST_FIXTURE dependency, no device data\n")
    monkeypatch.setattr(
        official_spice,
        "_download",
        lambda url, **kwargs: (archive_bytes.getvalue(), "application/zip", url),
    )

    def changed_during_load(model, *args, **kwargs):
        target = model if changed == "model" else model.parent / "helper.lib"
        target.write_bytes(target.read_bytes() + b"* simulated concurrent edit\n")
        return {"status": "loaded", "electrical_accuracy_verified": False}

    monkeypatch.setattr(official_delivery, "load_check", changed_during_load)
    result = make_model(build_request)
    assert result.status == "BLOCKED" and result.lib_path is None and result.asy_path is None
    assert "changed_during_validation" in result.detail


def test_reopen_verifies_preserved_source_zip(build_request, official_route, monkeypatch):
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("AD999.lib", ORIGINAL)
    monkeypatch.setattr(
        official_spice,
        "_download",
        lambda url, **kwargs: (archive_bytes.getvalue(), "application/zip", url),
    )
    result = make_model(build_request)
    assert result.status == "UNKNOWN"
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    package = build_request.out_dir / receipt["source_package_path"]
    assert load_model_summary(build_request.out_dir).status == "UNKNOWN"
    package.write_bytes(package.read_bytes() + b"changed original ZIP")
    summary = load_model_summary(build_request.out_dir)
    assert summary.status == "BLOCKED" and summary.lib_path is None
    assert "source package bytes changed" in summary.results_problem
