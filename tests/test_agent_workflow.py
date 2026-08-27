import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.adapters.python import PythonEnvironmentDiffer
from dprauto.agent.context import method_fingerprint
from dprauto.agent.models import (
    AttemptedMethod,
    ContextSummary,
    FixPlan,
    InvestigationDecision,
    ToolCall,
    ToolContext,
    ToolResult,
)
from dprauto.agent.state import create_agent_state
from dprauto.agent.tools import (
    ListProjectFilesTool,
    ModifyBuildScriptTool,
    PatchBuildScriptTool,
    PatchPythonDependenciesTool,
    PatchSystemPackagesTool,
    PatchVerificationDependenciesTool,
    ReadFileTool,
    SearchProjectTool,
    ToolRegistry,
)
from dprauto.agent.workflow import AgentWorkflow
from dprauto.config import AgentConfig
from dprauto.domain.enums import (
    AgentPhase,
    BuildFailureKind,
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
    CommandSpec,
    CommandResult,
    EnvironmentDiff,
    FailureInfo,
    FileChange,
    GeneratedFile,
    ProjectProfile,
    RepairPreflightCheck,
    RepairPreflightResult,
    SourceReference,
    ValueChange,
    VerificationCheck,
    VerificationReport,
    VerificationResult,
)


class FixedPlanner:
    def __init__(self):
        self.analysis_calls = 0
        self.plan_calls = 0
        self.available_tools = []

    def analyze_failure(self, state, observations):
        self.analysis_calls += 1
        return "the same broken Dockerfile instruction remains"

    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        self.available_tools.append(available_tools)
        return FixPlan(
            diagnosis,
            (
                ToolCall(
                    "modify_build_script",
                    {
                        "path": "Dockerfile",
                        "content": f"FROM scratch\n# repair {self.plan_calls}\n",
                    },
                ),
            ),
            "retry a bounded environment-only edit",
        )


class ReadOnlyPlanner(FixedPlanner):
    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        self.available_tools.append(available_tools)
        return FixPlan(
            "inspect the current build script",
            (ToolCall("read_file", {"path": "Dockerfile"}),),
            "read the Dockerfile",
        )


class FeedbackCorrectingPlanner(FixedPlanner):
    def __init__(self):
        super().__init__()
        self.feedback = []

    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        self.available_tools.append(available_tools)
        self.feedback.append(state.get("plan_feedback", ()))
        if self.plan_calls == 1:
            return FixPlan(
                "guess a Python package",
                (
                    ToolCall(
                        "patch_python_dependencies",
                        {"path": "Dockerfile", "packages": ["libpq"]},
                    ),
                ),
            )
        return FixPlan(
            "install the evidenced system package",
            (
                ToolCall(
                    "patch_system_packages",
                    {
                        "path": "Dockerfile",
                        "package_manager": "apt",
                        "packages": ["libpq-dev"],
                    },
                ),
            ),
        )


class DuplicateCorrectingPlanner(FixedPlanner):
    def __init__(self):
        super().__init__()
        self.feedback = []

    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        self.feedback.append(state.get("plan_feedback", ()))
        suffix = "first" if self.plan_calls == 1 else "alternative"
        return FixPlan(
            f"{suffix} build script repair",
            (
                ToolCall(
                    "modify_build_script",
                    {
                        "path": "Dockerfile",
                        "content": f"FROM scratch\n# {suffix}\n",
                    },
                ),
            ),
        )


class InvestigatingPlanner(FixedPlanner):
    def __init__(self):
        super().__init__()
        self.investigation_calls = 0
        self.analysis_observations = ()

    def plan_investigation(self, state, observations, available_tools):
        self.investigation_calls += 1
        if self.investigation_calls == 1:
            return InvestigationDecision(
                actions=(
                    ToolCall(
                        "read_file",
                        {"path": "pyproject.toml"},
                        "inspect declared dependencies",
                    ),
                ),
                rationale="dependency evidence is still needed",
            )
        return InvestigationDecision(
            complete=True,
            rationale="manifest evidence is sufficient",
        )

    def analyze_failure(self, state, observations):
        self.analysis_calls += 1
        self.analysis_observations = observations
        return "pyproject evidence identifies the missing environment dependency"


class VerificationOverlayPlanner(FixedPlanner):
    def analyze_failure(self, state, observations):
        self.analysis_calls += 1
        return "pytest-mock is missing only from the Testability environment"

    def plan_fix(self, state, diagnosis, available_tools):
        self.plan_calls += 1
        self.available_tools.append(available_tools)
        return FixPlan(
            diagnosis,
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["pytest-mock==3.14.0"]},
                ),
            ),
            "add the missing plugin only to Testability",
        )


