"""Syntax repairs that must preserve every value, reference and original byte.

The real failure these cover is the UCC28251 line LTspice rejected:

``G_EA nEA GND I = 1m * (V(FB_EAM,GND) - V(nEA_ref,GND))``
"""

import hashlib

import pytest

from boardmodeler.authoring.model_syntax import (
    normalize_behavioral_sources,
    validate_library,
)
from boardmodeler.authoring.probes import ProbeError

REAL_LINE = "G_EA nEA GND I = 1m * (V(FB_EAM,GND) - V(nEA_ref,GND))"
EXPRESSION = "1m * (V(FB_EAM,GND) - V(nEA_ref,GND))"


def test_the_ucc28251_line_becomes_a_b_source_and_keeps_its_expression(tmp_path):
    """The exact line from the failed run, repaired without touching the expression."""
    model = tmp_path / "UCC28251.lib"
    model.write_text(
        f"* TEST_FIXTURE\n.subckt UCC28251 nEA GND\n{REAL_LINE}\nR1 nEA GND 1k\n.ends UCC28251\n",
        encoding="utf-8",
    )
    original = model.read_text(encoding="utf-8")

    assert normalize_behavioral_sources(model, tmp_path / "original")

    text = model.read_text(encoding="utf-8")
    assert f"B_G_EA nEA GND I = {EXPRESSION}" in text
    assert "G_EA" not in text.split(), "the original G source name must not survive"
    saved = list((tmp_path / "original").glob("*.lib"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == original, "the original must be recoverable"
    validate_library(model)
    assert normalize_behavioral_sources(model, tmp_path / "original") is False


def test_current_references_follow_the_renamed_element(tmp_path):
    """A renamed element must stay addressable from every ``I(name)`` that read it."""
    model = tmp_path / "m.lib"
    model.write_text(
        ".subckt DUT a b c\nG_EA a b I = 1m * V(a)\nB2 c 0 I = I(G_EA) * 2\nR1 c 0 1k\n.ends DUT\n",
        encoding="utf-8",
    )

    assert normalize_behavioral_sources(model, tmp_path / "original")

    text = model.read_text(encoding="utf-8")
    assert "B_G_EA a b I = 1m * V(a)" in text
    assert "I(B_G_EA)" in text, text
    assert "I(G_EA)" not in text


def test_an_existing_element_name_is_never_overwritten(tmp_path):
    """A name collision must not silently rename a component the model already uses."""
    model = tmp_path / "m.lib"
    model.write_text(
        ".subckt DUT a b c d\nB_G1 a b I = 1\nG1 c d I = 2m * V(c)\nR1 c d 1k\n.ends DUT\n",
        encoding="utf-8",
    )

    assert normalize_behavioral_sources(model, tmp_path / "original")

    text = model.read_text(encoding="utf-8")
    assert "B_G1_FIX c d I = 2m * V(c)" in text
    assert "B_G1 a b I = 1" in text, "the unrelated element keeps its own name"


def test_an_e_source_with_voltage_syntax_is_repaired_too(tmp_path):
    """``E`` written with ``V=`` is the same mistake with the other quantity."""
    model = tmp_path / "m.lib"
    model.write_text(
        ".subckt DUT a b c\nE_GAIN c 0 V = 10 * V(a)\nR1 c 0 1k\n.ends DUT\n",
        encoding="utf-8",
    )

    assert normalize_behavioral_sources(model, tmp_path / "original")

    assert "B_E_GAIN c 0 V = 10 * V(a)" in model.read_text(encoding="utf-8")


def test_a_valid_library_is_left_alone(tmp_path):
    """Only the invalid combination is touched, so nothing else is rewritten."""
    body = ".subckt DUT a b\nB1 a b I = 1m * V(a)\nR1 a b 1k\n.ends DUT\n"
    model = tmp_path / "m.lib"
    model.write_text(body, encoding="utf-8")

    assert normalize_behavioral_sources(model, tmp_path / "original") is False
    assert model.read_text(encoding="utf-8") == body
    assert not (tmp_path / "original").exists(), "nothing was changed, so nothing is archived"


def test_validate_library_rejects_the_invalid_form(tmp_path):
    """The deterministic rule that keeps the rejected form out of a finished model."""
    model = tmp_path / "m.lib"
    model.write_text(
        f".subckt DUT a b\n{REAL_LINE.replace('G_EA nEA GND', 'G_EA a b')}\nR1 a b 1k\n.ends DUT\n",
        encoding="utf-8",
    )

    with pytest.raises(ProbeError, match="requires a B source"):
        validate_library(model)


def test_a_one_line_repair_keeps_lf_endings_and_archives_exact_bytes(tmp_path):
    """A repair must not re-encode the whole library (Windows text mode used to)."""
    model = tmp_path / "UCC28251.lib"
    model.write_text(
        f"* TEST_FIXTURE\n.subckt UCC28251 nEA GND\n{REAL_LINE}\nR1 nEA GND 1k\n.ends UCC28251\n",
        encoding="utf-8",
        newline="\n",
    )
    original_bytes = model.read_bytes()

    assert normalize_behavioral_sources(model, tmp_path / "original")

    assert b"\r" not in model.read_bytes(), "the repair must not introduce CRLF"
    before = original_bytes.decode("utf-8").splitlines()
    after = model.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [2]
    archived = list((tmp_path / "original").glob("*.lib"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original_bytes, "the archive is the exact replaced bytes"
    assert archived[0].name == f"{hashlib.sha256(original_bytes).hexdigest()}.lib"


def test_a_crlf_library_keeps_its_own_endings(tmp_path):
    """A Windows-authored library keeps CRLF: only the repaired line changes."""
    model = tmp_path / "m.lib"
    model.write_bytes(b".subckt DUT a b\r\nG1 a b I = 2m * V(a)\r\nR1 a b 1k\r\n.ends DUT\r\n")

    assert normalize_behavioral_sources(model, tmp_path / "original")

    text = model.read_bytes()
    assert b"B_G1 a b I = 2m * V(a)\r\n" in text
    assert text.count(b"\r\n") == 4, "every original terminator is preserved"
    assert text.count(b"\n") == 4, "no bare LF was introduced"


@pytest.mark.parametrize("nested", [False, True])
def test_repair_and_current_references_stay_in_their_own_scope(tmp_path, nested):
    repaired = ".subckt FIX a b\nBMON a b V = I(G1)\nG1 a b I = V(a,b)\n"
    untouched = ".subckt VALID a b c d\nG1 a b c d 1m\nBMON a b V = I(G1)\n.ends VALID\n"
    body = (
        repaired + untouched + "BPOST a b V = I(G1)\n.ends FIX\n"
        if nested
        else repaired + ".ends FIX\n" + untouched
    )
    model = tmp_path / "scopes.lib"
    model.write_bytes(body.encode())
    assert normalize_behavioral_sources(model, tmp_path / "evidence")
    text = model.read_text(encoding="utf-8")
    assert untouched in text
    assert "BMON a b V = I(B_G1)" in text
    if nested:
        assert "BPOST a b V = I(B_G1)" in text
    validate_library(model)


def test_independent_repairs_handle_local_collisions_and_preserve_comments(tmp_path):
    body = (
        ".subckt A a b\n* I(G1) documents the original\n\n"
        "  G1 a b I=V(a,b) ; I(G1) remains a comment\n"
        "BMON a b V=I ( g1 )\nB_G1 a b I=0\n.ends A\n"
        ".SUBCKT B a b\nG1 a b I=V(a,b)\nBMON a b V=I(G1)\n.ENDS B\n"
    )
    model = tmp_path / "scopes.lib"
    model.write_bytes(body.encode())
    assert normalize_behavioral_sources(model, tmp_path / "evidence")
    text = model.read_text(encoding="utf-8")
    assert "* I(G1) documents the original\n\n  B_G1_FIX" in text
    assert "; I(G1) remains a comment" in text
    assert "BMON a b V=I ( B_G1_FIX )" in text
    assert ".SUBCKT B a b\nB_G1 a b I=V(a,b)\nBMON a b V=I(B_G1)" in text
    validate_library(model)


def test_repair_does_not_hide_duplicate_component_names(tmp_path):
    model = tmp_path / "duplicate.lib"
    body = b".subckt A a b c d\nG1 a b I=V(a,b)\nG1 a b c d 1m\n.ends A\n"
    model.write_bytes(body)
    assert not normalize_behavioral_sources(model, tmp_path / "evidence")
    assert model.read_bytes() == body
    with pytest.raises(ProbeError):
        validate_library(model)
