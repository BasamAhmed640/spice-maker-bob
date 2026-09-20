"""Native reader for LTspice ``.raw`` waveform files (D7).

Empirically resolved on LTspice 26.0.0.3 (see ``docs/DECISIONS.md`` D-002/D-007):

* The header text is **UTF-16LE** (no BOM) for binary runs and UTF-16LE as well
  for ``-ascii`` runs; older releases wrote plain ASCII. Encoding is detected
  from the bytes, never assumed.
* The header ends with a line ``Binary:`` or ``Values:`` followed by a single
  newline; the payload starts immediately after it.
* Binary payload: for each point, one IEEE-754 **double** for variable 0,
  then one **float32** per remaining variable. No padding is inserted for the
  default (non-FastAccess) layout; the stride is therefore ``8 + 4*(nvars-1)``.
  The reader does not assume this: it selects the layout whose stride accounts
  for the payload size **exactly**, and reports the choice in ``RawFile.layout``.
* ``-ascii`` payload: one line per point holding ``<index>\\t<value0>`` followed
  by one line per remaining variable.
* Stepped runs (``Flags: ... stepped``) concatenate every step into one file
  with no step index in the header, so they are rejected rather than silently
  merged. BoardModeler runs one deck per corner.
* Complex (AC) payloads are rejected with an explicit error; the caller reports
  BLOCKED rather than pretending the data is real.

A layout that cannot be identified raises :class:`RawFormatError` — a run is
never reported with a zero-filled array.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = ["RawFile", "RawFormatError", "read_raw", "read_raw_spicelib"]

_HEADER_LIMIT = 1 << 20
_VAR_RE = re.compile(r"^\s*(?P<index>\d+)\s+(?P<name>\S+)\s+(?P<type>\S+)\s*$")


class RawFormatError(RuntimeError):
    """Raised when a ``.raw`` file cannot be read without guessing."""


@dataclass(frozen=True)
class RawFile:
    """Waveform data with the header facts that produced it."""

    path: Path | None
    plotname: str
    flags: list[str]
    variables: list[str]
    data: np.ndarray
    variable_types: list[str] = field(default_factory=list)
    complex_data: bool = False
    points_per_step: list[int] = field(default_factory=list)
    header_encoding: str = "utf-16-le"
    layout: str = "double+float32"
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def npoints(self) -> int:
        return int(self.data.shape[0])

    @property
    def nvars(self) -> int:
        return int(self.data.shape[1])

    def index(self, name: str) -> int:
        """Column index of ``name`` (exact match first, then case-insensitive)."""
        if name in self.variables:
            return self.variables.index(name)
        lowered = name.lower()
        for i, var in enumerate(self.variables):
            if var.lower() == lowered:
                return i
        raise KeyError(f"{name!r} not in {self.variables}")

    def column(self, name: str) -> np.ndarray:
        return self.data[:, self.index(name)]

    def time_column(self) -> np.ndarray | None:
        """The ``time`` variable if this is a transient/AC plot, else ``None``."""
        for i, var_type in enumerate(self.variable_types):
            if var_type == "time":
                return self.data[:, i]
        if self.variables and self.variables[0].lower() == "time":
            return self.data[:, 0]
        return None

    def has(self, name: str) -> bool:
        try:
            self.index(name)
        except KeyError:
            return False
        return True


# --------------------------------------------------------------------------- #
# header


@dataclass(frozen=True)
class _Header:
    fields: dict[str, str]
    variables: list[str]
    variable_types: list[str]
    plotname: str
    flags: list[str]
    nvars: int
    npoints: int
    encoding: str
    mode: str
    data_start: int


def _detect_encoding(data: bytes) -> str:
    if len(data) >= 4 and data[0] != 0 and data[1] == 0:
        return "utf-16-le"
    return "utf-8"


def _parse_header(data: bytes) -> _Header:
    encoding = _detect_encoding(data)
    bytes_per_char = 2 if "16" in encoding else 1
    prefix = data[:_HEADER_LIMIT].decode(encoding, errors="replace")

    mode = ""
    marker_at = -1
    for candidate in ("Binary:", "Values:"):
        found = prefix.find("\n" + candidate)
        if found != -1 and (marker_at == -1 or found < marker_at):
            marker_at = found
            mode = "binary" if candidate == "Binary:" else "values"
    if marker_at == -1:
        raise RawFormatError(
            "no 'Binary:' or 'Values:' section found in the first "
            f"{_HEADER_LIMIT} bytes; is this an LTspice .raw file? "
            f"(first bytes: {data[:64]!r})"
        )

    newline_len = len("\nBinary:\n") if mode == "binary" else len("\nValues:\n")
    header_text = prefix[: marker_at + newline_len]
    if not header_text.endswith("\n"):
        header_text += "\n"
    data_start = (marker_at + newline_len) * bytes_per_char

    fields: dict[str, str] = {}
    variables: list[str] = []
    variable_types: list[str] = []
    in_variables = False
    for raw_line in header_text.splitlines():
        line = raw_line.rstrip("\r")
        if in_variables:
            match = _VAR_RE.match(line)
            if match:
                variables.append(match.group("name"))
                variable_types.append(match.group("type"))
                continue
            in_variables = False
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            fields[key] = value
            if key == "Variables":
                in_variables = True

    try:
        nvars = int(fields["No. Variables"])
        npoints = int(fields["No. Points"])
    except KeyError as exc:
        raise RawFormatError(f"header is missing {exc.args[0]!r}: keys={sorted(fields)}") from exc
    if nvars <= 0:
        raise RawFormatError(f"header declares {nvars} variables")
    if len(variables) != nvars:
        raise RawFormatError(
            f"header declares {nvars} variables but lists {len(variables)}: {variables}"
        )

    flags = fields.get("Flags", "").lower().split()
    plotname = fields.get("Plotname", "")
    return _Header(
        fields=fields,
        variables=variables,
        variable_types=variable_types,
        plotname=plotname,
        flags=flags,
        nvars=nvars,
        npoints=npoints,
        encoding=encoding,
        mode=mode,
        data_start=data_start,
    )


# --------------------------------------------------------------------------- #
# payload


def _align8(value: int) -> int:
    return (value + 7) // 8 * 8


def _layout_candidates(nvars: int) -> list[tuple[str, int]]:
    """``(name, stride)`` in preference order."""
    return [
        ("double+float32", 8 + 4 * (nvars - 1)),
        ("double+float32-align8", _align8(8 + 4 * (nvars - 1))),
        ("float32", 4 * nvars),
        ("float32-align8", _align8(4 * nvars)),
        ("float64", 8 * nvars),
    ]


def _select_layout(nvars: int, npoints: int, payload_size: int) -> tuple[str, int]:
    matches = [
        (name, stride)
        for name, stride in _layout_candidates(nvars)
        if stride * npoints == payload_size
    ]
    if not matches:
        expected = ", ".join(
            f"{name}={stride * npoints}" for name, stride in _layout_candidates(nvars)
        )
        raise RawFormatError(
            f"payload of {payload_size} bytes does not match any known layout for "
            f"{npoints} points x {nvars} variables (candidates: {expected})"
        )
    return matches[0]


def _decode_binary(payload: bytes, nvars: int, npoints: int) -> tuple[np.ndarray, str]:
    layout, stride = _select_layout(nvars, npoints, len(payload))
    if stride == 0 or npoints == 0:
        return np.zeros((npoints, nvars), dtype=np.float64), layout

    if layout.startswith("double+float32"):
        if stride % 4 != 0:  # pragma: no cover - alignment layouts are multiples of 4
            raise RawFormatError(f"unsupported stride {stride}")
        words = np.frombuffer(payload, dtype="<u4", count=npoints * (stride // 4)).reshape(
            npoints, stride // 4
        )
        bits = words[:, 0].astype(np.uint64) | (words[:, 1].astype(np.uint64) << np.uint64(32))
        out = np.empty((npoints, nvars), dtype=np.float64)
        out[:, 0] = bits.view(np.float64)
        if nvars > 1:
            nfloat = min(nvars - 1, stride // 4 - 2)
            out[:, 1 : 1 + nfloat] = words[:, 2 : 2 + nfloat].view("<f4")
        return out, layout

    if layout.startswith("float32"):
        kind = np.dtype("<f4")
        raw = np.frombuffer(payload, dtype=kind, count=npoints * (stride // 4)).reshape(
            npoints, stride // 4
        )
        return raw[:, :nvars].astype(np.float64), layout

    if layout == "float64":
        raw = np.frombuffer(payload, dtype="<f8", count=npoints * nvars).reshape(npoints, nvars)
        return raw.astype(np.float64), layout

    raise RawFormatError(f"unsupported layout {layout!r}")  # pragma: no cover


_NUM_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def _decode_values(text: str, nvars: int, npoints: int) -> np.ndarray:
    out = np.empty((npoints, nvars), dtype=np.float64)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    point = 0
    collected = 0
    for line in lines:
        tokens = line.replace("\t", " ").split()
        if not tokens:
            continue
        # A new point starts with its integer index as the first token.
        if (
            collected == 0
            and len(tokens) > 1
            and _NUM_RE.match(tokens[0])
            and float(tokens[0]).is_integer()
        ):
            tokens = tokens[1:]
        for token in tokens:
            if not _NUM_RE.match(token):
                raise RawFormatError(f"non-numeric value {token!r} in ASCII payload")
            if point >= npoints:
                raise RawFormatError(f"ASCII payload has more than the declared {npoints} points")
            out[point, collected] = float(token)
            collected += 1
            if collected == nvars:
                point += 1
                collected = 0
    if point != npoints or collected != 0:
        raise RawFormatError(
            f"ASCII payload holds {point} complete points (+{collected} values), "
            f"header declares {npoints}"
        )
    return out


# --------------------------------------------------------------------------- #
# public API


def read_raw(path: str | Path) -> RawFile:
    """Read an LTspice ``.raw`` file exported by this project's runner."""
    target = Path(path)
    data = target.read_bytes()
    header = _parse_header(data)

    if "stepped" in header.flags:
        raise RawFormatError(
            "stepped .raw files concatenate every step without an index in the header; "
            "run one deck per corner instead of using .step"
        )

    payload = data[header.data_start :]
    if "fastaccess" in header.flags:
        raise RawFormatError(
            "FastAccess column-major raw data is unsupported; save normal binary data"
        )
    if "complex" in header.flags:
        expected = header.npoints * header.nvars * 16
        if header.mode != "binary" or "fastaccess" in header.flags or len(payload) != expected:
            raise RawFormatError(
                "unsupported or truncated complex raw payload; expected binary complex128 points"
            )
        values = np.frombuffer(payload, dtype="<c16").reshape(header.npoints, header.nvars).copy()
        layout = "complex128"
    elif header.mode == "binary" and "double" in header.flags:
        expected = header.npoints * header.nvars * 8
        if len(payload) != expected:
            raise RawFormatError("double-precision raw payload length disagrees with its header")
        values = np.frombuffer(payload, dtype="<f8").reshape(header.npoints, header.nvars).copy()
        layout = "float64"
    elif header.mode == "binary":
        values, layout = _decode_binary(payload, header.nvars, header.npoints)
    else:
        values = _decode_values(
            payload.decode(header.encoding, errors="replace"), header.nvars, header.npoints
        )
        layout = "values"

    if values.shape != (header.npoints, header.nvars):
        raise RawFormatError(
            f"decoded shape {values.shape} does not match header ({header.npoints}, {header.nvars})"
        )
    if not np.all(np.isfinite(values)):
        bad = int(np.count_nonzero(~np.isfinite(values)))
        raise RawFormatError(f"decoded data contains {bad} non-finite values")

    return RawFile(
        path=target,
        plotname=header.plotname,
        flags=header.flags,
        variables=header.variables,
        variable_types=header.variable_types,
        data=values,
        complex_data="complex" in header.flags,
        points_per_step=[header.npoints],
        header_encoding=header.encoding,
        layout=layout,
        fields=header.fields,
    )


