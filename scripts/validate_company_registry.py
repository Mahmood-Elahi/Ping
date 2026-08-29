"""Read-only live validation for proposed company-registry additions.

This command deliberately imports only the registry model and production ATS
adapters.  It does not initialize scanning persistence, Supabase, SQLite, or
Google Sheets.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

import requests
import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.company_careers.adapters import (
    adp,
    ashby,
    avature,
    bamboohr,
    clearcompany,
    cornerstone_csod,
    dayforce,
    deel,
    eightfold,
    embedded_data,
    exalead,
    greenhouse,
    hawksearch_v2,
    hibob,
    icims,
    lever,
    oracle_recruiting,
    paycor,
    peoplesoft,
    phenom,
    sap_successfactors,
    static_html,
    startdate,
    talentbrew,
    teamtailor,
    smartrecruiters,
    ukg_pro,
    workday,
)
from src.scraper.company_careers.models import AdapterError, CompanyTarget, JobRecord


DEFAULT_CURRENT_CONFIG = Path("config/target_companies.yaml")
DEFAULT_CANDIDATE_CONFIG = Path("config/target_companies.generated.yaml")
DEFAULT_OUTPUT = Path("output/company_registry_validation.json")
DEFAULT_VALIDATED_CONFIG = Path("config/target_companies.validated.yaml")
WORKDAY_CANDIDATE_CONFIG = Path("config/target_companies.workday_candidates.yaml")
WORKDAY_VALIDATED_CONFIG = Path("config/target_companies.workday_validated.yaml")
ORACLE_CANDIDATE_CONFIG = Path("config/target_companies.oracle_candidates.yaml")
ORACLE_VALIDATED_CONFIG = Path("config/target_companies.oracle_validated.yaml")
SUCCESSFACTORS_CANDIDATE_CONFIG = Path("config/target_companies.successfactors_candidates.yaml")
SUCCESSFACTORS_VALIDATED_CONFIG = Path("config/target_companies.successfactors_validated.yaml")
EIGHTFOLD_CANDIDATE_CONFIG = Path("config/target_companies.eightfold_candidates.yaml")
EIGHTFOLD_VALIDATED_CONFIG = Path("config/target_companies.eightfold_validated.yaml")
PHENOM_CANDIDATE_CONFIG = Path("config/target_companies.phenom_candidates.yaml")
PHENOM_VALIDATED_CONFIG = Path("config/target_companies.phenom_validated.yaml")
MAX_BATCH_SIZE = 5
MAX_NORMALIZED_JOBS = 20
DEFAULT_TIMEOUT = 20
DEFAULT_ATTEMPTS = 2
DEFAULT_RETRY_DELAY = 0.5
DEFAULT_BATCH_DELAY = 1.0

VALIDATED_WITH_JOBS = "validated_with_jobs"
VALIDATED_ZERO_JOBS = "validated_zero_jobs"
INVALID_CONFIGURATION = "invalid_configuration"
BLOCKED = "blocked"
BROWSER_REQUIRED = "browser_required"
AUTHENTICATION_REQUIRED = "authentication_required"
SESSION_REQUIRED = "session_required"
TEMPORARY_FAILURE = "temporary_failure"
DUPLICATE_COMPANY = "duplicate_company"
DUPLICATE_SOURCE = "duplicate_source"
UNSUPPORTED_RESPONSE = "unsupported_response"
MALFORMED_RESPONSE = "malformed_response"
STALE_FINGERPRINT = "stale_fingerprint"

VALIDATION_STATUSES = (
    VALIDATED_WITH_JOBS,
    VALIDATED_ZERO_JOBS,
    INVALID_CONFIGURATION,
    BLOCKED,
    BROWSER_REQUIRED,
    AUTHENTICATION_REQUIRED,
    SESSION_REQUIRED,
    TEMPORARY_FAILURE,
    DUPLICATE_COMPANY,
    DUPLICATE_SOURCE,
    UNSUPPORTED_RESPONSE,
    MALFORMED_RESPONSE,
    STALE_FINGERPRINT,
)

AdapterFn = Callable[[Any, CompanyTarget, int], list[JobRecord]]

ADAPTERS: dict[str, AdapterFn] = {
    "adp": adp.fetch_jobs,
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
    "workday": workday.fetch_jobs,
    "oracle_recruiting": oracle_recruiting.fetch_jobs,
    "sap_successfactors": sap_successfactors.fetch_jobs,
    "eightfold": eightfold.fetch_jobs,
    "phenom": phenom.fetch_jobs,
    "avature": avature.fetch_jobs,
    "icims": icims.fetch_jobs,
    "static_html": static_html.fetch_jobs,
    "javascript_embedded_data": embedded_data.fetch_jobs,
    "exalead": exalead.fetch_jobs,
    "clearcompany": clearcompany.fetch_jobs,
    "cornerstone_csod": cornerstone_csod.fetch_jobs,
    "dayforce": dayforce.fetch_jobs,
    "deel": deel.fetch_jobs,
    "bamboohr": bamboohr.fetch_jobs,
    "startdate": startdate.fetch_jobs,
    "talentbrew": talentbrew.fetch_jobs,
    "teamtailor": teamtailor.fetch_jobs,
    "smartrecruiters": smartrecruiters.fetch_jobs,
    "hawksearch_v2": hawksearch_v2.fetch_jobs,
    "hibob": hibob.fetch_jobs,
    "ukg_pro": ukg_pro.fetch_jobs,
    "paycor": paycor.fetch_jobs,
    "peoplesoft": peoplesoft.fetch_jobs,
}


def load_registry(path: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry_path = Path(path)
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    entries = data.get("target_companies")
    if not isinstance(entries, list):
        raise ValueError(f"Registry has no target_companies list: {registry_path}")
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"Registry entry {index} is not a mapping: {registry_path}")
        normalized.append(dict(entry))
    return data, normalized


def _name_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("name") or "").casefold().split())


def detect_additions(
    current_entries: Iterable[Mapping[str, Any]],
    candidate_entries: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return candidate entries whose normalized company name is not current."""
    current_names = {_name_key(entry) for entry in current_entries}
    return [copy.deepcopy(dict(entry)) for entry in candidate_entries if _name_key(entry) not in current_names]


