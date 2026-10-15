#!/usr/bin/env python3
"""Run a trusted production command behind a public-safe logging boundary."""

from __future__ import annotations

import argparse
import html
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.public_ci_report import (
    FAILURE_LABELS,
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    PUBLIC_REPORT_PATH_VARIABLE,
    SCAN_REASON_LABELS,
    STAGE_LABELS as REPORT_STAGE_LABELS,
    read_public_event_line,
)


STAGE_LABELS = {
    "production-preflight": "production preflight",
    "dashboard-update": "dashboard update",
}
GITHUB_COMMAND_FILE_VARIABLES = (
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one fixed CI stage without publishing its operational output."
    )
    parser.add_argument("--stage", required=True, choices=tuple(STAGE_LABELS))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def _private_log_path(stage: str) -> Path:
    runner_temp = os.environ.get("RUNNER_TEMP")
    base = (
        Path(runner_temp) / "ping-private-ci"
        if runner_temp
        else REPOSITORY_ROOT / "logs" / "private-ci"
    )
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        # Windows ACLs, rather than POSIX mode bits, govern the local fallback.
        pass
    return base / f"{stage}.log"


def _child_environment(report_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # A child must not bypass stdout/stderr capture by writing a public GitHub
    # command file directly. It does not need these channels to do its work.
    for variable in GITHUB_COMMAND_FILE_VARIABLES:
        environment.pop(variable, None)
    environment[PUBLIC_REPORT_PATH_VARIABLE] = str(report_path)
    return environment


def _report_path(diagnostic_path: Path) -> Path:
    return diagnostic_path.with_suffix(".report.jsonl")


def _read_public_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read bounded complete lines without retaining oversized input."""

    events: list[dict[str, Any]] = []
    rejected = 0
    pending = b""
    dropping = False
    try:
        with path.open("rb") as report_file:
            while chunk := report_file.read(MAX_EVENT_BYTES):
                while chunk:
                    newline = chunk.find(b"\n")
                    if dropping:
                        if newline < 0:
                            chunk = b""
                        else:
                            dropping = False
                            rejected += 1
                            chunk = chunk[newline + 1 :]
                        continue
                    if newline < 0:
                        pending += chunk
                        chunk = b""
                        if len(pending) >= MAX_EVENT_BYTES:
                            pending = b""
                            dropping = True
                        continue
                    line = pending + chunk[: newline + 1]
                    pending = b""
                    chunk = chunk[newline + 1 :]
                    event = read_public_event_line(line)
                    if event is None or len(events) >= MAX_EVENTS:
                        rejected += 1
                    else:
                        events.append(event)
            if dropping or pending:
                rejected += 1
    except OSError:
        rejected += 1
    return events, rejected


def _markdown_text(value: str) -> str:
    text = html.escape(value, quote=False)
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "|", "#"):
        text = text.replace(character, f"\\{character}")
    return text


class _PublicReportRenderer:
    def __init__(
        self,
        events: list[dict[str, Any]],
        rejected: int,
        *,
        wrapper_runtime_seconds: float,
    ):
        self.events = events
        self.rejected = rejected
        self.wrapper_runtime_seconds = max(0.0, wrapper_runtime_seconds)
        self.configured = next(
            (event["configured_companies"] for event in events if event["type"] == "run_start"),
            None,
        )
        self.companies = [event for event in events if event["type"] == "company"]
        self.scan_summary = next(
            (event for event in reversed(events) if event["type"] == "scan_summary"),
            None,
        )

    def print_log(self) -> None:
        for event in self.events:
            self._print_event(event)
        if self.scan_summary is None and self.companies:
            counts = self._partial_counts()
            configured = self.configured if self.configured is not None else "unknown"
            print(
                "Ping report: Partial scan summary: "
                f"attempted={len(self.companies)}/{configured}, "
                f"complete={counts['complete']}, incomplete={counts['incomplete']}, "
                f"failed={counts['failed']}; unattempted companies were not classified.",
                flush=True,
            )
        if not any(event["type"] == "run_summary" for event in self.events):
            print(
                "Ping report: Total execution time before exit: "
                f"{self.wrapper_runtime_seconds:.3f}s.",
                flush=True,
            )
        if self.rejected:
            print(
                f"Ping report: Rejected {self.rejected} malformed public reporting "
                "event(s); no rejected content was displayed.",
                file=sys.stderr,
                flush=True,
            )

    def _print_event(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        if event_type == "run_start":
            print(
                f"Ping report: Configured companies: {event['configured_companies']}.",
                flush=True,
            )
        elif event_type == "stage":
            label = REPORT_STAGE_LABELS[event["stage"]]
            if event["status"] == "started":
                detail = "started"
            elif event["status"] == "failed":
                detail = (
                    f"failed: {FAILURE_LABELS[event['failure_category']]} "
                    f"({event['duration_seconds']:.3f}s)"
                )
            else:
                detail = f"{event['status']} ({event['duration_seconds']:.3f}s)"
            print(f"Ping report: Stage {label}: {detail}.", flush=True)
        elif event_type == "company":
            reason = SCAN_REASON_LABELS[event["reason"]]
            print(
                "Ping report: Company "
                f"{event['name']}: status={event['status']}, "
                f"jobs_fetched={event['jobs_fetched']}, "
                f"jobs_retained={event['jobs_retained']}, reason={reason}.",
                flush=True,
            )
        elif event_type == "scan_summary":
            print(
                "Ping report: Scan summary: "
                f"configured={event['configured_companies']}, "
                f"attempted={event['attempted_companies']}, "
                f"complete={event['complete_companies']}, "
                f"incomplete={event['incomplete_companies']}, "
                f"failed={event['failed_companies']}, "
                f"coverage_health={event['coverage_health']}.",
                flush=True,
            )
            print(
                "Ping report: Job totals: "
                f"fetched={event['jobs_fetched']}, normalized={event['normalized_jobs']}, "
                f"retained={event['retained_jobs']}; "
                f"scan_runtime_seconds={event['scan_runtime_seconds']:.3f}.",
                flush=True,
            )
        elif event_type == "ranking_summary":
            print(
                f"Ping report: Final ranked jobs: {event['final_ranked_jobs']}.",
                flush=True,
            )
        elif event_type == "sync_summary":
            print(
                "Ping report: Supabase synchronization: "
                f"inserted={event['inserted']}, updated={event['updated']}, "
                f"unchanged={event['unchanged']}, "
                f"missing_advanced={event['missing_incremented']}, closed={event['closed']}.",
                flush=True,
            )
        elif event_type == "sheets_summary":
            print(
                "Ping report: Google Sheets rows: "
                f"new_jobs={event['new_jobs']}, active_jobs={event['active_jobs']}, "
                f"companies={event['companies']}.",
                flush=True,
            )
        elif event_type == "run_summary":
            print(
                f"Ping report: Total execution time: {event['runtime_seconds']:.3f}s.",
                flush=True,
            )

    def _partial_counts(self) -> dict[str, int]:
        return {
            state: sum(event["status"] == state for event in self.companies)
            for state in ("complete", "incomplete", "failed")
        }

    def append_job_summary(self, path_value: str | None, *, return_code: int) -> None:
        if not path_value or not self.events:
            return
        stages = [event for event in self.events if event["type"] == "stage"]
        final_stages: dict[str, dict[str, Any]] = {}
        for event in stages:
            final_stages[event["stage"]] = event
        lines = [
            "## Ping production report",
            "",
            f"- Process result: **{'success' if return_code == 0 else 'failed'}**",
        ]
        if self.scan_summary is not None:
            item = self.scan_summary
            lines.extend(
                [
                    f"- Source coverage: **{item['coverage_health']}**",
                    f"- Companies attempted: {item['attempted_companies']}/{item['configured_companies']}",
                    (
                        f"- Complete / incomplete / failed: {item['complete_companies']} / "
                        f"{item['incomplete_companies']} / {item['failed_companies']}"
                    ),
                    (
                        f"- Jobs fetched / normalized / retained: {item['jobs_fetched']} / "
                        f"{item['normalized_jobs']} / {item['retained_jobs']}"
                    ),
                ]
            )
        elif self.companies:
            counts = self._partial_counts()
            configured = self.configured if self.configured is not None else "unknown"
            lines.extend(
                [
                    f"- Partial companies attempted: {len(self.companies)}/{configured}",
                    (
                        f"- Complete / incomplete / failed so far: {counts['complete']} / "
                        f"{counts['incomplete']} / {counts['failed']}"
                    ),
                    "- Unattempted companies were not classified.",
                ]
            )
        ranking = next(
            (event for event in reversed(self.events) if event["type"] == "ranking_summary"),
            None,
        )
        if ranking:
            lines.append(f"- Final ranked jobs: {ranking['final_ranked_jobs']}")
        runtime = next(
            (event for event in reversed(self.events) if event["type"] == "run_summary"),
            None,
        )
        if runtime:
            lines.append(f"- Total execution time: {runtime['runtime_seconds']:.3f}s")
        else:
            lines.append(
                "- Total execution time before exit: "
                f"{self.wrapper_runtime_seconds:.3f}s"
            )

        if final_stages:
            lines.extend(["", "### Pipeline stages", "", "| Stage | Result |", "|---|---|"])
            for stage in REPORT_STAGE_LABELS:
                event = final_stages.get(stage)
                if event is None:
                    continue
                result = event["status"]
                if result == "failed":
                    result += f" — {FAILURE_LABELS[event['failure_category']]}"
                lines.append(f"| {_markdown_text(REPORT_STAGE_LABELS[stage])} | {_markdown_text(result)} |")

        if self.companies:
            lines.extend(
                [
                    "",
                    "### Company scan results",
                    "",
                    "| Company | Status | Fetched | Retained | Reason |",
                    "|---|---:|---:|---:|---|",
                ]
            )
            for event in self.companies:
                lines.append(
                    f"| {_markdown_text(event['name'])} | {event['status']} | "
                    f"{event['jobs_fetched']} | {event['jobs_retained']} | "
                    f"{_markdown_text(SCAN_REASON_LABELS[event['reason']])} |"
                )
        if self.rejected:
            lines.extend(
                [
                    "",
                    f"> {self.rejected} malformed reporting event(s) were rejected without display.",
                ]
            )
        try:
            with Path(path_value).open("a", encoding="utf-8") as summary_file:
                summary_file.write("\n".join(lines) + "\n\n")
        except OSError:
            print(
                "Ping report: The validated job summary could not be written.",
                file=sys.stderr,
                flush=True,
            )


def run_stage(stage: str, command: list[str]) -> int:
    label = STAGE_LABELS[stage]
    print(f"Ping CI: {label} started.", flush=True)
    wrapper_started = time.monotonic()

    try:
        diagnostic_path = _private_log_path(stage)
        report_path = _report_path(diagnostic_path)
        report_descriptor = os.open(
            report_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.close(report_descriptor)
        descriptor = os.open(
            diagnostic_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as diagnostics:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=diagnostics,
                stderr=subprocess.STDOUT,
                env=_child_environment(report_path),
                check=False,
            )
    except Exception:
        # Exception text can contain commands, credentials, connection details,
        # provider responses, and private paths. Never echo it in public CI.
        print(f"Ping CI: {label} failed (exit code 1).", file=sys.stderr, flush=True)
        return 1

    events, rejected = _read_public_events(report_path)
    renderer = _PublicReportRenderer(
        events,
        rejected,
        wrapper_runtime_seconds=time.monotonic() - wrapper_started,
    )
    renderer.print_log()
    renderer.append_job_summary(
        os.environ.get("GITHUB_STEP_SUMMARY"),
        return_code=result.returncode,
    )
    try:
        report_path.unlink()
    except OSError:
        pass

    return_code = result.returncode if 0 <= result.returncode <= 255 else 1
    if return_code:
        print(
            f"Ping CI: {label} failed (exit code {return_code}).",
            file=sys.stderr,
            flush=True,
        )
        return return_code

    print(f"Ping CI: {label} completed successfully.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_stage(args.stage, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
