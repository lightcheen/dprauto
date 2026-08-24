"""Shared deterministic build planning and recording helpers."""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dprauto.domain.enums import BuildStatus
from dprauto.domain.models import (
    ArtifactRef,
    BuildPlan,
    BuildResult,
    CommandResult,
    ProjectProfile,
)
from dprauto.errors import BuildExecutionError
from dprauto.ports.execution import CommandExecutor
from dprauto.ports.storage import Storage
from dprauto.serialization import to_json_bytes
from dprauto.time_budget import clamped_timeout_seconds, time_budget_exhausted


GENERATED_FILE_PREFIX = "__DPRAUTO_GENERATED__/"


def stable_image_reference(profile: ProjectProfile, repository: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", profile.project_id.lower()).strip("-._")
    slug = slug[:80] or "project"
    identity = f"{profile.project_id}\0{profile.source.locator}\0{profile.source.revision or ''}"
    tag = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{repository.rstrip('/')}/{slug}:{tag}"


def stable_plan_id(strategy: str, profile: ProjectProfile, payload: object) -> str:
    digest = hashlib.sha256(
        to_json_bytes(
            {
                "strategy": strategy,
                "project_id": profile.project_id,
                "source": profile.source,
                "payload": payload,
            }
        )
    ).hexdigest()[:16]
    return f"{strategy}-{digest}"


class RecordedBuildRunner:
    """Execute a plan and persist every input, command and complete log."""

    def __init__(self, executor: CommandExecutor, storage: Storage) -> None:
        self.executor = executor
        self.storage = storage

    def run(
        self,
        plan: BuildPlan,
        workspace: Path,
        *,
        deadline_at: datetime | None = None,
    ) -> BuildResult:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise BuildExecutionError(f"build workspace is not a directory: {workspace}")

        attempt_id = uuid.uuid4().hex
        prefix = f"attempts/{attempt_id}"
        started = datetime.now(timezone.utc)
        outputs: list[ArtifactRef] = [
            self.storage.save(
                f"plans/{plan.plan_id}/plan.json",
                to_json_bytes(plan),
                media_type="application/json",
            )
        ]
        command_results: list[CommandResult] = []
        logs: list[ArtifactRef] = []
        failed_stage = None
        budget_exceeded = False

        for source_file in plan.source_files:
            target = (workspace / source_file).resolve()
            if workspace not in target.parents or not target.is_file():
                raise BuildExecutionError(f"recorded build input is not a file: {source_file}")
            outputs.append(
                self.storage.save(
                    f"{prefix}/inputs/{source_file}",
                    target.read_bytes(),
                    media_type=(
                        "text/x-dockerfile"
                        if target.name.lower().startswith("dockerfile")
                        else "application/octet-stream"
                    ),
                )
            )

        with tempfile.TemporaryDirectory(prefix="dprauto-build-") as temporary_name:
            generated_root = Path(temporary_name).resolve()
            replacements: dict[str, str] = {}
            for generated in plan.generated_files:
                artifact = self.storage.save(
                    f"{prefix}/generated/{generated.path}",
                    generated.content.encode(),
                    media_type=generated.media_type,
                )
                outputs.append(artifact)
                target = (generated_root / generated.path).resolve()
                if generated_root not in target.parents:
                    raise BuildExecutionError(f"generated path escapes temporary root: {generated.path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(generated.content, encoding="utf-8")
                if generated.executable:
                    target.chmod(0o700)
                replacements[f"{GENERATED_FILE_PREFIX}{generated.path}"] = str(target)

            for index, step in enumerate(plan.steps, start=1):
                if time_budget_exhausted(deadline_at):
                    failed_stage = step.stage
                    budget_exceeded = True
                    break
                timeout_seconds = clamped_timeout_seconds(
                    step.command.timeout_seconds,
                    deadline_at,
                )
                if timeout_seconds <= 0:
                    failed_stage = step.stage
                    budget_exceeded = True
                    break
                actual = replace(
                    step.command,
                    argv=tuple(self._replace_argument(arg, replacements) for arg in step.command.argv),
                    timeout_seconds=timeout_seconds,
                )
                outputs.append(
                    self.storage.save(
                        f"{prefix}/commands/{index:02d}-{step.name}.txt",
                        (shlex.join(actual.argv) + "\n").encode(),
                        media_type="text/plain; charset=utf-8",
                    )
                )
                command_result = self.executor.execute(actual, workspace)
                command_results.append(command_result)
                if command_result.stdout:
                    logs.append(command_result.stdout)
                if command_result.stderr:
                    logs.append(command_result.stderr)
                if not command_result.succeeded and step.required:
                    failed_stage = step.stage
                    break

        finished = datetime.now(timezone.utc)
        final_command = command_results[-1] if command_results else None
        if budget_exceeded:
            status = BuildStatus.TIMED_OUT
        elif final_command and final_command.timed_out:
            status = BuildStatus.TIMED_OUT
        elif final_command and not final_command.succeeded:
            status = BuildStatus.FAILED
        elif command_results:
            status = BuildStatus.SUCCEEDED
        else:
            status = BuildStatus.CANCELLED
        image = str(plan.metadata.get("image_reference", "")) or None
        result_key = f"{prefix}/result.json"
        result = BuildResult(
            attempt_id=attempt_id,
            plan_id=plan.plan_id,
            status=status,
            started_at=started,
            finished_at=finished,
            exit_code=final_command.exit_code if final_command else None,
            failed_stage=failed_stage,
            image_reference=image if status is BuildStatus.SUCCEEDED else None,
            logs=tuple(logs),
            outputs=tuple(outputs),
            command_results=tuple(command_results),
            summary=(
                f"{plan.strategy} build succeeded"
                if status is BuildStatus.SUCCEEDED
                else f"{plan.strategy} build time budget exceeded"
                if budget_exceeded
                else f"{plan.strategy} build {status.value}"
            ),
            metadata={
                "result_artifact": result_key,
                "target_image": image or "",
                "strategy": plan.strategy,
                "time_budget_exceeded": budget_exceeded,
            },
        )
        self.storage.save(result_key, to_json_bytes(result), media_type="application/json")
        return result

    @staticmethod
    def _replace_argument(argument: str, replacements: dict[str, str]) -> str:
        result = argument
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)
        if GENERATED_FILE_PREFIX in result:
            raise BuildExecutionError(f"unresolved generated file in command argument: {argument}")
        return result
