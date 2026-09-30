# Hand-off — engine evidence milestone, 2026-09-29

Read this first, then the newest entry in `docs/STATUS.md` and decision D-059 in
`docs/DECISIONS.md`. Older status entries are historical observations, not the current support list.
This is source-engine work; no new installer release or universal functional coverage is claimed.
M6's package/symbol confirmation gate remains open. Default-route timing and measured row passes
do not establish completion of that publication gate or of the broader north-star acceptance.

## Current behavior

- The engine reads cited evidence, creates a typed design, renders SPICE in code and judges it in
  LTspice. The CLI, window and new `MakeModelRequest` API default to `behavioral`. AI extraction is
  the only default AI stage; supplied, cached or exact reviewed evidence can avoid it.
  `legacy_ai` explicitly enables authoring, planning and repair; local-route AI planning is a
  separate opt-in. Old saved requests without an engine keep their historical legacy interpretation.
  `pin_only` is separately requested and
  models no function. No route silently falls back to another.
- The window exposes engine and optional family choices. A family hint does not create support.
  First-page device identity participates in the MCU/FPGA/CPLD/processor/SoC refusal; incidental
  application mentions do not classify an ordinary analog part as a microcontroller.
- The implemented behavioral families remain the peak-current buck and eight-pin dual op amp.
  Support means an implementation matched, essential inputs were cited and essential behaviors have
  independent tests. It does not mean that every operating condition or whole family is qualified.
- The one-rail pin shell refuses a second required supply terminal before rendering or simulation.
  Its failed viability report remains inspectable when model deliverables are withheld. Do not make
  the shell pass by treating an unpowered required rail as a valid powered-device test.

## Current default-route acceptance

Citation replay now replaces each DOCUMENT row's saved verified flag with the current page-check
result before freezing the new requirements/qualification plan. Missing document text, failed
matches or missing verifier results make the row unverified; source history is not rewritten.
Do not allow replayed true flags to supply a qualification reference without that current check.

Both editions retain final real-LTspice replays after citation/provenance/status hardening under `runs/engine-acceptance-opamp/`,
`runs/engine-acceptance-buck/` and `runs/engine-acceptance-tps54331/`. Every run recorded zero
provider calls with complete accounting. No live provider was exercised.

| Case | General / Bob total time | Observed result in both editions |
| --- | --- | --- |
| LM358 exact reviewed PDF | 26.237 / 26.158 s | PASS; 32 PASS / 10 N/A |
| TPS54332DDA | 161.600 / 164.024 s | UNKNOWN; ordinary rows 12 PASS / 4 FAIL / 41 UNKNOWN / 49 N/A |
| TPS54331 reviewed PDF | 4.165 / 4.272 s | BLOCKED before authoring; no model delivery |

The LM358 delivered library hash in both editions is
`67766c0cf0d6ce85b9042df94ce4766deb264e917988df1e45d782e9fbaabed2`.
The buck hash is
`21b3c7f1f9ed14b0d4247d291b7ba6342fecfdf19acdef59ca699c603fdb2ae2`, with exact design
association and the same hash in its qualification report. Each buck qualification has 4 PASS /
12 UNKNOWN and `family_qualified=false`. Its default four qualification simulations took
44.520 s general and 45.164 s Bob, included in those full-run totals.

The edition-specific frozen plan hashes and extraction/compile/simulation breakdowns are in the
newest STATUS entry and the retained `run-timing.json` files. Their 106-row buck extraction must
not be conflated with the frozen 19-row regression: that older input still reports TPS54332
12 PASS / 4 FAIL / 3 UNKNOWN and LM358 19 PASS, with no changed row verdicts.

## Frozen qualification and wrong-candidate acceptance

`authoring/qualification.py` freezes the buck plan from source requirements and spec before the
candidate is authored. The plan contains source/spec/implementation hashes, conditions, independent
benches and reference bands. Candidate parameters cannot redefine its tests. Every execution stages
immutable candidate bytes and retains its own deck/raw/log receipts. Missing checks stay UNKNOWN.
The ordinary harness report and supplemental qualification report have different, explicit scopes.

Real LTspice acceptance is retained in the general checkout at
`runs/qualification-acceptance/acceptance.json`, with immutable receipts under `clean/` and `wrong/`:

| Candidate | Nominal and mandatory checklist result | Elapsed with controls |
| --- | --- | --- |
| Clean TPS54332DDA | 4 PASS / 0 FAIL / 12 UNKNOWN; family not qualified | 87.497255 s |
| Same candidate with VREF set to 0.72 V | 3 PASS / 1 FAIL / 12 UNKNOWN | 83.232343 s |

The fixed plan hash is `548871ecfa2b5ef595b85294ed3fc0cefbc7366b9d4a78185e64a0ed1dbf8234`.
The clean candidate hash is `d9b0b5e3d4729168933f1a834e49d4e25588dfb6239cf420047c00f999d913e2`;
the changed candidate hash is `6ab382f439e8811c8560e7feb8cbdb1a669090338a92fe49b5a030020cf9a7cb`.
Clean VREF measured 0.799932 V; the changed candidate measured 0.719933 V and failed the unchanged
0.772–0.828-V band. The clean run's four synthetic fault controls were all rejected and each pair
was discriminating. Synthetic controls are not counted as device passes.

