"""Sandboxed command execution port."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from dprauto.domain.models import CommandResult, CommandSpec


@runtime_checkable
class CommandExecutor(Protocol):
    def execute(self, command: CommandSpec, workspace: Path) -> CommandResult:
        """Execute one command and persist its output through the configured storage."""
        ...