def source_key(entry: Mapping[str, Any]) -> Optional[str]:
    platform = str(entry.get("platform") or "").strip().casefold()
    if platform in {"greenhouse", "lever", "ashby"}:
        slug = str(entry.get("slug") or "").strip().casefold()
        return f"{platform}:{slug}" if slug else None
    if platform == "workday":
        tenant = str(entry.get("tenant") or "").strip().casefold()
        # The production Workday adapter emits source=workday:<tenant>, so a
        # repeated tenant would create colliding source identifiers.
        return f"workday:{tenant}" if tenant else None
    if platform == "oracle_recruiting":
        endpoint = urlparse(str(entry.get("endpoint_url") or ""))
        site_number = str(entry.get("site_number") or "").strip().casefold()
        host = (endpoint.hostname or "").casefold()
        return f"oracle_recruiting:{host}:{site_number}" if host and site_number else None
    if platform == "sap_successfactors":
        host = (urlparse(str(entry.get("search_url") or "")).hostname or "").casefold()
        return f"sap_successfactors:{host}" if host else None
    if platform == "cornerstone_csod":
        host = (urlparse(str(entry.get("search_url") or "")).hostname or "").casefold()
        site_id = str(entry.get("site_id") or "").strip().casefold()
        return f"cornerstone_csod:{host}:{site_id}" if host and site_id else None
    if platform == "dayforce":
        tenant = str(entry.get("tenant") or "").strip().casefold()
        site_id = str(entry.get("site_id") or "").strip().casefold()
        return f"dayforce:{tenant}:{site_id}" if tenant and site_id else None
    if platform == "eightfold":
        host = (urlparse(str(entry.get("endpoint_url") or "")).hostname or "").casefold()
        return f"eightfold:{host}" if host else None
    if platform == "phenom":
        host = (urlparse(str(entry.get("search_url") or "")).hostname or "").casefold()
        ref_num = str(entry.get("ref_num") or "").strip().casefold()
        return f"phenom:{host}:{ref_num}" if host and ref_num else None
    if platform in {"avature", "icims"}:
        host = (urlparse(str(entry.get("endpoint_url") or entry.get("search_url") or "")).hostname or "").casefold()
        board_id = str(entry.get("board_id") or "").strip().casefold()
        return f"{platform}:{host}:{board_id}" if host and board_id else None
    if platform == "exalead":
        host = (urlparse(str(entry.get("endpoint_url") or "")).hostname or "").casefold()
        return f"exalead:{host}" if host else None
    if platform in {"static_html", "javascript_embedded_data", "talentbrew", "teamtailor"}:
        host = (urlparse(str(entry.get("listing_url") or "")).hostname or "").casefold()
        slug = str(entry.get("slug") or "").strip().casefold()
        namespace = slug if platform == "static_html" and slug else host
        return f"{platform}:{namespace}" if namespace else None
    if platform == "deel":
        tenant = str(entry.get("tenant") or "").strip().casefold()
        return f"deel:{tenant}" if tenant else None
    if platform == "clearcompany":
        site_id = str(entry.get("site_id") or "").strip().casefold()
        return f"clearcompany:{site_id}" if site_id else None
    if platform == "bamboohr":
        slug = str(entry.get("slug") or "").strip().casefold()
        return f"bamboohr:{slug}" if slug else None
    if platform == "startdate":
        host = (urlparse(str(entry.get("careers_url") or "")).hostname or "").casefold()
        return f"startdate:{host}" if host else None
    if platform == "ukg_pro":
        parsed = urlparse(str(entry.get("careers_url") or ""))
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 3 and parts[1].casefold() == "jobboard":
            return f"ukg_pro:{(parsed.hostname or '').casefold()}:{parts[0].casefold()}:{parts[2].casefold()}"
        return None
    if platform == "paycor":
        parsed = urlparse(str(entry.get("endpoint_url") or ""))
        client_id = str((parse_qs(parsed.query).get("clientId") or [""])[0]).casefold()
        return f"paycor:{client_id}" if client_id else None
    if platform == "peoplesoft":
        hosts = sorted(str(host).strip().casefold() for host in entry.get("allowed_apply_hosts") or [] if str(host).strip())
        site_id = str(entry.get("site_id") or "1").strip().casefold()
        return f"peoplesoft:{hosts[0]}:{site_id}" if hosts else None
    if platform == "smartrecruiters":
        slug = str(entry.get("slug") or "").strip().casefold()
        return f"smartrecruiters:{slug}" if slug else None
    if platform == "hawksearch_v2":
        host = (urlparse(str(entry.get("endpoint_url") or "")).hostname or "").casefold()
        client_guid = str(entry.get("client_guid") or "").strip().casefold()
        return f"hawksearch_v2:{host}:{client_guid}" if host and client_guid else None
    if platform == "adp":
        variant = str(entry.get("endpoint_variant") or "").strip().casefold()
        if variant == adp.WORKFORCE_NOW:
            parsed = urlparse(str(entry.get("careers_url") or ""))
            cid = str((parse_qs(parsed.query).get("cid") or [""])[0]).casefold()
            return f"adp:{(parsed.hostname or '').casefold()}:{cid}" if parsed.hostname and cid else None
        slug = str(entry.get("slug") or "").strip().casefold()
        org = str(entry.get("organization_id") or "").strip().casefold()
        return f"adp:{org}:{slug}" if slug and org else None
    if platform == "hibob":
        slug = str(entry.get("slug") or "").strip().casefold()
        return f"hibob:{slug}" if slug else None
    return None


