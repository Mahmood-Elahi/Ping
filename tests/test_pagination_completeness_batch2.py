from __future__ import annotations

import json
from math import ceil
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BATCH1 = ROOT / "config" / "pagination_completeness_recovery_batch1.yaml"
BATCH2 = ROOT / "config" / "pagination_completeness_recovery_batch2.yaml"
REGISTRY = ROOT / "config" / "target_companies.yaml"
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"
CANDIDATES = ROOT / "config" / "target_companies.phase3_smartrecruiters_canaries.yaml"
SNAPSHOT = ROOT / "jobs" / "company_careers_coverage_20261001_142550.json"

EXPECTED = [
    "Meta",
    "JPMorgan Chase",
    "Emerson",
    "Honeywell",
    "Microsoft",
    "Qualcomm",
    "Micron",
    "Eaton",
    "Rivian",
    "Goldman Sachs",
    "GlobalFoundries",
    "Oracle",
    "Stantec",
    "Espressif Systems",
    "Bosch",
    "CPKC",
    "Scotiabank",
    "ecobee",
    "Bombardier",
    "Fortinet",
]


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rows(path: Path, key: str) -> dict[str, dict]:
    return {row["name"]: row for row in _yaml(path)[key]}


def test_batch2_has_exact_requested_order_scope_and_status() -> None:
    review = _yaml(BATCH2)
    rows = review["companies"]

    assert [row["company"] for row in rows] == EXPECTED
    assert len({row["company"] for row in rows}) == 20
    assert all(row["source_scope"] == "default" for row in rows)
    assert {row["status"] for row in rows} == {"NEEDS_SOURCE_SPECIFIC_REPAIR"}
    assert review["status_counts"] == {
        "RECOVERED_BY_SHARED_FIX": 0,
        "READY_FOR_CANARY": 0,
        "NEEDS_SOURCE_SPECIFIC_REPAIR": 20,
        "FAIL_IDENTITY": 0,
        "FAIL_CONTRACT": 0,
        "FAIL_TRANSIENT": 0,
        "UNSUPPORTED_PROVIDER": 0,
        "NO_SAFE_REPAIR": 0,
        "INSUFFICIENT_EVIDENCE": 0,
    }


def test_adapter_groups_are_disjoint_complete_and_match_production() -> None:
    review = _yaml(BATCH2)
    grouped = [name for names in review["adapter_groups"].values() for name in names]
    production = _rows(REGISTRY, "target_companies")
    rows = {row["company"]: row for row in review["companies"]}

    assert len(grouped) == len(set(grouped)) == 20
    assert set(grouped) == set(EXPECTED)
    assert review["adapter_groups"]["workday"] == []
    assert all(rows[name]["platform"] == production[name]["platform"] for name in EXPECTED)


def test_latest_deterministic_failures_match_checked_in_snapshot() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    historic = {row["company"]: row for row in snapshot["coverage"]}
    reviewed = {row["company"]: row for row in _yaml(BATCH2)["companies"]}

    for name in EXPECTED:
        evidence = reviewed[name]["deterministic_failure"]
        row = historic[name]
        if "jobs_collected" in evidence:
            assert evidence["jobs_collected"] == row["jobs_collected"]
        if "jobs_collected_before_failure" in evidence:
            assert evidence["jobs_collected_before_failure"] == row["jobs_collected"]
        if "total" in evidence:
            assert evidence["total"] == row["total_available"]
        if "collection_changed" in evidence:
            assert evidence["collection_changed"] == row["collection_changed_during_scan"]
        if "rejected_records" in evidence:
            assert evidence["rejected_records"] == row["rejected_record_count"]


def test_oracle_and_eightfold_budgets_are_total_derived_not_promotions() -> None:
    rows = {row["company"]: row for row in _yaml(BATCH2)["companies"]}
    oracle_totals = {
        "JPMorgan Chase": 7393,
        "Emerson": 1058,
        "Honeywell": 1265,
        "Oracle": 2228,
        "Fortinet": 968,
    }
    eightfold_totals = {
        "Microsoft": 2369,
        "Qualcomm": 2029,
        "Micron": 2975,
        "Eaton": 2365,
        "GlobalFoundries": 558,
    }

    for name, total in oracle_totals.items():
        budget = rows[name]["request_budget"]
        assert budget["required_stable"] == ceil(total / 24)
        assert budget["restart_worst_case"] == 2 * ceil(total / 24)
        assert rows[name]["status"] == "NEEDS_SOURCE_SPECIFIC_REPAIR"
    for name, total in eightfold_totals.items():
        budget = rows[name]["request_budget"]
        assert budget["required_stable"] == ceil(total / 10)
        assert budget["restart_worst_case"] == 2 * ceil(total / 10)
        assert rows[name]["status"] == "NEEDS_SOURCE_SPECIFIC_REPAIR"


