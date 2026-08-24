"""Top-level LangGraph orchestration for a complete environment build."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from dprauto.agent.models import ContextSummary, ToolContext
from dprauto.agent.state import AgentState, create_agent_state
from dprauto.agent.workflow import AgentWorkflow
from dprauto.domain.enums import (
    AgentPhase,
    BuildFailureKind,
    BuildStatus,
    EnvironmentBuildStatus,
    FailureCategory,
    RegressionStatus,
    VerificationLevel,
)
from dprauto.domain.models import (
    BuildScriptSnapshot,
    EnvironmentBuildResult,
    SourceReference,
)
from dprauto.errors import AgentWorkflowError, DPRAutoError
from dprauto.ports.storage import Storage
from dprauto.serialization import to_json_bytes
from dprauto.time_budget import deadline_from, normalize_utc, time_budget_exhausted


class EnvironmentBuildWorkflow:
    """Compose existing parser/build/repair/verification services without duplicating them."""

    def __init__(self, repair_workflow: AgentWorkflow, storage: Storage) -> None:
        if not repair_workflow.tools.has("inspect_project"):
            raise ValueError("environment workflow requires the inspect_project tool")
        self.repair = repair_workflow
        self.storage = storage
        self.persistence = repair_workflow.persistence
        self.graph = self._compile_graph()

    def run(
        self,
        run_id: str,
        source: SourceReference,
        workspace: Path,
        *,
        interrupt_after: tuple[str, ...] = (),
    ) -> EnvironmentBuildResult:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise AgentWorkflowError(f"agent workspace is not a directory: {workspace}")
        state = create_agent_state(run_id)
        state["source_reference"] = source
        state["workspace"] = str(workspace)
        state = self._prepare_state(state)
        final = self.graph.invoke(
            state,
            self._thread_config(run_id),
            interrupt_after=list(interrupt_after) or None,
        )
        result = final.get("final_result")
        if result is None:
            raise AgentWorkflowError(
                f"workflow {run_id} interrupted before a final result was produced"
            )
        return result

    def run_state(
        self,
        initial_state: AgentState,
        workspace: Path,
        *,
        interrupt_after: tuple[str, ...] = (),
    ) -> AgentState:
        """Advanced entry point used when a caller already owns the initial state."""

        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise AgentWorkflowError(f"agent workspace is not a directory: {workspace}")
        if not initial_state.get("run_id", "").strip():
            raise AgentWorkflowError("agent state requires run_id")
        if initial_state.get("source_reference") is None:
            raise AgentWorkflowError("environment workflow requires source_reference")
        state: AgentState = dict(initial_state)
        state["workspace"] = str(workspace)
        state = self._prepare_state(state)
        return self.graph.invoke(
            state,
            self._thread_config(state["run_id"]),
            interrupt_after=list(interrupt_after) or None,
        )

    def resume(
        self,
        run_id: str,
        *,
        interrupt_after: tuple[str, ...] = (),
    ) -> AgentState:
        if self.persistence is None:
            raise AgentWorkflowError("resume requires a durable AgentPersistence adapter")
        result = self.graph.invoke(
            None,
            self._thread_config(run_id),
            interrupt_after=list(interrupt_after) or None,
        )
        if result is None:
            raise AgentWorkflowError(f"no checkpoint found for run {run_id}")
        return result

    def persisted_state(self, run_id: str) -> AgentState:
        if self.persistence is None:
            raise AgentWorkflowError("persisted_state requires AgentPersistence")
        snapshot = self.graph.get_state(self._thread_config(run_id))
        if not snapshot.values:
            raise AgentWorkflowError(f"no checkpoint found for run {run_id}")
        return snapshot.values

    def close(self) -> None:
        self.repair.close()

    def parse_project(self, state: AgentState) -> dict[str, Any]:
        source = state.get("source_reference")
        if source is None:
            return self.repair._stopped("parse_project requires SourceReference")
        budget_stop = self.repair._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        context = ToolContext(
            run_id=state["run_id"],
            attempt_number=0,
            workspace=state["workspace"],
            deadline_at=state.get("deadline_at"),
        )
        arguments: dict[str, Any] = {"source": source.locator}
        if source.revision is not None:
            arguments["revision"] = source.revision
        if source.subdirectory is not None:
            arguments["subdirectory"] = source.subdirectory
        try:
            observation = self.repair.tools.invoke(
                "inspect_project", arguments, context
            )
        except DPRAutoError as exc:
            return self.repair._stopped(str(exc))
        profile = observation.data.get("project_profile")
        if profile is None:
            return self.repair._stopped("inspect_project returned no ProjectProfile")
        return {
            "phase": AgentPhase.INSPECTING,
            "project_profile": profile,
            "tool_results": (observation,),
            "artifacts": self.repair._append(
                state.get("artifacts", ()), *observation.artifacts, limit=200
            ),
            "summaries": self.repair._append(
                state.get("summaries", ()), observation.summary
            ),
        }

    def classify_failure(self, state: AgentState) -> dict[str, Any]:
        """Route an already structured classifier result; never expose raw logs here."""

        result = state.get("build_result")
        failure = state.get("failure")
        if result is None:
            return self.repair._stopped("failure classification requires BuildResult")
        if result.status is BuildStatus.SUCCEEDED and failure is None:
            return {"phase": AgentPhase.VERIFYING}
        if failure is None:
            return self.repair._stopped(
                "failed build returned no classified FailureInfo"
            )
        ineligible = self.repair.repair_ineligibility_reason(state)
        if ineligible:
            return self.repair._stopped(ineligible)
        return {"phase": AgentPhase.CLASSIFYING}

    def finalize(self, state: AgentState) -> dict[str, Any]:
        state = self.repair.finalize_candidate_state(state)
        status = self._final_status(state)
        report = state.get("verification_report")
        scripts = self._build_script_snapshots(state)
        result = EnvironmentBuildResult(
            run_id=state["run_id"],
            final_status=status,
            project_profile=state.get("project_profile"),
            build_strategy=(
                state["build_plan"].strategy if state.get("build_plan") is not None else None
            ),
            build_plan=state.get("build_plan"),
            build_result=state.get("build_result"),
            agent_participated=state.get("agent_participated", False),
            repair_attempts=state.get("attempt_number", 0),
            final_build_scripts=scripts,
            installability=(
                report.result_for(VerificationLevel.INSTALLABILITY) if report else None
            ),
            testability=(
                report.result_for(VerificationLevel.TESTABILITY) if report else None
            ),
            runnability=(
                report.result_for(VerificationLevel.RUNNABILITY) if report else None
            ),
            verification_report=report,
            regression_result=state.get("regression_result"),
            environment_diff=state.get("environment_diff"),
            environment_diffs=state.get("environment_diffs", ()),
            failure=state.get("failure"),
            total_duration_seconds=self.repair._elapsed_seconds(state),
            llm_call_count=state.get("llm_call_count", 0),
            stop_reason=state.get("stop_reason", ""),
            artifacts=state.get("artifacts", ()),
        )
        artifact = self.storage.save(
            f"agent-runs/{state['run_id']}/results/final-{uuid4().hex}.json",
            to_json_bytes(result),
            media_type="application/json",
        )
        result = replace(result, artifacts=(*result.artifacts, artifact))
        update: dict[str, Any] = {
            "phase": (
                AgentPhase.COMPLETED if status is EnvironmentBuildStatus.SUCCEEDED
                else AgentPhase.STOPPED
            ),
            "final_result": result,
            "artifacts": result.artifacts,
            "repair_candidate_history": state.get("repair_candidate_history", ()),
        }
        if state.get("repair_candidate") is not None:
            update["repair_candidate"] = state["repair_candidate"]
        return update

    def _compile_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("parse_project", self.parse_project)
        builder.add_node("standard_build", self.repair.execute)
        builder.add_node("failure_classification", self.classify_failure)
        builder.add_node("failure_evaluation", self.repair.evaluate)
        builder.add_node("agent_repair_analyze", self.repair.analyze_failure)
        builder.add_node("agent_repair_plan", self.repair.plan_fix)
        builder.add_node("agent_repair_apply", self.repair.apply_fix)
        builder.add_node("repair_preflight", self.repair.preflight)
        builder.add_node("rebuild", self.repair.execute)
        builder.add_node("verification", self.repair.verify)
        builder.add_node("regression_evaluation", self.repair.evaluate)
        builder.add_node("environment_diff_and_result", self.finalize)

        builder.add_edge(START, "parse_project")
        builder.add_conditional_edges(
            "parse_project",
            self._continue_or_finalize,
            {"continue": "standard_build", "finalize": "environment_diff_and_result"},
        )
        builder.add_conditional_edges(
            "standard_build",
            self._post_build_route,
            {
                "verify": "verification",
                "classify": "failure_classification",
                "finalize": "environment_diff_and_result",
            },
        )
        builder.add_conditional_edges(
            "failure_classification",
            self._classification_route,
            {
                "evaluate": "failure_evaluation",
                "verify": "verification",
                "finalize": "environment_diff_and_result",
            },
        )
        builder.add_conditional_edges(
            "failure_evaluation",
            self._evaluation_route,
            {"repair": "agent_repair_analyze", "finalize": "environment_diff_and_result"},
        )
        builder.add_conditional_edges(
            "agent_repair_analyze",
            self._continue_or_finalize,
            {"continue": "agent_repair_plan", "finalize": "environment_diff_and_result"},
        )
        builder.add_conditional_edges(
            "agent_repair_plan",
            self._continue_or_finalize,
            {"continue": "agent_repair_apply", "finalize": "environment_diff_and_result"},
        )
        builder.add_conditional_edges(
            "agent_repair_apply",
            self._continue_or_finalize,
            {"continue": "repair_preflight", "finalize": "environment_diff_and_result"},
        )
        builder.add_conditional_edges(
            "repair_preflight",
            self._repair_preflight_route,
            {
                "rebuild": "rebuild",
                "verify": "verification",
                "evaluate": "failure_evaluation",
                "finalize": "environment_diff_and_result",
            },
        )
        builder.add_conditional_edges(
            "rebuild",
            self._post_build_route,
            {
                "verify": "verification",
                "classify": "failure_classification",
                "finalize": "environment_diff_and_result",
            },
        )
        builder.add_edge("verification", "regression_evaluation")
        builder.add_conditional_edges(
            "regression_evaluation",
            self._evaluation_route,
            {"repair": "agent_repair_analyze", "finalize": "environment_diff_and_result"},
        )
        builder.add_edge("environment_diff_and_result", END)
        checkpointer = self.persistence.checkpointer if self.persistence is not None else None
        return builder.compile(
            checkpointer=checkpointer,
            name="dprauto-environment-build-agent",
        )

    def _prepare_state(self, state: AgentState) -> AgentState:
        started_at = normalize_utc(state.get("started_at") or datetime.now(timezone.utc))
        state["started_at"] = started_at
        state.setdefault(
            "deadline_at",
            deadline_from(started_at, self.repair.config.max_total_seconds),
        )
        state.setdefault("attempt_number", 0)
        state.setdefault("repeated_failure_count", 0)
        state.setdefault("agent_participated", False)
        state.setdefault("llm_call_count", 0)
        state.setdefault("tool_results", ())
        state.setdefault("environment_diffs", ())
        state.setdefault("repair_candidate_history", ())
        state.setdefault("verification_results", ())
        state.setdefault("failure_history", ())
        state.setdefault("artifacts", ())
        state.setdefault("summaries", ())
        state.setdefault("context_summary", ContextSummary())
        if self.persistence is not None:
            persisted = self.persistence.recent_failed_methods(
                state["run_id"], self.repair.config.max_failed_methods
            )
            state["context_summary"] = self.repair.context_manager.merge_persisted_methods(
                state["context_summary"], persisted
            )
        return state

    @staticmethod
    def _is_infrastructure_failure(failure) -> bool:
        return failure.infrastructure_related or failure.kind in {
            BuildFailureKind.GIT,
            BuildFailureKind.NETWORK,
            BuildFailureKind.DOCKER_INFRASTRUCTURE,
        }

    @staticmethod
    def _continue_or_finalize(state: AgentState) -> Literal["continue", "finalize"]:
        return "finalize" if state.get("phase") is AgentPhase.STOPPED else "continue"

    @staticmethod
    def _post_build_route(
        state: AgentState,
    ) -> Literal["verify", "classify", "finalize"]:
        if state.get("phase") is AgentPhase.STOPPED:
            return "finalize"
        result = state.get("build_result")
        if result is None:
            return "finalize"
        if result.status is BuildStatus.SUCCEEDED and state.get("failure") is None:
            return "verify"
        return "classify"

    @staticmethod
    def _classification_route(
        state: AgentState,
    ) -> Literal["evaluate", "verify", "finalize"]:
        if state.get("phase") is AgentPhase.STOPPED:
            return "finalize"
        if state.get("phase") is AgentPhase.VERIFYING:
            return "verify"
        return "evaluate"

    @staticmethod
    def _evaluation_route(state: AgentState) -> Literal["repair", "finalize"]:
        return "repair" if state.get("phase") is AgentPhase.CLASSIFYING else "finalize"

    @staticmethod
    def _repair_preflight_route(
        state: AgentState,
    ) -> Literal["rebuild", "verify", "evaluate", "finalize"]:
        if state.get("phase") is AgentPhase.STOPPED:
            return "finalize"
        result = state.get("repair_preflight")
        if result is not None and not result.accepted:
            return "evaluate"
        if AgentWorkflow._is_verification_overlay_diff(state.get("environment_diff")):
            return "verify"
        return "rebuild"

    def _final_status(self, state: AgentState) -> EnvironmentBuildStatus:
        build = state.get("build_result")
        failure = state.get("failure")
        if (
            time_budget_exhausted(state.get("deadline_at"))
            or "maximum agent runtime reached" in state.get("stop_reason", "")
            or bool(build is not None and build.metadata.get("time_budget_exceeded", False))
            or (
                build is not None
                and build.status is BuildStatus.TIMED_OUT
                and self.repair._is_active_dependency_download_timeout(
                    failure
                )
            )
            or self.repair._is_active_test_progress_timeout(failure)
        ):
            return EnvironmentBuildStatus.TIME_BUDGET_EXCEEDED
        if failure is not None and self._is_infrastructure_failure(failure):
            return EnvironmentBuildStatus.INFRASTRUCTURE_FAILED
        if failure is not None and failure.category is FailureCategory.REGRESSION:
            return EnvironmentBuildStatus.REGRESSION
        if state.get("manual_review_required", False):
            return EnvironmentBuildStatus.MANUAL_REVIEW
        regression = state.get("regression_result")
        if regression is not None and regression.status is not RegressionStatus.PASSED:
            return EnvironmentBuildStatus.REGRESSION
        report = state.get("verification_report")
        if (
            build is not None
            and build.status is BuildStatus.SUCCEEDED
            and failure is None
            and report is not None
            and report.succeeded
        ):
            return EnvironmentBuildStatus.SUCCEEDED
        if build is not None and build.status is BuildStatus.SUCCEEDED and report is not None:
            return EnvironmentBuildStatus.VERIFICATION_FAILED
        if "maximum repair attempts reached" in state.get("stop_reason", ""):
            return EnvironmentBuildStatus.MAX_ATTEMPTS
        if state.get("project_profile") is None or build is None:
            return EnvironmentBuildStatus.ERROR
        return EnvironmentBuildStatus.PROJECT_FAILED

    def _build_script_snapshots(
        self, state: AgentState
    ) -> tuple[BuildScriptSnapshot, ...]:
        workspace = Path(state["workspace"]).resolve()
        selected = list(self.repair._build_scripts(state))
        selected.extend(("Dockerfile", "setup.sh"))
        snapshots: list[BuildScriptSnapshot] = []
        for value in dict.fromkeys(selected):
            relative = PurePosixPath(value)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = (workspace / relative.as_posix()).resolve()
            if workspace not in target.parents or not target.is_file() or target.is_symlink():
                continue
            if target.stat().st_size > 512 * 1024:
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            snapshots.append(
                BuildScriptSnapshot(
                    relative.as_posix(),
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
        return tuple(snapshots)

    def _thread_config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": max(35, self.repair.config.max_attempts * 9 + 20),
        }
