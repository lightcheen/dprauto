"""Execute a project-owned test command; never promote smoke probes to tests."""

import re
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath
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
from dprauto.verification.commands import TestCommandSelection, TestCommandSelector
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
        selections = self.selector.select_candidates_with_details(
            context.profile,
            python_version=self._runtime_python_version(context),
            max_candidates=self.config.max_test_command_candidates,
        )
        if not selections:
            return self._verify_selection(context, None)

        attempts: list[dict[str, object]] = []
        check_entries: list[tuple[int, VerificationCheck, bool]] = []
        executed_candidates: list[int] = []
        final: VerificationResult | None = None
        for index, selection in enumerate(selections, start=1):
            preflight_error = self._preflight_selection(context, selection)
            if preflight_error:
                check_entries.append(
                    (
                        index,
                        VerificationCheck(
                            f"project-tests-candidate-{index}",
                            VerificationStatus.SKIPPED,
                            preflight_error,
                            metadata={"source": selection.command.source},
                        ),
                        False,
                    )
                )
                attempts.append(
                    {
                        "candidate": index,
                        "source": selection.command.source,
                        "command": selection.command.command.display,
                        "status": "preflight-rejected",
                        "outcome_category": "candidate-preflight-rejected",
                        "retryable": True,
                        "evidence_strength": "static",
                        "reason": preflight_error,
                    }
                )
                continue
            result = self._verify_selection(context, selection)
            final = result
            executed_candidates.append(index)
            check_entries.extend((index, check, True) for check in result.checks)
            retryable = self._should_try_next(result)
            attempts.append(
                {
                    "candidate": index,
                    "source": selection.command.source,
                    "command": selection.command.command.display,
                    "executed_command": result.metadata.get(
                        "command", selection.command.command.display
                    ),
                    "status": result.status.value,
                    "outcome_category": result.metadata.get(
                        "outcome_category", "verification-result"
                    ),
                    "retryable": retryable,
                    "evidence_strength": result.metadata.get(
                        "test_evidence_strength", "limited"
                    ),
                    "observed_test_count": result.metadata.get(
                        "observed_test_count", 0
                    ),
                    "exit_code": (
                        result.command_result.exit_code
                        if result.command_result is not None
                        else None
                    ),
                    "timed_out": bool(
                        result.command_result and result.command_result.timed_out
                    ),
                    "duration_seconds": (
                        result.command_result.duration_seconds
                        if result.command_result is not None
                        else 0.0
                    ),
                    "summary": result.summary,
                }
            )
            if result.passed or not retryable:
                break

        if final is None:
            summary = "all project test command candidates were rejected by static preflight"
            return VerificationResult(
                verification_id(self.level),
                self.level,
                VerificationStatus.SKIPPED,
                summary=summary,
                metadata={
                    "command_kind": "none",
                    "skip_reason": "all-candidates-preflight-rejected",
                    "command_attempts": tuple(attempts),
                    "candidate_count": len(selections),
                },
                checks=tuple(check for _index, check, _executed in check_entries),
            )
        final_candidate = executed_candidates[-1]
        fallback_used = final_candidate > 1
        if final.passed:
            candidate_stop_reason = "tests-passed"
        elif not self._should_try_next(final):
            candidate_stop_reason = (
                "non-retryable-"
                + str(final.metadata.get("outcome_category", "verification-failure"))
            )
        else:
            candidate_stop_reason = "candidate-set-exhausted"
        checks: list[VerificationCheck] = []
        for candidate, check, executed in check_entries:
            check_id = check.check_id
            if candidate > 1 and not check_id:
                check_id = f"{check.name}-candidate-{candidate}"
            elif candidate > 1:
                check_id = f"{check_id}-candidate-{candidate}"
            if executed and candidate != final_candidate:
                check = replace(
                    check,
                    status=VerificationStatus.SKIPPED,
                    summary=(
                        f"candidate {candidate} was superseded by a later executable "
                        f"candidate; original result: {check.summary}"
                    ),
                    metadata={
                        **check.metadata,
                        "superseded": True,
                        "original_status": check.status.value,
                    },
                )
            if check_id:
                check = replace(check, check_id=check_id)
            checks.append(check)
        return replace(
            final,
            checks=tuple(checks),
            metadata={
                **final.metadata,
                "command_attempts": tuple(attempts),
                "candidate_count": len(selections),
                "attempted_candidate_count": sum(
                    item["status"] != "preflight-rejected" for item in attempts
                ),
                "selected_candidate": final_candidate,
                "candidate_fallback_used": fallback_used,
                "candidate_stop_reason": candidate_stop_reason,
            },
        )

    def _verify_selection(
        self,
        context: VerificationContext,
        selection: TestCommandSelection | None,
    ) -> VerificationResult:
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
        make_preflight = self._preflight_generated_make_target(
            context,
            selected.command,
            image,
        )
        if make_preflight is not None:
            return make_preflight
        if "bounded-file-slice" in selection.kind:
            test_command, parallel_workers = selected.command, 0
        else:
            test_command, parallel_workers = self._with_bounded_parallelism(
                context.profile,
                selected.command,
            )
        test_command = self._with_target_preflight(test_command, selection.targets)
        before_native_preparation = test_command
        test_command = self._with_native_test_preparation(context, test_command)
        native_test_preparation = test_command.display != before_native_preparation.display
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
            dependency_commands_are_additive=dependency_plan.additive,
        )
        dependency_setup = command.display != test_command.display
        command = self._with_gradle_result_probe(
            context.profile,
            command,
            selection,
        )
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
        if dependency_setup:
            timeout_policy = "dependency-and-test"
            requested_timeout_seconds = self.config.dependency_command_timeout_seconds
        elif native_test_preparation:
            timeout_policy = "native-test-preparation-and-test"
            requested_timeout_seconds = (
                self.config.native_test_preparation_timeout_seconds
                + self._test_timeout_seconds(context)
            )
        else:
            timeout_policy = "test-only"
            requested_timeout_seconds = self._test_timeout_seconds(context)
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
        outcome_category, retryable = self._execution_outcome(execution)
        passed = outcome_category == "tests-passed"
        test_evidence = self._test_evidence(
            selection,
            execution.output_excerpt,
            passed=passed,
            outcome_category=outcome_category,
        )
        check = VerificationCheck(
            "project-tests",
            VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            f"project test command from {selected.source} {'passed' if passed else 'failed'}",
            command_result=execution.command_result,
            evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            metadata={
                "source": selected.source,
                "output_excerpt": execution.output_excerpt,
                "outcome_category": outcome_category,
                "retryable_candidate_failure": retryable,
                "test_evidence": test_evidence,
            },
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
                "native_test_preparation": native_test_preparation,
                "native_test_preparation_timeout_seconds": (
                    self.config.native_test_preparation_timeout_seconds
                    if native_test_preparation
                    else 0
                ),
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
                "dependency_commands_are_additive": dependency_plan.additive,
                "outcome_category": outcome_category,
                "candidate_retryable": retryable,
                "test_evidence": test_evidence,
                "test_evidence_strength": test_evidence["strength"],
                "test_evidence_scope": test_evidence["scope"],
                "test_evidence_source_kind": test_evidence["source_kind"],
                "observed_test_count": test_evidence["observed_test_count"],
            },
            checks=(check,),
        )

    @staticmethod
    def _execution_outcome(execution) -> tuple[str, bool]:
        result = execution.command_result
        text = execution.output_excerpt.casefold()
        no_tests = re.search(
            r"(?:no tests ran|collected 0 items|no tests were found|"
            r"dprauto_no_tests_collected)",
            text,
        )
        if no_tests and not TestabilityVerifier._has_positive_test_evidence(text):
            return "no-tests-collected", True
        if result.succeeded:
            return "tests-passed", False
        if result.timed_out:
            return "test-timeout", False
        if result.exit_code == 127 or re.search(
            r"(?:command not found|could not find executable|/bin/sh: .*: not found)",
            text,
        ):
            return "candidate-command-unavailable", True
        if re.search(
            r"(?:no rule to make target|unknown task|task .* not found|"
            r"file or directory not found)",
            text,
        ):
            return "candidate-target-unavailable", True
        if re.search(
            r"(?:connection refused|service unavailable|nocredentialserror|"
            r"api[_ -]?key.*(?:missing|required|not set))",
            text,
        ):
            return "external-prerequisite-unavailable", False
        if re.search(
            r"(?:modulenotfounderror|no module named|could not find a version that satisfies|"
            r"no matching distribution found|resolutionimpossible)",
            text,
        ):
            return "test-dependency-failure", False
        if re.search(
            r"(?:=+ failures =+|\bassertionerror\b|\bfailed:\s|"
            r"\d+\s+failed(?:,|\s|$)|failures:\s*[1-9]\d*|"
            r"tests failed out of)",
            text,
        ):
            return "project-test-failure", False
        return "test-command-failure", False

    @staticmethod
    def _has_positive_test_evidence(output: str) -> bool:
        return bool(
            re.search(
                r"(?:\b[1-9]\d*\s+passed\b|\bran\s+[1-9]\d*\s+tests?\b|"
                r"\btests run:\s*[1-9]\d*\b|\bout of\s+[1-9]\d*\b|"
                r"dprauto_test_count=[1-9]\d*|running the integration test)",
                output,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _test_evidence(
        cls,
        selection: TestCommandSelection,
        output: str,
        *,
        passed: bool,
        outcome_category: str,
    ) -> dict[str, object]:
        counts: list[int] = []
        for pattern in (
            r"\b(\d+)\s+passed\b",
            r"\bran\s+(\d+)\s+tests?\b",
            r"\btests run:\s*(\d+)\b",
            r"\btests failed out of\s+(\d+)\b",
            r"\bout of\s+(\d+)\b",
            r"\bdprauto_test_count=(\d+)\b",
        ):
            counts.extend(int(value) for value in re.findall(pattern, output, re.I))
        observed_count = max(counts, default=0)
        if passed and (observed_count > 0 or selection.targets):
            strength = "strong"
        elif outcome_category == "project-test-failure":
            strength = "strong"
        elif passed:
            strength = "moderate"
        else:
            strength = "limited"
        source = selection.command.source.casefold()
        if source.startswith((".github/", ".circleci/")) or "workflow" in source:
            source_kind = "ci"
        elif any(
            value in source
            for value in (
                "pyproject",
                "tox.ini",
                "noxfile",
                "pom.xml",
                "build.gradle",
                "cmakelists",
                "makefile",
            )
        ):
            source_kind = "build-manifest"
        elif "readme" in source:
            source_kind = "documentation"
        elif "inferred:" in source:
            source_kind = "inferred"
        else:
            source_kind = "repository"
        return {
            "strength": strength,
            "scope": "bounded-targets" if selection.targets else "project-command",
            "source_kind": source_kind,
            "source": selection.command.source,
            "observed_test_count": observed_count,
            "selected_target_count": len(selection.targets),
            "outcome_category": outcome_category,
        }

    @staticmethod
    def _with_gradle_result_probe(
        profile,
        command: CommandSpec,
        selection: TestCommandSelection,
    ) -> CommandSpec:
        """Require fresh Gradle XML evidence for a bounded class selection."""

        managers = {value.casefold() for value in profile.package_managers}
        if "gradle" not in managers or not selection.targets:
            return command
        if not re.search(r"(?:^|\s)(?:\S*/)?gradlew?(?:\s|$)", command.display):
            return command
        probe = (
            f"{{ {command.display}; }} && "
            "report_count=$(find . -type f "
            "-path '*/build/test-results/*/TEST-*.xml' "
            "-exec grep -h -o 'tests=\"[0-9][0-9]*\"' {} + 2>/dev/null "
            "| awk -F'\"' '{total += $2} END {print total + 0}'); "
            "if [ \"$report_count\" -gt 0 ]; then "
            "echo DPRAUTO_TEST_COUNT=$report_count; "
            "else echo DPRAUTO_NO_TESTS_COLLECTED; exit 1; fi"
        )
        return replace(command, argv=(probe,), shell=True)

    @staticmethod
    def _preflight_selection(
        context: VerificationContext,
        selection: TestCommandSelection,
    ) -> str:
        workspace = context.workspace.resolve()
        cwd = selection.command.command.cwd or "."
        command_root = (workspace / cwd).resolve()
        if workspace != command_root and workspace not in command_root.parents:
            return f"candidate working directory escapes workspace: {cwd}"
        if not command_root.is_dir():
            return f"candidate working directory does not exist: {cwd}"
        missing = [
            path
            for path in selection.targets
            if not (workspace / path).is_file()
        ]
        if missing:
            return "selected repository test file is missing: " + ", ".join(missing[:3])
        make_target = TestabilityVerifier._make_target(selection.command.command)
        target_exists = (
            TestabilityVerifier._make_target_exists(command_root, make_target)
            if make_target
            else None
        )
        if target_exists is False:
            return f"Make target {make_target!r} is not declared under {cwd}"
        return ""

    @staticmethod
    def _make_target(command: CommandSpec) -> str:
        display = command.display
        if re.search(r"[;&|]", display):
            return ""
        try:
            tokens = shlex.split(display)
        except ValueError:
            return ""
        if not tokens or PurePosixPath(tokens[0]).name.casefold() not in {"make", "gmake"}:
            return ""
        return next(
            (
                token
                for token in tokens[1:]
                if not token.startswith("-") and "=" not in token
            ),
            "",
        )

    @staticmethod
    def _make_target_exists(root: Path, target: str) -> bool | None:
        makefiles = tuple(
            path for name in ("GNUmakefile", "makefile", "Makefile")
            if (path := root / name).is_file()
        )
        if not makefiles:
            # Autotools and configure-based projects create their Makefile in
            # the image. Absence in the source tree is unknown, not negative.
            return None
        included = tuple(
            path
            for path in root.rglob("*.mk")
            if path.is_file() and len(path.relative_to(root).parts) <= 4
        )[:128]
        pattern = re.compile(rf"(?m)^(?![.#\t ])[^\n:]*\b{re.escape(target)}\b[^\n:]*\s*:")
        for makefile in (*makefiles, *included):
            try:
                if pattern.search(makefile.read_text(encoding="utf-8", errors="replace")):
                    return True
            except OSError:
                continue
        return False

    def _preflight_generated_make_target(
        self,
        context: VerificationContext,
        command: CommandSpec,
        image: str,
    ) -> VerificationResult | None:
        """Dry-run targets that only exist in a generated in-image Makefile."""

        target = self._make_target(command)
        if not target:
            return None
        workspace = context.workspace.resolve()
        command_root = (workspace / (command.cwd or ".")).resolve()
        if self._make_target_exists(command_root, target) is not None:
            return None
        dry_run = self._make_dry_run_command(command)
        if dry_run is None:
            return None
        timeout_seconds = clamped_timeout_seconds(30, context.deadline_at)
        if timeout_seconds <= 0:
            summary = "workflow time budget exceeded before generated Make target preflight"
            check = VerificationCheck(
                "project-tests-make-preflight",
                VerificationStatus.ERROR,
                summary,
            )
            return VerificationResult(
                verification_id(self.level),
                self.level,
                check.status,
                summary=summary,
                metadata={"command_kind": "make-target-preflight"},
                checks=(check,),
            )
        execution = self.runtime.run_image(
            image,
            dry_run,
            timeout_seconds=timeout_seconds,
        )
        if execution.command_result.succeeded:
            return None
        summary = (
            f"generated Make target {target!r} cannot be expanded safely in the built image"
        )
        check = VerificationCheck(
            "project-tests-make-preflight",
            VerificationStatus.SKIPPED,
            summary,
            command_result=execution.command_result,
            evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            metadata={"output_excerpt": execution.output_excerpt},
        )
        return VerificationResult(
            verification_id(self.level),
            self.level,
            check.status,
            command_result=execution.command_result,
            evidence=check.evidence,
            summary=summary,
            metadata={
                "command_kind": "make-target-preflight",
                "skip_reason": "generated-make-target-dry-run-failed",
                "make_target": target,
                "preflight_command": dry_run.display,
                "effective_timeout_seconds": timeout_seconds,
            },
            checks=(check,),
        )

    @staticmethod
    def _make_dry_run_command(command: CommandSpec) -> CommandSpec | None:
        display = command.display
        if re.search(r"[;&|]", display):
            return None
        try:
            tokens = shlex.split(display)
        except ValueError:
            return None
        if not tokens or PurePosixPath(tokens[0]).name.casefold() not in {"make", "gmake"}:
            return None
        rendered = " ".join(
            shlex.quote(item)
            for item in (tokens[0], "--dry-run", "--no-builtin-rules", *tokens[1:])
        )
        return replace(command, argv=(rendered,), shell=True)

    @staticmethod
    def _with_target_preflight(
        command: CommandSpec,
        targets: tuple[str, ...],
    ) -> CommandSpec:
        if not targets:
            return command
        checks = " && ".join(
            f"test -f {shlex.quote('/workspace/' + target)}" for target in targets
        )
        return replace(command, argv=(f"{checks} && {command.display}",), shell=True)

    def _with_native_test_preparation(
        self,
        context: VerificationContext,
        command: CommandSpec,
    ) -> CommandSpec:
        if not re.match(r"^ctest(?:\s|$)", command.display.strip(), re.IGNORECASE):
            return command
        raw = context.profile.metadata.get("cmake_test_configuration_arguments", ())
        arguments = tuple(
            value
            for value in raw[:16]
            if isinstance(value, str)
            and re.fullmatch(r"-D[A-Za-z_][A-Za-z0-9_]{0,63}=(?:ON|OFF|DOWNLOAD)", value)
        ) if isinstance(raw, (list, tuple)) else ()
        build_target = str(context.profile.metadata.get("cmake_test_build_target", ""))
        configure = "cmake -S . -B build"
        if arguments:
            configure += " " + " ".join(arguments)
        build = "cmake --build build"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.+-]{0,63}", build_target):
            build += " --target " + build_target
        preparation = f"{configure} && {build}"
        bounded_preparation = (
            "timeout --signal=TERM --kill-after=5s "
            f"{self.config.native_test_preparation_timeout_seconds}s "
            f"sh -c {shlex.quote(preparation)}"
        )
        return replace(
            command,
            argv=(f"{bounded_preparation} && {command.display}",),
            shell=True,
        )

    @staticmethod
    def _should_try_next(result: VerificationResult) -> bool:
        retryable = result.metadata.get("candidate_retryable")
        if isinstance(retryable, bool):
            return retryable
        if result.status in {VerificationStatus.ERROR, VerificationStatus.SKIPPED}:
            summary = result.summary.casefold()
            return not any(
                value in summary
                for value in (
                    "time budget exceeded",
                    "cannot run tests without an image",
                    "invalid testability dependency overlay",
                    "cannot orchestrate required test prerequisites",
                )
            )
        excerpts = "\n".join(
            str(check.metadata.get("output_excerpt", "")) for check in result.checks
        ).casefold()
        return bool(
            re.search(
                r"(?:command not found|no rule to make target|unknown task|"
                r"task .* not found|file or directory not found|no tests ran|"
                r"collected 0 items|could not find executable)",
                excerpts,
            )
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

    def _test_timeout_seconds(self, context: VerificationContext) -> int:
        build_system = str(
            context.profile.metadata.get("primary_build_system", "")
        ).casefold()
        if build_system in {"gradle", "maven"}:
            return self.config.jvm_command_timeout_seconds
        return self.config.command_timeout_seconds

    def _with_test_dependency_install(
        self,
        profile,
        command: CommandSpec,
        *,
        overlay_requirements: tuple[str, ...] = (),
        dependency_commands: tuple[str, ...] | None = None,
        dependency_commands_are_additive: bool = False,
    ) -> CommandSpec:
        display = command.display
        tools: list[str] = []
        lowered = display.casefold()
        matrix_runner = bool(re.search(r"\b(?:tox|nox)\b", lowered))
        declared_installs = self._declared_test_dependency_commands(profile)
        if matrix_runner:
            installs = []
        elif dependency_commands is None:
            installs = declared_installs
        elif dependency_commands_are_additive:
            installs = list(dict.fromkeys((*declared_installs, *dependency_commands)))
        else:
            installs = list(dependency_commands)
        # A project-owned extra/group/requirements file is authoritative for the
        # runner version. Appending our fixed pytest pin can conflict with a
        # project pin and also breaks pip's --require-hashes mode for lock-style
        # requirements files. Only bootstrap pytest when no declared test
        # dependency source was found.
        declared_pytest = self._declared_test_source_provides_pytest(profile)
        if (
            re.search(r"\b(pytest|py\.test)\b", lowered)
            and declared_pytest is False
            and not any(re.search(r"\bpytest\b", item) for item in installs)
        ):
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
        # Dependency setup may need the forwarded host proxy. Project tests run
        # after setup with those variables removed so socket-mocking tests do
        # not accidentally observe or connect to the host proxy endpoint.
        test_display = self._proxy_neutral_test_command(display)
        return replace(
            command,
            argv=(" && ".join((*installs, test_display)),),
            shell=True,
        )

    @staticmethod
    def _proxy_neutral_test_command(display: str) -> str:
        names = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
        return "env " + " ".join(f"-u {name}" for name in names) + f" {display}"

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
    def _declared_test_source_provides_pytest(profile) -> bool | None:
        metadata = profile.metadata
        selected = tuple(metadata.get("test_dependency_extras", ())) + tuple(
            metadata.get("test_dependency_manager_groups", ())
        )
        mappings = (
            metadata.get("optional_dependency_groups", {}),
            metadata.get("manager_dependency_groups", {}),
        )
        group_evidence = False
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                continue
            for group in selected:
                if group not in mapping:
                    continue
                group_evidence = True
                values = mapping.get(group, ())
                if isinstance(values, (list, tuple)) and any(
                    isinstance(value, str)
                    and value.casefold().replace("_", "-") == "pytest"
                    for value in values
                ):
                    return True
        if group_evidence:
            return False
        if TestabilityVerifier._declared_test_dependency_commands(profile):
            return None
        return False

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
