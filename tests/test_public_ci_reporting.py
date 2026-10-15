import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.public_ci_report import (
    PUBLIC_REPORT_PATH_VARIABLE,
    PublicCIReporter,
    classify_scan_reason,
    read_public_event_line,
)
from src.scraper.company_careers.models import CompanyScanResult, CompanyTarget, CoverageStatus
from src.scraper.company_careers.scanner import CompanyCareersScanner


ROOT = Path(__file__).resolve().parents[1]


def _result(
    name: str,
    *,
    state: str,
    fetched: int,
    retained: int,
    health: str,
    reason: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        target=SimpleNamespace(name=name, source_scope="default"),
        scan_health=state,
        fetched_count=fetched,
        jobs_collected=fetched,
        matched_count=retained,
        source_health_code=health,
        scan_completeness_reason=reason,
        stop_reason=reason,
        error_type=None,
    )


class PublicEventContractTests(unittest.TestCase):
    def test_company_results_use_authoritative_states_counts_and_safe_reasons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.jsonl"
            path.touch()
            reporter = PublicCIReporter(path)
            reporter.company_result(
                _result(
                    "Complete Co",
                    state="complete",
                    fetched=7,
                    retained=3,
                    health="healthy",
                    reason="reported_total_reached",
                )
            )
            reporter.company_result(
                _result(
                    "Capped Co",
                    state="incomplete",
                    fetched=20,
                    retained=4,
                    health="safety_limit_reached",
                    reason="max_pages_safety_limit_reached",
                )
            )
            reporter.company_result(
                _result(
                    "Broken Co",
                    state="failed",
                    fetched=2,
                    retained=0,
                    health="malformed_collection",
                    reason="structured_payload_malformed",
                )
            )
            events = [
                read_public_event_line(line)
                for line in path.read_bytes().splitlines(keepends=True)
            ]

        self.assertEqual([event["status"] for event in events], ["complete", "incomplete", "failed"])
        self.assertEqual([event["jobs_fetched"] for event in events], [7, 20, 2])
        self.assertEqual([event["jobs_retained"] for event in events], [3, 4, 0])
        self.assertEqual(
            [event["reason"] for event in events],
            ["complete", "pagination_safety_limit", "invalid_response"],
        )

    def test_meaningful_reason_categories_do_not_publish_raw_error_text(self):
        secret = "token=super-secret-value"
        result = _result(
            "Safe Co",
            state="failed",
            fetched=0,
            retained=0,
            health="source_unreachable",
            reason="timeout",
        )
        result.error = secret
        result.remediation = secret
        self.assertEqual(classify_scan_reason(result), "timeout")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.jsonl"
            path.touch()
            PublicCIReporter(path).company_result(result)
            public_bytes = path.read_text(encoding="utf-8")
        self.assertNotIn(secret, public_bytes)

    def test_control_characters_and_unknown_fields_are_rejected(self):
        malicious = {
            "v": 1,
            "type": "company",
            "name": "Bad\n::warning::injected",
            "status": "failed",
            "jobs_fetched": 0,
            "jobs_retained": 0,
            "reason": "scan_failure_other",
        }
        line = (json.dumps(malicious) + "\n").encode("utf-8")
        self.assertIsNone(read_public_event_line(line))
        malicious["name"] = "Safe"
        malicious["raw_exception"] = "secret"
        line = (json.dumps(malicious) + "\n").encode("utf-8")
        self.assertIsNone(read_public_event_line(line))

    def test_scanner_observer_sees_completed_results_without_changing_report(self):
        targets = [
            CompanyTarget(name="One", platform="greenhouse", slug="one"),
            CompanyTarget(name="Two", platform="greenhouse", slug="two"),
        ]
        observed = []
        scanner = CompanyCareersScanner(
            targets=targets,
            max_workers=1,
            parent_source_mappings=[],
            result_observer=observed.append,
        )
        results = [
            CompanyScanResult(
                target=target,
                status=CoverageStatus.FAILED_TECHNICAL,
                error_type="timeout",
            )
            for target in targets
        ]
        with patch.object(scanner, "_scan_target", side_effect=results):
            report = scanner.scan()

        self.assertEqual(report.results, results)
        self.assertEqual(observed, results)
        self.assertTrue(all(not result.authoritative_for_absence for result in results))


