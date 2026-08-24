import unittest
from datetime import datetime, timedelta, timezone

from dprauto.domain.enums import (
    BuildStage,
    BuildStatus,
    ChangeKind,
    CommandPurpose,
    FailureCategory,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    DependencyChange,
    EnvironmentDiff,
    FailureInfo,
    FileChange,
    ProjectProfile,
    SourceReference,
    ValueChange,
    VerificationResult,
    VerificationReport,
    VerificationCheck,
)
from dprauto.errors import ModelValidationError


class DomainModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = CommandSpec(
            argv=("build-tool", "build"),
            purpose=CommandPurpose.BUILD,
            timeout_seconds=60,
        )
        self.profile = ProjectProfile(
            project_id="example/project@revision",
            source=SourceReference("https://example.invalid/project.git", revision="revision"),
            languages=("language-a", "language-b"),
            package_managers=("package-manager",),
            dependency_files=("deps.lock",),
        )
        self.plan = BuildPlan(
            plan_id="plan-1",
            project_id=self.profile.project_id,
            strategy="test-strategy",
            steps=(BuildStep("build", BuildStage.BUILD, self.command),),
        )

    def test_core_models_can_be_constructed(self) -> None:
        self.assertEqual(self.profile.source.revision, "revision")
        self.assertEqual(self.plan.steps[0].command.argv[0], "build-tool")

    def test_unsafe_relative_paths_are_rejected(self) -> None:
        with self.assertRaises(ModelValidationError):
            SourceReference("source", subdirectory="../outside")

    def test_build_plan_requires_at_least_one_step(self) -> None:
        with self.assertRaises(ModelValidationError):
            BuildPlan("plan", "project", "strategy", ())

    def test_build_result_duration_and_status_are_consistent(self) -> None:
        started = datetime.now(timezone.utc)
        result = BuildResult(
            attempt_id="attempt-1",
            plan_id=self.plan.plan_id,
            status=BuildStatus.SUCCEEDED,
            started_at=started,
            finished_at=started + timedelta(seconds=3),
            exit_code=0,
        )
        self.assertEqual(result.duration_seconds, 3)

        with self.assertRaises(ModelValidationError):
            BuildResult(
                attempt_id="attempt-2",
                plan_id=self.plan.plan_id,
                status=BuildStatus.SUCCEEDED,
                started_at=started,
                finished_at=started,
                exit_code=1,
            )

    def test_failure_and_verification_results(self) -> None:
        failure = FailureInfo(
            category=FailureCategory.TOOLCHAIN,
            failure_stage=BuildStage.BUILD,
            message="tool is unavailable",
            fingerprint="toolchain:missing-tool",
            confidence=0.9,
        )
        command_result = CommandResult(self.command, exit_code=0, duration_seconds=0.1)
        verification = VerificationResult(
            verification_id="verification-1",
            level=VerificationLevel.INSTALLABILITY,
            status=VerificationStatus.PASSED,
            command_result=command_result,
        )
        self.assertEqual(failure.category, FailureCategory.TOOLCHAIN)
        self.assertEqual(failure.stage, BuildStage.BUILD)
        self.assertTrue(command_result.succeeded)
        self.assertTrue(verification.passed)
        report = VerificationReport("report", "attempt", (verification,))
        self.assertFalse(report.succeeded)

    def test_verification_check_identity_must_be_unique_per_layer(self) -> None:
        with self.assertRaises(ModelValidationError):
            VerificationResult(
                "duplicate-checks",
                VerificationLevel.TESTABILITY,
                VerificationStatus.PASSED,
                checks=(
                    VerificationCheck("test A", VerificationStatus.PASSED, check_id="same"),
                    VerificationCheck("test B", VerificationStatus.PASSED, check_id="same"),
                ),
            )

    def test_environment_diff_reports_changes(self) -> None:
        self.assertTrue(EnvironmentDiff().is_empty)
        diff = EnvironmentDiff(
            files=(FileChange("setup.sh", ChangeKind.MODIFIED, "old", "new"),),
            dependencies=(
                DependencyChange("ecosystem", "library", ChangeKind.ADDED, after="1.0"),
            ),
            base_image=ValueChange("base_image", "old@sha256:a", "new@sha256:b"),
        )
        self.assertFalse(diff.is_empty)


if __name__ == "__main__":
    unittest.main()
