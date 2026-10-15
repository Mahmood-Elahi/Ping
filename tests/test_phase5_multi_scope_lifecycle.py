from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from src.integrations.supabase_job_store import (
    generate_provider_source_key,
    generate_source_key,
)
from src.scraper.company_careers.freshness import (
    FreshnessRuntime,
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
PHASE5_MIGRATION = (
    ROOT / "sql" / "migrations" / "20261010_phase5_multi_scope_lifecycle.sql"
)
PGCRYPTO_REPAIR_MIGRATION = (
    ROOT / "sql" / "migrations" / "20261010_phase5_pgcrypto_digest_resolution.sql"
)
SCOPE_LOCAL_FINALIZER_MIGRATION = (
    ROOT / "sql" / "migrations" / "20261010_phase5_scope_local_finalizer.sql"
)
OPTIMIZED_FINALIZER_MIGRATION = (
    ROOT / "sql" / "migrations" / "20261015_optimize_source_sync_finalizer.sql"
)
OPTIMIZED_FINALIZER_VERIFY = (
    ROOT / "sql" / "20261015_source_sync_optimization_verify.sql"
)
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


def derive_affected_lifecycle(staged_keys, owners, current_scope_key):
    """Deterministic model of the scope-local SQL aggregation."""
    affected = set(staged_keys) | {
        owner["source_key"]
        for owner in owners
        if owner["source_scope_key"] == current_scope_key
    }
    result = {}
    for source_key in affected:
        all_owners = [owner for owner in owners if owner["source_key"] == source_key]
        if not all_owners:
            continue
        has_active = any(owner["lifecycle_status"] == "active" for owner in all_owners)
        has_possible = any(
            owner["lifecycle_status"] == "possibly_missing" for owner in all_owners
        )
        result[source_key] = {
            "status": "active" if has_active or has_possible else "closed",
            "lifecycle_status": (
                "active" if has_active else "possibly_missing" if has_possible else "removed"
            ),
            "missing_successful_runs": min(
                owner["missing_successful_runs"] for owner in all_owners
            ),
        }
    return result


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


def test_scope_local_finalizer_model_aggregates_all_owners_for_affected_keys_only() -> None:
    professional = "canonical company x::professional"
    early = "canonical company x::early_careers"
    owners = [
        {
            "source_key": "shared",
            "source_scope_key": professional,
            "lifecycle_status": "removed",
            "missing_successful_runs": 2,
        },
        {
            "source_key": "shared",
            "source_scope_key": early,
            "lifecycle_status": "active",
            "missing_successful_runs": 0,
        },
        {
            "source_key": "absent-from-stage",
            "source_scope_key": professional,
            "lifecycle_status": "possibly_missing",
            "missing_successful_runs": 1,
        },
        {
            "source_key": "unrelated",
            "source_scope_key": early,
            "lifecycle_status": "removed",
            "missing_successful_runs": 2,
        },
        {
            "source_key": "staged-new",
            "source_scope_key": professional,
            "lifecycle_status": "active",
            "missing_successful_runs": 0,
        },
    ]

    derived = derive_affected_lifecycle(
        {"shared", "staged-new"}, owners, professional
    )

    assert set(derived) == {"shared", "absent-from-stage", "staged-new"}
    assert derived["shared"] == {
        "status": "active",
        "lifecycle_status": "active",
        "missing_successful_runs": 0,
    }
    assert derived["absent-from-stage"]["lifecycle_status"] == "possibly_missing"
    assert derived["staged-new"]["lifecycle_status"] == "active"
    assert "unrelated" not in derived


def test_exact_shared_job_emits_one_persistence_row_per_owner_scope() -> None:
    exact = {"kind": "shared_requisition_id", "value": "REQ-EXACT-2"}
    professional = job("P-2", scope="professional", equivalence=exact)
    early = job("E-2", scope="early_careers", equivalence=exact)
    result = reconcile(
        [professional, early],
        empty_freshness_state(),
        [
            ("Canonical Company X", "professional"),
            ("Canonical Company X", "early_careers"),
        ],
    )
    runtime = FreshnessRuntime(
        config=CONFIG,
        previous_state=empty_freshness_state(),
        as_of=AS_OF,
    )
    runtime.result = result

    rows = runtime.lifecycle_jobs([professional, early])

    assert len(rows) == 2
    assert {row["source_scope"] for row in rows} == {"professional", "early_careers"}
    assert len({row["source_key"] for row in rows}) == 1


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
    migration = PHASE5_MIGRATION.read_text(encoding="utf-8").casefold()
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


def test_pgcrypto_repair_keeps_finalizer_signature_and_resolves_digest() -> None:
    repair = PGCRYPTO_REPAIR_MIGRATION.read_text(encoding="utf-8").casefold()
    finalizer = SCOPE_LOCAL_FINALIZER_MIGRATION.read_text(
        encoding="utf-8"
    ).casefold()
    schema = (ROOT / "sql" / "supabase_schema.sql").read_text(
        encoding="utf-8"
    ).casefold()
    expected_alter = (
        "alter function public.finalize_student_job_source_sync(uuid)\n"
        "    set search_path to public, extensions, pg_temp;"
    )

    assert expected_alter in repair
    assert "create extension if not exists pgcrypto with schema extensions;" in schema
    assert "create or replace function public.finalize_student_job_source_sync" in schema
    assert "set search_path = public, extensions, pg_temp" in schema
    assert "set search_path = public, extensions, pg_temp" in finalizer
    assert "create or replace function public.finalize_student_job_source_sync" not in repair
    assert "set search_path to public, pg_temp" not in repair


def test_ownership_key_sql_matches_expected_deterministic_sha256() -> None:
    phase5 = PHASE5_MIGRATION.read_text(encoding="utf-8").casefold()
    source_key = "shared-requisition:req-4242"
    scope_key = "canonical company x::professional"

    ownership_key = hashlib.sha256(
        f"{source_key}\x1f{scope_key}".encode("utf-8")
    ).hexdigest()

    assert ownership_key == (
        "9a95ab3feee9994052bf98844502a699b1bf9ae0afbd66ed8c2b8c32009bfb0b"
    )
    assert "source_key || e'\\x1f' || source_scope_key,'sha256'" in phase5


def test_pgcrypto_repair_preserves_multi_scope_finalizer_and_rpc_access() -> None:
    phase5 = PHASE5_MIGRATION.read_text(encoding="utf-8").casefold()
    repair = PGCRYPTO_REPAIR_MIGRATION.read_text(encoding="utf-8").casefold()

    assert "owner.source_scope_key=v_session.source_scope_key" in phase5
    assert "from public.tracked_job_source_scopes group by source_key" in phase5
    assert "grant execute on function public.finalize_student_job_source_sync(uuid)" in phase5
    assert "create or replace function" not in repair
    assert "revoke " not in repair
    assert "grant " not in repair


def test_latest_finalizer_state_has_no_unresolved_pgcrypto_dependency() -> None:
    definitions = []
    for path in sorted((ROOT / "sql" / "migrations").glob("*.sql")):
        sql = path.read_text(encoding="utf-8").casefold()
        if "create or replace function public.finalize_student_job_source_sync" in sql:
            definitions.append(path.name)

    assert definitions == [
        "20260924_staged_source_sync.sql",
        "20261009_phase3_source_reliability.sql",
        "20261010_phase5_multi_scope_lifecycle.sql",
        "20261010_phase5_scope_local_finalizer.sql",
        "20261015_optimize_source_sync_finalizer.sql",
    ]
    assert PGCRYPTO_REPAIR_MIGRATION.name < definitions[-1]
    assert "set search_path to public, extensions, pg_temp" in (
        PGCRYPTO_REPAIR_MIGRATION.read_text(encoding="utf-8").casefold()
    )
    assert "set search_path = public, extensions, pg_temp" in (
        OPTIMIZED_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()
    )


def test_pgcrypto_repair_is_transactional_and_performs_no_data_work() -> None:
    repair = PGCRYPTO_REPAIR_MIGRATION.read_text(encoding="utf-8").casefold()
    statements = [
        statement.strip()
        for statement in re.sub(r"--[^\n]*", "", repair).split(";")
        if statement.strip()
    ]

    assert statements == [
        "begin",
        (
            "alter function public.finalize_student_job_source_sync(uuid)\n"
            "    set search_path to public, extensions, pg_temp"
        ),
        "commit",
    ]
    for data_operation in ("delete", "insert", "update", "truncate", "select"):
        assert not re.search(rf"\b{data_operation}\b", repair)


def test_phase3_through_phase5_security_definers_have_no_other_pgcrypto_calls() -> None:
    sql = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in sorted((ROOT / "sql" / "migrations").glob("202610*.sql"))
    )
    pgcrypto_calls = re.findall(
        r"(?<![.\w])(digest|gen_random_uuid|crypt|gen_salt|hmac|encrypt|decrypt|pgp_[a-z_]+)\s*\(",
        sql,
    )

    assert pgcrypto_calls == ["digest", "digest", "digest", "digest"]


