import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.application import DeterministicBuildService, create_deterministic_builder
from dprauto.config import BuildConfig
from dprauto.domain.enums import (
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    CommandPurpose,
    FailureCategory,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandSpec,
    FailureInfo,
    GeneratedFile,
    ProjectProfile,
    SourceReference,
)
from dprauto.strategies import StrategyRegistry


class ScriptedStrategy:
    def __init__(self, name, status, failure=None, *, repair_surface=False):
        self.name = name
        self.status = status
        self.failure = failure
        self.repair_surface = repair_surface
        self.build_calls = 0

    def supports(self, profile):
        return True

    def create_plan(self, profile):
        command = CommandSpec(
            (self.name, "build"),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=30,
        )
        return BuildPlan(
            f"{self.name}-plan",
            profile.project_id,
            self.name,
            (BuildStep("build", BuildStage.BUILD, command),),
            generated_files=(
                (GeneratedFile("Dockerfile", "FROM scratch\n"),)
                if self.repair_surface
                else ()
            ),
        )

    def build(self, plan, workspace, *, deadline_at=None):
        self.build_calls += 1
        now = datetime.now(timezone.utc)
        return BuildResult(
            f"{self.name}-attempt-{self.build_calls}",
            plan.plan_id,
            self.status,
            now,
            now,
            exit_code=0 if self.status is BuildStatus.SUCCEEDED else 1,
            failed_stage=(
                None if self.status is BuildStatus.SUCCEEDED else BuildStage.BUILD
            ),
            image_reference=(
                f"example/{self.name}:test"
                if self.status is BuildStatus.SUCCEEDED
                else None
            ),
            summary=f"{self.name} {self.status.value}",
            metadata={"scripted_failure": self.failure},
        )


class SequencedStrategy(ScriptedStrategy):
    def __init__(self, name, outcomes, *, repair_surface=False):
        status, failure = outcomes[0]
        super().__init__(name, status, failure, repair_surface=repair_surface)
        self.outcomes = outcomes

    def build(self, plan, workspace, *, deadline_at=None):
        index = min(self.build_calls, len(self.outcomes) - 1)
        self.status, self.failure = self.outcomes[index]
        return super().build(plan, workspace, deadline_at=deadline_at)


class ScriptedClassifier:
    def classify(self, profile, plan, result):
        return result.metadata.get("scripted_failure")


def project_failure(name="project failure", *, confidence=1.0, retryable=False):
    return FailureInfo(
        FailureCategory.BUILD_COMMAND,
        BuildStage.BUILD,
        name,
        name.replace(" ", "-"),
        retryable=retryable,
        confidence=confidence,
    )


def infrastructure_failure():
    return FailureInfo(
        FailureCategory.DOCKER,
        BuildStage.BUILD,
        "docker unavailable",
        "docker-unavailable",
        kind=BuildFailureKind.DOCKER_INFRASTRUCTURE,
        infrastructure_related=True,
    )


def network_failure(fingerprint="network-timeout"):
    return FailureInfo(
        FailureCategory.NETWORK,
        BuildStage.BUILD,
        "transient network failure",
        fingerprint,
        kind=BuildFailureKind.NETWORK,
        retryable=True,
        infrastructure_related=True,
    )


