# Quick structural checks and optional full verification

GUI GO defaults to quick mode. Read the selected datasheet, extract and validate its records and physical pin map, then ask the selected provider for a self-contained model. No AI test fixtures, supplemental web-search stage, or LTspice simulations are run in this mode. All extracted requirements are retained, including conditions, relative limits and citation warnings. The authoring prompt excludes simulation recipes and repeated excerpts.

Local checks screen subcircuit boundaries, duplicate component names, physical pin mapping, expression braces and unresolved subcircuit references. The app generates the standard symbol from the model's declared terminal order. It allows one draft plus at most one structural repair, and reuses a model only when its specification and structural-check receipt match its current hash. API duration still depends on the selected provider/model/reasoning setting; this is not a fixed-time generation guarantee.

The result is labelled **Sanity checked; electrical accuracy unverified**. Its machine status and all electrical rows remain UNKNOWN, never simulated PASS. The model card and sanity-report.json explicitly say no simulation was run. These checks are not a complete LTspice parser and do not establish convergence, electrical accuracy, timing, stability or temperature performance. The example is a connection template that needs appropriate supplies, inputs, loads and an analysis.

SETUP has **Full simulation verification (slower)**, off by default and saved in this extracted folder's config. **Run full verification** starts the existing full workflow in the GUI worker, keeping the UI responsive and cancellation available. It reuses extraction caches, plans independent measurement circuits, runs real LTspice and may repair a model against the frozen requirements. Only observed simulator measurements can produce PASS. This optional workflow can still take substantial time.

CLI: `boardmodeler model build --part PART --datasheet file.pdf --out folder --sanity --allow-remote`. The existing CLI without --sanity and the low-level MakeModelRequest API retain full verification for backward compatibility. The running old executable does not change when a new version is downloaded.

Quick-mode tests use explicitly synthetic circuits to verify control flow, structural failures, cache invalidation, bounded repair, tamper detection, stale-candidate rejection and honest export. They do not certify the accuracy of generated hardware models.
