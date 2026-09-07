"""Installability verifies build execution, dependency setup, target and image."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from dprauto.config import VerificationConfig
from dprauto.domain.enums import (
    BuildStatus,
    CommandPurpose,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import CommandSpec, VerificationCheck, VerificationResult
from dprauto.ports.runtime import ContainerRuntime
from dprauto.ports.verification import VerificationContext
from dprauto.time_budget import clamped_timeout_seconds, time_budget_exhausted
from dprauto.verification.common import aggregate_status, verification_id

_INSTALL_PATTERN = re.compile(
    r"\b(?:pip(?:3)?\s+install|python\d*\s+-m\s+pip\s+install|"
    r"poetry\s+install|uv\s+sync|pdm\s+(?:install|sync)|pipenv\s+(?:install|sync)|"
    r"conda\s+env\s+create|(?:\./)?mvnw?\s+.*\b(?:package|install)\b|"
    r"(?:\./)?gradlew?\s+.*\b(?:assemble|build|classes)\b|"
    r"apt-get\s+.*\binstall\b)\b",
    re.IGNORECASE,
)

_DEPENDENCY_BUILD_SYSTEMS = {
    "autotools",
    "cmake",
    "gradle",
    "make",
    "maven",
    "meson",
}
_INSTALLATION_HEALTH_MARKER = "DPRAUTO_INSTALL_HEALTH_OK"


class InstallabilityVerifier:
    level = VerificationLevel.INSTALLABILITY

    def __init__(
        self,
        runtime: ContainerRuntime,
        config: VerificationConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or VerificationConfig()

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

        contract_commands = self._dependency_contract_commands(context)
        dependency_required = (
            bool(contract_commands)
            or bool(context.profile.metadata.get("dependency_names"))
            or bool(
                {manager.casefold() for manager in context.profile.package_managers}
                & _DEPENDENCY_BUILD_SYSTEMS
            )
            or any(
                PurePosixPath(path).name.lower()
                in {"pyproject.toml", "setup.py", "setup.cfg"}
                for path in context.profile.dependency_files
            )
        )
        setup_text = self._setup_text(context)
        if contract_commands:
            dependency_ok = all(command in setup_text for command in contract_commands)
            contract_source = "build-plan"
        else:
            dependency_ok = not dependency_required or bool(_INSTALL_PATTERN.search(setup_text))
            contract_source = "definition-scan" if dependency_required else "none"
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
            metadata={
                "required": dependency_required,
                "contract_source": contract_source,
                "contract_commands": contract_commands,
            },
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
        health_check = self._installation_health_check(
            context,
            image_reference,
            prerequisites_ok=build_ok and dependency_ok and image_ok,
        )
        checks = (
            build_check,
            dependency_check,
            target_check,
            image_check,
            health_check,
        )
        status = aggregate_status(checks)
        return VerificationResult(
            verification_id(self.level),
            self.level,
            status,
            command_result=build_check.command_result,
            evidence=result.logs,
            summary="installability passed" if status is VerificationStatus.PASSED else "installability failed",
            metadata={
                "image_reference": image_reference,
                "installation_health_probe": health_check.metadata.get(
                    "probe_type", ""
                ),
                "installation_health_evidence_mode": health_check.metadata.get(
                    "evidence_mode", ""
                ),
                "checked_dynamic_artifacts": health_check.metadata.get(
                    "checked_dynamic_artifacts", 0
                ),
            },
            checks=checks,
        )

    def _installation_health_check(
        self,
        context: VerificationContext,
        image_reference: str,
        *,
        prerequisites_ok: bool,
    ) -> VerificationCheck:
        if not prerequisites_ok:
            return VerificationCheck(
                "installation-health",
                VerificationStatus.SKIPPED,
                "installation health probe skipped because a prerequisite check failed",
                metadata={"probe_type": "", "skip_reason": "prerequisite-failed"},
            )
        selected = self._installation_health_probe(context)
        if selected is None:
            return VerificationCheck(
                "installation-health",
                VerificationStatus.SKIPPED,
                "no bounded installation health probe supports this language family",
                metadata={"probe_type": "", "skip_reason": "unsupported-language"},
            )
        probe, probe_type = selected
        timeout_seconds = clamped_timeout_seconds(
            self.config.dependency_command_timeout_seconds,
            context.deadline_at,
        )
        if timeout_seconds <= 0:
            return VerificationCheck(
                "installation-health",
                VerificationStatus.ERROR,
                "workflow time budget exceeded before installation health probe",
                metadata={"probe_type": probe_type},
            )
        command = CommandSpec(
            (probe,),
            purpose=CommandPurpose.TEST,
            timeout_seconds=timeout_seconds,
            shell=True,
        )
        execution = self.runtime.run_image(
            image_reference,
            command,
            timeout_seconds=timeout_seconds,
        )
        output = execution.output_excerpt
        probe_succeeded = bool(
            execution.command_result.succeeded
            and _INSTALLATION_HEALTH_MARKER in output
        )
        mode_match = re.search(
            r"DPRAUTO_INSTALL_HEALTH_MODE=([A-Za-z0-9_.+-]+)", output
        )
        evidence_mode = mode_match.group(1) if mode_match else ""
        dynamic_count_match = re.search(r"DPRAUTO_NATIVE_DYNAMIC_COUNT=(\d+)", output)
        checked_dynamic_artifacts = (
            int(dynamic_count_match.group(1)) if dynamic_count_match else 0
        )
        limited_evidence = evidence_mode in {
            "metadata",
            "no-dynamic-artifacts",
            "pip-check-project-clean-tool-conflicts",
            "unavailable",
        }
        status = (
            VerificationStatus.FAILED
            if not probe_succeeded
            else VerificationStatus.SKIPPED
            if limited_evidence
            else VerificationStatus.PASSED
        )
        if status is VerificationStatus.PASSED:
            summary = f"installation health probe passed: {probe_type}"
        elif status is VerificationStatus.SKIPPED:
            summary = (
                f"installation health probe had limited evidence: {probe_type} "
                f"({evidence_mode})"
            )
        else:
            summary = f"installation health probe failed: {probe_type}"
        return VerificationCheck(
            "installation-health",
            status,
            summary,
            command_result=execution.command_result,
            evidence=(
                (execution.command_result.stdout,)
                if execution.command_result.stdout
                else ()
            ),
            metadata={
                "probe_type": probe_type,
                "evidence_mode": evidence_mode,
                "checked_dynamic_artifacts": checked_dynamic_artifacts,
                "output_excerpt": output,
            },
        )

    @staticmethod
    def _installation_health_probe(
        context: VerificationContext,
    ) -> tuple[str, str] | None:
        languages = {value.casefold() for value in context.profile.languages}
        managers = {value.casefold() for value in context.profile.package_managers}
        if "python" in languages or managers & {"pip", "poetry", "pipenv", "uv", "pdm"}:
            package_check = InstallabilityVerifier._python_package_check(
                managers,
                str(context.profile.metadata.get("project_name", "")),
            )
            return (
                f"{package_check} && echo DPRAUTO_INSTALL_HEALTH_OK",
                "python-pip-check",
            )
        if languages & {"java", "kotlin", "groovy"}:
            return (
                "java -version >/dev/null 2>&1 && "
                "echo DPRAUTO_INSTALL_HEALTH_MODE=runtime-load && "
                "echo DPRAUTO_INSTALL_HEALTH_OK",
                "jvm-runtime-load",
            )
        if languages & {"c", "c++", "cpp"}:
            system = str(
                context.profile.metadata.get("primary_build_system", "")
            ).casefold()
            if not system:
                system = next(
                    (
                        value
                        for value in ("cmake", "meson", "autotools", "make")
                        if value in managers
                    ),
                    "",
                )
            root = "build" if system in {"cmake", "meson"} else "."
            return (
                "if ! command -v ldd >/dev/null 2>&1; then "
                "echo DPRAUTO_INSTALL_HEALTH_MODE=unavailable; "
                "echo DPRAUTO_INSTALL_HEALTH_OK; exit 0; fi; "
                f"find {root} -type f "
                "\\( -perm -111 -o -name '*.so' -o -name '*.so.*' \\) "
                "| sort | head -n 256 | { checked=0; "
                "while IFS= read -r candidate; do "
                "if linked=$(LC_ALL=C ldd \"$candidate\" 2>&1); then "
                "checked=$((checked + 1)); "
                "if printf '%s\\n' \"$linked\" | grep -q '=> not found'; then "
                "printf '%s\\n' \"$candidate\" \"$linked\"; exit 1; fi; fi; done; "
                "echo DPRAUTO_NATIVE_DYNAMIC_COUNT=$checked; "
                "if [ \"$checked\" -gt 0 ]; then "
                "echo DPRAUTO_INSTALL_HEALTH_MODE=dynamic-link-check; "
                "else echo DPRAUTO_INSTALL_HEALTH_MODE=no-dynamic-artifacts; fi; } && "
                "echo DPRAUTO_INSTALL_HEALTH_OK",
                "native-dynamic-link-check",
            )
        return None

    @staticmethod
    def _python_package_check(managers: set[str], project_name: str) -> str:
        """Check project packages without treating retained installer tools as runtime deps."""

        normalized_project = re.sub(r"[-_.]+", "-", project_name.casefold())
        ignored_tools = tuple(
            tool
            for tool in ("poetry", "pdm", "pipenv", "uv")
            if tool in managers and tool != normalized_project
        )
        if ignored_tools:
            tool_pattern = "|".join(re.escape(tool) for tool in ignored_tools)
            pip_check = (
                'if pip_output=$("$python_bin" -m pip check 2>&1); then '
                'printf "%s\\n" "$pip_output"; '
                "echo DPRAUTO_INSTALL_HEALTH_MODE=pip-check; "
                "else printf \"%s\\n\" \"$pip_output\"; "
                "project_issues=$(printf \"%s\\n\" \"$pip_output\" | "
                f"grep -Eiv '^({tool_pattern})([[:space:]]|$)' || true); "
                'if [ -n "$project_issues" ]; then '
                'printf "%s\\n" "$project_issues"; exit 1; fi; '
                "echo DPRAUTO_INSTALL_HEALTH_MODE="
                "pip-check-project-clean-tool-conflicts; fi"
            )
        else:
            pip_check = (
                '"$python_bin" -m pip check && '
                "echo DPRAUTO_INSTALL_HEALTH_MODE=pip-check"
            )
        return (
            "python_bin=$(command -v python || command -v python3) || exit 1; "
            "if command -v uv >/dev/null 2>&1; then "
            "uv pip check && echo DPRAUTO_INSTALL_HEALTH_MODE=uv-pip-check; "
            'elif "$python_bin" -m pip --version >/dev/null 2>&1; then '
            f"{pip_check}; "
            'else "$python_bin" -c '
            '"import importlib.metadata as m; list(m.distributions())" '
            "&& echo DPRAUTO_INSTALL_HEALTH_MODE=metadata; fi"
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

    @staticmethod
    def _dependency_contract_commands(context: VerificationContext) -> tuple[str, ...]:
        plan = context.build_plan
        if plan is None:
            return ()
        values = plan.metadata.get("dependency_installation_commands", ())
        if not isinstance(values, (list, tuple)):
            return ()
        return tuple(
            value.strip()
            for value in values[:32]
            if isinstance(value, str) and value.strip()
        )
