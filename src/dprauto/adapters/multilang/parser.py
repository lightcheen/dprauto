"""Priority-ordered, language-neutral project parser registry."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from dprauto.adapters.multilang.common import (
    ci_files,
    extracted_commands,
    project_commands,
    readme_files,
)
from dprauto.adapters.multilang.ecosystem import BuildEcosystemDetector, EcosystemCandidate
from dprauto.adapters.multilang.dependencies import dependency_contract
from dprauto.adapters.multilang.jvm import JVMProjectParser
from dprauto.adapters.multilang.native import NativeProjectParser
from dprauto.adapters.python import PythonProjectParser
from dprauto.domain.enums import CommandPurpose, ProjectType
from dprauto.domain.models import ProjectCommand, ProjectProfile, SourceReference
from dprauto.errors import ProjectParsingError
from dprauto.inspection.commands import CommandExtractor
from dprauto.inspection.scanner import FileScanner, ScannedProject


class ScannedProjectParser(Protocol):
    name: str
    priority: int

    def supports(self, scanned: ScannedProject) -> bool: ...

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: ScannedProject,
    ) -> ProjectProfile: ...


class PythonScannedProjectParser:
    """Adapt the mature Python parser to the shared registry contract."""

    name = "python-rules-v1"
    priority = 100
    _ROOT_MARKERS = {
        "Pipfile",
        "environment.yml",
        "environment.yaml",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
    }

    def __init__(self, parser: PythonProjectParser | None = None) -> None:
        self.parser = parser or PythonProjectParser()

    def supports(self, scanned: ScannedProject) -> bool:
        has_root_marker = any(
            len(PurePosixPath(path).parts) == 1
            and (
                PurePosixPath(path).name in self._ROOT_MARKERS
                or PurePosixPath(path).name.startswith("requirements")
            )
            for path in scanned.files
        )
        return has_root_marker or any(path.endswith(".py") for path in scanned.files)

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: ScannedProject,
    ) -> ProjectProfile:
        return self.parser.parse(source, scanned.root)


@dataclass(frozen=True, slots=True)
class ParserRegistration:
    name: str
    priority: int
    parser: ScannedProjectParser


class ProjectParserRegistry:
    """Choose one root build ecosystem using explicit, auditable priority."""

    def __init__(
        self,
        parsers: tuple[ScannedProjectParser, ...],
        detector: BuildEcosystemDetector | None = None,
    ) -> None:
        if not parsers:
            raise ValueError("at least one project parser is required")
        names = [parser.name for parser in parsers]
        if len(names) != len(set(names)):
            raise ValueError("project parser names must be unique")
        self.registrations = tuple(
            ParserRegistration(parser.name, parser.priority, parser)
            for parser in sorted(parsers, key=lambda item: (-item.priority, item.name))
        )
        self.detector = detector or BuildEcosystemDetector()

    def select(self, scanned: ScannedProject) -> ScannedProjectParser:
        parser, _ = self.select_with_evidence(scanned)
        return parser

    def select_with_evidence(
        self,
        scanned: ScannedProject,
    ) -> tuple[ScannedProjectParser, EcosystemCandidate]:
        candidates = self.detector.candidates(scanned)
        registrations = {item.name: item for item in self.registrations}
        for candidate in candidates:
            registration = registrations.get(candidate.parser_name)
            if registration is None:
                continue
            scoped = self._scoped_project(scanned, candidate.build_root)
            if registration.parser.supports(scoped):
                return registration.parser, candidate
        for registration in self.registrations:
            if registration.parser.supports(scanned):
                return registration.parser, EcosystemCandidate(
                    registration.name,
                    ".",
                    registration.priority,
                    0.5,
                    ("parser-support-fallback",),
                )
        raise ProjectParsingError(
            f"no registered parser supports project workspace {scanned.root}"
        )

    @staticmethod
    def _scoped_project(scanned: ScannedProject, build_root: str) -> ScannedProject:
        if build_root == ".":
            return scanned
        prefix = build_root.rstrip("/") + "/"
        files = tuple(
            path[len(prefix) :]
            for path in scanned.files
            if path.startswith(prefix)
        )
        return ScannedProject(
            root=scanned.root / build_root,
            files=files,
            skipped_files=scanned.skipped_files,
            truncated=scanned.truncated,
            max_text_bytes=scanned.max_text_bytes,
        )


class MultiLanguageProjectParser:
    """Production ProjectParser supporting Python, JVM, C, and C++ roots."""

    def __init__(
        self,
        scanner: FileScanner | None = None,
        registry: ProjectParserRegistry | None = None,
    ) -> None:
        self.scanner = scanner or FileScanner()
        self.registry = registry or ProjectParserRegistry(
            (
                JVMProjectParser(),
                NativeProjectParser(),
                PythonScannedProjectParser(),
            )
        )

    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        scanned = self.scanner.scan(workspace)
        parser, selection = self.registry.select_with_evidence(scanned)
        scoped = self.registry._scoped_project(scanned, selection.build_root)
        profile = parser.parse_scanned(source, scoped)
        profile = self._rebase_profile(profile, selection.build_root)
        profile = self._merge_repository_context(
            profile,
            scanned,
            selection.build_root,
            parser.name,
        )
        # Preserve the chosen root parser separately from language counts so
        # mixed repositories and future Tree-sitter indexing stay auditable.
        metadata = dict(profile.metadata)
        metadata["parser_registry_selection"] = parser.name
        metadata["build_root"] = selection.build_root
        metadata["parser_selection_confidence"] = selection.confidence
        metadata["parser_selection_evidence"] = selection.evidence
        metadata["parser_candidate_scores"] = tuple(
            candidate.as_metadata() for candidate in self.registry.detector.candidates(scanned)
        )
        metadata["repository_evidence"] = self._repository_evidence(
            profile,
            selection,
            metadata["parser_candidate_scores"],
        )
        vcs_evidence = self._vcs_metadata_evidence(scanned, profile)
        metadata["vcs_metadata_required"] = bool(vcs_evidence)
        metadata["vcs_metadata_evidence"] = vcs_evidence
        metadata["project_roots"] = self._project_roots(scanned)
        profile = replace(profile, metadata=metadata)
        metadata = dict(profile.metadata)
        metadata["dependency_contract"] = dependency_contract(
            profile,
            parser_name=parser.name,
            build_root=selection.build_root,
        )
        return replace(profile, metadata=metadata)

    @staticmethod
    def _vcs_metadata_evidence(
        scanned: ScannedProject,
        profile: ProjectProfile,
    ) -> tuple[str, ...]:
        """Find build-time VCS consumers without relying on project identities."""

        patterns = re.compile(
            r"(?i)(?:\bgit\s+(?:describe|rev-parse|log)\b|"
            r"\bgrgit\b|\bgitversion\b|\bjgitver\b|"
            r"\bsetuptools[_-]scm\b|\bhatch-vcs\b|\bversioningit\b)"
        )
        selected: list[str] = []
        for path in tuple(dict.fromkeys((*profile.build_files, *profile.dependency_files)))[:128]:
            content = scanned.read_text(path)
            if patterns.search(content):
                selected.append(path)
        return tuple(selected[:16])

    @staticmethod
    def _project_roots(scanned: ScannedProject) -> tuple[str, ...]:
        """Return independent roots without treating aggregate modules as projects."""

        roots = {"."}
        python_manifests = {"pyproject.toml", "setup.py", "setup.cfg"}
        gradle_roots = {"settings.gradle", "settings.gradle.kts"}
        for path in scanned.files:
            pure = PurePosixPath(path)
            name = pure.name.casefold()
            if name in python_manifests or name in gradle_roots:
                roots.add(pure.parent.as_posix())

        root_pom = scanned.read_text("pom.xml")
        reactor_modules = {
            PurePosixPath(value.strip()).as_posix().strip("/")
            for value in re.findall(r"(?is)<module>\s*([^<]+?)\s*</module>", root_pom)
            if value.strip() and ".." not in PurePosixPath(value.strip()).parts
        }
        for path in scanned.files:
            pure = PurePosixPath(path)
            if pure.name.casefold() != "pom.xml" or pure.parent.as_posix() == ".":
                continue
            root = pure.parent.as_posix()
            if not any(
                root == module or root.startswith(module + "/")
                for module in reactor_modules
            ):
                roots.add(root)
        roots.add(".")
        return tuple(sorted(roots, key=lambda value: (len(PurePosixPath(value).parts), value)))

    @classmethod
    def _merge_repository_context(
        cls,
        profile: ProjectProfile,
        scanned: ScannedProject,
        build_root: str,
        parser_name: str,
    ) -> ProjectProfile:
        """Keep repository-level documentation and CI when building a nested root."""

        if build_root == ".":
            return profile
        root_readmes = readme_files(scanned)
        root_ci = ci_files(scanned)
        discovered = project_commands(
            extracted_commands(scanned, root_readmes, root_ci, CommandExtractor())
        )
        context_commands = tuple(
            item
            for item in discovered
            if not (
                parser_name == "native-rules-v1"
                and profile.project_type is ProjectType.LIBRARY
                and item.command.purpose is CommandPurpose.RUN
            )
        )
        if (
            parser_name == "native-rules-v1"
            and profile.project_type is ProjectType.CLI
        ):
            runtime = cls._native_cli_runtime_command(
                context_commands,
                str(profile.metadata.get("project_name", "")),
                build_root,
            )
            if runtime is not None:
                context_commands = (*context_commands, runtime)
        selected: dict[tuple[str, CommandPurpose], ProjectCommand] = {}
        for item in (*profile.commands, *context_commands):
            key = (item.command.display.casefold(), item.command.purpose)
            current = selected.get(key)
            if current is None or item.confidence > current.confidence:
                selected[key] = item
        return replace(
            profile,
            readme_files=tuple(dict.fromkeys((*root_readmes, *profile.readme_files))),
            ci_files=tuple(dict.fromkeys((*root_ci, *profile.ci_files))),
            commands=tuple(selected.values()),
        )

    @staticmethod
    def _native_cli_runtime_command(
        commands: tuple[ProjectCommand, ...],
        project_name: str,
        build_root: str,
    ) -> ProjectCommand | None:
        """Promote a bounded direct executable invocation from repository evidence."""

        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", project_name):
            return None
        pattern = re.compile(
            rf"^(?:\./)?{re.escape(project_name)}"
            r"(?:\s+[A-Za-z0-9_.+:/=-]+){1,8}$"
        )
        candidate = next(
            (
                item
                for item in commands
                if item.command.purpose is CommandPurpose.OTHER
                and pattern.fullmatch(item.command.display.strip())
            ),
            None,
        )
        if candidate is None:
            return None
        return replace(
            candidate,
            name="run-repository-executable",
            command=replace(
                candidate.command,
                purpose=CommandPurpose.RUN,
                cwd=None if build_root == "." else build_root,
            ),
            source=f"runtime-evidence:{candidate.source}",
        )

    @staticmethod
    def _prefixed_path(build_root: str, value: str) -> str:
        if build_root == "." or not value or value == ".":
            return value if value != "." else build_root
        return (PurePosixPath(build_root) / value).as_posix()

    @classmethod
    def _rebase_profile(cls, profile: ProjectProfile, build_root: str) -> ProjectProfile:
        if build_root == ".":
            return profile

        def paths(values: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(cls._prefixed_path(build_root, value) for value in values)

        commands = tuple(
            replace(
                item,
                command=replace(
                    item.command,
                    cwd=cls._prefixed_path(build_root, item.command.cwd or "."),
                ),
            )
            for item in profile.commands
        )
        metadata = dict(profile.metadata)
        for key in (
            "subprojects",
            "working_directories",
            "test_files",
            "safe_test_files",
            "unstable_test_files",
            "external_test_files",
            "pytest_capture_incompatible_files",
        ):
            values = metadata.get(key)
            if isinstance(values, (tuple, list)):
                metadata[key] = tuple(
                    cls._prefixed_path(build_root, value)
                    for value in values
                    if isinstance(value, str)
                )
        raw_dependency_evidence = metadata.get("system_dependency_evidence")
        if isinstance(raw_dependency_evidence, (tuple, list)):
            rebased_evidence: list[dict[str, object]] = []
            for item in raw_dependency_evidence:
                if not isinstance(item, dict):
                    continue
                rebased = dict(item)
                source = rebased.get("source")
                if isinstance(source, str):
                    rebased["source"] = cls._prefixed_path(build_root, source)
                rebased_evidence.append(rebased)
            metadata["system_dependency_evidence"] = tuple(rebased_evidence)
        for key in (
            "python_version_evidence",
            "java_version_evidence",
            "java_target_version_evidence",
            "cmake_version_evidence",
        ):
            value = metadata.get(key)
            if not isinstance(value, str) or not value:
                continue
            source, separator, detail = value.partition(":")
            metadata[key] = cls._prefixed_path(build_root, source) + (
                f":{detail}" if separator else ""
            )
        build_tool_evidence = metadata.get("build_tool_version_evidence")
        if isinstance(build_tool_evidence, dict):
            rebased_tool_evidence: dict[str, str] = {}
            for name, value in build_tool_evidence.items():
                if not isinstance(name, str) or not isinstance(value, str):
                    continue
                source, separator, detail = value.partition(":")
                rebased_tool_evidence[name] = cls._prefixed_path(build_root, source) + (
                    f":{detail}" if separator else ""
                )
            metadata["build_tool_version_evidence"] = rebased_tool_evidence
        return replace(
            profile,
            dependency_files=paths(profile.dependency_files),
            build_files=paths(profile.build_files),
            dockerfiles=paths(profile.dockerfiles),
            readme_files=paths(profile.readme_files),
            ci_files=paths(profile.ci_files),
            commands=commands,
            metadata=metadata,
        )

    @staticmethod
    def _repository_evidence(
        profile: ProjectProfile,
        selection: EcosystemCandidate,
        candidates: object,
    ) -> dict[str, object]:
        version_sources = {
            "python": profile.metadata.get("python_version_evidence", ""),
            "java": profile.metadata.get("java_version_evidence", ""),
            "java_target": profile.metadata.get("java_target_version_evidence", ""),
        }
        constraints = tuple(
            {
                "name": name,
                "value": value,
                "source": str(version_sources.get(name, "")),
                "confidence": 0.95 if version_sources.get(name) else 0.7,
            }
            for name, value in profile.runtime_constraints.items()
        )
        commands = tuple(
            {
                "value": item.command.display,
                "purpose": item.command.purpose.value,
                "source": item.source,
                "confidence": item.confidence,
                "working_directory": item.command.cwd or ".",
            }
            for item in profile.commands
        )
        return {
            "schema_version": 1,
            "parser_selection": selection.as_metadata(),
            "candidate_scores": candidates,
            "runtime_constraints": constraints,
            "documents": tuple(
                {"path": path, "kind": "documentation", "confidence": 0.9}
                for path in profile.readme_files
            )
            + tuple(
                {"path": path, "kind": "ci", "confidence": 0.98}
                for path in profile.ci_files
            ),
            "build_manifests": tuple(
                {"path": path, "kind": "build", "confidence": 0.99}
                for path in profile.build_files
            )
            + tuple(
                {"path": path, "kind": "dependency", "confidence": 0.95}
                for path in profile.dependency_files
            ),
            "commands": commands,
        }
