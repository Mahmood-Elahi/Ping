"""Bounded public-release secret scan and approved current-tree redaction.

The scanner reports paths and counts only; it never prints matched values.
History rewriting remains a separate, explicitly authorized operation.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 8 * 1024 * 1024
GOOGLE_API_KEY = re.compile(rb"AI" rb"za[0-9A-Za-z_-]{20,}")
HIGH_CONFIDENCE_PATTERNS = {
    "google_api_key": GOOGLE_API_KEY,
    "private_key": re.compile(rb"-----BEGIN " rb"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"(?:ghp_|github_pat_)[0-9A-Za-z_]{20,}"),
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "slack_token": re.compile(rb"xox[baprs]-[0-9A-Za-z-]{20,}"),
    "openai_key": re.compile(rb"sk-(?:proj-)?[0-9A-Za-z_-]{20,}"),
    "tavily_key": re.compile(rb"tvly-[0-9A-Za-z_-]{20,}"),
    "oauth_access_token": re.compile(rb"ya29\.[0-9A-Za-z_-]{20,}"),
    "jwt": re.compile(
        rb"eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}"
    ),
    "credentialed_url": re.compile(
        rb"https?://[^\s/:@]{1,128}:[^\s/@]{8,256}@[^\s/]+"
    ),
}
GOOGLE_MAPS_CSVS = (
    Path("config/company_unknown_investigation_v3.csv"),
    Path("config/company_uncovered_fingerprints_v4.csv"),
)
REDACTION = b"REDACTED_GOOGLE_MAPS_KEY"
KNOWN_TEST_FIXTURES = {
    Path("tests/test_google_sheets_publisher.py"): b"PRIVATEKEYMATERIALSHOULDNEVERAPPEAR"
}


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def scan() -> dict[Path, dict[str, int]]:
    findings: dict[Path, dict[str, int]] = {}
    for path in repository_files():
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            continue
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            continue
        relative_path = path.relative_to(ROOT)
        fixture = KNOWN_TEST_FIXTURES.get(relative_path)
        if fixture and fixture in data:
            data = HIGH_CONFIDENCE_PATTERNS["private_key"].sub(
                b"KNOWN_REDACTION_TEST_FIXTURE", data
            )
        matches = {
            name: len(pattern.findall(data))
            for name, pattern in HIGH_CONFIDENCE_PATTERNS.items()
        }
        matches = {name: count for name, count in matches.items() if count}
        if matches:
            findings[relative_path] = matches
    return findings


def _reachable_history_blobs() -> list[tuple[str, tuple[Path, ...]]]:
    """Return unique reachable blob IDs and known historical paths.

    ``--all`` intentionally includes private/local refs for the audit. A public
    rewrite must separately select the exact refs that are allowed to leave the
    private backup.
    """

    result = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    paths_by_oid: dict[str, set[Path]] = defaultdict(set)
    for line in result.stdout.splitlines():
        oid, separator, raw_path = line.partition(" ")
        if separator and raw_path:
            paths_by_oid[oid].add(Path(raw_path))
        else:
            paths_by_oid.setdefault(oid, set())
    return [
        (oid, tuple(sorted(paths)))
        for oid, paths in sorted(paths_by_oid.items())
    ]


def scan_history() -> tuple[dict[Path, dict[str, int]], int]:
    """Scan every unique blob reachable from every local ref without values."""

    objects = _reachable_history_blobs()
    findings: dict[Path, Counter[str]] = defaultdict(Counter)
    blob_count = 0
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for oid, paths in objects:
            process.stdin.write(f"{oid}\n".encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip()
            parts = header.split()
            if len(parts) != 3 or parts[1] == "missing":
                continue
            _resolved_oid, object_type, raw_size = parts
            size = int(raw_size)
            data = process.stdout.read(size)
            process.stdout.read(1)  # protocol newline
            if object_type != "blob":
                continue
            blob_count += 1
            if size > MAX_SCAN_BYTES or b"\x00" in data[:4096]:
                continue
            effective_paths = paths or (Path("<unknown-path>"),)
            for path in effective_paths:
                scanned_data = data
                fixture = KNOWN_TEST_FIXTURES.get(path)
                if fixture and fixture in scanned_data:
                    scanned_data = HIGH_CONFIDENCE_PATTERNS["private_key"].sub(
                        b"KNOWN_REDACTION_TEST_FIXTURE", scanned_data
                    )
                for name, pattern in HIGH_CONFIDENCE_PATTERNS.items():
                    count = len(pattern.findall(scanned_data))
                    if count:
                        findings[path][name] += count
        process.stdin.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    return {
        path: dict(counts)
        for path, counts in findings.items()
    }, blob_count


def redact_current_google_maps_csvs() -> int:
    total = 0
    for relative_path in GOOGLE_MAPS_CSVS:
        path = ROOT / relative_path
        data = path.read_bytes()
        redacted, count = GOOGLE_API_KEY.subn(REDACTION, data)
        if count:
            path.write_bytes(redacted)
            print(f"redacted {count} potential Google API key(s) in {relative_path}")
            total += count
    return total


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report high-confidence secret paths without printing values."
    )
    parser.add_argument(
        "--redact-current-google-maps-csvs",
        action="store_true",
        help="Replace key-shaped values only in the two audited historical CSVs.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Scan unique blobs reachable from every local ref without printing values.",
    )
    args = parser.parse_args()

    if args.redact_current_google_maps_csvs:
        redact_current_google_maps_csvs()

    findings = scan()
    for path, matches in sorted(findings.items(), key=lambda item: str(item[0])):
        summary = ", ".join(f"{name}={count}" for name, count in sorted(matches.items()))
        print(f"{path}: {summary}")
    if findings:
        print(f"public-release secret scan failed: {len(findings)} path(s)")
        return 1
    if args.history:
        history_findings, blob_count = scan_history()
        for path, matches in sorted(
            history_findings.items(), key=lambda item: str(item[0])
        ):
            summary = ", ".join(
                f"{name}={count}" for name, count in sorted(matches.items())
            )
            print(f"history:{path}: {summary}")
        if history_findings:
            print(
                "public-release history scan failed: "
                f"{len(history_findings)} path(s) across {blob_count} unique blobs"
            )
            return 1
        print(f"public-release history scan passed: {blob_count} unique blobs")
    print("public-release secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
