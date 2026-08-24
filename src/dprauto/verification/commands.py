"""Select project-owned test and run commands from normalized profile evidence."""

from __future__ import annotations

import re
from typing import Any, Mapping

from dprauto.adapters.python.test_matrix import matrix_entry, preferred_matrix_name
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile


_SMOKE_PATTERN = re.compile(
    r"(?:^|\s)(?:(?i:--help|-h|--version)|-V)(?:\s|$)|"
    r"(?i:python\d*\s+-c\s+.*\bimport\b)|"
    r"(?i:python\d*\s+-m\s+compileall\b)",
)


def is_smoke_command(command: ProjectCommand) -> bool:
    return bool(_SMOKE_PATTERN.search(command.command.display))


class TestCommandSelector:
    """Select one bounded, local project test command."""

    def select(
        self,
        profile: ProjectProfile,
        *,
        python_version: str = "",
    ) -> ProjectCommand | None:
        candidates = [
            item
            for item in profile.commands
            if item.command.purpose is CommandPurpose.TEST and not is_smoke_command(item)
            and not self._has_unresolved_variables(item.command.display)
            and not self._unsafe_test_candidate(profile, item)
        ]
        if not candidates:
            return None
        selected = min(
            candidates,
            key=lambda item: (
                self._command_rank(item.command.display),
                self._runtime_rank(item.command.display, python_version),
                self._source_rank(profile, item),
                -item.confidence,
            ),
        )
        return self._bounded_matrix_command(profile, selected, python_version)

    @staticmethod
    def _has_unresolved_variables(command: str) -> bool:
        return bool(re.search(r"\$\{\{|\$\{?[A-Za-z_][A-Za-z0-9_.-]*\}?", command))

    @staticmethod
    def _command_rank(command: str) -> int:
        text = command.lower()
        if re.search(r"\b(pytest|py\.test)\b", text):
            return 0
        if re.search(r"\bpython\S*\s+[^ ]*(?:u?tests?)/[^ ]+\.py\b", text):
            return 0
        if re.search(r"\bunittest\b", text):
            return 0
        if re.search(r"\b(tox|nox)\b", text):
            if re.search(r"\b(docs?|lint|format|typecheck|mypy|ruff)\b", text):
                return 3
            return 1
        if re.search(r"\b(integration|postgres|mysql|cockroach|redis)\b", text):
            return 3
        if re.search(r"\b(ruff|mypy|format|typecheck|docs?)\b", text):
            return 4
        return 2

    @classmethod
    def _unsafe_test_candidate(
        cls,
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> bool:
        display = command.command.display
        source = command.source.casefold()
        if re.search(
            r"(?:^|[/_.-])(docs?|lint|format|fuzz|benchmarks?|integration|e2e|"
            r"performance|slow|remote|release)(?:[/_.-]|$)",
            source,
        ):
            return True
        if re.search(
            r"--(?:remote-data|benchmark|run-slow)\b|"
            r"(?:^|\s)(?:tests?/)?(?:integration|e2e|benchmarks?|slow)(?:/|\s|$)",
            display,
            re.IGNORECASE,
        ):
            return True
        tool, name = cls._matrix_invocation(display)
        if not tool or not name:
            return False
        entries = cls._metadata_entries(profile, f"{tool}_environments")
        if tool == "nox":
            entries = cls._metadata_entries(profile, "nox_sessions")
        known = matrix_entry(entries, name)
        if known is not None:
            return known.get("safe") is not True
        return bool(
            re.search(
                r"(?:^|[-_.])(docs?|lint|format|typecheck|mypy|ruff|fuzz|"
                r"benchmarks?|integration|e2e|remote|release)(?:[-_.]|$)",
                name,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _runtime_rank(command: str, python_version: str) -> int:
        normalized = TestCommandSelector._normalize_python_version(python_version)
        if not normalized:
            return 1
        versions = re.findall(
            r"(?:python|py)(\d(?:\.\d{1,2}|\d{1,2}))",
            command,
            re.IGNORECASE,
        )
        if not versions:
            return 1
        normalized_versions = {
            TestCommandSelector._normalize_python_version(item) for item in versions
        }
        return 0 if normalized in normalized_versions else 2

    @staticmethod
    def _source_rank(profile: ProjectProfile, command: ProjectCommand) -> int:
        source = command.source.lower()
        if command.source in profile.ci_files or source.startswith((".github/", ".circleci/")):
            return 0
        if command.source in profile.readme_files or "readme" in source:
            return 1
        text = command.command.display.lower()
        if re.search(r"\b(pytest|unittest)\b", text) or source == "inferred:test-layout":
            return 2
        if any(name in source for name in ("pyproject", "tox.ini", "noxfile", "setup.cfg")):
            return 3
        return 4

    @staticmethod
    def _bounded_matrix_command(
        profile: ProjectProfile,
        command: ProjectCommand,
        python_version: str = "",
    ) -> ProjectCommand:
        display = command.command.display.strip()
        replacement = ""
        tool, selected_name = TestCommandSelector._matrix_invocation(display)
        if tool == "tox":
            entries = TestCommandSelector._metadata_entries(profile, "tox_environments")
            env = preferred_matrix_name(entries, python_version=python_version)
            if not env:
                env = str(profile.metadata.get("default_tox_env", "")).strip()
            selected = matrix_entry(entries, selected_name) if selected_name else None
            selected_version = str((selected or {}).get("python_version", ""))
            if env and (
                not selected_name
                or selected is None
                or selected.get("safe") is not True
                or (
                    python_version
                    and selected_version
                    and selected_version
                    != TestCommandSelector._normalize_python_version(python_version)
                )
            ):
                replacement = f"tox -e {env}"
        elif tool == "nox":
            entries = TestCommandSelector._metadata_entries(profile, "nox_sessions")
            session = preferred_matrix_name(entries, python_version=python_version)
            if not session:
                session = str(profile.metadata.get("default_nox_session", "")).strip()
            selected = matrix_entry(entries, session) if session else None
            versions = tuple((selected or {}).get("python_versions", ()))
            normalized = TestCommandSelector._normalize_python_version(python_version)
            if normalized and normalized in {
                TestCommandSelector._normalize_python_version(item) for item in versions
            }:
                session = f"{session}-{normalized}"
            if session and (not selected_name or selected_name != session):
                replacement = f"nox -s {session}"
        if not replacement:
            return command
        bounded = CommandSpec(
            (replacement,),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
            shell=True,
        )
        return ProjectCommand(
            command.name,
            bounded,
            command.source,
            command.confidence,
        )

    @staticmethod
    def _metadata_entries(
        profile: ProjectProfile,
        key: str,
    ) -> tuple[Mapping[str, Any], ...]:
        value = profile.metadata.get(key, ())
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _matrix_invocation(command: str) -> tuple[str, str]:
        tox = re.search(r"\btox\b(?:\s+[^;&|\s]+)*?\s+(?:-e|--environment)\s+([^\s,;&|]+)", command)
        if tox:
            return "tox", tox.group(1)
        if re.fullmatch(r"(?:python\S*\s+-m\s+)?tox", command):
            return "tox", ""
        nox = re.search(r"\bnox\b(?:\s+[^;&|\s]+)*?\s+(?:-s|--session)\s+([^\s,;&|]+)", command)
        if nox:
            return "nox", nox.group(1)
        if re.fullmatch(r"(?:python\S*\s+-m\s+)?nox", command):
            return "nox", ""
        return "", ""

    @staticmethod
    def _normalize_python_version(value: object) -> str:
        text = str(value).strip().casefold().removeprefix("python").removeprefix("py")
        dotted = re.fullmatch(r"(\d)\.(\d{1,2})(?:\.\d+)?", text)
        if dotted:
            return f"{dotted.group(1)}.{int(dotted.group(2))}"
        compact = re.fullmatch(r"(\d)(\d{1,2})", text)
        if compact:
            return f"{compact.group(1)}.{int(compact.group(2))}"
        return ""


def select_run_command(profile: ProjectProfile) -> ProjectCommand | None:
    candidates = [
        item
        for item in profile.commands
        if item.command.purpose is CommandPurpose.RUN and not is_smoke_command(item)
    ]
    return max(candidates, key=lambda item: item.confidence, default=None)
