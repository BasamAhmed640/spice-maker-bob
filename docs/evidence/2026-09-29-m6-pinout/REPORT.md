# M6 pinout confirmation: source, implementation and acceptance

Status: scoped publication gate implemented in both editions; LM358 and TPS54331 acceptance
recorded below. Final buck replays and full offline suites are pending. No all-package or
whole-family qualification is claimed.

The source audit is retained locally at `work/m6-audit/pinout-source-audit.json`. Vendor PDFs and
rendered pages remain local. Each pair below consists of distinct TI primary documents from the
same manufacturer; this is not independent-manufacturer corroboration. Page indexes below are
zero-based, while printed page numbers are one-based.

## Reviewed scope

| Device / scope | Physical pins in numerical order | Required external connection |
| --- | --- | --- |
| TPS54332DDA, DDA | 1 BOOT; 2 VIN; 3 EN; 4 SS; 5 VSENSE; 6 COMP; 7 GND; 8 PH; 9 PowerPAD | PowerPAD9 to GND7 |
| LM358, equivalent eight-pin D / DGK / P / PS / PW group | 1 OUT1; 2 IN1−; 3 IN1+; 4 V−; 5 IN2+; 6 IN2−; 7 OUT2; 8 V+ | No exposed-pad terminal in this group |

The LM358 group establishes the common numbered electrical pinout. **No physical package body
or PCB footprint is selected.** The exact base-part membership comes from its datasheet's printed
page 1 Package Information table. DDF, JG and FK are not included in this LM358 group.

Reviewed second-source aliases are TPS54332 `VSNS → VSENSE`, `PwPd → PowerPAD`; and LM358
`1OUT → OUT1`, `1IN− → IN1−`, `1IN+ → IN1+`, `GND → V−`, `2IN+ → IN2+`,
`2IN− → IN2−`, `2OUT → OUT2`, `VCC → V+`. These aliases establish role identity,
not permission to reorder physical pins.

## Primary sources and exact identity

