SYSTEM TEST SUITE -->> SPICE MAKER (model-alarm fixture catalog, revised 2026-09-26)

This catalog preserves 66 fault IDs in 13 categories. **57 are planned as
small, code-built LTspice circuits that prove alarms inside a generated SPICE
model; six are NOT COVERED by the model-only product; and PIN-01/02/05 belong
to the M6 model-generation pinout gate.** The original A–D card labels below
are synthetic contexts for choosing circuit stimuli, not assembled cards or
board-level acceptance tests. GND-01 and CLK-01 are synthetic stand-ins for the
owner's two reported mistakes until their actual circuits are available.
Twelve eligible cases request boundary pairs.

Spice Maker delivers models, symbols, model cards, and their tests. It does not
import a board or CAD/netlist, run a user-facing board checker, or issue a
user-facing findings report. An applicable problem is shown by an alarm inside
the user's own LTspice simulation. Milestone evidence files are developer test
records, not product reports. The per-case status table at the end controls
what has actually been built; a catalog description alone is not detection.

HOW EVERY MODEL-ALARM TEST WORKS
- Code builds a small circuit around the model, including other parts when a
  fault depends on a chain or load. Each eligible case can have up to three
  variants:
  - Clean: the model's applicable alarm stays quiet. A false alarm fails the
    test.
  - Fault: the intended internal model alarm fires at the relevant part and
    pin or port. A different alarm does not count. The model card must identify
    the alarm and its applicable cited source; a datasheet page is never
    invented for a board-defined assumption.
  - Boundary pair: a case marked (+/-) also tests a value just inside and just
    outside its stated limit. The declared fixture chooses a justified margin
    or real part value. The inside value must stay quiet and the outside value
    must fire the intended alarm.
- A cited corner run uses only the datasheet's min/max values and declares its
  stimulus and temperature. A model that cannot judge a fault yields UNKNOWN
  with a reason; UNKNOWN is not detection.
- Detection requires observed simulator data, including the correct alarm and
  a quiet clean control. A generated deck or a parameter declaration is not a
  measured PASS.

SYNTHETIC CONTEXT TAGS FROM THE ORIGINAL CATALOG
The A–D tags retain the original design examples so a later model fixture can
choose realistic neighboring parts. They do not require four reference-card
builds or any board import.
- A, power and control:
  - 24 V input -> reverse protection -> eFuse -> buck 5 V -> buck 3.3 V -> LDO 1.8 V;
  - a supervisor with reset;
  - an MCU/FPGA pin model with straps and a watchdog;
  - an oscillator and clock buffer;
  - an EEPROM;
  - I2C sensors behind a level translator.
- B, analog input:
  - 4-20 mA and 0-10 V inputs -> protection -> in-amp/op amp -> ADC with separate AGND/DGND and an external reference;
  - an analog mux;
  - the digital side across a digital isolator;
  - an isolated DC-DC module.
- C, digital I/O:
  - 24 V digital inputs (IEC 61131-2) through optocouplers;
  - high-side smart switches driving a relay, a solenoid and a lamp;
  - a shift register / GPIO expander driving the output enables;
  - a MOSFET half-bridge.
- D, fieldbus:
  - isolated RS-485 and CAN transceivers with termination and fail-safe bias;
  - TVS on the bus lines;
  - a backplane connector with staggered (make-first) pins.

POSSIBLE CODE-BUILT FIXTURE CHANGES
These are stimulus ideas, not a claim that all 66 tests are implemented:
disconnect or swap model terminals; merge circuit nodes; alter values, loads,
pull-ups, parts, or supply order; and change ramp, dip, polarity, surge,
sequence, or connector-mating stimuli. A clean fixture and its fault variant
must record their exact difference.

