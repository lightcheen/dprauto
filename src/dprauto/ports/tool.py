"""Framework-neutral tool contract used by agent graph nodes."""

from typing import Any, Mapping, Protocol, runtime_checkable

from dprauto.agent.models import ToolContext, ToolResult


@runtime_checkable
class AgentTool(Protocol):
    name: str
    description: str
    argument_schema: Mapping[str, Any]
    effect: str

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        """Execute one bounded side effect or inspection operation."""
        ...
