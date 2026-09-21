"""Deck construction (Phase 1 step 1).

A deck is the only thing the simulator ever sees, so it is built explicitly:

* every source is written out in full (an explicit ramp/`PWL`, never a bare
  ideal step that would hide a slew-rate or inrush question),
* ``.ic``/``uic`` appear **only** when the test declares them, so two tests that
  differ only in initial conditions cannot silently share one,
* `.meas` cards are emitted for the quantities a test names as its measurement
  list; the assertion evaluator reads waveforms, and `.meas` is kept as an
  independent cross-check of the same numbers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "DeckSpec",
    "Include",
    "MeasSpec",
    "Source",
    "TemplateError",
    "TranSpec",
    "render_template",
    "write_deck",
]


class TemplateError(ValueError):
    """Raised for an unrenderable template (unknown or leftover placeholder)."""


def _fmt(value: float | int | str) -> str:
    """Format a number for a SPICE card without losing precision or adding noise."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise TypeError("boolean is not a valid SPICE value")
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude < 1e-3 or magnitude >= 1e6:
        return f"{value:.12g}"
    return f"{value:.12g}"


@dataclass(frozen=True)
class Source:
    """One independent source card."""

    name: str
    terminals: tuple[str, str]
    kind: Literal["dc", "pulse", "pwl", "sine", "ac"]
    params: Mapping[str, float] = field(default_factory=dict)
    points: tuple[tuple[float, float], ...] = ()

    def card(self) -> str:
        if self.kind == "dc":
            return (
                f"{self.name} {self.terminals[0]} {self.terminals[1]} DC {_fmt(self.params['v'])}"
            )
        if self.kind == "pulse":
            order = ("v1", "v2", "td", "tr", "tf", "pw", "per")
            values = " ".join(_fmt(self.params[key]) for key in order if key in self.params)
            return f"{self.name} {self.terminals[0]} {self.terminals[1]} PULSE({values})"
        if self.kind == "pwl":
            if len(self.points) < 2:
                raise ValueError(f"{self.name}: PWL needs at least two points")
            pairs = " ".join(f"{_fmt(t)} {_fmt(v)}" for t, v in self.points)
            return f"{self.name} {self.terminals[0]} {self.terminals[1]} PWL({pairs})"
        if self.kind == "sine":
            order = ("offset", "ampl", "freq", "td", "theta", "phi")
            values = " ".join(_fmt(self.params[key]) for key in order if key in self.params)
            return f"{self.name} {self.terminals[0]} {self.terminals[1]} SINE({values})"
        if self.kind == "ac":
            values = " ".join(
                _fmt(self.params[key]) for key in ("ampl", "phase") if key in self.params
            )
            return f"{self.name} {self.terminals[0]} {self.terminals[1]} AC {values}"
        raise ValueError(f"unsupported source kind {self.kind!r}")  # pragma: no cover

    @staticmethod
    def dc(name: str, plus: str, minus: str, volts: float) -> Source:
        return Source(name=name, terminals=(plus, minus), kind="dc", params={"v": volts})

    @staticmethod
    def ramp(
        name: str,
        plus: str,
        minus: str,
        *,
        v0: float,
        v1: float,
        delay_s: float = 0.0,
        rise_s: float = 1e-3,
        hold_s: float = 1.0,
    ) -> Source:
        """An explicit linear ramp: ``v0`` until ``delay_s``, then a real rise time.

        A zero-width step would hide every slew-rate and inrush question, so the
        rise time is always explicit.
        """
        if rise_s <= 0:
            raise ValueError("rise_s must be > 0; use an explicit slope, not an ideal step")
        # PWL requires monotonically increasing times: the flat pre-delay point is
        # only emitted when there is an actual delay, and the hold point only when
        # it extends the trace.
        points: list[tuple[float, float]] = [(0.0, v0)]
        if delay_s > 0:
            points.append((delay_s, v0))
        points.append((delay_s + rise_s, v1))
        if hold_s > 0:
            points.append((delay_s + rise_s + hold_s, v1))
        return Source(name=name, terminals=(plus, minus), kind="pwl", points=tuple(points))

    @staticmethod
    def step(
        name: str, plus: str, minus: str, *, v0: float, v1: float, at_s: float, rise_s: float
    ) -> Source:
        return Source.ramp(name, plus, minus, v0=v0, v1=v1, delay_s=at_s, rise_s=rise_s)

    @staticmethod
    def pulse(
        name: str,
        plus: str,
        minus: str,
        *,
        v1: float,
        v2: float,
        delay_s: float = 0.0,
        rise_s: float = 1e-6,
        fall_s: float = 1e-6,
        width_s: float = 1e-3,
        period_s: float = 2e-3,
    ) -> Source:
        return Source(
            name=name,
            terminals=(plus, minus),
            kind="pulse",
            params={
                "v1": v1,
                "v2": v2,
                "td": delay_s,
                "tr": rise_s,
                "tf": fall_s,
                "pw": width_s,
                "per": period_s,
            },
        )


@dataclass(frozen=True)
class Include:
    """An ``.include``/``.lib`` card. Paths are resolved at deck-write time."""

    path: str
    section: str | None = None
    absolute: bool = True

    def card(self, deck_dir: Path) -> str:
        del deck_dir  # absolute includes only: the deck must not depend on cwd
        target = Path(self.path)
        if self.absolute:
            target = target.resolve()
        text = str(target)
        if self.section:
            return f".lib {text} {self.section}"
        return f".include {text}"


