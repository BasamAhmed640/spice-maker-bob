# M4b1: cited TPS54332DDA values in SW and AVG

**Result:** The four selected rows have measured clean PASS and deliberate
wrong-value FAIL controls in both modes, in both editions. Each edition ran 16
LTspice decks: 16 `MEASURED`, zero `UNKNOWN`, zero run failures, and zero
clean/fault control violations. This is a nominal 25 °C qualification slice;
M4b and M4 are still open.

The [general](general.json) and [Bob](bob.json) records hold all 32 per-run
deck, LTspice log, raw-waveform, and operating-point hashes, cited row data,
windows, waveform measurements, state checks, and verdicts. The raw files were
retained in each repository's ignored `runs/` directory for inspection; they
are not part of this committed evidence. All 16 raw and log hashes in each
edition were independently checked against those local files. The two editions
produced identical numeric measurements and verdicts for each matching case;
their deck and raw artifacts are recorded separately.

## Source and measurement method

The four rows come from the frozen [TI TPS54332 Rev. D datasheet](https://www.ti.com/lit/ds/symlink/tps54332.pdf),
PDF page index 5 (printed page 6), with verified citations in the local
requirements. The runner pinned the requirements SHA-256
`3884fd76976e071cd2ea81d5db64cb1bde17aecbaccc8f87399e7448646cc060`,
bindings SHA-256
`1b3d45e8977948ce2ed7f1079db2aaa598b543cd65a83b697af8455d6345ad57`,
and spec digest
`499ed2442781bd4cad22041e7cb028bb6af9ec3df7bd34fe053750b9782b14d7`.
The fresh SW library SHA-256 remained
`21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2`;
the AVG library was
`adce082491d0035c6514aaa9b3ada916f75bcd998da4731caebe58fa2294d0dc`.
Both editions used the same script SHA-256
`4bc00c93201bdacab6b93670d9e50ee064a660ee99dbab1acd5f122ad5224b26`
and an explicitly selected LTspice executable, SHA-256
`4ef4a32accc2eb51722916b9410e3eedb5eebfbf0b661523caee2ab67b854207`.
The script disables network access before importing the model code.

The SW and AVG fixtures use the M2 startup circuit's 2.5 µH inductor, 15 nF
SS capacitor, feedback divider, output capacitors, and load. AVG receives the
external inductor value through its instance `L_EXT` parameter. VREF and SS
use the real feedback divider. Operating non-switching current uses the
datasheet's separately forced `VSENSE = 0.85 V` condition. For both current
rows, a zero-volt shunt sits between the input capacitor and DUT VIN pin;
positive `I(Vdutvin)` is DUT input current and excludes the input capacitor's
charging current and the separate EN source.

The evaluator requires waveform evidence for the requested electrical state
before applying a numeric band. VREF needs a settled, divider-consistent
output with SS above reference and positive delivered current. SS current is
derived from the measured time for `V(SS)` to rise from 0.35 to 0.45 V:
`15 nF × 0.10 V / crossing time`, centered on the cited 0.4 V condition.
The shutdown window is 0.5–1.0 ms; the VREF and operating-current windows
are 7.71–8.21 ms. Current measurements require stable input draw, quiet PH,
and less than 10 mA magnitude through the output inductor. Shutdown further
requires EN, SS, and output low. Operating current requires EN high, completed
SS, and `VSENSE ≈ 0.85 V`. These state guards are fixture checks, not extra
datasheet limits. AVG's state check uses external PH and inductor waveforms;
an internal duty trace is optional diagnostic information.

## Measured clean and fault controls

The values below are from General; Bob's corresponding measured values match.
`PASS` and `FAIL` are the evaluator's cited-row verdicts after the state gates
passed. The deliberate instance overrides leave the acceptance bands fixed.

| Cited row and comparison | SW clean | AVG clean | Fault override | SW fault | AVG fault |
| --- | ---: | ---: | --- | ---: | ---: |
| `B002_TPS54332DDA_VREF`: 0.772–0.828 V | 0.799933 V PASS | 0.799932 V PASS | `VREF=0.72` | 0.719933 V FAIL | 0.719933 V FAIL |
| `B002_TPS54332DDA_SS_CHARGE`: 2 µA typical, fixture guard 1.8–2.2 µA | 1.99920 µA PASS | 1.99920 µA PASS | `ISS=1u` | 0.999198 µA FAIL | 0.999199 µA FAIL |
| `B002_TPS54332DDA_ISHDN`: 1 µA typical; 4 µA maximum at EN = 0 V, VIN = 12 V | 1.00121 µA PASS | 1.00000 µA PASS | `EN_PULLUP=10u` | 10.0012 µA FAIL | 10.0000 µA FAIL |
| `B002_TPS54332DDA_IOP_NONSW`: 82 µA typical; 120 µA maximum at VSENSE = 0.85 V | 83.0071 µA PASS | 83.0059 µA PASS | `IQOP=200u` | 201.007 µA FAIL | 201.006 µA FAIL |

The VREF verdict uses the cited minimum and maximum. The SS row has no cited
minimum or maximum; its ±10% typical band is a declared **synthetic test
guard**, not a TI tolerance. For each IQ row, the PASS/FAIL verdict compares
with the cited maximum, while the record separately compares the measured
value with ±10% of typical. All four clean IQ comparisons are within that
typical band; a value below the maximum alone would not prove typical
accuracy. The shutdown citation also names −40 °C to 85 °C, but these decks
ran at 25 °C only and do not establish that temperature range or process
corners.

All 16 runs per edition had a zero LTspice exit code, observable simulator
output, a `READY` state, and nonempty deck/log/raw hashes. The two manifests
have SHA-256
`c8a73500b26982c882e7c3e6bfa3ed78d48073d38c7e85215be6593b286d5280`
(General) and
`c11685d784475f89950d80ff1a4237be8537005cf90be6724e0e430ec5870ced`
(Bob). These hashes identify the exact machine-readable evidence summarized
here.

## Scope and open work

This result does not promote the full TPS54332 model or M4b to PASS. The
bidirectional UVLO/EN, gain/current-limit corners, switching shapes, power
behavior, and matched mode-speed acceptance remain for M4b2/M4b3. AVG ripple
and PH-edge properties still require `UNKNOWN` rather than inferred zero.
The existing [M1 comparison](https://github.com/BasamAhmed640/spice-maker/blob/main/docs/evidence/2026-09-25-system-models-m1/REPORT.md)
retains its TPS output-ripple **FAIL** and PH-edge **UNKNOWN**. This M4b1 run
did not rerun or change the frozen full TPS regression verdicts.

## Integration checks

The new focused evaluator tests passed 10/10 in each edition. The existing
integration command passed 269 tests with 11 expected skips in General
(138.92 s) and the same 269/11 in Bob (146.32 s), using the explicit
LTspice path. `ruff check src tests tools`, changed-file formatting,
`git diff --check`, and the 44-file shared-core comparison passed in both
editions. The unchanged model and shared harness did not trigger another
frozen 19-row TPS or LM358 regression; M4a records those prior results.

These integration checks do not extend the four cited-row verdicts to full
model or M4b acceptance.