def read_raw_spicelib(path: str | Path) -> RawFile:
    """Optional ``spicelib`` backend with the same contract as :func:`read_raw`.

    Imported lazily so that the package works without the ``sim`` extra.
    """
    target = Path(path)
    try:
        from spicelib.raw.raw_read import RawRead  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        raise RawFormatError(f"spicelib is not importable: {exc}") from exc

    native_header = _parse_header(target.read_bytes())
    reader = RawRead(str(target))
    names = list(reader.get_trace_names())
    if not names:
        raise RawFormatError("spicelib returned no traces")
    axis = reader.get_axis()
    axis_name = getattr(axis, "name", None) or native_header.variables[0]
    axis_values = np.asarray(axis.get_wave(0), dtype=np.float64)
    columns = [axis_values]
    variables = [axis_name]
    for name in names:
        if name == axis_name:
            continue
        wave = reader.get_wave(name)
        columns.append(np.asarray(wave.get_wave(0), dtype=np.float64))
        variables.append(name)
    data = np.column_stack(columns)
    return RawFile(
        path=target,
        plotname=native_header.plotname,
        flags=native_header.flags,
        variables=variables,
        variable_types=["time" if v == axis_name else "unknown" for v in variables],
        data=data,
        complex_data=False,
        points_per_step=[data.shape[0]],
        header_encoding=native_header.encoding,
        layout="spicelib",
        fields=native_header.fields,
    )
