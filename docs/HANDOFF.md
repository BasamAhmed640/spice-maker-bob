# Current hand-off — resize and two-datasheet checks / desktop 1.8.2

Read D-067 and [resize/new-part evidence](evidence/2026-09-30-resize-and-new-parts/REPORT.md). Native selection and retro timer remain. No new electrical families were implemented: CDCLVC1104/LTC2452 fail fast and publish no SPICE model. Continue source-derived pin contracts, reusable fanout/ADC/serial behavior and independent fixed tests. Earlier notes below are historical.

# Current hand-off — automatic manufacturer priority / desktop 1.8.1, 2026-09-30

The owner clarified the contract after the original engine page: datasheet + part number,
automatic routing, manufacturer originals preferred when they actually work in LTspice,
and component verification from official manufacturer documentation only. Read updated
[ENGINE_GUIDE.md](ENGINE_GUIDE.md), D-066 and
[the one-hour milestone evidence](evidence/2026-09-30-automatic-delivery/REPORT.md) first.
The original HTML guide remains byte-identical and is historical where these rules differ.

1.8.1 removes normal Engine/Family/Package controls, fixes file inputs/menus, retains the
retro timer, and puts explicit legacy choices under Advanced. It checks TI/ADI product
downloads before the generated whitelist, safely preserves originals/dependencies, tests
an actual bounded LTspice load and binds the delivered hashes. Common diode/BJT/MOSFET
primitive entries and .SUBCKT interfaces are recognized. Import accuracy remains UNKNOWN;
symbols describe model terminals, not verified package pin numbers. Unsupported imported
Install/Retest buttons remain disabled rather than invoking generated-spec commands.

Continue with independent fixed electrical tests for official originals, broader official
manufacturer discovery, runtime source-derived physical pin contracts and reusable behavior
composition. The generated route still has three reviewed implementations; this release is
not a universal generator. Preserve MCU/FPGA/CPLD/processor/SoC exclusions. Never turn a load
success or pin-only shell into an electrical accuracy PASS. Edition-specific authoring glue
must be mirrored deliberately; use changed-only syncing to avoid replacing unrelated flavor
history. The notes below describe the verified earlier 1.8.0 baseline.

Read [ENGINE_GUIDE.md](ENGINE_GUIDE.md), [current release evidence](evidence/2026-09-30-pwm-release/REPORT.md) and D-064/D-065 first. The original engine page is preserved byte-for-byte. [Prior hand-off](archive/HANDOFF-before-1.8.0.md) is historical, including older pending work and timings.

The current source, installed wheel and frozen desktop all default to behavioral evidence→typed design→code renderer→fixed LTspice checks. Exact reviewed PDFs need no AI. Legacy AI and pin-only are explicit separate choices; no fallback is allowed. MCU/FPGA/CPLD/processor/SoC stay excluded.

The exact supplied UCC28251 Rev. E PW/PWR source builds in about 51 seconds in the measured installed General routes, with 15 PASS/0 FAIL/21 UNKNOWN/8 N/A and zero provider calls. All four installed routes match model/spec/design/pinout hashes and run the generated amplifier-feedback controller example. Full prebias/hiccup/sync/pulse-enable/thermal behavior is unqualified; overall UNKNOWN is intentional. RGP and unspecified packages are blocked. Details, hashes and simulator receipts are in the evidence reports.

Current reviewed implementations are UCC28251PW/PWR primary-side PWM, TPS54332DDA buck and the LM358 common eight-pin group. TPS54331 still blocks. This architecture is extensible; most PCB part families are not implemented or qualified today. Buck remains limited with its historical FAIL/UNKNOWN and qualification gaps visible.

Version 1.8.0 preserves the compact retro desktop and timer, per-part save folders and detailed diagnostics. Saved full retests test existing bytes off-thread, preserve UNKNOWN/scope/citation state, refresh exact provenance from canonical source values and archive older receipts. Installers validate current wheel/desktop/source identity; stale environments upgrade from checked local wheels with staging/rollback and preserve user files.

The software, fault controls, actual previous-version upgrades and installed default routes pass their scoped checks.

Both 1.8.0 public releases are published and fresh-download/install verified. The owner's saved PW model and unchanged-file retest both retain 15 PASS / 0 FAIL / 21 UNKNOWN / 8 N/A, zero AI calls and exact provenance. Identified old downloads are archived without deletion. The current app starts at Downloads/SpiceMaker-1.8.0/Start.cmd; final receipts are in the release report.

Both repositories use main; desktop remains in place and terminal-app remains separate.

Future support requires reviewed physical package evidence, cited essential facts, an implemented family and independent fixed measurements. Do not add support merely by admitting a name to a profile or labeling a generic pin shell functional. Keep numerical assumptions, missing behavior and FAIL/UNKNOWN visible.
