"""Local synthetic artifacts exercise integrity, never device performance."""

from __future__ import annotations

import json

import pytest

from boardmodeler.models.library import ModelStore, ModelStoreError

ORIGINAL = b"* TEST_FIXTURE: synthetic vendor file, no device claim\r\n.SUBCKT SAMPLE 1 2\r\nR1 1 2 1k\r\n.ENDS\r\n"


@pytest.fixture
def original(tmp_path):
    source = tmp_path / "input.lib"
    source.write_bytes(ORIGINAL)
    store = ModelStore(tmp_path / "store")
    record = store.add_vendor_original(
        source,
        model_id="sample",
        license_note="TEST_FIXTURE: local use",
        source_url="https://example.invalid/sample.lib",
    )
    receipt = store.root / "models/records/sample.json"
    return source, store, record, receipt


def test_exact_original_reimport_is_idempotent_without_metadata_or_timestamp_changes(original):
    source, store, record, receipt = original
    target = record.absolute(store.root)
    before = (
        target.read_bytes(),
        target.stat().st_mtime_ns,
        receipt.read_bytes(),
        receipt.stat().st_mtime_ns,
    )
    returned = store.add_vendor_original(
        source,
        model_id="sample",
        license_note="Another label",
        source_url="https://example.invalid/new-location.lib",
    )
    assert returned == record and store.verify("sample")
    assert before == (
        target.read_bytes(),
        target.stat().st_mtime_ns,
        receipt.read_bytes(),
        receipt.stat().st_mtime_ns,
    )


def test_changed_original_cannot_overwrite_either_file_or_record(original):
    source, store, record, receipt = original
    target = record.absolute(store.root)
    before = (target.read_bytes(), receipt.read_bytes())
    source.write_bytes(ORIGINAL.replace(b"1k", b"2k"))
    with pytest.raises(ModelStoreError, match="cannot be replaced"):
        store.add_vendor_original(source, model_id="sample", license_note="TEST_FIXTURE")
    assert before == (target.read_bytes(), receipt.read_bytes())


@pytest.mark.parametrize(
    "kind", ["generated", "vendor_adapted", "primitive_library", "vendor_original"]
)
def test_other_artifact_writers_cannot_replace_original_identity(original, kind):
    _source, store, record, receipt = original
    before = (record.absolute(store.root).read_bytes(), receipt.read_bytes())
    with pytest.raises(ModelStoreError):
        store.add_text_artifact(ORIGINAL.decode(), model_id="sample", kind=kind)
    assert before == (record.absolute(store.root).read_bytes(), receipt.read_bytes())


@pytest.mark.parametrize(
    "corruption", ["bytes", "missing", "size", "sha256", "immutable", "json", "path"]
)
def test_reimport_verifies_saved_original_and_receipt_before_reuse(original, corruption, tmp_path):
    source, store, record, receipt = original
    target = record.absolute(store.root)
    if corruption == "bytes":
        target.write_bytes(b"modified")
    elif corruption == "missing":
        target.unlink()
    elif corruption == "json":
        receipt.write_text("{bad JSON", encoding="utf-8")
    else:
        metadata = json.loads(receipt.read_text())
        metadata[corruption] = {
            "size": 0,
            "sha256": "0" * 64,
            "immutable": False,
            "path": "../outside.lib",
        }[corruption]
        receipt.write_text(json.dumps(metadata), encoding="utf-8")
    before = (target.read_bytes() if target.exists() else None, receipt.read_bytes())
    with pytest.raises(ModelStoreError):
        store.add_vendor_original(source, model_id="sample", license_note="TEST_FIXTURE")
    assert before == (target.read_bytes() if target.exists() else None, receipt.read_bytes())


def test_sanitized_filename_collision_cannot_replace_original(original):
    source, store, record, receipt = original
    before = (record.absolute(store.root).read_bytes(), receipt.read_bytes())
    with pytest.raises(ModelStoreError, match="ID collision"):
        store.add_vendor_original(source, model_id="folder/sample", license_note="TEST_FIXTURE")
    assert before == (record.absolute(store.root).read_bytes(), receipt.read_bytes())


def test_original_requires_explicit_local_delivery_opt_in(original, tmp_path):
    _source, store, record, _receipt = original
    destination = tmp_path / "local-output/model.lib"
    with pytest.raises(ModelStoreError, match="not redistributed"):
        store.export_copy("sample", destination)
    assert not destination.exists()
    assert store.export_copy("sample", destination, allow_vendor_original=True) == destination
    assert destination.read_bytes() == ORIGINAL and store.verify("sample")
    assert record.absolute(store.root).read_bytes() == ORIGINAL


def test_local_delivery_checks_integrity_before_touching_destination(original, tmp_path):
    _source, store, record, _receipt = original
    destination = tmp_path / "existing.lib"
    destination.write_bytes(b"preserved user artifact")
    record.absolute(store.root).write_bytes(b"modified stored file")
    with pytest.raises(ModelStoreError, match="integrity"):
        store.export_copy("sample", destination, allow_vendor_original=True)
    assert destination.read_bytes() == b"preserved user artifact"


def test_original_does_not_replace_prior_generated_identity(tmp_path):
    store = ModelStore(tmp_path / "store")
    old = store.add_text_artifact("* TEST_FIXTURE\n", model_id="same", kind="generated")
    source = tmp_path / "input.lib"
    source.write_bytes(ORIGINAL)
    with pytest.raises(ModelStoreError, match="already registered"):
        store.add_vendor_original(source, model_id="same", license_note="TEST_FIXTURE")
    assert old.absolute(store.root).read_bytes() == b"* TEST_FIXTURE\n"


def test_unrecorded_original_is_not_overwritten(tmp_path):
    store = ModelStore(tmp_path / "store")
    orphan = store.root / "models/vendor/sample.lib"
    orphan.write_bytes(b"unrecorded original")
    source = tmp_path / "input.lib"
    source.write_bytes(ORIGINAL)
    with pytest.raises(ModelStoreError, match="already exists"):
        store.add_vendor_original(source, model_id="sample", license_note="TEST_FIXTURE")
    assert orphan.read_bytes() == b"unrecorded original"
    assert not (store.root / "models/records/sample.json").exists()


def test_generated_bytes_match_receipt_and_export_on_windows(tmp_path):
    store = ModelStore(tmp_path / "store")
    text = "* TEST_FIXTURE\n.SUBCKT SAMPLE 1 2\n.ENDS\n"
    record = store.add_text_artifact(text, model_id="sample", kind="generated")
    assert record.absolute(store.root).read_bytes() == text.encode()
    assert store.verify("sample")
    destination = tmp_path / "output.lib"
    store.export_copy("sample", destination)
    assert destination.read_bytes() == text.encode()


@pytest.mark.parametrize("destination", ["models/vendor/sample.lib", "models/records/sample.json"])
def test_export_cannot_replace_original_or_metadata_through_another_model(original, destination):
    _source, store, record, receipt = original
    store.add_text_artifact("* TEST_FIXTURE: another model\n", model_id="other", kind="generated")
    before = (record.absolute(store.root).read_bytes(), receipt.read_bytes())
    with pytest.raises(ModelStoreError, match="protected model storage"):
        store.export_copy("other", store.root / destination)
    assert before == (record.absolute(store.root).read_bytes(), receipt.read_bytes())
