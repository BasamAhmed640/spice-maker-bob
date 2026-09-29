# What happens after GO

New builds default to **Code-built behavioral** with **FULL VERIFICATION** in the window,
CLI and `MakeModelRequest` API. Code selects a supported implementation, builds a typed design
from cited inputs, renders SPICE and judges it in LTspice. AI extraction is the only default AI
stage; matching cached records, supplied evidence or an exact reviewed PDF profile can avoid it.
No default AI author, test planner, reinforcement search or repair loop runs.

Replayed DOCUMENT rows are checked against the currently available cited text. The result replaces
their saved citation flags before the new source/spec and qualification plan freeze. Missing text,
a failed match or a missing verifier result makes the row unverified; an old true flag cannot
certify itself. Original historical run artifacts are not rewritten.

The implemented behavioral families are the peak-current buck and eight-pin dual op amp.
A family name alone does not establish support: missing essential evidence, independent tests
or an unresolved package can stop the build as **BLOCKED**. Microcontrollers, FPGAs, CPLDs,
processors and SoCs are refused on every route. A family hint cannot override that gate.
An unsupported behavioral build never falls back to AI authoring or a pin shell.

| Stage | Who does it by default | What happens |
| --- | --- | --- |
| Read | Local code | Hashes the PDF and reads its pages and text. |
| Extract | Selected provider, if needed | Extracts the exact part's identity, pins, requirements, conditions and citations into structured records. Cached, supplied or reviewed records can avoid inference. Citation text is checked locally. |
| Bind and gate | Local code | Selects validated independent fixtures, keeps untested rows visible, checks support and package evidence, and freezes the specification. A buck qualification plan is also frozen from source evidence before model generation. |
| Build | Local code | Fills the matched implementation from cited rows, records parameter provenance and renders the candidate. It does not ask an AI to write or repair the model. |
| Judge | LTspice and local code | Measures the applicable characteristics against frozen requirements. Missing measurements and uncovered behavior stay UNKNOWN. |
| Save | Local code | Publishes only an authorized candidate whose bytes match its harness report and frozen spec. Records library/symbol hashes and pin order. A refusal can leave diagnostics without a model. |
| Qualify, for buck models | LTspice and local code | Tests the exact delivered library against the separately frozen qualification plan. Four nominal tests run by default; twelve mandatory gaps remain UNKNOWN. This report does not replace the ordinary row harness or qualify the whole family. |

The separate M6 publication gate is still open: confirm the package variant, symbol pin numbers
and discrete terminal order against cited evidence and owner confirmation or an independent source.
Present hash/pin-order checks and the TPS54331 package refusal do not complete that planned gate.

The **AI authored (legacy)** engine (`--engine legacy_ai`) explicitly enables the provider
authoring path, including full-mode AI test planning and bounded repair against the frozen
requirements. Any enabled reinforcement belongs to that route. `--plan-tests` separately opts
a local route into extra AI planning; it is off by default. These are visible choices, never
automatic recovery from a refused code-built model. In the Bob edition every AI job uses IBM Bob;
in the general edition it uses the selected HTTPS provider.

**Pins only** (`--engine pin_only`) is a separate, limited interface model for supported pin
mappings: pins, supply draw, clamps and wiring alarms. It has no device function and makes no
electrical accuracy claim. The current shell supports one supply rail and one ground; a second
required supply is refused. Its real LTspice viability checks do not turn it into a functional
model. Both code-built routes require full verification.

Only the legacy engine offers the optional [quick structural draft](QUICK_MODE.md), by
unchecking **FULL VERIFICATION** beside GO. Quick checks remain electrically unverified.

PASS covers only measured characteristics at their recorded conditions. The exact reviewed
LM358 PDF has checks on both channels, but common-mode range, temperature corners and other gaps
remain outside that evidence; see [LM358 validation](LM358_VALIDATION.md). A successful export,
a typed design or an exact file hash does not establish universal device accuracy.

The elapsed clock includes waits and cancellation cleanup and keeps the final duration.
`run-timing.json` records stage durations and observable provider calls. General API attempts include
retries. Bob Shell starts are counted separately; after a start, its internal provider-request
count is unknown. Zero author turns alone is not proof of zero extraction calls. Stage messages
describe application work, not the provider's private reasoning.

Saved requests retain their recorded engine. Older requests with no engine field retain their
historical legacy interpretation; reopening one does not silently migrate its authoring route.
