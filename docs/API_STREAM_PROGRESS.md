# API completion and progress in 1.2.1

The previous HTTP reader waited for socket EOF even after an SSE data: [DONE]
completion marker. A server could keep a completed stream open until the whole-turn
timeout. The reader now returns at the complete marker; the existing decoder still
requires finish_reason and rejects truncated/invalid streams. JSON responses are
not truncated when their content happens to mention a completion marker.

The before/after regression uses a real localhost HTTP server that sends a complete
response, then deliberately leaves the connection open. The old reader timed out at
2.07 seconds; the new reader returned successfully (test including teardown: 0.53 s).
This proves the transport defect. It does not prove that this particular live service
kept its connection open or that every user-visible delay came from this defect.

The shared HTTP transport supports connecting, receiving stream/response byte counts,
then stream completion and validation. Bob authoring continues through Bob Shell;
these HTTP transport checks do not claim streaming progress or measured latency
improvements for Bob Shell. Bob selection, reasoning settings and numerical acceptance
limits are unchanged.

Metadata requests now use introductions and complete labelled pin sections. Unknown
pin layouts fall back to all core pages. All electrical requirements still receive
every core page. The descriptive capability summary is limited to supplied pages and
is reporting material only, never a simulator capability claim. Five local vendor
PDFs were checked without an API call:

| Part | Electrical pages retained | Metadata pages | Metadata text before / after |
|---|---:|---:|---:|
| UCC28251 | 45 | 5 | 85762 / 13752 characters |
| LM358 | 32 | 4 | 65104 / 10994 characters |
| SN74LVC1GX04 | 16 | 4 | 25761 / 8741 characters |
| LTC7801 | 38 | 11 | 99344 / 34375 characters |
| S5D9 | 115 | 10 | 197532 / 25571 characters |

These are extraction-routing checks, not five generated/validated device models.
The UCC28251 live run in 1.1.11 returned multiple responses; one required repair for
missing typical values. Response receipt alone never counts as a validated model.
The portable storage behavior introduced in 1.2.0 remains in this release.
