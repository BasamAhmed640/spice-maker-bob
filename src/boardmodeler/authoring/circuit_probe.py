"""Frozen, declarative test circuits for devices outside the fixed probe catalog.

The planner supplies fixture data, not executable analysis code. The model
author cannot change the circuit, measurements, or limits after they are frozen.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

_NODE = r"[A-Za-z0-9_]+"
_SIGNAL = re.compile(rf"(?i)^(?:V\({_NODE}(?:,{_NODE})?\)|I\([A-Za-z][A-Za-z0-9_]*\))$")


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "mean",
        "min",
        "max",
        "peak_to_peak",
        "rms",
        "crossing_value",
        "delay",
        "slew",
        "gain",
        "unity_frequency",
        "frequency",
        "hysteresis",
        "rail_headroom",
    ]
    signal: str
    reference: str | None = None
    additional_signals: list[str] = Field(default_factory=list)
    second_start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    second_end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    trigger: str | None = None
    trigger_level: float | None = Field(default=None, allow_inf_nan=False)
    level: float | None = Field(default=None, allow_inf_nan=False)
    rising: bool = True
    trigger_rising: bool = True
    start: float = Field(default=0, ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    absolute: bool = False
    scale: float = Field(default=1, allow_inf_nan=False)
    offset: float = Field(default=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid(self):
        if self.start >= self.end:
            raise ValueError("measurement window must have positive width")
        for signal in (self.signal, self.reference, self.trigger, *self.additional_signals):
            if signal is not None and not _SIGNAL.fullmatch(signal):
                raise ValueError(f"invalid measurement signal: {signal}")
        if self.operation in ("delay", "crossing_value", "hysteresis") and self.trigger is None:
            raise ValueError("this measurement needs a trigger signal")
        if (
            self.operation in ("delay", "crossing_value", "hysteresis", "slew")
            and self.trigger_level is None
        ):
            raise ValueError("this measurement needs an explicit trigger_level")
        if self.operation in ("delay", "slew", "frequency") and self.level is None:
            raise ValueError(
                "this measurement needs an explicit output level; zero is not a default"
            )
        if self.operation in ("gain", "unity_frequency") and self.reference is None:
            raise ValueError("gain measurement needs an observed input reference")
        if self.scale == 0:
            raise ValueError("a zero scale would erase the measured device behavior")
        if self.operation == "rail_headroom" and (
            self.reference is None
            or self.second_start is None
            or self.second_end is None
            or not self.end < self.second_start < self.second_end
        ):
            raise ValueError(
                "rail headroom needs a supply reference and two separate settled windows"
            )
        return self


class CircuitRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str
    unit: str
    terminals: dict[str, str]
    components: list[str] = Field(min_length=1, max_length=100)
    analysis: Literal["tran", "ac"] = "tran"
    stop: float = Field(gt=0, le=10, default=0.001)
    step: float = Field(gt=0, default=1e-6)
    frequency_start: float = Field(gt=0, default=1)
    frequency_end: float = Field(gt=0, le=1e12, default=1e8)
    points_per_decade: int = Field(ge=10, le=200, default=60)
    temperature: float = 25
    measurement: Measurement
    operating_point: dict[str, float] = Field(default_factory=dict)
    condition_evidence: str

    @model_validator(mode="before")
    @classmethod
    def resolve_pin_measurements(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("measurement"), dict):
            return value
        # LTspice treats the top-level node name GND as node 0. A planner's
        # floating ground-current sense node must keep its distinct identity.
        if any(re.search(r"\bgnd\b", line, re.I) for line in value.get("components", [])):
            reserved = "bm_fixture_ground"
            if not any(reserved in line.lower() for line in value.get("components", [])):

                def rename(text):
                    return re.sub(r"\bgnd\b", reserved, text, flags=re.I)

                measurement = dict(value["measurement"])
                for key in ("signal", "reference", "trigger"):
                    if isinstance(measurement.get(key), str):
                        measurement[key] = rename(measurement[key])
                measurement["additional_signals"] = [
                    rename(s) for s in measurement.get("additional_signals", [])
                ]
                value = {
                    **value,
                    "components": [rename(line) for line in value["components"]],
                    "terminals": {p: rename(n) for p, n in value.get("terminals", {}).items()},
                    "measurement": measurement,
                }
        terminals = value.get("terminals", {})
        known = {"0", *(str(node).casefold() for node in terminals.values())}
        for line in value.get("components", []):
            words = line.split()
            if not words:
                continue
            count = 4 if words[0][0].upper() in "EG" else 2
            if len(words) > 3 and re.match(r"(?i)(value|table|poly|laplace)", words[3]):
                count = 2
            known.update(node.casefold() for node in words[1 : count + 1])
        aliases = {str(pin).casefold(): node for pin, node in terminals.items()}

        def resolve(signal):
            if not isinstance(signal, str) or not signal.upper().startswith("V("):
                return signal
            nodes = signal[2:-1].split(",")
            nodes = [
                aliases.get(n.casefold(), n) if n.casefold() not in known else n for n in nodes
            ]
            if any(node.casefold() not in known for node in nodes):
                raise ValueError(f"measurement references an unconnected node: {signal}")
            return "V(" + ",".join(nodes) + ")"

        measurement = dict(value["measurement"])
        for field in ("signal", "reference", "trigger"):
            if field in measurement:
                measurement[field] = resolve(measurement[field])
        if "additional_signals" in measurement:
            measurement["additional_signals"] = [
                resolve(s) for s in measurement["additional_signals"]
            ]
        return {**value, "measurement": measurement}

    @model_validator(mode="after")
    def valid(self):
        if not self.terminals or any(
            not re.fullmatch(_NODE, node) for node in self.terminals.values()
        ):
            raise ValueError("every physical terminal needs a named circuit node")
        if self.measurement.signal.upper() == "V(0)":
            raise ValueError("fixture measures forced ground, not a DUT response")
        if self.temperature != 25:
            raise ValueError("temperature dependence is not yet verified; use nominal 25 C")
        if not 1 <= self.stop / self.step <= 1_000_000:
            raise ValueError("transient needs 1 to 1,000,000 maximum-step intervals")
        if self.frequency_start >= self.frequency_end:
            raise ValueError("invalid AC sweep")
        if self.analysis == "tran" and self.measurement.end > self.stop:
            raise ValueError("measurement extends beyond the simulated window")
        if self.measurement.second_end is not None and self.measurement.second_end > self.stop:
            raise ValueError("second measurement extends beyond the simulated window")
        if self.analysis == "ac" and self.measurement.operation not in ("gain", "unity_frequency"):
            raise ValueError("AC recipes require a gain measurement")
        if self.measurement.operation == "frequency" and self.unit != "Hz":
            raise ValueError("frequency measurement must use Hz")
        if (
            self.measurement.operation == "delay"
            and self.unit == "Hz"
            and not (
                self.measurement.signal.casefold() == self.measurement.trigger.casefold()
                and self.measurement.level == self.measurement.trigger_level
                and self.measurement.rising == self.measurement.trigger_rising
            )
        ):
            raise ValueError("Hz cannot be measured by a delay between different events")
        for line in self.components:
            # Only local primitive circuit elements. No .include, .lib, .control,
            # X instances, continuations, semicolon commands or newlines.
            if (
                len(line) > 1000
                or any(ch in line for ch in '\r\n;"\\')
                or not re.match(r"^[RCLVIBEGFHD][A-Za-z0-9_]*\s+", line, re.I)
            ):
                raise ValueError(f"unsupported fixture component: {line[:80]}")
            if line[0].upper() == "D" and (
                len(line.split()) != 4 or line.split()[3].upper() != "BM_CATCH"
            ):
                raise ValueError("fixture diode must use the fixed BM_CATCH model")
        if not self.condition_evidence.strip():
            raise ValueError("the test operating point needs an evidence explanation")
        return self


def _trace(raw, signal):
    if signal.upper().startswith("V(") and "," in signal:
        a, b = signal[2:-1].split(",")
        return _trace(raw, f"V({a})") - _trace(raw, f"V({b})")
    if signal.upper() == "V(0)":
        return np.zeros(len(raw.data))
    return raw.column(signal)


def _saved(signal):
    if signal.upper().startswith("V(") and "," in signal:
        return [f"V({name})" for name in signal[2:-1].split(",") if name != "0"]
    return [] if signal.upper() == "V(0)" else [signal]


def _cross(t, y, level, rising):
    from boardmodeler.authoring.probes import ProbeError

    changes = (
        ((y[:-1] < level) & (y[1:] >= level)) if rising else ((y[:-1] > level) & (y[1:] <= level))
    )
    indices = np.flatnonzero(changes)
    if not len(indices):
        raise ProbeError("recipe_crossing_missing", "the requested device transition did not occur")
    i = int(indices[0])
    return float(t[i] + (level - y[i]) * (t[i + 1] - t[i]) / (y[i + 1] - y[i]))


def _frequency(t, y, level, rising):
    from boardmodeler.authoring.probes import ProbeError

    changes = (
        ((y[:-1] < level) & (y[1:] >= level)) if rising else ((y[:-1] > level) & (y[1:] <= level))
    )
    indices = np.flatnonzero(changes)
    if len(indices) < 2:
        raise ProbeError("recipe_frequency_edges_missing", "two device edges are required")
    edge_times = t[indices] + (level - y[indices]) * (
        (t[indices + 1] - t[indices]) / (y[indices + 1] - y[indices])
    )
    periods = np.diff(edge_times)
    middle = float(np.median(periods))
    if middle <= 0 or np.any(np.abs(periods - middle) > 0.1 * middle):
        raise ProbeError("recipe_frequency_unstable", "device edges do not form a stable period")
    return 1.0 / middle


def make_probe(payload):
    from boardmodeler.authoring.probes import ProbeError, ProbeSpec, model_ports
    from boardmodeler.simulation.raw import read_raw

    recipe = CircuitRecipe.model_validate(payload)
    m = recipe.measurement

    def render(spec, params, model: Path, subckt):
        ports = model_ports(model, subckt)
        if set(p.upper() for p in ports) != set(recipe.terminals):
            raise ProbeError(
                "physical_pin_contract", f"model {ports}; fixture {list(recipe.terminals)}"
            )
        save = sorted(
            {
                s
                for x in (m.signal, m.reference, m.trigger, *m.additional_signals)
                if x
                for s in _saved(x)
            }
        )
        analysis = (
            f".tran 0 {recipe.stop:g} 0 {recipe.step:g}"
            if recipe.analysis == "tran"
            else f".ac dec {recipe.points_per_decade} {recipe.frequency_start:g} {recipe.frequency_end:g}"
        )
        return "\n".join(
            [
                f"* Frozen fixture: {recipe.purpose}",
                f'.include "{model.resolve().as_posix()}"',
                *recipe.components,
                *(
                    [".model BM_CATCH D(Is=1u N=1.05 Rs=0.05)"]
                    if any(line[0].upper() == "D" for line in recipe.components)
                    else []
                ),
                f"Xdut {' '.join(recipe.terminals[p.upper()] for p in ports)} {subckt}",
                ".temp 25",
                ".options plotwinsize=0 numdgt=15",
                analysis,
                ".save " + " ".join(save),
                ".end",
                "",
            ]
        )

    def measure(path, params):
        try:
            raw = read_raw(path)
            t = raw.data[:, 0].real
            if len(t) < 3 or np.any(np.diff(t) < 0):
                raise ProbeError("recipe_axis_invalid")
            required_end = recipe.stop if recipe.analysis == "tran" else recipe.frequency_end
            if t[-1] < required_end * 0.999:
                raise ProbeError("recipe_run_truncated")
            mask = (t >= m.start) & (t <= m.end)
            if np.count_nonzero(mask) < 2:
                raise ProbeError("recipe_window_uncovered")
            full_axis = t
            t = t[mask]
            y = _trace(raw, m.signal)[mask]
            if not np.all(np.isfinite(y)):
                raise ProbeError("recipe_nonfinite")
            if m.operation in ("gain", "unity_frequency"):
                if not raw.complex_data:
                    raise ProbeError("recipe_ac_missing")
                ref = _trace(raw, m.reference)[mask]
                if np.any(np.abs(ref) < 1e-18):
                    raise ProbeError("recipe_reference_missing")
                ratio = np.abs(y / ref)
                value = float(ratio[0]) if m.operation == "gain" else _cross(t, ratio, 1, False)
            else:
                if raw.complex_data:
                    raise ProbeError("recipe_transient_missing")
                if m.operation == "rail_headroom":
                    other = (full_axis >= m.second_start) & (full_axis <= m.second_end)
                    if np.count_nonzero(other) < 2:
                        raise ProbeError("recipe_window_uncovered")
                    rail = float(np.mean(_trace(raw, m.reference)[other]))
                    values = []
                    for signal in (m.signal, *m.additional_signals):
                        low, high = _trace(raw, signal)[mask], _trace(raw, signal)[other]
                        if not np.all(np.isfinite(np.concatenate((low, high)))):
                            raise ProbeError("recipe_nonfinite")
                        if max(np.ptp(low), np.ptp(high)) > max(1e-6, abs(rail) * 1e-3):
                            raise ProbeError("recipe_not_settled")
                        bottom, top = float(np.mean(low)), float(np.mean(high))
                        if bottom < -1e-6 or top > rail + 1e-6 or bottom >= top:
                            raise ProbeError(
                                "output_outside_supplies",
                                f"low={bottom:g}, high={top:g}, supply={rail:g}",
                            )
                        values.extend((bottom, rail - top))
                    value = max(values)
                elif m.operation == "crossing_value":
                    when = _cross(
                        t, _trace(raw, m.trigger)[mask], m.trigger_level, m.trigger_rising
                    )
                    value = float(np.interp(when, t, y))
                elif m.operation == "hysteresis":
                    trigger = _trace(raw, m.trigger)[mask]
                    rising = _cross(t, trigger, m.trigger_level, True)
                    falling = _cross(t, trigger, m.trigger_level, False)
                    value = abs(float(np.interp(rising, t, y) - np.interp(falling, t, y)))
                elif m.operation == "delay":
                    if (
                        m.signal.casefold() == m.trigger.casefold()
                        and m.level == m.trigger_level
                        and m.rising == m.trigger_rising
                        and recipe.unit == "Hz"
                    ):
                        # Older frozen plans expressed switching frequency as a
                        # self-delay. A first edge minus itself is always zero;
                        # consecutive edges in the same frozen window answer Hz.
                        value = _frequency(t, y, m.level, m.rising)
                    else:
                        a = _cross(
                            t, _trace(raw, m.trigger)[mask], m.trigger_level, m.trigger_rising
                        )
                        b = _cross(t, y, m.level, m.rising)
                        if b < a:
                            raise ProbeError("recipe_noncausal_transition")
                        value = b - a
                elif m.operation == "frequency":
                    value = _frequency(t, y, m.level, m.rising)
                elif m.operation == "slew":
                    a = _cross(t, y, m.trigger_level, m.rising)
                    b = _cross(t, y, m.level, m.rising)
                    if b <= a:
                        raise ProbeError("recipe_slew_invalid")
                    value = abs(m.level - m.trigger_level) / (b - a)
                else:
                    if m.operation == "mean" and np.ptp(y) > max(
                        1e-9, abs(float(np.mean(y))) * 1e-3
                    ):
                        raise ProbeError("recipe_not_settled")
                    value = float(
                        {
                            "mean": np.mean,
                            "min": np.min,
                            "max": np.max,
                            "peak_to_peak": np.ptp,
                            "rms": lambda x: np.sqrt(np.mean(x * x)),
                        }[m.operation](y)
                    )
            if m.absolute:
                value = abs(value)
            value = value * m.scale + m.offset
            if not np.isfinite(value):
                raise ProbeError("recipe_nonfinite")
            return {"recipe_value": float(value)}
        except (KeyError, ValueError, OSError, IndexError) as exc:
            raise ProbeError("recipe_measurement_missing", str(exc)) from exc

    return ProbeSpec(
        probe_id="circuit_measurement",
        title=recipe.purpose,
        unit=recipe.unit,
        ports_needed=tuple(recipe.terminals),
        judge_key="recipe_value",
        defaults={"tstop_s": recipe.stop, "tmax_s": recipe.step},
        renderer=render,
        measurer=measure,
        analysis=recipe.analysis,
    )
