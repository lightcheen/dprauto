"""Role-aware command selection shared by build plans and verification."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile

GRADLE_PROXY_EXECUTABLE = "/usr/local/bin/dprauto-gradle-proxy"


_PYTHON_SMOKE_PATTERN = re.compile(
    r"(?i)^python\d*\s+-c\s+.*\bimport\b|"
    r"(?i)^python\d*\s+-m\s+compileall\b",
)
_HELP_OR_VERSION_ONLY_PATTERN = re.compile(
    r"(?i)^(?:python\S*\s+-m\s+)?[^\s]+\s+(?:--help|-h|--version|-V)$",
)
_WEB_SERVER_PATTERN = re.compile(
    r"\b(?:uvicorn|hypercorn|gunicorn|daphne)\b|"
    r"\bflask\s+run\b|"
    r"\bmanage\.py\s+runserver\b|"
    r"\bstreamlit\s+run\b|"
    r"\bdocker(?:-|\s+)compose\s+up\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:serve|start|dev)\b|"
    r"\b(?:mvnw?|gradlew?)\b.*\b(?:spring-boot:run|bootRun)\b|"
    r"\bjava\s+-jar\b",
    re.IGNORECASE,
)


def is_smoke_command(command: ProjectCommand) -> bool:
    display = command.command.display.strip()
    return bool(
        _PYTHON_SMOKE_PATTERN.search(display)
        or _HELP_OR_VERSION_ONLY_PATTERN.fullmatch(display)
    )


def is_web_server_command(command: str) -> bool:
    """Require positive evidence that a web command starts a persistent server."""

    return bool(_WEB_SERVER_PATTERN.search(command))


def _normalize_web_command(
    profile: ProjectProfile,
    command: ProjectCommand,
) -> ProjectCommand:
    display = command.command.display.strip()
    normalized = display
    if re.search(r"\bmanage\.py\s+runserver\s*$", display, re.IGNORECASE):
        normalized = f"{display} 0.0.0.0:8000"
    elif re.search(r"\bflask\s+run\b", display, re.IGNORECASE) and not re.search(
        r"(?:^|\s)--host(?:=|\s)", display, re.IGNORECASE
    ):
        normalized = f"{display} --host 0.0.0.0"
    elif re.search(r"\b(?:uvicorn|hypercorn)\b", display, re.IGNORECASE) and not re.search(
        r"(?:^|\s)--host(?:=|\s)", display, re.IGNORECASE
    ):
        normalized = f"{display} --host 0.0.0.0"
    if re.search(r"\bmanage\.py\s+runserver\b", normalized, re.IGNORECASE):
        migration = next(
            (
                item.command.display.strip()
                for item in profile.commands
                if item.command.purpose in {CommandPurpose.INSTALL, CommandPurpose.OTHER}
                and re.fullmatch(
                    r"(?:python\S*\s+)?manage\.py\s+migrate(?:\s+--noinput)?",
                    item.command.display.strip(),
                    re.IGNORECASE,
                )
            ),
            "",
        )
        if migration:
            if "--noinput" not in migration.casefold():
                migration += " --noinput"
            normalized = f"{migration} && {normalized}"
    if normalized == display:
        return command
    spec = CommandSpec(
        (normalized,),
        purpose=command.command.purpose,
        cwd=command.command.cwd,
        environment=command.command.environment,
        timeout_seconds=command.command.timeout_seconds,
        shell=True,
    )
    return ProjectCommand(
        command.name,
        spec,
        f"role-normalized:{command.source}",
        command.confidence,
    )


def select_run_command(profile: ProjectProfile) -> ProjectCommand | None:
    """Select only a command whose behavior matches the normalized project type."""

    if profile.project_type is ProjectType.LIBRARY:
        return None
    candidates = [
        item
        for item in profile.commands
        if item.command.purpose is CommandPurpose.RUN and not is_smoke_command(item)
        and not _unavailable_runtime_wrapper(profile, item.command.display)
    ]
    if profile.project_type is ProjectType.WEB:
        candidates = [
            item for item in candidates if is_web_server_command(item.command.display)
        ]
    selected = max(candidates, key=lambda item: item.confidence, default=None)
    if selected is not None and profile.project_type is ProjectType.WEB:
        return _normalize_web_command(profile, selected)
    return selected


def _unavailable_runtime_wrapper(profile: ProjectProfile, command: str) -> bool:
    """Reject host/development launchers not present in the built runtime contract."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    if not tokens:
        return True
    executable = PurePosixPath(tokens[0]).name.casefold()
    if executable in {"docker", "docker-compose"}:
        return True
    wrappers = {"pipenv", "poetry", "uv", "pdm", "hatch"}
    return bool(executable in wrappers and executable not in profile.package_managers)
