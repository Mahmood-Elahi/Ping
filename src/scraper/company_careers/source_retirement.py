"""Explicit administrative retirement for one logical source scope.

Normal scans, failures, cooldowns, and registry loading never call this module.
Retirement requires deliberate evidence and changes only the selected owner's
lifecycle state; canonical jobs remain active while another owner is active.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.utils.source_scope import normalize_source_scope, source_scope_key


@dataclass(frozen=True)
class RetirementResult:
    state: dict[str, Any]
    source_scope_key: str
    owners_retired: int
    canonical_jobs_closed: int
    reason: str
    approved_by: str


def retire_source_scope(
    state: Mapping[str, Any],
    *,
    company: str,
    source_scope: str,
    reason: str,
    approved_by: str,
    retired_at: datetime,
) -> RetirementResult:
    """Retire one scope in an in-memory lifecycle state with explicit evidence."""
    reason = str(reason or "").strip()
    approved_by = str(approved_by or "").strip()
    if not reason or not approved_by:
        raise ValueError("source retirement requires non-empty reason and approved_by")
    if retired_at.tzinfo is None:
        retired_at = retired_at.replace(tzinfo=timezone.utc)
    timestamp = retired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    scope = normalize_source_scope(source_scope, default=False)
    logical_scope = source_scope_key(company, scope)
    updated = copy.deepcopy(dict(state))
    jobs = updated.get("jobs")
    if not isinstance(jobs, dict):
        raise ValueError("lifecycle state must contain a jobs mapping")

    retired = 0
    closed = 0
    for job in jobs.values():
        owners = job.get("scope_ownership") or {}
        owner = owners.get(logical_scope)
        if not isinstance(owner, dict):
            continue
        if owner.get("lifecycle_status") != "removed":
            retired += 1
        owner["lifecycle_status"] = "removed"
        owner["removed_at"] = owner.get("removed_at") or timestamp
        owner["retirement_reason"] = reason
        owner["retirement_approved_by"] = approved_by

        owner_states = {
            str(value.get("lifecycle_status") or "active")
            for value in owners.values()
            if isinstance(value, Mapping)
        }
        previous = str(job.get("lifecycle_status") or "active")
        if "active" in owner_states:
            job["lifecycle_status"] = "active"
            job["status"] = "active"
            job["removed_at"] = None
            job["missing_scan_count"] = 0
        elif "possibly_missing" in owner_states:
            job["lifecycle_status"] = "possibly_missing"
            job["status"] = "active"
            job["removed_at"] = None
            pending = [
                int(value.get("missing_scan_count", 0) or 0)
                for value in owners.values()
                if isinstance(value, Mapping)
                and value.get("lifecycle_status") == "possibly_missing"
            ]
            job["missing_scan_count"] = min(pending) if pending else 0
        else:
            job["lifecycle_status"] = "removed"
            job["status"] = "closed"
            job["removed_at"] = job.get("removed_at") or timestamp
            if previous != "removed":
                closed += 1

    updated["updated_at"] = timestamp
    updated.setdefault("source_retirements", {})[logical_scope] = {
        "company": str(company).strip(),
        "source_scope": scope,
        "reason": reason,
        "approved_by": approved_by,
        "retired_at": timestamp,
    }
    return RetirementResult(
        state=updated,
        source_scope_key=logical_scope,
        owners_retired=retired,
        canonical_jobs_closed=closed,
        reason=reason,
        approved_by=approved_by,
    )
