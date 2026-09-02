import copy
import unittest

from evaluations.corpus60.build_workspace_ground_truth import build_document
from evaluations.corpus60.validate_workspace_ground_truth import (
    HERE,
    load_and_validate,
    validate_document,
)


class Corpus60WorkspaceGroundTruthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest, cls.document = load_and_validate(require_sources=True)

    def test_oracle_covers_all_frozen_cases(self) -> None:
        self.assertEqual(len(self.document["cases"]), 60)
        self.assertEqual(
            {case["case_id"] for case in self.document["cases"]},
            {case["case_id"] for case in self.manifest["cases"]},
        )

    def test_checked_in_oracle_matches_audited_generator(self) -> None:
        self.assertEqual(self.document, build_document(self.manifest))

    def test_nested_native_cases_have_reviewed_primary_components(self) -> None:
        cases = {case["case_id"]: case for case in self.document["cases"]}
        expected = {
            "cpp-twitter--vireo": ("vireo-library", "vireo", "vireo/configure"),
            "cpp-official-stockfish--stockfish": (
                "stockfish-engine",
                "src",
                "src/Makefile",
            ),
            "cpp-openalpr--openalpr": (
                "openalpr-native",
                "src",
                "src/CMakeLists.txt",
            ),
        }
        for case_id, (component_id, root, build_entry) in expected.items():
            case = cases[case_id]
            self.assertEqual(case["primary_component_ids"], [component_id])
            component = next(
                item for item in case["components"] if item["component_id"] == component_id
            )
            self.assertEqual(component["root"], root)
            self.assertIn(build_entry, [item["path"] for item in component["build_entries"]])

    def test_documented_build_entry_wins_over_manifest_order(self) -> None:
        cases = {case["case_id"]: case for case in self.document["cases"]}
        component = cases["cpp-pistacheio--pistache"]["components"][0]
        roles = {item["path"]: item["role"] for item in component["build_entries"]}
        self.assertEqual(roles["meson.build"], "primary")
        self.assertEqual(roles["CMakeLists.txt"], "alternative")

    def test_oracle_rejects_missing_or_unsafe_components(self) -> None:
        document = copy.deepcopy(self.document)
        document["cases"][0]["components"][0]["root"] = "../outside"
        errors = validate_document(
            self.manifest,
            document,
            corpus_root=HERE,
            require_sources=False,
        )
        self.assertTrue(any("unsafe component root" in error for error in errors))

    def test_agent_review_is_not_silently_human_approval(self) -> None:
        self.assertTrue(
            all(case["review_status"] == "agent_reviewed" for case in self.document["cases"])
        )


if __name__ == "__main__":
    unittest.main()
