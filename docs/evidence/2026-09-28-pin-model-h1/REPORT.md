# Pin model, hour 1: viability gate, pin shell, alarms, datasheet digest

Date: 2026-09-28. Branch `pin-model` in both editions (general `spice-maker` and
`spice-maker-bob`), cut from main after the uncommitted M4b2 work was parked on
`wip/m4b2` in each repository. Nothing was pushed. No AI provider was called.
LTspice 26 at the explicitly selected path, one run at a time. Decision: [D-053](../../DECISIONS.md).

## Why

The committed LM358 passes 32 datasheet rows, yet it is not a model a user can drop into
a system simulation. Measured before any change (a probe deck, then the gate below):

| Condition | Committed LM358 | Real part |
| --- | --- | --- |
| 5 mA load on an output, supply current | 0.700 mA, unchanged | about 5.7 mA |
| 10 mOhm short of an output to ground / to VCC | 249.7 A / 499.5 A | 40 mA typ, 60 mA max |
| Floating input, 5 V supply | 19-21 V above the ground pin | inside the rails |

Passing rows is not viability. The purpose is models that sit correctly in someone else's
circuit, so the gate that measures that comes first.

## What was built (all four are shared or mirrored to Bob)

| Piece | File | Tests |
| --- | --- | --- |
| Viability gate: part-agnostic judge from ports + a pin table | `src/boardmodeler/authoring/viability.py` | `tests/authoring/test_viability.py` |
| Pin shell: every package pin as a port that is ready to go, by kind | `src/boardmodeler/models/pin_shell.py` | `tests/models/test_pin_shell.py`, `tests/models/proof_parts.py` |
| Alarms in the model (`chk_abs_*`, `chk_ovl_*`, `chk_flt_*`, `chk_tie_*`, `chk_any`) | `pin_shell.py` + the gate's alarm checks | same |
| Datasheet digest: table columns read from character positions | `src/boardmodeler/documents/digest.py` | `tests/documents/test_digest.py` |

The gate, digest, and their tests are in the shared core (46 files identical:
`tools/shared_core.py --compare ..\spice-maker-bob`). The pin shell and proof parts are
copied byte-identical.

## Results

### The gate bites: the committed LM358 fails five checks

`fixtures/models/lm358_committed_2026-09-24.lib` (sha256 `f823eb6c...`), 23 benches, 15 s:

| Check | Verdict | Measured |
| --- | --- | --- |
| `no_global_ground` | FAIL | 4 elements use node 0 / `GND` instead of a port |
| `current_conservation` | FAIL | currents into the pins sum to 998.8 A, not 0 |
| `output_short_limited` | FAIL | 499.4 A in a short (rating 0.06 A) |
| `supply_carries_output_current` | FAIL | outputs sank 998.8 A; the ground pins returned 0.7 mA |
| `floating_inputs_in_rails` | FAIL | inputs float 19-21 V above the ground pin on 5 V |

The other six checks pass. The locally saved 32-line build (`models/L1-lm358`, 2 turns) has the same
class of defect (its output current returns through the VEE pin, not VCC, with no limit) and fails
`output_short_limited`, `supply_carries_output_current` and `floating_inputs_in_rails`.

### The LM358 rebuilt on the pin shell passes, and keeps every datasheet row

`LM358_pin_shell.lib` (sha256 `3c60f025...`): pin shell + a 6-line op-amp core per channel,
built from the pin table in `tests/models/proof_parts.py`.

* Gate: **PASS, 28 benches, 21 s**, including 7 alarms (5 absolute-maximum, 2 overload), each
  quiet in clean use and firing on its fault bench.
* The frozen 19-case / 32-row LM358 spec (digest `0fb03ad8...`), re-judged by the existing harness:
  **32 PASS, 0 changed against the committed baseline, 16.3 s** (`lm358_shell_frozen_rows.json`).

### A part with no function passes too: MCU8 (pins only)

`MCU8_pin_shell.lib` (sha256 `9a4b6d82...`), a microcontroller-class pin table with a push-pull
output, an io pin, a pull-up reset input, an NC pin and a required exposed pad. Gate: **PASS,
9 benches, 6.6 s**, with all four alarm kinds proven (abs, ovl, flt, tie). Its outputs are inert until
the instance parameter `LEVEL_<pin>` commands them, which is how the gate exercises them.

### Every kind of broken model is caught (the tests)

* 7 static mutants (missing pin, extra port, ports swapped against the symbol, pad tied to ground,
  return through node 0, read of global `GND`, a source that reads its own output): each fails the
  check that names it; a cited tie is allowed.
* 6 dynamic mutants on the reference op amp (no current limit, output sourced from ground, return
  through node 0, unclamped floating input, no quiescent current, leaky NC pin): each caught.