class SuccessfulOverlayVerifier:
    def __init__(self):
        self.calls = 0
        self.workspaces = []

    def verify(self, profile, build_result, workspace, *, build_plan=None, deadline_at=None):
        self.calls += 1
        self.workspaces.append(workspace)
        overlay = workspace / ".dprauto" / "requirements-verification.txt"
        if "pytest-mock==3.14.0" not in overlay.read_text(encoding="utf-8"):
            raise AssertionError("verification overlay was not visible in candidate workspace")
        results = tuple(
            VerificationResult(
                f"overlay-{level.value}",
                level,
                VerificationStatus.PASSED,
                summary=f"{level.value} passed",
            )
            for level in (
                VerificationLevel.INSTALLABILITY,
                VerificationLevel.TESTABILITY,
                VerificationLevel.RUNNABILITY,
            )
        )
        return VerificationReport("overlay-report", build_result.attempt_id, results)


class StaticTool:
    def __init__(self, name, callback):
        self.name = name
        self.description = name
        self.callback = callback

    def invoke(self, arguments, context):
        return self.callback(arguments, context)


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


def failure(*, infrastructure=False):
    return FailureInfo(
        FailureCategory.DOCKER if infrastructure else FailureCategory.BUILD_COMMAND,
        BuildStage.BUILD,
        "same failure",
        "same:fingerprint",
        kind=(
            BuildFailureKind.DOCKER_INFRASTRUCTURE
            if infrastructure
            else BuildFailureKind.PROJECT_BUILD
        ),
        key_log="same bounded log",
        infrastructure_related=infrastructure,
    )


class AgentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.storage = LocalArtifactStorage(self.root / "artifacts")
        command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
        self.plan = BuildPlan(
            "agent-plan",
            "agent-project",
            "docker",
            (BuildStep("build", BuildStage.BUILD, command),),
            metadata={"dockerfile": "Dockerfile"},
        )
        now = datetime.now(timezone.utc)
        log = self.storage.save("fixture/failure.log", b"same bounded log")
        self.result = BuildResult(
            "agent-attempt",
            self.plan.plan_id,
            BuildStatus.FAILED,
            now,
            now,
            exit_code=1,
            failed_stage=BuildStage.BUILD,
            logs=(log,),
        )
        self.profile = ProjectProfile(
            "agent-project",
            SourceReference("fixture"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
        )

    def test_analyze_failure_gathers_bounded_read_only_evidence_before_diagnosis(
        self,
    ) -> None:
        planner = InvestigatingPlanner()
        (self.root / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            "[project]\ndependencies = ['psycopg']\n",
            encoding="utf-8",
        )
        tools = ToolRegistry(
            (
                ListProjectFilesTool(),
                ReadFileTool(),
                SearchProjectTool(),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult(
                        "get_build_log",
                        True,
                        "read bounded log",
                        data={"content": "fatal error: libpq-fe.h"},
                    ),
                ),
                StaticTool(
                    "build_image",
                    lambda arguments, context: ToolResult(
                        "build_image", True, "unused build"
                    ),
                ),
            )
        )
        workflow = AgentWorkflow(planner, tools, self.storage)
        self.addCleanup(workflow.close)
        profile = ProjectProfile(
            "investigation-project",
            SourceReference("fixture://investigation-project"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
        )
        command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
        plan = BuildPlan(
            "investigation-plan",
            profile.project_id,
            "docker",
            (BuildStep("build", BuildStage.BUILD, command),),
            metadata={"dockerfile": "Dockerfile"},
        )
        state = create_agent_state("investigation-run")
        state.update(
            workspace=str(self.root),
            project_profile=profile,
            build_plan=plan,
            failure=failure(),
        )

        update = workflow.analyze_failure(state)

        self.assertEqual(update["phase"], AgentPhase.DIAGNOSING)
        self.assertEqual(update["llm_call_count"], 3)
        self.assertEqual(planner.investigation_calls, 2)
        self.assertEqual(update["evidence_pack"].rounds, 2)
        self.assertEqual(update["evidence_pack"].action_count, 1)
        self.assertTrue(update["evidence_pack"].completed)
        self.assertTrue(
            any(
                item.data.get("path") == "pyproject.toml"
                and "psycopg" in item.data.get("content", "")
                for item in planner.analysis_observations
            )
        )
        self.assertTrue(
            any(
                record.data.get("path") == "pyproject.toml"
                for record in update["evidence_pack"].records
            )
        )
    def workflow(self, planner, current_failure, config=None):
        diff = EnvironmentDiff(
            files=(FileChange("Dockerfile", ChangeKind.MODIFIED, "before", "after"),),
            summary="updated Dockerfile",
        )
        tools = ToolRegistry(
            (
                StaticTool(
                    "read_file",
                    lambda arguments, context: ToolResult(
                        "read_file", True, "read Dockerfile", data={"content": "FROM scratch\n"}
                    ),
                ),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult(
                        "get_build_log", True, "read log", data={"content": "same bounded log"}
                    ),
                ),
                StaticTool(
                    "modify_build_script",
                    lambda arguments, context: ToolResult(
                        "modify_build_script", True, "updated", environment_diff=diff
                    ),
                ),
                StaticTool(
                    "build_image",
                    lambda arguments, context: ToolResult(
                        "build_image",
                        False,
                        "build failed",
                        build_plan=self.plan,
                        build_result=self.result,
                        failure=current_failure,
                    ),
                ),
            )
        )
        return AgentWorkflow(
            planner,
            tools,
            self.storage,
            config
            or AgentConfig(max_attempts=5, max_repeated_failures=2, max_total_seconds=60),
        )

    def initial_state(self, current_failure):
        state = create_agent_state("workflow-run")
        state.update(
            project_profile=self.profile,
            build_plan=self.plan,
            build_result=self.result,
            failure=current_failure,
        )
        return state

    def test_graph_has_explicit_nodes_and_stops_on_repeated_fingerprint(self) -> None:
        planner = FixedPlanner()
        current_failure = failure()
        workflow = self.workflow(planner, current_failure)
        graph_nodes = set(workflow.graph.get_graph().nodes)
        self.assertTrue(
            {"analyze_failure", "plan_fix", "apply_fix", "execute", "evaluate"}
            <= graph_nodes
        )

        final = workflow.run(self.initial_state(current_failure), self.root)

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertEqual(final["attempt_number"], 2)
        self.assertEqual(final["repeated_failure_count"], 2)
        self.assertIn("same failure repeated", final["stop_reason"])
        self.assertEqual(planner.analysis_calls, 2)
        self.assertEqual(planner.plan_calls, 2)
        self.assertNotIn("build_image", planner.available_tools[0])
        self.assertNotIn("read_file", planner.available_tools[0])
        self.assertNotIn("get_build_log", planner.available_tools[0])
        self.assertIn("modify_build_script", planner.available_tools[0])
        self.assertEqual(len(final["environment_diffs"]), 2)
        self.assertTrue(
            any(item.key.endswith("environment-diff.json") for item in final["artifacts"])
        )
        self.assertTrue(
            any(item.key.endswith("repair-search-space.json") for item in final["artifacts"])
        )
        self.assertEqual(len(final["context_summary"].round_feedback), 2)
        self.assertEqual(final["context_summary"].round_feedback[-1].progress, "stagnant")

    def test_plan_gate_feedback_replans_inside_the_same_repair_round(self) -> None:
        planner = FeedbackCorrectingPlanner()
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchSystemPackagesTool(self.storage),
                PatchPythonDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(planner, tools, self.storage)
        self.addCleanup(workflow.close)
        state = self.initial_state(
            FailureInfo(
                FailureCategory.SYSTEM_DEPENDENCY,
                BuildStage.BUILD,
                "PostgreSQL headers are missing",
                "system:libpq",
                key_log="fatal error: libpq-fe.h: No such file or directory",
            )
        )
        state["diagnosis"] = "libpq headers are required by the selected build"

        update = workflow.plan_fix(state)

        self.assertEqual(update["phase"], AgentPhase.PROPOSING)
        self.assertEqual(planner.plan_calls, 2)
        self.assertEqual(update["fix_plan"].actions[0].tool, "patch_system_packages")
        self.assertEqual(
            tuple(planner.available_tools[0]),
            ("patch_system_packages",),
        )
        self.assertIn("outside the bounded search space", planner.feedback[1][0])
        self.assertEqual(update["llm_call_count"], 2)

    def test_duplicate_method_feedback_requests_an_alternative_before_stopping(self) -> None:
        planner = DuplicateCorrectingPlanner()
        workflow = self.workflow(planner, failure())
        self.addCleanup(workflow.close)
        state = self.initial_state(failure())
        state["diagnosis"] = "the build script requires a different repair"
        first_plan = FixPlan(
            "first build script repair",
            (
                ToolCall(
                    "modify_build_script",
                    {
                        "path": "Dockerfile",
                        "content": "FROM scratch\n# first\n",
                    },
                ),
            ),
        )
        state["context_summary"] = ContextSummary(
            failed_methods=(
                AttemptedMethod(
                    method_fingerprint(first_plan),
                    first_plan.hypothesis,
                    "failed",
                ),
            )
        )

        update = workflow.plan_fix(state)

        self.assertEqual(update["phase"], AgentPhase.PROPOSING)
        self.assertEqual(planner.plan_calls, 2)
        self.assertIn("duplicate repair method", planner.feedback[1][0])
        self.assertIn("alternative", update["fix_plan"].hypothesis)

    def test_failure_family_stops_volatile_fingerprint_churn(self) -> None:
        planner = FixedPlanner()
        initial = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "command failed after 12 seconds",
            "volatile:initial",
            key_log="RuntimeError: worker 17 failed",
        )
        current = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "command failed after 99 seconds",
            "volatile:current",
            key_log="RuntimeError: worker 42 failed",
        )

        final = self.workflow(planner, current).run(
            self.initial_state(initial), self.root
        )

        self.assertEqual(final["attempt_number"], 2)
        self.assertIn("no causal progress", final["stop_reason"])
        self.assertEqual(final["stagnant_failure_count"], 2)

    def test_infrastructure_failure_stops_before_llm_or_mutation(self) -> None:
        planner = FixedPlanner()
        current_failure = failure(infrastructure=True)
        final = self.workflow(planner, current_failure).run(
            self.initial_state(current_failure), self.root
        )

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertEqual(final["attempt_number"], 0)
        self.assertIn("infrastructure failure", final["stop_reason"])
        self.assertEqual(planner.analysis_calls, 0)
        self.assertEqual((self.root / "Dockerfile").read_text(), "FROM scratch\n")

    def test_plan_rejects_multiple_structured_high_risk_dimensions(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool(
                    "read_file",
                    lambda arguments, context: ToolResult("read_file", True, "read"),
                ),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult("get_build_log", True, "log"),
                ),
                StaticTool(
                    "build_image",
                    lambda arguments, context: ToolResult("build_image", True, "build"),
                ),
                PatchSystemPackagesTool(self.storage),
                PatchPythonDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        plan = FixPlan(
            "change two dependency dimensions",
            (
                ToolCall(
                    "patch_system_packages",
                    {
                        "path": "Dockerfile",
                        "package_manager": "apt",
                        "packages": ["git"],
                    },
                ),
                ToolCall(
                    "patch_python_dependencies",
                    {"path": "Dockerfile", "packages": ["requests"]},
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan)

        self.assertIn("one high-risk environment dimension", reason)
        self.assertIn("python-dependencies", reason)
        self.assertIn("system-packages", reason)

    def test_whole_file_plan_requires_complete_matching_read_proof(self) -> None:
        tools = ToolRegistry(
            (
                ReadFileTool(max_lines=100),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                ModifyBuildScriptTool(self.storage),
                PatchBuildScriptTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        content = "".join(f"RUN step-{number}\n" for number in range(1, 301))
        (self.root / "Dockerfile").write_text(content, encoding="utf-8")
        observation = tools.invoke(
            "read_file",
            {"path": "Dockerfile"},
            ToolContext("proof-run", 0, str(self.root)),
        )
        state = self.initial_state(failure())
        state["tool_results"] = (observation,)
        plan = FixPlan(
            "replace the whole paged file",
            (
                ToolCall(
                    "modify_build_script",
                    {
                        "path": "Dockerfile",
                        "content": content.replace("step-20", "step-twenty"),
                        "source_sha256": observation.data["source_sha256"],
                    },
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan, failure(), state)

        self.assertIn("complete read_file proof through EOF", reason)
        self.assertIn("use patch_build_script", reason)

    def test_whole_file_plan_accepts_complete_matching_read_proof(self) -> None:
        tools = ToolRegistry(
            (
                ReadFileTool(),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                ModifyBuildScriptTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        observation = tools.invoke(
            "read_file",
            {"path": "Dockerfile"},
            ToolContext("complete-proof-run", 0, str(self.root)),
        )
        state = self.initial_state(failure())
        state["tool_results"] = (observation,)
        plan = FixPlan(
            "replace a completely observed file",
            (
                ToolCall(
                    "modify_build_script",
                    {
                        "path": "Dockerfile",
                        "content": "FROM scratch\nRUN true\n",
                        "source_sha256": observation.data["source_sha256"],
                    },
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan, failure(), state)

        self.assertEqual(reason, "")

    def test_exact_patch_plan_accepts_prompt_visible_paged_evidence(self) -> None:
        tools = ToolRegistry(
            (
                ReadFileTool(max_lines=100),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                ModifyBuildScriptTool(self.storage),
                PatchBuildScriptTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        content = "".join(f"RUN step-{number}\n" for number in range(1, 301))
        (self.root / "Dockerfile").write_text(content, encoding="utf-8")
        observation = tools.invoke(
            "read_file",
            {"path": "Dockerfile"},
            ToolContext("patch-proof-run", 0, str(self.root)),
        )
        state = self.initial_state(failure())
        state["tool_results"] = (observation,)
        plan = FixPlan(
            "patch one observed instruction",
            (
                ToolCall(
                    "patch_build_script",
                    {
                        "path": "Dockerfile",
                        "source_sha256": observation.data["source_sha256"],
                        "old_content": "RUN step-20\n",
                        "new_content": "RUN step-20 --bounded\n",
                    },
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan, failure(), state)

        self.assertEqual(reason, "")

    def test_exact_patch_plan_rejects_unobserved_anchor(self) -> None:
        tools = ToolRegistry(
            (
                ReadFileTool(max_lines=100),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchBuildScriptTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        content = "".join(f"RUN step-{number}\n" for number in range(1, 301))
        (self.root / "Dockerfile").write_text(content, encoding="utf-8")
        observation = tools.invoke(
            "read_file",
            {"path": "Dockerfile"},
            ToolContext("patch-proof-run", 0, str(self.root)),
        )
        state = self.initial_state(failure())
        state["tool_results"] = (observation,)
        plan = FixPlan(
            "patch an instruction outside the observed page",
            (
                ToolCall(
                    "patch_build_script",
                    {
                        "path": "Dockerfile",
                        "source_sha256": observation.data["source_sha256"],
                        "old_content": "RUN step-250\n",
                        "new_content": "RUN step-250 --unseen\n",
                    },
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan, failure(), state)

        self.assertIn("prompt-visible read_file evidence", reason)

    def test_plan_rejects_verification_overlay_outside_test_stage(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchVerificationDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "add a verification dependency during build",
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["pytest-mock==3.14.0"]},
                ),
            ),
        )

        reason = workflow._plan_policy_violation(plan, failure())

        self.assertIn("only allowed for Testability failures", reason)

    def test_plan_rejects_runtime_dependency_patch_for_test_stage(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchPythonDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "add a test dependency to the runtime image",
            (
                ToolCall(
                    "patch_python_dependencies",
                    {"path": "Dockerfile", "packages": ["pytest-mock==3.14.0"]},
                ),
            ),
        )
        test_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability verification did not pass",
            "test:missing-pytest-mock-policy",
        )

        reason = workflow._plan_policy_violation(plan, test_failure)

        self.assertIn("must use patch_verification_dependencies", reason)

    def test_plan_rejects_verification_dependency_without_causal_evidence(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchVerificationDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "guess that a package may repair pytest capture",
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["dukpy"]},
                ),
            ),
        )
        capture_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability verification did not pass",
            "test:closed-stdout",
            key_log="ValueError: I/O operation on closed file",
        )

        reason = workflow._plan_policy_violation(plan, capture_failure)

        self.assertIn("require direct missing-module", reason)

    def test_plan_rejects_verification_dependency_already_satisfied(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchVerificationDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "add a dependency reported missing by another import",
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["dukpy"]},
                ),
            ),
        )
        contradictory_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability verification did not pass",
            "test:contradictory-dependency",
            key_log=(
                "Requirement already satisfied: dukpy in /usr/local/lib/python3.11/site-packages\n"
                "ModuleNotFoundError: No module named 'different_plugin'"
            ),
        )

        reason = workflow._plan_policy_violation(plan, contradictory_failure)

        self.assertIn("already satisfied", reason)

    def test_plan_rejects_dependency_not_named_by_missing_module_evidence(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchVerificationDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "guess an unrelated package",
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["requests-cache"]},
                ),
            ),
        )
        missing = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability failed",
            "test:missing-sybil",
            key_log="ModuleNotFoundError: No module named 'sybil'",
        )

        reason = workflow._plan_policy_violation(plan, missing)

        self.assertIn("not named by the failure evidence", reason)
        self.assertIn("sybil", reason)

    def test_plan_accepts_package_named_collection_compatibility_overlay(self) -> None:
        tools = ToolRegistry(
            (
                StaticTool("read_file", lambda arguments, context: None),
                StaticTool("get_build_log", lambda arguments, context: None),
                StaticTool("build_image", lambda arguments, context: None),
                PatchVerificationDependenciesTool(self.storage),
            )
        )
        workflow = AgentWorkflow(object(), tools, self.storage)
        self.addCleanup(workflow.close)
        plan = FixPlan(
            "pin the package named by the collection compatibility warning",
            (
                ToolCall(
                    "patch_verification_dependencies",
                    {"packages": ["python-multipart<0.0.14"]},
                ),
            ),
        )
        compatibility_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability collection failed",
            "test:python-multipart-compatibility",
            key_log=(
                "PendingDeprecationWarning: Please use import python_multipart instead"
            ),
        )

        reason = workflow._plan_policy_violation(plan, compatibility_failure)

        self.assertEqual(reason, "")

    def test_explicit_preflight_failure_rejects_candidate_without_full_rebuild(self) -> None:
        planner = FixedPlanner()
        current_failure = failure()
        diff = EnvironmentDiff(
            files=(FileChange("Dockerfile", ChangeKind.MODIFIED, "before", "after"),),
            build_scripts=(
                FileChange("Dockerfile", ChangeKind.MODIFIED, "before", "after"),
            ),
            summary="updated Dockerfile",
        )

        def modify(arguments, context):
            (Path(context.workspace) / "Dockerfile").write_text(
                "FROM\n", encoding="utf-8"
            )
            return ToolResult(
                "modify_build_script",
                True,
                "updated",
                environment_diff=diff,
            )

        def unexpected_build(arguments, context):
            raise AssertionError("full rebuild must not run after explicit preflight failure")

        tools = ToolRegistry(
            (
                StaticTool(
                    "read_file",
                    lambda arguments, context: ToolResult("read_file", True, "read"),
                ),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult("get_build_log", True, "log"),
                ),
                StaticTool("modify_build_script", modify),
                StaticTool("build_image", unexpected_build),
            )
        )
        workflow = AgentWorkflow(
            planner,
            tools,
            self.storage,
            AgentConfig(max_attempts=1, max_repeated_failures=2, max_total_seconds=60),
            preflight_runner=RejectingPreflight(),
        )

        final = workflow.run(self.initial_state(current_failure), self.root)

        self.assertEqual(final["attempt_number"], 1)
        self.assertEqual(final["repair_preflight"].status, VerificationStatus.FAILED)
        self.assertEqual(final["failure"].failure_stage, BuildStage.PLANNING)
        self.assertIn("repair_preflight=true", final["failure"].evidence)
        self.assertIn("maximum repair attempts", final["stop_reason"])
        self.assertEqual(final["repair_candidate"].status, "rejected")
        self.assertEqual((self.root / "Dockerfile").read_text(), "FROM scratch\n")
        self.assertTrue(
            any(item.key.endswith("repair-preflight.json") for item in final["artifacts"])
        )

    def test_exhausted_time_budget_stops_before_llm_or_tools(self) -> None:
        planner = FixedPlanner()
        current_failure = failure()
        state = self.initial_state(current_failure)
        state["started_at"] = datetime.now(timezone.utc) - timedelta(seconds=120)
        state["deadline_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        final = self.workflow(
            planner,
            current_failure,
            AgentConfig(max_attempts=5, max_repeated_failures=10, max_total_seconds=60),
        ).run(state, self.root)

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertIn("maximum agent runtime", final["stop_reason"])
        self.assertEqual(final["llm_call_count"], 0)
        self.assertEqual(planner.analysis_calls, 0)
        self.assertEqual(final["attempt_number"], 0)

    def test_template_python_install_timeout_without_owned_build_script_stops_before_llm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = LocalArtifactStorage(root / "artifacts")
            command = CommandSpec(("docker", "build", "."), purpose=CommandPurpose.BUILD)
            plan = BuildPlan(
                "template-plan",
                "template-project",
                "template",
                (BuildStep("build", BuildStage.BUILD, command),),
                generated_files=(
                    GeneratedFile(
                        "Dockerfile",
                        "FROM python:3.11-slim\nRUN python -m pip install --no-cache-dir .\n",
                    ),
                    GeneratedFile(
                        "setup.sh",
                        "python -m pip install --no-cache-dir .\n",
                        executable=True,
                    ),
                ),
            )
            now = datetime.now(timezone.utc)
            current_failure = FailureInfo(
                FailureCategory.BUILD_COMMAND,
                BuildStage.BUILD,
                "Build command timed out",
                "timeout:fingerprint",
                evidence=("timeout_profile=python-package-install",),
                key_log="command timed out after 300 seconds",
            )
            state = create_agent_state("template-timeout-run")
            state.update(
                workspace=str(root),
                project_profile=ProjectProfile(
                    "template-project",
                    SourceReference("fixture://template-project"),
                    languages=("Python",),
                    build_files=("pyproject.toml",),
                ),
                build_plan=plan,
                build_result=BuildResult(
                    "template-attempt",
                    plan.plan_id,
                    BuildStatus.TIMED_OUT,
                    now,
                    now,
                    failed_stage=BuildStage.BUILD,
                ),
                failure=current_failure,
            )
            planner = FixedPlanner()
            tools = ToolRegistry(
                (
                    StaticTool("read_file", lambda arguments, context: None),
                    StaticTool("get_build_log", lambda arguments, context: None),
                    StaticTool("build_image", lambda arguments, context: None),
                )
            )

            final = AgentWorkflow(
                planner,
                tools,
                storage,
                AgentConfig(max_attempts=5, max_repeated_failures=10, max_total_seconds=60),
            ).run(state, root)

            self.assertEqual(final["phase"], AgentPhase.STOPPED)
            self.assertIn("template dependency installation timed out", final["stop_reason"])
            self.assertFalse(final["agent_participated"])
            self.assertEqual(final["llm_call_count"], 0)
            self.assertEqual(planner.analysis_calls, 0)
            self.assertEqual(final["attempt_number"], 0)

    def test_active_dependency_download_timeout_stops_before_agent_entry(self) -> None:
        planner = FixedPlanner()
        current_failure = FailureInfo(
            FailureCategory.BUILD_COMMAND,
            BuildStage.BUILD,
            "Build command timed out",
            "timeout:active-download",
            evidence=(
                "timeout_profile=python-package-install",
                "timeout_activity=dependency-download",
                "last_active_line=Downloading pandas-2.0.whl",
            ),
            key_log="Downloading pandas-2.0.whl",
        )

        final = self.workflow(planner, current_failure).run(
            self.initial_state(current_failure), self.root
        )

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertIn("dependency download remained active", final["stop_reason"])
        self.assertFalse(final["agent_participated"])
        self.assertEqual(final["llm_call_count"], 0)
        self.assertEqual(planner.analysis_calls, 0)

    def test_active_test_progress_timeout_stops_before_agent_entry(self) -> None:
        planner = FixedPlanner()
        current_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability verification did not pass",
            "timeout:active-tests",
            evidence=(
                "timed_out=True",
                "timeout_activity=test-progress",
                "asdf/_tests/test_core.py .... [ 35%]",
            ),
            key_log="asdf/_tests/test_core.py .... [ 35%]",
        )

        final = self.workflow(planner, current_failure).run(
            self.initial_state(current_failure), self.root
        )

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertIn("project tests remained active", final["stop_reason"])
        self.assertFalse(final["agent_participated"])
        self.assertEqual(final["llm_call_count"], 0)
        self.assertEqual(planner.analysis_calls, 0)

    def test_generated_template_base_image_is_not_effective_verification_change(
        self,
    ) -> None:
        diff = EnvironmentDiff(
            base_image=ValueChange("base_image", None, "python:3.11-slim"),
            python_version=ValueChange("python_version", ">=3.8", "3.11"),
        )
        plan = BuildPlan(
            "template-plan",
            "template-project",
            "template",
            (BuildStep("build", BuildStage.BUILD, self.plan.steps[0].command),),
            metadata={"base_image": "python:3.11-slim"},
        )

        self.assertFalse(AgentWorkflow._effective_base_image_change(diff, plan))
        self.assertFalse(AgentWorkflow._effective_python_version_change(diff, plan))

    def test_incompatible_template_python_version_is_an_effective_change(self) -> None:
        diff = EnvironmentDiff(
            base_image=ValueChange("base_image", None, "python:3.11-slim"),
            python_version=ValueChange("python_version", ">=3.8,<3.11", "3.11"),
        )
        plan = BuildPlan(
            "template-plan",
            "template-project",
            "template",
            (BuildStep("build", BuildStage.BUILD, self.plan.steps[0].command),),
            metadata={"base_image": "python:3.11-slim"},
        )

        self.assertTrue(AgentWorkflow._effective_python_version_change(diff, plan))

    def test_read_only_plan_is_rejected_before_tool_execution(self) -> None:
        planner = ReadOnlyPlanner()
        current_failure = failure()
        final = self.workflow(planner, current_failure).run(
            self.initial_state(current_failure), self.root
        )

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertIn("outside the bounded search space", final["stop_reason"])
        self.assertEqual(final["attempt_number"], 0)
        self.assertEqual(planner.plan_calls, 2)
        self.assertEqual((self.root / "Dockerfile").read_text(), "FROM scratch\n")

    def test_maximum_attempts_is_an_independent_hard_stop(self) -> None:
        planner = FixedPlanner()
        current_failure = failure()
        final = self.workflow(
            planner,
            current_failure,
            AgentConfig(max_attempts=1, max_repeated_failures=10, max_total_seconds=60),
        ).run(self.initial_state(current_failure), self.root)

        self.assertEqual(final["phase"], AgentPhase.STOPPED)
        self.assertEqual(final["attempt_number"], 1)
        self.assertIn("maximum repair attempts", final["stop_reason"])
        self.assertEqual(planner.plan_calls, 1)

    def test_verification_failure_carries_command_and_output_evidence_to_llm(self) -> None:
        command = CommandSpec(("python", "-m", "pytest"), purpose=CommandPurpose.TEST)
        command_result = CommandResult(command, 1, duration_seconds=1.25)
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.FAILED,
            "project test command failed",
            command_result=command_result,
            metadata={
                "output_excerpt": "E ModuleNotFoundError: No module named 'pytest_mock'"
            },
        )
        result = VerificationResult(
            "testability-fixture",
            VerificationLevel.TESTABILITY,
            VerificationStatus.FAILED,
            command_result=command_result,
            checks=(check,),
        )
        report = VerificationReport("report-fixture", "attempt-fixture", (result,))

        failure_info = AgentWorkflow._verification_failure(report)

        self.assertEqual(failure_info.failed_command.display, "python -m pytest")
        self.assertIn("exit_code=1", failure_info.key_log)
        self.assertIn("ModuleNotFoundError", failure_info.key_log)
        state = self.initial_state(failure_info)
        context = self.workflow(FixedPlanner(), failure_info).context_manager.build_llm_context(
            state, ()
        )
        self.assertIn("ModuleNotFoundError", context["current_failure"]["key_log"])

    def test_testability_overlay_reverifies_without_rebuilding_runtime_image(self) -> None:
        now = datetime.now(timezone.utc)
        successful_build = BuildResult(
            "successful-image-attempt",
            self.plan.plan_id,
            BuildStatus.SUCCEEDED,
            now,
            now,
            exit_code=0,
            image_reference="fixture:already-built",
            summary="build succeeded",
        )
        current_failure = FailureInfo(
            FailureCategory.TEST,
            BuildStage.TEST,
            "Testability verification did not pass",
            "test:missing-pytest-mock",
            key_log="ModuleNotFoundError: No module named 'pytest_mock'",
        )
        state = create_agent_state("verification-overlay-run")
        state.update(
            project_profile=self.profile,
            build_plan=self.plan,
            build_result=successful_build,
            failure=current_failure,
        )

        def unexpected_build(arguments, context):
            raise AssertionError("verification-only overlay must not rebuild the image")

        verifier = SuccessfulOverlayVerifier()
        tools = ToolRegistry(
            (
                ReadFileTool(),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult("get_build_log", True, "unused"),
                ),
                PatchVerificationDependenciesTool(self.storage),
                StaticTool("build_image", unexpected_build),
            )
        )
        workflow = AgentWorkflow(
            VerificationOverlayPlanner(),
            tools,
            self.storage,
            AgentConfig(max_attempts=2, max_repeated_failures=2, max_total_seconds=60),
            verification_runner=verifier,
            environment_differ=PythonEnvironmentDiffer(),
        )
        self.addCleanup(workflow.close)

        final = workflow.run(state, self.root)

        self.assertEqual(final["phase"], AgentPhase.COMPLETED)
        self.assertEqual(final["attempt_number"], 1)
        self.assertEqual(final["build_result"].attempt_id, "successful-image-attempt")
        self.assertEqual(verifier.calls, 1)
        self.assertEqual(final["repair_preflight"].status, VerificationStatus.SKIPPED)
        self.assertEqual(final["repair_candidate"].status, "accepted")
        self.assertTrue(final["verification_report"].succeeded)
        self.assertIsNone(final.get("failure"))
        overlay = self.root / ".dprauto" / "requirements-verification.txt"
        self.assertIn("pytest-mock==3.14.0", overlay.read_text(encoding="utf-8"))

    def test_verification_timeout_marks_active_pytest_progress(self) -> None:
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
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.FAILED,
            "project test command failed",
            command_result=command_result,
            metadata={
                "output_excerpt": (
                    "asdf/_tests/core/test_integration.py .. [ 35%]\n"
                    "verification command timed out"
                )
            },
        )
        report = VerificationReport(
            "report-active-tests",
            "attempt-active-tests",
            (
                VerificationResult(
                    "testability-active-tests",
                    VerificationLevel.TESTABILITY,
                    VerificationStatus.FAILED,
                    command_result=command_result,
                    checks=(check,),
                ),
            ),
        )

        failure_info = AgentWorkflow._verification_failure(report)

        self.assertIn("timeout_activity=test-progress", failure_info.evidence)
        self.assertTrue(AgentWorkflow._is_active_test_progress_timeout(failure_info))

    def test_verification_timeout_does_not_treat_download_percent_as_test_progress(
        self,
    ) -> None:
        command = CommandSpec(
            ("python", "-m", "pip", "install", "pytest"),
            purpose=CommandPurpose.TEST,
        )
        command_result = CommandResult(command, None, timed_out=True)
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.FAILED,
            "project test command failed",
            command_result=command_result,
            metadata={"output_excerpt": "Downloading package.whl 35% (10 MB)"},
        )
        report = VerificationReport(
            "report-download",
            "attempt-download",
            (
                VerificationResult(
                    "testability-download",
                    VerificationLevel.TESTABILITY,
                    VerificationStatus.FAILED,
                    command_result=command_result,
                    checks=(check,),
                ),
            ),
        )

        failure_info = AgentWorkflow._verification_failure(report)

        self.assertNotIn("timeout_activity=test-progress", failure_info.evidence)

    def test_verification_network_failure_is_infrastructure_not_project_repair(
        self,
    ) -> None:
        command = CommandSpec(
            ("python", "-m", "pip", "install", "pytest"),
            purpose=CommandPurpose.TEST,
        )
        command_result = CommandResult(command, None, timed_out=True, duration_seconds=90.0)
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.FAILED,
            "project test command failed",
            command_result=command_result,
            metadata={
                "output_excerpt": (
                    "NewConnectionError: Failed to establish a new connection: "
                    "[Errno -3] Temporary failure in name resolution"
                )
            },
        )
        result = VerificationResult(
            "testability-network",
            VerificationLevel.TESTABILITY,
            VerificationStatus.FAILED,
            command_result=command_result,
            checks=(check,),
        )
        report = VerificationReport("report-network", "attempt-network", (result,))

        failure_info = AgentWorkflow._verification_failure(report)

        self.assertEqual(failure_info.category, FailureCategory.NETWORK)
        self.assertEqual(failure_info.kind, BuildFailureKind.NETWORK)
        self.assertTrue(failure_info.infrastructure_related)
        self.assertIn("Temporary failure in name resolution", failure_info.key_log)

    def test_rebuild_reparses_workspace_after_environment_edit(self) -> None:
        refreshed = ProjectProfile(
            "agent-project",
            self.profile.source,
            languages=("Python",),
            dockerfiles=("Dockerfile", "docker/Dockerfile.repaired"),
        )
        observed_profiles = []

        def build(arguments, context):
            observed_profiles.append(context.project_profile)
            return ToolResult(
                "build_image",
                False,
                "build failed",
                build_plan=self.plan,
                build_result=self.result,
                failure=failure(),
            )

        tools = ToolRegistry(
            (
                StaticTool(
                    "read_file",
                    lambda arguments, context: ToolResult("read_file", True, "read"),
                ),
                StaticTool(
                    "get_build_log",
                    lambda arguments, context: ToolResult("get_build_log", True, "log"),
                ),
                StaticTool(
                    "inspect_project",
                    lambda arguments, context: ToolResult(
                        "inspect_project",
                        True,
                        "refreshed",
                        data={"project_profile": refreshed},
                    ),
                ),
                StaticTool("build_image", build),
            )
        )
        workflow = AgentWorkflow(FixedPlanner(), tools, self.storage)
        state = self.initial_state(failure())
        state["workspace"] = str(self.root)
        state["attempt_number"] = 1

        update = workflow.execute(state)

        self.assertEqual(update["project_profile"], refreshed)
        self.assertEqual(observed_profiles, [refreshed])
        self.assertEqual(
            [item.tool for item in update["tool_results"]],
            ["inspect_project", "build_image"],
        )


if __name__ == "__main__":
    unittest.main()