class PublicBoundaryRenderingTests(unittest.TestCase):
    def _run_boundary(self, child_code: str, temp_dir: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["RUNNER_TEMP"] = temp_dir
        environment["GITHUB_STEP_SUMMARY"] = str(Path(temp_dir) / "summary.md")
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_public_ci_step.py"),
                "--stage",
                "dashboard-update",
                "--",
                sys.executable,
                "-c",
                child_code,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_mixed_results_aggregates_and_stage_counts_are_public(self):
        child_code = r'''
from scripts.public_ci_report import PublicCIReporter
r = PublicCIReporter.from_environment()
r.run_start(3)
r.emit("stage", stage="company_scanning", status="started", failure_category=None, duration_seconds=None)
for values in (
    dict(name="Complete Co", status="complete", jobs_fetched=8, jobs_retained=3, reason="complete"),
    dict(name="Incomplete Co", status="incomplete", jobs_fetched=5, jobs_retained=2, reason="missing_terminal_evidence"),
    dict(name="Failed Co", status="failed", jobs_fetched=0, jobs_retained=0, reason="timeout"),
):
    r.emit("company", **values)
r.emit("stage", stage="company_scanning", status="succeeded", failure_category=None, duration_seconds=9.5)
r.emit("scan_summary", configured_companies=3, attempted_companies=3, complete_companies=1, incomplete_companies=1, failed_companies=1, jobs_fetched=13, normalized_jobs=10, retained_jobs=5, coverage_health="degraded", scan_runtime_seconds=9.5)
r.emit("stage", stage="job_ranking", status="succeeded", failure_category=None, duration_seconds=0.2)
r.emit("ranking_summary", final_ranked_jobs=4)
r.emit("stage", stage="supabase_synchronization", status="succeeded", failure_category=None, duration_seconds=0.4)
r.emit("sync_summary", inserted=1, updated=2, unchanged=1, missing_incremented=0, closed=0)
r.emit("stage", stage="google_sheets_publication", status="succeeded", failure_category=None, duration_seconds=0.3)
r.emit("sheets_summary", new_jobs=1, active_jobs=4, companies=3)
r.emit("run_summary", runtime_seconds=10.5)
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(child_code, temp_dir)
            summary = (Path(temp_dir) / "summary.md").read_text(encoding="utf-8")
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, public_log)
        self.assertIn("Company Complete Co: status=complete", public_log)
        self.assertIn("Company Incomplete Co: status=incomplete", public_log)
        self.assertIn("Company Failed Co: status=failed", public_log)
        self.assertIn("configured=3, attempted=3, complete=1, incomplete=1, failed=1", public_log)
        self.assertIn("fetched=13, normalized=10, retained=5", public_log)
        self.assertIn("inserted=1, updated=2, unchanged=1", public_log)
        self.assertIn("Final ranked jobs: 4", public_log)
        self.assertIn("Company scan results", summary)

    def test_partial_progress_never_classifies_unattempted_companies(self):
        child_code = r'''
from scripts.public_ci_report import PublicCIReporter
r = PublicCIReporter.from_environment()
r.run_start(4)
r.emit("company", name="First", status="complete", jobs_fetched=2, jobs_retained=1, reason="complete")
r.emit("company", name="Second", status="failed", jobs_fetched=0, jobs_retained=0, reason="network_failure")
r.emit("stage", stage="company_scanning", status="failed", failure_category="internal_failure", duration_seconds=1.0)
raise SystemExit(17)
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(child_code, temp_dir)
            summary = (Path(temp_dir) / "summary.md").read_text(encoding="utf-8")
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 17, public_log)
        self.assertIn("attempted=2/4", public_log)
        self.assertIn("unattempted companies were not classified", public_log)
        self.assertNotIn("attempted=4", public_log)
        self.assertIn("Partial companies attempted: 2/4", summary)

    def test_workflow_commands_and_markdown_are_rendered_as_data(self):
        child_code = r'''
from scripts.public_ci_report import PublicCIReporter
r = PublicCIReporter.from_environment()
r.run_start(1)
r.emit("company", name="::warning:: Pipe | **bold** <script>", status="failed", jobs_fetched=0, jobs_retained=0, reason="scan_failure_other")
raise SystemExit(5)
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(child_code, temp_dir)
            summary = (Path(temp_dir) / "summary.md").read_text(encoding="utf-8")
        public_lines = (result.stdout + result.stderr).splitlines()
        self.assertEqual(result.returncode, 5)
        self.assertFalse(any(line.startswith("::") for line in public_lines))
        self.assertNotIn("<script>", summary)
        self.assertIn("\\|", summary)
        self.assertIn("\\*\\*bold\\*\\*", summary)

    def test_secret_exception_and_malformed_event_never_reach_public_output(self):
        secret = "PRIVATE_TOKEN=do-not-publish"
        child_code = f'''
import json
import os
from pathlib import Path
from scripts.public_ci_report import PUBLIC_REPORT_PATH_VARIABLE, PublicCIReporter
r = PublicCIReporter.from_environment()
try:
    raise RuntimeError({secret!r})
except RuntimeError as exc:
    with r.stage("supabase_synchronization"):
        raise exc
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(child_code, temp_dir)
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, public_log)
        self.assertIn("Supabase synchronization: failed: internal failure", public_log)
        self.assertNotIn(secret, public_log)

        malicious_code = f'''
import json
import os
from pathlib import Path
path = Path(os.environ[{PUBLIC_REPORT_PATH_VARIABLE!r}])
with path.open("a", encoding="utf-8") as report:
    report.write(json.dumps({{"v": 1, "type": "company", "name": "Bad\\n{secret}", "status": "failed", "jobs_fetched": 0, "jobs_retained": 0, "reason": "scan_failure_other"}}) + "\\n")
raise SystemExit(9)
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(malicious_code, temp_dir)
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 9, public_log)
        self.assertIn("malformed public reporting event", public_log)
        self.assertNotIn(secret, public_log)


if __name__ == "__main__":
    unittest.main()
