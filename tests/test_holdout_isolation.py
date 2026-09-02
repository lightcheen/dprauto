import copy
import json
from pathlib import Path
import tempfile
import unittest

from evaluations.holdout.validate_holdout import (
    COMMITMENT,
    DEFAULT_PRIVATE_MANIFEST,
    REPOSITORY_ROOT,
    identity_leaks,
    production_oracle_violations,
    validate,
    validate_private_manifest,
)


class HoldoutIsolationTests(unittest.TestCase):
    def test_public_commitment_is_sealed_and_aggregate_only(self) -> None:
        commitment = json.loads(COMMITMENT.read_text(encoding="utf-8"))
        self.assertEqual(commitment["state"], "sealed_not_run")
        self.assertEqual(commitment["case_count"], 12)
        self.assertEqual(sum(commitment["ecosystem_counts"].values()), 12)
        serialized = json.dumps(commitment)
        self.assertNotIn("repository", serialized)
        self.assertNotIn("revision", serialized)

    def test_public_validation_does_not_require_private_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            commitment, private_present = validate(
                private_manifest=Path(directory) / "absent.json",
                require_private=False,
            )
        self.assertEqual(commitment["case_count"], 12)
        self.assertFalse(private_present)

    def test_checked_out_private_manifest_matches_commitment_when_present(self) -> None:
        if not DEFAULT_PRIVATE_MANIFEST.is_file():
            self.skipTest("evaluator-private manifest is intentionally not checked in")
        _, private_present = validate(require_private=True)
        self.assertTrue(private_present)

    def test_identity_leak_detection_reports_tracked_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracked.txt"
            path.write_text("https://example.invalid/private/project", encoding="utf-8")
            cases = [
                {
                    "repository": "private/project",
                    "url": "https://example.invalid/private/project",
                    "revision": "a" * 40,
                }
            ]
            self.assertEqual(len(identity_leaks(cases, (path,))), 1)

    def test_development_overlap_is_rejected(self) -> None:
        if not DEFAULT_PRIVATE_MANIFEST.is_file():
            self.skipTest("evaluator-private manifest is intentionally not checked in")
        commitment = json.loads(COMMITMENT.read_text(encoding="utf-8"))
        manifest = json.loads(DEFAULT_PRIVATE_MANIFEST.read_text(encoding="utf-8"))
        contaminated = copy.deepcopy(manifest)
        corpus = json.loads(
            (REPOSITORY_ROOT / "evaluations/corpus60/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        contaminated["cases"][0]["repository"] = corpus["cases"][0]["repository"]
        errors = validate_private_manifest(
            commitment,
            contaminated,
            manifest_path=DEFAULT_PRIVATE_MANIFEST,
            files=tuple(
                path
                for path in (REPOSITORY_ROOT / "evaluations/corpus60").glob("*.json")
            ),
        )
        self.assertTrue(any("overlaps development history" in error for error in errors))

    def test_production_source_has_no_evaluator_dependency(self) -> None:
        self.assertEqual(production_oracle_violations(REPOSITORY_ROOT / "src"), [])


if __name__ == "__main__":
    unittest.main()
