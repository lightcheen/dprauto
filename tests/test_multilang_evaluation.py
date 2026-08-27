import copy
import hashlib
import json
import unittest
from pathlib import Path

from evaluations.multilang.run_evaluation import (
    DEFAULT_MANIFEST,
    ManifestValidationError,
    build_report,
    load_and_validate,
    validate_manifest,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
M9_BASELINE = REPOSITORY_ROOT / "evaluations/baselines/python-21-m9-20260824.json"


class MultilangEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest, cls.ground_truth, cls.ground_truth_path = load_and_validate()

    def test_real_manifest_has_balanced_seed_corpus(self) -> None:
        report = build_report(
            self.manifest,
            self.ground_truth,
            manifest_path=DEFAULT_MANIFEST,
            ground_truth_path=self.ground_truth_path,
        )

        self.assertEqual(report["case_count"], 21)
        self.assertEqual(report["languages"], {"c": 4, "cpp": 5, "java": 4, "python": 8})
        self.assertEqual(report["source_states"], {"ready": 21})
        self.assertEqual(report["ground_truth_review"], {"reviewed": 21})
        self.assertEqual(report["fetch_required_case_ids"], [])
        self.assertFalse(report["execution_performed"])

    def test_strict_validation_accepts_pinned_cxxcrafter_sources(self) -> None:
        validate_manifest(
            self.manifest,
            self.ground_truth,
            manifest_path=DEFAULT_MANIFEST,
            strict_sources=True,
        )

    def test_test_command_cannot_be_replaced_by_download_command(self) -> None:
        ground_truth = copy.deepcopy(self.ground_truth)
        ground_truth["cases"][0]["commands"]["test"] = [
            "pip download -r cryptography.txt"
        ]

        with self.assertRaisesRegex(
            ManifestValidationError,
            "test command has no recognized ordinary test runner|false/special target",
        ):
            validate_manifest(
                self.manifest,
                ground_truth,
                manifest_path=DEFAULT_MANIFEST,
            )

    def test_ready_source_requires_a_revision(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cases"][0]["source"]["revision"] = None

        with self.assertRaisesRegex(ManifestValidationError, "ready source needs a revision"):
            validate_manifest(
                manifest,
                self.ground_truth,
                manifest_path=DEFAULT_MANIFEST,
            )

    def test_strict_validation_rejects_a_revision_not_at_local_head(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["cases"][-1]["source"]["revision"] = "0" * 40

        with self.assertRaisesRegex(ManifestValidationError, "does not match pinned revision"):
            validate_manifest(
                manifest,
                self.ground_truth,
                manifest_path=DEFAULT_MANIFEST,
                strict_sources=True,
            )

    def test_ground_truth_must_cover_every_manifest_case_once(self) -> None:
        ground_truth = copy.deepcopy(self.ground_truth)
        ground_truth["cases"].pop()

        with self.assertRaisesRegex(ManifestValidationError, "case ID sets differ"):
            validate_manifest(
                self.manifest,
                ground_truth,
                manifest_path=DEFAULT_MANIFEST,
            )

    def test_m9_python_baseline_is_frozen_and_self_consistent(self) -> None:
        baseline = json.loads(M9_BASELINE.read_text(encoding="utf-8"))
        cases = baseline["cases"]

        self.assertEqual(baseline["selection"]["indices"], list(range(4, 25)))
        self.assertEqual([case["index"] for case in cases], list(range(4, 25)))
        self.assertEqual(len({case["repo"] for case in cases}), 21)
        self.assertEqual(
            baseline["metrics"]["standard_build_success"],
            sum(case["standard_build_status"] == "succeeded" for case in cases),
        )
        self.assertEqual(
            baseline["metrics"]["final_environment_success"],
            sum(case["final_status"] == "succeeded" for case in cases),
        )
        self.assertEqual(
            baseline["metrics"]["entered_agent"],
            sum(case["agent_participated"] for case in cases),
        )
        self.assertEqual(
            baseline["metrics"]["agent_repair_success"],
            sum(
                case["agent_participated"] and case["final_status"] == "succeeded"
                for case in cases
            ),
        )
        manifest_path = REPOSITORY_ROOT / baseline["selection"]["source_manifest"]
        self.assertEqual(
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            baseline["source_manifest_sha256"],
        )
        summary_path = REPOSITORY_ROOT / baseline["source_run"] / "summary.json"
        if summary_path.is_file():
            self.assertEqual(
                hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                baseline["source_summary_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
