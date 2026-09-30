"""Impossible source/package builds must not consume provider minutes."""

from __future__ import annotations

from reportlab.pdfgen import canvas

from boardmodeler.pipeline import make_model as engine


def test_unreviewed_source_stops_before_extraction_or_planning(tmp_path, monkeypatch):
    source = tmp_path / "ordinary.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 700, "TEST_FIXTURE: fictional PWM controller. No real device evidence.")
    pdf.save()

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported new build contacted an extraction or author provider")

    monkeypatch.setattr(engine._Run, "extract", forbidden)
    monkeypatch.setattr(engine, "build_backend", forbidden)
    request = engine.MakeModelRequest(
        "UNREVIEWED_PWM",
        "UNREVIEWED_PWM",
        source,
        tmp_path / "out",
        family="switching_regulator",
        plan_tests=True,
    )
    result = engine.make_model(request)
    assert result.status == "BLOCKED"
    assert "extraction was not sent to AI" in result.detail
    assert result.lib_path is None
    assert not any(event.stage == "extract" for event in result.stages)


def test_ucc_requires_package_before_extraction(tmp_path, monkeypatch):
    source = tmp_path / "ucc.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(20, 700, "TEST_FIXTURE: UCC28251 Advanced PWM controller")
    pdf.save()

    def forbidden(*args, **kwargs):
        raise AssertionError("package choice consumed extraction work")

    monkeypatch.setattr(engine._Run, "extract", forbidden)
    result = engine.make_model(
        engine.MakeModelRequest("UCC28251", "UCC28251", source, tmp_path / "out")
    )
    assert result.status == "BLOCKED"
    assert "package_required" in result.detail
    assert "UCC28251PW" in result.detail and "UCC28251RGP" in result.detail
