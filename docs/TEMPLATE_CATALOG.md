# System-model template catalog

Status as of 2026-09-25: **`peak_current_buck_v1` is the only reusable family slice**. It
has a switching model, statement-and-unit parameter mapping, pin aliases, and a
code-built current-limit bench. It is not yet system-verified: the frozen TPS54332
baseline contains PASS, FAIL, and UNKNOWN results. Generic primitives and earlier
regulator helpers are useful foundations, but none of the other families below has a
complete, evidenced family slice. The acceptance checks in this catalog are targets,
not claims of measured accuracy.

## Scope and acceptance contract

The target is a **system-level I/O-card sanity check**: correct package wiring and pin
behavior, key electrical numbers, and plausible startup, edge, ripple, and fault
behavior. The model must reveal board mistakes such as separate AGND/DGND nets or an
oscillator overloaded by clock inputs. An averaged switching model alone cannot
establish ripple or edge accuracy.

Each family, before it can be called built, needs:

1. A contract JSON with parameters mapped to cited datasheet statements and units,
   explicit provenance, and supported and unsupported behavior. Every package pin,
   number, name, subcircuit port, and symbol `SpiceOrder` must agree. Pin aliases must
   be explicit. No internal pin-to-pin tie is allowed without a datasheet citation.
   A pinout needs user confirmation or agreement from two independent sources before
   publication.
2. A code-built checklist for the relevant family behavior. Must-be-right values
   use datasheet min/max, or ±10% when only a typical value is available. Shape and
   transient checks use 0.5–2× of a reference, preferring a vendor model, then a
   datasheet typical-application figure, then a formula using the bench's own parts.
3. Real LTspice measurements for claimed electrical PASS results, plus evidence on a
   second compatible part. Missing evidence is UNKNOWN. Pin alarms need both a
   fault-injection test that fires and a clean-circuit test that stays quiet. The
   family-specific checks in the table are the minimum checklist to design, not
   already passing checks.

The general pin model precedes family templates and applies to **any IC**. It checks
absolute maximum ratings, required connections, floating inputs, back-power through
I/O, and overloaded outputs using `PinDefinition` records and existing primitives.
Processors and programmable logic are in scope at this pin/bank level, without
emulating firmware or internal logic.

## The 22 families