class DeterministicBuildServiceTests(unittest.TestCase):
    def test_portfolio_falls_back_after_project_failure_and_records_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            docker = ScriptedStrategy("docker", BuildStatus.FAILED, project_failure())
            template = ScriptedStrategy("template", BuildStatus.SUCCEEDED)
            cnb = ScriptedStrategy("cnb", BuildStatus.SUCCEEDED)
            builder = DeterministicBuildService(
                StrategyRegistry((docker, template, cnb)),
                ScriptedClassifier(),
                config=BuildConfig(max_strategy_attempts=3),
                storage=storage,
            )

            execution = builder.build(
                ProjectProfile("portfolio", SourceReference("fixture")), root
            )

            self.assertEqual(execution.plan.strategy, "template")
            self.assertEqual(
                tuple(attempt.plan.strategy for attempt in execution.attempts),
                ("docker", "template"),
            )
            self.assertEqual(cnb.build_calls, 0)
            self.assertIsNone(execution.failure)
            self.assertEqual(len(execution.artifacts), 1)
            selection = json.loads(storage.load(execution.artifacts[0]))
            self.assertEqual(selection["strategy_order"], ["docker", "template"])
            self.assertEqual(selection["selected_strategy"], "template")
            self.assertEqual(
                selection["portfolio_result"]["metadata"]["portfolio_strategy_order"],
                ["docker", "template"],
            )

    def test_portfolio_stops_on_infrastructure_failure(self) -> None:
        first = ScriptedStrategy(
            "docker", BuildStatus.FAILED, infrastructure_failure()
        )
        second = ScriptedStrategy("template", BuildStatus.SUCCEEDED)
        builder = DeterministicBuildService(
            StrategyRegistry((first, second)),
            ScriptedClassifier(),
        )

        execution = builder.build(
            ProjectProfile("infra", SourceReference("fixture")), Path.cwd()
        )

        self.assertEqual(tuple(item.plan.strategy for item in execution.attempts), ("docker",))
        self.assertEqual(second.build_calls, 0)
        self.assertTrue(execution.failure.infrastructure_related)

    def test_transient_network_failure_retries_same_strategy_once_and_records_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            first = SequencedStrategy(
                "template",
                (
                    (BuildStatus.FAILED, network_failure()),
                    (BuildStatus.SUCCEEDED, None),
                ),
            )
            fallback = ScriptedStrategy("cnb", BuildStatus.SUCCEEDED)
            builder = DeterministicBuildService(
                StrategyRegistry((first, fallback)),
                ScriptedClassifier(),
                config=BuildConfig(max_transient_retries=1),
                storage=storage,
            )

            execution = builder.build(
                ProjectProfile("network-retry", SourceReference("fixture")),
                root,
            )

            self.assertEqual(first.build_calls, 2)
            self.assertEqual(fallback.build_calls, 0)
            self.assertEqual(len(execution.attempts), 2)
            self.assertEqual(execution.attempts[1].strategy_attempt_number, 2)
            self.assertEqual(
                execution.attempts[1].retry_of_attempt_id,
                execution.attempts[0].result.attempt_id,
            )
            self.assertIn("after 1 transient retry", execution.selection_reason)
            selection = json.loads(storage.load(execution.artifacts[0]))
            self.assertEqual(selection["attempt_sequence"][1]["strategy_attempt_number"], 2)
            self.assertEqual(
                selection["attempt_sequence"][1]["retry_of_attempt_id"],
                execution.attempts[0].result.attempt_id,
            )
            self.assertEqual(
                selection["portfolio_result"]["metadata"][
                    "portfolio_transient_retry_count"
                ],
                1,
            )

    def test_repeated_network_failure_stops_after_bounded_retry(self) -> None:
        first = SequencedStrategy(
            "template",
            (
                (BuildStatus.FAILED, network_failure("network-first")),
                (BuildStatus.FAILED, network_failure("network-second")),
                (BuildStatus.SUCCEEDED, None),
            ),
        )
        fallback = ScriptedStrategy("cnb", BuildStatus.SUCCEEDED)
        builder = DeterministicBuildService(
            StrategyRegistry((first, fallback)),
            ScriptedClassifier(),
            config=BuildConfig(max_transient_retries=1),
        )

        execution = builder.build(
            ProjectProfile("network-exhausted", SourceReference("fixture")),
            Path.cwd(),
        )

        self.assertEqual(first.build_calls, 2)
        self.assertEqual(fallback.build_calls, 0)
        self.assertEqual(len(execution.attempts), 2)
        self.assertIn("transient retry exhausted", execution.selection_reason)

    def test_transient_retry_can_be_disabled(self) -> None:
        first = SequencedStrategy(
            "template",
            (
                (BuildStatus.FAILED, network_failure()),
                (BuildStatus.SUCCEEDED, None),
            ),
        )
        builder = DeterministicBuildService(
            StrategyRegistry((first,)),
            ScriptedClassifier(),
            config=BuildConfig(max_transient_retries=0),
        )

        execution = builder.build(
            ProjectProfile("network-no-retry", SourceReference("fixture")),
            Path.cwd(),
        )

        self.assertEqual(first.build_calls, 1)
        self.assertEqual(len(execution.attempts), 1)

    def test_retryable_project_failure_is_not_rerun_unchanged(self) -> None:
        first = ScriptedStrategy(
            "template",
            BuildStatus.FAILED,
            project_failure("repairable project error", retryable=True),
        )
        builder = DeterministicBuildService(
            StrategyRegistry((first,)),
            ScriptedClassifier(),
            config=BuildConfig(max_transient_retries=3),
        )

        execution = builder.build(
            ProjectProfile("project-no-blind-retry", SourceReference("fixture")),
            Path.cwd(),
        )

        self.assertEqual(first.build_calls, 1)
        self.assertEqual(len(execution.attempts), 1)

    def test_portfolio_stops_on_timeout(self) -> None:
        first = ScriptedStrategy(
            "docker", BuildStatus.TIMED_OUT, project_failure("timeout")
        )
        second = ScriptedStrategy("template", BuildStatus.SUCCEEDED)
        builder = DeterministicBuildService(
            StrategyRegistry((first, second)),
            ScriptedClassifier(),
        )

        execution = builder.build(
            ProjectProfile("timeout", SourceReference("fixture")), Path.cwd()
        )

        self.assertEqual(execution.result.status, BuildStatus.TIMED_OUT)
        self.assertEqual(second.build_calls, 0)

    def test_portfolio_can_be_disabled_for_legacy_single_selection(self) -> None:
        first = ScriptedStrategy("docker", BuildStatus.FAILED, project_failure())
        second = ScriptedStrategy("template", BuildStatus.SUCCEEDED)
        builder = DeterministicBuildService(
            StrategyRegistry((first, second)),
            ScriptedClassifier(),
            config=BuildConfig(strategy_portfolio_enabled=False),
        )

        execution = builder.build(
            ProjectProfile("single", SourceReference("fixture")), Path.cwd()
        )

        self.assertEqual(len(execution.attempts), 1)
        self.assertEqual(second.build_calls, 0)

    def test_portfolio_honors_maximum_strategy_attempts(self) -> None:
        strategies = tuple(
            ScriptedStrategy(name, BuildStatus.FAILED, project_failure(name))
            for name in ("one", "two", "three")
        )
        builder = DeterministicBuildService(
            StrategyRegistry(strategies),
            ScriptedClassifier(),
            config=BuildConfig(max_strategy_attempts=2),
        )

        execution = builder.build(
            ProjectProfile("bounded", SourceReference("fixture")), Path.cwd()
        )

        self.assertEqual(
            tuple(item.plan.strategy for item in execution.attempts), ("one", "two")
        )
        self.assertEqual(strategies[2].build_calls, 0)
        self.assertIn("maximum strategy attempts", execution.selection_reason)

    def test_failed_portfolio_selects_repairable_plan_instead_of_last_attempt(self) -> None:
        docker = ScriptedStrategy(
            "docker", BuildStatus.FAILED, project_failure("docker", confidence=0.7)
        )
        template = ScriptedStrategy(
            "template",
            BuildStatus.FAILED,
            project_failure("template", confidence=0.8),
            repair_surface=True,
        )
        cnb = ScriptedStrategy(
            "cnb", BuildStatus.FAILED, project_failure("cnb", confidence=0.99)
        )
        builder = DeterministicBuildService(
            StrategyRegistry((docker, template, cnb)),
            ScriptedClassifier(),
        )

        execution = builder.build(
            ProjectProfile("repair-baseline", SourceReference("fixture")), Path.cwd()
        )

        self.assertEqual(len(execution.attempts), 3)
        self.assertEqual(execution.plan.strategy, "template")
        self.assertIn("selected template as repair baseline", execution.selection_reason)

    def test_missing_docker_binary_is_infrastructure_not_project_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            storage = LocalArtifactStorage(root / "artifacts")
            builder = create_deterministic_builder(
                storage,
                BuildConfig(docker_binary="dprauto-docker-that-does-not-exist"),
            )
            profile = ProjectProfile(
                "missing-docker",
                SourceReference("fixture"),
                languages=("Python",),
                dockerfiles=("Dockerfile",),
            )

            execution = builder.build(profile, root)

            self.assertEqual(execution.result.status, BuildStatus.FAILED)
            self.assertEqual(execution.result.exit_code, 127)
            self.assertEqual(execution.failure.kind, BuildFailureKind.DOCKER_INFRASTRUCTURE)
            self.assertEqual(execution.failure.category, FailureCategory.DOCKER)


if __name__ == "__main__":
    unittest.main()
