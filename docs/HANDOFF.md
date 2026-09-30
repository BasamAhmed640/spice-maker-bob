# Current hand-off — desktop/PWM 1.8.0, 2026-09-30

Read [ENGINE_GUIDE.md](ENGINE_GUIDE.md), [current release evidence](evidence/2026-09-30-pwm-release/REPORT.md) and D-064/D-065 first. The original engine page is preserved byte-for-byte. [Prior hand-off](archive/HANDOFF-before-1.8.0.md) is historical, including older pending work and timings.

The current source, installed wheel and frozen desktop all default to behavioral evidence→typed design→code renderer→fixed LTspice checks. Exact reviewed PDFs need no AI. Legacy AI and pin-only are explicit separate choices; no fallback is allowed. MCU/FPGA/CPLD/processor/SoC stay excluded.

The exact supplied UCC28251 Rev. E PW/PWR source builds in about 51 seconds in the measured installed General routes, with 15 PASS/0 FAIL/21 UNKNOWN/8 N/A and zero provider calls. All four installed routes match model/spec/design/pinout hashes and run the generated amplifier-feedback controller example. Full prebias/hiccup/sync/pulse-enable/thermal behavior is unqualified; overall UNKNOWN is intentional. RGP and unspecified packages are blocked. Details, hashes and simulator receipts are in the evidence reports.

Current reviewed implementations are UCC28251PW/PWR primary-side PWM, TPS54332DDA buck and the LM358 common eight-pin group. TPS54331 still blocks. This architecture is extensible; most PCB part families are not implemented or qualified today. Buck remains limited with its historical FAIL/UNKNOWN and qualification gaps visible.

Version 1.8.0 preserves the compact retro desktop and timer, per-part save folders and detailed diagnostics. Saved full retests test existing bytes off-thread, preserve UNKNOWN/scope/citation state, refresh exact provenance from canonical source values and archive older receipts. Installers validate current wheel/desktop/source identity; stale environments upgrade from checked local wheels with staging/rollback and preserve user files.

The software, fault controls, actual previous-version upgrades and installed default routes pass their scoped checks. Public-release download and final owner-folder acceptance are recorded in the release report once published. Both repositories use main; desktop remains in place and terminal-app remains separate.

Future support requires reviewed physical package evidence, cited essential facts, an implemented family and independent fixed measurements. Do not add support merely by admitting a name to a profile or labeling a generic pin shell functional. Keep numerical assumptions, missing behavior and FAIL/UNKNOWN visible.
