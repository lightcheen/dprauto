"""Container runtime operations needed by verification."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from dprauto.domain.models import CommandResult, CommandSpec


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
    def inspect_image(self, image_reference: str) -> ImageInspection:
        ...

    def run_image(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        timeout_seconds: int,
    ) -> ContainerExecution:
        ...

    def probe_web(
        self,
        image_reference: str,
        command: CommandSpec | None,
        *,
        container_port: int,
        timeout_seconds: int,
        path: str = "/",
    ) -> WebProbe:
        ...
