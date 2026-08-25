"""Container runtime operations needed by verification."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dprauto.domain.models import CommandResult, CommandSpec


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """One bounded service container required by a verification command."""

    kind: str
    image_reference: str
    alias: str
    environment: Mapping[str, str] = field(default_factory=dict)
    healthcheck: tuple[str, ...] = ()
    initialization_commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class TestEnvironmentSpec:
    """Auditable prerequisites for one selected project test command."""

    __test__ = False

    services: tuple[ServiceSpec, ...] = ()
    command_environment: Mapping[str, str] = field(default_factory=dict)
    setup_commands: tuple[CommandSpec, ...] = ()
    required_executables: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageInspection:
    exists: bool
    image_id: str = ""
    working_directory: str = ""
    default_command: tuple[str, ...] = ()
    exposed_ports: tuple[int, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContainerExecution:
    command_result: CommandResult
    output_excerpt: str = ""
    filesystem_changes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebProbe:
    process_running: bool
    port_open: bool
    http_reachable: bool
    container_port: int
    host_port: int | None = None
    http_status: int | None = None
    output_excerpt: str = ""


@runtime_checkable
class ContainerRuntime(Protocol):
    def inspect_image(self, image_reference: str) -> ImageInspection: ...

    def run_image(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        timeout_seconds: int,
    ) -> ContainerExecution: ...

    def run_environment(
        self,
        image_reference: str,
        command: CommandSpec,
        environment: TestEnvironmentSpec,
        *,
        timeout_seconds: int,
    ) -> ContainerExecution: ...

    def probe_web(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        container_port: int,
        timeout_seconds: int,
        path: str = "/",
    ) -> WebProbe: ...
