#!/usr/bin/env python3
"""Run a trusted production command behind a public-safe logging boundary."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # A child must not bypass stdout/stderr capture by writing a public GitHub
    # command file directly. It does not need these channels to do its work.
    for variable in GITHUB_COMMAND_FILE_VARIABLES:
        environment.pop(variable, None)
    return environment


def run_stage(stage: str, command: list[str]) -> int:
    label = STAGE_LABELS[stage]
    print(f"Ping CI: {label} started.", flush=True)

    try:
        diagnostic_path = _private_log_path(stage)
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
                env=_child_environment(),
                check=False,
            )
    except Exception:
        # Exception text can contain commands, credentials, connection details,
        # provider responses, and private paths. Never echo it in public CI.
        print(f"Ping CI: {label} failed (exit code 1).", file=sys.stderr, flush=True)
        return 1

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
