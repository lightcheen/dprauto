import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.python import PythonProjectParser
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.models import ToolResult
from dprauto.agent.state import create_agent_state
from dprauto.agent.tools.registry import ToolRegistry
from dprauto.agent.workflow import AgentWorkflow
from dprauto.application.regression import PersistedRegressionChecker
from dprauto.config import AgentConfig
from dprauto.domain.enums import (
    AgentPhase,
    BuildStage,
    BuildStatus,
    CommandPurpose,
    FailureCategory,
    ProjectType,
    RegressionStatus,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandResult,
    CommandSpec,
    ProjectProfile,
    SourceReference,
    VerificationCheck,
    VerificationReport,
    VerificationResult,
)


EXPERIMENT_RESULT = Path(
    "/home/master/auto-build/CNB/cnb-benchmark/results/heragent-same-experiment/"
    "envbench-python-paper.jsonl"
)
OPENLANE_LOG = Path(
    "/home/master/auto-build/CNB/cnb-benchmark/results/heragent-same-experiment/logs/"
    "envbench-python-paper-the-openroad-project-openlane-e0d2e618a883.test.1.log"
)
OPENLANE_PROJECT = Path(
    "/home/master/auto-build/CNB/cnb-benchmark/work/heragent-same-experiment/repos/"
    "envbench-python-paper-the-openroad-project-openlane-e0d2e618a883"
)


def verification(level, status, *checks, identity="verification"):
    return VerificationResult(identity, level, status, checks=tuple(checks))


def check(check_id, status):
    return VerificationCheck(check_id, status, check_id=check_id)


def report(report_id, attempt_id, *, install, test_a, test_b, run):
    return VerificationReport(
        report_id,
        attempt_id,
        (
            verification(
                VerificationLevel.INSTALLABILITY,
                install,
                check("install", install),
                identity=f"{report_id}-install",
            ),
            verification(
                VerificationLevel.TESTABILITY,
                (
                    VerificationStatus.PASSED
                    if test_a is test_b is VerificationStatus.PASSED
                    else VerificationStatus.FAILED
                ),
                check("test-a", test_a),
                check("test-b", test_b),
                identity=f"{report_id}-test",
            ),
            verification(
                VerificationLevel.RUNNABILITY,
                run,
                check("run", run),
                identity=f"{report_id}-run",
            ),
        ),
    )


class RegressionCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = LocalArtifactStorage(Path(self.temporary.name) / "artifacts")
        self.checker = PersistedRegressionChecker(self.storage)
        self.profile = ProjectProfile(
            "regression-project",
            SourceReference("fixture://regression"),
            languages=("Python",),
            project_type=ProjectType.SCRIPT,
        )
        self.before = report(
            "before",
            "attempt-before",
            install=VerificationStatus.PASSED,
            test_a=VerificationStatus.PASSED,
            test_b=VerificationStatus.PASSED,
            run=VerificationStatus.FAILED,
        )
        self.baseline = self.checker.create_baseline(self.profile, self.before)

    def test_all_previous_passes_must_be_rerun_before_acceptance(self) -> None:
        after = report(
            "after",
            "attempt-after",
            install=VerificationStatus.PASSED,
            test_a=VerificationStatus.PASSED,
            test_b=VerificationStatus.PASSED,
            run=VerificationStatus.PASSED,
        )
        result = self.checker.check(self.baseline, after)
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, RegressionStatus.PASSED)
        self.assertEqual(len(result.findings), 3)
        self.assertNotIn("runnability:run", [item.expectation.identity for item in result.findings])
        self.assertTrue(self.storage.exists(result.artifacts[0]))

    def test_previous_pass_becoming_failure_is_regression(self) -> None:
        after = report(
            "after-regression",
            "attempt-after",
            install=VerificationStatus.PASSED,
            test_a=VerificationStatus.FAILED,
            test_b=VerificationStatus.PASSED,
            run=VerificationStatus.PASSED,
        )
        result = self.checker.check(self.baseline, after)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, RegressionStatus.REGRESSION)
        self.assertEqual(
            [item.expectation.identity for item in result.regressions],
            ["testability:test-a"],
        )

    def test_missing_or_skipped_previous_pass_is_incomplete(self) -> None:
        current = VerificationReport(
            "after-missing",
            "attempt-after",
            (
                verification(
                    VerificationLevel.INSTALLABILITY,
                    VerificationStatus.PASSED,
                    check("install", VerificationStatus.PASSED),
                ),
                verification(
                    VerificationLevel.TESTABILITY,
                    VerificationStatus.PASSED,
                    check("test-a", VerificationStatus.PASSED),
                ),
                verification(
                    VerificationLevel.RUNNABILITY,
                    VerificationStatus.PASSED,
                    check("run", VerificationStatus.PASSED),
                ),
            ),
        )
        result = self.checker.check(self.baseline, current)
        self.assertEqual(result.status, RegressionStatus.INCOMPLETE)
        self.assertFalse(result.accepted)
        missing = next(item for item in result.findings if item.expectation.check_id == "test-b")
        self.assertIsNone(missing.current_status)

    @unittest.skipUnless(
        EXPERIMENT_RESULT.is_file() and OPENLANE_LOG.is_file() and OPENLANE_PROJECT.is_dir(),
        "local CNB experiment result and OpenLane checkout are required",
    )
    def test_real_openlane_failure_is_not_mislabeled_as_regression(self) -> None:
        record = next(
            json.loads(line)
            for line in EXPERIMENT_RESULT.read_text(encoding="utf-8").splitlines()
            if '"repo": "The-OpenROAD-Project/OpenLane"' in line
        )
        log = OPENLANE_LOG.read_text(encoding="utf-8")
        self.assertEqual(record["installability"]["status"], "success")
        self.assertEqual(record["testability"]["status"], "failure")
        self.assertEqual(record["testability"]["commands"][0]["returncode"], 127)
        self.assertIn("python: not found", log)
        parsed = PythonProjectParser().parse(
            SourceReference("https://github.com/The-OpenROAD-Project/OpenLane", record["ref"]),
            OPENLANE_PROJECT,
        )
        self.assertEqual(parsed.source.revision, record["ref"])

        install = verification(
            VerificationLevel.INSTALLABILITY,
            VerificationStatus.PASSED,
            VerificationCheck("cnb-install", VerificationStatus.PASSED, check_id="cnb-install"),
            identity="openlane-install",
        )
        failed_test = verification(
            VerificationLevel.TESTABILITY,
            VerificationStatus.FAILED,
            VerificationCheck(
                "pyright",
                VerificationStatus.FAILED,
                summary=log.strip(),
                check_id="pyright",
            ),
            identity="openlane-test",
        )
        historical = VerificationReport(
            "openlane-historical", "openlane-attempt", (install, failed_test)
        )
        baseline = self.checker.create_baseline(parsed, historical)
        comparison = self.checker.check(baseline, historical)
        self.assertEqual(comparison.status, RegressionStatus.PASSED)
        self.assertEqual(
            [item.expectation.identity for item in comparison.findings],
            ["installability:cnb-install"],
        )


