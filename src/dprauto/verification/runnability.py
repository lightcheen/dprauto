"""Runtime policies specialized by normalized ProjectProfile type."""

from __future__ import annotations

import re

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
        return self._result(checks, metadata={"container_port": port, "host_port": probe.host_port})

    def _verify_cli(self, context: VerificationContext, image: str) -> VerificationResult:
        selected = select_run_command(context.profile)
        timeout_seconds = self._timeout_or_zero(self.config.command_timeout_seconds, context)
        if timeout_seconds <= 0:
            return self._single_error("cli-runtime", "workflow time budget exceeded before CLI probe")
        execution = self.runtime.run_image(
            image,
            selected.command if selected else None,
            timeout_seconds=timeout_seconds,
        )
        fallback_used = False
        fallback_reason = ""
        if (
            selected is not None
            and not self._has_help_or_version_argument(selected.command)
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
                    self._help_command(selected.command),
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
            },
        )

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
        return self._result(checks, command_result=execution.command_result)

    def _verify_library(self, context: VerificationContext, image: str) -> VerificationResult:
        compiled_probe = self._compiled_library_probe(context)
        if compiled_probe is not None:
            return self._verify_compiled_library(context, image, *compiled_probe)
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
        tests_passed = any(
            result.level is VerificationLevel.TESTABILITY and result.passed
            for result in context.prior_results
        )
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
        return self._result(checks, command_result=execution.command_result, metadata={"module": module})

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
        tests_passed = any(
            result.level is VerificationLevel.TESTABILITY and result.passed
            for result in context.prior_results
        )
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
            },
        )

    @staticmethod
    def _compiled_library_probe(
        context: VerificationContext,
    ) -> tuple[str, str, str] | None:
        plan = context.build_plan
        if plan is None:
            return None
        expected_markers = {
            "jvm-template": "DPRAUTO_JVM_ARTIFACT_OK",
            "native-template": "DPRAUTO_NATIVE_BUILD_OK",
        }
        expected = expected_markers.get(plan.strategy)
        if expected is None:
            return None
        probe = plan.metadata.get("runtime_probe_command", "")
        marker = plan.metadata.get("runtime_probe_marker", "")
        probe_type = plan.metadata.get("runtime_probe_type", "")
        if (
            not isinstance(probe, str)
            or not isinstance(marker, str)
            or not isinstance(probe_type, str)
            or marker != expected
            or not probe.strip()
            or len(probe) > 4096
            or "\x00" in probe
        ):
            return None
        return probe, marker, probe_type

    def _result(self, checks, *, command_result=None, metadata=None) -> VerificationResult:
        checks = tuple(checks)
        status = aggregate_status(checks)
        evidence = tuple(artifact for check in checks for artifact in check.evidence)
        return VerificationResult(
            verification_id(self.level),
            self.level,
            status,
            command_result=command_result,
            evidence=evidence,
            summary=f"runnability {status.value}",
            metadata=metadata or {},
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