* 5 alarm mutants on MCU8 (alarm absent, stuck on, and three that never fire): each caught.
* A report without dynamic benches is never PASS.

### The existing buck template, gated (informational; the template is frozen)

Pin table in the run script, 9 pins, 12 V supply, PH rated 6.5 A (datasheet current-limit typ):

| Mode | Verdict | Finding |
| --- | --- | --- |
| SW (default) | FAIL, 11 benches, 47 s | Passes 9 checks. `output_short_limited`: PH carried **28.86 A** with PH shorted to ground and the analog pins tied high (bound 13 A): the peak-current limit does not hold against a dead short during minimum on-time. |
| AVG | FAIL, 11 benches, 122 s | `supply_carries_output_current`: PH sources 5.2 A while VIN supplies 0.29 A (duty about 5.6%); the difference returns through the ground pin, which a non-synchronous part does not do. One bench (`short_vcc_high`) wrote a 186 MB waveform LTspice's reader could not lay out, so `converges_in_every_pin_state` is UNKNOWN, not PASS. |

The AVG finding may be stricter than a first-order regulator needs. It is reported, not relaxed.

### The digest reads the three rows an extraction once misread

`tps54332_p6_digest.md` (22 rows). The raw text layer of the same rows carries no column:

    Internal undervoltage lockout threshold Rising and Falling 3.5 V
    Enable threshold Rising and Falling 1.25 1.35 V
    Current limit threshold VIN = 12 V 4.2 6.5 A

The digest measures each number against the MIN / TYP / MAX header positions:

| Row | MIN | TYP | MAX |
| --- | --- | --- | --- |
| Undervoltage lockout, rising and falling (V) | | | **3.5** |
| Enable threshold (V) | | 1.25 | 1.35 |
| Current limit threshold (A) | **4.2** | **6.5** | |

Also read correctly: VREF 0.772 / 0.8 / 0.828 V, on-resistance 115 / 200 and 80 / 150 mOhm with its
row-spanning label and unit, minimum on-time 110 / 135 ns, switching frequency 800 / 1000 / 1200 kHz.
On the LM358 datasheet it yields 125 rows; the numbers land in the right columns, while labels of
multi-line cells are fragmentary (for example `VS = 3 V to 36 V` standing in for the parameter). The
golden test needs `SPICE_MAKER_DATASHEET_DIR` and skips without it; a synthetic page with real-table
geometry runs everywhere.

## Regression check (general edition, fast subset)

`pytest -q -m "not ltspice and not network and not gui and not installer and not slow"`:

| | passed | failed | skipped | deselected |
| --- | --- | --- | --- | --- |
| Baseline on unmodified main | 1483 | 105 | 5 | 283 |
| After (final run, 86.9 s) | 1514 | 105 | 6 | 298 |

All 105 baseline failures are machine state, not code: the local `data/config.json` has
INTERNET ACCESS off, which fails 75 + 10 tests in `test_api_backend*`, 13 in `test_make_model`,
3 in `test_sanity`, 2 key-check and 2 desktop-retry tests. The failing list is identical after this work (a comparison of the two lists shows no new
failure; the shared-core manifest test failed once until the manifest was regenerated). The
+31 passes are the new fast tests, +1 skip is the digest golden test without
`SPICE_MAKER_DATASHEET_DIR`, and +15 deselected are the new LTspice-marked tests.
The 15 new LTspice-marked tests run with `LTSPICE_EXE` set: 41 gate and shell tests pass in both
editions (about 145 s each), and 6 digest tests pass in both.

## Not done, and what is unproven

* No buck, LDO or other switching or regulating part has been rebuilt on the shell. Only an op amp
  (function core) and a pin-only part are proven.
* `part_class.py` still refuses microcontrollers and FPGAs; routing them to the shell belongs with
  the default build path (M7), which does not exist yet. `make_model.py` was not touched.
* The pin table is written by hand in `tests/models/proof_parts.py`; nothing yet builds one from
  the datasheet digest or asks for pinout confirmation (M6).
* The gate's supply-accounting check assumes the output stage is not a switching node.
* Bands: output overload uses the same cited current limit the output stage uses; the required-
  connection alarm depends on a 1 nA probe current (D-053 supersedes D-050's "no diagnostic
  current" for pins the pin table marks required).
* Not tested on other vendors' datasheets; the digest is proven on two TI parts.

## Reproduce

    set LTSPICE_EXE=<path to LTspice.exe>
    .venv\Scripts\python.exe -m pytest tests\authoring\test_viability.py tests\models\test_pin_shell.py tests\documents\test_digest.py -q
    (digest golden test: also set SPICE_MAKER_DATASHEET_DIR to a folder holding tps54332_datasheet.pdf)

Artifacts: `HASHES.txt` lists the sha256 of every deck, log and `.raw` behind these results (408
files); the `.raw` files stay local and are not committed.