| Source | Revision | Reviewed page / location |
| --- | --- | --- |
| [TPS54332 datasheet](https://www.ti.com/lit/ds/symlink/tps54332.pdf) | SLVS875D, September 2023 | Printed p3 / index 2, Figure 5-1 and Table 5-1 |
| [TPS54332 EVM guide](https://www.ti.com/lit/pdf/SLVU276) | SLVU276A, October 2021 | Printed p13 / index 12, Figure 4-1, U1 TPS54332DDA |
| [LM358 datasheet](https://www.ti.com/lit/ds/symlink/lm358.pdf) | SLOS068AB, October 2024 | Printed p3 / index 2, Figure 4-1 and Table 4-1; printed p1 / index 0, Package Information |
| [LM358 application schematic](https://www.ti.com/lit/an/slva313/slva313.pdf) | SLVA313, December 2008 | Printed p5 / index 4, Figure 6, U1 LM358 |

SHA-256 values identify the exact reviewed revisions; the vendor URLs may later serve a new revision.

| Source | Complete PDF SHA-256 |
| --- | --- |
| TPS54332 datasheet | `af3d92b2c6656a1088a4f192a13165590a797427a0b7350abb8c64e50ad58a4a` |
| TPS54332 EVM guide | `f9860fdaa989d20b54591543b98cac94fd769e0472a4b0aa0a3b695f4809ed9a` |
| LM358 datasheet | `58c89c68aff6b6025555f3b8bad9af6a825288cd96631deadc1c616a6062af46` |
| LM358 application schematic | `1daf7b25be48c3cc4fdaefc72bda3df408e37f0192965c31d781fcd85104bf9f` |

Complete pages were visually reviewed from PNGs rendered with `pdftoppm`. Image SHA-256 values
also depend on renderer output; the audit records the following `-scale-to` settings.

| Reviewed page | Scale | Page-image SHA-256 |
| --- | ---: | --- |
| TPS54332 datasheet p3 | 1700 | `bb2fe38288bd99edcce33f62e87f950d61bd50b1efcec787b7a7a76f88f36972` |
| TPS54332 EVM p13 | 2200 | `9a0e4181c6e94bd18743f2a855f8646ce2eda9c304bed711198ff92f2acfdb72` |
| LM358 datasheet p3 | 1700 | `f9374c48d3954437cb89236af215da5c15f1211f3744090a15e0919e8dffa0ba` |
| LM358 datasheet package table p1 | 2000 | `8abc1d295a4187864c641a61cf42e424699e9fee6240ce4117626872a7d1091a` |
| LM358 application p5 | 2200 | `2627cf57c709eca680f907cfafc5592eb96df365106203aea30d91862703c196` |

## Publication boundary and unfinished work

Publication must check confirmed physical pin numbers and roles against both ordered library
ports and symbol `SpiceOrder`. Required external ties stay external. Library/symbol hashes
bind artifacts but do not establish package truth by themselves.

TPS54331's D/DDA ambiguity remains unresolved; the DDA-only pad cannot be silently omitted.
Unreviewed discrete terminal orders and all other unconfirmed package mappings remain blocked.
This scoped confirmation is not whole-family electrical qualification. It does not alter or erase
existing FAIL/UNKNOWN rows or the twelve mandatory buck qualification gaps.

## Implemented gate

`models/pinout.py` and application-owned `models/reviewed_pinouts.json` define the reviewed
number/name/alias/package/source contract. `pipeline/make_model.py` freezes the source binding
in `spec/pinout-contract.json` before authoring and checks it again for every publication,
including legacy repair and resumed builds. The registered source hash, current frozen spec,
extracted map, stored contract and current reviewed profile must still agree.

Source agreement alone yields `SOURCE_CONFIRMED` and `publication_allowed=false`. Final staged
library/symbol agreement yields `CONFIRMED` and includes their exact SHA-256 values in
`pinout-report.json`. Physical pin number, ordered subcircuit position and symbol `SpiceOrder`
must agree, including when a library and symbol were permuted together. The report retains the
reviewed mapping and PIN-01/02/05 results. Unconfirmed builds retain a BLOCKED report and withhold
library/symbol delivery; prior app-owned outputs and receipts are archived on a rerun.

The LM358 minimal saved pin map remains replayable against the exact reviewed source without
inventing richer extracted fields. The pinout JSON is included in validation fingerprints,
shared-core parity and installer data declarations. This does not constitute an installer build.

Regressions are in `tests/models/test_pinout.py` and `tests/pipeline/test_pinout_publication.py`.
They cover clean exact bytes; altered number/name, source, contract or symbol identity; mutually
permuted model/symbol order; all public routes; refused rerun archival; unsupported package and
discrete terminal order; and the minimal LM358 saved map. Measured command results are pending
receipt below; the existence of a regression is not evidence that it passed.

## Observed default-route acceptance

The local driver `work/run_engine_acceptance.py` calls `MakeModelRequest` without an explicit
engine, verifies that it selects `behavioral`, and checks provider accounting and the exact
published model/symbol/source/contract associations. Each edition retains receipts under
`runs/engine-acceptance-opamp/` and `runs/engine-acceptance-tps54331/`; raw/log/deck artifacts
remain local. This exercises the default API pipeline, with no live provider.

| Case | General / Bob | Observed result |
| --- | --- | --- |
| LM358 total time | 27.247 / 27.783 s | PASS; 32 PASS / 10 N/A; pinout CONFIRMED and publication allowed |
| LM358 extract / compile / simulation | 5.600 / 0.001 / 16.446 s; 5.793 / 0.001 / 16.692 s | Same library bytes and row verdicts |
| TPS54331 total time | 4.692 / 4.620 s | BLOCKED before source confirmation; no delivered library/symbol |
| TPS54332DDA | Pending | New M6 live replay not yet recorded |

All completed runs above recorded zero provider calls with complete accounting. Both LM358
receipts have PIN-01/02 CONFIRMED, PIN-05 NOT_APPLICABLE and
`physical_footprint_selected=false`. Their exact identities are:

| Artifact | SHA-256 in both editions |
| --- | --- |
| Delivered library | `67766c0cf0d6ce85b9042df94ce4766deb264e917988df1e45d782e9fbaabed2` |
| Delivered symbol | `d82112bbf7172f82ae63cfccaeadd646fcb84d1c9b078d6222594dd67b563cf5` |
| Frozen pinout contract | `2f024e6bfdd674424a984c42dbc424b677dc913a24b7db2f1fd93094abaa29c0` |
| Frozen spec | `c129543a5641cfebe1b09bc3eda17d8d92d97146b78135f1a46f487d0cea6373` |

TPS54331's receipt is BLOCKED with `publication_allowed=false` and reason
`pinout_not_checked: this build did not reach source confirmation`. Its source/spec identities
are retained, but contract/library/symbol hashes are null and individual pin checks are UNKNOWN.
This proves withheld delivery and an honest diagnostic; it does not claim a completed pinout
comparison or any new electrical measurement for TPS54331.

## Completion receipts, 2026-09-30

The resumed focused command was `python -m pytest -q tests/models/test_pinout.py
tests/pipeline/test_pinout_publication.py tests/models/test_buck_design.py
tests/models/test_op_amp.py tests/test_shared_core.py -m "not ltspice and not network"`:
**92 passed, 2 skipped, 1 deselected in each edition**. The two skips require local frozen
device fixtures and are not counted as electrical evidence. Shared manifests were regenerated
after restoring the preserved work on current `main`.

The final pre-stop offline suites on 2026-09-29 recorded 2283 passed / 24 skipped / 190 deselected
General and 2045 passed / 31 skipped / 187 deselected Bob. Those are historical source-suite
receipts; the new GUI/PWM work is outside this milestone and will have its own validation.

Real default-route TPS54332DDA acceptance completed in General (164.950 s) and Bob (163.994 s).
Both publish exact library SHA-256
`21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2` with confirmed pinout
contract `f03037e6726fa58b8f5f47559241293b0ba0990b857b16ab39c9acc3df561cdc`, zero provider calls
and complete accounting. Ordinary rows remain 12 PASS / 4 FAIL / 41 UNKNOWN / 49 N/A.
Independent qualification remains 4 PASS / 12 UNKNOWN, `family_qualified=false`; public status
remains UNKNOWN. General simulation took 159.962 s and Bob 154.537 s. Plan hashes differ by
edition implementation fingerprint, while both bind the exact same delivered candidate.

A fresh General LM358 replay completed in 47.803 s with the same library and pinout-contract
hashes and unchanged 32 PASS / 10 N/A (37.819 s simulation, zero provider calls).

Adversarial regressions also reject duplicate SpiceModel attributes and PinName/SpiceOrder pairs
split across different PIN blocks. A thread-based public-entry test proves that a concurrent
build cannot archive or overwrite the previous output on any route. Buck metadata and both
family parameter units reject malformed saved designs. These safeguards establish publication
identity; they add no wider electrical-accuracy or package coverage claim.
