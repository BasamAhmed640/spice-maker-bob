"""BoardModeler: datasheet-grounded LTspice models plus schematic-level bring-up verification.

Honesty invariants enforced throughout this package:

* No status is ever recorded as PASS without an observed simulator artifact.
* A missing measurement, an uncovered assertion window, or an unresolved prerequisite
  yields UNKNOWN or BLOCKED, never PASS.
* No citation is reported as verified unless its excerpt appears on the cited page.
* Nothing is written into the LTspice installation directory.
"""

from __future__ import annotations

__version__ = "1.2.1"

__all__ = ["__version__"]
