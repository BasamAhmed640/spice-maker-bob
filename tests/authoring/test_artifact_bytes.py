"""Hash-critical artifacts are written without newline translation.

``.gitattributes`` marks ``*.lib`` and the generated ``.cir`` decks as binary precisely
because the pipeline content-hashes them, so a CRLF-only difference must not change a
hash. That means a repair may change only the bytes it repairs, and an archive must hold
the bytes it replaced.
"""

import hashlib

from boardmodeler.authoring.model_reference import normalize_ground_reference
from boardmodeler.authoring.model_syntax import archive_original, write_library

GND_PIN = [{"name": "GND", "function": "Device GND"}]
LIBRARY = "* TEST_FIXTURE\n.subckt DUT A Y VCC GND\nR1 A 0 1k\nR2 Y 0 1k\n.ends DUT\n"


def test_archive_original_keeps_exact_bytes_and_names_them_by_hash(tmp_path):
    data = b".subckt DUT A B\r\nR1 A B 1k\r\n.ends DUT\r\n"

    archive_original(tmp_path / "evidence", data)

    saved = list((tmp_path / "evidence").glob("*.lib"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == data, "the archive is the bytes that were replaced"
    assert saved[0].name == f"{hashlib.sha256(data).hexdigest()}.lib"


def test_write_library_never_translates_newlines(tmp_path):
    lf = tmp_path / "lf.lib"
    write_library(lf, "a\nb\n")
    assert lf.read_bytes() == b"a\nb\n"

    crlf = tmp_path / "crlf.lib"
    write_library(crlf, "a\r\nb\r\n")
    assert crlf.read_bytes() == b"a\r\nb\r\n"


def test_ground_reference_rewrite_keeps_lf_and_archives_the_replaced_bytes(tmp_path):
    """A one-token rewrite on Windows used to re-encode the whole library."""
    model = tmp_path / "DUT.lib"
    model.write_text(LIBRARY, encoding="utf-8", newline="\n")
    original = model.read_bytes()

    assert normalize_ground_reference(model, GND_PIN, tmp_path / "evidence") is True

    text = model.read_bytes()
    assert b"R1 A GND 1k" in text
    assert b"R2 Y GND 1k" in text
    assert b"\r" not in text, "only the tokens change, never the line endings"
    changed = [
        index
        for index, (before, after) in enumerate(
            zip(original.splitlines(), text.splitlines(), strict=True)
        )
        if before != after
    ]
    assert changed == [2, 3], changed
    saved = list((tmp_path / "evidence").glob("*.lib"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == original
