"""Local subprocess executor with complete combined-log persistence."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from dprauto.domain.models import CommandResult, CommandSpec
from dprauto.errors import CommandExecutionError
from dprauto.ports.storage import Storage


class SubprocessCommandExecutor:
    def __init__(self, storage: Storage, *, shell_path: str = "/bin/sh") -> None:
        self.storage = storage
        self.shell_path = shell_path

    def execute(self, command: CommandSpec, workspace: Path) -> CommandResult:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise CommandExecutionError(f"command workspace is not a directory: {workspace}")
        cwd = workspace / command.cwd if command.cwd else workspace
        cwd = cwd.resolve()
        if cwd != workspace and workspace not in cwd.parents:
            raise CommandExecutionError(f"command working directory escapes workspace: {cwd}")
        if not cwd.is_dir():
            raise CommandExecutionError(f"command working directory is not a directory: {cwd}")

        argv = (
            [self.shell_path, "-c", command.argv[0]]
            if command.shell
            else list(command.argv)
        )
        environment = os.environ.copy()
        environment.update(command.environment)
        started = time.monotonic()
        timed_out = False
        exit_code: int | None
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=command.timeout_seconds,
                check=False,
            )
            output = completed.stdout or b""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"") + (
                f"\ncommand timed out after {command.timeout_seconds} seconds\n".encode()
            )
            exit_code = None
            timed_out = True
        except OSError as exc:
            output = f"failed to execute {command.display}: {exc}\n".encode()
            exit_code = 127

        duration = time.monotonic() - started
        log = self.storage.save(
            f"command-logs/{uuid.uuid4().hex}.log",
            output,
            media_type="text/plain; charset=utf-8",
        )
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=log,
            timed_out=timed_out,
            duration_seconds=duration,
        )
