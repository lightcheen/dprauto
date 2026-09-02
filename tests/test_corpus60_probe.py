import ast
import unittest
from pathlib import Path

from dprauto.domain.enums import BuildStage, CommandPurpose
from dprauto.domain.models import (
    BuildPlan,
    BuildStep,
    CommandSpec,
    ProjectProfile,
    SourceReference,
)
from evaluations.corpus60.probe_dprauto import probe


class StaticParser:
    def parse(self, source: SourceReference, root: Path) -> ProjectProfile:
        return ProjectProfile(
            "fixture",
            source,
            languages=("Python",),
            metadata={"parser_registry_selection": "fixture"},
        )


class RecordingPlanner:
    def __init__(self) -> None:
        self.profiles: list[ProjectProfile] = []

    def plan(self, profile: ProjectProfile) -> tuple[BuildPlan, ...]:
        self.profiles.append(profile)
        command = CommandSpec(
            ("docker", "build", "."),
            purpose=CommandPurpose.BUILD,
        )
        return (
            BuildPlan(
                "fixture-plan",
                profile.project_id,
                "production-selected",
                (BuildStep("build", BuildStage.BUILD, command),),
            ),
        )


class Corpus60ProbeTests(unittest.TestCase):
    def test_probe_uses_production_planner_not_manifest_language(self) -> None:
        planner = RecordingPlanner()
        case = {
            "case_id": "fixture",
            "language": "cpp",
            "repository": "example/fixture",
            "revision": "a" * 40,
            "url": "https://example.invalid/fixture.git",
            "local_path": "sources/fixture",
        }

        result = probe(case, StaticParser(), planner)  # type: ignore[arg-type]

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["strategy"], "production-selected")
        self.assertEqual(result["strategy_candidates"], ["production-selected"])
        self.assertEqual(len(planner.profiles), 1)

    def test_probe_does_not_import_concrete_build_strategies(self) -> None:
        source_path = Path(__file__).parents[1] / "evaluations/corpus60/probe_dprauto.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertFalse(any(module.startswith("dprauto.strategies") for module in imports))


if __name__ == "__main__":
    unittest.main()
