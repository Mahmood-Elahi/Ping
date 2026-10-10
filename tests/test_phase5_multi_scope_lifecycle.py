from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.integrations.supabase_job_store import (
    generate_provider_source_key,
    generate_source_key,
)
from src.scraper.company_careers.freshness import (
    empty_freshness_state,
    load_freshness_config,
    reconcile_lifecycle,
)
from src.scraper.company_careers.models import CompanyTarget
from src.scraper.company_careers.phase5_coverage import load_phase5_candidates
from src.scraper.company_careers.source_cooldown import source_health_key
from src.scraper.company_careers.source_integrity import assess_registry
from src.utils.source_scope import source_scope_key


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "phase5_multi_scope_companies.yaml"
AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
CONFIG = load_freshness_config(str(ROOT / "config" / "freshness.yaml"))


def job(
    job_id: str,
    *,
    company: str = "Canonical Company X",
    scope: str = "default",
    source: str | None = None,
    title: str | None = None,
    location: str = "Calgary",
    equivalence: dict[str, str] | None = None,
) -> dict:
    return {
        "job_id": job_id,
        "title": title or job_id,
        "company": company,
        "location": location,
        "url": f"https://jobs.example.test/{scope}/{job_id}",
        "updated_at": "2026-10-10T00:00:00Z",
        "source": source or f"fixture:{scope}",
        "source_scope": scope,
        "cross_source_equivalence": equivalence,
    }


def reconcile(current, state, scopes, *, when=AS_OF, protected=()):
    return reconcile_lifecycle(
        current,
        state,
        complete_companies=(),
        complete_source_scopes=scopes,
        protected_source_keys=protected,
        as_of=when,
        config=CONFIG,
    )


def test_regression_old_company_wide_authority_would_mark_sibling_job_missing() -> None:
    current = {generate_source_key(job("A1", scope="professional"))}
    prior = [job("A1", scope="professional"), job("B1", scope="early_careers")]
    legacy_missing = {
        generate_source_key(item)
        for item in prior
        if item["company"].casefold() == "canonical company x".casefold()
        and generate_source_key(item) not in current
    }
    assert legacy_missing == {generate_source_key(job("B1", scope="early_careers"))}


def test_default_legacy_scope_keeps_two_authoritative_miss_rule() -> None:
    initial = reconcile(
        [job("LEGACY")], empty_freshness_state(), [("Canonical Company X", "default")]
    )
    first = reconcile([], initial.state, [("Canonical Company X", "default")])
    second = reconcile(
        [], first.state, [("Canonical Company X", "default")], when=AS_OF + timedelta(hours=1)
    )
    key = generate_source_key(job("LEGACY"))
    assert first.state["jobs"][key]["lifecycle_status"] == "possibly_missing"
    assert second.state["jobs"][key]["lifecycle_status"] == "removed"


@pytest.mark.parametrize(
    ("authoritative_scope", "sibling_scope"),
    [("professional", "early_careers"), ("early_careers", "professional")],
)
def test_scope_absence_never_advances_sibling(
    authoritative_scope: str, sibling_scope: str
) -> None:
    seeded = reconcile(
        [job("A", scope=authoritative_scope), job("B", scope=sibling_scope)],
        empty_freshness_state(),
        [
            ("Canonical Company X", authoritative_scope),
            ("Canonical Company X", sibling_scope),
        ],
    )
    result = reconcile(
        [job("A", scope=authoritative_scope)],
        seeded.state,
        [("Canonical Company X", authoritative_scope)],
    )
    sibling = result.state["jobs"][generate_source_key(job("B", scope=sibling_scope))]
    assert sibling["lifecycle_status"] == "active"
    assert sibling["missing_scan_count"] == 0