CATALOG (original synthetic context in brackets)
The third column preserves the **original proposed detector sketches** for
traceability. `S` meant a static wiring check, `Sc` a scenario check, `A` a
model alarm, and `C` a corner run in that older board-oriented proposal.
Those sketches are superseded: `S`/`Sc` are not product checker or report
features, and no cell in this column is a claim of implemented detection.
Only the model-alarm fixture procedure and the final status table govern
current acceptance. The six NOT COVERED cases and three M6 pinout-gate cases
remain in the catalog so all 66 IDs are accounted for.

1. Grounds and returns
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| GND-01 | AGND not tied to DGND at the ADC [B] | S required tie; A AGND-DGND beyond absolute max |
| GND-02 | Exposed pad / PowerPAD not connected to GND [A] | S required connection (datasheet page); A |
| GND-03 | Grounds bridged across an isolation barrier [B, D] | S isolation-domain merge |
| GND-04 | Signal crosses the backplane with no common ground or reference [D] | S interface without a reference; Sc receiver levels invalid |

2. Rails and regulators
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| PWR-01 (+/-) | Feedback divider wrong: 3.3 V rail comes up at 5 V [A] | Sc rail vs its declared domain; A downstream absolute max |
| PWR-02 | No headroom: LDO 3.3 V from 3.3 V; buck UVLO above the minimum field supply [A] | C min-input corner; Sc out of regulation |
| PWR-03 | Current limit at the min corner below start-up demand (bulk capacitance + load), so the regulator hiccups [A] | C + Sc `nominal_startup` never reaches PG |
| PWR-04 | Inductor saturation current below the converter's max current limit [A] | S Isat < ILIM max |
| PWR-05 | Required part missing: BOOT capacitor, input capacitor, SS capacitor [A] | S required component between pins (datasheet page) |
| PWR-06 | EN floating on a part with no internal pull, or EN tied above its absolute max [A] | S; A |
| PWR-07 | Backfeed: LDO output held above its input, or two regulator outputs in parallel [A] | A reverse current; S two sources on one net |
| PWR-08 (+/-) | Rail load above the regulator rating, or LDO dissipation (VIN-VOUT)*I above what the package allows [A] | C max load; Sc current and dissipation vs rating |

3. Sequencing and power cycling
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| SEQ-01 | A datasheet rail-order rule violated (for example FPGA I/O before core) [A] | Sc `staggered_rails` + extracted sequencing rules |
| SEQ-02 (+/-) | Reset released before the rails are valid or the clock is running (supervisor threshold or delay wrong) [A] | Sc `reset_early_release`, `clock_late` |
| SEQ-03 | PG -> EN chain broken: PG pull-up missing, or on a rail that isn't up yet [A] | Sc `pg_missing`, `pullup_missing`, `pullup_wrong_domain` |
| SEQ-04 | Pin driven while its chip is unpowered, so the chip powers up through its ESD diodes [A, B] | Sc `externally_driven_unpowered_pin`, `partial_power`; A injection current |
| SEQ-05 | Short input dip resets one domain but not another; supervisor chatters [A] | Sc `brownout_short_interrupt`, `repeat_power_cycles` |
| SEQ-06 | Re-enable before the output discharged; start-up into a pre-biased rail [A] | Sc `incomplete_discharge`, `prebiased_output` |
| SEQ-07 | Slow input ramp causes UVLO chatter; fast ramp causes excess inrush [A] | Sc `slow_rail`, `fast_rail` |
| SEQ-08 | Hot-plug: power pins mate before ground [D] | Sc (new) mating order; A absolute max |
| SEQ-09 (+/-) | Watchdog active at power-up with a timeout shorter than the declared boot time [A] | Sc timeout vs the declared boot time |

4. Clocks
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| CLK-01 (+/-) | One oscillator driving 5 clock inputs (pass at its rated load, flag at 5 loads) [A] | S sum of input capacitance > rated load; Sc edge time and VIH at the far inputs |
| CLK-02 | Output/input standard mismatch: LVPECL/LVDS into LVCMOS, or a 3.3 V clock into a 1.8 V input [A] | S levels; A absolute max |
| CLK-03 | Crystal load capacitors wrong, or oscillator gain margin too low (commonly required >= 5) [A] | S calculation from the datasheet |
| CLK-04 | Clock missing or late at reset release; oscillator OE pin floating [A] | Sc `clock_missing`, `clock_late`; S floating input |

