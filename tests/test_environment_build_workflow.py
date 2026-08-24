import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.persistence import SQLiteAgentPersistence
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.full_workflow import EnvironmentBuildWorkflow
from dprauto.agent.models import FixPlan, ToolCall, ToolResult
from dprauto.agent.tools import ToolRegistry
from dprauto.agent.workflow import AgentWorkflow
from dprauto.application.regression import PersistedRegressionChecker
from dprauto.config import AgentConfig
from dprauto.domain.enums import (
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    ChangeKind,
    CommandPurpose,
    EnvironmentBuildStatus,
    FailureCategory,
    RegressionStatus,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    EnvironmentDiff,
    FailureInfo,
    FileChange,
    ProjectProfile,
    RepairPreflightCheck,
    RepairPreflightResult,
    SourceReference,
    VerificationCheck,
    VerificationReport,
    VerificationResult,
)


class StaticTool:
    def __init__(self, name, callback):
        self.name = name
        self.description = name
        self.callback = callback

    def invoke(self, arguments, context):
        return self.callback(arguments, context)


class SequencedPlanner:
    def __init__(self):
        self.analysis_calls = 0
        self.plan_calls = 0

    def analyze_failure(self, state, observations):
        self.analysis_calls += 1
        return f"diagnosis-{self.analysis_calls}"

    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        return FixPlan(
            f"repair hypothesis {self.plan_calls}",
            (
                ToolCall(
                    "modify_build_script",
                    {"round": self.plan_calls},
                    "change only the environment build script",
                ),
            ),
            f"repair plan {self.plan_calls}",
        )


class SequencedVerifier:
    def __init__(self, reports):
        self.reports = list(reports)
        self.calls = 0

    def verify(self, profile, build_result, workspace, *, build_plan=None, deadline_at=None):
        if self.calls >= len(self.reports):
            raise AssertionError("unexpected verification call")
        report = self.reports[self.calls]
        self.calls += 1
        return report


class RejectingPreflight:
    def run(self, workspace, environment_diff, *, deadline_at=None):
        return RepairPreflightResult(
            VerificationStatus.FAILED,
            (
                RepairPreflightCheck(
                    "dockerfile-check:Dockerfile",
                    VerificationStatus.FAILED,
                    "Dockerfile parse error line 2",
                ),
            ),
            "repair preflight rejected 1 explicit check failure(s)",
        )


class EnvironmentBuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
        (self.root / "setup.sh").write_text("python -m pip install .\n", encoding="utf-8")
        self.storage = LocalArtifactStorage(self.root / "artifacts")
        self.profile = ProjectProfile(
            "e2e-project",
            SourceReference("fixture-project", revision="fixture-revision"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
            build_files=("setup.sh",),
        )
        command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
        self.plan = BuildPlan(
            "e2e-plan",
            self.profile.project_id,
            "docker",
            (BuildStep("docker build", BuildStage.BUILD, command),),
            metadata={"dockerfile": "Dockerfile", "setup_script": "setup.sh"},
        )
        self.workflows = []
        self.addCleanup(self._close_workflows)

    def _close_workflows(self):
        for workflow in self.workflows:
            workflow.close()

    def _build_result(self, index, succeeded):
        now = datetime.now(timezone.utc)
        log = self.storage.save(
            f"builds/{index}.log",
            ("build succeeded" if succeeded else f"build failed {index}").encode(),
        )
        return BuildResult(
            f"attempt-{index}",
            self.plan.plan_id,
            BuildStatus.SUCCEEDED if succeeded else BuildStatus.FAILED,
            now,
            now,
            exit_code=0 if succeeded else 1,
            failed_stage=None if succeeded else BuildStage.BUILD,
            image_reference=f"fixture:e2e-{index}" if succeeded else None,
            logs=(log,),
            summary="build succeeded" if succeeded else "build failed",
        )

    def _timed_out_build_result(self, index):
        now = datetime.now(timezone.utc)
        command = self.plan.steps[0].command
        log = self.storage.save(
            f"builds/{index}.log",
            b"command timed out after 60 seconds",
        )
        return BuildResult(
            f"attempt-{index}",
            self.plan.plan_id,
            BuildStatus.TIMED_OUT,
            now,
            now,
            exit_code=None,
            failed_stage=BuildStage.BUILD,
            logs=(log,),
            command_results=(CommandResult(command, None, stdout=log, timed_out=True),),
            summary="docker build timed_out",
        )

    @staticmethod
    def _failure(index, kind=BuildFailureKind.PROJECT_BUILD):
        infrastructure = kind is not BuildFailureKind.PROJECT_BUILD
        category = {
            BuildFailureKind.GIT: FailureCategory.SOURCE,
            BuildFailureKind.NETWORK: FailureCategory.NETWORK,
            BuildFailureKind.DOCKER_INFRASTRUCTURE: FailureCategory.DOCKER,
        }.get(kind, FailureCategory.BUILD_COMMAND)
        return FailureInfo(
            category,
            BuildStage.BUILD,
            f"failure {index}",
            f"failure:{index}",
            kind=kind,
            key_log=f"bounded failure {index}",
            infrastructure_related=infrastructure,
            retryable=not infrastructure,
        )

    @staticmethod
    def _report(index, *, install="pass", test="pass", run="pass"):
        statuses = {
            "pass": VerificationStatus.PASSED,
            "fail": VerificationStatus.FAILED,
            "skip": VerificationStatus.SKIPPED,
        }
        values = (
            (VerificationLevel.INSTALLABILITY, "install", install),
            (VerificationLevel.TESTABILITY, "tests", test),
            (VerificationLevel.RUNNABILITY, "run", run),
        )
        results = tuple(
            VerificationResult(
                f"{name}-{index}",
                level,
                statuses[value],
                summary=f"{name} {value}",
                checks=(
                    VerificationCheck(
                        name,
                        statuses[value],
                        f"{name} {value}",
                        check_id=name,
                    ),
                ),
            )
            for level, name, value in values
        )
        return VerificationReport(f"report-{index}", f"attempt-{index}", results)

    @staticmethod
    def _assertion_report(index):
        command = CommandSpec(
            ("python -m pytest -q",),
            purpose=CommandPurpose.TEST,
            shell=True,
        )
        command_result = CommandResult(command, 1)
        results = (
            VerificationResult(
                f"install-{index}",
                VerificationLevel.INSTALLABILITY,
                VerificationStatus.PASSED,
                summary="install passed",
            ),
            VerificationResult(
                f"tests-{index}",
                VerificationLevel.TESTABILITY,
                VerificationStatus.FAILED,
                command_result=command_result,
                summary="project tests failed",
                metadata={"output_excerpt": "E assert 1 == 2\n1 failed in 0.10s"},
                checks=(
                    VerificationCheck(
                        "tests",
                        VerificationStatus.FAILED,
                        "project test command failed",
                        command_result=command_result,
                        metadata={
                            "output_excerpt": "AssertionError\n1 failed in 0.10s"
                        },
                    ),
                ),
            ),
            VerificationResult(
                f"run-{index}",
                VerificationLevel.RUNNABILITY,
                VerificationStatus.SKIPPED,
                summary="run skipped",
            ),
        )
        return VerificationReport(f"report-{index}", f"attempt-{index}", results)

    @staticmethod
    def _collection_warning_report(index):
        command = CommandSpec(
            ("python -m pytest -q",),
            purpose=CommandPurpose.TEST,
            shell=True,
        )
        command_result = CommandResult(command, 2)
        excerpt = (
            "ERROR collecting tests/test_form.py\n"
            "E PendingDeprecationWarning: Please use import python_multipart instead.\n"
            "================ short test summary info ================\n"
            "ERROR tests/test_form.py - PendingDeprecationWarning\n"
            "Interrupted: 1 error during collection"
        )
        results = (
            VerificationResult(
                f"install-{index}",
                VerificationLevel.INSTALLABILITY,
                VerificationStatus.PASSED,
                summary="install passed",
            ),
            VerificationResult(
                f"tests-{index}",
                VerificationLevel.TESTABILITY,
                VerificationStatus.FAILED,
                command_result=command_result,
                summary="project test collection failed",
                metadata={"output_excerpt": excerpt},
                checks=(
                    VerificationCheck(
                        "tests",
                        VerificationStatus.FAILED,
                        "project test command failed during collection",
                        command_result=command_result,
                        metadata={"output_excerpt": excerpt},
                    ),
                ),
            ),
            VerificationResult(
                f"run-{index}",
                VerificationLevel.RUNNABILITY,
                VerificationStatus.SKIPPED,
                summary="run skipped",
            ),
        )
        return VerificationReport(f"report-{index}", f"attempt-{index}", results)

    @staticmethod
    def _active_test_timeout_report(index):
        command = CommandSpec(
            ("python -m pip install '.[tests]' pytest==8.3.5 && pytest",),
            purpose=CommandPurpose.TEST,
            shell=True,
        )
        command_result = CommandResult(
            command,
            None,
            timed_out=True,
            duration_seconds=93.08,
        )
        test_check = VerificationCheck(
            "tests",
            VerificationStatus.FAILED,
            "project test command timed out",
            command_result=command_result,
            metadata={
                "output_excerpt": (
                    "asdf/_tests/core/test_integration.py .. [ 35%]\n"
                    "verification command timed out"
                )
            },
        )
        results = (
            VerificationResult(
                f"install-{index}",
                VerificationLevel.INSTALLABILITY,
                VerificationStatus.PASSED,
                summary="install passed",
            ),
            VerificationResult(
                f"tests-{index}",
                VerificationLevel.TESTABILITY,
                VerificationStatus.FAILED,
                command_result=command_result,
                summary="project tests timed out",
                checks=(test_check,),
            ),
            VerificationResult(
                f"run-{index}",
                VerificationLevel.RUNNABILITY,
                VerificationStatus.SKIPPED,
                summary="run skipped",
            ),
        )
        return VerificationReport(f"report-{index}", f"attempt-{index}", results)

    def _workflow(
        self,
        build_steps,
        reports,
        *,
        max_attempts=3,
        max_repeated_failures=10,
        preflight_runner=None,
    ):
        planner = SequencedPlanner()
        remaining = list(build_steps)

        def inspect(arguments, context):
            source = self.profile.source
            self.assertEqual(arguments["source"], source.locator)
            self.assertEqual(arguments["revision"], source.revision)
            return ToolResult(
                "inspect_project",
                True,
                "parsed Python fixture",
                data={"project_profile": self.profile},
            )

        def build(arguments, context):
            if not remaining:
                raise AssertionError("unexpected build call")
            result, failure = remaining.pop(0)
            return ToolResult(
                "build_image",
                failure is None,
                result.summary,
                build_plan=self.plan,
                build_result=result,
                failure=failure,
                artifacts=result.logs,
            )

        def modify(arguments, context):
            round_number = arguments["round"]
            content = f"FROM python:3.11-slim\n# repair {round_number}\n"
            target = Path(context.workspace) / "Dockerfile"
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            target.write_text(content, encoding="utf-8")
            after = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return ToolResult(
                "modify_build_script",
                True,
                f"updated Dockerfile in round {round_number}",
                environment_diff=EnvironmentDiff(
                    files=(
                        FileChange(
                            "Dockerfile",
                            ChangeKind.MODIFIED,
                            before,
                            after,
                        ),
                    ),
                    build_scripts=(
                        FileChange(
                            "Dockerfile",
                            ChangeKind.MODIFIED,
                            before,
                            after,
                        ),
                    ),
                    summary=f"updated Dockerfile in round {round_number}",
                ),
            )

        registry = ToolRegistry(
            (
                StaticTool("inspect_project", inspect),
                StaticTool(
                    "read_file",
                    lambda arguments, context: ToolResult(
                        "read_file", True, "read build script", data={"content": "fixture"}
                    ),
                ),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult(
                        "get_build_log", True, "read bounded log", data={"content": "failure"}
                    ),
                ),
                StaticTool("modify_build_script", modify),
                StaticTool("build_image", build),
            )
        )
        persistence = SQLiteAgentPersistence(
            self.root / f"state-{len(self.workflows)}.sqlite", self.storage
        )
        repair = AgentWorkflow(
            planner,
            registry,
            self.storage,
            AgentConfig(
                max_attempts=max_attempts,
                max_repeated_failures=max_repeated_failures,
                max_total_seconds=60,
            ),
            persistence,
            SequencedVerifier(reports),
            PersistedRegressionChecker(self.storage),
            preflight_runner=preflight_runner,
        )
        workflow = EnvironmentBuildWorkflow(repair, self.storage)
        self.workflows.append(workflow)
        return workflow, planner, persistence

    def _run(self, workflow, run_id):
        return workflow.run(run_id, self.profile.source, self.root)

    def test_standard_build_succeeds_without_agent(self):
        workflow, planner, persistence = self._workflow(
            [(self._build_result(0, True), None)],
            [self._report(0)],
        )
        expected_nodes = {
            "parse_project",
            "standard_build",
            "failure_classification",
            "failure_evaluation",
            "agent_repair_analyze",
            "agent_repair_plan",
            "agent_repair_apply",
            "repair_preflight",
            "rebuild",
            "verification",
            "regression_evaluation",
            "environment_diff_and_result",
        }
        self.assertTrue(expected_nodes <= set(workflow.graph.get_graph().nodes))

        result = self._run(workflow, "direct-success")

        self.assertTrue(result.succeeded)
        self.assertEqual(result.final_status, EnvironmentBuildStatus.SUCCEEDED)
        self.assertFalse(result.agent_participated)
        self.assertEqual(result.repair_attempts, 0)
        self.assertEqual(result.llm_call_count, 0)
        self.assertEqual(result.build_strategy, "docker")
        self.assertEqual(result.project_profile, self.profile)
        self.assertEqual(result.installability.status, VerificationStatus.PASSED)
        self.assertEqual(result.testability.status, VerificationStatus.PASSED)
        self.assertEqual(result.runnability.status, VerificationStatus.PASSED)
        self.assertEqual(
            {item.path for item in result.final_build_scripts},
            {"Dockerfile", "setup.sh"},
        )
        self.assertGreater(result.total_duration_seconds, -0.001)
        self.assertEqual(planner.analysis_calls, 0)
        self.assertGreater(persistence.checkpoint_count("direct-success"), 0)

    def test_standard_build_fails_then_agent_repairs_successfully(self):
        workflow, planner, _ = self._workflow(
            [
                (self._build_result(0, False), self._failure(0)),
                (self._build_result(1, True), None),
            ],
            [self._report(1)],
        )

        result = self._run(workflow, "one-round-success")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.SUCCEEDED)
        self.assertTrue(result.agent_participated)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(result.llm_call_count, 2)
        self.assertEqual(planner.plan_calls, 1)
        self.assertIn("# repair 1", result.final_build_scripts[0].content)
        self.assertEqual(len(result.environment_diffs), 1)

    def test_agent_succeeds_after_multiple_rounds(self):
        workflow, planner, _ = self._workflow(
            [
                (self._build_result(0, False), self._failure(0)),
                (self._build_result(1, False), self._failure(1)),
                (self._build_result(2, True), None),
            ],
            [self._report(2)],
        )

        result = self._run(workflow, "multi-round-success")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.SUCCEEDED)
        self.assertEqual(result.repair_attempts, 2)
        self.assertEqual(result.llm_call_count, 4)
        self.assertEqual(planner.analysis_calls, 2)
        self.assertEqual(len(result.environment_diffs), 2)

    def test_failed_preflight_routes_to_evaluation_without_rebuild(self):
        workflow, planner, _ = self._workflow(
            [(self._build_result(40, False), self._failure(40))],
            [],
            max_attempts=1,
            preflight_runner=RejectingPreflight(),
        )

        result = self._run(workflow, "preflight-rejection")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.MAX_ATTEMPTS)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(planner.plan_calls, 1)
        self.assertEqual(result.failure.failure_stage, BuildStage.PLANNING)
        self.assertIn("repair_preflight=true", result.failure.evidence)
        self.assertTrue(
            any(item.key.endswith("repair-preflight.json") for item in result.artifacts)
        )

    def test_agent_stops_at_maximum_rounds(self):
        workflow, planner, _ = self._workflow(
            [
                (self._build_result(0, False), self._failure(0)),
                (self._build_result(1, False), self._failure(1)),
                (self._build_result(2, False), self._failure(2)),
            ],
            [],
            max_attempts=2,
        )

        result = self._run(workflow, "max-rounds")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.MAX_ATTEMPTS)
        self.assertEqual(result.repair_attempts, 2)
        self.assertEqual(planner.plan_calls, 2)
        self.assertIn("maximum repair attempts", result.stop_reason)

    def test_agent_stops_when_the_same_failure_repeats(self):
        repeated = self._failure(99)
        workflow, planner, _ = self._workflow(
            [
                (self._build_result(20, False), repeated),
                (self._build_result(21, False), repeated),
                (self._build_result(22, False), repeated),
            ],
            [],
            max_attempts=5,
            max_repeated_failures=2,
        )

        result = self._run(workflow, "repeated-failure")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.PROJECT_FAILED)
        self.assertEqual(result.repair_attempts, 2)
        self.assertEqual(planner.plan_calls, 2)
        self.assertIn("same failure repeated", result.stop_reason)

    def test_network_and_docker_failures_are_infrastructure_failures(self):
        for index, kind in enumerate(
            (BuildFailureKind.NETWORK, BuildFailureKind.DOCKER_INFRASTRUCTURE)
        ):
            with self.subTest(kind=kind):
                workflow, planner, _ = self._workflow(
                    [(self._build_result(10 + index, False), self._failure(10 + index, kind))],
                    [],
                )
                result = self._run(workflow, f"infra-{kind.value}")
                self.assertEqual(
                    result.final_status, EnvironmentBuildStatus.INFRASTRUCTURE_FAILED
                )
                self.assertFalse(result.agent_participated)
                self.assertEqual(result.llm_call_count, 0)
                self.assertEqual(planner.plan_calls, 0)

    def test_active_download_timeout_is_budget_failure_without_agent(self):
        current_failure = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "Build command timed out",
            "timeout:active-download",
            evidence=("timeout_activity=dependency-download",),
            key_log="Downloading pandas-2.0.whl",
        )
        workflow, planner, _ = self._workflow(
            [(self._timed_out_build_result(30), current_failure)],
            [],
        )

        result = self._run(workflow, "active-download-timeout")

        self.assertEqual(
            result.final_status, EnvironmentBuildStatus.TIME_BUDGET_EXCEEDED
        )
        self.assertFalse(result.agent_participated)
        self.assertEqual(result.llm_call_count, 0)
        self.assertEqual(planner.analysis_calls, 0)
        self.assertIn("dependency download remained active", result.stop_reason)

    def test_active_test_timeout_is_budget_failure_without_agent(self):
        workflow, planner, _ = self._workflow(
            [(self._build_result(32, True), None)],
            [self._active_test_timeout_report(32)],
        )

        result = self._run(workflow, "active-test-timeout")

        self.assertEqual(
            result.final_status, EnvironmentBuildStatus.TIME_BUDGET_EXCEEDED
        )
        self.assertEqual(result.testability.status, VerificationStatus.FAILED)
        self.assertFalse(result.agent_participated)
        self.assertEqual(result.repair_attempts, 0)
        self.assertEqual(result.llm_call_count, 0)
        self.assertEqual(planner.analysis_calls, 0)
        self.assertIn("project tests remained active", result.stop_reason)

    def test_assertion_failure_is_outside_environment_repair_scope(self):
        workflow, planner, _ = self._workflow(
            [(self._build_result(31, True), None)],
            [self._assertion_report(31)],
        )

        result = self._run(workflow, "assertion-failure")

        self.assertEqual(
            result.final_status, EnvironmentBuildStatus.VERIFICATION_FAILED
        )
        self.assertFalse(result.agent_participated)
        self.assertEqual(result.llm_call_count, 0)
        self.assertEqual(planner.analysis_calls, 0)
        self.assertIn("test assertions failed", result.stop_reason)

    def test_collection_warning_is_eligible_for_environment_repair(self):
        workflow, planner, _ = self._workflow(
            [
                (self._build_result(33, True), None),
                (self._build_result(34, True), None),
            ],
            [self._collection_warning_report(33), self._report(34, test="fail")],
            max_attempts=1,
        )

        result = self._run(workflow, "collection-warning")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.VERIFICATION_FAILED)
        self.assertTrue(result.agent_participated)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(planner.analysis_calls, 1)
        self.assertNotIn("test assertions failed", result.stop_reason)

    def test_build_succeeds_but_tests_keep_failing(self):
        workflow, _, _ = self._workflow(
            [
                (self._build_result(0, True), None),
                (self._build_result(1, True), None),
            ],
            [self._report(0, test="fail"), self._report(1, test="fail")],
            max_attempts=1,
        )

        result = self._run(workflow, "test-failure")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.VERIFICATION_FAILED)
        self.assertEqual(result.testability.status, VerificationStatus.FAILED)
        self.assertEqual(result.build_result.status, BuildStatus.SUCCEEDED)
        self.assertTrue(result.agent_participated)

    def test_repair_timeout_after_successful_standard_build_is_regression(self):
        workflow, _, _ = self._workflow(
            [
                (self._build_result(0, True), None),
                (self._timed_out_build_result(1), self._failure(1)),
            ],
            [self._report(0, test="fail")],
            max_attempts=1,
        )

        result = self._run(workflow, "repair-timeout-regression")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.REGRESSION)
        # The rejected candidate remains diagnostic evidence, but the final
        # executable result is bound to the last accepted build.
        self.assertEqual(result.build_result.status, BuildStatus.SUCCEEDED)
        self.assertEqual(result.failure.category, FailureCategory.REGRESSION)
        self.assertIn("previous build succeeded", result.stop_reason)
        self.assertEqual(result.repair_attempts, 1)

    def test_tests_pass_but_program_does_not_run(self):
        workflow, _, _ = self._workflow(
            [
                (self._build_result(0, True), None),
                (self._build_result(1, True), None),
            ],
            [self._report(0, run="fail"), self._report(1, run="fail")],
            max_attempts=1,
        )

        result = self._run(workflow, "run-failure")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.VERIFICATION_FAILED)
        self.assertEqual(result.testability.status, VerificationStatus.PASSED)
        self.assertEqual(result.runnability.status, VerificationStatus.FAILED)

    def test_repair_that_breaks_a_previously_passing_test_is_regression(self):
        workflow, _, _ = self._workflow(
            [
                (self._build_result(0, True), None),
                (self._build_result(1, True), None),
            ],
            [self._report(0, run="fail"), self._report(1, test="fail", run="pass")],
            max_attempts=1,
        )

        result = self._run(workflow, "regression")

        self.assertEqual(result.final_status, EnvironmentBuildStatus.REGRESSION)
        self.assertIsNotNone(result.regression_result)
        self.assertEqual(result.regression_result.status, RegressionStatus.REGRESSION)
        self.assertFalse(result.regression_result.accepted)
        self.assertEqual(result.failure.category, FailureCategory.REGRESSION)


if __name__ == "__main__":
    unittest.main()
