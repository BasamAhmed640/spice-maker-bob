"""Readable symbols and actual LTspice node order, independent of agent drawings."""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest

from boardmodeler.models.symbolism import symbol_pin_orders, symbol_text, validate_symbol
from boardmodeler.pipeline.make_model import MakeModelRequest, _Run, _StageLog
from boardmodeler.simulation.ltspice import netlist_step

LM358_PORTS = ("VCC", "OUT1", "IN1M", "IN1P", "VEE", "IN2P", "IN2M", "OUT2")


@pytest.mark.parametrize(
    "ports",
    [
        LM358_PORTS,
        ("IN",),
        tuple(f"INPUT_{i}" for i in range(32)) + tuple(f"OUTPUT_{i}" for i in range(32)),
        ("VERY_LONG_INPUT_SIGNAL_NAME", "VERY_LONG_OUTPUT_SIGNAL_NAME", "VCC", "GND"),
    ],
)
def test_pins_leads_and_labels_fit_the_box(ports: tuple[str, ...]) -> None:
    text = symbol_text("PART", ports, model_file="PART.lib")
    rectangle = re.search(r"RECTANGLE Normal (-?\d+) (-?\d+) (-?\d+) (-?\d+)", text)
    assert rectangle
    left, top, right, bottom = map(int, rectangle.groups())
    assert all(value % 16 == 0 for value in (left, top, right, bottom))
    rows: dict[int, list[tuple[int, int]]] = {}
    seen = set()
    for x, y, side, offset, name in re.findall(
        r"PIN (-?\d+) (-?\d+) (LEFT|RIGHT) (\d+)\nPINATTR PinName (\S+)", text
    ):
        x, y, offset = int(x), int(y), int(offset)
        assert x % 16 == y % 16 == 0
        assert top + 24 <= y <= bottom - 24
        assert (x, y) not in seen
        seen.add((x, y))
        edge = left if side == "LEFT" else right
        assert f"LINE Normal {x} {y} {edge} {y}" in text
        assert abs(x - edge) >= 16
        start = x + offset if side == "LEFT" else x - offset - 16 * len(name)
        end = start + 16 * len(name)
        assert left + 8 <= start < end <= right - 8
        rows.setdefault(y, []).append((start, end))
    assert len(seen) == len(ports)
    for spans in rows.values():
        spans.sort()
        assert all(a[1] + 16 <= b[0] for a, b in pairwise(spans))
    positions = sorted(rows)
    assert all(b - a >= 32 for a, b in pairwise(positions))
    windows = dict(re.findall(r"WINDOW ([03]) 0 (-?\d+) Center 2", text))
    assert int(windows["0"]) <= top - 24
    assert int(windows["3"]) >= bottom + 24


def test_permuted_orders_are_not_electrically_equivalent() -> None:
    text = symbol_text("LM358", LM358_PORTS, model_file="LM358.lib")
    bad = text.replace("SpiceOrder 1\n", "SpiceOrder 999\n")
    bad = bad.replace("SpiceOrder 2\n", "SpiceOrder 1\n")
    bad = bad.replace("SpiceOrder 999\n", "SpiceOrder 2\n")
    assert any(f.code.startswith("SYM004") for f in validate_symbol(bad, ports=LM358_PORTS))


def test_publisher_replaces_even_electrically_valid_agent_geometry(tmp_path: Path) -> None:
    request = MakeModelRequest(
        part="LM358", subckt="LM358", datasheet=tmp_path / "p.pdf", out_dir=tmp_path
    )
    run = _Run(request, _StageLog(None))
    run.pin_map = ({"name": "IN1M", "direction": "output"},)
    source = run.workdir / "model" / "LM358.asy"
    source.parent.mkdir(parents=True)
    agent_text = symbol_text("LM358", LM358_PORTS, model_file="LM358.lib")
    agent_text = re.sub(r"RECTANGLE Normal .*", "RECTANGLE Normal -1 -1 1 1", agent_text)
    source.write_text(agent_text, encoding="utf-8")
    target, note = run._publish_symbol(LM358_PORTS, "LM358.lib")
    published = target.read_text(encoding="utf-8")
    assert "locally" in note
    assert published != agent_text
    assert published == symbol_text(
        "LM358",
        LM358_PORTS,
        model_file="LM358.lib",
        model_name="LM358",
        description="LM358 authored model",
        directions={"IN1M": "output"},
    )
    assert symbol_pin_orders(published) == list(zip(LM358_PORTS, range(1, 9), strict=True))


@pytest.mark.ltspice
def test_real_ltspice_netlist_keeps_the_models_port_order(
    tmp_path: Path, ltspice_exe: Path
) -> None:
    text = symbol_text("LM358", LM358_PORTS, model_file="LM358.lib")
    (tmp_path / "LM358.asy").write_text(text, encoding="utf-8")
    # A connectivity fixture, not a claim about a real LM358's electrical behavior.
    (tmp_path / "LM358.lib").write_text(
        ".subckt LM358 " + " ".join(LM358_PORTS) + "\nR1 VCC VEE 1G\n.ends LM358\n",
        encoding="utf-8",
    )
    lines = ["Version 4", "SHEET 1 880 680"]
    for x, y, name in re.findall(r"PIN (-?\d+) (-?\d+) [A-Z]+ \d+\nPINATTR PinName (\S+)", text):
        lines.append(f"FLAG {int(x) + 400} {int(y) + 320} N_{name}")
    lines += ["SYMBOL LM358 400 320 R0", "SYMATTR InstName U1", "TEXT 80 560 Left 2 !.op"]
    schematic = tmp_path / "connectivity.asc"
    schematic.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = netlist_step(ltspice_exe, schematic, timeout_s=20)
    assert not result.timed_out, result
    assert result.net_path is not None, result
    net = result.net_path.read_text(encoding="utf-8", errors="replace")
    instance = next(
        line.split(";", 1)[0].split() for line in net.splitlines() if line.startswith("X")
    )
    assert instance[1:] == [*(f"N_{name}" for name in LM358_PORTS), "LM358"]