@dataclass(frozen=True)
class TranSpec:
    """A transient analysis card."""

    tstep: float
    tstop: float
    tstart: float | None = None
    tmax: float | None = None
    uic: bool = False

    def card(self) -> str:
        parts = [".tran", _fmt(self.tstep), _fmt(self.tstop)]
        if self.tstart is not None:
            parts.append(_fmt(self.tstart))
            if self.tmax is not None:
                parts.append(_fmt(self.tmax))
        elif self.tmax is not None:
            raise ValueError("tmax requires tstart")
        if self.uic:
            parts.append("uic")
        return " ".join(parts)


@dataclass(frozen=True)
class MeasSpec:
    """A ``.meas`` card (cross-check only; assertions read waveforms)."""

    name: str
    signal: str
    kind: Literal["find_at", "avg", "max", "min", "pp"] = "find_at"
    at_s: float | None = None
    from_s: float | None = None
    to_s: float | None = None

    def card(self) -> str:
        if self.kind == "find_at":
            if self.at_s is None:
                raise ValueError(f"{self.name}: find_at needs at_s")
            return f".meas TRAN {self.name} FIND {self.signal} AT={_fmt(self.at_s)}"
        if self.from_s is None or self.to_s is None:
            raise ValueError(f"{self.name}: {self.kind} needs from_s and to_s")
        keyword = {"avg": "AVG", "max": "MAX", "min": "MIN", "pp": "PP"}[self.kind]
        return (
            f".meas TRAN {self.name} {keyword} {self.signal} "
            f"FROM={_fmt(self.from_s)} TO={_fmt(self.to_s)}"
        )


@dataclass(frozen=True)
class DeckSpec:
    """A complete deck: everything the simulator needs, written out explicitly."""

    title: str
    includes: tuple[Include, ...] = ()
    sources: tuple[Source, ...] = ()
    elements: tuple[str, ...] = ()
    directives: tuple[str, ...] = ()
    tran: TranSpec | None = None
    save: tuple[str, ...] = ()
    meas: tuple[MeasSpec, ...] = ()
    initial_conditions: Mapping[str, float] = field(default_factory=dict)
    options: Mapping[str, str | float] = field(default_factory=dict)
    temperature_c: float | None = None
    backanno: bool = True

    def render(self, deck_dir: Path) -> str:
        lines: list[str] = [f"* {self.title}"]
        for include in self.includes:
            lines.append(include.card(deck_dir))
        for key, value in self.options.items():
            lines.append(f".options {key}={value}")
        if self.temperature_c is not None:
            lines.append(f".options temp={_fmt(self.temperature_c)}")
        for directive in self.directives:
            # A comment stays a comment; anything else is a directive card.
            if directive.startswith((".", "*")):
                lines.append(directive)
            else:
                lines.append(f".{directive}")
        for source in self.sources:
            lines.append(source.card())
        lines.extend(self.elements)
        if self.initial_conditions:
            pairs = " ".join(
                f"V({node})={_fmt(value)}" for node, value in self.initial_conditions.items()
            )
            lines.append(f".ic {pairs}")
        if self.tran is not None:
            lines.append(self.tran.card())
        if self.save:
            lines.append(".save " + " ".join(self.save))
        for meas in self.meas:
            lines.append(meas.card())
        if self.backanno:
            lines.append(".backanno")
        lines.append(".end")
        return "\n".join(lines) + "\n"

    def with_extra(self, **changes: object) -> DeckSpec:
        """Copy with fields replaced (used by mutations and corner sweeps)."""
        return DeckSpec(**{**self.__dict__, **changes})  # type: ignore[arg-type]


def write_deck(deck: DeckSpec, path: str | Path) -> Path:
    """Write ``deck`` to ``path`` and return the path.

    The deck's own directory is used to resolve relative includes, so the deck is
    self-contained when copied/exported.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(deck.render(target.parent), encoding="utf-8", newline="\n")
    return target


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(text: str, values: Mapping[str, float | str]) -> str:
    """Substitute ``{{name}}`` placeholders in a deck template.

    Unknown placeholders and leftover braces raise: a template that silently
    keeps ``{{rail}}`` would produce a deck that simulates the wrong thing.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return _fmt(values[key])

    rendered = _PLACEHOLDER_RE.sub(replace, text)
    if missing:
        raise TemplateError(f"template placeholders without values: {sorted(set(missing))}")
    leftovers = re.findall(r"\{\{.*?\}\}|\}\}", rendered)
    if leftovers:
        raise TemplateError(f"template contains unrendered placeholders: {leftovers[:4]}")
    return rendered


def load_template(path: str | Path, values: Mapping[str, float | str] | None = None) -> str:
    """Read a committed ``tests/decks/*.cir`` template and render it."""
    text = Path(path).read_text(encoding="utf-8")
    if values is None:
        return text
    return render_template(text, values)


def deck_from_template(
    template_path: str | Path,
    out_path: str | Path,
    values: Mapping[str, float | str] | None = None,
) -> Path:
    """Render a template into a runnable deck file."""
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(load_template(template_path, values), encoding="utf-8", newline="\n")
    return target


def save_signals(signals: Iterable[str]) -> tuple[str, ...]:
    """Deterministic ``.save`` list (sorted, de-duplicated)."""
    return tuple(sorted({s for s in signals if s}))