def test_scope_local_finalizer_sql_is_bounded_and_preserves_rpc_contract() -> None:
    sql = SCOPE_LOCAL_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()

    assert sql.lstrip().startswith("-- phase 5 forward repair")
    assert "\nbegin;" in sql
    assert sql.rstrip().endswith("commit;")
    assert "finalize_student_job_source_sync(p_sync_id uuid)" in sql
    assert "security definer" in sql
    assert "set search_path = public, extensions, pg_temp" in sql
    assert "with affected as (" in sql
    assert "from public.job_sync_stage\n        where sync_id=p_sync_id" in sql
    assert "where source_scope_key=v_session.source_scope_key" in sql
    assert "from affected\n        join public.tracked_job_source_scopes owner using(source_key)" in sql
    assert "group by owner.source_key" in sql
    assert "from public.tracked_job_source_scopes group by source_key" not in sql
    assert "where tracked.source_key=aggregate.source_key;" in sql
    assert "grant execute on function public.finalize_student_job_source_sync(uuid)" in sql
    assert "to service_role" in sql
    assert "from public,anon,authenticated" in sql


def test_scope_local_finalizer_keeps_reconciliation_replay_and_cleanup_contracts() -> None:
    sql = SCOPE_LOCAL_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()

    for contract in (
        "if v_session.status = 'finalized' then",
        "return v_session.result || jsonb_build_object('replayed', true)",
        "owner.source_scope_key=v_session.source_scope_key",
        "where v_session.reconcile_absence",
        "not(owner.provider_source_key=any(v_session.protected_source_keys))",
        "incoming.sync_id=p_sync_id and incoming.source_key=owner.source_key",
        "on conflict(source_key,source_scope_key) do update set",
        "delete from public.job_sync_chunks where sync_id=p_sync_id",
        "delete from public.job_sync_stage where sync_id=p_sync_id",
    ):
        assert contract in sql
    assert "delete from public.tracked_jobs" not in sql
    assert "delete from public.tracked_job_source_scopes" not in sql
    assert "truncate " not in sql
    assert "select public.finalize_student_job_source_sync" not in sql
    assert "perform public.finalize_student_job_source_sync" not in sql