The default runs only four nominal simulations. An earlier observed default run took about 45 s;
its summary receipt was not retained, so do not quote it as an archived timing record or a complete
build time. This evidence exercises the shared simulator qualification code, not a live Bob provider
or a final end-to-end publication. Source changes to the qualification implementation invalidate an
old frozen plan; freeze a new plan for new runs while preserving these historical receipts.

Publication provenance records the actual delivered library and symbol hashes plus pin order.
A resumed repair must invalidate an old exact design association when library bytes change; a
missing typed design is unavailable, not inferred from SPICE.
Resumed buck/op-amp designs are reconstructed and rendered before retaining an exact association.
Schema/type, design/library hashes, part, subcircuit and frozen spec must match; an edited payload
cannot keep an exact claim merely because library bytes are unchanged. Invalid provenance supplies
no exact design hash to qualification. Supplemental BLOCKED is publicly BLOCKED with its refusal
reason; fixed FAIL stays FAIL, while fixed UNKNOWN downgrades an ordinary PASS.
Publication preserves candidate bytes
and refuses an ordinary harness report whose model hash or frozen spec digest does not match.
Supplemental qualification receives that exact delivered library and its exact typed-design hash
when available. Reusing an output directory archives old app-owned deliverables under
`build/publication-history/`; a refused rerun cannot present them as current output. Corresponding
regressions include `tests/pipeline/test_publication_provenance.py` and
`tests/pipeline/test_qualification_publication.py`. These check identity and integration, not another
live end-to-end acceptance or electrical verification.

## TPS54331 remains blocked

A new partial reviewed profile matches the exact TI SLVS839H PDF SHA-256
`cf72dfd0ac69eec645b7b493de628dc1c3aa66f5f925a2ea9be6bb5c38260730`. It keeps source columns intact:
VIN UVLO MAX 3.5 V with no typical value; EN TYP 1.25 V / MAX 1.35 V; current limit MIN 3.5 A /
TYP 5.8 A with no maximum. Explicit quantity identities avoid false VREF/EN and ILIM/supply-current
conflicts. Raw cited rows feed physically loaded VREF/current-limit fixtures; VIN ranges cannot be
mistaken for singleton test points. Soft-start window checks remain enforced.

TPS54331 has no accepted nominal UVTH policy. Bare part identity also does not select D versus DDA:
pins 1–8 are common and the DDA package requires PowerPAD pin 9 tied to GND. The profile records the
ambiguity, and its saved pin marker blocks publication even if later numerical tests pass. Required
switching-frequency, soft-start and enable/UVLO coverage remains unfinished. Do not claim support.

The fresh reviewed-profile receipt at `../tps54331-reviewed-takeover/replay-summary.json` is BLOCKED,
5.034 s, zero provider calls with complete accounting and no library/symbol delivery. It predates the
last singleton-VIN parser fix. The earlier replay using untouched historical requirements/bindings
at `../tps54331-takeover/replay-summary.json` is also BLOCKED (6.685 s). Neither rewrites the historical
`.smsnap/ex/tps54331_plan` extraction. Supplied frozen evidence always takes precedence.

## Accounting and next work

General API accounting counts inference transport attempts, including retries. Bob records Shell
starts; after one starts, its internal provider-request count is null/incomplete with a reason.
Zero author turns alone does not establish zero extraction or planning requests. The timing receipt
separates read, extract, bind, gate, author, save and qualification work and records qualification time
separately from ordinary author/harness work. Refusal is not mislabeled as agent authoring.

1. Final lint/format and diff checks passed. Final hardened suites passed: general 2203 passed /
   20 skipped / 190 deselected; Bob 1961 passed / 31 skipped / 187 deselected, with four pre-existing
   `slow` marker warnings. Shared-core checks found 49 intact files per edition, all 49 identical.
   Exact suite command/timing is in STATUS.
   The default-route live simulator receipts above are complete, but are not evidence of live
   provider behavior, universal coverage or a rebuilt installer.
2. Complete the twelve mandatory buck qualification gaps with independent acceptance references and
   discriminating controls. Preserve the existing TPS54332 FAIL/UNKNOWN evidence; four nominal passes
   are not full-family qualification.
3. Resolve TPS54331's UVTH policy and actual package before attempting support promotion. Complete
   independent behavior coverage. Do not relabel a source maximum as a nominal value or alter old runs.
4. Complete M6: confirm the selected package, symbol pin numbers and discrete terminal order
   against cited evidence and the owner or an independent source before publication. Existing
   pin-order hashes and the TPS54331 ambiguity guard are necessary narrower checks.
5. Add further families/pinouts only with implementations, cited inputs and qualified independent
   tests. Multi-rail pin-shell behavior remains separate scope. The terminal setup (`Setup.cmd`,
   `Start.cmd`, `Boardmodeler.cmd`, a text menu instead of the window) was merged into `main` on
   2026-09-30 and reverted the same day at the owner's request (D-062): the owner wants the window.
   It stays on the `terminal-app` branch; do not merge it again without the owner's OK.

No PASS without real simulator evidence; never relax a limit or silently switch routes. Keep vendor
PDFs and models local, never write to the LTspice installation/library, and never print credentials.
Leave `wip/m4b2`, `spice-maker-next` and `spice-maker-bob-next` untouched. Work is on the two main
checkouts; verify the latest commit/push state rather than assuming a previous handoff's tip.