5. Digital levels and logic
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| DIG-01 (+/-) | Level mismatch: 3.3 V output into a 1.8 V input, or 1.8 V output into a 3.3 V input (VOH < VIH) [A, C] | S; A |
| DIG-02 | Floating CMOS input with no pull [A, C] | S; A |
| DIG-03 | Contention: two push-pull or two enabled tri-state drivers on one net; TX tied to TX [C, D] | S more than one driver; A contention current |
| DIG-04 | Undefined output at power-up (MCU pins high-impedance during reset; shift register with no reset or OE control) driving an enable or actuator with no defining pull [C] | Sc `nominal_startup`: undefined state on a critical net |
| DIG-05 | Auto-direction level translator with a pull-up or load beyond its datasheet limits [A] | S |

6. Buses and interfaces
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| BUS-01 (+/-) | I2C rise time over 1000 ns (standard mode) or 300 ns (fast mode), or VOL over 0.4 V at 3 mA with a pull-up that is too strong [A] | Sc measured rise time and VOL |
| BUS-02 | I2C address conflict from strap pins [A] | S |
| BUS-03 | RS-485/CAN termination missing, or more than two terminators [D] | S count; Sc differential amplitude vs receiver threshold |
| BUS-04 | RS-485 idle bus undefined: no fail-safe bias and no internal fail-safe [D] | S; Sc idle differential voltage |
| BUS-05 | Unit-load budget exceeded, or an unpowered node loads the bus [D] | S; Sc `partial_power` |

7. Analog chain
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| ANA-01 (+/-) | Op-amp input common-mode range exceeded [B] | A |
| ANA-02 | Output cannot swing to the needed level, so ADC full scale is unreachable [B] | Sc output range vs required |
| ANA-03 | Op amp drives a capacitive load (ADC input, cable) without an isolation resistor, and the ringing does not die out [B] | Sc ballpark transient |
| ANA-04 | 0-10 V field signal into a 0-2.5 V ADC input [B] | A absolute max; Sc range |
| ANA-05 | Reference overloaded or tied to the wrong rail [B] | Sc reference within its tolerance under ADC load |
| ANA-06 | Current-sense amp common mode above its max (high-side on 24 V), or its inputs swapped [A, C] | A; Sc output sign or stuck at a rail |
| ANA-07 | Mux or analog switch signal beyond its supply, or applied while the mux is unpowered [B] | A injection current; Sc `partial_power` |

8. Isolation
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| ISO-01 | One side unpowered, and the isolator's default output enables something [C, D] | Sc `partial_power` with default output states |
| ISO-02 (+/-) | Optocoupler at end-of-life CTR (current-transfer ratio) cannot pull its output to a valid level [C] | C min-CTR corner |
| ISO-03 | Unregulated isolated module below its minimum load: output rises above the downstream absolute max [B] | Sc; A |

9. Field side and protection
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| FLD-01 (+/-) | 24 V digital-input thresholds or currents outside the IEC 61131-2 Type 1/2/3 bands [C] | Sc threshold and current sweep |
| FLD-02 | Reverse-polarity field supply with no effective protection [A, C] | Sc reverse stimulus; A absolute max |
| FLD-03 | TVS clamp above the downstream absolute max, or TVS standoff below the normal operating voltage [C, D] | S; Sc surge pulse (ballpark) |
| FLD-04 (+/-) | 4-20 mA loop cannot reach 20 mA at max loop resistance and minimum supply [B] | C + Sc |
| FLD-05 | Fuse/PTC trips on normal inrush, or does not trip on a short [A] | Sc inrush vs rating (ballpark) |

