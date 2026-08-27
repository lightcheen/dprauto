"""Agent-specific immutable contracts for plans, tool calls and observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    EnvironmentDiff,
    FailureInfo,
    ProjectProfile,
    RegressionResult,
    VerificationReport,
    VerificationResult,
)
from dprauto.errors import ModelValidationError


def _required(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ModelValidationError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One structured tool choice emitted by the repair planner."""

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self) -> None:
        _required(self.tool, "tool call.tool")


@dataclass(frozen=True, slots=True)
class FixPlan:
    """A bounded repair hypothesis and the concrete tool calls used to test it."""

    hypothesis: str
    actions: tuple[ToolCall, ...]
    summary: str = ""

    def __post_init__(self) -> None:
        _required(self.hypothesis, "fix plan.hypothesis")
        if not self.actions:
            raise ModelValidationError("fix plan.actions must not be empty")


@dataclass(frozen=True, slots=True)
class InvestigationDecision:
    """One bounded, read-only evidence-gathering decision made before diagnosis."""

    actions: tuple[ToolCall, ...] = ()
    complete: bool = False
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.complete and self.actions:
            raise ModelValidationError(
                "completed investigation decision must not contain tool actions"
            )
        if not self.complete and not self.actions:
            raise ModelValidationError(
                "investigation decision must complete or contain tool actions"
            )


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """A compact observation retained as causal evidence for one repair round."""

    tool: str
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.tool, "evidence record.tool")
        _required(self.summary, "evidence record.summary")


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """Bounded read-only evidence gathered before an LLM proposes a mutation."""

    records: tuple[EvidenceRecord, ...] = ()
    rounds: int = 0
    action_count: int = 0
    completed: bool = False
    stop_reason: str = ""

    def __post_init__(self) -> None:
        if self.rounds < 0 or self.action_count < 0:
            raise ModelValidationError("evidence pack counters must not be negative")


@dataclass(frozen=True, slots=True)
class AttemptedMethod:
    """Compact failed-method memory retained in the active context."""

    fingerprint: str
    hypothesis: str
    outcome: str
    failure_fingerprint: str = ""

    def __post_init__(self) -> None:
        _required(self.fingerprint, "attempted method.fingerprint")
        _required(self.hypothesis, "attempted method.hypothesis")
        _required(self.outcome, "attempted method.outcome")


@dataclass(frozen=True, slots=True)
class RepairRoundFeedback:
    """Bounded execution feedback carried from one repair round to the next."""

    attempt_number: int
    method_fingerprint: str
    outcome: str
    progress: str
    failure_before: str = ""
    failure_after: str = ""
    failure_family_before: str = ""
    failure_family_after: str = ""
    environment_dimensions: tuple[str, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        if self.attempt_number <= 0:
            raise ModelValidationError("repair round feedback.attempt_number must be positive")
        _required(self.method_fingerprint, "repair round feedback.method_fingerprint")
        _required(self.outcome, "repair round feedback.outcome")
        _required(self.progress, "repair round feedback.progress")


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """Bounded repair memory that is safe to include in each LLM request."""

    resolved_issues: tuple[str, ...] = ()
    recent_modifications: tuple[str, ...] = ()
    failed_methods: tuple[AttemptedMethod, ...] = ()
    round_feedback: tuple[RepairRoundFeedback, ...] = ()
    narrative: str = "No repair attempts have completed yet."


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """Complete externally persisted record for one executed repair plan."""

    run_id: str
    attempt_number: int
    created_at: datetime
    method_fingerprint: str
    outcome: str
    fix_plan: FixPlan
    project_id: str
    build_result: BuildResult
    failure_before: FailureInfo | None = None
    failure_after: FailureInfo | None = None
    environment_diff: EnvironmentDiff | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    round_feedback: RepairRoundFeedback | None = None

    def __post_init__(self) -> None:
        _required(self.run_id, "repair record.run_id")
        _required(self.method_fingerprint, "repair record.method_fingerprint")
        _required(self.outcome, "repair record.outcome")
        _required(self.project_id, "repair record.project_id")
        if self.attempt_number <= 0:
            raise ModelValidationError("repair record.attempt_number must be positive")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Run-scoped context supplied to tools without coupling them to LangGraph."""

    run_id: str
    attempt_number: int
    workspace: str
    project_profile: ProjectProfile | None = None
    build_plan: BuildPlan | None = None
    build_result: BuildResult | None = None
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Normalized tool observation returned to graph nodes and the LLM planner."""

    tool: str
    succeeded: bool
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    environment_diff: EnvironmentDiff | None = None
    build_plan: BuildPlan | None = None
    build_result: BuildResult | None = None
    failure: FailureInfo | None = None

    def __post_init__(self) -> None:
        _required(self.tool, "tool result.tool")
        _required(self.summary, "tool result.summary")


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    """An isolated, transactional workspace for one repair attempt.

    The ``accepted_*`` fields are the rollback point for the whole candidate
    chain.  They deliberately travel with the candidate so a failed first
    repair can be used as the input to a second repair without making either
    one visible in the caller-owned workspace.
    """

    candidate_id: str
    attempt_number: int
    workspace: str
    accepted_workspace: str
    status: str = "pending"
    parent_candidate_id: str | None = None
    materialized_generated_files: tuple[str, ...] = ()
    environment_diff: EnvironmentDiff | None = None
    cumulative_environment_diff: EnvironmentDiff | None = None
    accepted_project_profile: ProjectProfile | None = None
    accepted_build_plan: BuildPlan | None = None
    accepted_build_result: BuildResult | None = None
    accepted_failure: FailureInfo | None = None
    accepted_verification_report: VerificationReport | None = None
    accepted_verification_results: tuple[VerificationResult, ...] = ()
    accepted_regression_result: RegressionResult | None = None
    disposition_reason: str = ""

    def __post_init__(self) -> None:
        _required(self.candidate_id, "repair candidate.candidate_id")
        _required(self.workspace, "repair candidate.workspace")
        _required(self.accepted_workspace, "repair candidate.accepted_workspace")
        if self.attempt_number <= 0:
            raise ModelValidationError("repair candidate.attempt_number must be positive")
        if self.status not in {"pending", "accepted", "rejected", "superseded"}:
            raise ModelValidationError(
                f"repair candidate.status is invalid: {self.status!r}"
            )