def test_latest_finalizer_bootstrap_definition_matches_forward_migration() -> None:
    migration = OPTIMIZED_FINALIZER_MIGRATION.read_text(encoding="utf-8")
    schema = (ROOT / "sql" / "supabase_schema.sql").read_text(encoding="utf-8")
    pattern = re.compile(
        r"create or replace function public\.finalize_student_job_source_sync"
        r"\(p_sync_id uuid\).*?\n\$\$;",
        re.IGNORECASE | re.DOTALL,
    )

    migration_definition = pattern.search(migration)
    schema_definition = pattern.search(schema)

    assert migration_definition is not None
    assert schema_definition is not None
    def normalized_definition(value: str) -> str:
        without_comments = re.sub(r"--[^\n]*", "", value)
        return re.sub(r"\s+", " ", without_comments).strip()

    assert normalized_definition(migration_definition.group(0)) == normalized_definition(
        schema_definition.group(0)
    )
    assert "20261015_optimize_source_sync_finalizer.sql" in schema


def test_optimized_finalizer_limits_lifecycle_work_and_serializes_scope() -> None:
    sql = OPTIMIZED_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()
    retirement_sql = PHASE5_MIGRATION.read_text(encoding="utf-8").casefold()
    function_match = re.search(
        r"create or replace function public\.finalize_student_job_source_sync"
        r"\(p_sync_id uuid\).*?\n\$\$;",
        sql,
        re.DOTALL,
    )
    assert function_match is not None
    function_sql = function_match.group(0)

    assert (
        "create index concurrently if not exists "
        "tracked_job_source_scopes_scope_source_idx"
    ) in sql
    assert "(source_scope_key, source_key)" in sql
    assert "'retire:' || v_session.source_scope_key" in sql
    assert "'retire:' || p_source_scope_key" in retirement_sql
    assert "v_session.scan_id::text || ':' || v_session.source_scope_key" not in sql
    assert "cannot finalize a retired source scope" in sql
    assert "cannot finalize a source scope after a newer observation" in sql
    assert "update public.tracked_jobs tracked set" in sql
    assert "select 1 from public.tracked_jobs existing" in sql
    assert "update public.tracked_job_source_scopes owner set" in sql
    assert "from public.tracked_job_source_scopes existing" in sql
    assert "text[]" not in function_sql
    for removed_array in (
        "v_material_keys",
        "v_owner_material_keys",
        "v_lifecycle_keys",
        "v_missing_keys",
    ):
        assert removed_array not in sql
    assert "with affected as (" in sql
    assert "left join missing using(ownership_key)" in sql
    assert "select count(*) into v_canonical_updated from canonical_updates" in sql
    assert ") is distinct from row(" in sql
    assert "not generally a hot update" in sql


