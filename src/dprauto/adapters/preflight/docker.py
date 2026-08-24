"""Cheap shell/Dockerfile checks for proposed build-environment repairs."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from dprauto.domain.enums import ChangeKind, CommandPurpose, VerificationStatus
from dprauto.domain.models import (
    ArtifactRef,
    CommandResult,
    CommandSpec,
    EnvironmentDiff,
    RepairPreflightCheck,
    RepairPreflightResult,
)
from dprauto.ports.execution import CommandExecutor
from dprauto.ports.storage import Storage
from dprauto.time_budget import (
    clamped_timeout_seconds,
    deadline_from,
    normalize_utc,
    utc_now,
)


class DockerRepairPreflight:
    """Parse changed build scripts without executing Dockerfile RUN instructions."""

    _INCONCLUSIVE_DOCKER_MARKERS = (
        "unknown flag: --check",
        "unknown shorthand flag",
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "failed to resolve source metadata",
        "could not resolve host",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection reset",
        "no such host",
        "tls handshake timeout",
        "pull access denied",
        "manifest unknown",
    )

    def __init__(
        self,
        executor: CommandExecutor,
        storage: Storage,
        *,
        docker_binary: str = "docker",
        timeout_seconds: int = 30,
        output_excerpt_characters: int = 2_000,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("preflight timeout_seconds must be positive")
        if output_excerpt_characters < 200:
            raise ValueError("preflight output excerpt must be at least 200 characters")
        self.executor = executor
        self.storage = storage
        self.docker_binary = docker_binary
        self.timeout_seconds = timeout_seconds
        self.output_excerpt_characters = output_excerpt_characters

    def run(
        self,
        workspace: Path,
        environment_diff: EnvironmentDiff,
        *,
        deadline_at: datetime | None = None,
    ) -> RepairPreflightResult:
        workspace = workspace.expanduser().resolve()
        local_deadline = deadline_from(utc_now(), self.timeout_seconds)
        if deadline_at is not None and normalize_utc(deadline_at) < local_deadline:
            local_deadline = normalize_utc(deadline_at)
        paths = self._changed_build_scripts(environment_diff)
        checks: list[RepairPreflightCheck] = []
        artifacts: list[ArtifactRef] = []
        for path in paths:
            timeout = clamped_timeout_seconds(self.timeout_seconds, local_deadline)
            if timeout <= 0:
                checks.append(
                    RepairPreflightCheck(
                        f"budget:{path}",
                        VerificationStatus.SKIPPED,
                        "repair preflight budget was exhausted; defer to workflow budget policy",
                    )
                )
                break
            target = self._safe_target(workspace, path)
            if target is None or not target.is_file():
                checks.append(
                    RepairPreflightCheck(
                        f"file:{path}",
                        VerificationStatus.FAILED,
                        f"changed build script is missing or unsafe: {path}",
                    )
                )
                continue
            name = PurePosixPath(path).name.casefold()
            if name == "setup.sh":
                check = self._check_shell(workspace, path, target, timeout)
            elif name.startswith("dockerfile"):
                check = self._check_dockerfile(workspace, path, timeout)
            else:
                continue
            checks.append(check)
            if check.command_result is not None:
                artifacts.extend(
                    item
                    for item in (
                        check.command_result.stdout,
                        check.command_result.stderr,
                    )
                    if item is not None
                )
        if not checks:
            return RepairPreflightResult(
                VerificationStatus.SKIPPED,
                (),
                "no changed Dockerfile or setup.sh required local preflight",
            )
        failed = tuple(item for item in checks if item.status is VerificationStatus.FAILED)
        passed = tuple(item for item in checks if item.status is VerificationStatus.PASSED)
        if failed:
            status = VerificationStatus.FAILED
            summary = f"repair preflight rejected {len(failed)} explicit check failure(s)"
        elif passed:
            status = VerificationStatus.PASSED
            summary = f"repair preflight passed {len(passed)} check(s)"
        else:
            status = VerificationStatus.SKIPPED
            summary = "repair preflight was inconclusive; full build remains authoritative"
        return RepairPreflightResult(
            status,
            tuple(checks),
            summary,
            tuple(dict.fromkeys(artifacts)),
        )

    def _check_shell(
        self,
        workspace: Path,
        path: str,
        target: Path,
        timeout: int,
    ) -> RepairPreflightCheck:
        first_line = target.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[:1]
        interpreter = (
            "bash"
            if first_line and re.search(r"\b(?:env\s+)?bash\b", first_line[0])
            else "sh"
        )
        command = CommandSpec(
            (interpreter, "-n", path),
            purpose=CommandPurpose.OTHER,
            timeout_seconds=timeout,
        )
        result = self.executor.execute(command, workspace)
        output = self._output(result)
        if result.succeeded:
            status = VerificationStatus.PASSED
            summary = f"{path} passed {interpreter} syntax validation"
        elif result.timed_out or result.exit_code == 127:
            status = VerificationStatus.SKIPPED
            summary = f"{path} syntax validation was unavailable or timed out"
        else:
            status = VerificationStatus.FAILED
            detail = output or "exit code " + str(result.exit_code)
            summary = f"{path} has invalid {interpreter} syntax: {detail}"
        return RepairPreflightCheck(f"shell-syntax:{path}", status, summary, result)

    def _check_dockerfile(
        self,
        workspace: Path,
        path: str,
        timeout: int,
    ) -> RepairPreflightCheck:
        command = CommandSpec(
            (
                self.docker_binary,
                "build",
                "--check",
                "--network",
                "none",
                "--file",
                path,
                ".",
            ),
            purpose=CommandPurpose.OTHER,
            timeout_seconds=timeout,
        )
        result = self.executor.execute(command, workspace)
        output = self._output(result)
        normalized = output.casefold()
        if result.succeeded:
            status = VerificationStatus.PASSED
            summary = f"{path} passed Docker build-check without executing RUN instructions"
        elif result.timed_out or result.exit_code == 127 or any(
            marker in normalized for marker in self._INCONCLUSIVE_DOCKER_MARKERS
        ):
            status = VerificationStatus.SKIPPED
            summary = (
                f"{path} Docker build-check was inconclusive; "
                "full build remains authoritative"
            )
        else:
            status = VerificationStatus.FAILED
            detail = output or "exit code " + str(result.exit_code)
            summary = f"{path} failed Docker build-check: {detail}"
        return RepairPreflightCheck(f"dockerfile-check:{path}", status, summary, result)

    def _output(self, result: CommandResult) -> str:
        values: list[str] = []
        for artifact in (result.stdout, result.stderr):
            if artifact is not None:
                values.append(self.storage.load(artifact).decode("utf-8", errors="replace"))
        return "\n".join(values).strip()[-self.output_excerpt_characters :]

    @staticmethod
    def _changed_build_scripts(environment_diff: EnvironmentDiff) -> tuple[str, ...]:
        changes = (*environment_diff.build_scripts, *environment_diff.files)
        return tuple(
            dict.fromkeys(
                item.path
                for item in changes
                if item.kind is not ChangeKind.REMOVED
                and (
                    PurePosixPath(item.path).name.casefold() == "setup.sh"
                    or PurePosixPath(item.path).name.casefold().startswith("dockerfile")
                )
            )
        )

    @staticmethod
    def _safe_target(workspace: Path, path: str) -> Path | None:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        unresolved = workspace / relative.as_posix()
        if unresolved.is_symlink():
            return None
        target = unresolved.resolve()
        if target != workspace and workspace not in target.parents:
            return None
        return target
