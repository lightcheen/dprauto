"""Role-aware command selection shared by build plans and verification."""

from __future__ import annotations

import re

from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile


_SMOKE_PATTERN = re.compile(
    r"(?:^|\s)(?:(?i:--help|-h|--version)|-V)(?:\s|$)|"
    r"(?i:python\d*\s+-c\s+.*\bimport\b)|"
    r"(?i:python\d*\s+-m\s+compileall\b)",
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
    return bool(_SMOKE_PATTERN.search(command.command.display))


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
    ]
    if profile.project_type is ProjectType.WEB:
        candidates = [
            item for item in candidates if is_web_server_command(item.command.display)
        ]
    selected = max(candidates, key=lambda item: item.confidence, default=None)
    if selected is not None and profile.project_type is ProjectType.WEB:
        return _normalize_web_command(profile, selected)
    return selected
