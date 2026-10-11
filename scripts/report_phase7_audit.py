"""Print the deterministic Phase 7 infrastructure audit summary."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scraper.company_careers.phase7_audit import build_summary


def main() -> None:
    print(json.dumps(build_summary(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
