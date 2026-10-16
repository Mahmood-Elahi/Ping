"""Offline validation for the Workday Batch 1 activation package.

This module never writes the production registry. It accepts only the complete
reviewed baseline or the complete reviewed activation, rejects partial/drifted
states, and keeps activation construction behind an explicit approval flag.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "config" / "target_companies.yaml"
TESTED_CANDIDATES = (
    ROOT / "config" / "target_companies.workday_identity_batch1_candidates.yaml"
)
PROPOSAL = (
    ROOT / "config" / "target_companies.workday_identity_batch1_activation_proposal.yaml"
)
ROLLBACK = (
    ROOT / "config" / "target_companies.workday_identity_batch1_activation_rollback.yaml"
)

APPROVED_COMPANIES = (
    "CrowdStrike",
    "Salesforce",
    "Boston Dynamics",
    "Cadence Design Systems",
    "Blue Origin",
    "Medtronic",
    "Visa",
    "NXP Semiconductors",
    "Intel",
    "KLA",
    "Marvell Technology",
    "TD Bank",
    "Analog Devices",
)
MIGRATION_REVIEW_COMPANIES = ("Adobe", "RBC", "CIBC")


class ActivationApprovalRequired(PermissionError):
    """Raised when code attempts to construct an unapproved activation."""


def load_registry(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("target_companies")
    if not isinstance(rows, list):
        raise ValueError(f"Registry has no target_companies list: {path}")
    return data


def rows_by_name(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or name in indexed:
            raise ValueError(f"Missing or duplicate canonical company: {name!r}")
        indexed[name] = row
    return indexed


def logical_scope_key(row: Mapping[str, Any]) -> tuple[str, str]:
    name = " ".join(str(row.get("name") or "").split()).casefold()
    scope = " ".join(str(row.get("source_scope") or "default").split()).casefold()
    return name, scope


def _validate_unique_scopes(rows: Sequence[Mapping[str, Any]]) -> None:
    keys = [logical_scope_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Registry contains duplicate canonical company/scope identities")


def validate_package(
    *,
    production: Path = PRODUCTION,
    tested_candidates: Path = TESTED_CANDIDATES,
    proposal: Path = PROPOSAL,
    rollback: Path = ROLLBACK,
) -> dict[str, Any]:
    production_rows = load_registry(production)["target_companies"]
    tested_rows = load_registry(tested_candidates)["target_companies"]
    proposal_rows = load_registry(proposal)["target_companies"]
    rollback_rows = load_registry(rollback)["target_companies"]

    production_by_name = rows_by_name(production_rows)
    tested_by_name = rows_by_name(tested_rows)
    proposal_by_name = rows_by_name(proposal_rows)
    rollback_by_name = rows_by_name(rollback_rows)
    expected = set(APPROVED_COMPANIES)
    registry_states: set[str] = set()

    if tuple(row["name"] for row in proposal_rows) != APPROVED_COMPANIES:
        raise ValueError("Activation proposal company set/order is not the reviewed 13")
    if tuple(row["name"] for row in rollback_rows) != APPROVED_COMPANIES:
        raise ValueError("Rollback company set/order is not the reviewed 13")
    if set(proposal_by_name) != expected or set(rollback_by_name) != expected:
        raise ValueError("Activation or rollback package contains an unapproved company")
    if expected.intersection(MIGRATION_REVIEW_COMPANIES):
        raise ValueError("Migration-review companies may not enter the proposal")

    for name in APPROVED_COMPANIES:
        if proposal_by_name[name] != tested_by_name[name]:
            raise ValueError(f"Proposal differs from the exact tested candidate: {name}")
        if production_by_name[name] == rollback_by_name[name]:
            registry_states.add("baseline")
        elif production_by_name[name] == proposal_by_name[name]:
            registry_states.add("activated")
        else:
            raise ValueError(
                f"Production differs from both rollback and proposal: {name}"
            )
        if logical_scope_key(proposal_by_name[name])[1] != "default":
            raise ValueError(f"Approved company changed logical source scope: {name}")
        evidence = proposal_by_name[name].get("workday_identity") or {}
        expected_host = str(evidence.get("expected_apply_hostname") or "").casefold()
        allowed_hosts = {
            str(host).casefold()
            for host in proposal_by_name[name].get("allowed_apply_hosts") or []
        }
        if not expected_host or expected_host not in allowed_hosts:
            raise ValueError(f"Expected Workday apply host is not permitted: {name}")

    if len(registry_states) != 1:
        raise ValueError(
            "Production contains a partial Workday Batch 1 activation: "
            f"{sorted(registry_states)}"
        )

    _validate_unique_scopes(production_rows)
    _validate_unique_scopes(proposal_rows)
    _validate_unique_scopes(rollback_rows)
    return {
        "status": "PASS",
        "network_requests": 0,
        "production_writes": 0,
        "registry_state": next(iter(registry_states)),
        "approved_companies": list(APPROVED_COMPANIES),
        "migration_review_companies": list(MIGRATION_REVIEW_COMPANIES),
    }


def build_activation_registry(
    *,
    activation_approved: bool,
    production: Path = PRODUCTION,
    tested_candidates: Path = TESTED_CANDIDATES,
    proposal: Path = PROPOSAL,
    rollback: Path = ROLLBACK,
) -> dict[str, Any]:
    """Return an approved in-memory merge; never write a registry file."""
    validate_package(
        production=production,
        tested_candidates=tested_candidates,
        proposal=proposal,
        rollback=rollback,
    )
    if not activation_approved:
        raise ActivationApprovalRequired(
            "Workday Batch 1 activation requires explicit operator approval"
        )

    production_data = load_registry(production)
    proposal_by_name = rows_by_name(load_registry(proposal)["target_companies"])
    merged = deepcopy(production_data)
    merged["target_companies"] = [
        deepcopy(proposal_by_name.get(row["name"], row))
        for row in production_data["target_companies"]
    ]
    _validate_unique_scopes(merged["target_companies"])
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the isolated package without producing activation output",
    )
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("only the offline --check operation is supported")
    print(json.dumps(validate_package(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
