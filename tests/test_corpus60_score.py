import copy
import json
import unittest
from pathlib import Path

from evaluations.corpus60.quality_gate import compare
from evaluations.corpus60.score_probe import score
from evaluations.corpus60.validate_workspace_ground_truth import load_and_validate


class Corpus60ScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _, cls.oracle = load_and_validate(require_sources=True)

    def minimal_probe(self) -> dict:
        records = []
        for case in self.oracle["cases"]:
            primary_ids = set(case["primary_component_ids"])
            component = next(
                item for item in case["components"] if item["component_id"] in primary_ids
            )
            build_system = next(
                item["build_system"]
                for item in component["build_entries"]
                if item["role"] == "primary"
            )
            records.append(
                {
                    "case_id": case["case_id"],
                    "status": "planned",
                    "component_candidates": [{"root": component["root"]}],
                    "selected_component_root": component["root"],
                    "primary_build_system": build_system,
                    "build_files": [],
                    "package_managers": [],
                    "scan_truncated": False,
                }
            )
        return {"records": records}

    def test_perfect_static_probe_scores_all_cases_without_execution_claim(self) -> None:
        report = score(self.minimal_probe(), self.oracle)
        metrics = report["metrics"]
        self.assertEqual(metrics["component_discovery_recall_at_k"]["passed"], 60)
        self.assertEqual(metrics["primary_selection_accuracy_at_1"]["passed"], 60)
        self.assertEqual(metrics["build_system_accuracy"]["passed"], 60)
        self.assertEqual(metrics["production_plan_success"]["passed"], 60)
        self.assertEqual(report["execution_metrics"]["status"], "not_run")
        self.assertIsNone(report["execution_metrics"]["strict_success"])

    def test_wrong_root_plan_is_counted_as_false_success(self) -> None:
        probe = self.minimal_probe()
        record = probe["records"][0]
        record["component_candidates"] = [{"root": "wrong"}]
        record["selected_component_root"] = "wrong"

        metrics = score(probe, self.oracle)["metrics"]

        self.assertEqual(metrics["component_discovery_recall_at_k"]["passed"], 59)
        self.assertEqual(metrics["primary_selection_accuracy_at_1"]["passed"], 59)
        self.assertEqual(metrics["false_positive_candidate_count"], 1)
        self.assertEqual(metrics["false_success_plan_count"], 1)

    def test_threshold_comparison_rejects_regression(self) -> None:
        metrics = score(self.minimal_probe(), self.oracle)["metrics"]
        regressed = copy.deepcopy(metrics)
        regressed["production_plan_success"]["passed"] = 58
        thresholds = {
            "minimum_passed": {"production_plan_success": 59},
            "maximum_counts": {"false_success_plan_count": 0},
        }
        errors = compare(regressed, thresholds)
        self.assertTrue(any("production_plan_success regressed" in error for error in errors))

    def test_threshold_file_matches_suite(self) -> None:
        corpus_root = Path(__file__).parents[1] / "evaluations/corpus60"
        thresholds = json.loads(
            (corpus_root / "quality-thresholds.json").read_text(encoding="utf-8")
        )
        self.assertEqual(thresholds["suite_id"], self.oracle["suite_id"])

    def test_thresholds_match_checked_in_p0_baseline(self) -> None:
        corpus_root = Path(__file__).parents[1] / "evaluations/corpus60"
        thresholds = json.loads(
            (corpus_root / "quality-thresholds.json").read_text(encoding="utf-8")
        )
        baseline = json.loads(
            (corpus_root / "baselines/p0-static-quality-20260902.json").read_text(
                encoding="utf-8"
            )
        )
        metrics = baseline["metrics"]
        for name, minimum in thresholds["minimum_passed"].items():
            self.assertEqual(metrics[name]["passed"], minimum)
        for name, maximum in thresholds["maximum_counts"].items():
            observed = (
                metrics["status_counts"].get(name.partition(":")[2], 0)
                if name.startswith("status:")
                else metrics[name]
            )
            self.assertEqual(observed, maximum)


if __name__ == "__main__":
    unittest.main()
