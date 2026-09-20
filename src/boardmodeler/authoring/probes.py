"""Deterministic probe decks: one LTspice run per physical question.

Honesty rule this module implements: a probe answers its question from the
observed waveform or it refuses. Every measurement raises
:class:`ProbeError` with a machine-readable reason when the waveform cannot
answer it (a signal was not saved, no crossing exists, the run was truncated,
a value is not finite, the operating point was never established). The harness
turns that into UNKNOWN — a probe never guesses and never fabricates PASS.

Every deck:

* ``.include``\\ s the model library by **absolute path** (nothing is ever
  written into the LTspice installation or its ``lib/`` directory),
* instantiates the model as a subcircuit with **all** of its declared ports
  wired by name (a port the probe needs that the model does not declare is a
  ``port_missing:<NAME>`` refusal, never a silent mismatch of node order),
* biases the part exactly as documented in each probe's docstring,
* ``.save``\\ s only the signals it measures,
* declares an explicit maximum timestep small enough for the measurement, and
* never uses ``.ic``/``uic`` (the DC operating point is the default), because a
  probe that short-circuits the solver's initial condition is not evidence.

Scope: these probes characterise a *pin-level behavioural* model whose regulated
output appears at ``VOUT`` and whose feedback is the external divider on ``FB``
(the ``BM_REG_*`` template family). A transistor-level model with a real switch
node needs the power stage that :mod:`boardmodeler.models.capability` builds.

Two reading notes, because a probe reports what the pin does, not what the die
thinks:

* ``pg_threshold`` observes the *pin*. A model with a power-good assertion delay
  (the templates' ``PG_DELAY``) releases later than its internal threshold, and
  the delay is not de-embedded because it is not observable from the pin.
* ``shutdown_current`` and ``quiescent_current`` report the current into ``VIN``,
  which for a single-port model includes the feedback divider current the
  output stage has to source. ``load_regulation`` reports the load current
  through ``I(I_load)``; the divider current is additional.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from boardmodeler.models.library import ModelStoreError, subckt_ports
from boardmodeler.simulation.raw import RawFormatError, read_raw

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from boardmodeler.authoring.spec import Characteristic

__all__ = [
    "PROBES",
    "ProbeError",
    "ProbeSpec",
    "judge_value",
    "model_ports",
    "required_ports",
]

#: Relative slack allowed when checking that a run reached its declared stop
#: time (the last solver step rarely lands exactly on ``tstop``).
_REACH_SLACK = 1e-3


class ProbeError(RuntimeError):
    """A probe could not answer its question. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    def full_reason(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


# --------------------------------------------------------------------------- #
# deck scaffolding


def _fmt(value: float) -> str:
    return f"{value:.12g}"


def _zero_safe(value: float) -> float:
    """Normalise ``-0.0`` (and float noise around zero) so reports say ``0``."""
    return 0.0 if value == 0.0 else value


#: Application node for each standard regulator port. ``GND`` is the SPICE
#: ground node, as the template family requires.
_PORT_NETS: dict[str, str] = {
    "VIN": "vin",
    "EN": "en",
    "FB": "fb",
    "PG": "pg",
    "VOUT": "vout",
    "GND": "0",
    "SW": "sw",
    "ILIM_MODE": "ilim_mode",
}

_BASE_DEFAULTS: dict[str, float] = {
    "temp_c": 25.0,
    "vout_nom": 3.3,
    "vin_dc": 12.0,
    "en_high": 2.0,
    "r_top": 10e3,
    "r_bot": 3.24e3,
    "c_out": 22e-6,
    "r_load": 100.0,
    "pg_rail": 5.0,
    "pg_pullup": 10e3,
    "gate_frac": 0.02,
    "hold_frac": 0.5,
    "tmax_s": 20e-6,
    "settle_frac": 0.1,
    "min_vout_frac": 0.25,
}


def model_ports(model_lib: Path, subckt: str) -> list[str]:
    """Declared ports of ``subckt`` in ``model_lib`` (raises :class:`ProbeError`)."""
    try:
        text = Path(model_lib).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProbeError("model_lib_unreadable", f"{model_lib}: {exc}") from exc
    try:
        return list(subckt_ports(text, subckt))
    except ModelStoreError as exc:
        raise ProbeError(f"subckt_missing:{subckt}", str(exc)) from exc


def judge_value(probe_id: str, measured: Mapping[str, float | str]) -> tuple[str, float]:
    """``(key, value)`` of the measurement a characteristic is judged against.

    A probe can report several numbers (context for the report); exactly one of
    them is the judged value, named by :attr:`ProbeSpec.judge_key`.
    """
    try:
        spec = PROBES[probe_id]
    except KeyError as exc:
        raise ValueError(f"unknown probe {probe_id!r}") from exc
    key = spec.judge_key
    value = measured.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError(f"judge_value_missing:{key}", f"probe {probe_id} did not report {key}")
    number = float(value)
    if not math.isfinite(number):
        raise ProbeError(f"judge_value_not_finite:{key}")
    return key, number


def required_ports(characteristics: Iterable[Characteristic]) -> tuple[str, ...]:
    """Ports a model must declare for the covered characteristics' probes.

    Deterministic: first-seen order over the probe registry, then over each
    probe's ``ports_needed``. The model may declare more ports than this (the
    harness wires every declared port); it must declare at least these.
    """
    needed: set[str] = set()
    for char in characteristics:
        if char.probe is None:
            continue
        if char.probe not in PROBES:
            raise ValueError(f"{char.char_id} binds unknown probe {char.probe!r}")
        needed.update(port.upper() for port in PROBES[char.probe].ports_needed)
    ordered: list[str] = []
    for spec in PROBES.values():
        for port in spec.ports_needed:
            upper = port.upper()
            if upper in needed and upper not in ordered:
                ordered.append(upper)
    return tuple(ordered)


@dataclass(frozen=True)
class _Waves:
    """One readable ``.raw``: time column plus named traces."""

    t: np.ndarray
    traces: dict[str, np.ndarray]

    def y(self, name: str) -> np.ndarray:
        key = name.lower()
        if key not in self.traces:
            available = ", ".join(sorted(self.traces)) or "(none)"
            raise ProbeError(
                f"signal_missing:{name}",
                f"the run saved {available}; the probe needs {name}",
            )
        return self.traces[key]


def _load(raw_path: Path, params: Mapping[str, float]) -> _Waves:
    """Read the waveform and refuse if it cannot answer a question at all."""
    try:
        raw = read_raw(raw_path)
    except (RawFormatError, OSError) as exc:
        raise ProbeError("raw_unreadable", f"{raw_path}: {exc}") from exc
    if raw.data.shape[0] < 2 or raw.data.shape[1] < 2:
        raise ProbeError("run_empty", f"{raw_path} has {raw.data.shape[0]} points")
    t = np.asarray(raw.data[:, 0], dtype=np.float64)
    traces: dict[str, np.ndarray] = {}
    for index, name in enumerate(raw.variables[1:], start=1):
        values = np.asarray(raw.data[:, index], dtype=np.float64)
        if not np.all(np.isfinite(values)):
            raise ProbeError(f"non_finite:{name}", "the waveform contains NaN/Inf")
        traces[name.lower()] = values
    if not np.all(np.diff(t) >= 0.0):
        raise ProbeError("time_not_monotonic", "the saved time column decreases")
    expected = params.get("tstop_s")
    if expected:
        reached = float(t[-1])
        if reached < expected * (1.0 - _REACH_SLACK):
            raise ProbeError(
                "run_truncated",
                f"the waveform reached {reached:g} s of the declared {expected:g} s",
            )
    return _Waves(t=t, traces=traces)


def _cross_up(t: np.ndarray, y: np.ndarray, level: float) -> float:
    """Time of the first rising crossing of ``level`` (interpolated)."""
    hits = np.flatnonzero(y >= level)
    if hits.size == 0:
        raise ProbeError(
            "no_crossing",
            f"the waveform never reached {level:g} (max {float(np.max(y)):g})",
        )
    index = int(hits[0])
    if index == 0:
        return float(t[0])
    return _interp_time(t, y, index, level)


def _cross_down(t: np.ndarray, y: np.ndarray, level: float) -> float:
    """Time of the last falling crossing of ``level`` (interpolated)."""
    hits = np.flatnonzero(y >= level)
    if hits.size == 0:
        raise ProbeError(
            "no_crossing",
            f"the waveform never reached {level:g} (max {float(np.max(y)):g})",
        )
    index = int(hits[-1])
    if index >= t.size - 1:
        raise ProbeError(
            "no_crossing",
            f"the waveform was still at or above {level:g} at the end of the run",
        )
    return _interp_time(t, y, index + 1, level)


def _interp_time(t: np.ndarray, y: np.ndarray, index: int, level: float) -> float:
    y1 = float(y[index])
    y0 = float(y[index - 1])
    t1 = float(t[index])
    t0 = float(t[index - 1])
    if y1 == y0:
        return t1
    fraction = (level - y0) / (y1 - y0)
    return t0 + fraction * (t1 - t0)


def _window(t: np.ndarray, start_s: float, stop_s: float) -> np.ndarray:
    mask = (t >= start_s) & (t <= stop_s)
    if not np.any(mask):
        raise ProbeError("window_not_covered", f"no saved samples in [{start_s:g}, {stop_s:g}] s")
    return mask


def _late_window_mean(
    waves: _Waves, name: str, params: Mapping[str, float], *, span_s: float
) -> float:
    stop = float(waves.t[-1])
    start = max(float(waves.t[0]), stop - span_s)
    mask = _window(waves.t, start, stop)
    return float(np.mean(waves.y(name)[mask]))


def _require_regulated(
    waves: _Waves, params: Mapping[str, float], *, window: tuple[float, float]
) -> None:
    """Refuse (UNKNOWN) when the converter never established its operating point."""
    mask = _window(waves.t, window[0], window[1])
    mean = float(np.mean(waves.y("V(vout)")[mask]))
    floor = params["min_vout_frac"] * params["vout_nom"]
    if mean < floor:
        raise ProbeError(
            "output_not_regulated",
            f"mean V(vout)={mean:g} V over [{window[0]:g}, {window[1]:g}] s is below "
            f"{floor:g} V; the probe cannot judge this operating point",
        )


def _deck(
    spec: ProbeSpec,
    params: Mapping[str, float],
    *,
    model_lib: Path,
    subckt: str,
    ports: Sequence[str],
    cards: Sequence[str],
    save: Sequence[str],
    port_nets: Mapping[str, str] | None = None,
) -> str:
    nets = dict(_PORT_NETS)
    nets.update(port_nets or {})
    nodes: list[str] = []
    extras: list[str] = []
    for port in ports:
        net = nets.get(port.upper())
        if net is None:
            net = f"bmx_{port.lower()}"
            card = f"R_{net} {net} 0 1e9"
            if card not in extras:
                extras.append(card)
        nodes.append(net)
    lines = [
        f"* BoardModeler datasheet probe {spec.probe_id}: {spec.title}",
        f"* question: {spec.question}",
        "* probe parameters: " + ", ".join(f"{k}={_fmt(v)}" for k, v in sorted(params.items())),
        "* model under test (absolute include; the LTspice installation is never written to):",
        f".include {Path(model_lib).resolve().as_posix()}",
        "",
        *cards,
        "",
        f"X1 {' '.join(nodes)} {subckt}",
        *extras,
        "",
        ".options plotwinsize=0",
        f".temp {_fmt(params['temp_c'])}",
        f".tran 0 {_fmt(params['tstop_s'])} 0 {_fmt(params['tmax_s'])}",
        ".save " + " ".join(sorted(set(save))),
        ".end",
        "",
    ]
    return "\n".join(lines)


def _application(
    params: Mapping[str, float],
    *,
    vin_card: str,
    en_card: str,
    load_cards: Sequence[str],
    extra_cards: Sequence[str] = (),
) -> list[str]:
    """The standard regulator application around the part under test."""
    return [
        vin_card,
        en_card,
        f"R_top vout fb {_fmt(params['r_top'])}",
        f"R_bot fb 0 {_fmt(params['r_bot'])}",
        f"C_out vout 0 {_fmt(params['c_out'])}",
        *load_cards,
        f"V_pg_rail pgrail 0 {_fmt(params['pg_rail'])}",
        f"R_pg pg pgrail {_fmt(params['pg_pullup'])}",
        *extra_cards,
    ]


@dataclass(frozen=True)
class ProbeSpec:
    """One deterministic probe: a deck recipe plus its waveform judgement."""

    probe_id: str
    title: str
    unit: str
    ports_needed: tuple[str, ...]
    question: str = ""
    defaults: Mapping[str, float] = field(default_factory=dict)
    judge_key: str = "value"
    renderer: Renderer | None = None
    measurer: Measurer | None = None

    def merged_params(self, params: Mapping[str, float] | None = None) -> dict[str, float]:
        merged = {**_BASE_DEFAULTS, **self.defaults}
        for key, value in (params or {}).items():
            if key not in merged:
                raise ValueError(f"probe {self.probe_id} has no parameter {key!r}")
            merged[key] = float(value)
        return merged

    def render(self, *, model_lib: Path, subckt: str, params: Mapping[str, float]) -> str:
        """A complete, self-contained deck for this probe (LTspice text)."""
        if self.renderer is None:  # pragma: no cover - every registry entry has one
            raise ProbeError(f"probe_unimplemented:{self.probe_id}")
        merged = self.merged_params(params)
        ports = model_ports(model_lib, subckt)
        declared = {port.upper() for port in ports}
        missing = [port for port in self.ports_needed if port.upper() not in declared]
        if missing:
            raise ProbeError(
                f"port_missing:{missing[0]}",
                f"the model declares {list(ports)}; the deck needs {list(self.ports_needed)}",
            )
        return self.renderer(self, merged, Path(model_lib), subckt)

    def measure(self, raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
        """Judged values from the observed waveform (raises :class:`ProbeError`)."""
        if self.measurer is None:  # pragma: no cover - every registry entry has one
            raise ProbeError(f"probe_unimplemented:{self.probe_id}")
        return self.measurer(Path(raw_path), self.merged_params(params))


Renderer = Callable[[ProbeSpec, Mapping[str, float], Path, str], str]
Measurer = Callable[[Path, Mapping[str, float]], dict[str, float]]


# --------------------------------------------------------------------------- #
# probes


def _render_uvlo_rise(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    ramp_s = params["vin_top"] / params["ramp_v_per_s"]
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 PWL(0 0 {_fmt(ramp_s)} {_fmt(params['vin_top'])})",
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(vin)", "V(vout)", "V(fb)"),
    )


def _measure_uvlo_rise(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    gate = params["gate_frac"] * params["vout_nom"]
    t_start = _cross_up(waves.t, waves.y("V(vout)"), gate)
    return {
        "vin_at_start": float(np.interp(t_start, waves.t, waves.y("V(vin)"))),
        "t_start_s": t_start,
        "vout_gate_v": gate,
        "vin_top_v": params["vin_top"],
    }


def _render_uvlo_fall(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    ramp_s = params["vin_top"] / params["ramp_v_per_s"]
    fall_end = params["t_hold_s"] + ramp_s
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=(
                f"V_vin vin 0 PWL(0 {_fmt(params['vin_top'])} {_fmt(params['t_hold_s'])} "
                f"{_fmt(params['vin_top'])} {_fmt(fall_end)} 0)"
            ),
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(vin)", "V(vout)", "V(fb)"),
    )


def _measure_uvlo_fall(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    hold = params["hold_frac"] * params["vout_nom"]
    t_stop = _cross_down(waves.t, waves.y("V(vout)"), hold)
    return {
        "vin_at_stop": float(np.interp(t_stop, waves.t, waves.y("V(vin)"))),
        "t_stop_s": t_stop,
        "vout_hold_v": hold,
    }


def _render_en_rise(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    ramp_s = params["en_high"] / params["en_ramp_v_per_s"]
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 PWL(0 0 {_fmt(ramp_s)} {_fmt(params['en_high'])})",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(en)", "V(vout)", "V(fb)"),
    )


def _measure_en_rise(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    gate = params["gate_frac"] * params["vout_nom"]
    t_start = _cross_up(waves.t, waves.y("V(vout)"), gate)
    return {
        "en_at_start": float(np.interp(t_start, waves.t, waves.y("V(en)"))),
        "t_start_s": t_start,
        "vin_dc_v": params["vin_dc"],
    }


def _render_en_fall(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    ramp_s = params["en_high"] / params["en_ramp_v_per_s"]
    fall_end = params["t_hold_s"] + ramp_s
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=(
                f"V_en en 0 PWL(0 {_fmt(params['en_high'])} {_fmt(params['t_hold_s'])} "
                f"{_fmt(params['en_high'])} {_fmt(fall_end)} 0)"
            ),
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(en)", "V(vout)", "V(fb)"),
    )


def _measure_en_fall(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    hold = params["hold_frac"] * params["vout_nom"]
    t_stop = _cross_down(waves.t, waves.y("V(vout)"), hold)
    return {
        "en_at_stop": float(np.interp(t_stop, waves.t, waves.y("V(en)"))),
        "t_stop_s": t_stop,
        "vin_dc_v": params["vin_dc"],
    }


def _render_vref(spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str) -> str:
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(fb)", "V(vout)"),
    )


def _measure_vref(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    stop = float(waves.t[-1])
    span = (stop - float(waves.t[0])) * params["settle_frac"]
    window = (stop - span, stop)
    _require_regulated(waves, params, window=window)
    fb = waves.y("V(fb)")[_window(waves.t, *window)]
    halfway = window[0] + span / 2.0
    first = float(np.mean(waves.y("V(fb)")[_window(waves.t, window[0], halfway)]))
    second = float(np.mean(waves.y("V(fb)")[_window(waves.t, halfway, window[1])]))
    value = float(np.mean(fb))
    if value and abs(second - first) > 0.02 * abs(value):
        raise ProbeError(
            "not_settled",
            f"V(fb) is still drifting: {first:g} V then {second:g} V over the window",
        )
    return {
        "v_fb": value,
        "v_out": float(np.mean(waves.y("V(vout)")[_window(waves.t, *window)])),
        "v_fb_drift_v": abs(second - first),
    }


def _render_load_regulation(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    step = params["t_step_s"]
    ramp_end = step + params["t_step_ramp_s"]
    load = (
        f"I_load vout 0 PWL(0 {_fmt(params['i_light'])} {_fmt(step)} {_fmt(params['i_light'])} "
        f"{_fmt(ramp_end)} {_fmt(params['i_heavy'])})"
    )
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[load],
        ),
        save=("V(fb)", "V(vout)", "I(I_load)"),
    )


def _measure_load_regulation(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    win = params["window_s"]
    light = (params["t_step_s"] - win, params["t_step_s"])
    heavy = (float(waves.t[-1]) - win, float(waves.t[-1]))
    _require_regulated(waves, params, window=light)
    _require_regulated(waves, params, window=heavy)
    vout_light = float(np.mean(waves.y("V(vout)")[_window(waves.t, *light)]))
    vout_heavy = float(np.mean(waves.y("V(vout)")[_window(waves.t, *heavy)]))
    return {
        "i_light": float(np.mean(waves.y("I(I_load)")[_window(waves.t, *light)])),
        "i_heavy": float(np.mean(waves.y("I(I_load)")[_window(waves.t, *heavy)])),
        "vout_light": vout_light,
        "vout_heavy": vout_heavy,
        "v_fb_light": float(np.mean(waves.y("V(fb)")[_window(waves.t, *light)])),
        "v_fb_heavy": float(np.mean(waves.y("V(fb)")[_window(waves.t, *heavy)])),
        "load_reg_error_pct": (
            0.0 if not vout_light else (vout_light - vout_heavy) / vout_light * 100.0
        ),
    }


def _render_current_limit(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    step = params["t_step_s"]
    ramp_end = step + params["t_step_ramp_s"]
    load = (
        f"I_load vout 0 PWL(0 {_fmt(params['i_start'])} {_fmt(step)} {_fmt(params['i_start'])} "
        f"{_fmt(ramp_end)} {_fmt(params['i_max'])})"
    )
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        port_nets={"VOUT": "vout_pin"},
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[load],
            extra_cards=["V_sense vout_pin vout 0"],
        ),
        save=("V(vout)", "V(fb)", "I(I_load)", "I(V_sense)"),
    )


def _measure_current_limit(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    win = params["window_s"]
    light = (params["t_step_s"] - win, params["t_step_s"])
    _require_regulated(waves, params, window=light)
    vout = waves.y("V(vout)")
    reference = float(np.mean(vout[_window(waves.t, *light)]))
    delivered = waves.y("I(V_sense)")
    mask = vout >= params["reg_frac"] * reference
    if not np.any(mask):
        raise ProbeError(
            "no_regulated_samples",
            f"the output never stayed above {params['reg_frac']:g} of {reference:g} V",
        )
    index = int(np.argmax(np.where(mask, delivered, -np.inf)))
    load = waves.y("I(I_load)")
    return {
        "i_out_limit": float(delivered[index]),
        "i_load_at_limit": float(load[index]),
        "vout_at_limit": float(vout[index]),
        "vout_ref": reference,
        "i_out_peak": float(np.max(delivered)),
    }


def _render_pg_threshold(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    enable_end = params["t_enable_s"]
    ov_cards = [
        (
            f"V_ov ov 0 PWL(0 0 {_fmt(params['t_ov_s'])} 0 "
            f"{_fmt(params['t_ov_s'] + params['t_ov_ramp_s'])} {_fmt(params['ov_v'])})"
        ),
        "D_ov ov vout DBM",
        ".model DBM D(Ron=0.5 Roff=1Meg Vfwd=0)",
    ]
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 PWL(0 0 {_fmt(enable_end)} {_fmt(params['en_high'])})",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
            extra_cards=ov_cards,
        ),
        save=("V(fb)", "V(vout)", "V(pg)", "I(R_pg)"),
    )


def _measure_pg_threshold(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    span = params["window_s"]
    settled = (params["t_ov_s"] - span, params["t_ov_s"])
    _require_regulated(waves, params, window=settled)
    pg = waves.y("V(pg)")
    fb = waves.y("V(fb)")
    released = _window(waves.t, *settled)
    reference = float(np.mean(fb[released]))
    if reference <= 0:
        raise ProbeError("no_reference", "V(fb) never settled to a positive value")

    release_level = 0.5 * params["pg_rail"]
    if not np.all(pg[released] >= release_level):
        raise ProbeError(
            "pg_not_released",
            "the power-good pin is not released (mean "
            f"{float(np.mean(pg[released])):.6g} V of {release_level:g} V) in the settled "
            "window before the overvoltage phase, so no release threshold can be observed",
        )
    # The release is the last sustained rise into the settled window: a transient
    # blip while the output passes through the window is not a release.
    lows = np.flatnonzero((waves.t < settled[0]) & (pg < release_level))
    if lows.size:
        rises = np.flatnonzero((waves.t > waves.t[int(lows[-1])]) & (pg >= release_level))
        if rises.size == 0:  # pragma: no cover - the settled window is already high
            raise ProbeError("no_pg_release", "the pin never rose above the release level")
        t_release = float(waves.t[int(rises[0])])
    else:
        t_release = float(waves.t[0])

    tripped = np.flatnonzero((waves.t > t_release) & (pg < release_level))
    if tripped.size == 0:
        raise ProbeError(
            "no_pg_trip",
            "PG stayed released after the overvoltage phase; the deck never crossed "
            "the power-good window",
        )
    i_trip = int(tripped[0])
    low_start = float(waves.t[i_trip]) + span / 2.0
    low = _window(waves.t, low_start, float(waves.t[-1]))
    fb_release = float(np.interp(t_release, waves.t, fb))
    fb_trip = float(np.interp(float(waves.t[i_trip]), waves.t, fb))
    return {
        "pg_ratio_release": fb_release / reference,
        "pg_ratio_trip": fb_trip / reference,
        "pg_leak_a": abs(float(np.mean(waves.y("I(R_pg)")[released]))),
        "pg_high_v": float(np.mean(pg[released])),
        "pg_low_v": float(np.mean(pg[low])),
        "pg_low_current_a": -float(np.mean(waves.y("I(R_pg)")[low])),
        "v_fb_ref": reference,
    }


def _render_soft_start(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=(f"V_en en 0 PWL(0 0 {_fmt(params['t_enable_s'])} {_fmt(params['en_high'])})"),
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("V(en)", "V(vout)", "V(fb)"),
    )


def _measure_soft_start(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    stop = float(waves.t[-1])
    span = (stop - float(waves.t[0])) * params["settle_frac"]
    final = _late_window_mean(waves, "V(vout)", params, span_s=span)
    _require_regulated(waves, params, window=(stop - span, stop))
    t_enable = _cross_up(waves.t, waves.y("V(en)"), 0.5 * params["en_high"])
    t_95 = _cross_up(waves.t, waves.y("V(vout)"), 0.95 * final)
    if t_95 <= t_enable:
        raise ProbeError("no_ramp", "the output was already at 95 % when EN went high")
    mask = _window(waves.t, t_enable, min(t_95 + 1e-3, stop))
    peak = float(np.max(waves.y("V(vout)")[mask]))
    return {
        "t_ss_s": t_95 - t_enable,
        "t_95_s": t_95,
        "t_enable_s": t_enable,
        "vout_final_v": final,
        "overshoot_v": peak - final,
    }


def _render_shutdown_current(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 {_fmt(params['en_low'])}",
            load_cards=[f"R_load vout 0 {_fmt(params['r_load'])}"],
        ),
        save=("I(V_vin)", "V(vout)", "V(en)"),
    )


def _measure_shutdown_current(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    stop = float(waves.t[-1])
    span = (stop - float(waves.t[0])) * params["settle_frac"]
    mask = _window(waves.t, stop - span, stop)
    vout = float(np.mean(waves.y("V(vout)")[mask]))
    if vout > params["min_vout_frac"] * params["vout_nom"]:
        raise ProbeError(
            "not_shutdown",
            f"the output sits at {vout:g} V with EN at {params['en_low']:g} V; "
            "the probe cannot claim a shutdown current",
        )
    return {
        "i_vin_a": _zero_safe(-float(np.mean(waves.y("I(V_vin)")[mask]))),
        "vout_v": _zero_safe(vout),
        "en_v": float(params["en_low"]),
    }


def _render_quiescent_current(
    spec: ProbeSpec, params: Mapping[str, float], model_lib: Path, subckt: str
) -> str:
    return _deck(
        spec,
        params,
        model_lib=model_lib,
        subckt=subckt,
        ports=model_ports(model_lib, subckt),
        cards=_application(
            params,
            vin_card=f"V_vin vin 0 {_fmt(params['vin_dc'])}",
            en_card=f"V_en en 0 {_fmt(params['en_high'])}",
            load_cards=[f"R_load vout 0 {_fmt(params['r_no_load'])}"],
        ),
        save=("I(V_vin)", "V(vout)", "V(fb)"),
    )


def _measure_quiescent_current(raw_path: Path, params: Mapping[str, float]) -> dict[str, float]:
    waves = _load(raw_path, params)
    stop = float(waves.t[-1])
    span = (stop - float(waves.t[0])) * params["settle_frac"]
    _require_regulated(waves, params, window=(stop - span, stop))
    mask = _window(waves.t, stop - span, stop)
    return {
        "i_vin_a": -float(np.mean(waves.y("I(V_vin)")[mask])),
        "vout_v": float(np.mean(waves.y("V(vout)")[mask])),
        "v_fb_v": float(np.mean(waves.y("V(fb)")[mask])),
    }


# --------------------------------------------------------------------------- #
# the registry
#
# Registry order is the order probes are run in and the order of the port union;
# keep it stable.

PROBES: dict[str, ProbeSpec] = {}


def _register(spec: ProbeSpec) -> None:
    PROBES[spec.probe_id] = spec


_register(
    ProbeSpec(
        probe_id="uvlo_rise",
        title="VIN at which the converter starts regulating (VIN rising)",
        question="At what VIN does the output begin to come up while VIN is slowly raised?",
        unit="V",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="vin_at_start",
        defaults={
            "vin_top": 6.0,
            "ramp_v_per_s": 20.0,
            "tstop_s": 0.31,
        },
        renderer=_render_uvlo_rise,
        measurer=_measure_uvlo_rise,
    )
)

_register(
    ProbeSpec(
        probe_id="uvlo_fall",
        title="VIN at which regulation stops (VIN falling)",
        question="At what VIN does the output stop regulating while VIN is slowly lowered?",
        unit="V",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="vin_at_stop",
        defaults={
            "vin_top": 6.0,
            "ramp_v_per_s": 20.0,
            "t_hold_s": 0.08,
            "tstop_s": 0.39,
        },
        renderer=_render_uvlo_fall,
        measurer=_measure_uvlo_fall,
    )
)

_register(
    ProbeSpec(
        probe_id="en_rise",
        title="EN voltage at which the converter starts (EN rising, VIN fixed)",
        question="At what EN voltage does the output begin to come up?",
        unit="V",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="en_at_start",
        defaults={
            "en_ramp_v_per_s": 10.0,
            "tstop_s": 0.205,
        },
        renderer=_render_en_rise,
        measurer=_measure_en_rise,
    )
)

_register(
    ProbeSpec(
        probe_id="en_fall",
        title="EN voltage at which regulation stops (EN falling, VIN fixed)",
        question="At what EN voltage does the output stop regulating?",
        unit="V",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="en_at_stop",
        defaults={
            "en_ramp_v_per_s": 10.0,
            "t_hold_s": 0.05,
            "tstop_s": 0.255,
        },
        renderer=_render_en_fall,
        measurer=_measure_en_fall,
    )
)

_register(
    ProbeSpec(
        probe_id="vref",
        title="Steady-state feedback voltage (internal reference seen at the FB pin)",
        question="What is V(FB) once the loop has settled at a fixed VIN and load?",
        unit="V",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="v_fb",
        defaults={"tstop_s": 30e-3, "tmax_s": 10e-6},
        renderer=_render_vref,
        measurer=_measure_vref,
    )
)

_register(
    ProbeSpec(
        probe_id="load_regulation",
        title="Output voltage at a light and at a heavy load",
        question="How far does the regulated output move between a light and a heavy load?",
        unit="A",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="i_heavy",
        defaults={
            "i_light": 0.05,
            "i_heavy": 2.0,
            "t_step_s": 20e-3,
            "t_step_ramp_s": 1e-3,
            "window_s": 2e-3,
            "tstop_s": 30e-3,
            "tmax_s": 10e-6,
        },
        renderer=_render_load_regulation,
        measurer=_measure_load_regulation,
    )
)

_register(
    ProbeSpec(
        probe_id="current_limit",
        title="Output current delivered just before the output leaves regulation",
        question="How much output current can the part deliver before the output droops?",
        unit="A",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="i_out_limit",
        defaults={
            "i_start": 0.5,
            "i_max": 3.4,
            "reg_frac": 0.9,
            "t_step_s": 20e-3,
            "t_step_ramp_s": 20e-3,
            "window_s": 2e-3,
            "tstop_s": 42e-3,
            "tmax_s": 10e-6,
        },
        renderer=_render_current_limit,
        measurer=_measure_current_limit,
    )
)

_register(
    ProbeSpec(
        probe_id="pg_threshold",
        title="PWRGD state vs output voltage with a pull-up to a declared rail",
        question=(
            "At what fraction of the settled reference does PG release and re-trip, and "
            "how much current does the open-drain pin leak while released?"
        ),
        unit="A",
        ports_needed=("VIN", "EN", "FB", "PG", "VOUT", "GND"),
        judge_key="pg_leak_a",
        defaults={
            "t_enable_s": 1e-3,
            "t_ov_s": 12e-3,
            "t_ov_ramp_s": 2e-3,
            "ov_v": 4.2,
            "window_s": 2e-3,
            "tstop_s": 22e-3,
            "tmax_s": 2e-6,
        },
        renderer=_render_pg_threshold,
        measurer=_measure_pg_threshold,
    )
)

_register(
    ProbeSpec(
        probe_id="soft_start",
        title="Time from EN high to 95 % of the settled output",
        question="How long does the output take to reach 95 % of its final value after EN goes high?",
        unit="s",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="t_ss_s",
        defaults={
            "t_enable_s": 100e-6,
            "tstop_s": 20e-3,
            "tmax_s": 5e-6,
        },
        renderer=_render_soft_start,
        measurer=_measure_soft_start,
    )
)

_register(
    ProbeSpec(
        probe_id="shutdown_current",
        title="VIN supply current with EN below its threshold",
        question="How much current does the part draw from VIN while EN holds it off?",
        unit="A",
        ports_needed=("VIN", "EN", "VOUT", "GND"),
        judge_key="i_vin_a",
        defaults={"en_low": 0.0, "tstop_s": 10e-3, "tmax_s": 10e-6},
        renderer=_render_shutdown_current,
        measurer=_measure_shutdown_current,
    )
)

_register(
    ProbeSpec(
        probe_id="quiescent_current",
        title="VIN supply current in regulation at no load",
        question="How much current does the part draw from VIN while regulating with no load?",
        unit="A",
        ports_needed=("VIN", "EN", "FB", "VOUT", "GND"),
        judge_key="i_vin_a",
        defaults={"tstop_s": 30e-3, "tmax_s": 10e-6, "r_no_load": 1e6},
        renderer=_render_quiescent_current,
        measurer=_measure_quiescent_current,
    )
)


def _register_io_probes() -> None:
    from boardmodeler.authoring.io_probes import register

    register(PROBES)


_register_io_probes()
