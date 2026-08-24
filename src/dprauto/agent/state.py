"""Serializable shared state used directly by the LangGraph workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

from dprauto.agent.models import (
    ContextSummary,
    EvidencePack,
    FixPlan,
    RepairCandidate,
    ToolResult,
)

from dprauto.domain.enums import AgentPhase
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    EnvironmentDiff,
    EnvironmentBuildResult,
    FailureInfo,
    ProjectProfile,
    RegressionBaseline,
    RegressionResult,
    RepairPreflightResult,
    SourceReference,
    VerificationResult,
    VerificationReport,
)
from dprauto.errors import ModelValidationError


class AgentState(TypedDict, total=False):
    run_id: str
    workspace: str
    started_at: datetime
    deadline_at: datetime
    source_reference: SourceReference
    phase: AgentPhase
    attempt_number: int
    repeated_failure_count: int
    agent_participated: bool
    llm_call_count: int
    context_summary: ContextSummary
    current_method_fingerprint: str
    project_profile: ProjectProfile
    build_plan: BuildPlan
    build_result: BuildResult
    failure: FailureInfo | None
    failure_history: tuple[FailureInfo, ...]
    diagnosis: str
    evidence_pack: EvidencePack
    fix_plan: FixPlan
    tool_results: tuple[ToolResult, ...]
    verification_results: tuple[VerificationResult, ...]
    verification_report: VerificationReport
    regression_baseline: RegressionBaseline
    regression_result: RegressionResult
    manual_review_required: bool
    environment_diff: EnvironmentDiff
    environment_diffs: tuple[EnvironmentDiff, ...]
    repair_candidate: RepairCandidate
    repair_candidate_history: tuple[RepairCandidate, ...]
    repair_preflight: RepairPreflightResult
    artifacts: tuple[ArtifactRef, ...]
    summaries: tuple[str, ...]
    stop_reason: str
    final_result: EnvironmentBuildResult


def create_agent_state(run_id: str) -> AgentState:
    """Create the minimal state without importing LangGraph."""

    if not run_id or not run_id.strip():
        raise ModelValidationError("agent state.run_id must not be empty")
    return AgentState(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        phase=AgentPhase.CREATED,
        attempt_number=0,
        repeated_failure_count=0,
        agent_participated=False,
        llm_call_count=0,
        context_summary=ContextSummary(),
        failure_history=(),
        tool_results=(),
        verification_results=(),
        environment_diffs=(),
        manual_review_required=False,
        artifacts=(),
        summaries=(),
    )
