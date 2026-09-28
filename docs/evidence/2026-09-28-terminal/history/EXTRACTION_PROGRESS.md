# Selected datasheet and extraction progress — 1.1.11

Reusing a save folder previously let model extraction read every datasheet stored
there. The observed UCC28251 run included older LM358 and oscillator PDFs: 93
electrical pages and 18 initial batches. The model pipeline now passes only the
selected PDF's document ID, before text collection, authorization and cache lookup.
The same UCC28251 document produces 45 electrical pages and 9 initial batches.
Manufacturing appendices remain inventoried separately. No API calls were needed
to verify the before/after plan. Older files remain available on disk.

The previous UI repeated "request 1/2" for different batches, making real progress
look like a stall. Progress now shows completed/total batches, active calls, retries
and elapsed time, refreshed every five seconds while calls wait. Only successful
batches count as completed. Cancel still cannot retract work already accepted by
the API provider; the UI names the wait for active calls to return.

The selected provider, reasoning level, numerical limits and accuracy checks are
unchanged. Reducing unrelated work does not promise a fixed model-generation time
or establish the accuracy of a completed UCC28251 model.

Both 1.1.11 installers were built on GitHub's Windows runner, installed locally
with exit 0, and verified against build hashes. Both installed GUIs opened and
their seven TLV9002 evaluation checks passed. Source tests passed: 1,186 general,
1,076 Bob, with additional marked tests excluded as recorded in STATUS.md.
The source, package, splash and installed window versions now agree. Code →
Download ZIP contains this same animated Install.exe. The API key is not bundled.
