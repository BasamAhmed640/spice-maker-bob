SYSTEM TEST SUITE -->> SPICE MAKER (board-level fault catalog, written 2026-09-25)

This suite tests whether the models and board checks catch real design mistakes across a whole I/O card. It is broad on purpose: 13 fault categories on 4 reference cards. The owner's two real misses are just 2 of the 66 cases (GND-01 and CLK-01). 12 of the cases are also run as boundary pairs.

HOW EVERY TEST WORKS
- Each fault is a mutation of a clean reference card, and each one runs as up to three variants:
  - Clean: the clean card must raise ZERO alarms and findings. It is the negative control, and any false alarm fails the suite.
  - Fault: the mutated card must be flagged by the RIGHT rule or alarm, naming the right part, pin or net, and citing the datasheet page. Being flagged by some other rule, or at the wrong place, counts as a miss.
  - Boundary pair: tests marked (+/-) also run just inside the limit, which must pass, and just outside it, which must flag. Default: 10% inside and 10% outside, or the nearest real part value. This checks the threshold itself, not only the obvious case.
- Corner runs (C) use datasheet min and max parameters, never extrapolated values: for example current limit at its min for start-up, and at its max for short-circuit stress.
- A fault the models cannot judge (for example, a pin model with no internal function) is reported as UNKNOWN with the reason. It counts as NOT detected, never as a pass.
- Ways a fault can be caught:
  - S: static check on the netlist plus pin data, no simulation;
  - A: an alarm inside a model during simulation;
  - Sc: a scenario check, named when one already exists in `verification/scenarios.py`;
  - C: a min/max corner run.

REFERENCE CARDS
All four cards are synthetic (labelled as such) and built in the `schematic/neutral.py` format.
- Card A, power and control:
  - 24 V input -> reverse protection -> eFuse -> buck 5 V -> buck 3.3 V -> LDO 1.8 V;
  - a supervisor with reset;
  - an MCU/FPGA pin model with straps and a watchdog;
  - an oscillator and clock buffer;
  - an EEPROM;
  - I2C sensors behind a level translator.
- Card B, analog input:
  - 4-20 mA and 0-10 V inputs -> protection -> in-amp/op amp -> ADC with separate AGND/DGND and an external reference;
  - an analog mux;
  - the digital side across a digital isolator;
  - an isolated DC-DC module.
- Card C, digital I/O:
  - 24 V digital inputs (IEC 61131-2) through optocouplers;
  - high-side smart switches driving a relay, a solenoid and a lamp;
  - a shift register / GPIO expander driving the output enables;
  - a MOSFET half-bridge.
- Card D, fieldbus:
  - isolated RS-485 and CAN transceivers with termination and fail-safe bias;
  - TVS on the bus lines;
  - a backplane connector with staggered (make-first) pins.

MUTATION OPERATORS (all exist or extend `schematic/mutate.py`; every mutation is recorded and the original stays byte-identical)
- Disconnect a pin.
- Merge two nets.
- Swap two pins.
- Change a value by a factor.
- Swap the part or package variant.
- Move a pull-up to another rail.
- Remove a part.
- Add loads.
- Change a stimulus: ramp speed, dip, polarity, surge.
- Change a sequence delay.
- Change the connector mating order.

CATALOG (card in brackets)

1. Grounds and returns
| ID | Fault injected | Caught by |
|---|---|---|
| GND-01 | AGND not tied to DGND at the ADC [B] | S required tie; A AGND-DGND beyond absolute max |
| GND-02 | Exposed pad / PowerPAD not connected to GND [A] | S required connection (datasheet page); A |
| GND-03 | Grounds bridged across an isolation barrier [B, D] | S isolation-domain merge |
| GND-04 | Signal crosses the backplane with no common ground or reference [D] | S interface without a reference; Sc receiver levels invalid |

2. Rails and regulators
| ID | Fault injected | Caught by |
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
| ID | Fault injected | Caught by |
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
| ID | Fault injected | Caught by |
|---|---|---|
| CLK-01 (+/-) | One oscillator driving 5 clock inputs (pass at its rated load, flag at 5 loads) [A] | S sum of input capacitance > rated load; Sc edge time and VIH at the far inputs |
| CLK-02 | Output/input standard mismatch: LVPECL/LVDS into LVCMOS, or a 3.3 V clock into a 1.8 V input [A] | S levels; A absolute max |
| CLK-03 | Crystal load capacitors wrong, or oscillator gain margin too low (commonly required >= 5) [A] | S calculation from the datasheet |
| CLK-04 | Clock missing or late at reset release; oscillator OE pin floating [A] | Sc `clock_missing`, `clock_late`; S floating input |

