"""Print the deterministic Phase 5 configuration-only coverage report."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.phase5_coverage import (
    build_phase5_batched_coverage_report,
)


def main() -> int:
    print(json.dumps(build_phase5_batched_coverage_report(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