def configured_identifiers(entry: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(entry.get("platform") or "").strip().casefold()
    if platform in {"greenhouse", "lever", "ashby"}:
        return {"slug": entry.get("slug")}
    if platform == "workday":
        identifiers: dict[str, Any] = {
            "tenant": entry.get("tenant"),
            "wd_subdomain": entry.get("wd_subdomain"),
        }
        if entry.get("site_id") is not None:
            identifiers["site_id"] = entry.get("site_id")
        if entry.get("site_ids"):
            identifiers["site_ids"] = list(entry.get("site_ids") or [])
        return identifiers
    if platform == "oracle_recruiting":
        identifiers = {
            "site_number": entry.get("site_number"),
            "locale": entry.get("locale"),
        }
        if entry.get("organization_id") is not None:
            identifiers["organization_id"] = entry.get("organization_id")
        return identifiers
    if platform == "sap_successfactors":
        parsed = urlparse(str(entry.get("search_url") or ""))
        identifiers = {
            "host": parsed.hostname,
            "endpoint_variant": entry.get("endpoint_variant"),
        }
        if entry.get("locale") is not None:
            identifiers["locale"] = entry.get("locale")
        return identifiers
    if platform == "cornerstone_csod":
        return {
            "tenant": entry.get("tenant"),
            "site_id": entry.get("site_id"),
            "locale": entry.get("locale"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform == "dayforce":
        return {
            "tenant": entry.get("tenant"),
            "site_id": entry.get("site_id"),
            "locale": entry.get("locale"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform == "eightfold":
        return {
            "organization_id": entry.get("organization_id"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform == "phenom":
        return {
            "ref_num": entry.get("ref_num"),
            "locale": entry.get("locale"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform in {"avature", "icims"}:
        return {
            "tenant": entry.get("tenant"),
            "site_id": entry.get("site_id"),
            "board_id": entry.get("board_id"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform == "exalead":
        return {
            "endpoint_url": entry.get("endpoint_url"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform == "clearcompany":
        return {"site_id": entry.get("site_id")}
    if platform in {"ukg_pro", "paycor", "peoplesoft"}:
        return {
            "careers_url": entry.get("careers_url"),
            "endpoint_url": entry.get("endpoint_url"),
            "listing_url": entry.get("listing_url"),
            "site_id": entry.get("site_id"),
            "identity_regex": entry.get("identity_regex"),
            "job_attribution_regex": entry.get("job_attribution_regex"),
            "job_attribution_exclude_regex": entry.get("job_attribution_exclude_regex"),
        }
    if platform in {"bamboohr", "startdate"}:
        return {
            "slug": entry.get("slug"),
            "careers_url": entry.get("careers_url"),
            "endpoint_url": entry.get("endpoint_url"),
            "listing_url": entry.get("listing_url"),
            "job_attribution_regex": entry.get("job_attribution_regex"),
        }
    if platform == "smartrecruiters":
        return {
            "slug": entry.get("slug"),
            "endpoint_variant": entry.get("endpoint_variant"),
            "identity_regex": entry.get("identity_regex"),
        }
    if platform == "hawksearch_v2":
        return {
            "client_guid": entry.get("client_guid"),
            "index_name": entry.get("index_name"),
            "endpoint_variant": entry.get("endpoint_variant"),
        }
    if platform in {"adp", "hibob"}:
        return {
            "slug": entry.get("slug"),
            "organization_id": entry.get("organization_id"),
            "endpoint_variant": entry.get("endpoint_variant"),
            "identity_regex": entry.get("identity_regex"),
        }
    if platform in {"static_html", "javascript_embedded_data", "talentbrew", "teamtailor", "deel"}:
        return {
            "tenant": entry.get("tenant"),
            "listing_url": entry.get("listing_url"),
            "endpoint_variant": entry.get("endpoint_variant"),
            "identity_regex": entry.get("identity_regex"),
            "job_card_selector": entry.get("job_card_selector"),
            "embedded_json_path": entry.get("embedded_json_path"),
        }
    return {}


def endpoint_for(entry: Mapping[str, Any]) -> Optional[str]:
    platform = str(entry.get("platform") or "").strip().casefold()
    slug = str(entry.get("slug") or "").strip()
    if platform == "greenhouse" and slug:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if platform == "lever" and slug:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    if platform == "ashby" and slug:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    if platform == "workday":
        tenant = str(entry.get("tenant") or "").strip()
        subdomain = str(entry.get("wd_subdomain") or "").strip()
        site_id = str(entry.get("site_id") or "").strip()
        if not site_id:
            site_ids = list(entry.get("site_ids") or [])
            site_id = str(site_ids[0]).strip() if site_ids else ""
        if tenant and subdomain and site_id:
            return (
                f"https://{tenant}.{subdomain}.myworkdayjobs.com"
                f"/wday/cxs/{tenant}/{site_id}/jobs"
            )
    if platform == "oracle_recruiting":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform == "sap_successfactors":
        endpoint = str(entry.get("search_url") or "").strip()
        return endpoint or None
    if platform == "cornerstone_csod":
        endpoint = str(entry.get("search_url") or "").strip()
        return endpoint or None
    if platform == "dayforce":
        endpoint = str(entry.get("search_url") or "").strip()
        return endpoint or None
    if platform == "eightfold":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform == "phenom":
        endpoint = str(entry.get("search_url") or "").strip()
        return endpoint or None
    if platform in {"avature", "icims"}:
        endpoint = str(entry.get("endpoint_url") or entry.get("search_url") or "").strip()
        return endpoint or None
    if platform == "exalead":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform in {"static_html", "javascript_embedded_data", "talentbrew", "teamtailor", "deel"}:
        endpoint = str(entry.get("listing_url") or "").strip()
        return endpoint or None
    if platform == "clearcompany":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform == "bamboohr":
        endpoint = str(entry.get("listing_url") or "").strip()
        return endpoint or None
    if platform == "startdate":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform == "ukg_pro":
        endpoint = str(entry.get("careers_url") or "").strip()
        return endpoint or None
    if platform == "paycor":
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    if platform == "peoplesoft":
        endpoint = str(entry.get("listing_url") or "").strip()
        return endpoint or None
    if platform in {"smartrecruiters", "hawksearch_v2", "adp", "hibob"}:
        endpoint = str(entry.get("endpoint_url") or "").strip()
        return endpoint or None
    return None


def configuration_error(entry: Mapping[str, Any]) -> Optional[str]:
    name = str(entry.get("name") or "").strip()
    platform = str(entry.get("platform") or "").strip().casefold()
    if not name:
        return "Missing company name"
    if platform not in ADAPTERS:
        return f"Unsupported validation platform: {platform or 'missing'}"
    if platform in {"greenhouse", "lever", "ashby"}:
        if not str(entry.get("slug") or "").strip():
            return f"{platform.title()} target is missing slug"
    elif platform == "workday":
        missing = [
            field
            for field in ("tenant", "wd_subdomain")
            if not str(entry.get(field) or "").strip()
        ]
        if not entry.get("site_id") and not entry.get("site_ids"):
            missing.append("site_id/site_ids")
        if missing:
            return f"Workday target is missing {', '.join(missing)}"
    try:
        target = CompanyTarget.from_dict(dict(entry))
    except (KeyError, TypeError, ValueError) as exc:
        return f"Invalid target fields: {exc}"
    if platform == "oracle_recruiting":
        try:
            oracle_recruiting.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "sap_successfactors":
        unsupported = [
            field
            for field in ("company_id", "career_site_id")
            if entry.get(field) not in (None, "")
        ]
        if unsupported:
            return (
                "SAP SuccessFactors public RMK endpoint does not accept "
                + ", ".join(unsupported)
            )
        try:
            sap_successfactors.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "cornerstone_csod":
        try:
            cornerstone_csod.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "dayforce":
        try:
            dayforce.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "eightfold":
        try:
            eightfold.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "phenom":
        try:
            phenom.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "avature":
        try:
            avature.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "icims":
        try:
            icims.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "exalead":
        try:
            exalead.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "static_html":
        try:
            static_html.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "talentbrew":
        try:
            talentbrew.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "teamtailor":
        try:
            teamtailor.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "deel":
        try:
            deel.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "javascript_embedded_data":
        try:
            embedded_data.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "clearcompany":
        try:
            clearcompany.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "bamboohr":
        try:
            bamboohr.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "startdate":
        try:
            startdate.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "ukg_pro":
        try:
            ukg_pro.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "paycor":
        try:
            paycor.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "peoplesoft":
        try:
            peoplesoft.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "smartrecruiters":
        try:
            smartrecruiters.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "hawksearch_v2":
        try:
            hawksearch_v2.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "adp":
        try:
            adp.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    if platform == "hibob":
        try:
            hibob.validate_target(target)
        except AdapterError as exc:
            return str(exc)
    return None


class RecordingRetrySession:
    """Duck-typed requests session that records final responses and retries."""

    TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.attempts = max(1, attempts)
        self.retry_delay = max(0.0, retry_delay)
        self.sleep = sleep
        self.responses: list[Any] = []
        self.requested_urls: list[str] = []
        self.attempt_count = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("post", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        last_error: Optional[requests.RequestException] = None
        for attempt in range(self.attempts):
            self.attempt_count += 1
            self.requested_urls.append(url)
            try:
                response = getattr(self.session, method)(url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    self.sleep(self.retry_delay)
                    continue
                raise
            self.responses.append(response)
            status_code = getattr(response, "status_code", None)
            if status_code in self.TRANSIENT_STATUSES and attempt + 1 < self.attempts:
                self.sleep(self.retry_delay)
                continue
            return response
        if last_error is not None:  # pragma: no cover - loop always raises first
            raise last_error
        raise RuntimeError("HTTP retry loop finished without a response")

    @property
    def final_response(self) -> Optional[Any]:
        return self.responses[-1] if self.responses else None


def _response_shape_error(platform: str, response: Any) -> Optional[str]:
    if platform in {
        "sap_successfactors",
        "cornerstone_csod",
        "dayforce",
        "phenom",
        "avature",
        "static_html",
        "javascript_embedded_data",
        "talentbrew",
        "teamtailor",
        "deel",
        "ukg_pro",
        "paycor",
        "peoplesoft",
        "smartrecruiters",
        "hawksearch_v2",
        "adp",
        "hibob",
    } or (
        platform == "icims"
        and "html" in str(getattr(response, "headers", {}).get("content-type", "")).casefold()
    ):
        # These production adapters validate every full HTML page/fragment
        # before returning normalized jobs.
        return None
    try:
        payload = response.json()
    except (ValueError, requests.RequestException) as exc:
        return f"Response is not valid JSON: {exc}"

    if platform == "lever":
        if not isinstance(payload, list):
            return "Lever response must be a JSON list"
        return None
    if platform == "oracle_recruiting":
        if not isinstance(payload, Mapping):
            return "Oracle Recruiting response must be a JSON object"
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
            return "Oracle Recruiting response must contain one search envelope in items"
        envelope = items[0]
        if not isinstance(envelope.get("requisitionList"), list):
            return "Oracle Recruiting search envelope must contain a requisitionList list"
        for field in ("TotalJobsCount", "Offset", "Limit", "SiteNumber"):
            if field not in envelope:
                return f"Oracle Recruiting search envelope is missing {field}"
        return None
    if platform == "eightfold":
        if not isinstance(payload, Mapping):
            return "Eightfold response must be a JSON object"
        if payload.get("status") != 200:
            return "Eightfold response status must be 200"
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return "Eightfold response must contain a data object"
        if not isinstance(data.get("positions"), list):
            return "Eightfold data must contain a positions list"
        count = data.get("count")
        if "count" in data and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            return "Eightfold data must contain a non-negative count"
        return None
    if platform == "icims":
        if not isinstance(payload, Mapping):
            return "iCIMS Jibe response must be a JSON object"
        if not isinstance(payload.get("jobs"), list):
            return "iCIMS Jibe response must contain a jobs list"
        total = payload.get("totalCount")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return "iCIMS Jibe totalCount must be a non-negative integer"
        return None
    if platform == "exalead":
        if not isinstance(payload, Mapping):
            return "Exalead Careers response must be a JSON object"
        if not isinstance(payload.get("hits"), list):
            return "Exalead Careers response must contain a hits list"
        total = payload.get("nhits")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return "Exalead Careers nhits must be a non-negative integer"
        return None
    if platform == "clearcompany":
        if not isinstance(payload, Mapping):
            return "ClearCompany response must be a JSON object"
        if not isinstance(payload.get("results"), list):
            return "ClearCompany response must contain a results list"
        return None
    expected_key = {
        "greenhouse": "jobs",
        "ashby": "jobs",
        "workday": "jobPostings",
    }.get(platform)
    if not isinstance(payload, Mapping):
        return f"{platform.title()} response must be a JSON object"
    if expected_key not in payload or not isinstance(payload[expected_key], list):
        return f"{platform.title()} response must contain a {expected_key} list"
    return None


def _redirects(session: RecordingRetrySession) -> list[dict[str, Any]]:
    redirects: list[dict[str, Any]] = []
    for response in session.responses:
        for prior in getattr(response, "history", []) or []:
            redirects.append(
                {
                    "status": getattr(prior, "status_code", None),
                    "from": getattr(prior, "url", None),
                    "to": getattr(response, "url", None),
                }
            )
    return redirects


def _response_error_status(response: Optional[Any]) -> tuple[str, str]:
    status_code = getattr(response, "status_code", None) if response is not None else None
    if status_code in {401, 403, 407, 451}:
        return BLOCKED, f"Endpoint access was blocked (HTTP {status_code})"
    if status_code in RecordingRetrySession.TRANSIENT_STATUSES:
        return TEMPORARY_FAILURE, f"Endpoint returned a temporary HTTP {status_code} failure"
    if status_code in {400, 404, 410, 422}:
        return INVALID_CONFIGURATION, f"Endpoint rejected the configured ATS identifier (HTTP {status_code})"
    if status_code is not None and status_code != 200:
        return UNSUPPORTED_RESPONSE, f"Unexpected HTTP status {status_code}"
    return UNSUPPORTED_RESPONSE, "Adapter could not parse the endpoint response"


def _adapter_error_status(response: Optional[Any], error: AdapterError) -> tuple[str, str]:
    text = str(error).casefold()
    if "authentication required" in text:
        return AUTHENTICATION_REQUIRED, "Endpoint requires authentication"
    if "session required" in text:
        return SESSION_REQUIRED, "Endpoint requires session initialization"
    if "browser required" in text or "javascript required" in text:
        return BROWSER_REQUIRED, "Endpoint requires JavaScript or browser execution"
    if "blocked" in text or "consent page" in text:
        return BLOCKED, "Endpoint returned blocked or consent content"
    if any(contract in text for contract in ("stable job_id", "valid apply url", "apply host")):
        return UNSUPPORTED_RESPONSE, str(error)
    status, message = _response_error_status(response)
    if getattr(response, "status_code", None) == 200:
        return MALFORMED_RESPONSE, "Endpoint returned malformed or unexpected content"
    return status, message


def _allowed_apply_host(host: str, allowed_hosts: Iterable[str]) -> bool:
    normalized_host = host.strip().casefold().rstrip(".")
    return any(
        normalized_host == str(allowed).strip().casefold().rstrip(".")
        for allowed in allowed_hosts
        if str(allowed).strip()
    )


def _valid_job_error(
    jobs: list[JobRecord],
    allowed_apply_hosts: Iterable[str] = (),
) -> Optional[str]:
    seen_ids: set[tuple[str, str]] = set()
    configured_hosts = [str(host) for host in allowed_apply_hosts if str(host).strip()]
    for index, job in enumerate(jobs, start=1):
        if not job.job_id or not job.source:
            return f"Job {index} has no stable job_id/source identifier"
        identity = (job.source.casefold(), job.job_id.casefold())
        if identity in seen_ids:
            return f"Job {index} duplicates stable job_id/source identifier"
        seen_ids.add(identity)
        parsed_url = urlparse(job.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return f"Job {index} has no valid apply URL"
        if configured_hosts and not _allowed_apply_host(parsed_url.hostname or "", configured_hosts):
            return f"Job {index} apply host is not allow-listed: {parsed_url.hostname or ''}"
    return None


def _reported_total(platform: str, response: Any) -> Optional[int]:
    try:
        payload = response.json()
    except (AttributeError, ValueError, requests.RequestException):
        return None
    candidate: Any = None
    if platform == "lever" and isinstance(payload, list):
        candidate = len(payload)
    elif isinstance(payload, Mapping):
        if platform == "greenhouse" and isinstance(payload.get("meta"), Mapping):
            candidate = payload["meta"].get("total")
        elif platform == "ashby" and isinstance(payload.get("jobs"), list):
            candidate = len(payload["jobs"])
        else:
            candidate = payload.get("total")
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        return None
    return candidate


def _base_result(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": str(entry.get("name") or ""),
        "fingerprint_confidence": entry.get("fingerprint_confidence"),
        "fingerprint_evidence": entry.get("fingerprint_evidence"),
        "platform": str(entry.get("platform") or ""),
        "endpoint_variant": entry.get("endpoint_variant"),
        "configured_identifiers": configured_identifiers(entry),
        "endpoint_tested": endpoint_for(entry),
        "request_method": (
            "GET"
            if str(entry.get("platform") or "").casefold() in {"eightfold", "phenom"}
            else None
        ),
        "http_status": None,
        "result_status": "not_started",
        "jobs_returned": 0,
        "jobs_normalized": 0,
        "pages_requested": 0,
        "total_available_jobs": None,
        "sample_job_id": None,
        "sample_job_title": None,
        "sample_apply_url": None,
        "sample_location": None,
        "sample_posting_date": None,
        "parse_success": False,
        "stable_job_id_result": "not_tested",
        "apply_url_result": "not_tested",
        "duplicate_source_status": "unique",
        "duplicate_company_status": "unique",
        "validation_status": INVALID_CONFIGURATION,
        "error": None,
        "exact_failure_reason": None,
        "warning": entry.get("fingerprint_warning"),
        "safe_to_activate": False,
        "redirects": [],
        "attempts": 0,
        "pagination_result": None,
    }


def _oracle_pagination_result(
    session: RecordingRetrySession,
    target: CompanyTarget,
    jobs: list[JobRecord],
) -> dict[str, Any]:
    pages: list[dict[str, int]] = []
    total_available: Optional[int] = None
    for response in session.responses:
        if getattr(response, "status_code", None) != 200:
            continue
        try:
            payload = response.json()
            envelope = payload["items"][0]
            requisitions = envelope["requisitionList"]
            page = {
                "offset": int(envelope["Offset"]),
                "limit": int(envelope["Limit"]),
                "jobs_returned": len(requisitions),
            }
            total_available = int(envelope["TotalJobsCount"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        pages.append(page)

    max_pages = min(max(1, target.max_pages), oracle_recruiting.MAX_PAGES)
    maximum_results = min(
        oracle_recruiting.MAX_RESULTS,
        max_pages * oracle_recruiting.PAGE_SIZE,
    )
    if total_available == 0 and pages:
        stop_reason = "validated_zero_jobs"
    elif total_available is not None and len(jobs) >= total_available:
        stop_reason = "total_jobs_reached"
    elif pages and pages[-1]["jobs_returned"] < pages[-1]["limit"]:
        stop_reason = "short_page"
    elif len(pages) >= max_pages:
        stop_reason = "maximum_pages_reached"
    else:
        stop_reason = "request_failed"
    return {
        "pages_fetched": len(pages),
        "page_size": oracle_recruiting.PAGE_SIZE,
        "maximum_pages": max_pages,
        "maximum_results": maximum_results,
        "total_available": total_available,
        "jobs_normalized": len(jobs),
        "offsets": [page["offset"] for page in pages],
        "pages": pages,
        "stop_reason": stop_reason,
    }


def validate_company(
    entry: Mapping[str, Any],
    *,
    duplicate_of: Optional[str] = None,
    duplicate_company_of: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    session_factory: Callable[[], Any] = requests.Session,
    sleep: Callable[[float], None] = time.sleep,
    adapters: Optional[Mapping[str, AdapterFn]] = None,
    max_normalized_jobs: int = MAX_NORMALIZED_JOBS,
    max_pages_limit: int = 1,
) -> dict[str, Any]:
    result = _base_result(entry)
    if entry.get("stale_fingerprint"):
        reason = str(
            entry.get("stale_fingerprint_reason")
            or "Stored platform fingerprint is stale"
        )
        result.update(
            result_status="stale_fingerprint",
            validation_status=STALE_FINGERPRINT,
            error=reason,
            exact_failure_reason=reason,
        )
        return result
    if entry.get("browser_required"):
        reason = "Source requires browser automation"
        result.update(
            result_status="browser_required",
            validation_status=BROWSER_REQUIRED,
            error=reason,
            exact_failure_reason=reason,
        )
        return result
    if entry.get("authentication_required"):
        reason = "Source requires authentication"
        result.update(
            result_status="authentication_required",
            validation_status=AUTHENTICATION_REQUIRED,
            error=reason,
            exact_failure_reason=reason,
        )
        return result
    if entry.get("session_required"):
        reason = "Source requires unstable session initialization"
        result.update(
            result_status="session_required",
            validation_status=SESSION_REQUIRED,
            error=reason,
            exact_failure_reason=reason,
        )
        return result
    if duplicate_company_of:
        reason = f"Company duplicates {duplicate_company_of}"
        result.update(
            result_status="duplicate_company",
            duplicate_company_status=f"duplicates {duplicate_company_of}",
            validation_status=DUPLICATE_COMPANY,
            error=reason,
            exact_failure_reason=reason,
        )
        return result
    config_error = configuration_error(entry)
    if config_error:
        result.update(
            result_status="configuration_error",
            error=config_error,
            exact_failure_reason=config_error,
        )
        return result
    if duplicate_of:
        result.update(
            result_status="duplicate",
            duplicate_source_status=f"duplicates {duplicate_of}",
            validation_status=DUPLICATE_SOURCE,
            error=f"Source configuration duplicates {duplicate_of}",
            exact_failure_reason=f"Source configuration duplicates {duplicate_of}",
        )
        return result

    platform = str(entry["platform"]).strip().casefold()
    target = CompanyTarget.from_dict(dict(entry))
    if platform == "workday":
        target.max_pages = min(max(1, target.max_pages), max(1, max_pages_limit))
    session_attempts = (
        1
        if platform in {"oracle_recruiting", "sap_successfactors", "eightfold", "phenom", "avature", "icims"}
        else attempts
    )
    session = RecordingRetrySession(
        session_factory(), attempts=session_attempts, retry_delay=retry_delay, sleep=sleep
    )
    adapter_map = adapters or ADAPTERS
    try:
        jobs = adapter_map[platform](session, target, timeout)
    except requests.RequestException as exc:
        response = session.final_response
        status, message = _response_error_status(response)
        if response is None:
            status, message = TEMPORARY_FAILURE, f"Request failed after retries: {exc}"
        result.update(validation_status=status, result_status="request_failed", error=message)
    except AdapterError as exc:
        status, message = _adapter_error_status(session.final_response, exc)
        result.update(
            validation_status=status,
            result_status="adapter_failed",
            error=f"{message}: {exc}",
        )
    except (KeyError, TypeError, ValueError) as exc:
        result.update(
            validation_status=UNSUPPORTED_RESPONSE,
            result_status="parse_failed",
            error=f"Malformed or unsupported response: {exc}",
        )
    except Exception as exc:  # contain a single company's unexpected parser failure
        result.update(
            validation_status=UNSUPPORTED_RESPONSE,
            result_status="parse_failed",
            error=f"Unexpected {exc.__class__.__name__}: {exc}",
        )
    else:
        jobs_returned = len(jobs)
        jobs = jobs[: max(0, max_normalized_jobs)]
        response = session.final_response
        shape_error = (
            _response_shape_error(platform, response)
            if response is not None
            else "Adapter made no HTTP request"
        )
        job_error = _valid_job_error(jobs, target.allowed_apply_hosts)
        if not shape_error:
            if jobs:
                result["stable_job_id_result"] = (
                    "valid"
                    if all(job.job_id and job.source for job in jobs)
                    and len({(job.source.casefold(), job.job_id.casefold()) for job in jobs})
                    == len(jobs)
                    else "invalid"
                )
                result["apply_url_result"] = (
                    "valid"
                    if all(
                        urlparse(job.url).scheme in {"http", "https"}
                        and bool(urlparse(job.url).netloc)
                        and (
                            not target.allowed_apply_hosts
                            or _allowed_apply_host(
                                urlparse(job.url).hostname or "",
                                target.allowed_apply_hosts,
                            )
                        )
                        for job in jobs
                    )
                    else "invalid"
                )
            else:
                result["stable_job_id_result"] = "not_applicable_zero_jobs"
                result["apply_url_result"] = "not_applicable_zero_jobs"
        if shape_error or job_error:
            result.update(
                validation_status=UNSUPPORTED_RESPONSE,
                result_status="parse_failed",
                error=shape_error or job_error,
            )
        else:
            sample = jobs[0] if jobs else None
            result.update(
                result_status="success",
                jobs_returned=jobs_returned,
                jobs_normalized=len(jobs),
                sample_job_id=sample.job_id if sample else None,
                sample_job_title=sample.title if sample else None,
                sample_apply_url=sample.url if sample else None,
                sample_location=sample.location if sample else None,
                sample_posting_date=sample.updated_at if sample else None,
                parse_success=True,
                validation_status=VALIDATED_WITH_JOBS if jobs else VALIDATED_ZERO_JOBS,
                safe_to_activate=True,
            )

    if platform == "oracle_recruiting":
        result["pagination_result"] = _oracle_pagination_result(
            session,
            target,
            jobs if "jobs" in locals() else [],
        )
    elif platform == "cornerstone_csod":
        pagination = getattr(session, "cornerstone_csod_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = len(pagination.get("pages") or [])
            result["total_available_jobs"] = pagination.get("total_available")
    elif platform == "dayforce":
        pagination = getattr(session, "dayforce_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = len(pagination.get("pages") or [])
            result["total_available_jobs"] = pagination.get("total_available")
    elif platform == "sap_successfactors":
        pagination = getattr(session, "sap_successfactors_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            result["total_available_jobs"] = pagination.get("total_available")
            diagnostics = list(pagination.get("diagnostics") or [])
            if diagnostics and result["safe_to_activate"]:
                result["warning"] = f"Skipped {len(diagnostics)} malformed or duplicate job entries"
    elif platform == "eightfold":
        pagination = getattr(session, "eightfold_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            result["total_available_jobs"] = pagination.get("final_total_available")
            diagnostics = list(pagination.get("diagnostics") or [])
            error_diagnostics = [item for item in diagnostics if item.get("error")]
            pagination_warnings = list(pagination.get("warnings") or [])
            if result["safe_to_activate"]:
                warning_messages = []
                if result["warning"]:
                    warning_messages.append(str(result["warning"]))
                warning_messages.extend(str(message) for message in pagination_warnings)
                if error_diagnostics:
                    warning_messages.append(
                        "Skipped "
                        f"{len(error_diagnostics)} malformed or duplicate job entries"
                    )
                result["warning"] = "; ".join(warning_messages) or None
    elif platform == "phenom":
        pagination = getattr(session, "phenom_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            result["total_available_jobs"] = pagination.get("total_available")
            diagnostics = list(pagination.get("diagnostics") or [])
            if diagnostics and result["safe_to_activate"]:
                message = f"Skipped {len(diagnostics)} malformed or duplicate job entries"
                result["warning"] = (
                    f"{result['warning']}; {message}" if result["warning"] else message
                )
    elif platform in {"avature", "icims"}:
        pagination = getattr(session, f"{platform}_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            result["total_available_jobs"] = pagination.get("total_available")
            warnings = list(pagination.get("warnings") or [])
            diagnostics = list(pagination.get("diagnostics") or [])
            if result["safe_to_activate"] and (warnings or diagnostics):
                messages = [str(item) for item in warnings]
                if diagnostics:
                    messages.append(f"Skipped {len(diagnostics)} malformed or duplicate job entries")
                result["warning"] = "; ".join(messages)
    elif platform in {"static_html", "javascript_embedded_data", "talentbrew", "teamtailor", "deel"}:
        attribute = {
            "static_html": "static_html_pagination",
            "javascript_embedded_data": "embedded_data_pagination",
            "talentbrew": "talentbrew_pagination",
            "teamtailor": "teamtailor_pagination",
            "deel": "deel_pagination",
        }[platform]
        pagination = getattr(session, attribute, None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            diagnostics = list(pagination.get("diagnostics") or [])
            warnings = list(pagination.get("warnings") or [])
            if result["safe_to_activate"] and (warnings or diagnostics):
                messages = [str(item) for item in warnings]
                if diagnostics:
                    messages.append(f"Skipped {len(diagnostics)} malformed or duplicate job entries")
                result["warning"] = "; ".join(messages)
    elif platform in {"smartrecruiters", "hawksearch_v2"}:
        pagination = getattr(session, f"{platform}_pagination", None)
        if isinstance(pagination, Mapping):
            result["pagination_result"] = dict(pagination)
            result["pages_requested"] = int(pagination.get("pages_fetched") or 0)
            result["total_available_jobs"] = pagination.get("total_available")
    response = session.final_response
    if response is not None and result["total_available_jobs"] is None:
        result["total_available_jobs"] = _reported_total(platform, response)
    if result["pages_requested"] == 0:
        result["pages_requested"] = len(session.responses)
    result["http_status"] = getattr(response, "status_code", None) if response is not None else None
    response_url = getattr(response, "url", None) if response is not None else None
    result["endpoint_tested"] = (
        response_url
        or (session.requested_urls[-1] if session.requested_urls else result["endpoint_tested"])
    )
    result["redirects"] = _redirects(session)
    result["attempts"] = session.attempt_count
    if result["redirects"] and result["safe_to_activate"]:
        result["warning"] = "Endpoint redirected before returning a valid ATS response"
    result["exact_failure_reason"] = result["error"]
    return result


def _duplicate_owners(
    current_entries: Iterable[Mapping[str, Any]],
    additions: Iterable[Mapping[str, Any]],
) -> dict[int, str]:
    owner_by_source: dict[str, str] = {}
    for entry in current_entries:
        key = source_key(entry)
        if key:
            owner_by_source.setdefault(key, str(entry.get("name") or "existing entry"))
    duplicates: dict[int, str] = {}
    for index, entry in enumerate(additions):
        key = source_key(entry)
        if not key:
            continue
        owner = owner_by_source.get(key)
        if owner:
            duplicates[index] = owner
        else:
            owner_by_source[key] = str(entry.get("name") or f"candidate {index + 1}")
    return duplicates


def _duplicate_company_owners(
    current_entries: Iterable[Mapping[str, Any]],
    additions: Iterable[Mapping[str, Any]],
) -> dict[int, str]:
    owner_by_name = {
        _name_key(entry): str(entry.get("name") or "existing entry")
        for entry in current_entries
        if _name_key(entry)
    }
    duplicates: dict[int, str] = {}
    for index, entry in enumerate(additions):
        key = _name_key(entry)
        if not key:
            continue
        owner = owner_by_name.get(key)
        if owner:
            duplicates[index] = owner
        else:
            owner_by_name[key] = str(entry.get("name") or f"candidate {index + 1}")
    return duplicates


def build_validated_registry_bytes(
    current_path: Path | str,
    safe_additions: Iterable[Mapping[str, Any]],
) -> bytes:
    """Append additions while leaving every byte of the current entries intact."""
    current_bytes = Path(current_path).read_bytes()
    newline = b"\r\n" if b"\r\n" in current_bytes else b"\n"
    additions = [dict(entry) for entry in safe_additions]
    if not additions:
        return current_bytes
    _, current_entries = load_registry(current_path)
    dumped = yaml.safe_dump(
        additions,
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )
    indented = "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))
    appended = indented.encode("utf-8").replace(b"\n", newline)
    if not current_entries:
        # There are no configuration-entry bytes to preserve in this special
        # case, and a compact `target_companies: []` cannot be appended to.
        return b"target_companies:" + newline + appended
    separator = b"" if current_bytes.endswith((b"\n", b"\r")) else newline
    return current_bytes + separator + appended


def build_candidate_registry_bytes(
    current_path: Path | str,
    candidate_additions: Iterable[Mapping[str, Any]],
) -> bytes:
    """Build a candidate registry with the current registry as an exact prefix."""
    return build_validated_registry_bytes(current_path, candidate_additions)


def _write_bytes_if_changed(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    path.write_bytes(content)


def _write_json_if_changed(path: Path, data: Mapping[str, Any]) -> None:
    content = (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
    _write_bytes_if_changed(path, content)


def validate_registry(
    current_config: Path | str,
    candidate_config: Path | str,
    *,
    only_additions: bool = False,
    batch_size: int = MAX_BATCH_SIZE,
    start: int = 0,
    limit: Optional[int] = None,
    output: Path | str = DEFAULT_OUTPUT,
    validated_config: Path | str = DEFAULT_VALIDATED_CONFIG,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    batch_delay: float = DEFAULT_BATCH_DELAY,
    session_factory: Callable[[], Any] = requests.Session,
    sleep: Callable[[float], None] = time.sleep,
    adapters: Optional[Mapping[str, AdapterFn]] = None,
) -> dict[str, Any]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if start < 0:
        raise ValueError("start must be at least 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be at least 0")

    _, current_entries = load_registry(current_config)
    _, candidate_entries = load_registry(candidate_config)
    selected_entries = (
        detect_additions(current_entries, candidate_entries)
        if only_additions
        else [copy.deepcopy(entry) for entry in candidate_entries]
    )
    total_selected = len(selected_entries)
    stop = None if limit is None else start + limit
    selected_entries = selected_entries[start:stop]

    if dry_run:
        return {
            "current_config": str(current_config),
            "candidate_config": str(candidate_config),
            "only_additions": only_additions,
            "dry_run": True,
            "batch_size": batch_size,
            "start": start,
            "limit": limit,
            "summary": {
                "candidate_entries": len(candidate_entries),
                "current_entries": len(current_entries),
                "additions_detected": len(detect_additions(current_entries, candidate_entries)),
                "companies_selected": len(selected_entries),
                "companies_tested": 0,
            },
            "planned_companies": [str(entry.get("name") or "") for entry in selected_entries],
            "results": [],
        }

    duplicates = _duplicate_owners(current_entries, selected_entries)
    duplicate_companies = _duplicate_company_owners(current_entries, selected_entries)
    results: list[dict[str, Any]] = []
    for batch_start in range(0, len(selected_entries), batch_size):
        batch = selected_entries[batch_start : batch_start + batch_size]
        for offset, entry in enumerate(batch):
            index = batch_start + offset
            results.append(
                validate_company(
                    entry,
                    duplicate_of=duplicates.get(index),
                    duplicate_company_of=duplicate_companies.get(index),
                    timeout=timeout,
                    attempts=attempts,
                    retry_delay=retry_delay,
                    session_factory=session_factory,
                    sleep=sleep,
                    adapters=adapters,
                )
            )
        if batch_start + batch_size < len(selected_entries) and batch_delay > 0:
            sleep(batch_delay)

    safe_names = [result["company"] for result in results if result["safe_to_activate"]]
    safe_name_keys = {" ".join(name.casefold().split()) for name in safe_names}
    safe_additions = [entry for entry in selected_entries if _name_key(entry) in safe_name_keys]
    status_counts = Counter(result["validation_status"] for result in results)
    validated_bytes = build_validated_registry_bytes(current_config, safe_additions)
    validated_data = yaml.safe_load(validated_bytes.decode("utf-8")) or {}
    validated_entries = validated_data.get("target_companies") or []
    report = {
        "current_config": str(current_config),
        "candidate_config": str(candidate_config),
        "validated_config": str(validated_config),
        "only_additions": only_additions,
        "dry_run": False,
        "batch_size": batch_size,
        "start": start,
        "limit": limit,
        "summary": {
            "candidate_entries": len(candidate_entries),
            "current_entries": len(current_entries),
            "additions_detected": len(detect_additions(current_entries, candidate_entries)),
            "companies_selected": len(selected_entries),
            "companies_tested": len(results),
            "status_counts": {status: status_counts.get(status, 0) for status in VALIDATION_STATUSES},
            "safe_to_activate": safe_names,
            "excluded": [
                {
                    "company": result["company"],
                    "status": result["validation_status"],
                    "reason": result["error"] or result["warning"],
                }
                for result in results
                if not result["safe_to_activate"]
            ],
            "validated_config_entries": len(validated_entries),
            "total_selected_before_window": total_selected,
        },
        "results": results,
    }
    _write_json_if_changed(Path(output), report)
    _write_bytes_if_changed(Path(validated_config), validated_bytes)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-config", type=Path, default=DEFAULT_CURRENT_CONFIG)
    parser.add_argument("--candidate-config", type=Path, default=DEFAULT_CANDIDATE_CONFIG)
    parser.add_argument("--only-additions", action="store_true")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--validated-config", type=Path)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    validated_config = args.validated_config
    if validated_config is None:
        validated_config = (
            PHENOM_VALIDATED_CONFIG
            if args.candidate_config == PHENOM_CANDIDATE_CONFIG
            else (
                EIGHTFOLD_VALIDATED_CONFIG
                if args.candidate_config == EIGHTFOLD_CANDIDATE_CONFIG
                else (
                    SUCCESSFACTORS_VALIDATED_CONFIG
                    if args.candidate_config == SUCCESSFACTORS_CANDIDATE_CONFIG
                    else (
                        ORACLE_VALIDATED_CONFIG
                        if args.candidate_config == ORACLE_CANDIDATE_CONFIG
                        else (
                            WORKDAY_VALIDATED_CONFIG
                            if args.candidate_config == WORKDAY_CANDIDATE_CONFIG
                            else DEFAULT_VALIDATED_CONFIG
                        )
                    )
                )
            )
        )
    try:
        report = validate_registry(
            args.current_config,
            args.candidate_config,
            only_additions=args.only_additions,
            batch_size=args.batch_size,
            start=args.start,
            limit=args.limit,
            output=args.output,
            validated_config=validated_config,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Validation setup failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