def test_incomplete_or_cooldown_skipped_scope_has_zero_absence_authority() -> None:
    seeded = reconcile(
        [job("A", scope="professional")],
        empty_freshness_state(),
        [("Canonical Company X", "professional")],
    )
    incomplete = reconcile([], seeded.state, [])
    cooldown_skipped = reconcile([], incomplete.state, [])
    key = generate_source_key(job("A", scope="professional"))
    assert incomplete.state["jobs"][key]["missing_scan_count"] == 0
    assert cooldown_skipped.state["jobs"][key]["missing_scan_count"] == 0


def test_scope_owned_record_closes_and_reopens_independently() -> None:
    seed = reconcile(
        [job("A", scope="professional"), job("B", scope="early_careers")],
        empty_freshness_state(),
        [
            ("Canonical Company X", "professional"),
            ("Canonical Company X", "early_careers"),
        ],
    )
    one = reconcile([], seed.state, [("Canonical Company X", "professional")])
    two = reconcile(
        [], one.state, [("Canonical Company X", "professional")],
        when=AS_OF + timedelta(hours=1),
    )
    reopened = reconcile(
        [job("A", scope="professional")],
        two.state,
        [("Canonical Company X", "professional")],
        when=AS_OF + timedelta(hours=2),
    )
    a_key = generate_source_key(job("A", scope="professional"))
    b_key = generate_source_key(job("B", scope="early_careers"))
    assert two.state["jobs"][a_key]["lifecycle_status"] == "removed"
    assert two.state["jobs"][b_key]["lifecycle_status"] == "active"
    assert reopened.state["jobs"][a_key]["lifecycle_status"] == "active"


def test_exact_shared_job_stays_active_while_any_owner_reports_it() -> None:
    exact = {"kind": "shared_requisition_id", "value": "REQ-EXACT-1"}
    professional = job("P-1", scope="professional", equivalence=exact)
    early = job("E-1", scope="early_careers", equivalence=exact)
    assert generate_source_key(professional) == generate_source_key(early)
    assert generate_provider_source_key(professional) != generate_provider_source_key(early)

    seed = reconcile(
        [professional, early],
        empty_freshness_state(),
        [
            ("Canonical Company X", "professional"),
            ("Canonical Company X", "early_careers"),
        ],
    )
    first = reconcile([], seed.state, [("Canonical Company X", "professional")])
    second = reconcile(
        [], first.state, [("Canonical Company X", "professional")],
        when=AS_OF + timedelta(hours=1),
    )
    shared = second.state["jobs"][generate_source_key(professional)]
    assert shared["lifecycle_status"] == "active"
    owners = shared["scope_ownership"]
    assert owners[source_scope_key("Canonical Company X", "professional")][
        "lifecycle_status"
    ] == "removed"
    assert owners[source_scope_key("Canonical Company X", "early_careers")][
        "lifecycle_status"
    ] == "active"


def test_title_and_location_similarity_never_creates_equivalence() -> None:
    first = job("P", scope="professional", title="Summer Analyst", location="New York")
    second = job("E", scope="early_careers", title="Summer Analyst", location="New York")
    assert generate_source_key(first) != generate_source_key(second)
    first.update(job_id="", url="")
    second.update(job_id="", url="")
    assert generate_source_key(first) != generate_source_key(second)
    with pytest.raises(ValueError, match="cross_source_equivalence kind"):
        generate_source_key(
            job(
                "BAD",
                scope="professional",
                equivalence={"kind": "title_location", "value": "Summer Analyst|New York"},
            )
        )


def test_unknown_overlap_sources_remain_independently_traceable() -> None:
    global_job = job("LS-1", company="Lightspeed Commerce", scope="global")
    france_job = job("LS-1", company="Lightspeed Commerce", scope="france")
    assert generate_source_key(global_job) != generate_source_key(france_job)


