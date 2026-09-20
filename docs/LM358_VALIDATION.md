# LM358 nominal model validation (1.1.5)

The exact TI SLOS068AB datasheet SHA256 `58c89c68aff6b6025555f3b8bad9af6a825288cd96631deadc1c616a6062af46` and target `LM358` select a reviewed extraction profile. LM358A/B and other PDFs cannot match it. The 42 recorded rows cover 32 measured nominal comparisons and 10 explicit coverage gaps. This is not a claim to extract or qualify every page of the 68-page family datasheet.

Citations are checked against the actual PDF before authoring. Both channels are tested for offset, input bias and offset current, open-loop gain, bandwidth, rising/falling slew and rail swing. Supply current is measured for the dual package and divided by two. Typical values retain the existing +/-10% comparison rule; they are not manufacturer guarantees. Maximum limits are checked separately. A zero-bias, zero-offset ideal amplifier fails the typical-value checks.

The selected agent still writes the model. No prewritten device model or alternate provider is silently substituted. Repeat builds revalidate existing model bytes with LTspice rather than re-authoring. The exported op-amp example is a follower step circuit, not the former regulator example.

## Scope

Nominal single-supply sample points at 25 C only. Noise, temperature drift/corners, full common-mode and supply ranges, PSRR, CMRR, channel separation, output-current limiting and short-circuit behavior are not qualified. Read the generated MODEL_CARD.md before using a model for a design decision. The current UI's NOT_APPLICABLE rows mean no applicable measurement instrument is provided; they are **untested**, not passing device behaviors.

## Instrument qualification

Real LTspice tests recover synthetic constants independently on both channels. A tenfold capacitance mutation must produce tenfold lower bandwidth. The complex raw reader is checked against the analytical amplitude and phase of an RC low-pass. Truncated AC files must fail. AC open-loop gain is measured from the actual differential input under series feedback injection, avoiding unstable inductor/servo operating points.