10. Drivers and loads
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| DRV-01 | Inductive load switched with no clamp: switch voltage exceeds its absolute max [C] | Sc turn-off; A absolute max |
| DRV-02 | Smart-switch current limit below the load's inrush (lamp, capacitive load) [C] | Sc output never turns on |
| DRV-03 | MOSFET gate drive: logic-level drive on a standard-threshold MOSFET, or VGS above its max [C] | S; A |
| DRV-04 | Relay coil below its pick-up voltage or above its rating [C] | S; Sc |
| DRV-05 | Half-bridge with both switches on at power-up from undefined inputs [C] | Sc `nominal_startup`; A shoot-through current |

11. Configuration and straps
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| CFG-01 | Strap pin floating, or an invalid combination (boot mode, address, voltage select) [A] | Sc `invalid_strap`; S |
| CFG-02 | Strap sampled before its driving rail or signal is valid [A] | Sc `late_strap` |
| CFG-03 | EEPROM/flash write-protect or chip-select floating [A] | S |

12. Pinout and library integrity (the "wiring must be absolute" rule)
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| PIN-01 | Symbol pin numbers don't match the datasheet package (library error) [any] | S SC005 + pinout gate |
| PIN-02 | Wrong package variant: same part, different pinout [A] | S package check |
| PIN-03 | Only one of several same-name supply or ground pins connected [A, B] | S every same-name pin connected |
| PIN-04 | Do-not-connect pin connected to a net [A] | S |
| PIN-05 | Discrete pin order (SOT-23 MOSFET/BJT) doesn't match the model [C] | S |

13. Values and ratings
| ID | Fault injected | Original detector sketch (superseded) |
|---|---|---|
| VAL-01 | Value typo on a timing or feedback part (10 kOhm <-> 10 Ohm, 1 uF <-> 1 nF): soft-start, reset delay or frequency far off [A] | Sc timing vs its expected band |
| VAL-02 (+/-) | Power or voltage rating exceeded on a resistor, capacitor or connector pin [A, C] | Sc vs rating; S |
| VAL-03 | Ceramic capacitor below the regulator's minimum after DC-bias derating: ripple or ringing out of band [A] | S calculation; Sc ripple (ballpark) |

SCORING
- Detection: every implemented model fixture fires the intended internal alarm
  at the relevant part and pin or port, with its source in the model card.
  Target: 100% of eligible, implemented cases.
- False alarms in clean model fixtures. Target: 0.
- Boundary pairs: inside stays quiet and outside fires the intended alarm.
  Target: 100% of implemented marked pairs.
- UNKNOWN and NOT COVERED counts retain reasons and never count as detection.
- Record time per small test circuit. AVG suits long power-up or fault sweeps;
  SW is needed for ripple, switching edges, and fast transients. An AVG ripple
  or edge result is UNKNOWN, not a quiet-alarm PASS.
- Keep a developer evidence matrix of each model alarm, fault fixture, clean
  control, and applicable boundary pair. It is not a user-facing report.

WHEN EACH PART OF THE SUITE COMES ALIVE (agent-prompt milestones)
- M5: model-alarm circuits for GND-01/02/04, PIN-03, CFG-03, DIG-02,
  and PWR-05/06, each with a clean control.
- M6: PIN-01/02/05 at the model-generation pinout gate, not by LTspice alarms.
- Later family work: remaining eligible PWR, SEQ, VAL, CLK, DIG, BUS, ANA,
  ISO, FLD, DRV, and CFG cases become small model-test circuits as the needed
  families and neighboring parts exist. A chain involving several parts is
  still a small code-built circuit, not a reference-card run.
- The six NOT COVERED IDs remain excluded unless the owner changes scope.

OUT OF SCOPE
Board/CAD/netlist import, user-facing board checking, and user-facing findings
reports or screens. Signal integrity of fast links, EMI, precise thermal
behavior, firmware logic, and layout effects (trace inductance, ground bounce)
are also outside this suite.

---

