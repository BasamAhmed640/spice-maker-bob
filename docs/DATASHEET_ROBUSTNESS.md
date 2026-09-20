# Datasheet robustness and honest model coverage

The model path now plans independent device-specific test circuits before writing
the model. The existing reviewed LM358 path remains available. Other parts are no
longer forced into regulator or simple logic fixtures that cannot represent their pins.

The selected backend extracts the exact variant, physical pins, quantities and cited
conditions. Long documents use bounded, cached page batches, with smaller recovery
requests after interrupted or invalid responses. A saved successful batch is reused.
Unknown units are preserved as explicit gaps; they cannot produce a numeric verdict.
Absolute-maximum ratings remain stress information, not operating acceptance limits.

The independent planner supplies declarative primitive circuits and measurements.
Local code rejects injected commands, invented terminals, unsupported quantities,
missing thresholds, and measurements of a forced source instead of a device response.
Simple, unambiguous amplifier fixtures have reviewed local compilers. The specification,
test circuits and limits are frozen before model authoring; the author cannot edit them.

Real LTspice runs measure the resulting library. Structural library errors are caught
before simulation; an invalid simulation defers the remaining probes until repair.
Failures, including UNKNOWN outcomes, reach the first repair after a restart. The
best observed candidate is retained. An unsuccessful API turn that changes no model
bytes stops instead of repeatedly simulating the same file. Transport timeouts cover
the entire streamed response, even when data keeps arriving. These changes do not
change the selected model, provider or reasoning level.

Symbols are still generated locally from the declared physical ports. Unambiguous
ground-reference and library-ending corrections retain the original library and
require re-simulation. REAL/DOUBLE waveform files are decoded according to their
declared layout, and long Windows paths preserve the simulator's output basename.

## What a result means

PASS for a row means the recorded circuit and operating point produced an observed
value inside that row's limits. It does not validate an entire operating range,
temperature distribution, protocol, oscillator startup, or every possible circuit.
Untested numeric requirements remain UNKNOWN. An otherwise passing candidate with
coverage gaps is published with an overall UNKNOWN / limited-coverage result.

Free-form fixture planning still requires scrutiny. A model can fit its generated
fixtures and fail a different load or supply arrangement. Independent checks are
necessary before relying on a model in a hardware design. The five-device diagnostic
matrix is deliberately kept separate from unit tests; passing software tests alone
does not certify any generated device model.

## Verification

Regression coverage includes streamed-response completion and deadlines, compound
units and relative limits, Unicode pin polarity, frozen circuits, a known-gain
negative control, measured gain-bandwidth, ground-offset invariance, library syntax,
and long-path execution. See STATUS.md for observed commands and results.


Fixture pin names are resolved to connected circuit nodes before freezing. The reserved
LTspice top-level GND alias is renamed when it represents a distinct sense node.
A regulator with one explicitly identified regulated output and an unambiguous
nominal fixture voltage can receive a local NODESET hint. This seeds DC iteration;
it is not a voltage constraint. The modified library is re-simulated, and a real
negative-control test verifies that the hint cannot turn a wrong output into a pass.
