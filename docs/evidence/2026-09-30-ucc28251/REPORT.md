# UCC28251 source and deterministic PWM audit

The exact supplied Rev. E datasheet now has a functional first-order primary-side PWM implementation. Code reads the reviewed, cited facts, constructs a typed design, and renders the library deterministically. It does not substitute the generic pin shell, ask a provider to author a model, or change the tests to fit a candidate.

This is deliberately limited coverage. Passing the fifteen bound probes does not make the whole component PASS. Synchronization, pulse enable, SR startup/prebias, full hiccup recovery, thermal behavior, minimum pulse width and unmeasured rows remain outside qualification. The final delivered model card must retain that scope and UNKNOWN coverage.

## Manufacturer evidence and package identity

The user's PDF contains embedded text; no OCR was needed. Relevant full pages and diagrams were rendered and visually inspected before the source columns, equations and pin map were entered. Page numbers below are printed page numbers; machine evidence stores zero-based PDF indexes.

| Document | Evidence used | SHA256 |
|---|---|---|
| [TI UCC28251 SLUSBD8E, revised December 2014](https://www.ti.com/lit/ds/symlink/ucc28251.pdf) | Exact user-supplied 58-page file; PW/RGP pin figures and table on pp.4–5; electrical columns pp.6–8; startup pp.13–16; oscillator equation3 p.17; PWM/reset/current-limit/dead-time functions pp.20–26; application qualifications p.29 | `81a4414e6f2d7de81398bba5f7ceffb2f4f117dbc005a8c9e157b372646dc602` |
| [TI SLUU441A UCC28250 EVM guide](https://www.ti.com/lit/pdf/SLUU441) | Separately authored U3 UCC28250PW twenty-pin schematic, p.4 | `83cff7c1b5821641a4f7d92165b7f6df6c624ffccd7af48edb766516b50b436f` |
| [TI SLUA673A UCC28250/UCC28251 comparison](https://www.ti.com/lit/pdf/SLUA673) | p.1 explicitly instructs replacing UCC28250PW with UCC28251PW, and changing RT pin2 resistor75k to37k | `da93b82a9551d84481943d9ae188dc1089cf2ee01666831b148c3658c5331f7e` |

The second pin observation is a reviewed EVM schematic supported by TI's explicit PW substitution instruction. The old controller is not silently treated as identical hardware. These are distinct primary manufacturer documents, not independent manufacturers. That substitution supports the PW package only.

The reviewed PW20 physical order is:

`1 VSENSE, 2 RT, 3 RAMP/CS, 4 ILIM, 5 EN, 6 OVP/OTP, 7 VREF, 8 REF/EA+, 9 FB/EA-, 10 COMP, 11 GND, 12 VDD, 13 SRB, 14 SRA, 15 OUTB, 16 OUTA, 17 HICC, 18 PS, 19 SP, 20 SS`.

For the primary-side operating domain, physical pin1 VSENSE must connect externally to pin7 VREF. The design and reviewed pinout both record this tie. The reviewed profile admits explicit UCC28251PW/PWR; it makes no PCB footprint or package geometry claim. Bare UCC28251 requires a package choice. RGP has a different twenty-pin order and remains publication-blocked pending independent package corroboration. No RGP package/pad inference is used to bypass that gate.

Pin-observation render SHA256 values are `8fcde672bb3f4769e4bb046261d1da569eb44e55a7a040b7bd2d3aa421e254d3` for the datasheet p.4 and `672655f6c5e96baba386dd72590d799ba8156486d514a30eec99cb0b05b3a782` for EVM p.4. The release stores source identities and reviewed observations rather than vendor PDF/model binaries.

## Design scope and source decisions

The renderer implements the resistor-timed alternating primary PWM core, the externally driven ramp/current comparator and per-cycle latch, UVLO hysteresis, level enable, VREF, soft-start charging/discharging, OVP shutdown, finite primary/SR drivers and the two nominal primary/SR dead times. It accounts sourced driver/reference/amplifier/timing-pin current at VDD. SR outputs have the reviewed steady-state interlock: each synchronous output remains low whenever its same-channel primary output or delayed primary state is high.

All device parameters have cited row origins in the typed design. Numerical resistance sensing, state regularization, finite reference impedance and the approximate amplifier pole/transconductance are separately labeled assumptions. The design has a strict payload round trip; altered delivered bytes invalidate its association. Returned payload dictionaries cannot mutate future design hashes.

Source conflicts are retained explicitly. The electrical table supplies28µA SS charging and3.25V VREF, rather than prose27µA/3.3V. Rev. E supplies the20µs startup delay, rather than the older comparison note's10µs. The420mV primary COMP floor comes from application prose on p.29, which TI explicitly excludes from guaranteed component specifications; it is labeled typical. The same section's typical100ns minimum pulse width and primary SR startup ramp/handover are omitted explicitly.

Frequency is qualified at RT75k/SP20k. Other RT programming settings are unqualified and the numerical resistance sensor clamps12.5k–200k. PS/SP scaling is qualified at the table's27k/20k points. External synchronization, pulse enable, primary SR startup ramp, secondary prebias servo, full HICC/duty-match recovery, ILIM filter-capacitor reset, thermal/process/statistical behavior and full power-converter dynamics are not certified by this model.

## Frozen measurements

The fixture constants and acceptance bands come from reviewed source evidence. Candidate design parameters are not imported into the probe module. Changing a candidate ILIM, UVLO or EN value leaves every fixture byte and datasheet band unchanged.

UVLO, EN, OVP and ILIM use independently fixed endpoint/guard/interior plateaus held75µs. Measurement uses settled public-pin behavior and the actual observed adjacent stimulus min/max. A threshold bracket crossing a frozen manufacturer endpoint produces UNKNOWN. UVLO/EN/OVP classify settled output activity; ILIM requires at least three complete settled pulses and fixed physical width guards: longer than1µs is normal, shorter than250ns is blank-limited, ambiguous widths are UNKNOWN. These guards do not scale with candidate pulse width.

ILIM propagation is measured separately. A fixed fixture rule steps ILIM0.3→0.7V one microsecond after an observed OUTA edge, excluding the source's maximum90ns leading-edge blanking. At1ns maximum timestep it pairs the actual sense transition with the first subsequent OUTA falling edge while OUTA was already high. The observed interval includes two waveform steps of uncertainty; endpoint-crossing intervals are UNKNOWN. Source PW and RGP propagation bounds remain package-specific. The delivered PW implementation uses12.3–38.7ns.

| Bound probe | Actual nominal observation | Fixed source band/assertion |
|---|---|---|
| VREF |3.249786V |3.17–3.33V, including source's0<IREF<10mA row |
| Each primary frequency |99.5421kHz |90–106kHz |
| UVLO rise |4.25–4.30V observed bracket |4.00–4.65V |
| UVLO fall |4.10–4.15V bracket |3.8–4.4V |
| Level enable |2.00–2.05V bracket |1.5–2.25V |
| SS charging |28.000µA |26–30µA |
| ILIM threshold |0.504–0.505V bracket |0.497–0.513V |
| ILIM propagation |28.6653ns; observed26.6653–30.6653ns |12.3–38.7ns PW |
| OVP shutdown |0.70–0.71V bracket |0.66–0.74V |
| Source resistance |20.000ohm |12–35ohm |
| Sink resistance |12.000ohm |4–30ohm |
| SR-off→primary-on |43.0017ns |39–48ns at SP20k/25°C |
| Primary-off→SR-on |37.9865ns |32–43ns at PS27k/25°C |
| Alternation/interlocks |zero observed violations |Reviewed zero-violation relationship assertion, not manufacturer numeric tolerance |
| COMP duty response |zero observed violations |Higher COMP must produce greater duty in the two fixed operating points |

The exact baseline library SHA256 is `c1f6947332b9ba311e3b81654def87cd43bfae3378c67b595d8801def302273b`. [audit-receipts.json](audit-receipts.json) records the candidate, deck, log and raw SHA256 identities and sizes for these actual measurements and fault controls. Large waveform files remain local; hashes and compact receipts are committed. The largest nominal waveform in this audit was48,773,816bytes, below the64MiB application cap. Each bench is bounded to120seconds; observed source-audit ordinary fixtures took roughly1.5–3.5seconds and1ns fixtures roughly7seconds.

## Fault controls and test evidence

Ten distinct wrong candidates were simulated against the identical frozen fixtures and limits; eleven observations include the paired threshold/propagation check on the same long-delay candidate. Every fault observation is FAIL:

| Deliberate candidate defect | Actual unchanged test result |
|---|---|
| UVLO rise3.99V with ordered3.7V falling threshold |Observed3.8–3.995V bracket below4.00V minimum |
| UVLO fall4.41V with ordered4.7V rise |Observed4.405–4.8V bracket above4.4V maximum |
| ILIM496mV |Observed0.3–0.4965V bracket below497mV minimum |
| ILIM514mV |Observed0.5135–0.7V bracket above513mV maximum |
| EN1.49V |Observed0–1.495V bracket below1.5V minimum |
| EN2.26V |Observed2.255–3.25V bracket above2.25V maximum |
| OVP0.659V |Observed0.5–0.6595V bracket below0.66V minimum |
| OVP0.741V |Observed0.7405–0.9V bracket above0.74V maximum |
| ILIM496mV plus2µs propagation |Threshold still below497mV; independently observed propagation2.003665µs exceeds38.7ns |
| Removed primary/SR interlock |Observed same-channel overlap; violation count1 |

The audit caught and removed four misleading measurement approaches before release: UVLO first-pulse latency, EN/OVP pulse-edge sweep quantization, ILIM first-blank-pulse quantization, and a source-delay assumption that hid a wrong threshold behind a2µs candidate delay. Source bands were never widened. Earlier14-check run receipts and timing are explicitly superseded; the old38.263-second production total is not the final15-check release timing.

`tests/models/test_pwm_controller.py` completed34PASS in81.13seconds with real LTspice enabled:33 source/design/fixture/synthetic unit checks plus one real regression that exercises all fifteen nominal checks and all eleven fault observations. Synthetic traces are labeled instrument controls, not device/simulator receipts. An independent reviewer also exercised ninety synthetic threshold/offset cases without a falsePASS, including the previous90µV plateau-offset and2µs ILIM-delay counterexamples.

The General and Bob source modules, reviewed pinout, fixture and tests are mirrored. Final default-route installed acceptance, final source/design digest, provider-call ledger and delivered artifact associations are recorded separately in the release evidence report. No live provider was called by this source audit.
