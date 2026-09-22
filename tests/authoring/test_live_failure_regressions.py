from __future__ import annotations

from pathlib import Path


def test_unknown_simulation_feedback_survives_without_becoming_cached_pass(tmp_path):
    from boardmodeler.authoring.harness import HarnessReport, ProbeOutcome
    from boardmodeler.authoring.spec import SpecSet
    from boardmodeler.authoring.validation_cache import read_feedback, write_report
    from boardmodeler.domain.hashing import sha256_file

    model = tmp_path / "model.lib"
    model.write_text("* original candidate")
    spec = SpecSet(part="TEST_FIXTURE", subckt="CELL", doc_id="synthetic", characteristics=())
    report = HarnessReport(
        part=spec.part,
        spec_digest=spec.digest(),
        model_sha256=sha256_file(model),
        outcomes=(
            ProbeOutcome(
                probe_id="example",
                status="UNKNOWN",
                measured={},
                detail="",
                char_ids=("example",),
                run_dir=str(tmp_path),
                unknown_reason="run_timeout",
            ),
        ),
    )
    write_report(tmp_path, "test-key", report)
    assert read_feedback(tmp_path, "test-key", spec, model) == report
    assert not (tmp_path / "test-key" / "report.json").exists()
    model.write_text("* changed candidate")
    assert read_feedback(tmp_path, "test-key", spec, model) is None


def test_subcircuit_end_repair_is_narrow_and_duplicate_names_are_rejected(tmp_path):
    import pytest

    from boardmodeler.authoring.model_syntax import normalize_library_end, validate_library
    from boardmodeler.authoring.probes import ProbeError

    model = tmp_path / "model.lib"
    original = ".subckt CELL A GND\nR1 A GND 1000\n.end\n"
    model.write_text(original)
    assert normalize_library_end(model, tmp_path / "originals")
    assert model.read_text().endswith(".ends CELL\n")
    assert next((tmp_path / "originals").glob("*.lib")).read_text() == original
    validate_library(model)
    model.write_text(".subckt CELL A GND\nR1 A GND 1000\nr1 A GND 2000\n.ends CELL\n")
    with pytest.raises(ProbeError, match="duplicate component"):
        validate_library(model)
    model.write_text("V1 a 0 1\nR1 a 0 1000\n.end\n")
    assert not normalize_library_end(model, tmp_path / "originals")


import json

import pytest

from boardmodeler.authoring import api_backend
from boardmodeler.providers.base import ProviderError


def stream(*events, done=True):
    return "\n\n".join("data: " + json.dumps(e) for e in events) + (
        "\n\ndata: [DONE]\n" if done else ""
    )


@pytest.mark.skipif(
    not hasattr(api_backend, "_decoded_chat_stream"),
    reason="this edition does not use HTTP streaming",
)
def test_stream_keeps_answer_and_usage_but_not_reasoning():
    body = stream(
        {"choices": [{"index": 0, "delta": {"reasoning_content": "PRIVATE"}}]},
        {"choices": [{"index": 0, "delta": {"content": '{"ok":'}}]},
        {"choices": [{"index": 0, "delta": {"content": "true}"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 9}},
    )
    result = api_backend._decoded_chat_stream(body, secrets=[])
    assert result["choices"][0]["message"]["content"] == '{"ok":true}'
    assert result["usage"]["completion_tokens"] == 9
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("done,finish", [(False, "stop"), (True, None)])
@pytest.mark.skipif(
    not hasattr(api_backend, "_decoded_chat_stream"),
    reason="this edition does not use HTTP streaming",
)
def test_complete_looking_json_in_an_interrupted_stream_is_not_accepted(done, finish):
    body = stream(
        {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": finish}]}, done=done
    )
    with pytest.raises(ProviderError, match="stream_incomplete"):
        api_backend._decoded_chat_stream(body, secrets=[])


@pytest.mark.skipif(
    not hasattr(api_backend, "_decoded_chat_stream"),
    reason="this edition does not use HTTP streaming",
)
def test_stream_error_does_not_echo_a_credential():
    with pytest.raises(ProviderError) as caught:
        api_backend._decoded_chat_stream(
            stream({"error": {"message": "secret-token rejected"}}), secrets=["secret-token"]
        )
    assert "secret-token" not in str(caught.value)


def test_active_stream_cannot_reset_the_total_response_deadline(monkeypatch):
    from boardmodeler.providers import http_inference

    clock = [0]
    monkeypatch.setattr(http_inference.time, "monotonic", lambda: clock[0])

    class EndlessResponse:
        def read1(self, size):
            clock[0] += 1
            return b"data: keepalive\n"

    with pytest.raises(TimeoutError, match="total time budget"):
        http_inference._read_response_body(EndlessResponse(), 3)
    assert clock[0] == 3


@pytest.mark.ltspice
def test_ground_reference_normalization_preserves_zero_ground_and_floats(
    tmp_path: Path, ltspice_exe: Path
):
    from boardmodeler.authoring.model_reference import normalize_ground_reference
    from boardmodeler.simulation.ltspice import run_batch
    from boardmodeler.simulation.raw import read_raw

    model = tmp_path / "fixture.lib"
    model.write_text(
        "* TEST_FIXTURE with the observed global-reference mistake\n.subckt DUT A Y VCC GND\nBdriver Y GND I=(V(Y)-V(VCC)*(V(A)>0.5*V(VCC)))/10\n.ends DUT\n"
    )
    assert normalize_ground_reference(
        model, [{"name": "GND", "function": "Device GND"}], tmp_path / "original"
    )
    assert not normalize_ground_reference(
        model, [{"name": "GND", "function": "Device GND"}], tmp_path / "original"
    )
    for offset in (0, 2):
        for level in (0, 1):
            folder = tmp_path / f"{offset}-{level}"
            folder.mkdir()
            deck = folder / "test.cir"
            deck.write_text(
                f'* TEST_FIXTURE\n.include "{model.as_posix()}"\nVg g 0 {offset}\nVcc vcc g 3.3\nVa a g {3.3 * level}\nRload y g 100000\nXdut a y vcc g DUT\n.options plotwinsize=0 numdgt=15\n.tran 0 1m 0 1u\n.save V(y)\n.end\n'
            )
            run = run_batch(ltspice_exe, deck, folder, timeout_s=30)
            assert run.raw_path
            value = read_raw(run.raw_path).column("V(y)")[-1] - offset
            assert value == pytest.approx(3.3 * level, abs=0.001)


@pytest.mark.ltspice
def test_long_windows_evidence_path_launches_simulator(tmp_path: Path, ltspice_exe: Path):
    import os

    from boardmodeler.simulation.ltspice import _native_path, run_batch

    if os.name != "nt":
        pytest.skip("Windows LTspice required")
    folder = tmp_path / ("a" * 90) / ("b" * 90)
    folder.mkdir(parents=True)
    deck = folder / "long-evidence-name.cir"
    deck.write_text("* TEST_FIXTURE\nV1 out 0 1\nR1 out 0 1k\n.tran 0 1m\n.save V(out)\n.end\n")
    if len(str(deck)) < 240:
        pytest.skip("temporary root did not create a long path")
    try:
        _native_path(deck)
    except OSError:
        pytest.skip("NTFS short names disabled on this volume")
    result = run_batch(ltspice_exe, deck, folder, timeout_s=30)
    assert result.raw_path and result.raw_path.stat().st_size > 0
