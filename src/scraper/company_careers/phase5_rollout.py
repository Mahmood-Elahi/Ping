"""Offline-only decision contracts for a future Phase 5 source rollout."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class CanaryOutcome(str, Enum):
    PASS_COMPLETE = "PASS_COMPLETE"
    PASS_PARTIAL = "PASS_PARTIAL"
    FAIL_IDENTITY = "FAIL_IDENTITY"
    FAIL_CONTRACT = "FAIL_CONTRACT"
    FAIL_TRANSIENT = "FAIL_TRANSIENT"


CANARY_DECISIONS: dict[CanaryOutcome, str] = {
    CanaryOutcome.PASS_COMPLETE: "ELIGIBLE_IF_ALL_OTHER_GATES_PASS",
    CanaryOutcome.PASS_PARTIAL: "DO_NOT_ACTIVATE",
    CanaryOutcome.FAIL_IDENTITY: "DO_NOT_ACTIVATE",
    CanaryOutcome.FAIL_CONTRACT: "DO_NOT_ACTIVATE",
    CanaryOutcome.FAIL_TRANSIENT: "DO_NOT_ACTIVATE_RETRY_MANUALLY_LATER",
}


@dataclass(frozen=True)
class ActivationDecision:
    activate: bool
    decision: str
    unmet_gates: tuple[str, ...]


def evaluate_activation_gate(
    canary: CanaryOutcome | str,
    *,
    migration_deployed_and_verified: bool,
    production_config_diff_reviewed: bool,
    rollback_prepared: bool,
) -> ActivationDecision:
    """Apply the fail-closed activation gate without changing any state."""
    try:
        outcome = canary if isinstance(canary, CanaryOutcome) else CanaryOutcome(str(canary))
    except ValueError:
        return ActivationDecision(False, "DO_NOT_ACTIVATE", ("known_canary_outcome",))

    gates = {
        "canary_PASS_COMPLETE": outcome is CanaryOutcome.PASS_COMPLETE,
        "migration_deployed_and_verified": migration_deployed_and_verified,
        "production_config_diff_reviewed": production_config_diff_reviewed,
        "rollback_prepared": rollback_prepared,
    }
    unmet = tuple(name for name, ready in gates.items() if not ready)
    return ActivationDecision(
        activate=not unmet,
        decision="ACTIVATE" if not unmet else "DO_NOT_ACTIVATE",
        unmet_gates=unmet,
    )


def classify_canary_evidence(evidence: Mapping[str, Any]) -> CanaryOutcome:
    """Convert the central finalizer's evidence into one exact outcome.

    Ordering is intentional: identity and protocol contradictions outrank
    partial observations; transient transport failures remain distinguishable.
    Only a centrally authoritative, complete result can pass completely.
    """
    if not bool(evidence.get("employer_identity_valid")) or not bool(
        evidence.get("board_identity_valid")
    ) or bool(evidence.get("identity_contradiction")):
        return CanaryOutcome.FAIL_IDENTITY
    if bool(evidence.get("transient_failure")):
        return CanaryOutcome.FAIL_TRANSIENT
    if any(
        bool(evidence.get(field))
        for field in (
            "pagination_contradiction",
            "stable_id_contradiction",
            "terminal_contradiction",
            "mutation_failure",
            "budget_exhausted",
        )
    ):
        return CanaryOutcome.FAIL_CONTRACT
    complete = all(
        bool(evidence.get(field))
        for field in (
            "scope_correct",
            "inventory_complete",
            "terminal_evidence_valid",
            "central_finalizer_authoritative",
        )
    )
    if complete:
        return CanaryOutcome.PASS_COMPLETE
    if int(evidence.get("jobs_observed") or 0) > 0:
        return CanaryOutcome.PASS_PARTIAL
    return CanaryOutcome.FAIL_CONTRACT
