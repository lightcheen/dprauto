"""Installability verifies build execution, dependency setup, target and image."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from dprauto.domain.enums import BuildStatus, VerificationLevel, VerificationStatus
from dprauto.domain.models import VerificationCheck, VerificationResult
from dprauto.ports.runtime import ContainerRuntime
from dprauto.ports.verification import VerificationContext
from dprauto.time_budget import time_budget_exhausted
from dprauto.verification.common import aggregate_status, verification_id


_INSTALL_PATTERN = re.compile(
    r"\b(?:pip(?:3)?\s+install|python\d*\s+-m\s+pip\s+install|"
    r"poetry\s+install|uv\s+sync|pdm\s+(?:install|sync)|pipenv\s+(?:install|sync)|"
    r"conda\s+env\s+create)\b",
    re.IGNORECASE,
)


class InstallabilityVerifier:
    level = VerificationLevel.INSTALLABILITY

    def __init__(self, runtime: ContainerRuntime) -> None:
        self.runtime = runtime

    def supports(self, profile, level: VerificationLevel) -> bool:
        return level is self.level

    def verify(self, context: VerificationContext) -> VerificationResult:
        result = context.build_result
        build_ok = result.status is BuildStatus.SUCCEEDED and all(
            item.succeeded for item in result.command_results
        )
        build_check = VerificationCheck(
            "build",
            VerificationStatus.PASSED if build_ok else VerificationStatus.FAILED,
            "all required build commands completed" if build_ok else "build did not complete successfully",
            command_result=result.command_results[-1] if result.command_results else None,
            evidence=result.logs,
        )

        dependency_required = bool(context.profile.metadata.get("dependency_names")) or any(
            PurePosixPath(path).name.lower() in {"pyproject.toml", "setup.py", "setup.cfg"}
            for path in context.profile.dependency_files
        )
        setup_text = self._setup_text(context)
        dependency_ok = not dependency_required or bool(_INSTALL_PATTERN.search(setup_text))
        dependency_check = VerificationCheck(
            "dependency-installation",
            VerificationStatus.PASSED if dependency_ok else VerificationStatus.FAILED,
            (
                "dependency installation is present in the build definition"
                if dependency_required and dependency_ok
                else "project declares no dependency manifest"
                if not dependency_required
                else "dependency manifests exist but no installation step was found"
            ),
            metadata={"required": dependency_required},
        )

        image_reference = result.image_reference or ""
        target_check = VerificationCheck(
            "target-artifact",
            VerificationStatus.PASSED if image_reference else VerificationStatus.FAILED,
            f"target image recorded: {image_reference}" if image_reference else "no target image recorded",
        )
        if image_reference and time_budget_exhausted(context.deadline_at):
            image_check = VerificationCheck(
                "image",
                VerificationStatus.ERROR,
                "workflow time budget exceeded before image inspection",
                metadata={"image_reference": image_reference},
            )
            checks = (build_check, dependency_check, target_check, image_check)
            return VerificationResult(
                verification_id(self.level),
                self.level,
                aggregate_status(checks),
                command_result=build_check.command_result,
                evidence=result.logs,
                summary="installability failed",
                metadata={"image_reference": image_reference},
                checks=checks,
            )
        inspection = self.runtime.inspect_image(image_reference) if image_reference else None
        image_ok = bool(inspection and inspection.exists and inspection.image_id)
        image_check = VerificationCheck(
            "image",
            VerificationStatus.PASSED if image_ok else VerificationStatus.FAILED,
            f"image exists: {inspection.image_id}" if image_ok else "target image cannot be inspected",
            metadata={
                "image_reference": image_reference,
                "working_directory": inspection.working_directory if inspection else "",
            },
        )
        checks = (build_check, dependency_check, target_check, image_check)
        status = aggregate_status(checks)
        return VerificationResult(
            verification_id(self.level),
            self.level,
            status,
            command_result=build_check.command_result,
            evidence=result.logs,
            summary="installability passed" if status is VerificationStatus.PASSED else "installability failed",
            metadata={"image_reference": image_reference},
            checks=checks,
        )

    @staticmethod
    def _setup_text(context: VerificationContext) -> str:
        plan = context.build_plan
        chunks: list[str] = []
        if plan is not None:
            chunks.extend(file.content for file in plan.generated_files)
            for source in plan.source_files:
                target = (context.workspace / source).resolve()
                if context.workspace.resolve() in target.parents and target.is_file():
                    chunks.append(target.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)
