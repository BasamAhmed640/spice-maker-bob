# Standard IC symbols

The application draws a plain rectangular LTspice symbol for every published model,
including cached builds. The physical package outline is not needed for simulation.

The selected agent extracts the datasheet pin identities, physical pin numbers,
directions and supply domains. These are recorded in the frozen specification.
Matching pin directions guide placement; the actual `.subckt` declaration determines
every `PINATTR SpiceOrder`. Package pin numbers and model port positions are different
concepts and must not be substituted for one another.

Symbol generation uses no inference request. Both editions use the same renderer:

- Consistent grid, visible leads and top-to-bottom rows.
- Supply pins grouped after signal pins.
- Body width reserves space for labels on both sides; body height contains all pins.
- Reference and model name outside the body.
- No agent-authored geometry is published, even if its electrical attributes are valid.
- Every pin name must match its exact position in the model declaration, not merely
  have a unique order number.

IBM Bob can provide the extracted pin data and author the electrical model through
the existing backend. The application owns layout and checks. This change does not
establish that every extracted pin map is correct, certify a new provider, or extend
the electrical behaviors tested for a device.

## Verification

`tests/reporting/test_symbol_layout.py` exercises the LM358, a one-pin device,
a 64-pin device, long opposing labels, wrong electrical orders, and replacement of
bad agent geometry. Its LTspice-marked test generates a real schematic and inspects
LTspice's actual netlist to verify all eight LM358 connections. Geometry tests alone
cannot prove rendering, so the LM358 was also opened and visually inspected in
LTspice 26.0.0.3 on Windows.

Existing placements in an already-open LTspice schematic may retain their old symbol.
Use the newly generated symbol when inserting a component. The program does not
rewrite existing user schematics.
