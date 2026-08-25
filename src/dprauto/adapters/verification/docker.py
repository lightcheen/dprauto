"""Docker CLI adapter used by the verification core."""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
from pathlib import PurePosixPath

from dprauto.config import BuildConfig, VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandResult, CommandSpec
from dprauto.ports.runtime import (
    ContainerExecution,
    ImageInspection,
    TestEnvironmentSpec,
    WebProbe,
)
from dprauto.ports.storage import Storage
from dprauto.proxy import docker_proxy_environment_arguments


class DockerContainerRuntime:
    """Keep Docker lifecycle and HTTP probing outside verification policies."""

    def __init__(
        self,
        storage: Storage,
        build_config: BuildConfig | None = None,
        verification_config: VerificationConfig | None = None,
    ) -> None:
        self.storage = storage
        self.build_config = build_config or BuildConfig()
        self.verification_config = verification_config or VerificationConfig()

    def inspect_image(self, image_reference: str) -> ImageInspection:
        completed = subprocess.run(
            [self.build_config.docker_binary, "image", "inspect", image_reference],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if completed.returncode != 0:
            return ImageInspection(False, metadata={"error": self._excerpt(completed.stdout)})
        try:
            payload = json.loads(completed.stdout)[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            return ImageInspection(False, metadata={"error": "invalid docker inspect output"})
        config = payload.get("Config") or {}
        command = tuple(config.get("Entrypoint") or ()) + tuple(config.get("Cmd") or ())
        ports: list[int] = []
        for value in config.get("ExposedPorts") or {}:
            try:
                ports.append(int(str(value).split("/", 1)[0]))
            except ValueError:
                continue
        return ImageInspection(
            exists=True,
            image_id=str(payload.get("Id") or ""),
            working_directory=str(config.get("WorkingDir") or ""),
            default_command=command,
            exposed_ports=tuple(sorted(set(ports))),
            metadata={"architecture": payload.get("Architecture", "")},
        )

    def run_image(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        timeout_seconds: int,
    ) -> ContainerExecution:
        return self._run_image_on_network(
            image_reference,
            command,
            timeout_seconds=timeout_seconds,
            network=self.verification_config.docker_network,
        )

    def _run_image_on_network(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        timeout_seconds: int,
        network: str,
    ) -> ContainerExecution:
        name = f"dprauto-verify-{uuid.uuid4().hex[:12]}"
        requested = command or CommandSpec(
            ("<image-default-command>",),
            purpose=CommandPurpose.RUN,
            timeout_seconds=timeout_seconds,
        )
        create = [self.build_config.docker_binary, "create", "--name", name]
        if network:
            create.extend(("--network", network))
        create.extend(docker_proxy_environment_arguments(self.build_config))
        if self.build_config.use_cache:
            create.extend(
                (
                    "--mount",
                    "type=volume,source=dprauto-python-package-cache,target=/root/.cache",
                )
            )
        if command is not None:
            for key, value in command.environment.items():
                create.extend(("--env", f"{key}={value}"))
            if command.cwd:
                create.extend(("--workdir", self._container_workdir(command.cwd)))
            # Docker treats arguments after the image as arguments to the
            # image ENTRYPOINT.  Override it so an explicit verification
            # command is executed by the shell instead of being appended to
            # an unrelated project entrypoint.
            create.extend(("--entrypoint", "/bin/sh"))
        create.append(image_reference)
        if command is not None:
            create.extend(("-lc", command.display))
        created = subprocess.run(
            create, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
        )
        started = time.monotonic()
        output = created.stdout or b""
        timed_out = False
        exit_code: int | None = created.returncode if created.returncode else None
        changes: tuple[str, ...] = ()
        try:
            if created.returncode == 0:
                try:
                    run = subprocess.run(
                        [self.build_config.docker_binary, "start", "--attach", name],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=timeout_seconds,
                        check=False,
                    )
                    output = run.stdout or b""
                    exit_code = self._container_exit_code(name, run.returncode)
                except subprocess.TimeoutExpired as exc:
                    output = (exc.stdout or b"") + b"\nverification command timed out\n"
                    timed_out = True
                    exit_code = None
                    subprocess.run(
                        [self.build_config.docker_binary, "stop", "--time", "1", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                diff = subprocess.run(
                    [self.build_config.docker_binary, "diff", name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if diff.returncode == 0:
                    changes = tuple(
                        line.decode("utf-8", "replace").strip()
                        for line in diff.stdout.splitlines()
                        if line.strip()
                    )
        finally:
            subprocess.run(
                [self.build_config.docker_binary, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        artifact = self.storage.save(
            f"verification-logs/{uuid.uuid4().hex}.log",
            output,
            media_type="text/plain; charset=utf-8",
        )
        result = CommandResult(
            requested,
            exit_code,
            stdout=artifact,
            timed_out=timed_out,
            duration_seconds=time.monotonic() - started,
        )
        return ContainerExecution(result, self._excerpt(output), changes)

    def run_environment(
        self,
        image_reference: str,
        command: CommandSpec,
        environment: TestEnvironmentSpec,
        *,
        timeout_seconds: int,
    ) -> ContainerExecution:
        """Run a command with short-lived, health-checked service containers."""

        if not environment.services:
            prepared = self._prepare_environment_command(command, environment)
            return self.run_image(
                image_reference,
                prepared,
                timeout_seconds=timeout_seconds,
            )
        if not self.verification_config.service_orchestration_enabled:
            return self._environment_failure(
                command,
                "verification service orchestration is disabled",
                metadata={"failure_stage": "policy"},
            )
        if len(environment.services) > self.verification_config.max_service_containers:
            return self._environment_failure(
                command,
                "verification service count exceeds the configured bound",
                metadata={"failure_stage": "policy"},
            )

        token = uuid.uuid4().hex[:12]
        network = f"dprauto-test-{token}"
        deadline = time.monotonic() + timeout_seconds
        service_names: list[str] = []
        evidence: list[str] = []
        network_created = False
        try:
            created_network = subprocess.run(
                [
                    self.build_config.docker_binary,
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    network,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if created_network.returncode != 0:
                return self._environment_failure(
                    command,
                    created_network.stdout or b"failed to create verification network",
                    metadata={"failure_stage": "network-create"},
                )
            network_created = True
            for index, service in enumerate(environment.services):
                service_name = f"dprauto-service-{token}-{index}"
                service_names.append(service_name)
                create = [
                    self.build_config.docker_binary,
                    "create",
                    "--name",
                    service_name,
                    "--network",
                    network,
                    "--network-alias",
                    service.alias,
                ]
                for key, value in service.environment.items():
                    create.extend(("--env", f"{key}={value}"))
                create.append(service.image_reference)
                created = subprocess.run(
                    create,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if created.returncode != 0:
                    return self._environment_failure(
                        command,
                        created.stdout or b"failed to create verification service",
                        metadata={
                            "failure_stage": "service-create",
                            "service_kind": service.kind,
                        },
                    )
                started = subprocess.run(
                    [self.build_config.docker_binary, "start", service_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if started.returncode != 0:
                    return self._environment_failure(
                        command,
                        started.stdout or b"failed to start verification service",
                        metadata={
                            "failure_stage": "service-start",
                            "service_kind": service.kind,
                        },
                    )
                health_budget = int(deadline - time.monotonic())
                if health_budget <= 0:
                    return self._environment_failure(
                        command,
                        "verification time budget exceeded before service healthcheck",
                        metadata={
                            "failure_stage": "environment-timeout",
                            "service_kind": service.kind,
                        },
                    )
                ready, health_output = self._wait_for_service(
                    service_name,
                    service.healthcheck,
                    timeout_seconds=min(
                        health_budget,
                        self.verification_config.service_startup_timeout_seconds,
                    ),
                )
                evidence.append(health_output)
                if not ready:
                    logs = subprocess.run(
                        [self.build_config.docker_binary, "logs", service_name],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    return self._environment_failure(
                        command,
                        (logs.stdout or b"") + health_output.encode("utf-8", "replace"),
                        metadata={
                            "failure_stage": "service-healthcheck",
                            "service_kind": service.kind,
                        },
                    )
                for initialization in service.initialization_commands:
                    initialized = subprocess.run(
                        [self.build_config.docker_binary, "exec", service_name, *initialization],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    evidence.append(self._excerpt(initialized.stdout or b""))
                    if initialized.returncode != 0:
                        return self._environment_failure(
                            command,
                            initialized.stdout or b"service initialization failed",
                            metadata={
                                "failure_stage": "service-initialization",
                                "service_kind": service.kind,
                            },
                        )

            remaining_seconds = int(deadline - time.monotonic())
            if remaining_seconds <= 0:
                return self._environment_failure(
                    command,
                    "verification time budget exceeded during service startup",
                    metadata={"failure_stage": "environment-timeout"},
                )
            execution = self._run_image_on_network(
                image_reference,
                self._prepare_environment_command(command, environment),
                timeout_seconds=remaining_seconds,
                network=network,
            )
            return replace(
                execution,
                metadata={
                    "isolated_network": True,
                    "service_kinds": tuple(service.kind for service in environment.services),
                    "service_images": tuple(
                        service.image_reference for service in environment.services
                    ),
                    "healthcheck_evidence": tuple(item for item in evidence if item),
                },
            )
        finally:
            for service_name in reversed(service_names):
                subprocess.run(
                    [self.build_config.docker_binary, "rm", "--force", service_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if network_created:
                subprocess.run(
                    [self.build_config.docker_binary, "network", "rm", network],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    @staticmethod
    def _prepare_environment_command(
        command: CommandSpec,
        environment: TestEnvironmentSpec,
    ) -> CommandSpec:
        command_environment = dict(command.environment)
        command_environment.update(environment.command_environment)
        executable_checks = tuple(
            f"command -v {shlex.quote(item)} >/dev/null 2>&1"
            for item in environment.required_executables
        )
        if not environment.setup_commands and not executable_checks:
            return replace(command, environment=command_environment)
        display = " && ".join(
            (
                *executable_checks,
                *[item.display for item in environment.setup_commands],
                command.display,
            )
        )
        return replace(
            command,
            argv=(display,),
            environment=command_environment,
            shell=True,
        )

    def _wait_for_service(
        self,
        name: str,
        healthcheck: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> tuple[bool, str]:
        if not healthcheck:
            return self._is_running(name), "service has no explicit healthcheck"
        deadline = time.monotonic() + timeout_seconds
        last_output = b""
        while time.monotonic() < deadline:
            checked = subprocess.run(
                [self.build_config.docker_binary, "exec", name, *healthcheck],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            last_output = checked.stdout or b""
            if checked.returncode == 0:
                return True, self._excerpt(last_output)
            if not self._is_running(name):
                break
            time.sleep(0.2)
        return False, self._excerpt(last_output)

    def _environment_failure(
        self,
        command: CommandSpec,
        output: bytes | str,
        *,
        metadata: dict[str, object],
    ) -> ContainerExecution:
        payload = output.encode("utf-8", "replace") if isinstance(output, str) else output
        artifact = self.storage.save(
            f"verification-logs/{uuid.uuid4().hex}.log",
            payload,
            media_type="text/plain; charset=utf-8",
        )
        result = CommandResult(command, 125, stdout=artifact)
        return ContainerExecution(
            result,
            self._excerpt(payload),
            metadata=metadata,
        )

    def probe_web(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        container_port: int,
        timeout_seconds: int,
        path: str = "/",
    ) -> WebProbe:
        name = f"dprauto-web-{uuid.uuid4().hex[:12]}"
        host_port = self._free_port()
        create = [
            self.build_config.docker_binary,
            "create",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{host_port}:{container_port}",
        ]
        if self.verification_config.docker_network:
            create.extend(("--network", self.verification_config.docker_network))
        create.extend(docker_proxy_environment_arguments(self.build_config))
        if command is not None:
            for key, value in command.environment.items():
                create.extend(("--env", f"{key}={value}"))
            if command.cwd:
                create.extend(("--workdir", self._container_workdir(command.cwd)))
            create.extend(("--entrypoint", "/bin/sh"))
        create.append(image_reference)
        if command is not None:
            create.extend(("-lc", command.display))
        created = subprocess.run(
            create, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
        )
        output = created.stdout or b""
        process_running = False
        port_open = False
        http_reachable = False
        http_status: int | None = None
        try:
            if created.returncode != 0:
                return WebProbe(
                    False,
                    False,
                    False,
                    container_port,
                    host_port,
                    output_excerpt=self._excerpt(output),
                )
            started = subprocess.run(
                [self.build_config.docker_binary, "start", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            output += started.stdout or b""
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                process_running = self._is_running(name)
                if not process_running:
                    break
                port_open = self._port_open(host_port)
                if port_open:
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{host_port}{path}", timeout=1
                        ) as response:
                            http_status = response.status
                            http_reachable = True
                            break
                    except urllib.error.HTTPError as exc:
                        http_status = exc.code
                        http_reachable = True
                        break
                    except (urllib.error.URLError, TimeoutError):
                        pass
                time.sleep(0.2)
            logs = subprocess.run(
                [self.build_config.docker_binary, "logs", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            output += logs.stdout or b""
            return WebProbe(
                process_running,
                port_open,
                http_reachable,
                container_port,
                host_port,
                http_status,
                self._excerpt(output),
            )
        finally:
            subprocess.run(
                [self.build_config.docker_binary, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    def _container_exit_code(self, name: str, fallback: int) -> int:
        inspected = subprocess.run(
            [
                self.build_config.docker_binary,
                "inspect",
                "--format",
                "{{.State.ExitCode}}",
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        try:
            return int(inspected.stdout.strip()) if inspected.returncode == 0 else fallback
        except ValueError:
            return fallback

    @staticmethod
    def _container_workdir(relative: str) -> str:
        path = PurePosixPath(relative)
        return "/workspace" if path.as_posix() == "." else f"/workspace/{path.as_posix()}"

    def _is_running(self, name: str) -> bool:
        completed = subprocess.run(
            [self.build_config.docker_binary, "inspect", "--format", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as stream:
            stream.bind(("127.0.0.1", 0))
            return int(stream.getsockname()[1])

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def _excerpt(self, output: bytes) -> str:
        limit = self.verification_config.output_excerpt_characters
        return output.decode("utf-8", "replace")[-limit:]