def test_scope_source_index_check_requires_the_exact_physical_definition() -> None:
    sql = OPTIMIZED_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()

    for contract in (
        "table_namespace.nspname='public'",
        "table_class.relname='tracked_job_source_scopes'",
        "access_method.amname='btree'",
        "not index_state.indisunique",
        "index_state.indnkeyatts=2",
        "index_state.indnatts=2",
        "index_state.indexprs is null",
        "index_state.indpred is null",
        "pg_catalog.pg_get_indexdef(index_state.indexrelid,1,true)='source_scope_key'",
        "pg_catalog.pg_get_indexdef(index_state.indexrelid,2,true)='source_key'",
        "index_state.indisvalid",
        "index_state.indisready",
        "index_state.indislive",
    ):
        assert contract in sql
    assert sql.index("create index concurrently") < sql.index("\nbegin;")
    assert "invalid_or_wrong_definition" in sql


def test_large_inventory_avoids_material_and_canonical_noop_writes() -> None:
    """Model a 100k+ ownership inventory without claiming a live speedup."""
    background = {
        f"background-{index}": ("other-scope", 0, "active", None)
        for index in range(100_001)
    }
    current = {
        f"source-{index}": ("target-scope", 0, "active", None)
        for index in range(5_000)
    }
    owners = {**background, **current}
    staged = {source_key: (0, "active", None) for source_key in current}
    material_payloads = {
        source_key: ("title", "location", "raw")
        for source_key in current
    }
    tracked_payloads = dict(material_payloads)

    material_keys = {
        source_key
        for source_key, desired in material_payloads.items()
        if tracked_payloads.get(source_key) != desired
    }

    assert len(owners) == 105_001
    assert material_keys == set()
    assert len(staged) == 5_000  # required last_seen_at heartbeats; not generally HOT
    canonical_updates = {
        source_key
        for source_key, desired in staged.items()
        if owners[source_key][1:] != desired
    }
    assert canonical_updates == set()

    material_payloads["source-5"] = ("changed title", "location", "raw")
    material_keys = {
        source_key
        for source_key, desired in material_payloads.items()
        if tracked_payloads.get(source_key) != desired
    }
    owners["source-7"] = ("target-scope", 1, "possibly_missing", None)
    canonical_updates = {
        source_key
        for source_key, desired in staged.items()
        if source_key not in owners or owners[source_key][1:] != desired
    }

    assert material_keys == {"source-5"}
    assert canonical_updates == {"source-7"}
    assert len(canonical_updates) < len(staged)


def _derive_canonical(owner_rows: list[dict]) -> tuple[str, str, int]:
    has_active = any(row["lifecycle_status"] == "active" for row in owner_rows)
    has_possible = any(
        row["lifecycle_status"] == "possibly_missing" for row in owner_rows
    )
    return (
        "active" if has_active or has_possible else "closed",
        "active" if has_active else "possibly_missing" if has_possible else "removed",
        min(row["missing_successful_runs"] for row in owner_rows),
    )


def _canonical_write_if_needed(
    canonical: dict,
    owner_rows: list[dict],
) -> bool:
    desired = _derive_canonical(owner_rows)
    current = (
        canonical["status"],
        canonical["lifecycle_status"],
        canonical["missing_successful_runs"],
    )
    if current == desired:
        return False
    canonical["status"], canonical["lifecycle_status"], canonical[
        "missing_successful_runs"
    ] = desired
    return True