def test_config_fingerprint_change_does_not_change_logical_scope() -> None:
    first = CompanyTarget(
        name="Example", platform="greenhouse", source_scope="professional", slug="old"
    )
    second = CompanyTarget(
        name="Example", platform="greenhouse", source_scope="professional", slug="new"
    )
    assert first.source_scope_key == second.source_scope_key
    assert source_health_key(first) != source_health_key(second)


def test_registry_allows_same_company_in_distinct_scopes_and_rejects_duplicate_scope() -> None:
    distinct = assess_registry(
        [
            {"name": "Example", "platform": "greenhouse", "slug": "one", "source_scope": "professional"},
            {"name": "Example", "platform": "greenhouse", "slug": "two", "source_scope": "early_careers"},
        ]
    )
    duplicate = assess_registry(
        [
            {"name": "Example", "platform": "greenhouse", "slug": "one"},
            {"name": "Example", "platform": "greenhouse", "slug": "two"},
        ]
    )
    assert all(item.activation_state.value == "active" for item in distinct)
    assert duplicate[1].activation_state.value == "quarantined"


@pytest.mark.parametrize("scope", ["Early Careers", "france!", "a:b:c", "-"])
def test_malformed_explicit_scope_fails_closed(scope: str) -> None:
    with pytest.raises(ValueError, match="source_scope"):
        CompanyTarget(name="Example", platform="greenhouse", source_scope=scope)


def test_ready_candidate_without_explicit_scope_identity_is_rejected(tmp_path: Path) -> None:
    candidate = tmp_path / "candidates.yaml"
    candidate.write_text(
        "phase5_candidates:\n"
        "  - candidate_id: ambiguous\n"
        "    company: Example\n"
        "    scope: regional\n"
        "    status: READY_FOR_CANARY\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="explicit canonical_company and source_scope"):
        load_phase5_candidates(candidate)


def test_concrete_fixture_covers_required_scope_families() -> None:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    by_company = {
        item["canonical_company"]: item for item in data["multi_scope_companies"]
    }
    assert by_company["BlackRock"]["source_scopes"] == [
        "professional",
        "early_careers",
    ]
    assert by_company["Chevron"]["source_scopes"] == ["professional", "university"]
    assert by_company["Chevron Stations"]["source_scopes"] == ["default"]
    assert by_company["Lightspeed Commerce"]["overlap"] == "unknown"
    assert by_company["Ultium Cells"]["source_scopes"] == [
        "spring_hill",
        "troy",
        "warren",
    ]
    assert by_company["CATL"]["source_scopes"] == ["global", "regional"]
    assert by_company["Siemens Healthineers"]["source_scopes"] == [
        "global",
        "regional",
        "specialized",
    ]


def test_forward_migration_and_bootstrap_schema_have_scope_contract_parity() -> None:
    migration = (
        ROOT / "sql" / "migrations" / "20261010_phase5_multi_scope_lifecycle.sql"
    ).read_text(encoding="utf-8").casefold()
    schema = (ROOT / "sql" / "supabase_schema.sql").read_text(
        encoding="utf-8"
    ).casefold()
    for contract in (
        "create table if not exists public.tracked_job_source_scopes",
        "source_scope text not null default 'default'",
        "source_scope_key text not null",
        "provider_source_key text not null",
        "unique (source_key, source_scope_key)",
        "tracked_job_source_scopes_scope_idx",
        "enable row level security",
        "grant all on table public.tracked_job_source_scopes to service_role",
    ):
        assert contract in migration
        assert contract in schema
    assert "p_source_scope text" in migration
    assert "p_source_scope_key text" in migration
    assert "owner.source_scope_key=v_session.source_scope_key" in migration
    assert "not(owner.provider_source_key=any(v_session.protected_source_keys))" in migration
    assert "from public.tracked_job_source_scopes group by source_key" in migration
    assert "drop function if exists public.begin_student_job_source_sync" in migration
    assert "grant all on table public.tracked_job_source_scopes to anon" not in migration
    assert "grant all on table public.tracked_job_source_scopes to authenticated" not in migration
