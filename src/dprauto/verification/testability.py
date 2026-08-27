"""Execute a project-owned test command; never promote smoke probes to tests."""

import re
import shlex
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Mapping

from dprauto.config import VerificationConfig
from dprauto.domain.enums import VerificationLevel, VerificationStatus
from dprauto.domain.models import (
    CommandSpec,
    ProjectCommand,
    VerificationCheck,
    VerificationResult,
)
from dprauto.ports.runtime import ContainerRuntime
from dprauto.ports.verification import VerificationContext
from dprauto.time_budget import clamped_timeout_seconds
from dprauto.verification.commands import TestCommandSelector
from dprauto.verification.common import verification_id
from dprauto.verification.dependencies import TestDependencyPlan, TestDependencyPlanner
from dprauto.verification.overlay import load_verification_requirements
from dprauto.verification.prerequisites import TestEnvironmentPlanner


class TestabilityVerifier:
    level = VerificationLevel.TESTABILITY
    __test__ = False

    def __init__(
        self,
        runtime: ContainerRuntime,
        selector: TestCommandSelector | None = None,
        config: VerificationConfig | None = None,
        environment_planner: TestEnvironmentPlanner | None = None,
        dependency_planner: TestDependencyPlanner | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or VerificationConfig()
        self.selector = selector or TestCommandSelector(
            max_test_files=self.config.max_test_files_per_slice,
            max_parallel_workers=self.config.max_parallel_test_workers,
        )
        self.environment_planner = environment_planner or TestEnvironmentPlanner(self.config)
        self.dependency_planner = dependency_planner or TestDependencyPlanner(
            max_analysis_files=self.config.max_dependency_analysis_files,
            max_test_files=self.config.max_test_files_per_slice,
        )

    def supports(self, profile, level: VerificationLevel) -> bool:
        return level is self.level

    def verify(self, context: VerificationContext) -> VerificationResult:
        selection = self.selector.select_with_details(
            context.profile,
            python_version=self._runtime_python_version(context),
        )
        if selection is None:
            required_environment = self.selector._required_environment(context.profile)
            test_files = self.selector._metadata_paths(context.profile, "test_files")
            safe_files = self.selector._metadata_paths(context.profile, "safe_test_files")
            if required_environment:
                skip_reason = "required-secret-environment"
                summary = (
                    "project tests require unavailable secret environment variable(s): "
                    + ", ".join(required_environment)
                )
            elif test_files and not safe_files:
                skip_reason = "external-tests-only"
                summary = (
                    "only external-service or expensive project tests were discovered; "
                    "no local Testability command was executed"
                )
            else:
                skip_reason = "no-project-test-command"
                summary = "no project-owned test command was discovered; smoke probes are excluded"
            check = VerificationCheck(
                "project-tests",
                VerificationStatus.SKIPPED,
                summary,
            )
            return VerificationResult(
                verification_id(self.level),
                self.level,
                VerificationStatus.SKIPPED,
                summary=check.summary,
                metadata={
                    "command_kind": "none",
                    "skip_reason": skip_reason,
                    "required_environment_variables": required_environment,
                },
                checks=(check,),
            )
        dependency_plan = TestDependencyPlan(targets=selection.targets)
        if self.config.minimal_test_dependency_closure_enabled:
            dependency_plan = self.dependency_planner.plan(
                context.profile,
                context.workspace,
                selection.targets,
                command=selection.command.command.display,
            )
            if dependency_plan.applied and dependency_plan.targets != selection.targets:
                selection = self._with_dependency_closed_targets(
                    selection,
                    dependency_plan,
                )
        selected = selection.command
        image = context.build_result.image_reference
        if not image:
            check = VerificationCheck(
                "project-tests", VerificationStatus.ERROR, "cannot run tests without an image"
            )
            return VerificationResult(
                verification_id(self.level),
                self.level,
                VerificationStatus.ERROR,
                summary=check.summary,
                checks=(check,),
            )
        if "bounded-file-slice" in selection.kind:
            test_command, parallel_workers = selected.command, 0
        else:
            test_command, parallel_workers = self._with_bounded_parallelism(
                context.profile,
                selected.command,
            )
        try:
            overlay_requirements = load_verification_requirements(context.workspace)
        except (OSError, UnicodeError, ValueError) as exc:
            check = VerificationCheck(
                "project-tests",
                VerificationStatus.ERROR,
                f"invalid Testability dependency overlay: {exc}",
            )
            return VerificationResult(
                verification_id(self.level),
                self.level,
                check.status,
                summary=check.summary,
                metadata={"command_kind": "project-test", "overlay_invalid": True},
                checks=(check,),
            )
        command = self._with_test_dependency_install(
            context.profile,
            test_command,
            overlay_requirements=overlay_requirements,
            dependency_commands=(
                dependency_plan.install_commands if dependency_plan.applied else None
            ),
        )
        dependency_setup = command.display != test_command.display
        environment = self.environment_planner.plan(context.profile, selected)
        if dependency_plan.required_executables:
            environment = replace(
                environment,
                required_executables=tuple(
                    dict.fromkeys(
                        (
                            *environment.required_executables,
                            *dependency_plan.required_executables,
                        )
                    )
                ),
                evidence=tuple(
                    dict.fromkeys(
                        (
                            *environment.evidence,
                            *(
                                f"test-dependency-executable:{item}"
                                for item in dependency_plan.required_executables
                            ),
                        )
                    )
                ),
            )
        if environment.services and not self.config.service_orchestration_enabled:
            service_kinds = tuple(service.kind for service in environment.services)
            summary = "project tests require disabled verification service(s): " + ", ".join(
                service_kinds
            )
            check = VerificationCheck("project-tests", VerificationStatus.SKIPPED, summary)
            return VerificationResult(
                verification_id(self.level),
                self.level,
                VerificationStatus.SKIPPED,
                summary=summary,
                metadata={
                    "command_kind": "project-test",
                    "skip_reason": "service-orchestration-disabled",
                    "required_services": service_kinds,
                },
                checks=(check,),
            )
        timeout_policy = "dependency-and-test" if dependency_setup else "test-only"
        requested_timeout_seconds = (
            self.config.dependency_command_timeout_seconds
            if dependency_setup
            else self.config.command_timeout_seconds
        )
        timeout_seconds = clamped_timeout_seconds(
            requested_timeout_seconds,
            context.deadline_at,
        )
        if timeout_seconds <= 0:
            check = VerificationCheck(
                "project-tests",
                VerificationStatus.ERROR,
                "workflow time budget exceeded before project tests",
            )
            return VerificationResult(
                verification_id(self.level),
                self.level,
                VerificationStatus.ERROR,
                summary=check.summary,
                checks=(check,),
            )
        environment_runner = getattr(self.runtime, "run_environment", None)
        if (
            environment.services
            or environment.setup_commands
            or environment.command_environment
            or environment.required_executables
        ):
            if not callable(environment_runner):
                summary = "container runtime cannot orchestrate required test prerequisites"
                check = VerificationCheck("project-tests", VerificationStatus.ERROR, summary)
                return VerificationResult(
                    verification_id(self.level),
                    self.level,
                    VerificationStatus.ERROR,
                    summary=summary,
                    metadata={
                        "command_kind": "project-test",
                        "required_services": tuple(
                            service.kind for service in environment.services
                        ),
                    },
                    checks=(check,),
                )
            execution = environment_runner(
                image,
                command,
                environment,
                timeout_seconds=timeout_seconds,
            )
        else:
            execution = self.runtime.run_image(
                image,
                command,
                timeout_seconds=timeout_seconds,
            )
        passed = execution.command_result.succeeded
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            f"project test command from {selected.source} {'passed' if passed else 'failed'}",
            command_result=execution.command_result,
            evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            metadata={"source": selected.source, "output_excerpt": execution.output_excerpt},
        )
        return VerificationResult(
            verification_id(self.level),
            self.level,
            check.status,
            command_result=execution.command_result,
            evidence=check.evidence,
            summary=check.summary,
            metadata={
                "command_kind": "project-test",
                "command_source": selected.source,
                "command": command.display,
                "original_command": selection.original_command.display,
                "selection_kind": selection.kind,
                "selection_reason": selection.reason,
                "selection_targets": selection.targets,
                "selection_target_count": len(selection.targets),
                "parallel_workers": parallel_workers,
                "parallel_source": ("tox.ini+ci" if parallel_workers else ""),
                "timeout_policy": timeout_policy,
                "requested_timeout_seconds": requested_timeout_seconds,
                "effective_timeout_seconds": timeout_seconds,
                "verification_overlay_packages": overlay_requirements,
                "required_services": tuple(service.kind for service in environment.services),
                "service_images": tuple(
                    service.image_reference for service in environment.services
                ),
                "test_environment_variable_names": tuple(environment.command_environment),
                "test_setup_commands": tuple(item.display for item in environment.setup_commands),
                "required_executables": environment.required_executables,
                "prerequisite_evidence": environment.evidence,
                "runtime_environment": dict(execution.metadata),
                "dependency_plan_mode": dependency_plan.mode,
                "dependency_plan_source": dependency_plan.source,
                "dependency_plan_applied": dependency_plan.applied,
                "dependency_plan_reason": dependency_plan.reason,
                "dependency_analyzed_files": dependency_plan.analyzed_files,
                "dependency_import_roots": dependency_plan.import_roots,
                "dependency_selected_requirements": dependency_plan.selected_requirements,
                "dependency_unresolved_imports": dependency_plan.unresolved_imports,
                "dependency_excluded_targets": dependency_plan.excluded_targets,
                "dependency_required_executables": dependency_plan.required_executables,
            },
            checks=(check,),
        )

    @staticmethod
    def _runtime_python_version(context: VerificationContext) -> str:
        plan = context.build_plan
        if plan is not None:
            for key in ("runtime_base_image", "base_image"):
                image = str(plan.metadata.get(key, ""))
                match = re.search(r"(?:python[:_-])?(3\.\d{1,2})(?:[.-]|$)", image, re.I)
                if match:
                    return match.group(1)
        constraint = context.profile.runtime_constraints.get("python", "")
        exact = re.search(r"(?:==|~=)\s*(3\.\d{1,2})", constraint)
        return exact.group(1) if exact else ""

    def _with_test_dependency_install(
        self,
        profile,
        command: CommandSpec,
        *,
        overlay_requirements: tuple[str, ...] = (),
        dependency_commands: tuple[str, ...] | None = None,
    ) -> CommandSpec:
        display = command.display
        tools: list[str] = []
        lowered = display.casefold()
        matrix_runner = bool(re.search(r"\b(?:tox|nox)\b", lowered))
        installs = (
            []
            if matrix_runner
            else list(dependency_commands)
            if dependency_commands is not None
            else self._declared_test_dependency_commands(profile)
        )
        # A project-owned extra/group/requirements file is authoritative for the
        # runner version. Appending our fixed pytest pin can conflict with a
        # project pin and also breaks pip's --require-hashes mode for lock-style
        # requirements files. Only bootstrap pytest when no declared test
        # dependency source was found.
        if not installs and re.search(r"\b(pytest|py\.test)\b", lowered):
            tools.append(f"pytest=={self.config.pytest_version}")
        if self._parallel_metadata(profile) and re.search(
            r"(?:--numprocesses(?:=|\s)|(?:^|\s)-n\s)", display
        ):
            tools.append(f"pytest-xdist=={self.config.pytest_xdist_version}")
        if re.search(r"\btox\b", lowered):
            tools.append(f"tox=={self.config.tox_version}")
        if re.search(r"\bnox\b", lowered):
            tools.append(f"nox=={self.config.nox_version}")
        tools.extend(overlay_requirements)
        if tools:
            tool_requirements = " ".join(shlex.quote(tool) for tool in dict.fromkeys(tools))
            if installs and installs[-1].startswith("python -m pip install "):
                installs[-1] += " " + tool_requirements
            else:
                installs.append("python -m pip install " + tool_requirements)
        if not installs:
            return command
        return replace(
            command,
            argv=(" && ".join((*installs, display)),),
            shell=True,
        )

    @staticmethod
    def _with_dependency_closed_targets(selection, plan: TestDependencyPlan):
        capture_disabled = selection.command.command.display.endswith(" -s")
        argv = ("python", "-m", "pytest", *plan.targets)
        if capture_disabled:
            argv += ("-s",)
        command = ProjectCommand(
            selection.command.name,
            replace(selection.command.command, argv=argv, shell=False),
            f"dependency-closed:{selection.command.source}",
            selection.command.confidence,
        )
        kind = selection.kind
        if "dependency-closed-slice" not in kind:
            kind = f"{kind}+dependency-closed-slice"
        return replace(
            selection,
            command=command,
            kind=kind,
            targets=plan.targets,
            reason=f"{selection.reason}; {plan.reason}",
        )

    def _with_bounded_parallelism(
        self,
        profile,
        command: CommandSpec,
    ) -> tuple[CommandSpec, int]:
        metadata = self._parallel_metadata(profile)
        display = command.display.strip()
        if (
            not metadata
            or not re.search(r"\b(?:pytest|py\.test)\b", display, re.IGNORECASE)
            or re.search(r"(?:--numprocesses(?:=|\s)|(?:^|\s)-n\s)", display)
            or re.search(r"[;&|]", display)
        ):
            return command, 0
        requested = str(metadata.get("requested_workers", "")).strip()
        workers = self.config.max_parallel_test_workers
        if requested.isdigit():
            workers = min(workers, int(requested))
        if workers <= 1:
            return command, 0
        return (
            replace(
                command,
                argv=(f"{display} --numprocesses {workers}",),
                shell=True,
            ),
            workers,
        )

    @staticmethod
    def _parallel_metadata(profile) -> Mapping[str, object]:
        value = profile.metadata.get("pytest_parallel", {})
        if not isinstance(value, Mapping):
            return {}
        if (
            value.get("runner") != "pytest"
            or value.get("dependency") != "pytest-xdist"
            or value.get("argument") != "--numprocesses"
            or value.get("ci_confirmed") is not True
        ):
            return {}
        return value

    @staticmethod
    def _declared_test_dependency_commands(profile) -> list[str]:
        metadata = profile.metadata
        extras = tuple(metadata.get("test_dependency_extras", ()))
        manager_groups = tuple(metadata.get("test_dependency_manager_groups", ()))
        legacy_groups = tuple(metadata.get("test_dependency_groups", ()))
        managers = set(profile.package_managers)
        if not extras and not manager_groups and legacy_groups:
            if managers & {"poetry", "pdm", "uv"}:
                manager_groups = legacy_groups
            else:
                extras = legacy_groups

        if "poetry" in managers and (manager_groups or extras):
            selected_groups = ",".join(dict.fromkeys(("main", *manager_groups)))
            command = f"poetry install --only {shlex.quote(selected_groups)}"
            if extras:
                command += " --extras " + shlex.quote(" ".join(extras))
            return [command + " --no-interaction --no-ansi"]
        elif "uv" in managers and (manager_groups or extras):
            parts = ["uv sync --frozen --inexact --no-default-groups"]
            parts.extend(f"--group {shlex.quote(group)}" for group in manager_groups)
            parts.extend(f"--extra {shlex.quote(extra)}" for extra in extras)
            return [" ".join(parts)]
        elif "pdm" in managers and (manager_groups or extras):
            groups = tuple(dict.fromkeys((*manager_groups, *extras)))
            pdm_command = (
                "pdm sync"
                if any(
                    PurePosixPath(path).name.casefold() == "pdm.lock"
                    for path in profile.dependency_files
                )
                else "pdm install"
            )
            return [
                f"{pdm_command} --no-editable "
                + " ".join(f"-G {shlex.quote(group)}" for group in groups)
            ]
        elif extras:
            target = ".[" + ",".join(extras) + "]"
            return [f"python -m pip install {shlex.quote(target)}"]

        requirement_files = [
            path
            for path in profile.dependency_files
            if TestabilityVerifier._test_requirement_priority(path) is not None
        ]
        if not requirement_files:
            return []
        selected = min(
            requirement_files,
            key=lambda path: (
                TestabilityVerifier._test_requirement_priority(path),
                len(PurePosixPath(path).parts),
                path,
            ),
        )
        return [f"python -m pip install -r {shlex.quote(selected)}"]

    @staticmethod
    def _test_requirement_priority(path: str) -> int | None:
        name = PurePosixPath(path).name.lower()
        if not name.endswith((".txt", ".in")):
            return None
        stem = name.rsplit(".", 1)[0]
        if re.fullmatch(
            r"(?:requirements[-_.](?:test|tests|testing)|"
            r"(?:test|tests|testing)[-_.]requirements)",
            stem,
        ):
            return 0
        match = re.search(
            r"(?:^|[-_.])(test|tests|testing|tox|dev|develop|development|qa)(?:[-_.]|$)",
            name,
        )
        if not match:
            return None
        kind = match.group(1)
        if kind in {"test", "tests", "testing"}:
            return 1
        if kind == "tox":
            return 2
        return 3
