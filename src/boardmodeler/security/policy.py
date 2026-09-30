"""Data egress policy (D11, A8).

Defaults are the conservative ones: nothing leaves the machine unless the
document is public *and* the document record allows remote inference *and* the
user passed ``--allow-remote`` (CLI) or confirmed the dialog (GUI).

There is no telemetry, and there is no path that reaches the network without
producing a :class:`boardmodeler.domain.records.DataDisclosure` first.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from boardmodeler.domain.records import DataDisclosure, DocumentRecord

__all__ = [
    "EGRESS_DENIED_CLASSIFICATIONS",
    "EGRESS_PERMITTED_CLASSIFICATIONS",
    "DataPolicy",
    "EgressDecision",
    "build_disclosure",
]

EGRESS_PERMITTED_CLASSIFICATIONS: tuple[str, ...] = ("public", "synthetic_fixture")
EGRESS_DENIED_CLASSIFICATIONS: tuple[str, ...] = ("internal", "confidential", "unknown")


class DataPolicy(BaseModel):
    """What may be sent to a remote provider."""

    model_config = ConfigDict(extra="forbid")

    allow_remote: bool = False
    """Set only by ``--allow-remote`` / the GUI confirmation dialog."""

    permitted_classifications: list[str] = Field(
        default_factory=lambda: list(EGRESS_PERMITTED_CLASSIFICATIONS)
    )
    deny_unknown_classification: bool = True
    allow_bob_shell: bool = False
    """Bob Shell executes tools non-interactively and is off unless policy *and*
    the ``--allow-bob-shell`` switch are both present."""
    allow_bob_shell_for_classifications: list[str] = Field(
        default_factory=lambda: ["public", "synthetic_fixture"]
    )
    cache_extraction: bool = True
    """Extraction artifacts are cached by prompt hash, so a repeat run makes
    zero inference requests."""

    def describe(self) -> str:
        """Human-readable summary recorded with every disclosure."""
        kinds = ",".join(self.permitted_classifications)
        return (
            f"remote={'allowed' if self.allow_remote else 'disabled'}; "
            f"classifications={kinds}; bob_shell={'on' if self.allow_bob_shell else 'off'}; "
            f"cache={'on' if self.cache_extraction else 'off'}"
        )


class EgressDecision(BaseModel):
    """Result of the egress check for one document, with a machine-checkable code."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: Literal[
        "allowed",
        "remote_not_enabled",
        "document_remote_inference_not_allowed",
        "classification_not_permitted",
        "classification_unknown_and_denied",
    ]
    detail: str


def evaluate_egress(
    policy: DataPolicy, doc: DocumentRecord, *, allow_remote: bool | None = None
) -> EgressDecision:
    """Decide whether ``doc`` may be sent to a remote provider.

    ``allow_remote`` overrides the policy flag for this call (CLI ``--allow-remote``
    is applied here so the same function serves both surfaces). Every failure
    path returns a distinct reason code — the caller reports it instead of
    falling back to another provider.
    """
    remote = policy.allow_remote if allow_remote is None else allow_remote

    if not remote:
        return EgressDecision(
            allowed=False,
            reason="remote_not_enabled",
            detail="remote inference is disabled; pass --allow-remote to enable it",
        )
    if not doc.remote_inference_allowed:
        return EgressDecision(
            allowed=False,
            reason="document_remote_inference_not_allowed",
            detail=(
                f"document {doc.doc_id!r} has remote_inference_allowed=False; "
                "nothing about this document may leave the machine"
            ),
        )
    if doc.classification == "unknown" and policy.deny_unknown_classification:
        return EgressDecision(
            allowed=False,
            reason="classification_unknown_and_denied",
            detail=(
                f"document {doc.doc_id!r} is classified 'unknown'; set its classification "
                "before enabling remote inference for it"
            ),
        )
    if doc.classification not in policy.permitted_classifications:
        return EgressDecision(
            allowed=False,
            reason="classification_not_permitted",
            detail=(
                f"document {doc.doc_id!r} is classified {doc.classification!r}; the policy "
                f"permits only {sorted(policy.permitted_classifications)}"
            ),
        )
    return EgressDecision(
        allowed=True,
        reason="allowed",
        detail=f"document {doc.doc_id!r} is {doc.classification!r} and remote inference is enabled",
    )


def build_disclosure(
    *,
    provider: str,
    endpoint: str | None,
    page_ranges: dict[str, list[int]],
    chars: int,
    policy: DataPolicy,
) -> DataDisclosure:
    """Record exactly what was sent, before the first call."""
    return DataDisclosure(
        provider=provider,
        endpoint=endpoint,
        doc_ids=sorted(page_ranges),
        page_ranges={k: sorted(v) for k, v in sorted(page_ranges.items())},
        chars=chars,
        policy=policy.describe(),
    )