def test_touched_canonical_drift_is_repaired_but_consistent_state_is_not_written() -> None:
    owners = [
        {"lifecycle_status": "active", "missing_successful_runs": 0},
    ]
    canonical = {
        "status": "closed",
        "lifecycle_status": "removed",
        "missing_successful_runs": 2,
    }

    assert _canonical_write_if_needed(canonical, owners)
    assert canonical == {
        "status": "active",
        "lifecycle_status": "active",
        "missing_successful_runs": 0,
    }
    assert not _canonical_write_if_needed(canonical, owners)


def test_shared_job_stays_active_when_one_scope_becomes_missing_and_removed() -> None:
    professional = {"lifecycle_status": "active", "missing_successful_runs": 0}
    university = {"lifecycle_status": "active", "missing_successful_runs": 0}
    canonical = {
        "status": "active",
        "lifecycle_status": "active",
        "missing_successful_runs": 0,
    }

    professional.update(lifecycle_status="possibly_missing", missing_successful_runs=1)
    assert not _canonical_write_if_needed(canonical, [professional, university])
    professional.update(lifecycle_status="removed", missing_successful_runs=2)
    assert not _canonical_write_if_needed(canonical, [professional, university])
    assert canonical["status"] == "active"


def test_reappearance_preserves_first_seen_and_advances_last_seen() -> None:
    first_seen = AS_OF - timedelta(days=5)
    canonical = {
        "status": "closed",
        "lifecycle_status": "removed",
        "missing_successful_runs": 2,
        "first_seen_at": first_seen,
        "last_seen_at": AS_OF - timedelta(days=1),
    }
    owner = {"lifecycle_status": "removed", "missing_successful_runs": 2}

    owner.update(lifecycle_status="active", missing_successful_runs=0)
    canonical["last_seen_at"] = AS_OF
    assert _canonical_write_if_needed(canonical, [owner])
    assert canonical["first_seen_at"] == first_seen
    assert canonical["last_seen_at"] == AS_OF
    assert canonical["lifecycle_status"] == "active"


def test_protected_or_non_authoritative_absence_cannot_advance_evidence() -> None:
    owner = {
        "provider_source_key": "unreadable",
        "lifecycle_status": "active",
        "missing_successful_runs": 0,
    }

    for reconcile_absence, protected in ((True, {"unreadable"}), (False, set())):
        before = dict(owner)
        if reconcile_absence and owner["provider_source_key"] not in protected:
            owner["missing_successful_runs"] += 1
        assert owner == before


def test_two_complete_absences_close_and_replay_is_idempotent() -> None:
    owner = {"lifecycle_status": "active", "missing_successful_runs": 0}
    completed_syncs: set[str] = set()

    def finalize(sync_id: str) -> bool:
        if sync_id in completed_syncs:
            return True
        owner["missing_successful_runs"] += 1
        owner["lifecycle_status"] = (
            "removed" if owner["missing_successful_runs"] >= 2 else "possibly_missing"
        )
        completed_syncs.add(sync_id)
        return False

    assert not finalize("scan-1")
    assert owner == {"lifecycle_status": "possibly_missing", "missing_successful_runs": 1}
    assert finalize("scan-1")
    assert owner["missing_successful_runs"] == 1
    assert not finalize("scan-2")
    assert owner == {"lifecycle_status": "removed", "missing_successful_runs": 2}


def test_overlapping_scope_scans_reject_out_of_order_finalization() -> None:
    finalized_at = AS_OF + timedelta(minutes=5)
    older_observation = AS_OF

    with pytest.raises(ValueError, match="newer observation"):
        if finalized_at > older_observation:
            raise ValueError("newer observation already finalized")


