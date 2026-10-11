"""Deterministic Phase 6 capability triage for career-site adapters.

This module is policy metadata, not an activation mechanism.  Every entry
returns an ``IncrementalCapability.NONE`` policy until an adapter-specific
contract review proves exact equivalence and material production savings.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from statistics import median
from typing import Iterable, Mapping

from src.scraper.company_careers.incremental import (
    IncrementalCapability,
    IncrementalPolicy,
)
from src.scraper.company_careers.models import CompanyTarget


class Phase6Classification(str, Enum):
    HIGH_VALUE_PHASE6_CANDIDATE = "HIGH_VALUE_PHASE6_CANDIDATE"
    POSSIBLE_BUT_LOW_SAVINGS = "POSSIBLE_BUT_LOW_SAVINGS"
    FULL_CRAWL_ONLY = "FULL_CRAWL_ONLY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class OptimizationPattern(str, Enum):
    COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION = (
        "COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION"
    )
    TIMESTAMP_POSITIVE_ONLY = "TIMESTAMP_POSITIVE_ONLY"
    CURSOR_POSITIVE_ONLY = "CURSOR_POSITIVE_ONLY"
    EXPLICIT_CHANGE_FEED = "EXPLICIT_CHANGE_FEED"
    VALIDATED_CONDITIONAL_SNAPSHOT = "VALIDATED_CONDITIONAL_SNAPSHOT"
    CONTENT_HASH_DOWNSTREAM_SKIP = "CONTENT_HASH_DOWNSTREAM_SKIP"
    PERIODIC_REHYDRATION = "PERIODIC_REHYDRATION"
    PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY = (
        "PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY"
    )
    BROWSER_RENDER_RESULT_REUSE = "BROWSER_RENDER_RESULT_REUSE"
    NONE = "NONE"


@dataclass(frozen=True)
class PriorityScore:
    """Repository-evidence scores, each bounded to the inclusive range 0..5."""

    production_volume: int
    request_cost: int
    theoretical_savings: int
    contract_strength: int
    implementation_complexity: int
    lifecycle_risk: int

    def __post_init__(self) -> None:
        values = (
            self.production_volume,
            self.request_cost,
            self.theoretical_savings,
            self.contract_strength,
            self.implementation_complexity,
            self.lifecycle_risk,
        )
        if any(value < 0 or value > 5 for value in values):
            raise ValueError("Phase 6 priority score dimensions must be in 0..5")

    @property
    def priority(self) -> int:
        """Higher is more worth reviewing; risk and complexity are penalties."""

        return max(
            0,
            self.production_volume
            + self.request_cost
            + self.theoretical_savings
            + self.contract_strength
            - self.implementation_complexity
            - self.lifecycle_risk,
        )


@dataclass(frozen=True)
class AdapterCapability:
    family: str
    classification: Phase6Classification
    patterns: tuple[OptimizationPattern, ...]
    complete_inventory: str
    detail_hydration: str
    browser_rendering: bool
    stable_change_evidence: str
    request_model: str
    expected_savings: str
    evidence: str
    score: PriorityScore

    def __post_init__(self) -> None:
        if not self.patterns:
            raise ValueError(f"{self.family} must have at least one pattern")
        if self.classification is Phase6Classification.FULL_CRAWL_ONLY and (
            self.patterns != (OptimizationPattern.NONE,)
        ):
            raise ValueError(f"{self.family} full-crawl-only policy must use NONE")
        if self.classification is Phase6Classification.HIGH_VALUE_PHASE6_CANDIDATE and (
            self.patterns == (OptimizationPattern.NONE,)
        ):
            raise ValueError(f"{self.family} high-value policy needs a safe pattern")


def _score(volume: int, cost: int, savings: int, contract: int, complexity: int, risk: int) -> PriorityScore:
    return PriorityScore(volume, cost, savings, contract, complexity, risk)


def _cap(
    family: str,
    classification: Phase6Classification,
    patterns: tuple[OptimizationPattern, ...],
    inventory: str,
    detail: str,
    browser: bool,
    version: str,
    requests: str,
    savings: str,
    evidence: str,
    score: PriorityScore,
) -> AdapterCapability:
    return AdapterCapability(
        family,
        classification,
        patterns,
        inventory,
        detail,
        browser,
        version,
        requests,
        savings,
        evidence,
        score,
    )


F = Phase6Classification
P = OptimizationPattern


# The keys are the canonical scanner dispatch names after alias reconciliation.
# ``source_xml`` is dispatchable but has no production source.  ``workable.py``
# is a dormant module: it is not wired into scanner.ADAPTERS and is therefore
# not represented as a supported production family.
CAPABILITIES: Mapping[str, AdapterCapability] = {
    "adp": _cap("adp", F.INSUFFICIENT_EVIDENCE, (P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION,), "yes; total/offset inventory", "7 Workforce Now sources hydrate every item; MyJobs is listing-only", False, "stable IDs and posting dates, but no material version", "bootstrap + ceil(total/page size) listings + N Workforce Now details; shared bounded budget", "potentially N detail calls, not safely selectable today", "Inventory is independent of detail, but no listing version proves an unchanged detail.", _score(2, 4, 4, 3, 3, 4)),
    "amazon": _cap("amazon", F.FULL_CRAWL_ONLY, (P.NONE,), "yes only after every configured search-term total reconciles", "none", False, "posting/update display dates only", "sum of term-specific 100-row offset pages, bounded by max_pages/max_jobs", "0 safe requests", "Search-term union and overlap accounting require the complete traversal.", _score(1, 2, 1, 2, 3, 4)),
    "apple": _cap("apple", F.FULL_CRAWL_ONLY, (P.NONE,), "yes; authoritative totalRecords", "none", False, "postDateInGMT/postingDate are not material versions", "ceil(totalRecords / provider page size), bounded by configured pages/jobs", "0 safe requests", "No reviewed update filter, cursor, change feed, or representation validator.", _score(1, 2, 1, 3, 3, 3)),
    "ashby": _cap("ashby", F.INSUFFICIENT_EVIDENCE, (P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION,), "yes; one compact board collection", "up to 20 optional detail documents", False, "publishedDate/publishedAt are posting times, not proven versions", "1 compact inventory + D details, 0 <= D <= 20", "theoretical D-K details; K cannot be chosen safely", "Large source count and separated hydration justify watching, but exact detail-version evidence is absent.", _score(5, 4, 5, 3, 3, 4)),
    "atlassian": _cap("atlassian", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes; one JSON collection", "none", False, "updatedDate exists without a reviewed update query", "1 collection request", "0 requests; downstream processing only", "The baseline is already one request, so a hash skip cannot materially reduce crawl cost.", _score(1, 1, 1, 3, 1, 1)),
    "avature": _cap("avature", F.FULL_CRAWL_ONLY, (P.NONE,), "variant-dependent HTML terminal evidence", "none", False, "no trustworthy material version", "up to configured HTML pages (2 for every production source)", "0 safe requests", "Three HTML variants expose no reviewed incremental boundary.", _score(2, 2, 1, 2, 3, 4)),
    "bamboohr": _cap("bamboohr", F.INSUFFICIENT_EVIDENCE, (P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION,), "yes; one total-checked collection", "up to 20 details", False, "datePosted is not a proven material version", "1 inventory + D details, 0 <= D <= 20", "theoretical D-K details; K is unproven", "The listing protects identities independently, but it cannot prove an unchanged description.", _score(2, 4, 4, 3, 2, 4)),
    "beesite": _cap("beesite", F.FULL_CRAWL_ONLY, (P.NONE,), "yes only through complete offset pagination", "none", False, "no material version", "ceil(total / 20), capped at 2 production pages", "0 safe requests", "No update ordering/filter or conditional snapshot contract exists.", _score(1, 2, 1, 2, 2, 3)),
    "browser_rendered_html": _cap("browser_rendered_html", F.INSUFFICIENT_EVIDENCE, (P.BROWSER_RENDER_RESULT_REUSE, P.VALIDATED_CONDITIONAL_SNAPSHOT), "only MediaTek has a reviewed single-page terminal contract", "none beyond rendered listing extraction", True, "no source-bound validator or reusable snapshot token", "1 isolated browser context/render per source plus delegated static parsing", "a render per unchanged source in theory; no safe cache proof", "Caching needs source/config binding, integrity, expiry, and fail-closed validation; infrastructure belongs to Phase 7.", _score(2, 5, 4, 1, 5, 5)),
    "clearcompany": _cap("clearcompany", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes; one collection", "none", False, "no material version", "1 request", "0 requests; downstream processing only", "The full snapshot is already the minimum safe network operation.", _score(1, 1, 1, 3, 1, 1)),
    "cornerstone_csod": _cap("cornerstone_csod", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "yes only after provider total/terminal reconciliation", "none", False, "stable IDs but no trustworthy update version/order", "offset pages at configured size 25; production cap is 1", "at most low single-source page savings, presently zero at the cap", "The production board is already one page and ordering semantics are unproven.", _score(1, 1, 1, 2, 3, 4)),
    "dayforce": _cap("dayforce", F.INSUFFICIENT_EVIDENCE, (P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION,), "yes only with identity, bootstrap, token, maxCount, and offset reconciliation", "detail only for listings missing structured location", False, "posting timestamp is not a material version", "shell + CSRF + 2-4 listing pages + conditional location details", "conditional detail calls only; no safe selector today", "All five sources also lack reviewed employer identity, so optimization cannot outrun Phase 4 authority proof.", _score(2, 4, 3, 2, 4, 5)),
    "deel": _cap("deel", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes; one structured ItemList", "none", False, "no material version", "1 request", "0 requests; downstream processing only", "A single complete response is already cheap.", _score(1, 1, 1, 3, 1, 1)),
    "eightfold": _cap("eightfold", F.INSUFFICIENT_EVIDENCE, (P.TIMESTAMP_POSITIVE_ONLY, P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION), "yes only through the full PCSX count/offset traversal", "none; PCSX rows already carry Job Radar material fields", False, "postedTs is a posting timestamp, not a proven edit version", "ceil(count / 10), currently capped at 1-2 pages per production source", "at most 1 listing request/source under present caps; adding a sitemap may cost more", "Jobseek has authoritative sitemap + PCSX enrichment, but Job Radar has neither the sitemap authority nor a separate expensive enrichment path.", _score(3, 2, 2, 2, 4, 4)),
    "exalead": _cap("exalead", F.FULL_CRAWL_ONLY, (P.NONE,), "yes only after HTML total/terminal evidence", "none", False, "no material version", "up to 2 configured pages", "0 safe requests", "No update filter/order/cursor or validated snapshot exists.", _score(1, 2, 1, 2, 3, 3)),
    "first_party_json": _cap("first_party_json", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "yes only after all supplier pages and totals reconcile", "none", False, "provider dates exist without proven edit semantics", "Bytedance supplier API: up to 20 pages / 2,000 rows", "potential earlier positive discovery only; absence still needs full", "One endpoint may be expensive, but deterministic newest-first and change coverage are not proven.", _score(1, 4, 3, 2, 3, 4)),
    "google": _cap("google", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes only after bespoke collection terminal evidence", "none", False, "display timestamps only", "up to 3 configured requests", "0 network requests safely", "No generic Google change contract is represented.", _score(1, 2, 1, 2, 2, 3)),
    "greenhouse": _cap("greenhouse", F.FULL_CRAWL_ONLY, (P.NONE,), "yes; one atomic active-board collection", "up to 20 details (plus one CoreWeave department index)", False, "updated_at is explicitly untrusted as a material version", "1 listing + D details (CoreWeave: 2 + D), 0 <= D <= 20", "0 safe requests", "Dedicated Stage B review rejected every safe incremental/selective pattern.", _score(5, 4, 4, 4, 3, 5)),
    "hawksearch_v2": _cap("hawksearch_v2", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "yes only after total/offset reconciliation", "none", False, "no trustworthy material version/order contract", "no production source; 100-row bounded offset pages when configured", "not estimable without a production source", "The adapter is dispatchable but currently has no production contract to score for materiality.", _score(0, 0, 0, 1, 3, 3)),
    "hibob": _cap("hibob", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes; one collection", "none", False, "no material version", "1 request", "0 requests; downstream processing only", "The baseline is one bounded response.", _score(1, 1, 1, 3, 1, 1)),
    "ibm_search": _cap("ibm_search", F.FULL_CRAWL_ONLY, (P.NONE,), "bespoke total/terminal traversal", "none", False, "no reviewed material version", "1-2 configured listing requests", "0 safe requests", "No incremental query or validator is represented.", _score(1, 2, 1, 2, 3, 4)),
    "icims": _cap("icims", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "variant-dependent Jibe/legacy/Radancy inventory", "none", False, "stable IDs, but no cross-variant update/order contract", "1-2 configured pages across three endpoint variants", "at most one page/source under present caps", "Architecture suggests a positive slice, but no deterministic ordering/change contract spans the variants.", _score(3, 2, 2, 2, 4, 4)),
    "javascript_embedded_data": _cap("javascript_embedded_data", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "configuration-bound embedded/JSON collection", "none", False, "variant-specific dates, no trusted version", "9 sources use 1 request; 1 source permits 2", "0 network requests safely", "The generic family is already mostly a single response.", _score(2, 1, 1, 2, 2, 3)),
    "jobvite": _cap("jobvite", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes; one total-checked search response", "none", False, "no material version", "1 request, up to 500 rows", "0 requests; possible processing skip only", "Both production sources already fetch one complete response.", _score(1, 1, 1, 3, 1, 2)),
    "lever": _cap("lever", F.FULL_CRAWL_ONLY, (P.NONE,), "yes only after complete offset pagination", "none; postings rows are rich", False, "createdAt is creation time, not a material edit version", "ceil(total / provider page size), bounded at 25 pages / 500 jobs", "0 safe requests", "No changed-since query, edit-complete ordering rule, cursor, or change feed is proven.", _score(4, 4, 3, 3, 3, 4)),
    "mcloud_cws": _cap("mcloud_cws", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes only after total/pagination reconciliation", "none", False, "no material version", "up to 2 requests", "0 network requests safely", "The single production source is already tightly bounded.", _score(1, 2, 1, 2, 2, 3)),
    "meta": _cap("meta", F.FULL_CRAWL_ONLY, (P.NONE,), "bespoke full search traversal", "none", False, "dates are not proven material versions", "up to 3 configured pages", "0 safe requests", "No reviewed incremental filter or removal/change feed exists.", _score(1, 2, 1, 2, 3, 4)),
    "oracle_recruiting": _cap("oracle_recruiting", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "yes only after hasMore/count/offset terminal evidence", "listing records are normalized without configured detail enrichment", False, "stable requisition IDs; update semantics/filter unproven", "1-5 configured pages across CE variants", "possible positive-page savings only", "Oracle query architecture is promising, but the repository proves no edit-complete boundary.", _score(3, 3, 3, 3, 4, 4)),
    "paycor": _cap("paycor", F.FULL_CRAWL_ONLY, (P.NONE,), "one bounded board collection", "none", False, "no material version", "1 request", "0 safe requests", "The complete response is the minimum safe mechanism.", _score(1, 1, 1, 3, 1, 2)),
    "peoplesoft": _cap("peoplesoft", F.FULL_CRAWL_ONLY, (P.NONE,), "session-bound bounded collection", "none in current adapter", False, "no material version", "1 configured page plus required session/bootstrap operations", "0 safe requests", "Stateful identity and no change contract make the full path necessary.", _score(1, 2, 1, 2, 3, 4)),
    "phenom": _cap("phenom", F.INSUFFICIENT_EVIDENCE, (P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY,), "yes only after complete refine-search pagination", "none", False, "posting dates but no proven material edit/order contract", "15 sources allow 50 pages/500 jobs; one allows 10/100", "potentially many positive-only pages, but not safely bounded today", "High configured depth makes it worth watching; absence and edits still require the full traversal.", _score(3, 5, 4, 2, 4, 5)),
    "public_json": _cap("public_json", F.INSUFFICIENT_EVIDENCE, (P.VALIDATED_CONDITIONAL_SNAPSHOT, P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY), "variant-specific complete collections or paginated APIs", "none", False, "date/version semantics differ by seven endpoints", "1-50 configured pages depending on variant", "endpoint-specific only; no generic safe estimate", "A generic capability would overstate seven unrelated contracts.", _score(2, 4, 3, 2, 4, 4)),
    "sap_successfactors": _cap("sap_successfactors", F.INSUFFICIENT_EVIDENCE, (P.COMPLETE_INVENTORY_PLUS_SELECTIVE_HYDRATION, P.PAGINATION_SHORT_CIRCUIT_POSITIVE_ONLY), "yes only after variant-specific RMK terminal evidence", "columnized variant hydrates each listed job; table/tile are listing-only", False, "no trustworthy item material version across variants", "up to 20 listing pages; columnized adds up to N detail requests", "potentially large detail/page savings, but neither changed set is provable", "High cost is real, while three protocol variants and missing identity evidence make a generic watermark unsafe.", _score(4, 5, 5, 3, 5, 5)),
    "shopify": _cap("shopify", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "bespoke full collection", "none", False, "no material version", "up to 5 configured pages", "0 safe network requests", "One production source does not justify adapter-specific state without a provider contract.", _score(1, 2, 1, 2, 2, 3)),
    "smartrecruiters": _cap("smartrecruiters", F.FULL_CRAWL_ONLY, (P.NONE,), "yes; totalFound + offset inventory", "none for all production sources", False, "releasedDate is release time, not material version", "ceil(totalFound / 100), zero production details", "0 safe requests", "Dedicated Stage B review rejected releasedAfter and all other patterns.", _score(3, 3, 1, 4, 3, 4)),
    "source_xml": _cap("source_xml", F.INSUFFICIENT_EVIDENCE, (P.VALIDATED_CONDITIONAL_SNAPSHOT, P.TIMESTAMP_POSITIVE_ONLY), "configuration-driven XML collection", "none", False, "lastmod/version semantics require an exact feed contract", "no production source; bounded XML page/index traversal when configured", "not estimable without a production contract", "The adapter is dispatchable but has zero production sources.", _score(0, 0, 0, 1, 3, 3)),
    "startdate": _cap("startdate", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "one bounded collection", "none", False, "no material version", "1 request", "0 requests; downstream only", "The single production source is already one request.", _score(1, 1, 1, 2, 1, 2)),
    "static_html": _cap("static_html", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "selector/terminal-contract dependent", "none", False, "dates are display values, not trusted versions", "24 sources cap at 1 page; BlackRock, Siemens, Chevron cap at 2", "0 network requests safely; minor downstream skip only", "There are no large-pagination production static sources and no safe newest-first boundary.", _score(4, 2, 1, 2, 2, 4)),
    "talentbrew": _cap("talentbrew", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "yes after server result/terminal evidence", "none", False, "no material version", "three sources cap at 1 page; Rakuten at 2", "0 safe network requests", "Current pagination cost is already minimal.", _score(1, 2, 1, 3, 2, 3)),
    "teamtailor": _cap("teamtailor", F.POSSIBLE_BUT_LOW_SAVINGS, (P.CONTENT_HASH_DOWNSTREAM_SKIP,), "one server-rendered collection", "none", False, "no material version", "1 request", "0 requests; downstream only", "The full snapshot is already one response.", _score(1, 1, 1, 2, 1, 2)),
    "uber": _cap("uber", F.FULL_CRAWL_ONLY, (P.NONE,), "bespoke full search traversal", "none", False, "posting timestamps only", "up to 5 configured pages", "0 safe requests", "No edit-complete timestamp/order contract exists.", _score(1, 2, 1, 2, 3, 4)),
    "ukg_pro": _cap("ukg_pro", F.FULL_CRAWL_ONLY, (P.NONE,), "yes; totalCount + offset inventory", "none; details are canonical URLs, not fetched content", False, "PostedDate is posting time", "ceil(totalCount / provider page size), capped at 1-3 pages", "0 safe requests", "No changed-since query or trustworthy material version exists.", _score(2, 2, 2, 3, 3, 4)),
    "workday": _cap("workday", F.FULL_CRAWL_ONLY, (P.NONE,), "yes only through total-checked full traversal or exact facet-partition union", "none; CXS listing rows are normalized directly", False, "postedOn is a display/posting age, not update metadata", "20-row CXS pages; up to 25 normal pages, with bounded facet subdivision above the 2,000-result safe capacity", "0 safe requests", "The API exposes no reviewed update filter/cursor/order guarantee; partitioning solves completeness, not incrementality.", _score(5, 5, 4, 4, 5, 5)),
}


ALIASES: Mapping[str, str] = {
    **{family: family for family in CAPABILITIES},
    "embedded_data": "javascript_embedded_data",
    "browser_html": "browser_rendered_html",
    "cornerstone": "cornerstone_csod",
    "csod": "cornerstone_csod",
    "successfactors": "sap_successfactors",
    "custom:amazon": "amazon",
    "custom:atlassian": "atlassian",
    "custom:google": "google",
    "custom:ibm_search": "ibm_search",
    "custom:meta": "meta",
    "custom:shopify": "shopify",
    "custom:uber": "uber",
}


def canonical_family(value: str) -> str:
    key = str(value or "").strip().casefold()
    try:
        return ALIASES[key]
    except KeyError:
        raise KeyError(f"Unknown Phase 6 adapter family: {value!r}") from None


def capability_for(value: str) -> AdapterCapability:
    return CAPABILITIES[canonical_family(value)]


def incremental_policy_for(value: str) -> IncrementalPolicy:
    """Return the deliberately disabled policy for a triaged family."""

    family = canonical_family(value)
    return IncrementalPolicy(
        capability=IncrementalCapability.NONE,
        strategy_version=f"phase6_triage_v1:{family}",
    )


@dataclass(frozen=True)
class RequestCostSummary:
    source_count: int
    listing_page_ceiling_total: int
    listing_page_ceiling_median: float
    listing_page_ceiling_max: int
    job_ceiling_total: int
    job_ceiling_max: int
    endpoint_variants: tuple[tuple[str, int], ...]


def request_cost_summary(targets: Iterable[CompanyTarget]) -> RequestCostSummary:
    """Summarize configured ceilings without pretending they are measurements."""

    values = tuple(targets)
    if not values:
        return RequestCostSummary(0, 0, 0.0, 0, 0, 0, ())
    pages = [int(target.max_pages) for target in values]
    jobs = [int(target.max_jobs) for target in values]
    if any(value < 1 for value in (*pages, *jobs)):
        raise ValueError("request-cost inputs must be positive configured bounds")
    variants = Counter(target.endpoint_variant or "<default>" for target in values)
    return RequestCostSummary(
        source_count=len(values),
        listing_page_ceiling_total=sum(pages),
        listing_page_ceiling_median=float(median(pages)),
        listing_page_ceiling_max=max(pages),
        job_ceiling_total=sum(jobs),
        job_ceiling_max=max(jobs),
        endpoint_variants=tuple(sorted(variants.items())),
    )