## Counts and open interpretations

The catalog above contains 66 unique case IDs in 13 categories. Twelve IDs carry
the `(+/-)` boundary-pair marker: PWR-01, PWR-08, SEQ-02, SEQ-09, CLK-01,
DIG-01, BUS-01, ANA-01, ISO-02, FLD-01, FLD-04, and VAL-02. These are planned
tests, not measured outcomes. All 12 belong to the 57 eligible model-fixture
IDs. The A–D tags are inherited synthetic contexts, not acceptance fixtures.

The 66 IDs partition into 57 eligible but NOT BUILT model-alarm fixtures,
three M6 pinout-gate checks, and six NOT COVERED cases. A selected model
fixture may need neighboring devices, loads, or a PG-to-EN chain. Its result
still comes from the intended internal model alarm in that code-built circuit.

Open interpretation points to settle before scoring those cases:

- An eligible fault with a board-defined assumption, such as DIG-03
  contention, needs an explicit applicable source and an internal model alarm.
  A datasheet citation must not be invented. GND-03 and BUS-02 are NOT COVERED.
- PWR-08 asks for package dissipation checks while precise thermal behavior is
  out of scope. A bounded check needs declared ambient and package assumptions;
  otherwise that portion remains UNKNOWN.
- A clean circuit must keep the applicable alarm quiet. UNKNOWN coverage for
  an unimplemented family never counts as detection or a clean-circuit PASS.

## Per-case build status

`NOT BUILT` means no completed model-alarm clean/fault fixture, boundary pair,
or measured suite verdict is claimed. `PINOUT GATE M6` means the error is
checked when the symbol/model is generated, rather than by an LTspice alarm.
`NOT COVERED` records the model-only limitation; it is neither PASS nor
UNKNOWN. The fixture route is a plan, not evidence of implementation.