def test_optimized_finalizer_verification_is_read_only_and_checks_access() -> None:
    sql = OPTIMIZED_FINALIZER_VERIFY.read_text(encoding="utf-8").casefold()
    executable = re.sub(r"--[^\n]*", "", sql)
    executable = re.sub(r"'(?:''|[^'])*'", "''", executable)

    assert "begin transaction read only;" in sql
    assert "pg_get_functiondef" in sql
    assert "function_definition_md5" in sql
    assert "normalized_function_body_md5" in sql
    assert "owner_name='postgres'" in sql
    assert "target.prosecdef" in sql
    assert "search_path=public, extensions, pg_temp" in sql
    assert "postgres:execute:false" in sql
    assert "service_role:execute:false" in sql
    assert "target.postgres_can_execute" in sql
    assert "target.service_role_can_execute" in sql
    assert "not target.anon_can_execute" in sql
    assert "not target.authenticated_can_execute" in sql
    assert "canonical_lifecycle_inconsistency_count" in sql
    assert "stop_canonical_lifecycle_drift" in sql
    assert "stop_scope_index_wrong_or_invalid" in sql
    assert "pass_baseline_ready" in sql
    assert "pass_optimized" in sql
    assert "checkpoint_trigger_exact_and_enabled" in sql
    assert "retention_index_exact_and_ready" in sql
    assert "index_state.indexprs is null" in sql
    assert "index_state.indpred is null" in sql
    assert "access_method.amname='btree'" in sql
    assert "pg_get_indexdef(index_state.indexrelid,1,true)='source_scope_key'" in sql
    assert "pg_get_indexdef(index_state.indexrelid,2,true)='source_key'" in sql
    assert "index_state.indisvalid" in sql
    assert "index_state.indisready" in sql
    assert sql.rstrip().endswith("rollback;")
    assert not re.search(
        r"\b(insert|update|delete|truncate|alter|drop|create)\b", executable
    )


