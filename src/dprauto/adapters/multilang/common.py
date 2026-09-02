"""Common evidence helpers for deterministic JVM and native parsers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable

from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand
from dprauto.inspection.commands import CommandExtractor, ExtractedCommand
from dprauto.domain.workspace import RepositoryScan


CI_ROOT_FILES = {
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".travis.yml",
    "azure-pipelines.yml",
    "jenkinsfile",
}


def depth(path: str) -> int:
    return len(PurePosixPath(path).parts)


def readme_files(scanned: RepositoryScan) -> tuple[str, ...]:
    return tuple(
        path
        for path in scanned.files
        if depth(path) <= 2
        and PurePosixPath(path).name.casefold().startswith(
            ("readme", "contributing", "building")
        )
        and PurePosixPath(path).suffix.casefold() in {"", ".md", ".rst", ".txt", ".adoc"}
    )


def ci_files(scanned: RepositoryScan) -> tuple[str, ...]:
    selected = []
    for path in scanned.files:
        normalized = PurePosixPath(path)
        name = normalized.name.casefold()
        if name in CI_ROOT_FILES or (
            len(normalized.parts) >= 3
            and normalized.parts[0] in {".github", ".gitea"}
            and normalized.parts[1] == "workflows"
            and normalized.suffix.casefold() in {".yml", ".yaml"}
        ):
            selected.append(path)
    return tuple(selected)


def dockerfiles(scanned: RepositoryScan) -> tuple[str, ...]:
    return tuple(
        path
        for path in scanned.files
        if depth(path) <= 3
        and (
            PurePosixPath(path).name.casefold() == "dockerfile"
            or PurePosixPath(path).name.casefold().startswith("dockerfile.")
        )
    )


def extracted_commands(
    scanned: RepositoryScan,
    readmes: tuple[str, ...],
    workflows: tuple[str, ...],
    extractor: CommandExtractor,
) -> tuple[ExtractedCommand, ...]:
    commands: list[ExtractedCommand] = []
    for path in readmes:
        if depth(path) <= 2:
            commands.extend(extractor.extract_markdown(path, scanned.read_text(path)))
    for path in workflows:
        commands.extend(extractor.extract_ci(path, scanned.read_text(path)))
    return tuple(commands)


COMMAND_LIMITS = {
    CommandPurpose.INSTALL: 12,
    CommandPurpose.BUILD: 12,
    CommandPurpose.TEST: 12,
    CommandPurpose.RUN: 8,
    CommandPurpose.OTHER: 8,
}


def project_commands(commands: Iterable[ExtractedCommand]) -> tuple[ProjectCommand, ...]:
    """Deduplicate and bound the command search space by semantic purpose."""

    unique: dict[tuple[str, CommandPurpose], ExtractedCommand] = {}
    for command in commands:
        key = (re.sub(r"\s+", " ", command.text.strip()).casefold(), command.purpose)
        current = unique.get(key)
        if current is None or command.confidence > current.confidence:
            unique[key] = command

    ordered = list(unique.values())
    positions = {id(command): index for index, command in enumerate(ordered)}
    ranked = sorted(
        ordered,
        key=lambda command: (
            command.purpose.value,
            not command.source.startswith("inferred:"),
            (
                positions[id(command)]
                if command.source.startswith("inferred:")
                else -command.confidence
            ),
            len(command.text),
            command.text.casefold(),
        ),
    )
    counters: dict[CommandPurpose, int] = {}
    result: list[ProjectCommand] = []
    for command in ranked:
        limit = COMMAND_LIMITS.get(command.purpose, 8)
        if counters.get(command.purpose, 0) >= limit:
            continue
        counters[command.purpose] = counters.get(command.purpose, 0) + 1
        result.append(
            ProjectCommand(
                name=f"{command.purpose.value}-{counters[command.purpose]}",
                command=CommandSpec(
                    argv=(command.text,),
                    purpose=command.purpose,
                    shell=True,
                ),
                source=command.source,
                confidence=command.confidence,
            )
        )
    return tuple(result)


def safe_subproject(value: str) -> str:
    normalized = value.strip().strip("'\"").replace(":", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or not re.fullmatch(r"[A-Za-z0-9_.+/-]+", normalized)
        or path.is_absolute()
        or ".." in path.parts
    ):
        return ""
    return path.as_posix()
