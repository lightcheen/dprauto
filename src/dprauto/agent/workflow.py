"""LangGraph orchestration for bounded build-environment repair."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

from langgraph.graph import END, START, StateGraph

from dprauto.agent.context import AgentContextManager, method_fingerprint
from dprauto.agent.candidates import RepairCandidateManager
from dprauto.agent.models import (
    ContextSummary,
    EvidencePack,
    RepairCandidate,
    RepairRecord,
    ToolContext,
    ToolResult,
)
from dprauto.agent.search_space import (
    bounded_repair_search_space,
    dependency_candidates,
    failure_family,
)
from dprauto.agent.state import AgentState
from dprauto.agent.tools.registry import ToolRegistry
from dprauto.config import AgentConfig
from dprauto.domain.enums import (
    AgentPhase,
    BuildFailureKind,
    BuildStage,
    BuildStatus,
    ChangeKind,
    FailureCategory,
    RegressionStatus,
    RiskLevel,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import (
    EnvironmentDiff,
    FailureInfo,
    FileChange,
    RegressionResult,
    RepairPreflightResult,
)
from dprauto.errors import AgentWorkflowError, DPRAutoError
from dprauto.inspection.security import is_sensitive_repository_path
from dprauto.ports.repair import InvestigationPlanner, RepairPlanner
from dprauto.ports.environment import EnvironmentDiffer
from dprauto.ports.regression import RegressionChecker
from dprauto.ports.persistence import AgentPersistence
from dprauto.ports.preflight import RepairPreflightRunner
from dprauto.ports.storage import Storage
from dprauto.ports.verification import VerificationRunner
from dprauto.serialization import to_json_bytes
from dprauto.time_budget import deadline_from, normalize_utc, time_budget_exhausted
from dprauto.verification.overlay import VERIFICATION_REQUIREMENTS_PATH


def _python_requirement_name(value: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
    return match.group(0).replace("_", "-").casefold() if match else value.casefold()


class AgentWorkflow:
    """A compiled StateGraph whose nodes each own one repair responsibility."""

    _REQUIRED_TOOLS = frozenset({"read_file", "get_build_log", "build_image"})
    _INVESTIGATION_TOOLS = frozenset(
        {
            "list_project_files",
            "query_repository_context",
            "read_file",
            "search_project",
        }
    )

    def __init__(
        self,
        planner: RepairPlanner,
        tools: ToolRegistry,
        storage: Storage,
        config: AgentConfig | None = None,
        persistence: AgentPersistence | None = None,
        verification_runner: VerificationRunner | None = None,
        regression_checker: RegressionChecker | None = None,
        environment_differ: EnvironmentDiffer | None = None,
        preflight_runner: RepairPreflightRunner | None = None,
    ) -> None:
        missing = sorted(name for name in self._REQUIRED_TOOLS if not tools.has(name))
        if missing:
            raise ValueError(f"agent workflow is missing required tools: {', '.join(missing)}")
        self.planner = planner
        self.tools = tools
        self.storage = storage
        self.config = config or AgentConfig()
        self.persistence = persistence
        self.verification_runner = verification_runner
        self.regression_checker = regression_checker
        self.environment_differ = environment_differ
        self.preflight_runner = preflight_runner
        self.candidates = RepairCandidateManager(storage)
        self.context_manager = AgentContextManager(self.config)
        self.graph = self._compile_graph()

    def run(
        self,
        initial_state: AgentState,
        workspace: Path,
        *,
        interrupt_after: tuple[str, ...] = (),
    ) -> AgentState:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise AgentWorkflowError(f"agent workspace is not a directory: {workspace}")
        if not initial_state.get("run_id", "").strip():
            raise AgentWorkflowError("agent state requires run_id")
        state: AgentState = dict(initial_state)
        state["workspace"] = str(workspace)
        started_at = normalize_utc(state.get("started_at") or datetime.now(timezone.utc))
        state["started_at"] = started_at
        state.setdefault("deadline_at", deadline_from(started_at, self.config.max_total_seconds))
        state.setdefault("attempt_number", 0)
        state.setdefault("repeated_failure_count", 0)
        state.setdefault("stagnant_failure_count", 0)
        state.setdefault("agent_participated", False)
        state.setdefault("llm_call_count", 0)
        state.setdefault("tool_results", ())
        state.setdefault("environment_diffs", ())
        state.setdefault("repair_candidate_history", ())
        state.setdefault("verification_results", ())
        state.setdefault("artifacts", ())
        state.setdefault("summaries", ())
        state.setdefault("context_summary", ContextSummary())
        if (
            self.regression_checker is not None
            and state.get("regression_baseline") is None
            and state.get("verification_report") is not None
            and state.get("project_profile") is not None
            and not state["verification_report"].succeeded
        ):
            baseline = self.regression_checker.create_baseline(
                state["project_profile"], state["verification_report"]
            )
            state["regression_baseline"] = baseline
            state["artifacts"] = self._append(
                state.get("artifacts", ()), *baseline.artifacts, limit=200
            )
        if self.persistence is not None:
            persisted = self.persistence.recent_failed_methods(
                state["run_id"], self.config.max_failed_methods
            )
            state["context_summary"] = self.context_manager.merge_persisted_methods(
                state["context_summary"], persisted
            )
        if state.get("failure") is not None and not state.get("failure_history"):
            state["failure_history"] = (state["failure"],)
        else:
            state.setdefault("failure_history", ())
        recursion_limit = max(25, self.config.max_attempts * 7 + 10)
        return self.graph.invoke(
            state,
            self._thread_config(state["run_id"], recursion_limit),
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
        if not run_id.strip():
            raise AgentWorkflowError("resume requires run_id")
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
        self.candidates.close()
        if self.persistence is not None:
            self.persistence.close()

    def _compile_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("analyze_failure", self.analyze_failure)
        builder.add_node("plan_fix", self.plan_fix)
        builder.add_node("apply_fix", self.apply_fix)
        builder.add_node("preflight", self.preflight)
        builder.add_node("execute", self.execute)
        builder.add_node("verify", self.verify)
        builder.add_node("evaluate", self.evaluate)
        builder.add_conditional_edges(
            START,
            self._entry_route,
            {"analyze_failure": "analyze_failure", "execute": "execute", "end": END},
        )
        builder.add_conditional_edges(
            "analyze_failure",
            self._continue_route,
            {"continue": "plan_fix", "end": END},
        )
        builder.add_conditional_edges(
            "plan_fix",
            self._continue_route,
            {"continue": "apply_fix", "end": END},
        )
        builder.add_conditional_edges(
            "apply_fix",
            self._continue_route,
            {"continue": "preflight", "end": END},
        )
        builder.add_conditional_edges(
            "preflight",
            self._preflight_route,
            {
                "execute": "execute",
                "verify": "verify",
                "evaluate": "evaluate",
                "end": END,
            },
        )
        builder.add_edge("execute", "verify")
        builder.add_edge("verify", "evaluate")
        builder.add_conditional_edges(
            "evaluate",
            self._evaluation_route,
            {"continue": "analyze_failure", "end": END},
        )
        checkpointer = self.persistence.checkpointer if self.persistence is not None else None
        return builder.compile(
            checkpointer=checkpointer,
            name="dprauto-repair-agent",
        )

    def analyze_failure(self, state: AgentState) -> dict[str, Any]:
        failure = state.get("failure")
        if failure is None:
            return self._stopped("analyze_failure requires a classified FailureInfo")
        ineligible = self.repair_ineligibility_reason(state)
        if ineligible:
            return self._stopped(ineligible)
        if state.get("attempt_number", 0) >= self.config.max_attempts:
            return self._stopped(f"maximum repair attempts reached: {self.config.max_attempts}")
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        observations: list[ToolResult] = []
        llm_call_count = state.get("llm_call_count", 0)
        evidence_pack = EvidencePack(stop_reason="investigation did not start")
        context = self._context(state)
        try:
            # A successful image's build log is irrelevant (and often large) when
            # Testability/Runnability failed. Those failures carry their bounded
            # command/output evidence in FailureInfo instead.
            if (
                failure.failure_stage not in {BuildStage.TEST, BuildStage.STARTUP}
                and "repair_preflight=true" not in failure.evidence
            ):
                observations.append(self.tools.invoke("get_build_log", {}, context))
            for build_script in self._build_scripts(state):
                observations.append(
                    self.tools.invoke("read_file", {"path": build_script}, context)
                )
            investigation_rounds = 0
            investigation_actions = 0
            investigation_complete = False
            investigation_reason = "planner does not support read-only investigation"
            investigation_tools = {
                name: specification
                for name, specification in self.tools.specifications.items()
                if name in self._INVESTIGATION_TOOLS
                and specification.get("effect") == "observe"
            }
            if (
                self.tools.has("list_project_files")
                and isinstance(self.planner, InvestigationPlanner)
                and investigation_tools
            ):
                seen_actions: set[str] = set()
                investigation_reason = "maximum investigation rounds reached"
                for investigation_round in range(1, self.config.max_investigation_rounds + 1):
                    if time_budget_exhausted(state.get("deadline_at")):
                        investigation_reason = self._time_budget_stop_reason()
                        break
                    llm_call_count += 1
                    investigation_rounds = investigation_round
                    decision = self.planner.plan_investigation(
                        state,
                        tuple(observations),
                        investigation_tools,
                    )
                    if decision.complete:
                        investigation_complete = True
                        investigation_reason = decision.rationale or "evidence is sufficient"
                        break
                    remaining = (
                        self.config.max_investigation_actions - investigation_actions
                    )
                    if remaining <= 0:
                        investigation_reason = "maximum investigation actions reached"
                        break
                    selected = decision.actions[:remaining]
                    made_progress = False
                    for action in selected:
                        fingerprint = hashlib.sha256(to_json_bytes(action)).hexdigest()
                        if fingerprint in seen_actions:
                            continue
                        seen_actions.add(fingerprint)
                        made_progress = True
                        investigation_actions += 1
                        violation = self._investigation_policy_violation(
                            action.tool,
                            action.arguments,
                            investigation_tools,
                        )
                        if violation:
                            observations.append(ToolResult(action.tool, False, violation))
                            continue
                        try:
                            observations.append(
                                self.tools.invoke(action.tool, action.arguments, context)
                            )
                        except DPRAutoError as exc:
                            observations.append(
                                ToolResult(
                                    action.tool,
                                    False,
                                    f"investigation tool failed: {exc}",
                                )
                            )
                    if not made_progress:
                        investigation_reason = (
                            "investigation stopped because every requested action was repeated"
                        )
                        break
                    if investigation_actions >= self.config.max_investigation_actions:
                        investigation_reason = "maximum investigation actions reached"
                        break
            evidence_pack = self.context_manager.build_evidence_pack(
                tuple(observations),
                rounds=investigation_rounds,
                action_count=investigation_actions,
                completed=investigation_complete,
                stop_reason=investigation_reason,
            )
            analysis_state: AgentState = dict(state)
            analysis_state["evidence_pack"] = evidence_pack
            llm_call_count += 1
            diagnosis = self.planner.analyze_failure(
                analysis_state,
                tuple(observations),
            )
        except DPRAutoError as exc:
            return self._stopped(
                str(exc),
                tool_results=tuple(observations),
                agent_participated=True,
                llm_call_count=llm_call_count,
                evidence_pack=evidence_pack,
            )
        return {
            "phase": AgentPhase.DIAGNOSING,
            "agent_participated": True,
            "llm_call_count": llm_call_count,
            "diagnosis": diagnosis,
            "evidence_pack": evidence_pack,
            "tool_results": tuple(observations),
            "summaries": self._append(state.get("summaries", ()), f"diagnosis: {diagnosis}"),
        }

    def plan_fix(self, state: AgentState) -> dict[str, Any]:
        diagnosis = state.get("diagnosis", "").strip()
        if not diagnosis:
            return self._stopped("plan_fix requires a diagnosis")
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        failure = state.get("failure")
        if failure is None:
            return self._stopped("plan_fix requires a classified FailureInfo")
        search_space, planning_tools = bounded_repair_search_space(
            failure,
            self.tools.specifications,
        )
        if not planning_tools:
            signals = "; ".join(search_space["evidence_signals"])
            return self._stopped(
                f"repair search space is empty for this failure: {signals}",
                repair_search_space=search_space,
                artifacts=self._search_space_artifacts(state, search_space, ()),
            )

        llm_call_count = state.get("llm_call_count", 0)
        feedback: list[str] = []
        for proposal_number in range(1, self.config.max_plan_feedback_rounds + 1):
            if time_budget_exhausted(state.get("deadline_at")):
                return self._stopped(
                    self._time_budget_stop_reason(),
                    llm_call_count=llm_call_count,
                    repair_search_space=search_space,
                    plan_feedback=tuple(feedback),
                    artifacts=self._search_space_artifacts(
                        state, search_space, tuple(feedback)
                    ),
                )
            llm_call_count += 1
            planning_state: AgentState = dict(state)
            planning_state["repair_search_space"] = search_space
            planning_state["plan_feedback"] = tuple(feedback)
            try:
                plan = self.planner.plan_fix(planning_state, diagnosis, planning_tools)
            except DPRAutoError as exc:
                return self._stopped(
                    str(exc),
                    llm_call_count=llm_call_count,
                    repair_search_space=search_space,
                    plan_feedback=tuple(feedback),
                    artifacts=self._search_space_artifacts(
                        state, search_space, tuple(feedback)
                    ),
                )

            outside = tuple(
                action.tool for action in plan.actions if action.tool not in planning_tools
            )
            fingerprint = method_fingerprint(plan)
            if outside:
                violation = (
                    "repair plan selected tool(s) outside the bounded search space: "
                    + ", ".join(dict.fromkeys(outside))
                )
            elif self._method_already_attempted(state, fingerprint):
                violation = (
                    "duplicate repair method rejected before execution: " + fingerprint
                )
            else:
                violation = self._plan_policy_violation(plan, failure, state)
            if not violation:
                return {
                    "phase": AgentPhase.PROPOSING,
                    "llm_call_count": llm_call_count,
                    "fix_plan": plan,
                    "current_method_fingerprint": fingerprint,
                    "repair_search_space": search_space,
                    "plan_feedback": tuple(feedback),
                    "artifacts": self._search_space_artifacts(
                        state, search_space, tuple(feedback)
                    ),
                    "summaries": self._append(
                        state.get("summaries", ()),
                        f"plan: {plan.summary or plan.hypothesis}",
                    ),
                }
            feedback.append(f"proposal {proposal_number}: {violation}")

        return self._stopped(
            "repair planning feedback exhausted: " + feedback[-1],
            llm_call_count=llm_call_count,
            repair_search_space=search_space,
            plan_feedback=tuple(feedback),
            artifacts=self._search_space_artifacts(
                state, search_space, tuple(feedback)
            ),
        )

    def _method_already_attempted(self, state: AgentState, fingerprint: str) -> bool:
        attempted = any(
            item.fingerprint == fingerprint
            for item in state.get("context_summary", ContextSummary()).failed_methods
        )
        if self.persistence is not None:
            attempted = attempted or self.persistence.was_attempted(
                state["run_id"], fingerprint
            )
        return attempted

    def _search_space_artifacts(
        self,
        state: AgentState,
        search_space: Mapping[str, Any],
        feedback: tuple[str, ...],
    ) -> tuple:
        round_number = state.get("attempt_number", 0) + 1
        artifact = self.storage.save(
            f"agent-runs/{state['run_id']}/rounds/{round_number:02d}/repair-search-space.json",
            to_json_bytes(
                {
                    "round": round_number,
                    "search_space": search_space,
                    "plan_feedback": feedback,
                }
            ),
            media_type="application/json",
        )
        return self._append(state.get("artifacts", ()), artifact, limit=200)

    def apply_fix(self, state: AgentState) -> dict[str, Any]:
        plan = state.get("fix_plan")
        if plan is None:
            return self._stopped("apply_fix requires a FixPlan")
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        attempt_number = state.get("attempt_number", 0) + 1
        try:
            candidate = self.candidates.create(state, attempt_number)
        except DPRAutoError as exc:
            return self._stopped(str(exc), attempt_number=attempt_number)
        candidate_history = state.get("repair_candidate_history", ())
        previous_candidate = state.get("repair_candidate")
        if previous_candidate is not None and previous_candidate.status == "pending":
            superseded = replace(
                previous_candidate,
                status="superseded",
                disposition_reason=f"superseded by {candidate.candidate_id}",
            )
            self.candidates.cleanup(previous_candidate)
            candidate_history = self._append(candidate_history, superseded, limit=50)
        context = self._context(
            state,
            attempt_number=attempt_number,
            workspace=candidate.workspace,
        )
        results: list[ToolResult] = []
        before_snapshot = None
        action_failure = ""
        try:
            if self.environment_differ is not None:
                before_snapshot = self.environment_differ.snapshot(
                    state["project_profile"], Path(candidate.workspace)
                )
        except DPRAutoError as exc:
            return self._stopped(
                str(exc),
                attempt_number=attempt_number,
                tool_results=tuple(results),
            ) | self._reject_candidate(candidate, candidate_history, str(exc))
        try:
            for action in plan.actions:
                result = self.tools.invoke(action.tool, action.arguments, context)
                results.append(result)
                if not result.succeeded:
                    action_failure = f"tool {action.tool} failed: {result.summary}"
                    break
        except DPRAutoError as exc:
            action_failure = str(exc)

        if self.environment_differ is not None and before_snapshot is not None:
            try:
                after_snapshot = self.environment_differ.snapshot(
                    state["project_profile"], Path(candidate.workspace)
                )
                combined = self.environment_differ.compare(before_snapshot, after_snapshot)
            except DPRAutoError as exc:
                return self._stopped(
                    str(exc),
                    attempt_number=attempt_number,
                    tool_results=tuple(results),
                ) | self._reject_candidate(candidate, candidate_history, str(exc))
        else:
            diffs = tuple(
                result.environment_diff
                for result in results
                if result.environment_diff is not None and not result.environment_diff.is_empty
            )
            combined = self._combine_diffs(diffs) if diffs else EnvironmentDiff()
        if combined.is_empty:
            reason = action_failure or "fix plan made no environment change"
            return self._stopped(
                reason,
                attempt_number=attempt_number,
                tool_results=tuple(results),
            ) | self._reject_candidate(candidate, candidate_history, reason)
        previous_diff = candidate.cumulative_environment_diff
        cumulative = (
            self._combine_diffs((previous_diff, combined))
            if previous_diff is not None
            else combined
        )
        candidate = self.candidates.with_diff(candidate, combined, cumulative)
        timeout_policy_violation = self._timeout_policy_violation(
            state.get("failure"), combined
        )
        verification_policy_violation = self._verification_repair_policy_violation(
            state.get("failure"), state.get("build_result"), state.get("build_plan"), combined
        )
        dimension_policy_violation = self._dimension_policy_violation(combined)
        whole_file_policy_violation = self._whole_file_policy_violation(plan, combined)
        diff_artifact = self.storage.save(
            f"agent-runs/{state['run_id']}/rounds/{attempt_number:02d}/environment-diff.json",
            to_json_bytes(combined),
            media_type="application/json",
        )
        result_artifacts = tuple(
            artifact for result in results for artifact in result.artifacts
        )
        update = {
            "phase": (
                AgentPhase.STOPPED
                if (
                    combined.requires_manual_review
                    or timeout_policy_violation
                    or verification_policy_violation
                    or dimension_policy_violation
                    or whole_file_policy_violation
                    or action_failure
                )
                else AgentPhase.APPLYING
            ),
            "attempt_number": attempt_number,
            "tool_results": tuple(results),
            "environment_diff": combined,
            "environment_diffs": self._append(
                state.get("environment_diffs", ()),
                combined,
                limit=self.config.max_recent_modifications,
            ),
            "artifacts": self._append(
                state.get("artifacts", ()),
                *result_artifacts,
                diff_artifact,
                limit=200,
            ),
            "summaries": self._append(
                state.get("summaries", ()),
                f"round {attempt_number}: {combined.summary}",
            ),
            "repair_candidate": candidate,
            "repair_candidate_history": candidate_history,
        }
        if combined.requires_manual_review:
            update["manual_review_required"] = True
            update["stop_reason"] = (
                "business source changed; current repair requires manual review"
            )
        elif timeout_policy_violation:
            update["stop_reason"] = timeout_policy_violation
        elif verification_policy_violation:
            update["stop_reason"] = verification_policy_violation
        elif dimension_policy_violation:
            update["stop_reason"] = dimension_policy_violation
        elif whole_file_policy_violation:
            update["stop_reason"] = whole_file_policy_violation
        elif action_failure:
            update["stop_reason"] = action_failure
        rejection_reason = update.get("stop_reason", "")
        if rejection_reason:
            update.update(
                self._reject_candidate(candidate, candidate_history, rejection_reason)
            )
        return update

    def preflight(self, state: AgentState) -> dict[str, Any]:
        """Reject explicit local syntax failures before paying for a full rebuild."""

        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        candidate = state.get("repair_candidate")
        environment_diff = state.get("environment_diff")
        if candidate is None or candidate.status != "pending" or environment_diff is None:
            return self._stopped(
                "repair preflight requires a pending candidate and EnvironmentDiff"
            )
        if self._is_verification_overlay_diff(environment_diff):
            result = RepairPreflightResult(
                VerificationStatus.SKIPPED,
                (),
                "Testability-only dependency overlay requires no image rebuild preflight",
            )
        elif self.preflight_runner is None:
            result = RepairPreflightResult(
                VerificationStatus.SKIPPED,
                (),
                "repair preflight runner is not configured; full build remains authoritative",
            )
        else:
            try:
                result = self.preflight_runner.run(
                    Path(candidate.workspace),
                    environment_diff,
                    deadline_at=state.get("deadline_at"),
                )
            except DPRAutoError as exc:
                result = RepairPreflightResult(
                    VerificationStatus.SKIPPED,
                    (),
                    f"repair preflight adapter was unavailable: {exc}",
                )
        attempt_number = state.get("attempt_number", 0)
        report_artifact = self.storage.save(
            f"agent-runs/{state['run_id']}/rounds/{attempt_number:02d}/repair-preflight.json",
            to_json_bytes(result),
            media_type="application/json",
        )
        artifacts = self._append(
            state.get("artifacts", ()),
            *result.artifacts,
            report_artifact,
            limit=200,
        )
        summaries = self._append(
            state.get("summaries", ()),
            f"round {attempt_number} preflight: {result.summary}",
        )
        if result.accepted:
            return {
                "phase": AgentPhase.POLICY_CHECK,
                "repair_preflight": result,
                "artifacts": artifacts,
                "summaries": summaries,
            }
        failure = self._preflight_failure(result)
        reason = result.summary
        return {
            "phase": AgentPhase.CLASSIFYING,
            "repair_preflight": result,
            "failure": failure,
            "stop_reason": "",
            "artifacts": artifacts,
            "summaries": summaries,
        } | self._reject_candidate(
            candidate,
            state.get("repair_candidate_history", ()),
            reason,
        )

    def execute(self, state: AgentState) -> dict[str, Any]:
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        effective_state: AgentState = state
        observations: list[ToolResult] = []
        previous_build = state.get("build_result")
        if state.get("attempt_number", 0) > 0 and self.tools.has("inspect_project"):
            profile = state.get("project_profile")
            inspect_arguments: dict[str, str] = {}
            if profile is not None:
                inspect_arguments["source"] = profile.source.locator
                if profile.source.revision:
                    inspect_arguments["revision"] = profile.source.revision
                if profile.source.subdirectory:
                    inspect_arguments["subdirectory"] = profile.source.subdirectory
            try:
                inspected = self.tools.invoke(
                    "inspect_project", inspect_arguments, self._context(state)
                )
            except DPRAutoError as exc:
                return self._stopped(str(exc))
            refreshed = inspected.data.get("project_profile")
            if refreshed is None:
                return self._stopped("inspect_project returned no ProjectProfile before rebuild")
            effective_state = dict(state)
            effective_state["project_profile"] = refreshed
            observations.append(inspected)
        try:
            result = self.tools.invoke("build_image", {}, self._context(effective_state))
        except DPRAutoError as exc:
            return self._stopped(str(exc))
        if result.build_plan is None or result.build_result is None:
            return self._stopped("build_image returned no BuildPlan/BuildResult")
        budget_exceeded = bool(result.build_result.metadata.get("time_budget_exceeded"))
        repaired_build_timed_out = (
            state.get("attempt_number", 0) > 0
            and previous_build is not None
            and previous_build.status is BuildStatus.SUCCEEDED
            and result.build_result.status is BuildStatus.TIMED_OUT
        )
        failure = result.failure
        if repaired_build_timed_out:
            failure = self._repair_timeout_regression_failure(
                previous_build,
                result.build_result,
            )
        stop_reason = (
            self._time_budget_stop_reason()
            if budget_exceeded
            else (
                "repair cost regression: previous build succeeded but repaired build timed out"
                if repaired_build_timed_out
                else ""
            )
        )
        return {
            "phase": (
                AgentPhase.STOPPED
                if budget_exceeded or repaired_build_timed_out
                else AgentPhase.BUILDING
            ),
            "project_profile": effective_state.get("project_profile"),
            "build_plan": result.build_plan,
            "build_result": result.build_result,
            "failure": failure,
            "stop_reason": stop_reason,
            "tool_results": (*observations, result),
            "artifacts": self._append(
                state.get("artifacts", ()), *result.artifacts, limit=200
            ),
        }

    def verify(self, state: AgentState) -> dict[str, Any]:
        """Run the pyramid after a successful image build, before evaluation."""

        result = state.get("build_result")
        profile = state.get("project_profile")
        plan = state.get("build_plan")
        if result is None or profile is None:
            return self._stopped("verify requires ProjectProfile and BuildResult")
        overlay_reverification = self._is_verification_overlay_diff(
            state.get("environment_diff")
        )
        if result.status is not BuildStatus.SUCCEEDED or (
            state.get("failure") is not None and not overlay_reverification
        ):
            return {"verification_results": ()}
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return budget_stop
        # Unit-level workflows may intentionally omit external runtime wiring. The
        # production composition root always injects the layered runner.
        if self.verification_runner is None:
            return {}
        try:
            report = self.verification_runner.verify(
                profile,
                result,
                Path(self._active_workspace(state)),
                build_plan=plan,
                deadline_at=state.get("deadline_at"),
            )
        except DPRAutoError as exc:
            return self._stopped(str(exc))
        update: dict[str, Any] = {
            "phase": AgentPhase.VERIFYING,
            "failure": None,
            "verification_report": report,
            "verification_results": report.results,
            "artifacts": self._append(
                state.get("artifacts", ()), *report.artifacts, limit=200
            ),
        }
        baseline = state.get("regression_baseline")
        if self.regression_checker is not None and baseline is not None:
            regression = self.regression_checker.check(baseline, report)
            update["regression_result"] = regression
            update["artifacts"] = self._append(
                update["artifacts"], *regression.artifacts, limit=200
            )
            if not regression.accepted:
                update["failure"] = self._regression_failure(regression)
                return update
        elif self.regression_checker is not None and not report.succeeded:
            baseline = self.regression_checker.create_baseline(profile, report)
            update["regression_baseline"] = baseline
            update["artifacts"] = self._append(
                update["artifacts"], *baseline.artifacts, limit=200
            )
        if not report.succeeded:
            update["failure"] = self._verification_failure(report)
        return update

    def evaluate(self, state: AgentState) -> dict[str, Any]:
        candidate_update = self._settle_candidate(state)
        if candidate_update:
            effective_state: AgentState = dict(state)
            effective_state.update(candidate_update)
            state = effective_state
        result = state.get("build_result")
        if result is None:
            return candidate_update | self._stopped("evaluate requires BuildResult")
        budget_stop = self._time_budget_stop(state)
        if budget_stop:
            return candidate_update | budget_stop
        if result.status is BuildStatus.SUCCEEDED and state.get("failure") is None:
            report = state.get("verification_report")
            if self.verification_runner is not None and report is None:
                return candidate_update | self._stopped(
                    "successful build returned no layered verification report"
                )
            return self._evaluation_update(state, outcome="succeeded") | candidate_update | {
                "phase": AgentPhase.COMPLETED,
                "stop_reason": (
                    "layered verification succeeded"
                    if self.verification_runner is not None
                    else "build succeeded"
                ),
                "repeated_failure_count": 0,
                "stagnant_failure_count": 0,
                "summaries": self._append(
                    state.get("summaries", ()),
                    (
                        f"round {state.get('attempt_number', 0)}: layered verification succeeded"
                        if self.verification_runner is not None
                        else f"round {state.get('attempt_number', 0)}: build succeeded"
                    ),
                ),
            }
        failure = state.get("failure")
        if failure is None:
            return candidate_update | self._stopped(
                "failed build returned no classified FailureInfo"
            )

        history = state.get("failure_history", ())
        previous = history[-1] if history else None
        repeated = (
            state.get("repeated_failure_count", 0) + 1
            if previous is not None and previous.fingerprint == failure.fingerprint
            else 0
        )
        stagnant = (
            state.get("stagnant_failure_count", 0) + 1
            if previous is not None and failure_family(previous) == failure_family(failure)
            else 0
        )
        updated_history = self._append(
            history,
            failure,
            limit=self.config.max_failed_methods + 1,
        )
        attempt_number = state.get("attempt_number", 0)
        stop_reason = ""
        ineligible = self.repair_ineligibility_reason(state)
        if ineligible:
            stop_reason = ineligible
        elif attempt_number >= self.config.max_attempts:
            stop_reason = f"maximum repair attempts reached: {self.config.max_attempts}"
        elif repeated >= self.config.max_repeated_failures:
            stop_reason = (
                f"same failure repeated {repeated} consecutive repair rounds: "
                f"{failure.fingerprint}"
            )
        elif stagnant >= self.config.max_repeated_failures:
            stop_reason = (
                f"no causal progress after {stagnant} consecutive repair rounds: "
                f"{failure_family(failure)}"
            )
        elif time_budget_exhausted(state.get("deadline_at")):
            stop_reason = self._time_budget_stop_reason()

        update: dict[str, Any] = {
            "phase": AgentPhase.STOPPED if stop_reason else AgentPhase.CLASSIFYING,
            "failure_history": updated_history,
            "repeated_failure_count": repeated,
            "stagnant_failure_count": stagnant,
            "summaries": self._append(
                state.get("summaries", ()),
                f"round {attempt_number}: {failure.category.value} ({failure.fingerprint})",
            ),
        }
        if stop_reason:
            update["stop_reason"] = stop_reason
        else:
            update["stop_reason"] = ""
        return self._evaluation_update(state, outcome="failed") | candidate_update | update

    @staticmethod
    def _verification_failure(report) -> FailureInfo:
        failed = next(
            (
                result
                for result in report.results
                if result.status in {VerificationStatus.FAILED, VerificationStatus.ERROR}
            ),
            report.results[-1],
        )
        if failed.level is VerificationLevel.TESTABILITY:
            stage = BuildStage.TEST
            category = FailureCategory.TEST
        elif failed.level is VerificationLevel.RUNNABILITY:
            stage = BuildStage.STARTUP
            category = FailureCategory.RUN
        else:
            stage = BuildStage.DEPENDENCY_INSTALLATION
            category = FailureCategory.VERIFICATION
        failed_checks = tuple(
            check
            for check in failed.checks
            if check.status in {VerificationStatus.FAILED, VerificationStatus.ERROR}
        )
        evidence_lines: list[str] = [
            f"verification_level={failed.level.value}",
            f"verification_status={failed.status.value}",
        ]
        command_results = failed.command_results
        command_result = command_results[-1] if command_results else None
        if command_result is not None:
            evidence_lines.extend(
                (
                    f"failed_command={command_result.command.display}",
                    f"exit_code={command_result.exit_code}",
                    f"timed_out={command_result.timed_out}",
                    f"duration_seconds={command_result.duration_seconds:.3f}",
                )
            )
        for check in failed_checks:
            evidence_lines.append(f"failed_check={check.identity}: {check.summary}")
        # Runtime adapters already bound this excerpt via VerificationConfig. Include
        # excerpts from sibling checks too: some run verifiers separate exit-code and
        # output assertions, while both describe the same failed process.
        for check in failed.checks:
            excerpt = check.metadata.get("output_excerpt")
            if isinstance(excerpt, str) and excerpt.strip():
                evidence_lines.append(
                    f"output_excerpt[{check.identity}]:\n{excerpt.strip()}"
                )
        top_excerpt = failed.metadata.get("output_excerpt")
        if isinstance(top_excerpt, str) and top_excerpt.strip():
            evidence_lines.append(f"output_excerpt:\n{top_excerpt.strip()}")
        if (
            failed.level is VerificationLevel.TESTABILITY
            and command_result is not None
            and command_result.timed_out
            and AgentWorkflow._has_active_test_progress("\n".join(evidence_lines))
        ):
            evidence_lines.append("timeout_activity=test-progress")
        key_log = "\n".join(evidence_lines)[-12_000:]
        identity = f"{failed.level.value}\0{key_log or failed.summary}"
        fingerprint = f"verification:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        network_infrastructure = AgentWorkflow._verification_network_failure(key_log)
        return FailureInfo(
            category=FailureCategory.NETWORK if network_infrastructure else category,
            failure_stage=stage,
            message=(
                "Network operation failed"
                if network_infrastructure
                else f"{failed.level.value} verification did not pass"
            ),
            fingerprint=fingerprint,
            kind=(
                BuildFailureKind.NETWORK
                if network_infrastructure
                else BuildFailureKind.PROJECT_BUILD
            ),
            failed_command=(failed.command_results[-1].command if failed.command_results else None),
            key_log=key_log or failed.summary,
            possible_cause=(
                "DNS, proxy, registry, or package download connectivity is unavailable or unstable"
                if network_infrastructure
                else "one or more layered environment checks failed"
            ),
            evidence=tuple(evidence_lines),
            retryable=True,
            infrastructure_related=network_infrastructure,
            confidence=1.0,
        )

    @staticmethod
    def _preflight_failure(result: RepairPreflightResult) -> FailureInfo:
        failed = tuple(
            check
            for check in result.checks
            if check.status is VerificationStatus.FAILED
        )
        key_log = "\n".join(check.summary for check in failed)[-6_000:]
        identity = "\0".join(
            f"{check.check_id}\0{check.summary}" for check in failed
        )
        command = next(
            (
                check.command_result.command
                for check in failed
                if check.command_result is not None
            ),
            None,
        )
        return FailureInfo(
            category=FailureCategory.BUILD_COMMAND,
            failure_stage=BuildStage.PLANNING,
            message="Repair preflight rejected the proposed environment change",
            fingerprint=f"preflight:{hashlib.sha256(identity.encode()).hexdigest()[:16]}",
            kind=BuildFailureKind.PROJECT_BUILD,
            failed_command=command,
            key_log=key_log or result.summary,
            possible_cause=(
                "the proposed Dockerfile or setup script is locally invalid before rebuild"
            ),
            evidence=(
                "repair_preflight=true",
                *(f"{check.check_id}={check.status.value}" for check in result.checks),
            ),
            suggestions=(
                "Correct only the explicit preflight error before requesting another full rebuild.",
            ),
            retryable=True,
            confidence=1.0,
        )

    @staticmethod
    def _verification_network_failure(key_log: str) -> bool:
        normalized = key_log.casefold()
        return any(
            marker in normalized
            for marker in (
                "temporary failure in name resolution",
                "could not resolve host",
                "failed to establish a new connection",
                "network is unreachable",
                "connection timed out",
                "connection reset",
                "readtimeouterror",
                "proxyerror",
                "tls handshake timeout",
            )
        )

    @staticmethod
    def _regression_failure(regression: RegressionResult) -> FailureInfo:
        rejected = tuple(
            item
            for item in regression.findings
            if item.regressed or not item.rerun
        )
        key_log = "\n".join(
            f"{item.expectation.identity}: {item.summary}" for item in rejected
        )
        identity = "\0".join(
            sorted(item.expectation.identity for item in rejected)
        )
        fingerprint = f"regression:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        return FailureInfo(
            category=FailureCategory.REGRESSION,
            failure_stage=BuildStage.TEST,
            message=(
                "repair introduced a regression"
                if regression.status is RegressionStatus.REGRESSION
                else "repair did not rerun every previously passing check"
            ),
            fingerprint=fingerprint,
            kind=BuildFailureKind.PROJECT_BUILD,
            key_log=key_log or regression.summary,
            possible_cause="the current environment change broke or omitted a prior passing check",
            evidence=tuple(item.expectation.identity for item in rejected),
            retryable=True,
            confidence=1.0,
        )

    def _context(
        self,
        state: AgentState,
        *,
        attempt_number: int | None = None,
        workspace: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            run_id=state["run_id"],
            attempt_number=(
                state.get("attempt_number", 0)
                if attempt_number is None
                else attempt_number
            ),
            workspace=workspace or self._active_workspace(state),
            project_profile=state.get("project_profile"),
            build_plan=state.get("build_plan"),
            build_result=state.get("build_result"),
            deadline_at=state.get("deadline_at"),
        )

    @staticmethod
    def _build_scripts(state: AgentState) -> tuple[str, ...]:
        workspace = AgentWorkflow._active_workspace(state)
        selected: list[str] = []
        plan = state.get("build_plan")
        if plan is not None:
            for key in ("dockerfile", "setup_script"):
                value = plan.metadata.get(key)
                if isinstance(value, str) and value:
                    target = Path(workspace) / value
                    if target.is_file():
                        selected.append(value)
        profile = state.get("project_profile")
        if profile is not None:
            for value in (
                *profile.dockerfiles,
                "Dockerfile",
                "setup.sh",
                VERIFICATION_REQUIREMENTS_PATH,
            ):
                if (Path(workspace) / value).is_file():
                    selected.append(value)
        return tuple(dict.fromkeys(selected))[:4]

    @staticmethod
    def _active_workspace(state: AgentState) -> str:
        candidate = state.get("repair_candidate")
        if candidate is not None and candidate.status == "pending":
            return candidate.workspace
        return state["workspace"]

    def _reject_candidate(
        self,
        candidate: RepairCandidate,
        history: tuple[RepairCandidate, ...],
        reason: str,
    ) -> dict[str, Any]:
        rejected = self.candidates.reject(candidate, reason)
        return {
            "repair_candidate": rejected,
            "repair_candidate_history": self._append(history, rejected, limit=50),
        }

    def _settle_candidate(self, state: AgentState) -> dict[str, Any]:
        """Promote an improving candidate, or discard a proven regression."""

        candidate = state.get("repair_candidate")
        if candidate is None or candidate.status != "pending":
            return {}
        result = state.get("build_result")
        if result is None:
            return {}
        regression = state.get("regression_result")
        if regression is not None and not regression.accepted:
            return self._restore_rejected_candidate(
                state, candidate, "candidate failed regression evaluation"
            )
        report = state.get("verification_report")
        accepted_build = candidate.accepted_build_result
        installability_improved = (
            result.status is BuildStatus.SUCCEEDED
            and (accepted_build is None or accepted_build.status is not BuildStatus.SUCCEEDED)
        )
        verification_succeeded = (
            result.status is BuildStatus.SUCCEEDED
            and state.get("failure") is None
            and (self.verification_runner is None or (report is not None and report.succeeded))
        )
        if not (installability_improved or verification_succeeded):
            return {}
        try:
            accepted = self.candidates.accept(candidate)
        except (DPRAutoError, OSError) as exc:
            return self._restore_rejected_candidate(
                state, candidate, f"candidate promotion failed: {exc}"
            ) | self._stopped(f"candidate promotion failed: {exc}")
        return {
            "repair_candidate": accepted,
            "repair_candidate_history": self._append(
                state.get("repair_candidate_history", ()), accepted, limit=50
            ),
        }

    def _restore_rejected_candidate(
        self,
        state: AgentState,
        candidate: RepairCandidate,
        reason: str,
    ) -> dict[str, Any]:
        update = self._reject_candidate(
            candidate, state.get("repair_candidate_history", ()), reason
        )
        update.update(
            {
                "project_profile": candidate.accepted_project_profile,
                "build_plan": candidate.accepted_build_plan,
                "build_result": candidate.accepted_build_result,
                "verification_report": candidate.accepted_verification_report,
                "verification_results": candidate.accepted_verification_results,
            }
        )
        return update

    def finalize_candidate_state(self, state: AgentState) -> AgentState:
        """Return a final-result view bound to the last accepted workspace state."""

        candidate = state.get("repair_candidate")
        if candidate is None or candidate.status != "pending":
            return state
        restored: AgentState = dict(state)
        restored.update(
            self._restore_rejected_candidate(
                state, candidate, "workflow finalized before candidate acceptance"
            )
        )
        return restored

    def _evaluation_update(self, state: AgentState, *, outcome: str) -> dict[str, Any]:
        plan = state.get("fix_plan")
        result = state.get("build_result")
        profile = state.get("project_profile")
        if plan is None or result is None or profile is None or state.get("attempt_number", 0) <= 0:
            return {}
        history = state.get("failure_history", ())
        previous_failure = history[-1] if history else None
        current_failure = state.get("failure")
        fingerprint = state.get("current_method_fingerprint") or method_fingerprint(plan)
        summary = self.context_manager.after_attempt(
            state.get("context_summary", ContextSummary()),
            previous_failure=previous_failure,
            current_failure=current_failure,
            plan=plan,
            fingerprint=fingerprint,
            outcome=outcome,
            attempt_number=state["attempt_number"],
            environment_diff=state.get("environment_diff"),
        )
        update: dict[str, Any] = {"context_summary": summary}
        if self.persistence is None:
            return update
        record = RepairRecord(
            run_id=state["run_id"],
            attempt_number=state["attempt_number"],
            created_at=result.finished_at,
            method_fingerprint=fingerprint,
            outcome=outcome,
            fix_plan=plan,
            project_id=profile.project_id,
            build_result=result,
            failure_before=previous_failure,
            failure_after=current_failure,
            environment_diff=state.get("environment_diff"),
            artifacts=state.get("artifacts", ())[-20:],
            round_feedback=(summary.round_feedback[-1] if summary.round_feedback else None),
        )
        history_artifact = self.persistence.record(record)
        update["artifacts"] = self._append(
            state.get("artifacts", ()), history_artifact, limit=200
        )
        return update

    @staticmethod
    def _combine_diffs(diffs: tuple[EnvironmentDiff, ...]) -> EnvironmentDiff:
        base_image = next((item.base_image for item in reversed(diffs) if item.base_image), None)
        python_version = next(
            (item.python_version for item in reversed(diffs) if item.python_version), None
        )
        risk_order = {
            RiskLevel.NONE: 0,
            RiskLevel.LOW: 1,
            RiskLevel.MEDIUM: 2,
            RiskLevel.HIGH: 3,
            RiskLevel.CRITICAL: 4,
        }
        risk_level = max(
            (item.risk_level for item in diffs),
            key=lambda value: risk_order[value],
            default=RiskLevel.NONE,
        )
        return EnvironmentDiff(
            files=AgentWorkflow._combine_file_changes(
                tuple(change for item in diffs for change in item.files)
            ),
            dependencies=tuple(change for item in diffs for change in item.dependencies),
            environment_variables=tuple(
                change for item in diffs for change in item.environment_variables
            ),
            base_image=base_image,
            python_version=python_version,
            system_packages=tuple(
                change for item in diffs for change in item.system_packages
            ),
            python_dependencies=tuple(
                change for item in diffs for change in item.python_dependencies
            ),
            startup_arguments=tuple(
                change for item in diffs for change in item.startup_arguments
            ),
            build_scripts=AgentWorkflow._combine_file_changes(
                tuple(change for item in diffs for change in item.build_scripts)
            ),
            business_source=AgentWorkflow._combine_file_changes(
                tuple(change for item in diffs for change in item.business_source)
            ),
            source_changed=any(item.source_changed for item in diffs),
            risk_level=risk_level,
            requires_manual_review=any(item.requires_manual_review for item in diffs),
            policy_violations=tuple(
                violation for item in diffs for violation in item.policy_violations
            ),
            summary="; ".join(item.summary for item in diffs if item.summary),
        )

    @staticmethod
    def _combine_file_changes(changes: tuple[FileChange, ...]) -> tuple[FileChange, ...]:
        """Keep the earliest precondition and latest result for each changed path."""

        combined: dict[str, FileChange] = {}
        for change in changes:
            previous = combined.get(change.path)
            if previous is None:
                combined[change.path] = change
                continue
            before_digest = previous.before_digest
            after_digest = change.after_digest
            kind = (
                ChangeKind.ADDED
                if before_digest is None and after_digest is not None
                else ChangeKind.REMOVED
                if after_digest is None
                else ChangeKind.MODIFIED
            )
            combined[change.path] = FileChange(
                change.path,
                kind,
                before_digest,
                after_digest,
            )
        return tuple(combined.values())

    @staticmethod
    def _elapsed_seconds(state: AgentState) -> float:
        started = state.get("started_at")
        if started is None:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()

    def _time_budget_stop(self, state: AgentState) -> dict[str, Any] | None:
        if time_budget_exhausted(state.get("deadline_at")):
            return self._stopped(self._time_budget_stop_reason())
        return None

    def _time_budget_stop_reason(self) -> str:
        return f"maximum agent runtime reached: {self.config.max_total_seconds}s"

    def _plan_policy_violation(
        self,
        plan,
        failure: FailureInfo | None = None,
        state: AgentState | None = None,
    ) -> str:
        specifications = self.tools.specifications
        mutating_actions = 0
        dimensions: set[str] = set()
        dimension_by_tool = {
            "patch_system_packages": "system-packages",
            "patch_python_dependencies": "python-dependencies",
            "patch_verification_dependencies": "verification-python-dependencies",
            "patch_base_image": "runtime",
            "patch_build_script": "build-script",
            "modify_build_script": "opaque-whole-file",
        }
        for action in plan.actions:
            specification = specifications.get(action.tool)
            if specification is None:
                return f"repair plan selected unavailable tool: {action.tool}"
            effect = specification.get("effect", "observe")
            if effect != "mutate":
                return (
                    f"repair plan selected non-mutating tool {action.tool}; "
                    "repair plans must change the build environment"
                )
            if action.tool == "patch_verification_dependencies" and (
                failure is None or failure.failure_stage is not BuildStage.TEST
            ):
                return (
                    "repair plan rejected: verification dependency overlays are only allowed "
                    "for Testability failures after a successful image build"
                )
            if action.tool == "patch_verification_dependencies":
                evidence_violation = self._verification_dependency_evidence_violation(
                    action.arguments,
                    failure,
                )
                if evidence_violation:
                    return evidence_violation
            if (
                failure is not None
                and failure.failure_stage is BuildStage.TEST
                and action.tool
                in {
                    "patch_system_packages",
                    "patch_python_dependencies",
                    "patch_base_image",
                    "patch_build_script",
                    "modify_build_script",
                }
            ):
                return (
                    "repair plan rejected: Testability dependencies must use "
                    "patch_verification_dependencies instead of changing the runtime image"
                )
            if state is not None and action.tool in {
                "modify_build_script",
                "patch_build_script",
            }:
                proof_violation = self._mutation_read_proof_violation(action, state)
                if proof_violation:
                    return proof_violation
            mutating_actions += 1
            dimensions.add(dimension_by_tool.get(action.tool, action.tool))
        if mutating_actions == 0:
            return "repair plan made no environment change"
        if "opaque-whole-file" in dimensions and mutating_actions > 1:
            return (
                "repair plan rejected: modify_build_script must be the only action in a "
                "whole-file fallback plan"
            )
        if "build-script" in dimensions and mutating_actions > 1:
            return (
                "repair plan rejected: patch_build_script must be the only action in an "
                "atomic source-SHA patch plan"
            )
        if len(dimensions) > 1:
            return (
                "repair plan rejected: one round may change only one high-risk environment "
                f"dimension ({', '.join(sorted(dimensions))})"
            )
        return ""

    def _mutation_read_proof_violation(self, action, state: AgentState) -> str:
        """Require prompt-visible source evidence before an opaque build-script mutation."""

        specification = self.tools.specifications.get(action.tool, {})
        schema = specification.get("argument_schema", {})
        required = schema.get("required", ()) if isinstance(schema, Mapping) else ()
        # Lightweight fake tools used by orchestration tests do not claim the production
        # CAS contract. The concrete tools still enforce the digest again at execution.
        if "source_sha256" not in required:
            return ""
        path = action.arguments.get("path")
        source_sha256 = action.arguments.get("source_sha256")
        if not isinstance(path, str) or not path.strip():
            return f"repair plan rejected: {action.tool} requires a non-empty path"
        if not isinstance(source_sha256, str) or not source_sha256.strip():
            return f"repair plan rejected: {action.tool} requires source_sha256"
        source_sha256 = source_sha256.casefold()
        if source_sha256 != "absent" and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            return (
                f"repair plan rejected: {action.tool} source_sha256 must be a SHA-256 "
                "digest or 'absent'"
            )

        context = self.context_manager.build_llm_context(
            state,
            state.get("tool_results", ()),
        )
        scripts = tuple(context.get("current_build_scripts", ()))
        matching_scripts = tuple(
            item
            for item in scripts
            if isinstance(item, Mapping)
            and item.get("path") == path
            and item.get("source_sha256") == source_sha256
        )
        if action.tool == "modify_build_script":
            if source_sha256 == "absent":
                relative = PurePosixPath(path)
                if relative.is_absolute() or ".." in relative.parts:
                    return (
                        "repair plan rejected: modify_build_script path must be a safe "
                        f"workspace-relative path: {path}"
                    )
                target = Path(self._active_workspace(state)) / relative.as_posix()
                return (
                    "repair plan rejected: source_sha256='absent' does not match an existing "
                    f"mutation target: {path}"
                    if target.exists()
                    else ""
                )
            if not any(bool(item.get("complete_file")) for item in matching_scripts):
                return (
                    "repair plan rejected: modify_build_script requires a complete read_file "
                    f"proof through EOF with matching source_sha256 for {path}; use "
                    "patch_build_script for a paged or truncated file"
                )
            return ""

        if source_sha256 == "absent":
            return "repair plan rejected: patch_build_script cannot patch an absent source"
        old_content = action.arguments.get("old_content")
        if not isinstance(old_content, str) or not old_content:
            return "repair plan rejected: patch_build_script requires non-empty old_content"
        if "characters omitted" in old_content:
            return (
                "repair plan rejected: patch_build_script old_content cannot include a "
                "context truncation marker"
            )
        prompt_visible_records = tuple(context.get("evidence", ()))
        matching_evidence = tuple(
            record.get("data", {})
            for record in prompt_visible_records
            if isinstance(record, Mapping)
            and record.get("tool") == "read_file"
            and isinstance(record.get("data"), Mapping)
            and record["data"].get("path") == path
            and record["data"].get("source_sha256") == source_sha256
        )
        visible_content = tuple(
            item.get("content")
            for item in (*matching_scripts, *matching_evidence)
            if isinstance(item.get("content"), str)
        )
        if not any(old_content in content for content in visible_content):
            return (
                "repair plan rejected: patch_build_script old_content and source_sha256 must "
                f"come from prompt-visible read_file evidence for {path}"
            )
        return ""

    @staticmethod
    def _verification_dependency_evidence_violation(
        arguments: Mapping[str, Any],
        failure: FailureInfo | None,
    ) -> str:
        """Require a dependency-shaped failure before allowing an overlay mutation."""

        if failure is None:
            return "repair plan rejected: verification dependency evidence is unavailable"
        evidence = "\n".join((*failure.evidence, failure.message, failure.key_log))
        normalized = evidence.casefold()
        direct_dependency_markers = (
            "modulenotfounderror",
            "no module named",
            "importerror: cannot import name",
            "distributionnotfound",
            "packagenotfounderror",
            "versionconflict",
            "resolutionimpossible",
            "could not find a version that satisfies",
            "no matching distribution found",
        )
        plugin_marker = (
            "unrecognized arguments:" in normalized
            or bool(re.search(r"fixture\s+['\"][^'\"]+['\"]\s+not found", normalized))
        )
        packages = arguments.get("packages")
        requested = (
            tuple(item for item in packages if isinstance(item, str))
            if isinstance(packages, (list, tuple))
            else ()
        )
        pytest_plugin_requested = any(
            _python_requirement_name(item).startswith("pytest-")
            for item in requested
        )
        normalized_names = tuple(_python_requirement_name(item) for item in requested)
        normalized_evidence = normalized.replace("_", "-")
        candidates = dependency_candidates(failure)
        compatibility_marker = (
            "deprecationwarning" in normalized
            and (
                ("please use" in normalized and "import" in normalized)
                or "deprecated" in normalized
            )
            and bool(normalized_names)
            and all(name in normalized_evidence for name in normalized_names)
        )
        if (
            not any(marker in normalized for marker in direct_dependency_markers)
            and not (plugin_marker and pytest_plugin_requested)
            and not compatibility_marker
        ):
            return (
                "repair plan rejected: verification dependency changes require direct "
                "missing-module, missing-plugin, distribution, version-conflict, or "
                "package-named collection compatibility evidence from Testability"
            )

        for item in requested:
            name = re.escape(_python_requirement_name(item))
            already_satisfied = re.search(
                rf"requirement already satisfied:\s+{name}(?:\b|\[)",
                normalized_evidence,
            )
            if already_satisfied:
                return (
                    "repair plan rejected: Testability evidence says the requested "
                    f"dependency is already satisfied: {item}"
                )
        unexpected = tuple(
            name for name in normalized_names if candidates and name not in candidates
        )
        if unexpected:
            return (
                "repair plan rejected: requested verification dependency is not named by "
                "the failure evidence (expected one of "
                + ", ".join(candidates)
                + "): "
                + ", ".join(unexpected)
            )
        return ""

    @staticmethod
    def _investigation_policy_violation(
        tool: str,
        arguments: Mapping[str, Any],
        available_tools: Mapping[str, Any],
    ) -> str:
        if tool not in available_tools:
            return f"investigation rejected unavailable or non-read-only tool: {tool}"
        if tool != "read_file":
            return ""
        value = arguments.get("path")
        if not isinstance(value, str) or not value.strip():
            return "investigation read_file requires a non-empty path"
        if is_sensitive_repository_path(value):
            return f"investigation refused sensitive project file: {value}"
        return ""

    @staticmethod
    def _dimension_policy_violation(diff: EnvironmentDiff) -> str:
        dimensions: list[str] = []
        if diff.base_image is not None or diff.python_version is not None:
            dimensions.append("runtime")
        if diff.system_packages:
            dimensions.append("system-packages")
        if diff.python_dependencies:
            dimensions.append("python-dependencies")
        if diff.startup_arguments:
            dimensions.append("startup")
        unique = tuple(dict.fromkeys(dimensions))
        if len(unique) > 1:
            return (
                "repair rejected after diff: one round changed multiple high-risk "
                f"environment dimensions ({', '.join(unique)})"
            )
        return ""

    @staticmethod
    def _whole_file_policy_violation(plan, diff: EnvironmentDiff) -> str:
        if not any(action.tool == "modify_build_script" for action in plan.actions):
            return ""
        if diff.base_image is not None or diff.python_version is not None:
            return (
                "whole-file repair rejected: use patch_base_image for an auditable runtime "
                "image change"
            )
        if diff.system_packages:
            return (
                "whole-file repair rejected: use patch_system_packages for an auditable OS "
                "dependency change"
            )
        if diff.python_dependencies:
            return (
                "whole-file repair rejected: use patch_python_dependencies for an auditable "
                "Python dependency change"
            )
        return ""

    def _timeout_policy_violation(
        self,
        failure: FailureInfo | None,
        diff: EnvironmentDiff,
    ) -> str:
        if not self._is_build_timeout_failure(failure):
            return ""
        if diff.base_image is not None:
            return "timeout repair rejected: base image changes require causal evidence"
        if diff.python_version is not None:
            return "timeout repair rejected: Python version changes require causal evidence"
        expanded = tuple(
            item
            for item in (*diff.dependencies, *diff.system_packages, *diff.python_dependencies)
            if item.kind in {ChangeKind.ADDED, ChangeKind.MODIFIED}
        )
        if expanded:
            names = ", ".join(dict.fromkeys(item.name for item in expanded))
            return (
                "timeout repair rejected: dependency expansion is not justified by "
                f"a build timeout alone ({names})"
            )
        return ""

    def _unrepairable_template_install_timeout(self, state: AgentState) -> str:
        failure = state.get("failure")
        if not self._is_build_timeout_failure(failure):
            return ""
        plan = state.get("build_plan")
        if plan is None or plan.strategy != "template":
            return ""
        if not self._is_python_package_install_timeout(failure):
            return ""
        if self._has_project_owned_build_script(state):
            return ""
        return (
            "timeout repair skipped: generated template dependency installation timed out "
            "and no project-owned Dockerfile/setup.sh can be safely narrowed"
        )

    def repair_ineligibility_reason(self, state: AgentState) -> str:
        """Explain why a classified failure must not consume an Agent/LLM round."""

        failure = state.get("failure")
        if failure is None:
            return ""
        if failure.infrastructure_related or failure.kind in {
            BuildFailureKind.GIT,
            BuildFailureKind.NETWORK,
            BuildFailureKind.DOCKER_INFRASTRUCTURE,
        }:
            return (
                "infrastructure failure is not eligible for project repair: "
                f"{failure.kind.value}"
            )
        if self._is_active_dependency_download_timeout(failure):
            return (
                "agent repair skipped: dependency download remained active at build timeout; "
                "retry with the persistent package cache or a larger build budget"
            )
        if self._is_active_test_progress_timeout(failure):
            return (
                "agent repair skipped: project tests remained active at verification timeout; "
                "use a representative bounded test selection or a larger verification budget"
            )
        template_timeout = self._unrepairable_template_install_timeout(state)
        if template_timeout:
            return template_timeout
        if self._is_project_test_assertion_failure(state):
            return (
                "agent repair skipped: project test assertions failed after a successful build; "
                "business-source changes are outside the environment-repair scope"
            )
        return ""

    @staticmethod
    def _is_active_dependency_download_timeout(failure: FailureInfo | None) -> bool:
        if failure is None:
            return False
        evidence = "\n".join((*failure.evidence, failure.key_log)).casefold()
        return "timeout_activity=dependency-download" in evidence

    @staticmethod
    def _is_active_test_progress_timeout(failure: FailureInfo | None) -> bool:
        if failure is None:
            return False
        evidence = "\n".join((*failure.evidence, failure.key_log)).casefold()
        return "timeout_activity=test-progress" in evidence

    @staticmethod
    def _has_active_test_progress(output: str) -> bool:
        """Recognize strong runner progress signals, not generic percentage output."""

        normalized = output.casefold()
        return bool(
            # pytest's progress column, including quiet-mode dot output.
            re.search(r"\[\s*(?:100|[1-9]?\d)%\]", normalized)
            # unittest's verbose per-test result lines.
            or re.search(
                r"(?m)^\s*test[\w. ()/-]*\s+\.\.\.\s+"
                r"(?:ok|skipped|expected failure)\s*$",
                normalized,
            )
            # TAP-compatible runners emit monotonically numbered success lines.
            or re.search(r"(?m)^\s*ok\s+\d+\b", normalized)
        )

    @staticmethod
    def _is_project_test_assertion_failure(state: AgentState) -> bool:
        failure = state.get("failure")
        build = state.get("build_result")
        if (
            failure is None
            or failure.failure_stage is not BuildStage.TEST
            or failure.category is not FailureCategory.TEST
            or build is None
            or build.status is not BuildStatus.SUCCEEDED
        ):
            return False
        evidence = "\n".join((*failure.evidence, failure.key_log)).casefold()
        environment_markers = (
            "modulenotfounderror",
            "no module named",
            "importerror",
            "cannot import name",
            "command not found",
            "distributionnotfound",
            "versionconflict",
            "connection refused",
            "connection timed out",
            "temporary failure in name resolution",
        )
        if any(marker in evidence for marker in environment_markers):
            return False
        return bool(
            "assertionerror" in evidence
            or re.search(r"(?m)^\s*e\s+assert\b", evidence)
            or re.search(r"\b\d+\s+failed(?:,|\s|$)", evidence)
        )

    @staticmethod
    def _is_python_package_install_timeout(failure: FailureInfo | None) -> bool:
        if failure is None:
            return False
        evidence = "\n".join((*failure.evidence, failure.key_log)).casefold()
        return (
            "timeout_profile=python-package-install" in evidence
            or "python -m pip install" in evidence
            or "pip install" in evidence
        )

    @staticmethod
    def _has_project_owned_build_script(state: AgentState) -> bool:
        profile = state.get("project_profile")
        candidates: set[str] = set()
        if profile is not None:
            candidates.update(profile.dockerfiles)
            candidates.update(
                item
                for item in profile.build_files
                if Path(item).name.lower() in {"dockerfile", "setup.sh"}
            )
        workspace = state.get("workspace")
        if isinstance(workspace, str) and workspace:
            root = Path(workspace)
            for value in ("Dockerfile", "setup.sh"):
                if (root / value).is_file():
                    candidates.add(value)
        return bool(candidates)

    def _verification_repair_policy_violation(
        self,
        failure: FailureInfo | None,
        previous_build: BuildResult | None,
        build_plan,
        diff: EnvironmentDiff,
    ) -> str:
        if failure is None or previous_build is None:
            return ""
        if previous_build.status is not BuildStatus.SUCCEEDED:
            return ""
        if failure.failure_stage not in {BuildStage.TEST, BuildStage.STARTUP}:
            return ""
        if (
            failure.failure_stage is BuildStage.TEST
            and self._is_verification_overlay_diff(diff)
        ):
            return ""
        if self._effective_base_image_change(diff, build_plan):
            return (
                "verification repair rejected: base image changes are not allowed "
                "after a successful build"
            )
        if self._effective_python_version_change(diff, build_plan):
            return (
                "verification repair rejected: Python version changes are not allowed "
                "after a successful build"
            )
        expanded = tuple(
            item
            for item in (*diff.dependencies, *diff.system_packages, *diff.python_dependencies)
            if item.kind in {ChangeKind.ADDED, ChangeKind.MODIFIED}
        )
        if expanded:
            names = ", ".join(dict.fromkeys(item.name for item in expanded))
            return (
                "verification repair rejected: dependency expansion belongs in "
                f"verification commands, not the build image ({names})"
            )
        return ""

    @staticmethod
    def _is_verification_overlay_diff(diff: EnvironmentDiff | None) -> bool:
        if diff is None:
            return False
        paths = {
            item.path
            for item in (*diff.files, *diff.build_scripts, *diff.business_source)
        }
        return bool(paths) and paths == {VERIFICATION_REQUIREMENTS_PATH} and not (
            diff.business_source
            or diff.source_changed
            or diff.base_image is not None
            or diff.python_version is not None
            or diff.system_packages
            or diff.environment_variables
            or diff.startup_arguments
        )

    @staticmethod
    def _effective_base_image_change(diff: EnvironmentDiff, build_plan) -> bool:
        if diff.base_image is None:
            return False
        planned = ""
        if build_plan is not None:
            candidate = build_plan.metadata.get("base_image")
            if isinstance(candidate, str):
                planned = candidate
        if diff.base_image.before is None and diff.base_image.after == planned:
            return False
        return True

    @staticmethod
    def _effective_python_version_change(diff: EnvironmentDiff, build_plan) -> bool:
        if diff.python_version is None:
            return False
        planned_base = ""
        if build_plan is not None:
            candidate = build_plan.metadata.get("base_image")
            if isinstance(candidate, str):
                planned_base = candidate
        after = diff.python_version.after or ""
        if diff.python_version.before is None and after and f":{after}" in planned_base:
            return False
        before = diff.python_version.before or ""
        if (
            before
            and after
            and f":{after}" in planned_base
            and AgentWorkflow._python_constraint_allows(before, after)
        ):
            return False
        return True

    @staticmethod
    def _python_constraint_allows(constraint: str, version: str) -> bool:
        """Return true only when a simple requires-python range proves compatibility."""

        def release(value: str) -> tuple[int, ...]:
            return tuple(int(part) for part in value.split("."))

        try:
            selected = release(version)
        except ValueError:
            return False
        clauses = tuple(part.strip() for part in constraint.split(",") if part.strip())
        if not clauses:
            return False
        comparisons = {
            ">=": lambda left, right: left >= right,
            "<=": lambda left, right: left <= right,
            ">": lambda left, right: left > right,
            "<": lambda left, right: left < right,
            "==": lambda left, right: left == right,
            "!=": lambda left, right: left != right,
        }
        for clause in clauses:
            match = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,2})", clause)
            if match is None:
                return False
            expected = release(match.group(2))
            width = max(len(selected), len(expected))
            left = selected + (0,) * (width - len(selected))
            right = expected + (0,) * (width - len(expected))
            if not comparisons[match.group(1)](left, right):
                return False
        return True

    @staticmethod
    def _is_build_timeout_failure(failure: FailureInfo | None) -> bool:
        if failure is None:
            return False
        return (
            failure.message.casefold().strip() == "build command timed out"
            or "timed out" in failure.message.casefold()
        ) and failure.failure_stage in {BuildStage.BUILD, BuildStage.DEPENDENCY_INSTALLATION}

    @staticmethod
    def _repair_timeout_regression_failure(
        previous: BuildResult,
        current: BuildResult,
    ) -> FailureInfo:
        command = current.command_results[-1].command if current.command_results else None
        identity = (
            f"{previous.attempt_id}\0{current.attempt_id}\0"
            f"{command.display if command else current.summary}"
        )
        fingerprint = f"regression:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        evidence = [
            f"previous_build_status={previous.status.value}",
            f"current_build_status={current.status.value}",
        ]
        if command is not None:
            evidence.append(f"timed_out_command={command.display}")
        return FailureInfo(
            category=FailureCategory.REGRESSION,
            failure_stage=current.failed_stage or BuildStage.BUILD,
            message="repair introduced a build-time cost regression",
            fingerprint=fingerprint,
            kind=BuildFailureKind.PROJECT_BUILD,
            failed_command=command,
            key_log=current.summary,
            possible_cause=(
                "the current environment change made a previously successful build time out"
            ),
            evidence=tuple(evidence),
            retryable=True,
            confidence=1.0,
        )

    @staticmethod
    def _append(values: tuple, *items, limit: int = 20) -> tuple:
        return (tuple(values) + tuple(items))[-limit:]

    @staticmethod
    def _thread_config(run_id: str, recursion_limit: int = 100) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": recursion_limit,
        }

    @staticmethod
    def _stopped(reason: str, **updates: Any) -> dict[str, Any]:
        return {
            "phase": AgentPhase.STOPPED,
            "stop_reason": reason,
            **updates,
        }

    @staticmethod
    def _entry_route(state: AgentState) -> Literal["analyze_failure", "execute", "end"]:
        if state.get("phase") is AgentPhase.STOPPED:
            return "end"
        if state.get("failure") is not None:
            return "analyze_failure"
        if state.get("project_profile") is not None:
            return "execute"
        return "end"

    @staticmethod
    def _continue_route(state: AgentState) -> Literal["continue", "end"]:
        return "end" if state.get("phase") is AgentPhase.STOPPED else "continue"

    @staticmethod
    def _preflight_route(
        state: AgentState,
    ) -> Literal["execute", "verify", "evaluate", "end"]:
        if state.get("phase") is AgentPhase.STOPPED:
            return "end"
        result = state.get("repair_preflight")
        if result is not None and not result.accepted:
            return "evaluate"
        if AgentWorkflow._is_verification_overlay_diff(state.get("environment_diff")):
            return "verify"
        return "execute"

    @staticmethod
    def _evaluation_route(state: AgentState) -> Literal["continue", "end"]:
        return "continue" if state.get("phase") is AgentPhase.CLASSIFYING else "end"