def test_verifier_hashes_match_actual_baseline_and_optimized_function_bodies() -> None:
    def normalized_body_md5(path: Path) -> str:
        sql = path.read_text(encoding="utf-8")
        match = re.search(
            r"create or replace function public\.finalize_student_job_source_sync"
            r"\(p_sync_id uuid\).*?as \$\$(.*?)\n\$\$;",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        assert match is not None
        normalized = re.sub(r"\s+", " ", match.group(1)).strip()
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    verifier = OPTIMIZED_FINALIZER_VERIFY.read_text(encoding="utf-8")
    assert normalized_body_md5(SCOPE_LOCAL_FINALIZER_MIGRATION) in verifier
    assert normalized_body_md5(OPTIMIZED_FINALIZER_MIGRATION) in verifier


def _deployment_verification_result(**overrides: object) -> str:
    state: dict[str, object] = {
        "function_exists": True,
        "exact_overload_inventory": True,
        "exact_function_contract": True,
        "retention_index_exact_and_ready": True,
        "checkpoint_trigger_exact_and_enabled": True,
        "canonical_lifecycle_inconsistency_count": 0,
        "scope_index_name_present": False,
        "scope_index_exact_and_ready": False,
        "normalized_body": "baseline",
    }
    state.update(overrides)
    if not state["function_exists"]:
        return "STOP_FUNCTION_MISSING"
    if not state["exact_overload_inventory"]:
        return "STOP_FUNCTION_OVERLOAD_MISMATCH"
    if not state["exact_function_contract"]:
        return "STOP_FUNCTION_CONTRACT_MISMATCH"
    if not state["retention_index_exact_and_ready"]:
        return "STOP_RETENTION_INDEX_MISMATCH"
    if not state["checkpoint_trigger_exact_and_enabled"]:
        return "STOP_CHECKPOINT_TRIGGER_MISMATCH"
    if state["canonical_lifecycle_inconsistency_count"] != 0:
        return "STOP_CANONICAL_LIFECYCLE_DRIFT"
    if state["scope_index_name_present"] and not state["scope_index_exact_and_ready"]:
        return "STOP_SCOPE_INDEX_WRONG_OR_INVALID"
    if state["normalized_body"] == "baseline":
        return "PASS_BASELINE_READY"
    if state["normalized_body"] == "optimized" and state["scope_index_exact_and_ready"]:
        return "PASS_OPTIMIZED"
    return "STOP_FUNCTION_DEFINITION_MISMATCH"


def test_exact_expected_overload_inventory_passes_and_additional_overload_stops() -> None:
    sql = OPTIMIZED_FINALIZER_VERIFY.read_text(encoding="utf-8").casefold()

    for contract in (
        "count(*)=1 and bool_and(",
        "procedure_state.pronargs=1",
        "procedure_state.proargtypes[0]='uuid'::regtype::oid",
        "procedure_state.proallargtypes is null",
        "procedure_state.proargmodes is null",
        "procedure_state.provariadic=0",
        "procedure_state.pronargdefaults=0",
        "not procedure_state.proretset",
        "procedure_state.prorettype='jsonb'::regtype",
        "stop_function_overload_mismatch",
    ):
        assert contract in sql

    assert _deployment_verification_result() == "PASS_BASELINE_READY"
    assert (
        _deployment_verification_result(exact_overload_inventory=False)
        == "STOP_FUNCTION_OVERLOAD_MISMATCH"
    )


_EXPECTED_CHECKPOINT_WHEN = (
    "((new.status = 'finalized'::text) and "
    "(old.status is distinct from new.status))"
)


def _checkpoint_trigger_contract(**overrides: object) -> bool:
    state: dict[str, object] = {
        "inventory_count": 1,
        "table": ("public", "job_sync_sessions"),
        "function": "public.commit_student_job_incremental_checkpoint()",
        "tgtype": 17,
        "updated_columns": ("status",),
        "when": _EXPECTED_CHECKPOINT_WHEN,
        "enabled": "O",
        "arguments": (),
        "transition_tables": (),
    }
    state.update(overrides)
    return state == {
        "inventory_count": 1,
        "table": ("public", "job_sync_sessions"),
        "function": "public.commit_student_job_incremental_checkpoint()",
        "tgtype": 17,
        "updated_columns": ("status",),
        "when": _EXPECTED_CHECKPOINT_WHEN,
        "enabled": "O",
        "arguments": (),
        "transition_tables": (),
    }


def test_checkpoint_trigger_exact_contract_is_catalog_backed() -> None:
    sql = OPTIMIZED_FINALIZER_VERIFY.read_text(encoding="utf-8").casefold()

    for contract in (
        "trigger_namespace.nspname='public'",
        "trigger_table.relname='job_sync_sessions'",
        "trigger_state.tgtype=17",
        "trigger_state.tgattr::text=status_attribute.attnum::text",
        "trigger_state.tgenabled='o'",
        "trigger_state.tgnargs=0",
        "trigger_state.tgoldtable is null",
        "trigger_state.tgnewtable is null",
        "pg_get_triggerdef(trigger_state.oid,false)",
        "checkpoint_trigger_inventory_count",
    ):
        assert contract in sql
    assert _checkpoint_trigger_contract()


@pytest.mark.parametrize(
    "mutation",
    [
        {"tgtype": 19},  # BEFORE UPDATE at row level
        {"when": "new.status = 'finalized'::text"},
        {"enabled": "D"},
        {"inventory_count": 2},
    ],
    ids=["wrong-timing", "wrong-when", "disabled", "conflicting-duplicate"],
)
def test_checkpoint_trigger_mismatches_fail_closed(mutation: dict[str, object]) -> None:
    assert not _checkpoint_trigger_contract(**mutation)
    assert (
        _deployment_verification_result(checkpoint_trigger_exact_and_enabled=False)
        == "STOP_CHECKPOINT_TRIGGER_MISMATCH"
    )


_EXPECTED_RETENTION_PREDICATE = (
    "((lifecycle_status = 'removed'::text) and (status = 'closed'::text))"
)


def _retention_index_contract(**overrides: object) -> bool:
    state: dict[str, object] = {
        "identity": ("public", "tracked_jobs", "tracked_jobs_retention_removed_idx"),
        "access_method": "btree",
        "unique": False,
        "key_count": 2,
        "attribute_count": 2,
        "keys": (
            "coalesce(removed_at, last_seen_at, first_seen_at)",
            "source_key",
        ),
        "ordering_options": (0, 0),
        "predicate": _EXPECTED_RETENTION_PREDICATE,
        "valid": True,
        "ready": True,
        "live": True,
    }
    expected = dict(state)
    state.update(overrides)
    return state == expected


def test_retention_index_exact_contract_is_catalog_backed() -> None:
    sql = OPTIMIZED_FINALIZER_VERIFY.read_text(encoding="utf-8").casefold()

    for contract in (
        "index_class.relname='tracked_jobs_retention_removed_idx'",
        "table_namespace.nspname='public'",
        "table_class.relname='tracked_jobs'",
        "access_method.amname='btree'",
        "not index_state.indisunique",
        "index_state.indnkeyatts=2",
        "index_state.indnatts=2",
        "index_state.indkey[0]=0",
        "index_state.indkey[1]=source_key_attribute.attnum",
        "index_state.indoption[0]=0",
        "index_state.indoption[1]=0",
        "pg_get_expr(index_state.indexprs,index_state.indrelid,false)",
        "pg_get_expr(index_state.indpred,index_state.indrelid,false)",
        "index_state.indisvalid",
        "index_state.indisready",
        "index_state.indislive",
    ):
        assert contract in sql
    assert "like '%lifecycle_status%removed%status%closed%'" not in sql
    assert _retention_index_contract()


@pytest.mark.parametrize(
    "mutation",
    [
        {"predicate": "(status = 'closed'::text)"},
        {"keys": ("coalesce(last_seen_at, removed_at, first_seen_at)", "source_key")},
        {"keys": ("source_key", "coalesce(removed_at, last_seen_at, first_seen_at)")},
        {"attribute_count": 3},
        {"ordering_options": (1, 0)},
        {"valid": False},
        {"ready": False},
        {"live": False},
    ],
    ids=[
        "wrong-predicate",
        "wrong-expression",
        "wrong-key-order",
        "included-column",
        "descending-key",
        "invalid",
        "unfinished",
        "not-live",
    ],
)
def test_retention_index_mismatches_fail_closed(mutation: dict[str, object]) -> None:
    assert not _retention_index_contract(**mutation)
    assert (
        _deployment_verification_result(retention_index_exact_and_ready=False)
        == "STOP_RETENTION_INDEX_MISMATCH"
    )


def _normalized_finalizer_body_md5(path: Path) -> str:
    sql = path.read_text(encoding="utf-8")
    match = re.search(
        r"create or replace function public\.finalize_student_job_source_sync"
        r"\(p_sync_id uuid\).*?as \$\$(.*?)\n\$\$;",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    normalized = re.sub(r"\s+", " ", match.group(1)).strip()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def test_migration_checks_compatible_baseline_before_concurrent_index_creation() -> None:
    sql = OPTIMIZED_FINALIZER_MIGRATION.read_text(encoding="utf-8").casefold()
    baseline_hash = _normalized_finalizer_body_md5(SCOPE_LOCAL_FINALIZER_MIGRATION)
    optimized_hash = _normalized_finalizer_body_md5(OPTIMIZED_FINALIZER_MIGRATION)

    assert sql.index("do $preflight$") < sql.index("create index concurrently")
    assert baseline_hash in sql
    assert optimized_hash in sql
    assert "baseline_overload_mismatch" in sql
    assert "baseline_contract_mismatch" in sql
    assert "baseline_body_mismatch" in sql
    assert "owner_name is distinct from 'postgres'" in sql
    assert "security_definer is distinct from true" in sql
    assert "postgres:execute:false" in sql
    assert "service_role:execute:false" in sql
    assert "v_postgres_can_execute is distinct from true" in sql
    assert "v_service_role_can_execute is distinct from true" in sql
    assert "v_anon_can_execute is distinct from false" in sql
    assert "v_authenticated_can_execute is distinct from false" in sql


def test_baseline_to_optimized_and_safe_optimized_rerun_are_the_only_body_paths() -> None:
    assert _deployment_verification_result(normalized_body="baseline") == (
        "PASS_BASELINE_READY"
    )
    assert _deployment_verification_result(
        normalized_body="optimized",
        scope_index_name_present=True,
        scope_index_exact_and_ready=True,
    ) == "PASS_OPTIMIZED"
    assert _deployment_verification_result(normalized_body="unknown") == (
        "STOP_FUNCTION_DEFINITION_MISMATCH"
    )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({"normalized_body": "unknown"}, "STOP_FUNCTION_DEFINITION_MISMATCH"),
        ({"exact_function_contract": False}, "STOP_FUNCTION_CONTRACT_MISMATCH"),
        ({"exact_overload_inventory": False}, "STOP_FUNCTION_OVERLOAD_MISMATCH"),
        (
            {"scope_index_name_present": True, "scope_index_exact_and_ready": False},
            "STOP_SCOPE_INDEX_WRONG_OR_INVALID",
        ),
    ],
    ids=["wrong-body", "wrong-owner-or-acl", "wrong-overload", "wrong-index"],
)
def test_incompatible_preexisting_state_fails_closed(
    mutation: dict[str, object], expected: str
) -> None:
    assert _deployment_verification_result(**mutation) == expected
