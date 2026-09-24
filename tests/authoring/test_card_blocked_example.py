"""A build that ends without a model still writes its card and example deck.

A Bob-edition LM358 build with no Bob key (BLOCKED, no model file) crashed inside
``write_deliverables``: the op-amp example is rendered from the model's port list,
and the model was never written. The outcome was already decided, so the crash only
hid it behind a traceback.
"""

from __future__ import annotations

from types import SimpleNamespace

from boardmodeler.authoring import card


def test_blocked_opamp_build_writes_the_generic_example(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(card, "render_card", lambda **kwargs: "blocked build card")
    spec = SimpleNamespace(covered=lambda: [SimpleNamespace(probe="opamp_slew_rise")])

    written = card.write_deliverables(
        out_dir=tmp_path, part="LM358", subckt="LM358", spec=spec, report=None
    )

    assert not (tmp_path / "LM358.lib").exists()
    example = (tmp_path / "example.cir").read_text(encoding="utf-8")
    assert ".include LM358.lib" in example
    assert tmp_path / "MODEL_CARD.md" in written
