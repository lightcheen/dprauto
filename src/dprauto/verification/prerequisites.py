"""Construct a minimal, deterministic environment for a selected test command."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from dprauto.config import VerificationConfig
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile
from dprauto.ports.runtime import ServiceSpec, TestEnvironmentSpec

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_EXECUTABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_SAFE_SETUP = re.compile(
    r"(?:python\S*\s+)?(?:manage\.py\s+(?:migrate|loaddata|check)|"
    r"[^\s/]+/manage\.py\s+(?:migrate|loaddata|check))\b",
    re.IGNORECASE,
)


class TestEnvironmentPlanner:
    """Resolve only prerequisites explicitly tied to the chosen test command."""

    __test__ = False

    def __init__(self, config: VerificationConfig | None = None) -> None:
        self.config = config or VerificationConfig()

    def plan(
        self,
        profile: ProjectProfile,
        selected: ProjectCommand,
    ) -> TestEnvironmentSpec:
        service_names = self._service_names(profile, selected.command.display)
        services = tuple(self._service(name) for name in service_names)
        service_environment = self._command_environment(profile)
        if "postgresql" in service_names:
            for key, value in {
                "PGUSER": "postgres",
                "PGDATABASE": "postgres",
                "PG_DATABASE": "postgres",
                "PGPASSWORD": "postgres",
                "PG_PASSWORD": "postgres",
            }.items():
                service_environment.setdefault(key, value)
            # CI values commonly say localhost because GitHub publishes a
            # service port onto its job VM.  Our test runs in another
            # container, so routing keys must use the isolated network alias.
            service_environment.update(
                {"PGHOST": "postgres", "PG_HOST": "postgres", "PGPORT": "5432"}
            )
        if "redis" in service_names:
            service_environment.update(
                {
                    "REDIS_HOST": "redis",
                    "REDIS_PORT": "6379",
                    "REDIS_URL": "redis://redis:6379/0",
                }
            )
        required_executables = tuple(
            value
            for value in self._string_tuple(profile.metadata.get("test_required_executables", ()))
            if _EXECUTABLE_NAME.fullmatch(value)
        )
        return TestEnvironmentSpec(
            services=services,
            command_environment=service_environment,
            setup_commands=self._setup_commands(profile),
            required_executables=required_executables,
            evidence=self._evidence(profile, service_names),
        )

    def _service(self, name: str) -> ServiceSpec:
        if name == "postgresql":
            return ServiceSpec(
                kind=name,
                image_reference=self.config.postgres_service_image,
                alias="postgres",
                environment={
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD": "postgres",
                    "POSTGRES_DB": "postgres",
                },
                healthcheck=("pg_isready", "-U", "postgres", "-d", "postgres"),
            )
        if name == "redis":
            return ServiceSpec(
                kind=name,
                image_reference=self.config.redis_service_image,
                alias="redis",
                healthcheck=("redis-cli", "ping"),
            )
        raise ValueError(f"unsupported verification service: {name}")

    @classmethod
    def _service_names(cls, profile: ProjectProfile, command: str) -> tuple[str, ...]:
        requested = list(cls._string_tuple(profile.metadata.get("test_service_requirements", ())))
        by_command = profile.metadata.get("test_service_requirements_by_command", {})
        if isinstance(by_command, Mapping):
            values = by_command.get(command, ())
            requested.extend(cls._string_tuple(values))
        aliases = {"postgres": "postgresql", "postgresql": "postgresql", "redis": "redis"}
        normalized = [
            aliases[value.casefold()] for value in requested if value.casefold() in aliases
        ]
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _command_environment(profile: ProjectProfile) -> dict[str, str]:
        values = profile.metadata.get("test_environment_variables", {})
        if not isinstance(values, Mapping):
            return {}
        return {
            key: value
            for key, value in tuple(values.items())[:32]
            if isinstance(key, str)
            and isinstance(value, str)
            and _ENVIRONMENT_NAME.fullmatch(key)
            and len(value) <= 512
            and "\x00" not in value
        }

    @staticmethod
    def _setup_commands(profile: ProjectProfile) -> tuple[CommandSpec, ...]:
        values = profile.metadata.get("test_setup_commands", ())
        if not isinstance(values, (list, tuple)):
            return ()
        commands: list[CommandSpec] = []
        for value in values[:8]:
            if not isinstance(value, str) or not _SAFE_SETUP.search(value):
                continue
            try:
                argv = tuple(shlex.split(value))
            except ValueError:
                continue
            if argv and not any(token in value for token in (";", "&&", "||", "|", "`", "$")):
                commands.append(CommandSpec(argv, purpose=CommandPurpose.OTHER))
        return tuple(commands)

    @staticmethod
    def _evidence(profile: ProjectProfile, services: tuple[str, ...]) -> tuple[str, ...]:
        evidence = TestEnvironmentPlanner._string_tuple(
            profile.metadata.get("test_prerequisite_evidence", ())
        )
        if services:
            return tuple(dict.fromkeys((*evidence, *(f"service:{name}" for name in services))))
        return evidence

    @staticmethod
    def _string_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value[:32] if isinstance(item, str) and item)
