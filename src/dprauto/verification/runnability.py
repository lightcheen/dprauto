"""Runtime policies specialized by normalized ProjectProfile type."""

from __future__ import annotations

import re
import shlex

from dprauto.config import VerificationConfig
from dprauto.domain.enums import (
    CommandPurpose,
    ProjectType,
    VerificationLevel,
    VerificationStatus,
)
from dprauto.domain.models import CommandSpec, VerificationCheck, VerificationResult
from dprauto.ports.runtime import ContainerRuntime
from dprauto.ports.verification import VerificationContext
from dprauto.time_budget import clamped_timeout_seconds
from dprauto.verification.commands import select_run_command
from dprauto.verification.common import aggregate_status, verification_id


class RunnabilityVerifier:
    level = VerificationLevel.RUNNABILITY

    def __init__(
        self,
        runtime: ContainerRuntime,
        config: VerificationConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.config = config or VerificationConfig()

    def supports(self, profile, level: VerificationLevel) -> bool:
        return level is self.level and profile.project_type is not ProjectType.UNKNOWN

    def verify(self, context: VerificationContext) -> VerificationResult:
        image = context.build_result.image_reference
        if not image:
            return self._single_error("runtime", "cannot verify runtime without an image")
        if context.profile.project_type is ProjectType.WEB:
            return self._verify_web(context, image)
        if context.profile.project_type is ProjectType.CLI:
            return self._verify_cli(context, image)
        if context.profile.project_type is ProjectType.SCRIPT:
            return self._verify_script(context, image)
        if context.profile.project_type is ProjectType.LIBRARY:
            return self._verify_library(context, image)
        check = VerificationCheck("runtime", VerificationStatus.SKIPPED, "unknown project type")
        return VerificationResult(
            verification_id(self.level), self.level, check.status, summary=check.summary, checks=(check,)
        )

    def _verify_web(self, context: VerificationContext, image: str) -> VerificationResult:
        selected = select_run_command(context.profile)
        inspection = self.runtime.inspect_image(image)
        command = selected.command if selected else None
        port = inspection.exposed_ports[0] if inspection.exposed_ports else self._infer_web_port(
            command.display if command else ""
        )
        timeout_seconds = self._timeout_or_zero(
            self.config.web_startup_timeout_seconds, context
        )
        if timeout_seconds <= 0:
            return self._single_error("web-runtime", "workflow time budget exceeded before web probe")
        probe = self.runtime.probe_web(
            image,
            command,
            container_port=port,
            timeout_seconds=timeout_seconds,
            path=self.config.web_path,
        )
        http_ok = bool(
            probe.http_reachable
            and probe.http_status is not None
            and 100 <= probe.http_status < 500
        )
        checks = (
            VerificationCheck(
                "web-process",
                VerificationStatus.PASSED if probe.process_running else VerificationStatus.FAILED,
                "web process remained running" if probe.process_running else "web process exited",
            ),
            VerificationCheck(
                "web-port",
                VerificationStatus.PASSED if probe.port_open else VerificationStatus.FAILED,
                f"container port {port} accepted connections" if probe.port_open else f"container port {port} did not accept connections",
            ),
            VerificationCheck(
                "web-http",
                VerificationStatus.PASSED if http_ok else VerificationStatus.FAILED,
                (
                    f"HTTP responded with acceptable status {probe.http_status}"
                    if http_ok
                    else (
                        f"HTTP responded with server-error status {probe.http_status}"
                        if probe.http_reachable and probe.http_status is not None
                        else "HTTP endpoint was unreachable"
                    )
                ),
                metadata={"output_excerpt": probe.output_excerpt},
            ),
        )
        if http_ok and probe.process_running and probe.port_open:
            outcome_category = "service-responsive"
            evidence_strength = "strong"
        elif not probe.process_running:
            outcome_category = "service-process-exited"
            evidence_strength = "none"
        elif not probe.port_open:
            outcome_category = "service-port-unreachable"
            evidence_strength = "none"
        elif probe.http_reachable:
            outcome_category = "service-http-server-error"
            evidence_strength = "limited"
        else:
            outcome_category = "service-http-unreachable"
            evidence_strength = "limited"
        return self._result(
            checks,
            metadata={
                "container_port": port,
                "host_port": probe.host_port,
                "runtime_contract": "service-health",
                "runtime_outcome_category": outcome_category,
                "runtime_evidence_strength": evidence_strength,
                "runtime_evidence_scope": "process-port-http",
                "runtime_probe_source": selected.source if selected else "image-default",
                "selected_command": command.display if command else "",
                "http_status": probe.http_status,
            },
        )

    def _verify_cli(self, context: VerificationContext, image: str) -> VerificationResult:
        selected = select_run_command(context.profile)
        selected_command = selected.command if selected else None
        command = self._safe_inferred_cli_probe(selected)
        timeout_seconds = self._timeout_or_zero(self.config.command_timeout_seconds, context)
        if timeout_seconds <= 0:
            return self._single_error("cli-runtime", "workflow time budget exceeded before CLI probe")
        execution = self.runtime.run_image(
            image,
            command,
            timeout_seconds=timeout_seconds,
        )
        fallback_used = False
        fallback_reason = ""
        if (
            selected is not None
            and command is not None
            and not self._has_help_or_version_argument(command)
            and (
                not execution.command_result.succeeded
                or not execution.output_excerpt.strip()
            )
        ):
            fallback_timeout = self._timeout_or_zero(
                self.config.command_timeout_seconds, context
            )
            if fallback_timeout > 0:
                help_execution = self.runtime.run_image(
                    image,
                    self._help_command(command),
                    timeout_seconds=fallback_timeout,
                )
                if (
                    help_execution.command_result.succeeded
                    and help_execution.output_excerpt.strip()
                ):
                    fallback_reason = (
                        "initial-command-failed"
                        if not execution.command_result.succeeded
                        else "initial-command-produced-no-output"
                    )
                    execution = help_execution
                    fallback_used = True
        exit_ok = execution.command_result.succeeded
        output_ok = bool(execution.output_excerpt.strip())
        checks = (
            VerificationCheck(
                "cli-exit-code",
                VerificationStatus.PASSED if exit_ok else VerificationStatus.FAILED,
                "CLI exited with code 0" if exit_ok else "CLI returned a non-zero exit or timed out",
                command_result=execution.command_result,
                evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            ),
            VerificationCheck(
                "cli-output",
                VerificationStatus.PASSED if output_ok else VerificationStatus.FAILED,
                "CLI produced output" if output_ok else "CLI produced no observable output",
                metadata={"output_excerpt": execution.output_excerpt},
            ),
        )
        return self._result(
            checks,
            command_result=execution.command_result,
            metadata={
                "help_fallback_used": fallback_used,
                "help_fallback_reason": fallback_reason,
                "empty_output_help_fallback": (
                    fallback_used
                    and fallback_reason == "initial-command-produced-no-output"
                ),
                "safe_probe_normalized": command != selected_command,
                "selected_command": selected_command.display if selected_command else "",
                "runtime_contract": "cli-entrypoint",
                "runtime_outcome_category": self._command_outcome(
                    execution,
                    success="cli-invocable" if output_ok else "cli-no-observable-output",
                ),
                "runtime_evidence_strength": (
                    "moderate" if exit_ok and output_ok else "none"
                ),
                "runtime_evidence_scope": "safe-help-or-version-probe",
                "runtime_probe_source": selected.source if selected else "image-default",
                **self._execution_metadata(execution),
            },
        )

    @classmethod
    def _safe_inferred_cli_probe(cls, selected) -> CommandSpec | None:
        if selected is None:
            return None
        command = selected.command
        if not selected.source.startswith("inferred:"):
            return command
        try:
            tokens = (
                tuple(command.argv)
                if not command.shell
                else tuple(shlex.split(command.display))
            )
        except (TypeError, ValueError):
            return command
        bare_entry = len(tokens) == 1
        bare_module = (
            len(tokens) == 3
            and tokens[0].casefold().startswith("python")
            and tokens[1:2] == ("-m",)
        )
        if not (bare_entry or bare_module) or cls._has_help_or_version_argument(command):
            return command
        return cls._help_command(command)

    @staticmethod
    def _has_help_or_version_argument(command: CommandSpec) -> bool:
        return bool(
            re.search(
                r"(?:^|\s)(?:--help|-h|--version|-V)(?:\s|$)",
                command.display,
            )
        )

    @staticmethod
    def _help_command(command: CommandSpec) -> CommandSpec:
        argv = (
            (f"{command.argv[0]} --help",)
            if command.shell and len(command.argv) == 1
            else (*command.argv, "--help")
        )
        return CommandSpec(
            argv,
            purpose=command.purpose,
            cwd=command.cwd,
            environment=command.environment,
            timeout_seconds=command.timeout_seconds,
            shell=command.shell,
        )

    def _verify_script(self, context: VerificationContext, image: str) -> VerificationResult:
        selected = select_run_command(context.profile)
        timeout_seconds = self._timeout_or_zero(self.config.command_timeout_seconds, context)
        if timeout_seconds <= 0:
            return self._single_error("script-runtime", "workflow time budget exceeded before script probe")
        execution = self.runtime.run_image(
            image,
            selected.command if selected else None,
            timeout_seconds=timeout_seconds,
        )
        exit_ok = execution.command_result.succeeded
        observable = bool(execution.output_excerpt.strip() or execution.filesystem_changes)
        checks = (
            VerificationCheck(
                "script-exit-code",
                VerificationStatus.PASSED if exit_ok else VerificationStatus.FAILED,
                "script exited with code 0" if exit_ok else "script returned a non-zero exit or timed out",
                command_result=execution.command_result,
                evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            ),
            VerificationCheck(
                "script-observable-effect",
                VerificationStatus.PASSED if observable else VerificationStatus.FAILED,
                "script produced output or a filesystem side effect" if observable else "script produced no observable output or filesystem side effect",
                metadata={
                    "output_excerpt": execution.output_excerpt,
                    "filesystem_changes": execution.filesystem_changes,
                },
            ),
        )
        return self._result(
            checks,
            command_result=execution.command_result,
            metadata={
                "runtime_contract": "script-observable-effect",
                "runtime_outcome_category": self._command_outcome(
                    execution,
                    success=(
                        "script-observable-execution"
                        if observable
                        else "script-no-observable-effect"
                    ),
                ),
                "runtime_evidence_strength": (
                    "strong" if exit_ok and observable else "none"
                ),
                "runtime_evidence_scope": "exit-output-filesystem-diff",
                "runtime_probe_source": selected.source if selected else "image-default",
                "selected_command": selected.command.display if selected else "",
                **self._execution_metadata(execution),
            },
        )

    def _verify_library(self, context: VerificationContext, image: str) -> VerificationResult:
        compiled_probe = self._compiled_library_probe(context)
        if compiled_probe is None:
            compiled_probe = self._image_default_compiled_library_probe(context, image)
        if compiled_probe is not None:
            return self._verify_compiled_library(context, image, *compiled_probe)
        if not self._is_python_library(context):
            return self._single_error(
                "library-artifact",
                "no safe compiled-library runtime probe was discovered",
            )
        modules = tuple(context.profile.metadata.get("import_modules", ()))
        project_name = str(context.profile.metadata.get("project_name", "")).replace("-", "_")
        module = modules[0] if modules else project_name
        if not module or not all(part.isidentifier() for part in module.split(".")):
            return self._single_error("library-import", "no safe import module was discovered")
        script = (
            "import importlib; "
            f"m=importlib.import_module({module!r}); "
            "print('DPRAUTO_IMPORT_OK'); "
            "public=[n for n in dir(m) if not n.startswith('_')]; "
            "print('DPRAUTO_API_COUNT='+str(len(public)))"
        )
        timeout_seconds = self._timeout_or_zero(self.config.command_timeout_seconds, context)
        if timeout_seconds <= 0:
            return self._single_error("library-import", "workflow time budget exceeded before library import")
        command = CommandSpec(("python", "-c", script), timeout_seconds=timeout_seconds)
        execution = self.runtime.run_image(
            image, command, timeout_seconds=timeout_seconds
        )
        output = execution.output_excerpt
        import_ok = execution.command_result.succeeded and "DPRAUTO_IMPORT_OK" in output
        match = re.search(r"DPRAUTO_API_COUNT=(\d+)", output)
        api_found = bool(match and int(match.group(1)) > 0)
        tests_passed, test_evidence_strength = self._project_tests_evidence(context)
        tests_unavailable_reason = next(
            (
                str(result.metadata.get("skip_reason", ""))
                for result in context.prior_results
                if result.level is VerificationLevel.TESTABILITY
                and result.status is VerificationStatus.SKIPPED
                and result.metadata.get("skip_reason")
                in {"required-secret-environment", "external-tests-only"}
            ),
            "",
        )
        api_ok = api_found or tests_passed
        api_status = (
            VerificationStatus.PASSED
            if api_ok
            else (
                VerificationStatus.SKIPPED
                if import_ok and tests_unavailable_reason
                else VerificationStatus.FAILED
            )
        )
        if api_ok:
            api_summary = "public API was observed or project tests passed"
        elif tests_unavailable_reason:
            api_summary = (
                "public API evidence was unavailable and project tests were explicitly "
                f"skipped by policy: {tests_unavailable_reason}"
            )
        else:
            api_summary = (
                "import alone is only a smoke test; no public API or passing tests were observed"
            )
        checks = (
            VerificationCheck(
                "library-import",
                VerificationStatus.PASSED if import_ok else VerificationStatus.FAILED,
                f"imported {module}" if import_ok else f"failed to import {module}",
                command_result=execution.command_result,
                evidence=(execution.command_result.stdout,) if execution.command_result.stdout else (),
            ),
            VerificationCheck(
                "library-api-or-tests",
                api_status,
                api_summary,
                metadata={
                    "public_api_found": api_found,
                    "project_tests_passed": tests_passed,
                    "tests_unavailable_reason": tests_unavailable_reason,
                },
            ),
        )
        if import_ok and api_found:
            outcome_category = "python-library-loadable"
            evidence_strength = "strong"
            evidence_scope = "import-public-api"
        elif import_ok and tests_passed:
            outcome_category = "python-library-test-backed"
            evidence_strength = test_evidence_strength
            evidence_scope = "import-project-tests"
        elif import_ok:
            outcome_category = "python-library-import-only"
            evidence_strength = "limited"
            evidence_scope = "import-only"
        else:
            outcome_category = self._command_outcome(
                execution,
                success="python-library-import-failed",
            )
            evidence_strength = "none"
            evidence_scope = "import-public-api"
        return self._result(
            checks,
            command_result=execution.command_result,
            metadata={
                "module": module,
                "runtime_contract": "library-load",
                "runtime_outcome_category": outcome_category,
                "runtime_evidence_strength": evidence_strength,
                "runtime_evidence_scope": evidence_scope,
                "runtime_probe_source": "project-import-metadata",
                "public_api_found": api_found,
                "project_tests_passed": tests_passed,
                "project_test_evidence_strength": test_evidence_strength,
                **self._execution_metadata(execution),
            },
        )

    def _verify_compiled_library(
        self,
        context: VerificationContext,
        image: str,
        probe: str,
        marker: str,
        probe_type: str,
    ) -> VerificationResult:
        timeout_seconds = self._timeout_or_zero(self.config.command_timeout_seconds, context)
        if timeout_seconds <= 0:
            return self._single_error(
                "library-artifact",
                "workflow time budget exceeded before compiled library probe",
            )
        command = CommandSpec(
            (probe,),
            purpose=CommandPurpose.RUN,
            timeout_seconds=timeout_seconds,
            shell=True,
        )
        execution = self.runtime.run_image(image, command, timeout_seconds=timeout_seconds)
        output = execution.output_excerpt
        probe_ok = execution.command_result.succeeded and marker in output
        count_match = re.search(r"DPRAUTO_(?:API|ARTIFACT)_COUNT=(\d+)", output)
        artifact_count = int(count_match.group(1)) if count_match else 0
        tests_passed, test_evidence_strength = self._project_tests_evidence(context)
        observable_ok = artifact_count > 0 or tests_passed
        checks = (
            VerificationCheck(
                "library-artifact",
                VerificationStatus.PASSED if probe_ok else VerificationStatus.FAILED,
                (
                    f"compiled library probe passed: {probe_type}"
                    if probe_ok
                    else f"compiled library probe failed: {probe_type}"
                ),
                command_result=execution.command_result,
                evidence=(
                    (execution.command_result.stdout,)
                    if execution.command_result.stdout
                    else ()
                ),
                metadata={"output_excerpt": output},
            ),
            VerificationCheck(
                "library-artifact-or-tests",
                VerificationStatus.PASSED if observable_ok else VerificationStatus.FAILED,
                (
                    "compiled artifact content was observed or project tests passed"
                    if observable_ok
                    else "no compiled artifact content or passing project tests were observed"
                ),
                metadata={
                    "artifact_count": artifact_count,
                    "project_tests_passed": tests_passed,
                },
            ),
        )
        return self._result(
            checks,
            command_result=execution.command_result,
            metadata={
                "runtime_probe_type": probe_type,
                "artifact_count": artifact_count,
                "project_tests_passed": tests_passed,
                "project_test_evidence_strength": test_evidence_strength,
                "runtime_contract": "compiled-library-availability",
                "runtime_outcome_category": (
                    "compiled-library-test-backed"
                    if probe_ok and observable_ok and tests_passed
                    else (
                        "compiled-artifact-present"
                        if probe_ok and artifact_count > 0
                        else self._command_outcome(
                            execution,
                            success="compiled-artifact-not-observed",
                        )
                    )
                ),
                "runtime_evidence_strength": (
                    test_evidence_strength
                    if probe_ok and observable_ok and tests_passed
                    else ("limited" if probe_ok and artifact_count > 0 else "none")
                ),
                "runtime_evidence_scope": (
                    "artifact-content-and-project-tests"
                    if tests_passed
                    else "artifact-content-only"
                ),
                "runtime_probe_source": (
                    "image-default"
                    if "image-default" in probe_type
                    else "build-plan"
                ),
                **self._execution_metadata(execution),
            },
        )

    @classmethod
    def _compiled_library_probe(
        cls, context: VerificationContext
    ) -> tuple[str, str, str] | None:
        plan = context.build_plan
        if plan is None:
            return None
        probe = plan.metadata.get("runtime_probe_command", "")
        marker = plan.metadata.get("runtime_probe_marker", "")
        probe_type = plan.metadata.get("runtime_probe_type", "")
        expected_markers = cls._compiled_markers(context)
        if (
            not isinstance(probe, str)
            or not isinstance(marker, str)
            or not isinstance(probe_type, str)
            or marker not in expected_markers
            or not probe.strip()
            or len(probe) > 4096
            or "\x00" in probe
        ):
            return None
        return probe, marker, probe_type

    def _image_default_compiled_library_probe(
        self,
        context: VerificationContext,
        image: str,
    ) -> tuple[str, str, str] | None:
        """Recover a generated artifact probe after a repair selects DockerStrategy.

        Structured repairs materialize the deterministic plan as a Dockerfile.  On
        the next portfolio pass it is intentionally selected as a normal Docker
        plan, whose plan metadata no longer contains the original runtime probe.
        The generated probe remains the image CMD, so recover it only when its
        language-specific marker and bounded shell shape are both recognizable.
        """

        markers = self._compiled_markers(context)
        if not markers:
            return None
        inspection = self.runtime.inspect_image(image)
        command = inspection.default_command
        if (
            not inspection.exists
            or len(command) != 3
            or command[0].rsplit("/", 1)[-1] not in {"sh", "bash"}
            or command[1] not in {"-c", "-lc"}
            or not isinstance(command[2], str)
        ):
            return None
        probe = command[2]
        if not probe.strip() or len(probe) > 4096 or "\x00" in probe:
            return None
        marker = next((value for value in markers if value in probe), "")
        if not marker:
            return None
        family = "jvm" if marker == "DPRAUTO_JVM_ARTIFACT_OK" else "native"
        return probe, marker, f"{family}-image-default-artifacts"

    @staticmethod
    def _compiled_markers(context: VerificationContext) -> tuple[str, ...]:
        languages = {value.casefold() for value in context.profile.languages}
        markers: list[str] = []
        if languages & {"java", "kotlin", "groovy"}:
            markers.append("DPRAUTO_JVM_ARTIFACT_OK")
        if languages & {"c", "c++", "cpp"}:
            markers.append("DPRAUTO_NATIVE_BUILD_OK")
        return tuple(markers)

    @staticmethod
    def _is_python_library(context: VerificationContext) -> bool:
        languages = {value.casefold() for value in context.profile.languages}
        managers = {value.casefold() for value in context.profile.package_managers}
        return "python" in languages or bool(managers & {"pip", "poetry", "pipenv"})

    @staticmethod
    def _project_tests_evidence(context: VerificationContext) -> tuple[bool, str]:
        """Accept only successful test evidence, never a zero-test false success."""

        for result in context.prior_results:
            if result.level is not VerificationLevel.TESTABILITY or not result.passed:
                continue
            category = str(result.metadata.get("outcome_category", ""))
            if category and category != "tests-passed":
                continue
            output = "\n".join(
                str(check.metadata.get("output_excerpt", ""))
                for check in result.checks
            )
            if re.search(
                r"(?:no tests ran|collected 0 items|no tests were found)",
                output,
                re.I,
            ):
                continue
            strength = str(result.metadata.get("test_evidence_strength", "moderate"))
            if strength not in {"strong", "moderate", "limited"}:
                strength = "moderate"
            return True, strength
        return False, "none"

    @staticmethod
    def _execution_metadata(execution) -> dict[str, object]:
        command_result = execution.command_result
        return {
            "executed_command": command_result.command.display,
            "exit_code": command_result.exit_code,
            "timed_out": command_result.timed_out,
            "duration_seconds": command_result.duration_seconds,
        }

    @staticmethod
    def _command_outcome(execution, *, success: str) -> str:
        command_result = execution.command_result
        if command_result.timed_out:
            return "runtime-command-timeout"
        if not command_result.succeeded:
            return "runtime-command-failure"
        return success

    def _result(self, checks, *, command_result=None, metadata=None) -> VerificationResult:
        checks = tuple(checks)
        status = aggregate_status(checks)
        evidence = tuple(artifact for check in checks for artifact in check.evidence)
        result_metadata = dict(metadata or {})
        strength = str(result_metadata.get("runtime_evidence_strength", "none"))
        result_metadata["runtime_semantically_proven"] = bool(
            status is VerificationStatus.PASSED
            and strength in {"strong", "moderate"}
        )
        return VerificationResult(
            verification_id(self.level),
            self.level,
            status,
            command_result=command_result,
            evidence=evidence,
            summary=f"runnability {status.value}",
            metadata=result_metadata,
            checks=checks,
        )

    def _single_error(self, name: str, summary: str) -> VerificationResult:
        check = VerificationCheck(name, VerificationStatus.ERROR, summary)
        return VerificationResult(
            verification_id(self.level), self.level, check.status, summary=summary, checks=(check,)
        )

    def _timeout_or_zero(self, requested_seconds: int, context: VerificationContext) -> int:
        return clamped_timeout_seconds(requested_seconds, context.deadline_at)

    @staticmethod
    def _infer_web_port(command: str) -> int:
        match = re.search(r"(?:--port(?:=|\s+)|:)(\d{2,5})\b", command)
        if match:
            return int(match.group(1))
        if "flask" in command.lower():
            return 5000
        return 8000
