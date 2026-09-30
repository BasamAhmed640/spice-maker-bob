# Full verification and optional quick structural checks

New builds default to **Code-built behavioral** and full LTspice verification. The behavioral
and pin-only engines require full verification; the text menu and `model build` keep full verification
for both. AI extraction may still be needed, but the default engine does not ask AI to author,
plan tests or repair a model.

For a quick structural draft, choose the legacy engine explicitly and add `--sanity`
(`model build --engine legacy_ai --sanity`). Quick mode reads
the datasheet, extracts and validates its records and physical pin map, then asks the selected
provider for a model. It skips AI test planning, supplemental web search and the full electrical
suite. The same blocked-class and source/package safety gates still apply.
The broader M6 package/symbol confirmation gate remains unfinished; a quick structural result
does not establish that confirmation.

Local checks screen subcircuit boundaries, duplicate component names, physical pin mapping,
expression braces and unresolved subcircuit references. The app generates the standard symbol
from the declared terminal order. Each candidate receives structural checks and, when LTspice
is configured, an unpowered operating-point load capped at five seconds. The legacy quick loop
allows one draft and at most one structural repair. Reuse requires matching spec and
structural-check receipts for the current candidate hash.

The result is labelled **Sanity checked; electrical accuracy unverified**. Its machine status
and electrical rows remain UNKNOWN. The model card and `sanity-report.json` record local checks
and whether the bounded LTspice load was `loaded`, `inconclusive`, `unavailable` or `cancelled`.
A model the simulator rejects outright is not published. An unpowered load measures no datasheet
behavior and does not establish powered convergence, accuracy, timing, stability or temperature
performance. The example still needs appropriate supplies, inputs, loads and an analysis.

Run `model test --out DIR` to re-test a saved draft with the configured LTspice executable. It reuses extraction caches, plans independent measurement circuits, runs real LTspice and may repair a model against the frozen requirements. Only observed simulator measurements can produce PASS. This workflow can take substantial time.

CLI example:

```powershell
boardmodeler model build --part PART --datasheet file.pdf --out folder --engine legacy_ai --sanity --allow-remote
```

Without `--engine`, the CLI and `MakeModelRequest` use `behavioral`; combining that default with
`--sanity` is rejected. Select the legacy engine explicitly rather than expecting a fallback.
`pin_only` is also a separate explicit route, has no functional behavior, and cannot use quick
mode. An older installed executable does not change when source files are updated.

Quick-mode tests use synthetic circuits to check structural failures, cache invalidation,
bounded repair, tamper detection, stale-candidate rejection and honest export. They do not
certify hardware-model accuracy or guarantee a fixed generation time.
