"""Plan or execute the three read-only collection canaries for run #424."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_source_integrity_canaries import BoundedSession, run_canary, selected_targets
from src.scraper.company_careers.models import AdapterError, CompanyTarget
from src.scraper.company_careers.student_roles import configure_student_targets


@dataclass(frozen=True)
class Source:
    key: str
    company: str
    listing_url: str
    target_sha256: str
    request_budget: int


SOURCES = (
    Source("amd", "AMD", "https://careers.amd.com/api/jobs",
           "5c67c15a11335263717bbeff19862919b052717c1728233faf5cbfe8a1d3a61a", 250),
    Source("mercado-libre", "Mercado Libre", "https://mercadolibre.eightfold.ai/api/pcsx/search",
           "0e2bc30a4a8ac2588d2f880f973e34495ec7d687cba38403a2c26f8318f5cf9b", 164),
    Source("pcl-construction", "PCL Construction", "https://careers.pcl.com/search/",
           "aea34af269746f6aa3d6aba1f6f68e67c988dc29d3f8ff8305772c5a7d0cfa20", 96),
)
SOURCE_SCOPE = "default"
CHECKPOINT_DIR = ROOT / "output" / "run424-verification"


class ReviewedListingSession(BoundedSession):
    def __init__(self, source: Source) -> None:
        super().__init__(source.request_budget)
        self.listing_url = urlsplit(source.listing_url)
        self.trust_env = False

    def send_admitted(self, request, **kwargs):
        destination = urlsplit(request.url)
        if (
            request.method != "GET"
            or request.body is not None
            or (destination.scheme, destination.netloc, destination.path)
            != (self.listing_url.scheme, self.listing_url.netloc, self.listing_url.path)
        ):
            raise AdapterError("Canary listing destination identity mismatch")
        response = super().send_admitted(request, **kwargs)
        if response.status_code == 429:
            response.close()
            raise AdapterError("Canary received HTTP 429 from the listing provider")
        return response


def reviewed_targets() -> dict[str, CompanyTarget]:
    targets = configure_student_targets(
        selected_targets([source.company for source in SOURCES], source_scope=SOURCE_SCOPE)
    )
    by_name = {target.name: target for target in targets}
    for source in SOURCES:
        target = by_name[source.company]
        encoded = json.dumps(asdict(target), sort_keys=True, separators=(",", ":")).encode()
        if (
            sha256(encoded).hexdigest() != source.target_sha256
            or (target.endpoint_url or target.search_url) != source.listing_url
        ):
            raise ValueError(f"Reviewed production target changed for {source.company}")
    return by_name


def revision(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def write_checkpoint(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def execute(targets: dict[str, CompanyTarget], checkpoint_dir: Path, plan: dict) -> int:
    import fcntl

    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    with (checkpoint_dir / "campaign.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = checkpoint_dir / "campaign.json"
        existing_receipts = [
            checkpoint_dir / f"{source.key}.json" for source in SOURCES
        ]
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != plan:
                raise ValueError("Checkpoint directory belongs to a different review plan")
        elif any(path.exists() for path in existing_receipts):
            raise ValueError("Checkpoint receipts exist without their review plan")
        else:
            write_checkpoint(manifest, plan)

        statuses = []
        for source, receipt in zip(SOURCES, existing_receipts, strict=True):
            if receipt.exists():
                record = json.loads(receipt.read_text(encoding="utf-8"))
                if record.get("company") != source.company or record.get("state") not in {
                    "started", "finished",
                }:
                    raise ValueError(f"Invalid checkpoint receipt for {source.company}")
                if record["state"] == "started":
                    statuses.append("INTERRUPTED")
                    print(f"{source.company} INTERRUPTED. Review {receipt}", flush=True)
                    continue
            else:
                record = {
                    "state": "started",
                    "company": source.company,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                # Persist the claim before a request. An interrupted claim cannot be replayed.
                write_checkpoint(receipt, record)
                try:
                    result = run_canary(
                        targets[source.company], source.request_budget,
                        session=ReviewedListingSession(source),
                    )
                except Exception as exc:
                    result = {"canary_outcome": "RUNNER_ERROR", "error_type": type(exc).__name__}
                record.update(
                    state="finished",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    result=result,
                )
                write_checkpoint(receipt, record)
            outcome = record["result"]["canary_outcome"]
            statuses.append(outcome)
            print(f"{source.company} {outcome}. Receipt {receipt}", flush=True)
        return 0 if all(status == "PASS_COMPLETE" for status in statuses) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-dir", type=Path, default=CHECKPOINT_DIR)
    parser.add_argument("--run", action="store_true", help="Send the three authorized canaries")
    args = parser.parse_args(argv)
    try:
        targets = reviewed_targets()
        plan = {
            "campaign": "run424-collection-recovery",
            "revision": revision("rev-parse", "HEAD"),
            "source_scope": SOURCE_SCOPE,
            "sources": [asdict(source) for source in SOURCES],
            "total_request_budget": sum(source.request_budget for source in SOURCES),
        }
        if not args.run:
            print(json.dumps({**plan, "checkpoint_dir": str(args.checkpoint_dir.resolve())}, indent=2))
            return 0
        if os.name != "posix":
            raise ValueError("Live verification requires POSIX checkpoint locking")
        if revision("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Use a clean checkout of the reviewed PR revision")
        return execute(targets, args.checkpoint_dir.resolve(), plan)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
