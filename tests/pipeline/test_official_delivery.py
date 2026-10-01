"""The default route preserves supplier bytes and cannot turn loading into accuracy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas

from boardmodeler.authoring import official_spice
from boardmodeler.pipeline import official_delivery
from boardmodeler.pipeline.make_model import MakeModelRequest, make_model

ORIGINAL = b"* Official test fixture\r\n.SUBCKT AD999 IN OUT GND\r\nR1 IN OUT 1k\r\nR2 OUT GND 1k\r\n.ENDS AD999\r\n"


@pytest.fixture
def build_request(tmp_path):
    pdf = tmp_path / "datasheet.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(40, 800, "AD999 operational amplifier - Analog Devices")
    canvas.save()
    return MakeModelRequest("AD999", "AD999_USER", pdf, tmp_path / "model")


@pytest.fixture
def official_route(monkeypatch):
    monkeypatch.setattr(official_delivery, "internet_allowed", lambda: True)
    monkeypatch.setattr(
        official_spice,
        "fetch_official_bytes",
        lambda *a, **k: (
            b'<a href="https://www.analog.com/models/AD999.lib">AD999 LTspice model</a>',
            "text/html",
        ),
    )
    monkeypatch.setattr(official_spice, "_download", lambda url, **k: (ORIGINAL, "text/plain", url))
    monkeypatch.setattr(
        official_delivery,
        "load_check",
        lambda *a, **k: {"status": "loaded", "electrical_accuracy_verified": False, "wall_s": 0.01},
    )
    monkeypatch.setattr(
        "boardmodeler.pipeline.make_model.build_backend",
        lambda *a, **k: pytest.fail("Official route must make zero AI calls"),
    )


def test_default_prefers_original_before_part_whitelist(build_request, official_route):
    result = make_model(build_request)
    assert result.status == "UNKNOWN"
    assert result.lib_path.read_bytes() == ORIGINAL
    assert result.lib_path.name == "AD999.lib"
    assert ".SUBCKT AD999" in result.lib_path.read_text()
    assert "SYMATTR Value2 AD999" in result.asy_path.read_text()
    assert result.counts["PASS"] == 0
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    assert receipt["model_sha256"] == hashlib.sha256(ORIGINAL).hexdigest()
    assert receipt["adaptation_applied"] is False
    assert receipt["electrical_accuracy_verified"] is False
    assert receipt["physical_package_pin_mapping"] == "not_verified"
    timing = json.loads((build_request.out_dir / "run-timing.json").read_text())
    assert timing["provider_calls"] == 0
    assert timing["route"] == "official_manufacturer_original"


def test_native_candidate_unverified_is_withheld_without_generation(
    build_request, official_route, monkeypatch
):
    monkeypatch.setattr(official_delivery, "load_check", lambda *a, **k: {"status": "inconclusive"})
    result = make_model(build_request)
    assert result.status == "BLOCKED"
    assert result.lib_path is None
    assert result.asy_path is None
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    assert receipt["status"] == "withheld"


def test_wrong_part_in_original_is_not_delivered(build_request, official_route, monkeypatch):
    monkeypatch.setattr(
        official_spice,
        "_download",
        lambda url, **k: (ORIGINAL.replace(b"AD999", b"AD123"), "text/plain", url),
    )
    result = make_model(build_request)
    assert result.status == "BLOCKED"
    assert result.lib_path is None
    assert "model_entry_ambiguous" in (build_request.out_dir / "official-model.json").read_text()


def test_mcu_refused_before_official_download(build_request, official_route, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        official_spice,
        "fetch_official_bytes",
        lambda *a, **k: pytest.fail("MCU must stop before download"),
    )
    result = make_model(replace(build_request, part="STM32F407", subckt="STM32F407"))
    assert result.status == "BLOCKED"
    assert result.lib_path is None


def test_offline_lookup_makes_no_fetch(build_request, monkeypatch):
    monkeypatch.setattr(official_delivery, "internet_allowed", lambda: False)
    monkeypatch.setattr(
        official_spice,
        "fetch_official_bytes",
        lambda *a, **k: pytest.fail("Offline must stop before download"),
    )
    result = make_model(build_request)
    assert result.lib_path is None
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    assert receipt["reason"] == "internet_access_off"


def test_ordering_suffix_does_not_choose_another_document_part():
    assert official_delivery.product_page(
        "UCC28251PW", "UCC28251 PWM controller", manufacturer_context="Texas Instruments UCC28251PW"
    ) == ("https://www.ti.com/product/UCC28251", ("ti.com",))
    assert official_delivery.product_page("AD999", "AD123 Analog Devices") is None


@pytest.mark.parametrize(
    "model_type,prefix,ports",
    [
        ("D", "D", ["A", "K"]),
        ("NPN", "Q", ["C", "B", "E"]),
        ("PMOS", "M", ["D", "G", "S", "B"]),
        ("VDMOS", "M", ["D", "G", "S"]),
    ],
)
def test_official_primitive_original_has_correct_symbol_interface(
    build_request, official_route, monkeypatch, model_type, prefix, ports
):
    data = f".MODEL AD999 {model_type}\r\n".encode()
    monkeypatch.setattr(official_spice, "_download", lambda url, **k: (data, "text/plain", url))
    result = make_model(build_request)
    assert result.status == "UNKNOWN"
    assert result.lib_path.read_bytes() == data
    assert f"SYMATTR Prefix {prefix}" in result.asy_path.read_text()
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    assert receipt["model_ports"] == ports
    assert receipt["element_prefix"] == prefix
    assert receipt["electrical_accuracy_verified"] is False


def test_similar_longer_part_name_is_not_accepted(build_request, official_route, monkeypatch):
    monkeypatch.setattr(
        official_spice,
        "_download",
        lambda url, **k: (ORIGINAL.replace(b"AD999", b"AD9999"), "text/plain", url),
    )
    result = make_model(build_request)
    assert result.status == "BLOCKED"
    assert result.lib_path is None


@pytest.mark.ltspice
@pytest.mark.parametrize("primitive", [False, True])
def test_original_really_loads_without_changing_bytes(
    build_request, official_route, monkeypatch, ltspice_exe, primitive
):
    from boardmodeler.authoring.sanity import load_check
    from boardmodeler.simulation.ltspice import LtspiceInstall

    monkeypatch.setattr(official_delivery, "load_check", load_check)
    data = b".MODEL AD999 D(IS=1e-14 N=1)\r\n" if primitive else ORIGINAL
    monkeypatch.setattr(official_spice, "_download", lambda url, **k: (data, "text/plain", url))
    monkeypatch.setattr(
        official_delivery, "locate", lambda: LtspiceInstall(Path(ltspice_exe), "test-input")
    )
    result = make_model(build_request)
    assert result.status == "UNKNOWN"
    assert result.lib_path.read_bytes() == data
    receipt = json.loads((build_request.out_dir / "official-model.json").read_text())
    assert receipt["load_check"]["status"] == "loaded"
    assert receipt["load_check"]["artifacts"]
    assert receipt["electrical_accuracy_verified"] is False
