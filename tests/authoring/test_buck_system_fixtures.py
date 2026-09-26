"""M2 system benches need verified citations and measurable waveforms."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from boardmodeler.authoring.buck_system_fixtures import (
    BuckBenchParts,
    build_buck_system_benches,
    evaluate_waveform,
    render_deck,
)
from boardmodeler.authoring.spec import Characteristic, SpecSet
from boardmodeler.simulation.raw import RawFile


def _source(tmp_path: Path, *, unverified: str | None = None) -> tuple[SpecSet, Path]:
    entries = (
        ("vref", "Voltage reference", "V", 0.772, 0.8, 0.828, 5),
        ("fsw", "Switching frequency", "Hz", 8e5, 1e6, 1.2e6, 5),
        ("ilim", "Current limit threshold", "A", 4.2, None, 6.5, 5),
        ("gmcs", "Switch current to COMP transconductance", "A/V", None, 12.0, None, 5),
        ("veco", "COMP falls in Eco-mode at 0.5 V", "V", None, 0.5, None, 12),
        ("iss", "Soft-start charge current", "A", None, 2e-6, None, 5),
    )
    characteristics = []
    requirements = []
    for identifier, statement, unit, minimum, typical, maximum, page in entries:
        excerpt = f"{statement}: cited source row"
        characteristics.append(
            Characteristic(
                char_id=identifier,
                statement=statement,
                unit=unit,
                min_value=minimum,
                typ_value=typical,
                max_value=maximum,
                target=typical,
                source_page=page,
                excerpt=excerpt,
                req_class="ELECTRICAL",
                probe=None,
                probe_params={},
                not_testable_reason="measured by system bench",
            )
        )
        requirements.append(
            {
                "req_id": identifier,
                "citation_verified": identifier != unverified,
                "limits": {"unit": unit, "min": minimum, "typ": typical, "max": maximum},
                "evidence": [
                    {"doc_id": "fixture-doc", "excerpt": excerpt, "page": {"pdf_page": page}}
                ],
            }
        )
    pins = tuple(
        {
            "name": name,
            "physical_pin": str(index),
            "direction": "ground" if name in {"GND", "PowerPAD"} else "input",
        }
        for index, name in enumerate(
            ("BOOT", "VIN", "EN", "SS", "VSENSE", "COMP", "GND", "PH", "PowerPAD"),
            1,
        )
    )
    spec = SpecSet("TPS54332DDA", "TPS54332DDA", "fixture-doc", tuple(characteristics), pins)
    source = tmp_path / "requirements.json"
    source.write_text(
        json.dumps({"document": {"doc_id": "fixture-doc"}, "requirements": requirements}),
        encoding="utf-8",
    )
    return spec, source


def _model(tmp_path: Path) -> Path:
    model = tmp_path / "candidate.lib"
    model.write_text(
        ".subckt TPS54332DDA BOOT VIN EN SS VSENSE COMP GND PH POWERPAD\n"
        ".param ILIM=5.35\n.ends TPS54332DDA\n",
        encoding="utf-8",
    )
    return model


def _raw(variables: tuple[str, ...], columns: tuple[np.ndarray, ...]) -> RawFile:
    return RawFile(
        path=None,
        plotname="Transient Analysis",
        flags=["real"],
        variables=list(variables),
        data=np.column_stack(columns),
        variable_types=["time"] + ["voltage"] * (len(variables) - 1),
    )


def test_seven_decks_are_cited_reproducible_and_use_resistive_faults(tmp_path: Path) -> None:
    spec, source = _source(tmp_path)
    benches = build_buck_system_benches(spec, requirements_path=source)
    assert tuple(bench.kind for bench in benches) == (
        "gain",
        "limit_min",
        "limit_max",
        "ripple",
        "edge",
        "load_step",
        "startup",
    )
    assert all(bench.ports[-1] == "POWERPAD" and bench.nodes[-1] == "0" for bench in benches)
    assert all(
        bench.source_rows and all(row.excerpt and row.source_page >= 0 for row in bench.source_rows)
        for bench in benches
    )
    gain = benches[0]
    assert len(gain.gain_points) >= 3
    assert all(point.comp_v > 0.5 for point in gain.gain_points)
    assert gain.gain_points[0].early_window_s[0] > 15e-9 * 0.828 / 2e-6
    assert gain.metadata["ss_timing_vref_v"] == 0.828
    model = _model(tmp_path)
    decks = {bench.kind: render_deck(bench, model) for bench in benches}
    assert decks["gain"] == render_deck(gain, model)
    assert "XU1 boot vin en ss vsense comp 0 ph 0 TPS54332DDA ILIM=4.2" in decks["limit_min"]
    assert "XU1 boot vin en ss vsense comp 0 ph 0 TPS54332DDA ILIM=6.5" in decks["limit_max"]
    for kind in ("limit_min", "limit_max", "load_step"):
        assert (
            "Rfault out" in decks[kind] if kind.startswith("limit") else "Rstep out" in decks[kind]
        )
        assert "Sfault" in decks[kind] if kind.startswith("limit") else "Sstep" in decks[kind]
        assert "Iload" not in decks[kind]
    assert ".temp 25" in decks["ripple"]
    assert ".meas tran ph_rise" in decks["edge"]
    assert ".meas tran start_t90" in decks["startup"]


def test_unverified_or_changed_source_row_is_rejected(tmp_path: Path) -> None:
    spec, source = _source(tmp_path, unverified="veco")
    with pytest.raises(ValueError, match="citation-checked"):
        build_buck_system_benches(spec, requirements_path=source)
    spec, source = _source(tmp_path)
    record = json.loads(source.read_text(encoding="utf-8"))
    record["requirements"][2]["limits"]["max"] = 9.0
    source.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="source and spec max differ"):
        build_buck_system_benches(spec, requirements_path=source)
    spec, source = _source(tmp_path)
    record = json.loads(source.read_text(encoding="utf-8"))
    duplicate = dict(record["requirements"][0], citation_verified=False)
    record["requirements"].append(duplicate)
    source.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate requirement vref"):
        build_buck_system_benches(spec, requirements_path=source)


def test_deck_rejects_wrong_port_order_and_comment_injection(tmp_path: Path) -> None:
    spec, source = _source(tmp_path)
    with pytest.raises(ValueError, match="named source"):
        BuckBenchParts(source="safe\n.tran 1")
    bench = build_buck_system_benches(spec, requirements_path=source)[1]
    model = _model(tmp_path)
    model.write_text(
        model.read_text(encoding="utf-8").replace("BOOT VIN", "VIN BOOT"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="physical_pin_contract"):
        render_deck(bench, model)
    model.write_text(
        model.read_text(encoding="utf-8")
        .replace(".param ILIM=5.35", ".param OTHER=5.35")
        .replace("VIN BOOT", "BOOT VIN"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="local ILIM"):
        render_deck(bench, model)


def test_gain_measurement_selects_only_stable_linear_region(tmp_path: Path) -> None:
    spec, source = _source(tmp_path)
    gain = build_buck_system_benches(spec, requirements_path=source)[0]
    time = np.arange(0, gain.stop_s + 2e-7, 1e-7)
    ph = np.where(np.mod(time, 1e-6) < 4e-7, 12.0, 0.0)
    current = np.ones_like(time)
    for point in gain.gain_points:
        start = point.early_window_s[0] - 150e-6
        stop = point.late_window_s[1] + 5e-6
        level = 1.0 if point.comp_v <= 0.6 else min(2.0 + 12.0 * (point.comp_v - 0.65), 6.2)
        current[(time >= start) & (time < stop)] = level
    raw = _raw(("time", "V(ph)", "I(Lout)"), (time, ph, current))
    result = evaluate_waveform(gain, raw)
    assert result.status == "MEASURED"
    assert len(result.metrics["points"]) == len(gain.gain_points)
    fit = result.metrics["fit"]
    assert fit["selected_comp_v"][0] >= 0.65
    assert fit["fit_points"] >= 3
    assert fit["slope_a_per_v"] == pytest.approx(12.0, abs=0.01)


def test_unresolved_ph_edges_are_unknown(tmp_path: Path) -> None:
    spec, source = _source(tmp_path)
    edge = build_buck_system_benches(spec, requirements_path=source)[4]
    time = np.arange(0, edge.stop_s + 2e-8, 2e-8)
    ph = np.where(np.mod(time, 1e-6) < 4e-7, 12.0, 0.0)
    raw = _raw(("time", "V(ph)"), (time, ph))
    result = evaluate_waveform(edge, raw)
    assert result.status == "UNKNOWN"
    assert result.metrics["rise"]["resolved_transitions"] == 0
    assert result.metrics["fall"]["resolved_transitions"] == 0


def test_startup_observation_has_the_frequency_needed_for_tail_analysis(tmp_path: Path) -> None:
    spec, source = _source(tmp_path)
    startup = build_buck_system_benches(spec, requirements_path=source)[-1]
    time = np.arange(0, startup.stop_s + 1e-6, 1e-6)
    out = startup.metadata["vout_target_v"] * np.clip(time / 0.006, 0, 1)
    raw = _raw(("time", "V(out)"), (time, out))
    result = evaluate_waveform(startup, raw)
    assert result.status == "MEASURED"
    assert result.metrics["rise_10_90_s"] == pytest.approx(0.0048, abs=1e-6)
    assert result.metrics["ringing_status"].startswith("UNKNOWN")
