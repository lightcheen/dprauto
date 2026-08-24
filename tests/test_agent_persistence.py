import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dprauto.adapters.persistence import SQLiteAgentPersistence
from dprauto.adapters.storage import LocalArtifactStorage
from dprauto.agent.context import method_fingerprint
from dprauto.agent.models import FixPlan, ToolCall, ToolContext, ToolResult
from dprauto.agent.state import create_agent_state
from dprauto.agent.tools import GetBuildLogTool, ModifyBuildScriptTool, ReadFileTool, ToolRegistry
from dprauto.agent.workflow import AgentWorkflow
from dprauto.application.build import BuildStrategyAttempt
from dprauto.config import AgentConfig
from dprauto.domain.enums import AgentPhase, BuildStage, BuildStatus, FailureCategory
from dprauto.domain.models import (
    BuildPlan,
    BuildResult,
    BuildStep,
    CommandSpec,
    FailureInfo,
    ProjectProfile,
    SourceReference,
)


def classified_failure(marker):
    return FailureInfo(
        FailureCategory.BUILD_COMMAND,
        BuildStage.BUILD,
        f"build still contains {marker}",
        f"failure:{marker.lower()}",
        key_log=f"intentional marker {marker}",
    )


class MarkerBuildTool:
    name = "build_image"
    description = "deterministic marker build"

    def __init__(self, workspace, storage, plan):
        self.workspace = workspace
        self.storage = storage
        self.plan = plan

    def invoke(self, arguments, context):
        content = Path(context.workspace).joinpath("Dockerfile").read_text()
        marker = "FAIL_A" if "FAIL_A" in content else "FAIL_B" if "FAIL_B" in content else ""
        output = f"build {'failed at ' + marker if marker else 'succeeded'}\n".encode()
        digest = hashlib.sha256(output).hexdigest()[:12]
        log = self.storage.save(f"marker-build/{digest}.log", output, media_type="text/plain")
        now = datetime.now(timezone.utc)
        result = BuildResult(
            f"marker-{digest}",
            self.plan.plan_id,
            BuildStatus.FAILED if marker else BuildStatus.SUCCEEDED,
            now,
            now,
            exit_code=1 if marker else 0,
            failed_stage=BuildStage.BUILD if marker else None,
            logs=(log,),
            summary=f"marker build {'failed' if marker else 'succeeded'}",
        )
        failure = classified_failure(marker) if marker else None
        return ToolResult(
            self.name,
            failure is None,
            result.summary,
            artifacts=(log,),
            build_plan=self.plan,
            build_result=result,
            failure=failure,
        )


class MarkerPlanner:
    def __init__(self):
        self.seen_summaries = []
        self.plans = []

    def analyze_failure(self, state, observations):
        self.seen_summaries.append(state["context_summary"])
        return f"remove {state['failure'].fingerprint} without repeating older methods"

    def plan_fix(self, state, diagnosis, available_tools):
        marker = "FAIL_A" if state["failure"].fingerprint.endswith("fail_a") else "FAIL_B"
        candidate = state.get("repair_candidate")
        workspace = (
            candidate.workspace
            if candidate is not None and candidate.status == "pending"
            else state["workspace"]
        )
        current = Path(workspace).joinpath("Dockerfile").read_text()
        replacement = current.replace(f"# {marker}\n", "")
        plan = FixPlan(
            f"remove {marker}",
            (ToolCall("modify_build_script", {"path": "Dockerfile", "content": replacement}),),
            f"remove only {marker}",
        )
        self.plans.append(plan)
        return plan


class DuplicatePlanner:
    def __init__(self, plan):
        self.plan = plan

    def analyze_failure(self, state, observations):
        return "duplicate"

    def plan_fix(self, state, diagnosis, available_tools):
        return self.plan


class AgentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "Dockerfile").write_text(
            "FROM scratch\n# FAIL_A\n# FAIL_B\n", encoding="utf-8"
        )
        self.storage = LocalArtifactStorage(self.root / "artifacts")
        self.database = self.root / "agent-state.sqlite"
        command = CommandSpec(("marker-build",))
        self.plan = BuildPlan(
            "marker-plan",
            "marker-project",
            "marker",
            (BuildStep("marker", BuildStage.BUILD, command),),
            metadata={"dockerfile": "Dockerfile"},
        )
        self.profile = ProjectProfile(
            "marker-project",
            SourceReference("fixture"),
            languages=("Python",),
            dockerfiles=("Dockerfile",),
        )
        initial_tool = MarkerBuildTool(self.workspace, self.storage, self.plan)
        initial = initial_tool.invoke(
            {}, ToolContext("multi-round", 0, str(self.workspace), self.profile)
        )
        self.state = create_agent_state("multi-round")
        self.state.update(
            project_profile=self.profile,
            build_plan=self.plan,
            build_result=initial.build_result,
            failure=initial.failure,
        )
        self.config = AgentConfig(
            max_attempts=4,
            max_repeated_failures=2,
            max_total_seconds=60,
            max_context_characters=6_000,
            max_recent_modifications=1,
            max_failed_methods=2,
            max_resolved_issues=2,
        )

    def workflow(self, planner, persistence):
        tools = ToolRegistry(
            (
                ReadFileTool(),
                GetBuildLogTool(self.storage),
                ModifyBuildScriptTool(self.storage),
                MarkerBuildTool(self.workspace, self.storage, self.plan),
            )
        )
        return AgentWorkflow(
            planner,
            tools,
            self.storage,
            self.config,
            persistence,
        )

    def test_build_strategy_attempt_tool_data_round_trips_through_checkpoint_serde(self):
        persistence = SQLiteAgentPersistence(self.database, self.storage)
        self.addCleanup(persistence.close)
        attempt = BuildStrategyAttempt(
            self.plan,
            self.state["build_result"],
            self.state["failure"],
        )
        tool_result = ToolResult(
            "build_image",
            False,
            "portfolio selected a repair baseline",
            data={"strategy_attempts": (attempt,)},
        )

        encoded = persistence.checkpointer.serde.dumps_typed(tool_result)
        restored = persistence.checkpointer.serde.loads_typed(encoded)

        restored_attempt = restored.data["strategy_attempts"][0]
        self.assertIsInstance(restored_attempt, BuildStrategyAttempt)
        self.assertEqual(restored_attempt.plan.plan_id, self.plan.plan_id)
        self.assertEqual(
            restored_attempt.failure.fingerprint,
            self.state["failure"].fingerprint,
        )

    def test_multiround_checkpoint_resume_summary_and_duplicate_guard(self):
        first_persistence = SQLiteAgentPersistence(self.database, self.storage)
        first_planner = MarkerPlanner()
        first_workflow = self.workflow(first_planner, first_persistence)
        paused = first_workflow.run(
            self.state,
            self.workspace,
            interrupt_after=("evaluate",),
        )

        self.assertEqual(paused["phase"], AgentPhase.CLASSIFYING)
        self.assertEqual(paused["attempt_number"], 1)
        self.assertEqual(paused["failure"].fingerprint, "failure:fail_b")
        paused_checkpoint_count = first_persistence.checkpoint_count("multi-round")
        self.assertGreater(paused_checkpoint_count, 5)
        first_plan = first_planner.plans[0]
        first_fingerprint = method_fingerprint(first_plan)
        self.assertTrue(first_persistence.was_attempted("multi-round", first_fingerprint))
        first_workflow.close()

        second_persistence = SQLiteAgentPersistence(self.database, self.storage)
        second_planner = MarkerPlanner()
        second_workflow = self.workflow(second_planner, second_persistence)
        restored = second_workflow.persisted_state("multi-round")
        self.assertEqual(restored["attempt_number"], 1)
        self.assertTrue(restored["context_summary"].resolved_issues)
        self.assertEqual(len(restored["context_summary"].failed_methods), 1)

        final = second_workflow.resume("multi-round")

        self.assertEqual(final["phase"], AgentPhase.COMPLETED)
        self.assertEqual(final["attempt_number"], 2)
        self.assertNotIn("FAIL_", (self.workspace / "Dockerfile").read_text())
        self.assertTrue(second_planner.seen_summaries[0].resolved_issues)
        self.assertEqual(len(final["environment_diffs"]), 1)
        self.assertLessEqual(len(final["context_summary"].failed_methods), 2)
        history_artifacts = [
            item for item in final["artifacts"] if "/history/" in item.key
        ]
        self.assertEqual(len(history_artifacts), 2)
        first_record = json.loads(self.storage.load(history_artifacts[0]))
        self.assertEqual(first_record["attempt_number"], 1)
        self.assertEqual(first_record["outcome"], "failed")
        self.assertEqual(first_record["failure_after"]["fingerprint"], "failure:fail_b")
        self.assertGreater(
            second_persistence.checkpoint_count("multi-round"), paused_checkpoint_count
        )

        duplicate_state = dict(final)
        duplicate_state["diagnosis"] = "try old plan again"
        duplicate_state["failure"] = classified_failure("FAIL_B")
        duplicate_result = self.workflow(
            DuplicatePlanner(first_plan), second_persistence
        ).plan_fix(duplicate_state)
        self.assertEqual(duplicate_result["phase"], AgentPhase.STOPPED)
        self.assertIn("duplicate repair method", duplicate_result["stop_reason"])
        second_workflow.close()


if __name__ == "__main__":
    unittest.main()