5. Digital levels and logic
| ID | Fault injected | Caught by |
|---|---|---|
| DIG-01 (+/-) | Level mismatch: 3.3 V output into a 1.8 V input, or 1.8 V output into a 3.3 V input (VOH < VIH) [A, C] | S; A |
| DIG-02 | Floating CMOS input with no pull [A, C] | S; A |
| DIG-03 | Contention: two push-pull or two enabled tri-state drivers on one net; TX tied to TX [C, D] | S more than one driver; A contention current |
| DIG-04 | Undefined output at power-up (MCU pins high-impedance during reset; shift register with no reset or OE control) driving an enable or actuator with no defining pull [C] | Sc `nominal_startup`: undefined state on a critical net |
| DIG-05 | Auto-direction level translator with a pull-up or load beyond its datasheet limits [A] | S |

6. Buses and interfaces
| ID | Fault injected | Caught by |
|---|---|---|
| BUS-01 (+/-) | I2C rise time over 1000 ns (standard mode) or 300 ns (fast mode), or VOL over 0.4 V at 3 mA with a pull-up that is too strong [A] | Sc measured rise time and VOL |
| BUS-02 | I2C address conflict from strap pins [A] | S |
| BUS-03 | RS-485/CAN termination missing, or more than two terminators [D] | S count; Sc differential amplitude vs receiver threshold |
| BUS-04 | RS-485 idle bus undefined: no fail-safe bias and no internal fail-safe [D] | S; Sc idle differential voltage |
| BUS-05 | Unit-load budget exceeded, or an unpowered node loads the bus [D] | S; Sc `partial_power` |

7. Analog chain
| ID | Fault injected | Caught by |
|---|---|---|
| ANA-01 (+/-) | Op-amp input common-mode range exceeded [B] | A |
| ANA-02 | Output cannot swing to the needed level, so ADC full scale is unreachable [B] | Sc output range vs required |
| ANA-03 | Op amp drives a capacitive load (ADC input, cable) without an isolation resistor, and the ringing does not die out [B] | Sc ballpark transient |
| ANA-04 | 0-10 V field signal into a 0-2.5 V ADC input [B] | A absolute max; Sc range |
| ANA-05 | Reference overloaded or tied to the wrong rail [B] | Sc reference within its tolerance under ADC load |
| ANA-06 | Current-sense amp common mode above its max (high-side on 24 V), or its inputs swapped [A, C] | A; Sc output sign or stuck at a rail |
| ANA-07 | Mux or analog switch signal beyond its supply, or applied while the mux is unpowered [B] | A injection current; Sc `partial_power` |

8. Isolation
| ID | Fault injected | Caught by |
|---|---|---|
| ISO-01 | One side unpowered, and the isolator's default output enables something [C, D] | Sc `partial_power` with default output states |
| ISO-02 (+/-) | Optocoupler at end-of-life CTR (current-transfer ratio) cannot pull its output to a valid level [C] | C min-CTR corner |
| ISO-03 | Unregulated isolated module below its minimum load: output rises above the downstream absolute max [B] | Sc; A |

9. Field side and protection
| ID | Fault injected | Caught by |
|---|---|---|
| FLD-01 (+/-) | 24 V digital-input thresholds or currents outside the IEC 61131-2 Type 1/2/3 bands [C] | Sc threshold and current sweep |
| FLD-02 | Reverse-polarity field supply with no effective protection [A, C] | Sc reverse stimulus; A absolute max |
| FLD-03 | TVS clamp above the downstream absolute max, or TVS standoff below the normal operating voltage [C, D] | S; Sc surge pulse (ballpark) |
| FLD-04 (+/-) | 4-20 mA loop cannot reach 20 mA at max loop resistance and minimum supply [B] | C + Sc |
| FLD-05 | Fuse/PTC trips on normal inrush, or does not trip on a short [A] | Sc inrush vs rating (ballpark) |

10. Drivers and loads
| ID | Fault injected | Caught by |
|---|---|---|
| DRV-01 | Inductive load switched with no clamp: switch voltage exceeds its absolute max [C] | Sc turn-off; A absolute max |
| DRV-02 | Smart-switch current limit below the load's inrush (lamp, capacitive load) [C] | Sc output never turns on |
| DRV-03 | MOSFET gate drive: logic-level drive on a standard-threshold MOSFET, or VGS above its max [C] | S; A |
| DRV-04 | Relay coil below its pick-up voltage or above its rating [C] | S; Sc |
| DRV-05 | Half-bridge with both switches on at power-up from undefined inputs [C] | Sc `nominal_startup`; A shoot-through current |