def test_no_shared_fix_or_canary_is_claimed_without_terminal_contract() -> None:
    review = _yaml(BATCH2)

    assert review["shared_repairs"]["implemented"] == []
    assert review["manual_canaries"] == []
    assert review["ready_for_canary"] == []
    assert review["recovered_by_shared_fix"] == []
    assert review["needs_source_specific_repair"] == EXPECTED
    assert {row["family"] for row in review["shared_repairs"]["rejected"]} == {
        "oracle_recruiting",
        "eightfold_pcsx",
        "sap_successfactors",
        "static_or_browser",
    }


def test_alternate_inventory_leads_remain_nonproduction() -> None:
    review = _yaml(BATCH2)
    tracks = review["provider_tracks"]

    assert tracks["meta_sitemap"]["result"] == "insufficient_deterministic_evidence_for_candidate"
    assert tracks["rivian_sitemap"]["result"] == "insufficient_deterministic_evidence_for_candidate"
    assert tracks["goldman_graphql"]["result"] == "insufficient_deterministic_evidence_for_candidate"
    assert tracks["goldman_graphql"]["operation"] == "GetRoles"
    assert tracks["goldman_graphql"]["stable_id"] == "roleId"
    assert review["alternate_inventories"]["Stantec"]["verdict"] == "REJECT_FOR_CURRENT_SCOPE"


def test_sibling_mechanics_and_rejections_are_explicit() -> None:
    findings = _yaml(BATCH2)["sibling_findings"]

    assert {row["classification"] for row in findings["jobseek"]} >= {
        "REUSE_CONTRACT",
        "ADAPT",
        "CONCEPT_ONLY",
        "REJECT",
    }
    assert {row["classification"] for row in findings["freehire"]} >= {
        "REUSE_CONTRACT",
        "ADAPT",
        "CONCEPT_ONLY",
        "REJECT",
    }
    assert findings["jobspy"] == [
        {
            "classification": "REJECT",
            "file": "jobspy/__init__.py",
            "function": "scrape_jobs",
            "mechanic": "third-party search pagination and aggregation",
            "safeguard": "no exact first-party identity or completeness authority for any Batch 2 target",
        }
    ]


def test_intuitive_exact_live_candidate_is_effective_in_both_production_layers() -> None:
    base = _rows(REGISTRY, "target_companies")["Intuitive Surgical"]
    overlay = _rows(REPAIRS, "company_careers_reliability_repairs")["Intuitive Surgical"]
    candidate = _rows(CANDIDATES, "target_companies")["Intuitive Surgical"]
    exact_fields = {
        "name",
        "platform",
        "slug",
        "endpoint_variant",
        "careers_url",
        "endpoint_url",
        "identity_regex",
        "allowed_apply_hosts",
        "page_size",
        "max_pages",
        "max_jobs",
        "max_response_bytes",
        "selective_detail_enrichment",
    }

    assert {key: base[key] for key in exact_fields} == {
        key: candidate[key] for key in exact_fields
    }
    assert {key: overlay[key] for key in exact_fields} == {
        key: candidate[key] for key in exact_fields
    }
    assert sum(row["name"] == "Intuitive Surgical" for row in _yaml(REGISTRY)["target_companies"]) == 1


def test_shopify_unchanged_and_batch1_live_closeout_is_durable() -> None:
    production = _rows(REGISTRY, "target_companies")
    batch1 = {row["company"]: row for row in _yaml(BATCH1)["companies"]}

    assert production["Shopify"] == {"name": "Shopify", "platform": "shopify"}
    assert batch1["Shopify"]["status"] == "RECOVERED"
    assert batch1["Shopify"]["live_validation"]["validation_status"] == "PASS_COMPLETE"
    assert batch1["Intuitive Surgical"]["status"] == "RECOVERED"
    assert batch1["Atlassian"]["status"] == "NEEDS_SOURCE_SPECIFIC_REPAIR"
    assert batch1["Atlassian"]["recovery_state"] == "NOT_RECOVERED"


def test_mutation_accounting_and_pass_gates_remain_fail_closed() -> None:
    review = _yaml(BATCH2)
    mutation = review["mutation_contract"]
    gates = set(review["pass_complete_gates"])

    assert mutation["outcomes"] == {
        "positive_discoveries_retained": True,
        "scan_complete": False,
        "absence_authorized": False,
    }
    assert review["inventory_accounting_contract"]["silent_loss_allowed"] is False
    assert {
        "canonical_employer_identity_validated",
        "provider_source_identity_validated",
        "terminal_evidence_valid",
        "collection_mutation_absent",
        "inventory_fully_accounted",
        "unreadable_stable_ids_protected",
        "central_finalizer_authoritative",
        "contradictory_diagnostics_absent",
    }.issubset(gates)
