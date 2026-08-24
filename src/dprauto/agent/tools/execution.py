"""Command, deterministic image-build and bounded build-log tools."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from dprauto.agent.models import ToolContext, ToolResult
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectProfile
from dprauto.errors import PolicyViolationError, ToolExecutionError
from dprauto.ports.execution import CommandExecutor
from dprauto.ports.storage import Storage
from dprauto.time_budget import clamped_timeout_seconds


class _ProjectBuilder(Protocol):
    def build(
        self,
        profile: ProjectProfile,
        workspace: Path,
        *,
        deadline_at: datetime | None = None,
    ) -> Any:
        ...


class RunCommandTool:
    name = "run_command"
    effect = "execute"
    description = (
        "Run one previously detected ProjectProfile/BuildPlan argv command; arbitrary and shell "
        "commands are rejected."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "cwd": {"type": "string", "minLength": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    }

    def __init__(self, executor: CommandExecutor, *, max_timeout_seconds: int = 1800) -> None:
        if max_timeout_seconds <= 0:
            raise ValueError("max_timeout_seconds must be positive")
        self.executor = executor
        self.max_timeout_seconds = max_timeout_seconds

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        argv = arguments.get("argv")
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise ToolExecutionError("run_command.argv must be a non-empty string list")
        if arguments.get("shell"):
            raise ToolExecutionError("run_command does not permit shell execution")
        allowed = set()
        if context.project_profile is not None:
            allowed.update(item.command.argv for item in context.project_profile.commands)
        if context.build_plan is not None:
            allowed.update(step.command.argv for step in context.build_plan.steps)
        if tuple(argv) not in allowed:
            raise PolicyViolationError(
                "run_command only accepts commands already present in ProjectProfile or BuildPlan"
            )
        timeout = arguments.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or timeout <= 0:
            raise ToolExecutionError("run_command.timeout_seconds must be a positive integer")
        timeout_seconds = clamped_timeout_seconds(
            min(timeout, self.max_timeout_seconds),
            context.deadline_at,
        )
        if timeout_seconds <= 0:
            raise ToolExecutionError("run_command skipped because workflow time budget is exhausted")
        command = CommandSpec(
            tuple(argv),
            purpose=CommandPurpose.OTHER,
            cwd=str(arguments["cwd"]) if arguments.get("cwd") else None,
            timeout_seconds=timeout_seconds,
            shell=False,
        )
        result = self.executor.execute(command, Path(context.workspace))
        artifacts = tuple(item for item in (result.stdout, result.stderr) if item is not None)
        return ToolResult(
            self.name,
            result.succeeded,
            f"command exited with {result.exit_code}",
            data={"command_result": result},
            artifacts=artifacts,
        )


class BuildImageTool:
    name = "build_image"
    effect = "execute"
    description = "Run the existing deterministic BuildStrategy and return BuildResult/FailureInfo."
    argument_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, builder: _ProjectBuilder) -> None:
        self.builder = builder

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.project_profile is None:
            raise ToolExecutionError("build_image requires ProjectProfile in tool context")
        execution = self.builder.build(
            context.project_profile,
            Path(context.workspace),
            deadline_at=context.deadline_at,
        )
        attempts = getattr(execution, "attempts", ())
        selection_reason = getattr(execution, "selection_reason", "")
        artifacts = list(getattr(execution, "artifacts", ()))
        artifacts.extend(execution.result.logs)
        artifacts.extend(execution.result.outputs)
        for attempt in attempts:
            artifacts.extend(attempt.result.logs)
            artifacts.extend(attempt.result.outputs)
        artifacts = list(dict.fromkeys(artifacts))
        summary = execution.result.summary
        if selection_reason:
            summary = f"{summary}; {selection_reason}"
        return ToolResult(
            self.name,
            execution.failure is None,
            summary,
            data={
                "strategy_attempts": attempts,
                "selection_reason": selection_reason,
            },
            artifacts=tuple(artifacts),
            build_plan=execution.plan,
            build_result=execution.result,
            failure=execution.failure,
        )


class GetBuildLogTool:
    name = "get_build_log"
    effect = "observe"
    description = "Load only the bounded tail of the current build log for diagnosis."
    argument_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, storage: Storage, *, max_bytes: int = 32 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.storage = storage
        self.max_bytes = max_bytes

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.build_result is None:
            raise ToolExecutionError("get_build_log requires BuildResult in tool context")
        remaining = self.max_bytes
        chunks: list[bytes] = []
        for artifact in reversed(context.build_result.logs):
            if remaining <= 0:
                break
            content = self.storage.load(artifact)
            chunks.append(content[-remaining:])
            remaining -= min(len(content), remaining)
        content = b"\n".join(reversed(chunks)).decode("utf-8", errors="replace")
        return ToolResult(
            self.name,
            True,
            f"loaded {len(content.encode('utf-8'))} bytes from bounded build-log tail",
            data={"content": content},
        )