11. Configuration and straps
| ID | Fault injected | Caught by |
|---|---|---|
| CFG-01 | Strap pin floating, or an invalid combination (boot mode, address, voltage select) [A] | Sc `invalid_strap`; S |
| CFG-02 | Strap sampled before its driving rail or signal is valid [A] | Sc `late_strap` |
| CFG-03 | EEPROM/flash write-protect or chip-select floating [A] | S |

12. Pinout and library integrity (the "wiring must be absolute" rule)
| ID | Fault injected | Caught by |
|---|---|---|
| PIN-01 | Symbol pin numbers don't match the datasheet package (library error) [any] | S SC005 + pinout gate |
| PIN-02 | Wrong package variant: same part, different pinout [A] | S package check |
| PIN-03 | Only one of several same-name supply or ground pins connected [A, B] | S every same-name pin connected |
| PIN-04 | Do-not-connect pin connected to a net [A] | S |
| PIN-05 | Discrete pin order (SOT-23 MOSFET/BJT) doesn't match the model [C] | S |

13. Values and ratings
| ID | Fault injected | Caught by |
|---|---|---|
| VAL-01 | Value typo on a timing or feedback part (10 kOhm <-> 10 Ohm, 1 uF <-> 1 nF): soft-start, reset delay or frequency far off [A] | Sc timing vs its expected band |
| VAL-02 (+/-) | Power or voltage rating exceeded on a resistor, capacitor or connector pin [A, C] | Sc vs rating; S |
| VAL-03 | Ceramic capacitor below the regulator's minimum after DC-bias derating: ripple or ringing out of band [A] | S calculation; Sc ripple (ballpark) |

SCORING
- Detection: every implemented rule catches its fault at the right part, pin and page. Target: 100%.
- False alarms on the four clean cards. Target: 0.
- Boundary pairs: the inside value passes and the outside value flags. Target: 100%.
- UNKNOWN count, with reasons. It is reported and never hidden; it should shrink as families are added.
- Run time per card: AVG mode for power-up and fault sweeps (target: seconds per run); SW mode only for the ripple and transient checks.
- Coverage matrix, category x detection layer: every rule and alarm has at least one fault test and one clean control, plus a boundary pair when it is numeric.

WHEN EACH PART OF THE SUITE COMES ALIVE (agent-prompt milestones)
- M3/M5: GND, PIN, CFG-03, DIG-02, PWR-05/06 (static checks and pin-model alarms).
- M4/M8: PWR, SEQ, VAL (regulators in both modes plus power-up scenarios) on card A.
- M9+: CLK, DIG, BUS, ANA, ISO, FLD, DRV, as each family template arrives. Until a family arrives, its cases may only be UNKNOWN (pin model only) or caught statically.

OUT OF SCOPE
Signal integrity of fast links, EMI, precise thermal behavior, firmware logic, layout (trace inductance, ground bounce).

---

## Import note: counts, activation, and open interpretations

The catalog above contains 66 unique case IDs in 13 categories. Twelve IDs carry
the `(+/-)` boundary-pair marker: PWR-01, PWR-08, SEQ-02, SEQ-09, CLK-01,
DIG-01, BUS-01, ANA-01, ISO-02, FLD-01, FLD-04, and VAL-02. These are planned
tests, not measured outcomes. The four cards are synthetic; GND-01 and CLK-01
are synthetic stand-ins for the owner's reported mistakes until the real board
is available.

Activation follows the milestone schedule above: M3/M5 covers GND, PIN,
CFG-03, DIG-02, and PWR-05/06; M4/M8 adds PWR, SEQ, and VAL on card A; M9+
adds CLK, DIG, BUS, ANA, ISO, FLD, and DRV as their families arrive. Overlap
between the early PWR-05/06 checks and later full PWR coverage is intentional.

Open interpretation points to settle before scoring those cases:

- Requiring a datasheet page for *every* finding is unclear for board-defined
  topology faults such as GND-03, DIG-03, and BUS-02. These need an explicit
  applicable source or board rule; a datasheet citation must not be invented.
- PWR-08 asks for package dissipation checks while precise thermal behavior is
  out of scope. A bounded check needs declared ambient and package assumptions;
  otherwise that portion remains UNKNOWN.
- The clean-card target of zero findings needs to distinguish actual fault
  findings from UNKNOWN coverage for unimplemented families. UNKNOWN never
  counts as a detected fault or a clean-card verification PASS.

## Per-case build status

