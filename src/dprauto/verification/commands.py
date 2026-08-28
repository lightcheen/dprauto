"""Select project-owned test and run commands from normalized profile evidence."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from dprauto.adapters.python.test_matrix import matrix_entry, preferred_matrix_name
from dprauto.command_semantics import is_smoke_command, select_run_command
from dprauto.domain.enums import CommandPurpose
from dprauto.domain.models import CommandSpec, ProjectCommand, ProjectProfile

@dataclass(frozen=True, slots=True)
class TestCommandSelection:
    command: ProjectCommand
    original_command: CommandSpec
    kind: str = "project-command"
    targets: tuple[str, ...] = ()
    reason: str = "selected the narrowest local project test command"


class TestCommandSelector:
    """Select one bounded, local project test command."""

    __test__ = False

    def __init__(
        self,
        *,
        max_test_files: int = 8,
        max_parallel_workers: int = 1,
    ) -> None:
        if not 1 <= max_test_files <= 32:
            raise ValueError("max_test_files must be between 1 and 32")
        if not 1 <= max_parallel_workers <= 32:
            raise ValueError("max_parallel_workers must be between 1 and 32")
        self.max_test_files = max_test_files
        self.max_parallel_workers = max_parallel_workers

    def select(
        self,
        profile: ProjectProfile,
        *,
        python_version: str = "",
    ) -> ProjectCommand | None:
        selection = self.select_with_details(
            profile,
            python_version=python_version,
        )
        return selection.command if selection is not None else None

    def select_with_details(
        self,
        profile: ProjectProfile,
        *,
        python_version: str = "",
    ) -> TestCommandSelection | None:
        if self._required_environment(profile):
            return None
        test_files = self._metadata_paths(profile, "test_files")
        safe_files = self._metadata_paths(profile, "safe_test_files")
        if test_files and not safe_files:
            return None
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
        selected = self._bounded_matrix_command(profile, selected, python_version)
        original = selected.command
        bounded, targets, reason = self._bounded_local_command(profile, selected)
        capture_safe = self._with_pytest_capture_disabled(profile, bounded)
        parallel_safe = self._with_ctest_parallelism(capture_safe)
        kinds: list[str] = []
        if targets:
            kinds.append("bounded-file-slice")
        elif bounded.command != original:
            kinds.append("coverage-normalized")
        if capture_safe.command != bounded.command:
            kinds.append("pytest-capture-disabled")
            reason += (
                "; disabled pytest output capture because project source rewraps "
                "sys.stdout/sys.stderr at import time"
            )
        if parallel_safe.command != capture_safe.command:
            kinds.append("ctest-bounded-parallel")
            reason += f"; bounded CTest concurrency to {self.max_parallel_workers} workers"
        kind = "+".join(kinds) or "project-command"
        return TestCommandSelection(
            parallel_safe,
            original,
            kind,
            targets,
            reason,
        )

    def _with_ctest_parallelism(self, command: ProjectCommand) -> ProjectCommand:
        if self.max_parallel_workers <= 1:
            return command
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        if (
            not tokens
            or PurePosixPath(tokens[0]).name.casefold() != "ctest"
            or any(
                token in {"-j", "--parallel"}
                or token.startswith(("-j", "--parallel="))
                for token in tokens[1:]
            )
        ):
            return command
        spec = CommandSpec(
            (*tokens, "--parallel", str(self.max_parallel_workers)),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"parallel-safe:{command.source}",
            command.confidence,
        )

    def _bounded_local_command(
        self,
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> tuple[ProjectCommand, tuple[str, ...], str]:
        normalized = self._without_coverage_observation(command)
        safe_files = self._metadata_paths(profile, "safe_test_files")
        external_files = self._metadata_paths(profile, "external_test_files")
        needs_targets = len(safe_files) > self.max_test_files or bool(external_files)
        if (
            not needs_targets
            or not safe_files
            or command.command.cwd
            or not self._is_direct_pytest(command.command.display)
        ):
            reason = (
                "removed coverage-only observation arguments"
                if normalized.command != command.command
                else "selected the narrowest local project test command"
            )
            return normalized, (), reason
        targets = self._representative_targets(safe_files)
        bounded_spec = CommandSpec(
            ("python", "-m", "pytest", *targets),
            purpose=command.command.purpose,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return (
            ProjectCommand(
                command.name,
                bounded_spec,
                f"bounded:{command.source}",
                command.confidence,
            ),
            targets,
            (
                f"selected {len(targets)} local test file(s) from "
                f"{len(safe_files)} safe and {len(external_files)} external-risk file(s)"
            ),
        )

    def _representative_targets(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        groups: dict[str, list[str]] = {}
        for path in paths:
            groups.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)
        ordered = [
            sorted(groups[parent])
            for parent in sorted(
                groups,
                key=lambda item: (len(PurePosixPath(item).parts), item),
            )
        ]
        selected: list[str] = []
        while len(selected) < self.max_test_files and any(ordered):
            for group in ordered:
                if group and len(selected) < self.max_test_files:
                    selected.append(group.pop(0))
        return tuple(selected)

    @staticmethod
    def _metadata_paths(profile: ProjectProfile, key: str) -> tuple[str, ...]:
        values = profile.metadata.get(key, ())
        if not isinstance(values, (list, tuple)):
            return ()
        selected: list[str] = []
        for value in values[:512]:
            if not isinstance(value, str):
                continue
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix.casefold() != ".py"
            ):
                continue
            selected.append(path.as_posix())
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _required_environment(profile: ProjectProfile) -> tuple[str, ...]:
        values = profile.metadata.get("test_required_environment_variables", ())
        if not isinstance(values, (list, tuple)):
            return ()
        supplied = profile.metadata.get("test_environment_variables", {})
        supplied_names = set(supplied) if isinstance(supplied, Mapping) else set()
        return tuple(
            value
            for value in values[:32]
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value)
            and value not in supplied_names
        )

    @staticmethod
    def _is_direct_pytest(command: str) -> bool:
        return bool(
            re.search(
                r"(?:^|\s)(?:python\S*\s+-m\s+)?(?:pytest|py\.test)(?:\s|$)",
                command,
                re.IGNORECASE,
            )
        ) and not re.search(r"[;&|]", command)

    @staticmethod
    def _without_coverage_observation(command: ProjectCommand) -> ProjectCommand:
        display = command.command.display
        if not TestCommandSelector._is_direct_pytest(display) or "cov" not in display.casefold():
            return command
        try:
            tokens = shlex.split(display)
        except ValueError:
            return command
        filtered: list[str] = []
        skip_next = False
        value_options = {"--cov", "--cov-report", "--cov-config", "--cov-fail-under"}
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            lowered = token.casefold()
            if lowered in value_options:
                skip_next = True
                continue
            if lowered.startswith(
                ("--cov=", "--cov-report=", "--cov-config=", "--cov-fail-under=")
            ) or lowered in {"--cov-append", "--cov-branch", "--no-cov-on-fail"}:
                continue
            filtered.append(token)
        if filtered == tokens or not filtered:
            return command
        spec = CommandSpec(
            tuple(filtered),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"normalized:{command.source}",
            command.confidence,
        )

    @staticmethod
    def _with_pytest_capture_disabled(
        profile: ProjectProfile,
        command: ProjectCommand,
    ) -> ProjectCommand:
        evidence = profile.metadata.get("pytest_capture_incompatible_files", ())
        if (
            not isinstance(evidence, (list, tuple))
            or not any(isinstance(item, str) and item for item in evidence)
            or not TestCommandSelector._is_direct_pytest(command.command.display)
        ):
            return command
        try:
            tokens = shlex.split(command.command.display)
        except ValueError:
            return command
        lowered = tuple(token.casefold() for token in tokens)
        if (
            "-s" in lowered
            or "--capture=no" in lowered
            or any(
                token == "--capture" and index + 1 < len(lowered)
                and lowered[index + 1] == "no"
                for index, token in enumerate(lowered)
            )
        ):
            return command
        spec = CommandSpec(
            (*tokens, "-s"),
            purpose=command.command.purpose,
            cwd=command.command.cwd,
            environment=command.command.environment,
            timeout_seconds=command.command.timeout_seconds,
        )
        return ProjectCommand(
            command.name,
            spec,
            f"capture-safe:{command.source}",
            command.confidence,
        )

    @staticmethod
    def _has_unresolved_variables(command: str) -> bool:
        return bool(
            re.search(
                r"\$\{\{|\$\{?[A-Za-z_][A-Za-z0-9_.-]*\}?|"
                r"%[A-Za-z_][A-Za-z0-9_]*%",
                command,
            )
        )

    @staticmethod
    def _command_rank(command: str) -> int:
        text = command.lower()
        if re.fullmatch(
            r"(?:\./)?mvnw?\s+-b\s+test|"
            r"(?:\./)?gradlew?\s+(?:--no-daemon\s+)?test|"
            r"ctest\s+--test-dir\s+build\s+--output-on-failure|"
            r"meson\s+test\s+-c\s+build\s+--print-errorlogs",
            text.strip(),
        ):
            return 0
        if re.search(r"\b(pytest|py\.test)\b", text):
            return 0
        if re.search(r"\bpython\S*\s+(?:manage\.py\s+test|runtests\.py)\b", text):
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
        if re.fullmatch(r"make\s+(?:test|check)", text.strip()):
            return 1
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
        if source.startswith("framework:"):
            return 0
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
