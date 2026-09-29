# Engine refactor acceptance — 2026-09-29

Edition: **Bob**. New window, CLI and API builds default to `behavioral`: cited source
records become a typed design and locally rendered SPICE, then real LTspice supplies the verdicts.
AI extraction is the only default AI stage. These final replays followed citation/provenance/status hardening, used local evidence and recorded
**zero provider calls with complete accounting**. They do not exercise live inference.

## Default-route observations

| Part and source | Public status | Ordinary row counts | Supplemental buck qualification |
| --- | --- | --- | --- |
| LM358, exact reviewed PDF | PASS | 32 PASS / 10 N/A | Not applicable |
| TPS54332DDA, local cited source records and PDF | UNKNOWN | 12 PASS / 4 FAIL / 41 UNKNOWN / 49 N/A | 4 PASS / 12 UNKNOWN; `family_qualified=false` |
| TPS54331, exact reviewed PDF | BLOCKED | Refused before authoring | No model delivered |

TPS54331 still lacks an accepted cited nominal UVTH, independent switching-frequency,
soft-start and enable/UVLO coverage, and resolved D versus DDA packaging. DDA requires PowerPAD
pin 9. The reviewed source keeps VIN UVLO MAX 3.5 V distinct from a missing typical value;
the gate is not relaxed to publish a model.

## Delivered identity

| Artifact | SHA-256 |
| --- | --- |
| LM358 library | `67766c0cf0d6ce85b9042df94ce4766deb264e917988df1e45d782e9fbaabed2` |
| TPS54332DDA library | `21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2` |
| This edition's frozen buck qualification plan | `331fd872052ffb0491b2c792276ad0c01a49196b0d5d629e6d490d8a9df1c71c` |

The libraries have exact typed-design associations. The ordinary harness report matches the
delivered library hash and frozen spec; the buck qualification report identifies that same
delivered library. The plan froze before candidate generation. General and Bob delivered identical
library bytes; their plan hashes differ because each includes its edition's simulator implementation
hashes. Symbol hashes and pin order are recorded in each local `model-design.json`.

## Observed time and calls

| Part | Extraction (s) | Compile (s) | Simulation (s) | Total (s) | Provider calls |
| --- | --- | --- | --- | --- | --- |
| LM358 | 5.513 | 0.001 | 15.648 | 26.158 | 0 |
| TPS54332DDA | 2.104 | 0.009 | 158.944 | 164.024 | 0 |
| TPS54331 | 2.083 | — | — | 4.272 | 0 |

Buck simulation includes 113.779 s ordinary judgment and 45.164 s for the four default
qualification simulations. Total time also includes reading, binding, gates and publication.
These are observed local timings, not fixed performance guarantees.

## Receipts, regression and limits

The planned M6 gate is still open: source-backed confirmation of package selection, symbol pin
numbers and discrete terminal order before publication. Current byte/pin-order records and the
TPS54331 package guard do not complete it. These measurements are a bounded engine milestone.

Citation replay now saves this run's DOCUMENT citation checks before freezing new requirements
and qualification sources. Missing text, a failed excerpt or a missing verifier result clears
a stale true flag and leaves affected qualification checks as gaps. Historical inputs stay intact.
Resumed buck/op-amp designs are reconstructed and rendered before retaining an exact association.
Schema/type, design/library hashes, part, subcircuit and frozen spec must match; an edited payload
cannot keep an exact claim merely because library bytes are unchanged. Invalid provenance supplies
no exact design hash to qualification. Supplemental BLOCKED is publicly BLOCKED with its refusal
reason; fixed FAIL stays FAIL, while fixed UNKNOWN downgrades an ordinary PASS.

Local, git-ignored receipts are under `runs/engine-acceptance-opamp/`,
`runs/engine-acceptance-buck/` and `runs/engine-acceptance-tps54331/`. They include
`results.json`, `run-timing.json`, support decisions, and model/design/harness/qualification
records where applicable; immutable simulator decks/raw/logs are retained under each run's
`build/` tree. Vendor PDFs and raw simulator files are not included in this committed summary.

A separate real-LTspice replay of frozen historical 19-row inputs retained TPS54332
12 PASS / 4 FAIL / 3 UNKNOWN and LM358 19 PASS with `changed_rows={}`; its receipts are
in the general checkout's `runs/recheck-frozen/`. Those inputs are distinct from the current
106-row buck extraction and must not be used as its coverage count. The earlier independent
wrong-candidate test in `runs/qualification-acceptance/` rejected VREF=0.72 V against the
unchanged 0.772–0.828-V band.

Offline suite: `pytest -q -m 'not ltspice and not network'` → **1961 passed, 31 skipped, 187 deselected in 76.09 s; four pre-existing unregistered `slow` marker warnings**.

Shared-core checks found 49 intact files per edition, all 49 identical. Repo-wide Ruff check,
Ruff format check and diff whitespace checks passed in both editions.

The twelve mandatory buck qualification gaps remain UNKNOWN, TPS54331 remains BLOCKED, and
the ordinary buck failures remain visible. LM358 PASS covers the tested characteristics only;
temperature, statistical behavior and broader operating conditions are not established.
No universal coverage, whole-family qualification, live provider acceptance or rebuilt installer
release is claimed. See [current status](../../STATUS.md) and [handoff](../../HANDOFF.md).
