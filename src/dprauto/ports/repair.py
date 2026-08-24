"""Repair-planning port implemented by an LLM-backed adapter in production."""

from typing import Any, Mapping, Protocol, runtime_checkable

from dprauto.agent.models import FixPlan, InvestigationDecision, ToolResult
from dprauto.agent.state import AgentState


@runtime_checkable
class RepairPlanner(Protocol):
    def analyze_failure(
        self,
        state: AgentState,
        observations: tuple[ToolResult, ...],
    ) -> str:
        """Return one concise, evidence-based repair hypothesis."""
        ...

    def plan_fix(
        self,
        state: AgentState,
        diagnosis: str,
        available_tools: Mapping[str, Any],
    ) -> FixPlan:
        """Select concrete tools and arguments that test the hypothesis."""
        ...


@runtime_checkable
class InvestigationPlanner(Protocol):
    def plan_investigation(
        self,
        state: AgentState,
        observations: tuple[ToolResult, ...],
        available_tools: Mapping[str, Any],
    ) -> InvestigationDecision:
        """Select bounded read-only evidence tools or declare the evidence sufficient."""
        ...
