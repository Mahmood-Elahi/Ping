from __future__ import annotations

import json
from pathlib import Path
import unittest
import yaml

from src.scraper.company_careers.adapters import ashby, greenhouse, jobvite, lever, public_json
from src.scraper.company_careers.models import (
    AdapterError,
    CompanyTarget,
    source_scan_metadata,
)
from src.scraper.company_careers.registry import load_target_companies
from src.scraper.company_careers.source_integrity import (
    IntegrityAction,
    assess_registry_entry,
    load_registry_entries,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "target_companies.yaml"
REPAIRS = ROOT / "config" / "company_careers_reliability_repairs_v1.yaml"


class Response:
    def __init__(self, body, *, url: str, content_type: str = "text/html", status=200):
        self.status_code = status
        self.url = url
        self.headers = {"content-type": content_type}
        self.payload = body if isinstance(body, (dict, list)) else None
        self.content = (
            json.dumps(body).encode("utf-8")
            if self.payload is not None
            else str(body).encode("utf-8")
        )
        self.text = self.content.decode("utf-8")

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.responses.pop(0)


def jobvite_target(slug="absolute", name="Absolute Software", **overrides):
    values = dict(
        name=name,
        platform="jobvite",
        slug=slug,
        careers_url=f"https://official.example/{slug}/careers",
        listing_url=f"https://jobs.jobvite.com/{slug}/search",
        endpoint_variant=jobvite.ENDPOINT_VARIANT,
        identity_regex=name.split()[0],
        allowed_apply_hosts=["jobs.jobvite.com"],
        max_pages=1,
        max_jobs=500,
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def jobvite_identity(target: CompanyTarget, *, company=None, tenant=None):
    company = company or target.name
    tenant = tenant or target.slug
    return Response(
        f"<html><body>{company} <a href='https://jobs.jobvite.com/{tenant}/apply'>Jobs</a></body></html>",
        url=str(target.careers_url),
    )


def jobvite_listing(target: CompanyTarget, rows, *, total=None, end=None, range_text=None):
    total = len(rows) if total is None else total
    end = len(rows) if end is None else end
    range_text = range_text or f"Displaying 1 to {end} of {total}"
    rendered = "".join(
        "<tr><td class='jv-job-list-name'>"
        f"<a href='https://jobs.jobvite.com/{target.slug}/job/{job_id}'>{title}</a>"
        "</td><td class='jv-job-list-location'>"
        f"{location}</td></tr>"
        for job_id, title, location in rows
    )
    return Response(
        f"<html><body><div class='jv-pagination-text'>{range_text}</div>"
        f"<table class='jv-job-list'>{rendered}</table></body></html>",
        url=str(target.listing_url),
    )


def jobvite_listing_html(target: CompanyTarget, range_text: str, rows: str):
    return Response(
        f"<html><body><div class='jv-pagination-text'>{range_text}</div>"
        f"<table class='jv-job-list'>{rows}</table></body></html>",
        url=str(target.listing_url),
    )


def optiver_target(**overrides):
    values = dict(
        name="Optiver",
        platform="public_json",
        careers_url="https://www.optiver.com/join-us/jobs/",
        endpoint_url="https://www.optiver.com/en/api/v1/jobs",
        endpoint_variant=public_json.OPTIVER_JOBS,
        identity_regex=r"\bOptiver\b",
        allowed_source_hosts=["www.optiver.com"],
        allowed_apply_hosts=["www.optiver.com"],
        page_size=16,
        max_pages=25,
        max_jobs=400,
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def optiver_identity(target: CompanyTarget, *, company="Optiver", endpoint="/en/api/v1/jobs"):
    return Response(
        f"<html><body>{company} <script>window.state={{\"apiEndpoint\":\"{endpoint}\"}}</script></body></html>",
        url=str(target.careers_url),
    )


def optiver_row(index=1, **overrides):
    values = {
        "componentID": index,
        "title": f"Engineer {index}",
        "location": "Amsterdam, Netherlands",
        "domain": "Technology",
        "href": f"/join-us/jobs/{index}/engineer-{index}/",
    }
    values.update(overrides)
    return values


def optiver_payload(*rows, total=None):
    return {"items": list(rows), "totalCount": len(rows) if total is None else total}


class RecoveryRegistryTests(unittest.TestCase):
    def test_ten_proven_sources_use_exact_reviewed_platforms(self):
        expected = {
            "AMD": ("icims", None),
            "Aptiv": ("workday", None),
            "DRW": ("greenhouse", "drweng"),
            "Optiver": ("public_json", None),
            "Absolute Software": ("jobvite", "absolute"),
            "EPCOR": ("jobvite", "epcor"),
            "PointClickCare": ("lever", "pointclickcare"),
            "Flexport": ("greenhouse", "flexport"),
            "Anysphere": ("ashby", "cursor"),
            "Temporal": ("ashby", "temporal"),
        }
        targets = {
            target.name: target
            for target in load_target_companies(
                str(REGISTRY), apply_reliability_repairs=True
            )
        }
        for name, (platform, slug) in expected.items():
            with self.subTest(company=name):
                self.assertEqual(targets[name].platform, platform)
                self.assertEqual(targets[name].slug, slug)
        self.assertEqual(
            set(targets["AMD"].allowed_apply_hosts),
            {
                "campus-amd.icims.com",
                "campuscanada-amd.icims.com",
                "careers-amd.icims.com",
                "canadacareers-amd.icims.com",
                "global-external-amd.icims.com",
                "globalcampus-amd.icims.com",
            },
        )

    def test_each_recovered_registry_entry_has_an_active_reviewed_contract(self):
        recovered = {
            "AMD", "Aptiv", "DRW", "Optiver", "Absolute Software", "EPCOR",
            "PointClickCare", "Flexport", "Anysphere", "Temporal",
        }
        entries = {
            str(entry["name"]): entry
            for entry in load_registry_entries(REGISTRY)
            if str(entry.get("name")) in recovered
        }
        repair_entries = (
            yaml.safe_load(REPAIRS.read_text(encoding="utf-8")) or {}
        ).get("company_careers_reliability_repairs", [])
        entries.update(
            {
                str(entry["name"]): entry
                for entry in repair_entries
                if str(entry.get("name")) in recovered
            }
        )
        self.assertEqual(set(entries), recovered)
        for name, entry in entries.items():
            with self.subTest(company=name):
                assessment = assess_registry_entry(entry)
                self.assertEqual(assessment.action, IntegrityAction.OK)
                self.assertEqual(assessment.activation_state.value, "active")


class JobviteRecoveryTests(unittest.TestCase):
    def test_absolute_and_epcor_complete_counted_collections(self):
        for target in (
            jobvite_target(),
            jobvite_target("epcor", "EPCOR", identity_regex=r"\bEPCOR\b"),
        ):
            with self.subTest(company=target.name):
                rows = [("one", "Engineer", "Calgary, AB"), ("two", "Analyst", "Remote")]
                session = Session([jobvite_identity(target), jobvite_listing(target, rows)])
                jobs = jobvite.fetch_jobs(session, target)
                metadata = source_scan_metadata(session)
                self.assertEqual([job.job_id for job in jobs], [f"jobvite:{target.slug}:one", f"jobvite:{target.slug}:two"])
                self.assertTrue(metadata["scan_complete"])
                self.assertEqual(metadata["total_available"], 2)
                self.assertEqual(metadata["source_identity_status"], "validated")

    def test_wrong_company_or_wrong_tenant_identity_fails_closed(self):
        target = jobvite_target()
        for identity in (
            jobvite_identity(target, company="Unrelated Company"),
            jobvite_identity(target, tenant="other"),
        ):
            with self.subTest(identity=identity.text):
                with self.assertRaises(AdapterError):
                    jobvite.fetch_jobs(Session([identity]), target)

    def test_official_identity_cross_domain_redirect_fails_closed(self):
        target = jobvite_target()
        identity = jobvite_identity(target)
        identity.url = "https://evil.example/absolute/careers"
        with self.assertRaises(AdapterError):
            jobvite.fetch_jobs(Session([identity]), target)

    def test_identity_url_authority_tricks_fail_closed(self):
        target = jobvite_target()
        identities = [
            jobvite_identity(target),
            Response(
                "Absolute Software "
                "<a href='https://jobs.jobvite.com@evil.example/absolute/apply'>Jobs</a>",
                url=str(target.careers_url),
            ),
        ]
        identities[0].url = "https://attacker@official.example/absolute/careers"
        for identity in identities:
            with self.subTest(url=identity.url, body=identity.text):
                with self.assertRaises(AdapterError):
                    jobvite.fetch_jobs(Session([identity]), target)

    def test_cross_tenant_and_malformed_job_title_anchors_fail_closed(self):
        target = jobvite_target()
        valid = (
            "<tr><td class='jv-job-list-name'>"
            "<a href='/absolute/job/one'>Engineer</a></td>"
            "<td class='jv-job-list-location'>Calgary</td></tr>"
        )
        invalid_rows = [
            (
                "<tr><td class='jv-job-list-name'>"
                "<a href='/epcor/job/two'>Analyst</a></td>"
                "<td class='jv-job-list-location'>Edmonton</td></tr>"
            ),
            (
                "<tr><td class='jv-job-list-name'>"
                "<a href='/absolute/jobs/two'>Analyst</a></td>"
                "<td class='jv-job-list-location'>Edmonton</td></tr>"
            ),
        ]
        for invalid in invalid_rows:
            with self.subTest(row=invalid):
                listing = jobvite_listing_html(target, "1-2 of 2", valid + invalid)
                with self.assertRaises(AdapterError):
                    jobvite.fetch_jobs(
                        Session([jobvite_identity(target), listing]), target
                    )

    def test_result_range_contract_is_strict(self):
        target = jobvite_target()
        rows = [("one", "Engineer", "Calgary"), ("two", "Analyst", "Remote")]

        valid = jobvite_listing(target, rows, range_text="1 to 2 of 2")
        jobs = jobvite.fetch_jobs(
            Session([jobvite_identity(target), valid]), target
        )
        self.assertEqual(len(jobs), 2)

        conflicting = jobvite_listing(target, rows, range_text="1-2 of 2")
        conflicting.text = conflicting.text.replace(
            "<table", "<div class='jv-pagination-text'>1-3 of 3</div><table"
        )
        conflicting.content = conflicting.text.encode("utf-8")
        cases = [
            conflicting,
            jobvite_listing(target, rows, total=3, end=2),
            jobvite_listing(target, [], range_text="1-1 of 0"),
        ]
        for listing in cases:
            with self.subTest(listing=listing.text):
                with self.assertRaises(AdapterError):
                    jobvite.fetch_jobs(
                        Session([jobvite_identity(target), listing]), target
                    )

        zero = jobvite_listing(target, [], range_text="0-0 of 0")
        session = Session([jobvite_identity(target), zero])
        self.assertEqual(jobvite.fetch_jobs(session, target), [])
        self.assertEqual(source_scan_metadata(session)["stop_reason"], "validated_zero_jobs")

    def test_malformed_duplicate_truncated_and_wrong_host_rows_fail_closed(self):
        target = jobvite_target()
        cases = [
            jobvite_listing(target, [("one", "Engineer", "")]),
            jobvite_listing(target, [("one", "Engineer", "Calgary"), ("one", "Engineer", "Calgary")]),
            jobvite_listing(target, [("one", "Engineer", "Calgary"), ("one", "Different", "Toronto")]),
            jobvite_listing(target, [("one", "Engineer", "Calgary")], total=2, end=2),
            jobvite_listing(target, [("one", "Engineer", "Calgary")], total=2, end=1),
            Response(
                "<div class='jv-pagination-text'>1 to 1 of 1</div>"
                "<table class='jv-job-list'><tr>"
                "<td class='jv-job-list-name'><a href='https://evil.example/absolute/job/one'>Engineer</a></td>"
                "<td class='jv-job-list-location'>Calgary</td></tr></table>",
                url=str(target.listing_url),
            ),
        ]
        for listing in cases:
            with self.subTest(listing=listing.text):
                with self.assertRaises(AdapterError):
                    jobvite.fetch_jobs(Session([jobvite_identity(target), listing]), target)

    def test_wrong_source_redirect_and_response_truncation_fail_closed(self):
        target = jobvite_target()
        wrong_source = jobvite_listing(target, [("one", "Engineer", "Calgary")])
        wrong_source.url = "https://evil.example/absolute/search"
        with self.assertRaises(AdapterError):
            jobvite.fetch_jobs(
                Session([jobvite_identity(target), wrong_source]), target
            )

        bounded = jobvite_target(max_response_bytes=300)
        oversized = jobvite_listing(bounded, [("one", "Engineer", "Calgary")])
        oversized.content += b"x" * 500
        with self.assertRaises(AdapterError):
            jobvite.fetch_jobs(
                Session([jobvite_identity(bounded), oversized]), bounded
            )


class OptiverRecoveryTests(unittest.TestCase):
    def test_counted_same_origin_collection_is_complete(self):
        target = optiver_target()
        session = Session([
            optiver_identity(target),
            Response(
                optiver_payload(optiver_row(1), optiver_row(2)),
                url=str(target.endpoint_url),
                content_type="application/json",
            ),
        ])
        jobs = public_json.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual([job.job_id for job in jobs], ["public_json:www.optiver.com:1", "public_json:www.optiver.com:2"])
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["total_available"], 2)
        self.assertEqual(session.calls[1][1]["params"], {"from": "0", "size": "16"})

    def test_wrong_company_or_endpoint_identity_fails_closed(self):
        target = optiver_target()
        for identity in (
            optiver_identity(target, company="Unrelated Company"),
            optiver_identity(target, endpoint="/api/other"),
        ):
            with self.subTest(identity=identity.text):
                with self.assertRaises(AdapterError):
                    public_json.fetch_jobs(Session([identity]), target)

    def test_malformed_duplicate_total_mismatch_and_wrong_host_fail_closed(self):
        target = optiver_target()
        payloads = [
            optiver_payload(optiver_row(1, componentID=None)),
            optiver_payload(optiver_row(1), optiver_row(1, title="Different")),
            optiver_payload(optiver_row(1), total=2),
            optiver_payload(optiver_row(1, href="https://evil.example/join-us/jobs/1/x/")),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                session = Session([
                    optiver_identity(target),
                    Response(payload, url=str(target.endpoint_url), content_type="application/json"),
                ])
                with self.assertRaises(AdapterError):
                    public_json.fetch_jobs(session, target)

    def test_wrong_api_redirect_and_response_truncation_fail_closed(self):
        target = optiver_target()
        wrong = Response(
            optiver_payload(optiver_row(1)),
            url="https://evil.example/en/api/v1/jobs",
            content_type="application/json",
        )
        with self.assertRaises(AdapterError):
            public_json.fetch_jobs(Session([optiver_identity(target), wrong]), target)

        bounded = optiver_target(max_response_bytes=300)
        payload = optiver_payload(optiver_row(1, padding="x" * 500))
        with self.assertRaises(AdapterError):
            public_json.fetch_jobs(
                Session([
                    optiver_identity(bounded),
                    Response(payload, url=str(bounded.endpoint_url), content_type="application/json"),
                ]),
                bounded,
            )


def ashby_target(slug="cursor", name="Anysphere", **overrides):
    values = dict(
        name=name,
        platform="ashby",
        slug=slug,
        endpoint_url=f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        endpoint_variant=ashby.POSTING_API_VARIANT,
        allowed_source_hosts=["api.ashbyhq.com"],
        allowed_apply_hosts=["jobs.ashbyhq.com"],
        max_jobs=10_000,
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def ashby_row(slug, index=1, **overrides):
    job_id = f"00000000-0000-4000-8000-{index:012d}"
    values = {
        "id": job_id,
        "title": f"Engineer {index}",
        "department": "Engineering",
        "location": "San Francisco",
        "secondaryLocations": [],
        "publishedAt": "2026-09-29T00:00:00Z",
        "isListed": True,
        "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/{slug}/{job_id}/application",
    }
    values.update(overrides)
    return values


class AshbyPostingApiRecoveryTests(unittest.TestCase):
    def test_cursor_and_temporal_atomic_api_collections(self):
        for target in (ashby_target(), ashby_target("temporal", "Temporal")):
            with self.subTest(company=target.name):
                payload = {"apiVersion": "1", "jobs": [ashby_row(target.slug)]}
                session = Session([
                    Response(payload, url=str(target.endpoint_url), content_type="application/json")
                ])
                jobs = ashby.fetch_jobs(session, target)
                metadata = source_scan_metadata(session)
                self.assertEqual(len(jobs), 1)
                self.assertTrue(metadata["scan_complete"])
                self.assertEqual(metadata["source_identity_status"], "validated")

    def test_malformed_duplicate_conflicting_wrong_host_and_truncated_api_fail(self):
        target = ashby_target()
        good = ashby_row(target.slug)
        cases = [
            ({"apiVersion": "1", "jobs": [{**good, "id": ""}]}, target),
            ({"apiVersion": "1", "jobs": [good, dict(good)]}, target),
            ({"apiVersion": "1", "jobs": [good, {**good, "title": "Different"}]}, target),
            ({"apiVersion": "1", "jobs": [{**good, "applyUrl": good["applyUrl"].replace("jobs.ashbyhq.com", "evil.example")}]}, target),
            ({"apiVersion": "2", "jobs": [good]}, target),
            ({"apiVersion": "1", "jobs": [good]}, ashby_target(max_response_bytes=10)),
        ]
        for payload, configured in cases:
            with self.subTest(payload=payload, bound=configured.max_response_bytes):
                session = Session([
                    Response(payload, url=str(configured.endpoint_url), content_type="application/json")
                ])
                with self.assertRaises(AdapterError):
                    ashby.fetch_jobs(session, configured)


def greenhouse_target(slug, name, **overrides):
    values = dict(
        name=name,
        platform="greenhouse",
        slug=slug,
        endpoint_variant=greenhouse.STRICT_BOARD_VARIANT,
        allowed_apply_hosts=["job-boards.greenhouse.io"],
        max_jobs=10_000,
        max_response_bytes=3_000_000,
    )
    values.update(overrides)
    return CompanyTarget(**values)


def greenhouse_row(slug, index=1, **overrides):
    values = {
        "id": index,
        "title": f"Engineer {index}",
        "location": {"name": "Chicago"},
        "absolute_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{index}",
        "updated_at": "2026-09-29T00:00:00Z",
    }
    values.update(overrides)
    return values


class GreenhouseStrictRecoveryTests(unittest.TestCase):
    def test_drw_and_flexport_exact_board_collections(self):
        for target in (
            greenhouse_target("drweng", "DRW"),
            greenhouse_target("flexport", "Flexport"),
        ):
            with self.subTest(company=target.name):
                session = Session([
                    Response({"jobs": [greenhouse_row(target.slug)]}, url=f"https://boards-api.greenhouse.io/v1/boards/{target.slug}/jobs", content_type="application/json")
                ])
                jobs = greenhouse.fetch_jobs(session, target)
                metadata = source_scan_metadata(session)
                self.assertEqual(len(jobs), 1)
                self.assertTrue(metadata["scan_complete"])
                self.assertEqual(metadata["source_identity_status"], "validated")

    def test_malformed_duplicate_conflicting_and_wrong_host_rows_fail(self):
        target = greenhouse_target("drweng", "DRW")
        good = greenhouse_row(target.slug)
        payloads = [
            {"jobs": [{**good, "id": ""}]},
            {"jobs": [good, dict(good)]},
            {"jobs": [good, {**good, "title": "Different"}]},
            {"jobs": [{**good, "absolute_url": "https://evil.example/drweng/jobs/1"}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                session = Session([
                    Response(payload, url="https://boards-api.greenhouse.io/v1/boards/drweng/jobs", content_type="application/json")
                ])
                with self.assertRaises(AdapterError):
                    greenhouse.fetch_jobs(session, target)

    def test_response_truncation_fails_closed(self):
        target = greenhouse_target("drweng", "DRW", max_response_bytes=100)
        session = Session([
            Response({"jobs": [greenhouse_row("drweng", padding="x" * 500)]}, url="https://boards-api.greenhouse.io/v1/boards/drweng/jobs", content_type="application/json")
        ])
        with self.assertRaises(AdapterError):
            greenhouse.fetch_jobs(session, target)


class LeverOfficialLinkRecoveryTests(unittest.TestCase):
    def target(self, **overrides):
        values = dict(
            name="PointClickCare",
            platform="lever",
            slug="pointclickcare",
            careers_url="https://pointclickcare.com/life-at-pointclickcare/",
            endpoint_url="https://api.lever.co/v0/postings/pointclickcare",
            endpoint_variant=lever.OFFICIAL_LINK_VARIANT,
            identity_regex=r"\bPointClickCare\b",
            allowed_source_hosts=["pointclickcare.com"],
            allowed_apply_hosts=["jobs.lever.co"],
            max_pages=25,
            max_jobs=500,
            max_response_bytes=3_000_000,
        )
        values.update(overrides)
        return CompanyTarget(**values)

    def test_official_page_binds_exact_lever_tenant(self):
        target = self.target()
        identity = Response(
            "<html><body>PointClickCare <a href='https://jobs.lever.co/pointclickcare'>Jobs</a></body></html>",
            url=str(target.careers_url),
        )
        row = {
            "id": "00000000-0000-4000-8000-000000000001",
            "text": "Engineer",
            "hostedUrl": "https://jobs.lever.co/pointclickcare/00000000-0000-4000-8000-000000000001",
            "categories": {"location": "Toronto", "department": "Engineering"},
        }
        session = Session([
            identity,
            Response([row], url=str(target.endpoint_url), content_type="application/json"),
        ])
        jobs = lever.fetch_jobs(session, target)
        metadata = source_scan_metadata(session)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(metadata["scan_complete"])
        self.assertEqual(metadata["source_identity_status"], "validated")

    def test_wrong_company_and_wrong_tenant_official_pages_fail(self):
        target = self.target()
        pages = [
            "<html>Unrelated <a href='https://jobs.lever.co/pointclickcare'>Jobs</a></html>",
            "<html>PointClickCare <a href='https://jobs.lever.co/other'>Jobs</a></html>",
        ]
        for page in pages:
            with self.subTest(page=page):
                with self.assertRaises(AdapterError):
                    lever.fetch_jobs(Session([Response(page, url=str(target.careers_url))]), target)


if __name__ == "__main__":
    unittest.main()