class StaticTool:
    def __init__(self, name, callback):
        self.name = name
        self.description = name
        self.callback = callback

    def invoke(self, arguments, context):
        return self.callback(arguments, context)


class UnusedPlanner:
    def analyze_failure(self, state, observations):
        raise AssertionError("planner should not be called after regression hard stop")

    def plan_fix(self, state, diagnosis, available_tools):
        raise AssertionError("planner should not be called after regression hard stop")


class FixedVerificationRunner:
    def __init__(self, value):
        self.value = value

    def verify(self, profile, build_result, workspace, *, build_plan=None, deadline_at=None):
        return self.value


class AgentRegressionAcceptanceTests(unittest.TestCase):
    def test_agent_does_not_accept_a_fix_that_breaks_previous_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            checker = PersistedRegressionChecker(storage)
            profile = ProjectProfile(
                "agent-regression",
                SourceReference("fixture://agent-regression"),
                project_type=ProjectType.SCRIPT,
            )
            before = report(
                "agent-before",
                "attempt-before",
                install=VerificationStatus.PASSED,
                test_a=VerificationStatus.PASSED,
                test_b=VerificationStatus.PASSED,
                run=VerificationStatus.FAILED,
            )
            after = report(
                "agent-after",
                "attempt-after",
                install=VerificationStatus.PASSED,
                test_a=VerificationStatus.FAILED,
                test_b=VerificationStatus.PASSED,
                run=VerificationStatus.PASSED,
            )
            baseline = checker.create_baseline(profile, before)
            build_command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
            plan = BuildPlan(
                "plan",
                profile.project_id,
                "fake",
                (BuildStep("build", BuildStage.BUILD, build_command),),
            )
            build_result = BuildResult(
                "attempt-after",
                plan.plan_id,
                BuildStatus.SUCCEEDED,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                exit_code=0,
                image_reference="fake:image",
                command_results=(CommandResult(build_command, 0),),
            )
            tools = ToolRegistry(
                (
                    StaticTool("read_file", lambda arguments, context: ToolResult("read_file", True, "ok")),
                    StaticTool("get_build_log", lambda arguments, context: ToolResult("get_build_log", True, "ok")),
                    StaticTool(
                        "build_image",
                        lambda arguments, context: ToolResult(
                            "build_image",
                            True,
                            "built",
                            build_plan=plan,
                            build_result=build_result,
                        ),
                    ),
                )
            )
            workflow = AgentWorkflow(
                UnusedPlanner(),
                tools,
                storage,
                AgentConfig(max_attempts=1, max_repeated_failures=1, max_total_seconds=60),
                verification_runner=FixedVerificationRunner(after),
                regression_checker=checker,
            )
            state = create_agent_state("agent-regression-run")
            state.update(
                project_profile=profile,
                regression_baseline=baseline,
                attempt_number=1,
            )
            final = workflow.run(state, root)
            self.assertEqual(final["phase"], AgentPhase.STOPPED)
            self.assertEqual(final["failure"].category, FailureCategory.REGRESSION)
            self.assertEqual(final["regression_result"].status, RegressionStatus.REGRESSION)
            self.assertNotEqual(final["phase"], AgentPhase.COMPLETED)


if __name__ == "__main__":
    unittest.main()
