# Template catalog — which part families to build next

Status as of 2026-09-25. One family has a complete, reusable slice: **peak-current-mode
buck (`peak_current_buck_v1`)**: model, parameter-to-datasheet mapping by statement and
unit, pin roles with aliases, a deterministic current-limit bench, pre-freeze fixture
rules, and explicit supported/unsupported behaviours in
`src/boardmodeler/models/peak_current_buck.json`. Every other family below is **not built**;
nothing here claims accuracy for a family that has not been measured.

## How the next families are ranked

Target use: system-level simulation of **I/O cards** (digital and analog I/O, fieldbus and
isolated channels, card power entry). Each family is scored on four questions:

1. **Prevalence** — how many of them sit on a typical I/O card.
2. **System value** — does a board-level simulation need its behaviour (sequencing,
   protection, thresholds, bus levels), or is a vendor/ideal model enough?
3. **Probe fit** — can today's probes judge its rows (DC levels, thresholds, currents,
   delays), or does it need new measurement machinery?
4. **Reuse** — how much of an existing template, primitive or bench it can reuse.

## Ranked families

| Rank | Family | Why on I/O cards | Rows a template must judge | Reuse / new work | Effort |
|---|---|---|---|---|---|
| 1 | **LDO regulator** | several per card: analog rails, references, isolated-side rails | VOUT/VREF, dropout, UVLO, EN threshold, current limit, IQ, shutdown current, soft-start | `BM_REG_LDO` family + regulator probes exist; needs the buck slice's statement mapping, pin aliases and pre-freeze rules | low |
| 2 | **Load switch / eFuse / hot-swap** | card power entry, inrush and fault isolation | Ron, current limit, inrush slew, UVLO/OVLO, fault response, reverse blocking | `load_switch` family exists; add a current-limit bench after turn-on (same timing rule as the buck) | low–medium |
| 3 | **Supervisor / reset** | sequencing and brown-out on every card | threshold + hysteresis, reset delay, output topology | `supervisor` family exists; mapping + aliases only | low |
| 4 | **Digital isolator** | every isolated digital channel | propagation delay, input thresholds, default (fail-safe) output, supply current | new A-device buffer/delay template (8-node gates on the part's own ground, as the buck now does) | low–medium |
| 5 | **RS-485 / RS-422 transceiver** | fieldbus I/O | driver VOD, receiver ±200 mV thresholds, fail-safe, common-mode range, enable timing | new template; differential probes exist in part (op-amp) | medium |
| 6 | **CAN transceiver** | fieldbus I/O | dominant/recessive levels, thresholds, TXD-dominant timeout, standby | shares most of rank 5's bench | medium |
| 7 | **Comparator** | digital-input conditioning, threshold detection | threshold, hysteresis, propagation delay, output topology | A-device SCHMITT + output stage; small | low |
| 8 | **Op-amp / current-sense amplifier** | analog inputs, 4–20 mA and shunt sensing | Vos, Ib, gain, GBW, slew, swing, CMRR, IQ | op-amp probes proven (LM358: 32/32 PASS via reviewed rows); needs a parameterised macro template | medium |
| 9 | **Voltage reference** | ADC/DAC reference on analog cards | VOUT, load/line regulation, start-up | small template; tempco stays unsupported | low |
| 10 | **Low-/high-side smart switch** | digital outputs driving relays/solenoids | Ron, current limit, inductive clamp, fault flag | needs an inductive-load bench and clamp probes | medium–high |
| 11 | **Analog switch / multiplexer** | channel selection | Ron, leakage, switching time | small template; charge injection unsupported at first | medium |
| 12 | **4–20 mA transmitter/receiver** | analog current-loop I/O | loop current range, compliance, span/zero | builds on ranks 1 and 8 | medium–high |
| 13 | **Isolated DC-DC driver (push-pull / flyback)** | isolated channel power | switching frequency, UVLO, current limit | reuses the buck's clock/limit logic and bench timing rule | medium–high |
| 14 | **TVS / ESD diode** | every connector | clamp voltage, leakage, capacitance | diode primitives; vendor models are often available instead | low |

## What each new family must deliver (the buck slice is the pattern)

1. A contract JSON: parameters with **statement + unit sources** (row ids from a fresh
   extraction are generic, so suffix-only mapping silently fell back to defaults),
   provenance `cited_row` / `derived_from_bounds` / `template_default`, pin roles with
   aliases, and supported/unsupported behaviour lists.
2. A model whose logic gates tie unused inputs to the model's own ground pin (lint
   `a_device_input_ground` enforces it), with no B-source latches.
3. Deterministic benches whose timing comes from cited rows, accepted by the same
   pre-freeze rules as an AI-planned fixture.
4. LTspice evidence on a second compatible part with its own cited parameters.

## Remaining work on the buck slice itself

* The pipeline still asks the AI planner for every row; the deterministic current-limit
  bench is proven but not yet used by `bind()` to skip planning for the rows it covers.
* Frozen-spec FAIL/UNKNOWN rows stay as measured: VOUT_MIN 0.79985 V, switch-current gain
  9 A/V, frozen ILIM bench 10.6 A (its COMP shunt), SS-match, VREF/Dmax not settled, min
  on-time transition missing.
* EN and UVLO hysteresis and the base shutdown current are template defaults (no row
  source); RON takes the first cited on-resistance row, whose condition may differ from
  the bench's.
