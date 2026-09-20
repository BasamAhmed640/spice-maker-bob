"""Measure real LTspice validation/replay with an explicitly synthetic author."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from tests.authoring.test_fast_io import BUFFER, io_spec

from boardmodeler.authoring.backends import ScriptedBackend
from boardmodeler.authoring.loop import BuildRequest, build_model
from boardmodeler.simulation.ltspice import locate


def main():
    install = locate()
    if install is None:
        raise SystemExit("LTspice is required; no simulated timing will be reported")
    with tempfile.TemporaryDirectory(prefix="spice-benchmark-") as name:

        def write(turn, workdir, prompt):
            (workdir / "model/IO.lib").write_text(BUFFER)

        backend = ScriptedBackend(write)
        request = BuildRequest(
            part="SYNTHETIC_IO",
            subckt="IO",
            spec=io_spec(),
            workdir=Path(name),
            ltspice=install.path,
            backend=backend,
        )
        results = []
        for label in ("first", "repeat"):
            started = time.perf_counter()
            result = build_model(request)
            results.append(
                {
                    "run": label,
                    "seconds": round(time.perf_counter() - started, 4),
                    "status": result.status,
                    "author_turns": result.iterations,
                    "passed_probes": result.report.counts()["PASS"],
                }
            )
        print(
            json.dumps(
                {
                    "scope": "Synthetic I/O model, real LTspice; excludes network/API latency and device qualification",
                    "simulator": str(install.path),
                    "results": results,
                },
                indent=2,
            )
        )
        return 0 if all(row["status"] == "PASS" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