| # | Family: parts | Modeled as | Family-specific acceptance contract |
|---|---|---|---|
| 1 | **Switching regulators:** buck, boost, buck-boost/inverting, SEPIC | A template per topology and control scheme (peak-current, voltage-mode, constant-on-time), in **SW and AVG** modes with identical pins and parameters. | Check reference, UVLO/EN, soft-start, switching frequency, supply currents, current limit at min and max corners, and current-sense gain from multiple active COMP points where applicable. In SW, check ripple, edges, startup, and load step. In AVG, ripple and edge verdicts are UNKNOWN. |
| 2 | **Linear regulators:** LDOs, linear controllers, DDR VTT | Template. | Check output level, dropout, enable/UVLO, current limit, supply current, startup, and power-good behavior where present. |
| 3 | **Isolated power:** flyback/push-pull controllers, transformer drivers, DC-DC modules | Template; modules are an averaged black box. | Check supply/startup thresholds, output rails, current limit and fault behavior; check switching frequency and waveforms for switching templates. Module ripple/edge claims need separate evidence. |
| 4 | **Charge pumps and PMICs** | Rail templates plus a sequencer; factory defaults selected by part suffix. | Check each rail's level, enable, sequencing, reset/fault response, and the cited factory-default configuration. |
| 5 | **Power-path switches:** load switches, eFuses, hot-swap, ideal-diode/ORing, surge stoppers, power muxes | Template. | Check on resistance, inrush/slew, current limit, reverse blocking, UVLO/OVLO, switching and fault timing as applicable. |
| 6 | **Supervision and sequencing:** supervisors, reset ICs, watchdogs, sequencers, power-fail comparators | Template. | Check trip and release thresholds, hysteresis, reset/watchdog delays, output topology, and power-up state. |
| 7 | **References and monitoring:** voltage/current references, current-sense amps, voltage/power monitors | Template; digital monitors are pin models. | Check reference level, gain/offset or trip points, supply and load behavior; keep unmodeled digital protocol behavior UNKNOWN. |
| 8 | **Amplifiers and comparators:** op amps, in-amps, difference amps, ADC drivers, PGAs, comparators | Template. Active filters and TIAs are circuits built from these. | Check offset, gain, input/output range, supply current, slew/bandwidth, or comparator threshold, hysteresis, and delay where specified. |
| 9 | **Data converters:** ADCs, DACs, AFEs | Pin model plus input/reference load; DAC output template, including output state at power-up. | Check supplies, reference and input loading, pin thresholds, and DAC output range and startup state. Do not claim conversion accuracy without a modeled transfer and measured bench. |
| 10 | **Analog routing:** analog switches, muxes, crosspoints, digital pots | Template, including unpowered behavior and wiper position at power-up. | Check channel selection, on resistance, leakage, switching time, unpowered isolation, and initial wiper state. |
| 11 | **Clocks and timing:** crystals, oscillators, PLLs, jitter cleaners, clock buffers/muxes/dividers, RTCs, timers | Passive crystal model; oscillator/buffer template with frequency, startup, output standard, drive and max load; PLLs as pin model plus lock time. | Check oscillator frequency/startup/edges and loaded output levels. Inject an excessive fanout fault, including five clock loads when beyond the cited drive rating. For PLLs, check supply/lock timing without claiming jitter or phase-noise fidelity. |
| 12 | **Digital glue:** gates, buffers, Schmitt triggers, level translators, bus switches, GPIO expanders, latches, shift registers, debounce ICs | LTspice logic gates plus pin model, including power-up states and `Ioff`. | Check input thresholds, output levels/drive, delay, supply-domain behavior, power-up state, and powered-off isolation. |
| 13 | **Processors and programmable logic:** MCUs, FPGAs, CPLDs, SoCs | Pin model per I/O bank, power-on/reset, strap pins, and sequencing rules. | Check bank supply compatibility, reset/strap levels, required connections, back-power and sequence alarms; do not assert firmware or FPGA logic behavior. |
| 14 | **Memory, ID and configuration:** EEPROM/FRAM, SPI flash, SRAM, ID ICs, config switches, JTAG routing | Pin model. | Check package pins, supply and I/O levels, power-up/default pins, required pull-ups/straps, and back-power alarms. |
| 15 | **Wired interfaces:** RS-232/422/485, CAN, LIN, I2C/I3C, SPI, LVDS, USB/Ethernet PHYs | Transceiver template; PHYs as pin model. | Check transmit/receive levels and thresholds, failsafe/idle, enable timing, drive and powered-off behavior where specified. PHY protocol and signal-integrity claims need separate models. |
| 16 | **Isolation:** digital isolators, optocouplers with current-transfer-ratio corners, isolated transceivers/amplifiers/modulators, isolated gate drivers | Template. | Check isolated supply domains, default outputs, propagation delay, drive or transfer ratio at cited corners, and prohibited cross-domain ties. |
| 17 | **Industrial field I/O:** 4–20 mA, 0–10 V, 24 V digital inputs (IEC 61131-2), digital outputs, RTD/thermocouple front ends, HART, IO-Link, configurable-I/O ICs | Built from amplifier, switch, and threshold templates. | Check loop span/compliance, input thresholds, output load limits, sensor range, and fault responses relevant to the selected function. |
| 18 | **Output and load drivers:** gate drivers, smart high/low-side switches, relay/solenoid/LED/motor drivers | Template. | Check output drive, switching edges, current limit, inductive clamp, fault indication, and startup state with representative loads. |
| 19 | **Connector-side protection:** ESD/TVS, surge clamps, MOVs/GDTs, fuses/PTCs, NTC limiters, reverse-polarity parts, EMI filters, ferrites, common-mode chokes | Vendor model or building block, plus pin check. | Check package/terminal order and rated clamp, leakage, impedance or trip behavior with a cited stimulus; mark unmodeled surge physics UNKNOWN. |
| 20 | **Discrete semiconductors:** diodes, BJTs, MOSFETs, JFETs, solid-state relays | Vendor or built-in model, plus package pin-order check and absolute-max alarms. | Check package pin order, basic conduction/switching, and absolute-max fault alarms; retain vendor model provenance. |
| 21 | **Passives and interconnect:** R, C, L, networks, transformers, terminations, connectors (including staggered hot-plug pins), cables, backplanes | Built-in parts plus ratings, ceramic-capacitor DC-bias derating, and inductor saturation. | Check values and terminal mapping, rating violations, bias-dependent capacitance, saturation, and connection order for hot-plug cases. |
| 22 | **Loads, sensors and field environment:** relay coils, solenoids, lamps, motors, capacitive loads, RTDs, thermocouples, Hall sensors, field-supply ramps and dips, surge/ESD pulses | Simple behavioral models and a stimulus library. | Check stimulus amplitude/timing and load response against declared parameters. Label synthetic examples as synthetic, never as device measurements. |

## Build order

1. Pin model for every IC.
2. Power backbone: families **1, 2, 5, 6**.
3. Clocks: **11**.
4. Digital glue and processors: **12, 13**.
5. Buses: **15**.
6. Isolation: **16**.
7. Field side: **17, 18, 22**.
8. Analog: **7, 8, 10**.
9. Protection, discretes, passives: **19–21**.
10. Converters, PMICs, isolated power: **9, 4, 3**.

Family **14** (memory, ID and configuration) is covered initially by the general
pin-model work in step 1; its family-specific contract still requires the evidence
above before it can be marked built. The numbered order is the requested priority,
not a claim that every family must wait for all earlier families.

## Outside the model scope

Beyond a pin model, this catalog does not cover RF, multi-gigabit signal integrity,
jitter/phase noise, EMI, temperature dependence, or firmware/FPGA logic. Unsupported
behavior stays explicitly UNKNOWN; the catalog does not turn an unevaluated property
into a PASS.
