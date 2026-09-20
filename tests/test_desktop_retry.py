"""Desktop document-permission retry regressions for IBM Bob."""

from pathlib import Path

import pytest

from boardmodeler.documents.store import DocumentStore, DocumentStoreError
from boardmodeler.pipeline.make_model import MakeModelRequest, _Run, _StageLog
from tests.documents.test_store import _write_datasheet


def test_desktop_retry_can_grant_and_revoke_remote_permission(tmp_path: Path):
    pdf = tmp_path / "datasheet.pdf"
    _write_datasheet(pdf)
    records = []
    for consent in (False, True, False):
        run = _Run(
            MakeModelRequest(
                part="LM358",
                subckt="LM358",
                datasheet=pdf,
                out_dir=tmp_path / "model",
                allow_remote=consent,
            ),
            _StageLog(None),
        )
        run.read()
        assert run.record.remote_inference_allowed is consent
        assert run.store.get(run.record.doc_id).remote_inference_allowed is consent
        records.append(run.record)
    assert len({r.doc_id for r in records}) == 1
    assert len({r.file_hash for r in records}) == 1
    assert len({r.path for r in records}) == 1


def test_permission_update_does_not_allow_other_metadata_changes(tmp_path: Path):
    pdf = tmp_path / "datasheet.pdf"
    _write_datasheet(pdf)
    store = DocumentStore(tmp_path / "project")
    original = store.add_file(pdf, doc_type="datasheet", provenance="user_supplied")
    with pytest.raises(DocumentStoreError, match="different metadata"):
        store.add_file(
            pdf,
            doc_type="datasheet",
            provenance="user_supplied",
            remote_inference_allowed=True,
        )
    with pytest.raises(DocumentStoreError, match="different metadata"):
        store.add_file(
            pdf,
            doc_type="datasheet",
            provenance="user_supplied",
            classification="public",
            update_remote_permission=True,
        )
    assert store.get(original.doc_id) == original
