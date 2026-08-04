"""Create a deterministic generated company registry from enriched watchlist rows."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.company_careers.models import CompanyTarget

try:
    from scripts.company_watchlist_common import (
        DEFAULT_REGISTRY_PATH,
        canonicalize_url,
        clean_text,
        detect_ats,
        load_registry,
        normalize_company_name,
        read_enriched_csv,
        registry_by_name,
        registry_source_url,
        validate_detection_fields,
    )
except ModuleNotFoundError:  # Direct execution places scripts/ first on sys.path.
    from company_watchlist_common import (  # type: ignore[no-redef]
        DEFAULT_REGISTRY_PATH,
        canonicalize_url,
        clean_text,
        detect_ats,
        load_registry,
        normalize_company_name,
        read_enriched_csv,
        registry_by_name,
        registry_source_url,
        validate_detection_fields,
    )


DEFAULT_INPUT = Path("config/company_watchlist_enriched.csv")
DEFAULT_OUTPUT = Path("config/target_companies.generated.yaml")
DEFAULT_REPORT = Path("output/company_watchlist_import_report.json")


def _new_target(row: Mapping[str, Any]) -> dict[str, Any]:
    platform = clean_text(row.get("ATS Platform"))
    target: dict[str, Any] = {
        "name": clean_text(row.get("Company")),
        "platform": platform,
    }
    if platform in {"greenhouse", "lever", "ashby"}:
        target["slug"] = clean_text(row.get("ATS Slug"))
    elif platform == "workday":
        target.update(
            {
                "tenant": clean_text(row.get("Workday Tenant")),
                "wd_subdomain": clean_text(row.get("Workday Subdomain")),
                "site_id": clean_text(row.get("Workday Site ID")),
            }
        )
    careers_url = canonicalize_url(row.get("Careers URL"))
    if careers_url:
        target["careers_url"] = careers_url
    priority = clean_text(row.get("Priority"))
    notes = clean_text(row.get("Notes"))
    source_url = canonicalize_url(row.get("Source URL"))
    if priority:
        target["priority"] = priority
    if notes:
        target["notes"] = notes
    if source_url:
        target["source_url"] = source_url
    return target


def build_generated_registry(
    rows: list[Mapping[str, Any]],
    existing_entries: list[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    generated = [copy.deepcopy(dict(entry)) for entry in existing_entries]
    existing_by_name = registry_by_name(existing_entries)
    result_by_name = dict(existing_by_name)
    seen_urls: dict[str, str] = {}
    for entry in generated:
        entry_urls = [entry.get("careers_url"), entry.get("source_url"), registry_source_url(entry)]
        for candidate in entry_urls:
            url = canonicalize_url(candidate)
            if url:
                seen_urls[url] = clean_text(entry.get("name"))

    decisions: list[dict[str, str]] = []
    status_counts: Counter[str] = Counter()
    input_name_counts = Counter(normalize_company_name(row.get("Company")) for row in rows)

    for index, row in enumerate(rows, start=1):
        company = clean_text(row.get("Company"))
        name_key = normalize_company_name(company)
        status = clean_text(row.get("Import Status"))
        platform = clean_text(row.get("ATS Platform"))
        url = canonicalize_url(row.get("Careers URL"))
        source_url = canonicalize_url(row.get("Source URL"))
        decision = "skipped"
        reason = ""

        if not name_key:
            reason = "Missing company name"
        elif name_key in existing_by_name:
            decision = "existing_preserved"
            reason = "Existing configuration is authoritative"
        elif input_name_counts[name_key] > 1 or name_key in result_by_name:
            decision = "duplicate_company"
            reason = "Duplicate normalized company name"
        elif url and url in seen_urls:
            decision = "duplicate_url"
            reason = f"Careers URL already belongs to {seen_urls[url]}"
        elif source_url and source_url in seen_urls:
            decision = "duplicate_url"
            reason = f"Source URL already belongs to {seen_urls[source_url]}"
        elif status not in {"validated", "validated_supported"}:
            decision = "manual_review"
            reason = f"Import Status is {status or 'missing'}, not validated_supported"
        else:
            detection = detect_ats(url)
            if detection.platform != platform and source_url:
                detection = detect_ats(source_url)
            fields_valid, fields_reason = validate_detection_fields(detection)
            supplied_detection_matches = (
                detection.platform == platform
                and detection.slug == clean_text(row.get("ATS Slug"))
                and detection.tenant == clean_text(row.get("Workday Tenant"))
                and detection.wd_subdomain == clean_text(row.get("Workday Subdomain"))
                and detection.site_id == clean_text(row.get("Workday Site ID"))
            )
            if not fields_valid:
                decision = "manual_review"
                reason = fields_reason
            elif not supplied_detection_matches:
                decision = "manual_review"
                reason = "ATS fields do not match the validated careers URL"
            else:
                target = _new_target(row)
                generated.append(target)
                result_by_name[name_key] = target
                if url:
                    seen_urls[url] = company
                if source_url:
                    seen_urls[source_url] = company
                decision = "added"
                reason = "Validated entry added"

        status_counts[decision] += 1
        decisions.append(
            {
                "row": str(index),
                "company": company,
                "decision": decision,
                "reason": reason,
            }
        )

    result = {"target_companies": generated}
    report = {
        "summary": {
            "input_rows": len(rows),
            "existing_entries": len(existing_entries),
            "generated_entries": len(generated),
            "added_entries": len(generated) - len(existing_entries),
            "decision_counts": dict(sorted(status_counts.items())),
        },
        "decisions": decisions,
    }
    return result, report


def validate_registry_data(data: Mapping[str, Any]) -> None:
    entries = data.get("target_companies")
    if not isinstance(entries, list):
        raise ValueError("Generated registry has no target_companies list")
    registry_by_name(entries)
    for entry in entries:
        CompanyTarget.from_dict(entry)


def dump_registry(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True, width=1000)


def import_watchlist(
    input_path: Path | str,
    output_path: Path | str = DEFAULT_OUTPUT,
    merge_path: Path | str = DEFAULT_REGISTRY_PATH,
    *,
    validate: bool = False,
    dry_run: bool = False,
    report_path: Path | str = DEFAULT_REPORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = read_enriched_csv(input_path)
    _, existing_entries = load_registry(merge_path)
    generated, report = build_generated_registry(rows, existing_entries)
    if validate:
        validate_registry_data(generated)
    if not dry_run:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(dump_registry(generated), encoding="utf-8", newline="\n")
        report_output = Path(report_path)
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return generated, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--merge",
        nargs="?",
        type=Path,
        const=DEFAULT_REGISTRY_PATH,
        default=DEFAULT_REGISTRY_PATH,
        help="Registry to preserve (defaults to config/target_companies.yaml)",
    )
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _, report = import_watchlist(
        args.input,
        args.output,
        args.merge,
        validate=args.validate,
        dry_run=args.dry_run,
    )
    output = {
        **report,
        "dry_run": args.dry_run,
        "output": None if args.dry_run else str(args.output),
        "report_output": None if args.dry_run else str(DEFAULT_REPORT),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
