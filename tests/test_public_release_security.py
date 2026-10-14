import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main_api


ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-api-key-with-sufficient-entropy"
ADMIN_KEY = "test-admin-key-with-sufficient-entropy"


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main_api.app)

    def test_public_health_does_not_require_authentication(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_sensitive_routes_fail_closed_without_configuration(self):
        with patch.object(main_api, "API_KEY", ""):
            for path in ("/search/history", "/jobs", "/jobs/stats"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503)

    def test_sensitive_routes_reject_missing_or_wrong_credentials(self):
        with patch.object(main_api, "API_KEY", API_KEY):
            self.assertEqual(self.client.get("/search/history").status_code, 401)
            response = self.client.get(
                "/search/history", headers={"X-API-Key": "not-the-key"}
            )
            self.assertEqual(response.status_code, 401)

    def test_sensitive_route_accepts_configured_credential(self):
        with patch.object(main_api, "API_KEY", API_KEY):
            response = self.client.get(
                "/search/history", headers={"X-API-Key": API_KEY}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("searches", response.json())

    def test_operational_output_is_not_publicly_mounted(self):
        response = self.client.get("/output/search_history.json")
        self.assertEqual(response.status_code, 404)

    def test_result_identifier_is_validated_before_file_access(self):
        with patch.object(main_api, "API_KEY", API_KEY):
            response = self.client.get(
                "/search/not-a-generated-id", headers={"X-API-Key": API_KEY}
            )
        self.assertEqual(response.status_code, 400)

    def test_migration_requires_api_and_admin_auth_and_explicit_enablement(self):
        with (
            patch.object(main_api, "API_KEY", API_KEY),
            patch.object(main_api, "ADMIN_API_KEY", ""),
        ):
            response = self.client.post(
                "/jobs/migrate", headers={"X-API-Key": API_KEY}
            )
            self.assertEqual(response.status_code, 503)

        with (
            patch.object(main_api, "API_KEY", API_KEY),
            patch.object(main_api, "ADMIN_API_KEY", ADMIN_KEY),
            patch.object(main_api, "API_MIGRATIONS_ENABLED", False),
        ):
            response = self.client.post(
                "/jobs/migrate",
                headers={"X-API-Key": API_KEY, "X-Admin-Key": ADMIN_KEY},
            )
            self.assertEqual(response.status_code, 403)

    def test_websocket_rejects_unauthenticated_connections_before_accept(self):
        with patch.object(main_api, "API_KEY", API_KEY):
            with self.assertRaises(WebSocketDisconnect) as context:
                with self.client.websocket_connect("/ws"):
                    pass
        self.assertEqual(context.exception.code, 1008)

    def test_websocket_accepts_header_authentication(self):
        with patch.object(main_api, "API_KEY", API_KEY):
            with self.client.websocket_connect(
                "/ws", headers={"X-API-Key": API_KEY}
            ) as websocket:
                websocket.send_json({"action": "unknown", "data": {}})
                response = websocket.receive_json()
        self.assertEqual(response["type"], "error")

    def test_cors_requires_explicit_http_origins(self):
        with patch.dict(
            os.environ,
            {"ALLOWED_ORIGINS": "https://ping.example,http://localhost:3000/"},
            clear=False,
        ):
            self.assertEqual(
                main_api._trusted_cors_origins(),
                ["https://ping.example", "http://localhost:3000"],
            )

        with patch.dict(os.environ, {"ALLOWED_ORIGINS": "*"}, clear=False):
            with self.assertRaises(RuntimeError):
                main_api._trusted_cors_origins()


class OptionalCredentialImportTests(unittest.TestCase):
    def test_agent_package_imports_without_tavily_credentials(self):
        environment = os.environ.copy()
        environment["TAVILY_API_KEY"] = ""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import src.agents.search_agents_adk as module; "
                    "assert module.tavily_search_agent is None"
                ),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class PublicRepositoryDocumentationTests(unittest.TestCase):
    def test_quick_start_clones_the_permanent_public_repository(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/Mahmood-Elahi/Job-Radar.git", readme)
        self.assertNotIn("git clone git@github.com:swanidhi/role-radar.git", readme)


class DockerReleaseContextTests(unittest.TestCase):
    def test_sensitive_context_classes_are_explicitly_excluded(self):
        rules = {
            line.strip()
            for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        required = {
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "*credential*.json",
            "*service-account*.json",
            "secrets/",
            "evidence/",
            "archive/",
            "archives/",
            "*.zip",
            "*.log",
            "logs/",
            "output/",
            "jobs/",
            "*.db",
            "*.sqlite",
            "*.sqlite3",
            "config/*",
            "!config/target_companies.yaml",
            "config/job_radar_run_history.json",
            "config/company_unknown_investigation_v3.csv",
            "config/company_uncovered_fingerprints_v4.csv",
            "discovery*.txt",
            "identity*_audit.txt",
            "identity*_history.txt",
        }
        self.assertFalse(required - rules)

    def test_dockerfile_does_not_copy_the_whole_repository(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY . .", dockerfile)
        self.assertNotIn("COPY config ./config", dockerfile)
        self.assertIn("COPY src ./src", dockerfile)
        self.assertIn("COPY config/target_companies.yaml ./config/", dockerfile)

    def test_historical_key_bearing_csvs_are_not_runtime_image_inputs(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("company_unknown_investigation_v3.csv", dockerfile)
        self.assertNotIn("company_uncovered_fingerprints_v4.csv", dockerfile)

    def test_production_workflow_does_not_depend_on_docker(self):
        workflow = (ROOT / ".github" / "workflows" / "hourly-job-radar.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(workflow.lower(), r"\bdocker\b|\bcontainer\s*:")

    def test_documentation_marks_docker_optional_and_unverified(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        for document in (readme, operations):
            with self.subTest(document=document[:20]):
                self.assertIn("Docker is optional", document)
                self.assertIn("actual image build has not been verified", document)


class WorkflowSecurityTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/hourly-job-radar.yml").read_text(
            encoding="utf-8"
        )

    def test_workflow_has_no_untrusted_pull_request_trigger(self):
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("pull_request_target:", self.workflow)
        self.assertIn("permissions:\n  contents: read", self.workflow)

    def test_checkout_does_not_persist_its_token(self):
        self.assertIn("persist-credentials: false", self.workflow)

    def test_actions_are_pinned_to_full_commit_shas(self):
        uses = re.findall(r"^\s*uses:\s+([^\s#]+)", self.workflow, flags=re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_production_secrets_are_not_job_wide_or_present_during_install(self):
        install_index = self.workflow.index("Install Python dependencies")
        first_secret_index = self.workflow.index("secrets.SUPABASE_URL")
        self.assertLess(install_index, first_secret_index)
        job_header = self.workflow[
            self.workflow.index("update-student-dashboard:") : install_index
        ]
        self.assertNotIn("SUPABASE_SERVICE_ROLE_KEY", job_header)
        self.assertIn("secrets.GOOGLE_SHEET_ID", self.workflow)
        self.assertNotIn("vars.GOOGLE_SHEET_ID", self.workflow)

    def test_public_workflow_does_not_publish_operational_artifacts(self):
        self.assertNotIn("actions/upload-artifact", self.workflow)
        self.assertNotIn("job-radar-diagnostics", self.workflow)

    def test_operational_steps_use_the_public_logging_boundary(self):
        self.assertIn("--stage production-preflight", self.workflow)
        self.assertIn("--stage dashboard-update", self.workflow)
        self.assertIn("scripts/run_public_ci_step.py", self.workflow)
        self.assertNotIn(
            "run: python scripts/update_student_dashboard.py", self.workflow
        )

    def test_all_four_production_values_come_from_github_secrets(self):
        for name in (
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_SHEET_ID",
        ):
            with self.subTest(name=name):
                self.assertIn(f"${{{{ secrets.{name} }}}}", self.workflow)
        self.assertNotIn("vars.GOOGLE_SHEET_ID", self.workflow)

    def test_current_tree_secret_scan_runs_before_dependencies(self):
        scan_index = self.workflow.index("Scan the checked-out tree for credentials")
        install_index = self.workflow.index("Install Python dependencies")
        self.assertLess(scan_index, install_index)
        self.assertIn("scripts/check_public_release_secrets.py", self.workflow)


class PublicCILoggingBoundaryTests(unittest.TestCase):
    def _run_boundary(
        self,
        temp_dir: str,
        child_code: str,
        *,
        stage: str = "dashboard-update",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["RUNNER_TEMP"] = temp_dir
        for variable in (
            "GITHUB_ENV",
            "GITHUB_OUTPUT",
            "GITHUB_PATH",
            "GITHUB_STEP_SUMMARY",
        ):
            environment[variable] = str(Path(temp_dir) / f"{variable}.txt")
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_public_ci_step.py"),
                "--stage",
                stage,
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

    def test_sensitive_stdout_stderr_subprocesses_and_exceptions_are_private(self):
        sensitive_values = (
            "PRIVATE_COMPANY_INVENTORY=Acme:42",
            "PRIVATE_RANKING=student@example.invalid",
            "PRIVATE_JOB_RECORD=requisition-123",
            "PRIVATE_SOURCE_FAILURE=provider payload",
            "SUPABASE_URL=https://private.invalid",
            "GOOGLE_SHEET_ID=private-sheet",
            "PRIVATE_TOKEN=secret-value",
            "PRIVATE_PATH=/private/operator/path",
            "NESTED_PRIVATE_DIAGNOSTIC",
            "RAW_PRIVATE_EXCEPTION",
        )
        child_code = """
import os
from pathlib import Path
import subprocess
import sys
import traceback

values = (
    "PRIVATE_COMPANY_INVENTORY=Acme:42",
    "PRIVATE_RANKING=student@example.invalid",
    "PRIVATE_JOB_RECORD=requisition-123",
    "PRIVATE_SOURCE_FAILURE=provider payload",
    "SUPABASE_URL=https://private.invalid",
    "GOOGLE_SHEET_ID=private-sheet",
    "PRIVATE_TOKEN=secret-value",
    "PRIVATE_PATH=/private/operator/path",
)
for index, value in enumerate(values):
    print(value, file=sys.stdout if index % 2 == 0 else sys.stderr)
subprocess.run(
    [sys.executable, "-c", "import sys; print('NESTED_PRIVATE_DIAGNOSTIC', file=sys.stderr)"],
    check=True,
)
for variable in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "GITHUB_STEP_SUMMARY"):
    path = os.environ.get(variable)
    if path:
        Path(path).write_text("PRIVATE_GITHUB_COMMAND_FILE_LEAK", encoding="utf-8")
try:
    raise RuntimeError("RAW_PRIVATE_EXCEPTION")
except RuntimeError:
    traceback.print_exc()
sys.exit(23)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(temp_dir, child_code)
            public_log = result.stdout + result.stderr
            self.assertEqual(result.returncode, 23, public_log)
            self.assertIn("dashboard update started", public_log)
            self.assertIn("dashboard update failed (exit code 23)", public_log)
            for value in sensitive_values:
                with self.subTest(value=value):
                    self.assertNotIn(value, public_log)

            diagnostics = (
                Path(temp_dir) / "ping-private-ci" / "dashboard-update.log"
            ).read_text(encoding="utf-8")
            for value in sensitive_values:
                with self.subTest(diagnostic=value):
                    self.assertIn(value, diagnostics)

            for variable in (
                "GITHUB_ENV",
                "GITHUB_OUTPUT",
                "GITHUB_PATH",
                "GITHUB_STEP_SUMMARY",
            ):
                self.assertFalse((Path(temp_dir) / f"{variable}.txt").exists())

    def test_success_output_is_generic_and_exit_zero_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._run_boundary(
                temp_dir,
                "print('PRIVATE_SUCCESS_PAYLOAD')",
                stage="production-preflight",
            )
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, public_log)
        self.assertIn("production preflight started", public_log)
        self.assertIn("production preflight completed successfully", public_log)
        self.assertNotIn("PRIVATE_SUCCESS_PAYLOAD", public_log)

    def test_spawn_exception_does_not_echo_command_or_exception_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment["RUNNER_TEMP"] = temp_dir
            private_command = "private-token-command-that-does-not-exist"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "run_public_ci_step.py"),
                    "--stage",
                    "dashboard-update",
                    "--",
                    private_command,
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
        public_log = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, public_log)
        self.assertIn("dashboard update failed (exit code 1)", public_log)
        self.assertNotIn(private_command, public_log)
        self.assertNotIn("FileNotFoundError", public_log)


if __name__ == "__main__":
    unittest.main()
