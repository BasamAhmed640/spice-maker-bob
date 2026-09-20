# Model creation and validation

The selected provider now reads the datasheet and authors the model using the same
API key. Four extraction tasks share one document context and one response. The
actual record schemas describe citations, pins, conditions and expressions; invalid
records get one bounded correction attempt; remaining errors stop extraction. GO discloses transmission to the selected provider.
Explicit CLI builds need `--allow-remote`. Keys remain in the OS credential store.

Repairs receive the current model and measured feedback. A numeric error reduction
counts as progress even if the same row still fails. The loop retains its best
candidate, saves each attempted revision with duration/usage/report, and stops on
stalls or a requested turn limit. Bob uses stdin for long prompts, reads `last_message`
for extraction, and resumes the exact authoring task on repair turns. It never resumes
an extraction task as an authoring task.

Identical successful builds use the extraction cache and a validation cache keyed by
model, specification, engine, Python/NumPy version, simulator executable and timeout.
A cache entry is only reused inside the process that observed the harness run that
produced it: the report, raw and log on disk are writable by the authoring agent, so a
checksum stored beside them cannot bind them to a model. Before any in-process reuse,
raw/log hashes, completed waveforms and measurements are checked against the frozen
limits again, and the on-disk report must match the observed one. A fresh process finds
no receipt, so it re-judges an existing candidate with one LTspice run before deciding
PASS — still skipping every author/API turn. Changed or missing evidence causes a fresh
simulation, and UNKNOWN results, models with external includes and files that are not
valid UTF-8 are never reused. Keep the original build directory: cached artifacts use
absolute paths. This accelerates repeats; it is not a guarantee about first-build
provider latency or success on arbitrary parts.

## Electrical I/O coverage

The original 11 regulator probes remain. Nine additional probes cover VOH, VOL, input
leakage, disabled-output leakage, power-off leakage, rising/falling propagation delay,
and rise/fall time. The logical view uses VCC, A, Y and GND; OE is also required for
disabled-output tests. A physical pin map is preserved alongside that reduced view.
Separate datasheet operating points produce separate decks, never one merged corner.

Conditions use SI-valued `parameter_overrides`: `io_vcc`, `io_input_high`, `io_load_a`,
`io_cap_f`, `io_test_v`, `io_inverting` (0/1), `io_oe_active_high` (0/1). The binder
requires the applicable supply, load and polarity conditions; missing/contradictory
conditions remain untestable. A row may take its output polarity from a *verified*
verbatim citation on a neighbouring row for the same part and document when that excerpt
names this row's signal; the binding records the source requirement and excerpt under
`derived_conditions`, and an unverified citation, another part/document, an unrelated
signal or conflicting evidence leaves the row a declared gap. Timing binding also
requires the input edge (`io_edge_s`)
and rise/fall-time binding requires the low/high fractions (`io_low_frac`, `io_high_frac`).
Propagation delay binding requires the cited input/output fractions (`io_input_frac`, `io_output_frac`). Every actual deck parameter is in the report.
Nominal 25 C behavior does not qualify a full temperature range. PASS applies only to
covered rows at the recorded conditions; uncovered requirements remain visible.

These probes have been exercised on **synthetic** buffers with real LTspice, including
an output-resistance perturbation that must fail. Such tests establish harness behavior,
not device accuracy. Device qualification still requires cited data and relevant tests.

## High-speed I/O sources

PCIe/USB/Ethernet channel and protocol accuracy cannot be established by these reduced
LTspice probes. Preserve the vendor's IBIS/AMI/Touchstone data instead:

```powershell
uv run boardmodeler model import --file vendor.s4p --part PART_NUMBER `
  --source-url https://vendor.example/models/part --license-note "Local use; see vendor terms" `
  --out build/channel --json
```

This copies the original bytes into a content-addressed directory, records attribution,
licensing notes and declared format metadata, and writes the required external validation
step. It performs only basic recognition, **not** standards conformance or electrical
validation. Attribution URLs are caller-supplied and explicitly unverified. AMI binaries
are not loaded. Electrical status stays UNKNOWN until a compatible external simulator
has established the requested channel behavior. No inferred eye/BER/protocol PASS is added.

## Shared editions and downloads

`build_flavor.py` selects the Bob-only catalog. Both repositories otherwise use the same
source, tests and packaging. `python tools/sync_shared_core.py <other-checkout>` checks
drift; `--apply` copies shared files while retaining that flavor. The backend and setup
reject providers absent from the edition's catalog.

`installer/build.ps1 -Version 1.1.0` builds the animated Velopack installer and an easy
download ZIP containing **Install.exe**, a short readme and SHA256SUMS.txt. Install.exe
is byte-identical to the normal Setup.exe, so the existing pepper animation is preserved.
Python is bundled; LTspice and Bob Shell remain separate prerequisites. The two editions
use separate package IDs/install directories. GitHub's Windows download workflow can
rebuild the ZIP from source. Builds are unsigned unless a signing step is configured.