| Case ID | Status | Model-only route or exclusion reason |
| --- | --- | --- |
| GND-01 | NOT BUILT | M5 model-alarm circuit |
| GND-02 | NOT BUILT | M5 model-alarm circuit |
| GND-03 | NOT COVERED | Requires board-wide isolation-domain topology inspection |
| GND-04 | NOT BUILT | M5 model-alarm circuit |
| PWR-01 | NOT BUILT | Model-alarm circuit with feedback and downstream rail |
| PWR-02 | NOT BUILT | Model-alarm circuit with input and load corners |
| PWR-03 | NOT BUILT | Model-alarm startup circuit with cited current-limit corner |
| PWR-04 | NOT COVERED | External inductor Isat rating versus converter ILIM requires component-rating inspection |
| PWR-05 | NOT BUILT | M5 model-alarm circuit |
| PWR-06 | NOT BUILT | M5 model-alarm circuit |
| PWR-07 | NOT BUILT | Model-alarm circuit with external backfeed stimulus |
| PWR-08 | NOT BUILT | Model-alarm load circuit; thermal portion may remain UNKNOWN |
| SEQ-01 | NOT BUILT | Model-alarm circuit with multiple rail stimuli |
| SEQ-02 | NOT BUILT | Model-alarm circuit with reset and clock stimuli |
| SEQ-03 | NOT BUILT | Model-alarm circuit with PG-to-EN chain |
| SEQ-04 | NOT BUILT | Model-alarm circuit with powered-off I/O stimulus |
| SEQ-05 | NOT BUILT | Model-alarm circuit with input dip and supervisor |
| SEQ-06 | NOT BUILT | Model-alarm circuit with prebiased output |
| SEQ-07 | NOT BUILT | Model-alarm circuit with slow and fast supply ramps |
| SEQ-08 | NOT BUILT | Model-alarm circuit with staged connector stimuli |
| SEQ-09 | NOT BUILT | Model-alarm circuit with watchdog and boot stimulus |
| CLK-01 | NOT BUILT | Model-alarm circuit with rated versus excess clock loads |
| CLK-02 | NOT BUILT | Model-alarm circuit with mismatched logic levels |
| CLK-03 | NOT COVERED | Crystal load and gain-margin calculation requires external-network design inspection |
| CLK-04 | NOT BUILT | Model-alarm circuit with missing or late clock |
| DIG-01 | NOT BUILT | Model-alarm circuit with incompatible I/O levels |
| DIG-02 | NOT BUILT | M5 model-alarm circuit |
| DIG-03 | NOT BUILT | Model-alarm circuit with two conflicting drivers |
| DIG-04 | NOT BUILT | Model-alarm circuit with undefined startup enable |
| DIG-05 | NOT COVERED | Translator pull-up/load rating requires external configuration inspection |
| BUS-01 | NOT BUILT | Model-alarm circuit with I2C edge and sink-current stimulus |
| BUS-02 | NOT COVERED | Address conflict requires network/protocol comparison across devices |
| BUS-03 | NOT BUILT | Model-alarm circuit with termination and receiver load |
| BUS-04 | NOT BUILT | Model-alarm circuit with unbiased idle bus |
| BUS-05 | NOT BUILT | Model-alarm circuit with excess and unpowered bus loads |
| ANA-01 | NOT BUILT | Model-alarm circuit with input common-mode boundary |
| ANA-02 | NOT BUILT | Model-alarm circuit with required output swing |
| ANA-03 | NOT BUILT | Model-alarm circuit with capacitive load transient |
| ANA-04 | NOT BUILT | Model-alarm circuit with ADC overvoltage stimulus |
| ANA-05 | NOT BUILT | Model-alarm circuit with reference load |
| ANA-06 | NOT BUILT | Model-alarm circuit with current-sense input conditions |
| ANA-07 | NOT BUILT | Model-alarm circuit with over-supply or unpowered mux input |
| ISO-01 | NOT BUILT | Model-alarm circuit with one isolator side unpowered |
| ISO-02 | NOT BUILT | Model-alarm circuit with cited CTR corner |
| ISO-03 | NOT BUILT | Model-alarm circuit with isolated supply light load |
| FLD-01 | NOT BUILT | Model-alarm circuit with cited digital-input bands |
| FLD-02 | NOT BUILT | Model-alarm circuit with reversed supply |
| FLD-03 | NOT BUILT | Model-alarm circuit with protection and surge stimulus |
| FLD-04 | NOT BUILT | Model-alarm circuit with loop compliance corners |
| FLD-05 | NOT BUILT | Model-alarm circuit with inrush and short stimuli |
| DRV-01 | NOT BUILT | Model-alarm circuit with inductive turn-off |
| DRV-02 | NOT BUILT | Model-alarm circuit with load inrush |
| DRV-03 | NOT BUILT | Model-alarm circuit with gate-drive boundary |
| DRV-04 | NOT BUILT | Model-alarm circuit with relay coil load |
| DRV-05 | NOT BUILT | Model-alarm circuit with simultaneous switch enables |
| CFG-01 | NOT BUILT | Model-alarm circuit with invalid strap stimulus |
| CFG-02 | NOT BUILT | Model-alarm circuit with late strap stimulus |
| CFG-03 | NOT BUILT | M5 model-alarm circuit |
| PIN-01 | PINOUT GATE M6 | Compare generated symbol pin numbers with cited package pinout |
| PIN-02 | PINOUT GATE M6 | Confirm the selected package variant and its pinout |
| PIN-03 | NOT BUILT | M5 model-alarm circuit |
| PIN-04 | NOT COVERED | DNC-net attachment requires connection inspection outside a model alarm |
| PIN-05 | PINOUT GATE M6 | Confirm discrete package pin order against the model |
| VAL-01 | NOT BUILT | Model-alarm circuit with timing or feedback value mutation |
| VAL-02 | NOT BUILT | Model-alarm circuit with cited component rating boundary |
| VAL-03 | NOT BUILT | Model-alarm circuit with capacitor derating and ripple |
