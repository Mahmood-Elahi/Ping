from __future__ import annotations

import json
from pathlib import Path

import pytest


MANIFEST_PATH = Path(__file__).with_name("private_evidence_manifest.json")


def _matches_selector(nodeid: str, selector: str) -> bool:
    return nodeid == selector or nodeid.startswith(f"{selector}::")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    records = manifest["evidence_groups"]
    matched_selectors: set[str] = set()
    marked_nodeids: set[str] = set()

    for item in items:
        matches = [
            record
            for record in records
            if any(
                _matches_selector(item.nodeid, selector)
                for selector in record["selectors"]
            )
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise pytest.UsageError(
                f"Private-evidence test has overlapping manifest entries: {item.nodeid}"
            )
        record = matches[0]
        matching = [
            selector
            for selector in record["selectors"]
            if _matches_selector(item.nodeid, selector)
        ]
        matched_selectors.update(matching)
        marked_nodeids.add(item.nodeid)
        dependencies = ", ".join(record["dependencies"])
        item.add_marker(
            pytest.mark.private_evidence(
                reason=f"requires {record['classification']}: {dependencies}"
            )
        )

    selectors = {
        selector
        for record in records
        for selector in record["selectors"]
    }
    requested_nodes = [
        argument
        for argument in config.invocation_params.args
        if "::" in argument
        or argument.endswith(".py")
        or argument in {"tests", "tests/"}
    ]
    if not requested_nodes or requested_nodes == ["tests"]:
        unmatched = sorted(selectors - matched_selectors)
        if unmatched:
            raise pytest.UsageError(
                "Private-evidence manifest selectors matched no collected tests: "
                + ", ".join(unmatched)
            )

        expected = manifest["expected_test_count"]
        if len(marked_nodeids) != expected:
            raise pytest.UsageError(
                "Private-evidence manifest expected "
                f"{expected} tests but marked {len(marked_nodeids)}"
            )
