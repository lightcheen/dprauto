import unittest

from dprauto.agent.search_space import (
    bounded_repair_search_space,
    dependency_candidates,
    failure_family,
)
from dprauto.domain.enums import BuildStage, FailureCategory
from dprauto.domain.models import FailureInfo


def specifications():
    return {
        name: {"effect": "mutate", "argument_schema": {"type": "object"}}
        for name in (
            "modify_build_script",
            "patch_base_image",
            "patch_build_script",
            "patch_python_dependencies",
            "patch_system_packages",
            "patch_verification_dependencies",
        )
    }


class RepairSearchSpaceTests(unittest.TestCase):
    def test_test_dependency_failure_exposes_only_verification_overlay(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability failed",
            "test:missing-plugin",
            key_log="ModuleNotFoundError: No module named 'pytest_mock'",
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tuple(tools), ("patch_verification_dependencies",))
        self.assertEqual(metadata["preferred_tools"], ("patch_verification_dependencies",))
        self.assertIn("patch_python_dependencies", metadata["excluded_tools"])

    def test_non_dependency_test_failure_has_no_environment_mutation_space(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability failed",
            "test:capture",
            key_log="ValueError: I/O operation on closed file",
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tools, {})
        self.assertIn("no dependency-shaped", metadata["evidence_signals"][0])

    def test_unknown_mutation_tool_is_not_admitted_for_generic_failure(self) -> None:
        failure = FailureInfo(
            FailureCategory.UNKNOWN,
            BuildStage.BUILD,
            "unclassified build failure",
            "unknown:build",
        )
        available = specifications()
        available["future_unreviewed_mutation"] = {
            "effect": "mutate",
            "argument_schema": {"type": "object"},
        }

        metadata, tools = bounded_repair_search_space(failure, available)

        self.assertNotIn("future_unreviewed_mutation", tools)
        self.assertIn("future_unreviewed_mutation", metadata["excluded_tools"])

    def test_package_named_collection_warning_exposes_only_overlay(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "collection failed",
            "test:multipart-warning",
            key_log=(
                "PendingDeprecationWarning: Please use import python_multipart instead"
            ),
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tuple(tools), ("patch_verification_dependencies",))
        self.assertIn("compatibility warning", metadata["evidence_signals"][0])

    def test_timeout_exposes_only_cost_reduction_fallback(self) -> None:
        failure = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.DEPENDENCY_INSTALLATION,
            "Build command timed out",
            "timeout:install",
            evidence=("timeout_profile=python-package-install",),
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(
            tuple(tools),
            ("modify_build_script", "patch_build_script"),
        )
        self.assertEqual(metadata["preferred_tools"], ("patch_build_script",))

    def test_system_and_python_failures_have_distinct_spaces(self) -> None:
        system = FailureInfo(
            FailureCategory.SYSTEM_DEPENDENCY,
            BuildStage.BUILD,
            "header missing",
            "system:header",
            key_log="fatal error: libpq-fe.h: No such file or directory",
        )
        python = FailureInfo(
            FailureCategory.PYTHON_DEPENDENCY,
            BuildStage.BUILD,
            "module missing",
            "python:module",
            key_log="ModuleNotFoundError: No module named 'yaml'",
        )

        _, system_tools = bounded_repair_search_space(system, specifications())
        _, python_tools = bounded_repair_search_space(python, specifications())

        self.assertEqual(
            tuple(system_tools),
            ("modify_build_script", "patch_build_script", "patch_system_packages"),
        )
        self.assertEqual(
            tuple(python_tools),
            ("modify_build_script", "patch_build_script", "patch_python_dependencies"),
        )

    def test_failure_family_ignores_volatile_numbers_but_keeps_missing_module(self) -> None:
        first = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "command failed after 12.3 seconds",
            "volatile:first",
            key_log="RuntimeError: worker 17 failed",
        )
        second = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "command failed after 99.8 seconds",
            "volatile:second",
            key_log="RuntimeError: worker 42 failed",
        )
        missing = FailureInfo(
            FailureCategory.PYTHON_DEPENDENCY,
            BuildStage.BUILD,
            "module missing",
            "missing:yaml",
            key_log="ModuleNotFoundError: No module named 'yaml.loader'",
        )

        self.assertEqual(failure_family(first), failure_family(second))
        self.assertIn("missing-module:yaml", failure_family(missing))

    def test_import_symbol_failure_names_the_owning_distribution(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "collection failed",
            "test:ratelimiter-symbol",
            key_log=(
                "from requests_ratelimiter import LimiterMixin, MemoryQueueBucket\n"
                "ImportError: cannot import name 'MemoryQueueBucket' from "
                "'requests_ratelimiter'"
            ),
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tuple(tools), ("patch_verification_dependencies",))
        self.assertEqual(dependency_candidates(failure), ("requests-ratelimiter",))
        self.assertEqual(
            metadata["dependency_candidates"],
            ("requests-ratelimiter",),
        )

    def test_controlled_pytest_conflict_does_not_enter_llm_mutation_space(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "dependency installation failed",
            "test:pytest-conflict",
            key_log=(
                "ERROR: Cannot install pytest==8.2.1 and pytest==8.3.5 because "
                "these package versions have conflicting dependencies.\n"
                "ERROR: ResolutionImpossible"
            ),
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tools, {})
        self.assertEqual(metadata["dependency_candidates"], ("pytest",))
        self.assertIn("controlled test-runner", metadata["evidence_signals"][0])

    def test_extra_install_suggestion_names_base_distribution(self) -> None:
        failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "PostgreSQL driver missing",
            "test:piccolo-postgres",
            key_log=(
                "ModuleNotFoundError: PostgreSQL driver not found. "
                "Try running `pip install 'piccolo[postgres]'`"
            ),
        )

        metadata, tools = bounded_repair_search_space(failure, specifications())

        self.assertEqual(tuple(tools), ("patch_verification_dependencies",))
        self.assertEqual(metadata["dependency_candidates"], ("piccolo",))


if __name__ == "__main__":
    unittest.main()