`NOT BUILT` means no clean control, fault mutation, boundary pair, or measured
verdict is claimed here. The activation column records the first planned
milestone; PWR-05/06 also belong to the later M4/M8 full power coverage.
The assignment totals are 13 at M3/M5, 17 at M4/M8 on card A, one at M8 on
card D, and 35 at M9+.

| Case ID | Status | First activation |
| --- | --- | --- |
| GND-01 | NOT BUILT | M3/M5 |
| GND-02 | NOT BUILT | M3/M5 |
| GND-03 | NOT BUILT | M3/M5 |
| GND-04 | NOT BUILT | M3/M5 |
| PWR-01 | NOT BUILT | M4/M8 |
| PWR-02 | NOT BUILT | M4/M8 |
| PWR-03 | NOT BUILT | M4/M8 |
| PWR-04 | NOT BUILT | M4/M8 |
| PWR-05 | NOT BUILT | M3/M5 |
| PWR-06 | NOT BUILT | M3/M5 |
| PWR-07 | NOT BUILT | M4/M8 |
| PWR-08 | NOT BUILT | M4/M8 |
| SEQ-01 | NOT BUILT | M4/M8 |
| SEQ-02 | NOT BUILT | M4/M8 |
| SEQ-03 | NOT BUILT | M4/M8 |
| SEQ-04 | NOT BUILT | M4/M8 |
| SEQ-05 | NOT BUILT | M4/M8 |
| SEQ-06 | NOT BUILT | M4/M8 |
| SEQ-07 | NOT BUILT | M4/M8 |
| SEQ-08 | NOT BUILT | M8 (Card D) |
| SEQ-09 | NOT BUILT | M4/M8 |
| CLK-01 | NOT BUILT | M9+ |
| CLK-02 | NOT BUILT | M9+ |
| CLK-03 | NOT BUILT | M9+ |
| CLK-04 | NOT BUILT | M9+ |
| DIG-01 | NOT BUILT | M9+ |
| DIG-02 | NOT BUILT | M3/M5 |
| DIG-03 | NOT BUILT | M9+ |
| DIG-04 | NOT BUILT | M9+ |
| DIG-05 | NOT BUILT | M9+ |
| BUS-01 | NOT BUILT | M9+ |
| BUS-02 | NOT BUILT | M9+ |
| BUS-03 | NOT BUILT | M9+ |
| BUS-04 | NOT BUILT | M9+ |
| BUS-05 | NOT BUILT | M9+ |
| ANA-01 | NOT BUILT | M9+ |
| ANA-02 | NOT BUILT | M9+ |
| ANA-03 | NOT BUILT | M9+ |
| ANA-04 | NOT BUILT | M9+ |
| ANA-05 | NOT BUILT | M9+ |
| ANA-06 | NOT BUILT | M9+ |
| ANA-07 | NOT BUILT | M9+ |
| ISO-01 | NOT BUILT | M9+ |
| ISO-02 | NOT BUILT | M9+ |
| ISO-03 | NOT BUILT | M9+ |
| FLD-01 | NOT BUILT | M9+ |
| FLD-02 | NOT BUILT | M9+ |
| FLD-03 | NOT BUILT | M9+ |
| FLD-04 | NOT BUILT | M9+ |
| FLD-05 | NOT BUILT | M9+ |
| DRV-01 | NOT BUILT | M9+ |
| DRV-02 | NOT BUILT | M9+ |
| DRV-03 | NOT BUILT | M9+ |
| DRV-04 | NOT BUILT | M9+ |
| DRV-05 | NOT BUILT | M9+ |
| CFG-01 | NOT BUILT | M9+ |
| CFG-02 | NOT BUILT | M9+ |
| CFG-03 | NOT BUILT | M3/M5 |
| PIN-01 | NOT BUILT | M3/M5 |
| PIN-02 | NOT BUILT | M3/M5 |
| PIN-03 | NOT BUILT | M3/M5 |
| PIN-04 | NOT BUILT | M3/M5 |
| PIN-05 | NOT BUILT | M3/M5 |
| VAL-01 | NOT BUILT | M4/M8 |
| VAL-02 | NOT BUILT | M4/M8 |
| VAL-03 | NOT BUILT | M4/M8 |

The source schedule names CFG-03 at M3/M5 but leaves CFG-01/02 unassigned;
they are listed at M9+ as the remaining configuration cases, pending an explicit
family milestone. The schedule groups SEQ with M4/M8 work on card A, while
SEQ-08 explicitly uses card D. Its first activation is therefore assigned to
M8 (Card D); execution still needs the card-D fixture and cannot be inferred
from card A.
