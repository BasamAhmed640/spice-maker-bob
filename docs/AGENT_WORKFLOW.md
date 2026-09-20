# What happens after GO

The elapsed clock beside GO shows hours:minutes:seconds. It updates while the agent
is quiet, includes cancellation cleanup, and keeps the final duration after success,
a blocked run or an error. The next accepted GO resets it. It measures elapsed time;
it does not predict completion or show the agent's private reasoning.

The application uses the selected agent for several sequential jobs. Short inputs share extraction context. Long inputs use smaller cached page batches,
with up to three requests in parallel and bounded recovery of failed batches.
In the Bob edition, all of these AI jobs use IBM Bob.

| Stage | Who does it | What actually happens |
| --- | --- | --- |
| Read | Local code | Registers and hashes the PDF, then reads its pages and text. |
| Extract | Selected agent | Returns the exact part's identity, pins, electrical requirements with conditions and citations, and a capability summary in structured JSON. A malformed reply gets one correction request before extraction fails. Matching cached extraction can avoid this call. |
| Check and bind | Local code | Validates the structure and cited text, plans and validates independent device-specific circuits (or uses a reviewed fixture), records unsupported rows, then freezes the specification and its hash. |
| Reinforce, when enabled | Selected agent plus local retrieval | Suggests supporting URLs such as application notes and errata. The app retrieves sources and records evidence. These references cannot replace frozen limits or produce a passing verdict. |
| Author | Selected agent | Writes a self-contained LTspice library against the frozen requirements and required ports. The application generates the symbol locally. On a repair turn, it receives the current model and the previous simulation failures. |
| Judge | LTspice plus local code | Runs the applicable test circuits, reads observed outputs, and compares measurements with the frozen requirements. The agent cannot award itself PASS or loosen the limits. |
| Repair | Selected agent, then the judge again | Revises the model and repeats the applicable checks. The best observed candidate is retained. By default, two consecutive turns without measurable improvement stop the loop; cancellation, errors, or an explicitly configured turn cap can also stop it. |
| Save | Local code | Publishes the model and symbol when available, the model card, row results, and recorded evidence. An early failure can leave diagnostics without a model. |

There is no separate AI reviewer that proves the design correct. PASS covers only
the characteristics the installed probes actually measure at their recorded conditions.
Unsupported rows remain visible. The exact reviewed LM358 datasheet now has nominal
gain, offset, bandwidth, slew-rate, bias-current and output-swing checks on both channels.
Full common-mode range, temperature corners and other gaps remain untested; see
LM358_VALIDATION.md. A successful file export alone does not establish device accuracy.

Existing candidates can skip further author calls if the simulator revalidates them.
The elapsed clock and the stage table remain visible during API waits; the stage
detail is an application event, not a live transcript of the agent's internal work.
