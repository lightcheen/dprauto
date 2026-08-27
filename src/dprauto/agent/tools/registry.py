"""Explicit registry for tool discovery and dispatch."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from dprauto.agent.models import ToolContext, ToolResult
from dprauto.agent.tools.schema import validate_tool_arguments
from dprauto.errors import ToolExecutionError
from dprauto.ports.tool import AgentTool


class ToolRegistry:
    def __init__(self, tools: Iterable[AgentTool]) -> None:
        indexed: dict[str, AgentTool] = {}
        for tool in tools:
            if tool.name in indexed:
                raise ValueError(f"duplicate agent tool: {tool.name}")
            indexed[tool.name] = tool
        if not indexed:
            raise ValueError("tool registry must not be empty")
        self._tools = indexed

    @property
    def descriptions(self) -> Mapping[str, str]:
        return {name: tool.description for name, tool in self._tools.items()}

    @property
    def specifications(self) -> Mapping[str, Mapping[str, Any]]:
        """Descriptions plus machine-readable argument contracts for LLM planning."""

        return {
            name: {
                "description": tool.description,
                "effect": getattr(tool, "effect", self._default_effect(name)),
                "argument_schema": getattr(
                    tool,
                    "argument_schema",
                    {"type": "object", "additionalProperties": True},
                ),
            }
            for name, tool in self._tools.items()
        }

    def has(self, name: str) -> bool:
        return name in self._tools

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolExecutionError(f"unknown agent tool: {name}") from exc
        schema = getattr(tool, "argument_schema", None)
        if isinstance(schema, Mapping):
            validate_tool_arguments(name, arguments, schema)
        return tool.invoke(arguments, context)

    @staticmethod
    def _default_effect(name: str) -> str:
        if name in {"modify_build_script", "patch_build_script"}:
            return "mutate"
        if name in {"build_image", "run_command"}:
            return "execute"
        return "observe"
