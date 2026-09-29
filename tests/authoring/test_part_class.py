"""The part-class refusal: which parts the analogue harness must not attempt.

Refusing is the honest option for a part whose datasheet rows no probe can
reach, but it is only honest while it stays *rare*, so these tests pin both
sides: the written families refuse with the harness's own reason, recognisably
analogue parts -- including lookalikes such as Maxim's ``MAX232`` and Torex's
``XC6206`` -- keep building, and a refused part never reaches an agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from boardmodeler.authoring.backends import ScriptedBackend
from boardmodeler.authoring.part_class import classify
from boardmodeler.pipeline import make_model as engine
from boardmodeler.pipeline.make_model import MakeModelRequest, MakeModelResult, make_model

# --------------------------------------------------------------------------- #
# (a) the part number decides


@pytest.mark.parametrize(
    ("part", "kind"),
    [
        ("STM32F407", "microcontroller"),
        ("STM32F407VGT6", "microcontroller"),
        ("ATmega328P", "microcontroller"),
        ("ATtiny85", "microcontroller"),
        ("PIC16F877A", "microcontroller"),
        ("MSP430G2553", "microcontroller"),
        ("nRF52840", "microcontroller"),
        ("ESP32", "microcontroller"),
        ("RP2040", "microcontroller"),
        ("XC7A35T", "fpga"),
        ("XC6SLX9", "fpga"),
        ("XCZU9EG", "fpga"),
        ("LFE5U-45F", "fpga"),
        ("ECP5", "fpga"),
        ("iCE40UP5K", "fpga"),
        ("LCMXO2-7000HC", "fpga"),
        ("Cyclone V", "fpga"),
        ("EP4CE6E22C8N", "fpga"),
        ("10M08SAU169C8G", "fpga"),
        ("5SGXEA7N2F45C2", "fpga"),
    ],
)
def test_written_families_are_refused_with_the_harnesss_own_reason(part: str, kind: str) -> None:
    classified = classify(part)

    assert classified.supported is False
    assert classified.kind == kind
    assert "no datasheet row this harness can bind" in classified.reason
    assert classified.detail == f"unsupported_part_class: {kind}: {classified.reason}"


@pytest.mark.parametrize(
    "part",
    [
        "TPS54320",
        "LM358",
        "TLV431",
        "INA293",
        # Lookalikes that a loose matcher would refuse: Maxim's MAX family (never
        # the bare token "MAX"), and Torex's XC62xx/XC95xx regulators (never the
        # bare "XC" that Xilinx part codes use).
        "MAX232",
        "MAX98357A",
        "XC6206P332MR",
        "XC9504",
        "XC9104",
        "LTC3891",
        "BQ24074",
    ],
)
def test_analogue_parts_and_lookalikes_stay_supported(part: str) -> None:
    classified = classify(part)

    assert classified.kind == "analogue_ic"
    assert classified.supported is True
    assert classified.reason.startswith("no microcontroller, FPGA, CPLD, SoC or processor-core")


# --------------------------------------------------------------------------- #
# (b) the document's own text


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("STM32F407 32-bit Microcontroller Datasheet", "microcontroller"),
        ("MICROCONTROLLERS SELECTION GUIDE", "microcontroller"),
        ("Cyclone V FPGA Device Handbook", "fpga"),
        ("CoolRunner-II CPLD Family Data Sheet", "fpga"),
        ("Zynq-7000 SoC Technical Reference Manual", "unsupported"),
        ("ARM Cortex-M processor core technical reference", "unsupported"),
    ],
)
def test_document_text_names_the_class(text: str, kind: str) -> None:
    classified = classify("CUSTOM-1", text=text)

    assert classified.kind == kind
    assert classified.supported is False
    assert "the document text describes" in classified.reason


@pytest.mark.parametrize(
    "text",
    [
        "TPS54320 4.5-V to 18-V Input, 3-A Synchronous Step-Down Converter",
        "Socionext assoc note with no class words",  # "soc" inside other words
        "",
    ],
)
def test_text_without_a_whole_digital_word_is_not_a_refusal(text: str) -> None:
    assert classify("CUSTOM-1", text=text).supported is True


# --------------------------------------------------------------------------- #
# (c) the pipeline stop: BLOCKED, no agent turn, nothing published


def _stand_in_datasheet(path: Path, *, title: str | None = None) -> Path:
    """A synthetic one-page PDF (no vendor data): enough to register a document."""
    sheet = canvas.Canvas(str(path))
    if title is not None:
        sheet.setTitle(title)
    sheet.drawString(72, 720, "Synthetic stand-in sheet (test fixture): not device data.")
    sheet.showPage()
    sheet.save()
    return path


def _counting_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedBackend, list[object]]:
    """A scripted backend that fatals if asked to author, plus a record of the ask."""

    def never_author(turn: int, workdir: Path, prompt: str) -> None:  # pragma: no cover
        raise AssertionError("a refused part must never reach the authoring loop")

    backend = ScriptedBackend(never_author)
    built: list[object] = []
    monkeypatch.setattr(engine, "build_backend", lambda request: built.append(request) or backend)
    return backend, built


def _run(
    part: str, datasheet: Path, tmp_path: Path
) -> tuple[MakeModelRequest, MakeModelResult, list[object]]:
    request = MakeModelRequest(
        part=part,
        subckt="REFUSED_PART",
        datasheet=datasheet,
        out_dir=tmp_path / "out",
        backend_name="scripted",
    )
    events: list[object] = []
    result = make_model(request, progress=events.append)
    return request, result, events


def test_a_refused_part_number_blocks_before_any_agent_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend, built = _counting_backend(monkeypatch)
    datasheet = _stand_in_datasheet(tmp_path / "sheet.pdf")
    request, result, events = _run("STM32F407", datasheet, tmp_path)

    assert result.status == "BLOCKED"
    assert result.detail.startswith("unsupported_part_class: microcontroller: ")
    assert "the probes measure analogue thresholds and regulation" in result.detail
    assert "a firmware-defined part has no datasheet row this harness can bind" in result.detail
    assert result.rows == ()
    assert (result.lib_path, result.asy_path, result.card_path) == (None, None, None)
    assert built == [] and backend.turns == 0
    assert ("read", "failed") in [(event.stage, event.status) for event in events]
    assert not [event for event in events if event.stage in ("author", "judge")]
    # No model, no spec and no agent sandbox: only the read stage's own registration.
    assert not (request.out_dir / "REFUSED_PART.lib").exists()
    assert not (request.out_dir / "MODEL_CARD.md").exists()
    assert not (request.out_dir / "spec").exists()
    assert not (request.out_dir / "build" / "model").exists()


def test_the_datasheets_own_title_refuses_an_unrecognised_part_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend, built = _counting_backend(monkeypatch)
    datasheet = _stand_in_datasheet(
        tmp_path / "sheet.pdf", title="ACME CUSTOM-1 Microcontroller Datasheet"
    )
    _, result, _ = _run("CUSTOM-1", datasheet, tmp_path)

    assert result.status == "BLOCKED"
    assert result.detail.startswith(
        "unsupported_part_class: microcontroller: the document text describes a microcontroller"
    )
    assert built == [] and backend.turns == 0


# --------------------------------------------------------------------------- #
# families added after the coverage scan of typed part numbers


@pytest.mark.parametrize(
    ("part", "kind"),
    [
        ("XC9572XL", "fpga"),
        ("XC95144XL", "fpga"),
        ("LFE3-35EA", "fpga"),
        ("LC4064V", "fpga"),
        ("ATF1502AS", "fpga"),
        ("GAL22V10", "fpga"),
        ("MPF300T", "fpga"),
        ("M2S025", "fpga"),
        ("BCM2711", "unsupported"),
        ("BCM2837", "unsupported"),
        ("RK3399", "unsupported"),
        ("Exynos 4412", "unsupported"),
        ("AM6254", "unsupported"),
        ("MK64FN1M0VLL12", "microcontroller"),
        ("MKL25Z128VLK4", "microcontroller"),
        ("S32K144", "microcontroller"),
        ("AT89C51", "microcontroller"),
        ("ATxmega128A1", "microcontroller"),
        ("CH32V003", "microcontroller"),
        ("STC89C52", "microcontroller"),
        ("NUC123", "microcontroller"),
        ("C8051F320", "microcontroller"),
    ],
)
def test_the_added_families_are_refused(part: str, kind: str) -> None:
    classified = classify(part)

    assert classified.kind == kind
    assert classified.supported is False


@pytest.mark.parametrize(
    "part",
    [
        # near misses of the prefixes above that are ordinary analogue or interface parts
        "MAX3232",
        "MAX3232E",
        "TDA2030",
        "TDA7293",
        "CH340G",
        "AM26LS31",
        "AM26C32",
        "XC9504",
        "XC9104",
        "XC6206P332MR",
        "BCM43438",
        "MK1584",
        "LC709203F",
        "GAL",
        "PMS",
        "TPS62130",
        "ATA5782",
    ],
)
def test_lookalike_analogue_parts_stay_supported_after_the_additions(part: str) -> None:
    assert classify(part).supported is True


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Field Programmable Gate Array Family Overview", "fpga"),
        ("Field-programmable gate arrays", "fpga"),
        ("Complex Programmable Logic Device datasheet", "fpga"),
        ("GAL16V8 Programmable Logic Device", "fpga"),
        ("One-chip Microcomputer", "microcontroller"),
        ("A System on a Chip for cameras", "unsupported"),
        ("Systems-on-chip overview", "unsupported"),
    ],
)
def test_more_wordings_of_the_digital_classes_are_recognised(text: str, kind: str) -> None:
    assert classify("XYZ-1", text=text).kind == kind


@pytest.mark.parametrize(
    "text",
    [
        "Microprocessor Supervisory Circuit",
        "Low-Dropout Regulator for powering processors",
        "Programmable Gain Amplifier",
        "Programmable Current Source",
    ],
)
def test_analogue_titles_that_mention_processors_or_programming_stay_supported(text: str) -> None:
    assert classify("XYZ-1", text=text).supported is True
