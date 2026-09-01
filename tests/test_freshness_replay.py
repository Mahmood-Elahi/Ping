from __future__ import annotations

import json
import unittest

from scripts import replay_freshness_v1 as replay


@unittest.skipUnless(replay.SOURCE_ARTIFACT.exists(), "frozen ranked replay artifact unavailable")
class FreshnessReplayTests(unittest.TestCase):
    def test_replay_is_deterministic_and_preserves_ranking(self):
        self.assertEqual(replay.main([]), 0)
        first_json = replay.JSON_REPORT.read_bytes()
        first_markdown = replay.MARKDOWN_REPORT.read_bytes()
        self.assertEqual(replay.main([]), 0)
        self.assertEqual(replay.JSON_REPORT.read_bytes(), first_json)
        self.assertEqual(replay.MARKDOWN_REPORT.read_bytes(), first_markdown)

        report = json.loads(first_json)
        self.assertEqual(report["validation_subset"]["current_fixture_rows"], 10)
        self.assertEqual(report["validation_subset"]["source_rows_used_to_shape_fixtures"], 12)
        for band in ("new", "fresh", "normal", "aging", "stale"):
            self.assertGreater(report["summary"][band], 0, band)
        for band in ("apply_now", "high", "normal", "low", "closed"):
            self.assertGreater(report["summary"][f"urgency_{band}"], 0, band)
        transitions = {item["transition"] for item in report["transitions"]}
        self.assertIn("removed_after_complete_absence", transitions)
        self.assertIn("reappeared", transitions)
        self.assertTrue(report["ranking_semantics_unchanged"])
        self.assertFalse(report["historical_lifecycle_available"])
        self.assertGreater(len(report["date_parse_failures"]), 0)

        full = report["full_artifact"]
        self.assertEqual(full["total_input_jobs"], report["source_artifact_jobs"])
        self.assertEqual(full["successfully_annotated"], full["total_input_jobs"])
        self.assertEqual(len(full["annotation_outcomes"]), full["total_input_jobs"])
        self.assertEqual(full["duplicate_canonical_jobs"], 0)
        self.assertEqual(full["unknown_or_unclassifiable_count"], 0)
        self.assertTrue(full["ranking_invariance"]["all_invariants_hold"])
        self.assertEqual(full["ranking_invariance"]["score_difference_count"], 0)


if __name__ == "__main__":
    unittest.main()
