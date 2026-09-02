"""Priority-ordered, language-neutral project parser registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from dprauto.adapters.multilang.jvm import JVMProjectParser
from dprauto.adapters.multilang.native import NativeProjectParser
from dprauto.adapters.python import PythonProjectParser
from dprauto.domain.models import ProjectProfile, SourceReference
from dprauto.domain.workspace import RepositoryScan
from dprauto.errors import ProjectParsingError
from dprauto.inspection.scanner import FileScanner


class RepositoryScanParser(Protocol):
    name: str
    priority: int

    def supports(self, scanned: RepositoryScan) -> bool: ...

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: RepositoryScan,
    ) -> ProjectProfile: ...


class PythonRepositoryScanParser:
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

    def supports(self, scanned: RepositoryScan) -> bool:
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
        scanned: RepositoryScan,
    ) -> ProjectProfile:
        return self.parser.parse(source, scanned.root)


@dataclass(frozen=True, slots=True)
class ParserRegistration:
    name: str
    priority: int
    parser: RepositoryScanParser


class ProjectParserRegistry:
    """Choose one root build ecosystem using explicit, auditable priority."""

    def __init__(self, parsers: tuple[RepositoryScanParser, ...]) -> None:
        if not parsers:
            raise ValueError("at least one project parser is required")
        names = [parser.name for parser in parsers]
        if len(names) != len(set(names)):
            raise ValueError("project parser names must be unique")
        self.registrations = tuple(
            ParserRegistration(parser.name, parser.priority, parser)
            for parser in sorted(parsers, key=lambda item: (-item.priority, item.name))
        )

    def select(self, scanned: RepositoryScan) -> RepositoryScanParser:
        for registration in self.registrations:
            if registration.parser.supports(scanned):
                return registration.parser
        raise ProjectParsingError(
            f"no registered parser supports project workspace {scanned.root}"
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
                PythonRepositoryScanParser(),
            )
        )

    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        scanned = self.scanner.scan(workspace)
        parser = self.registry.select(scanned)
        profile = parser.parse_scanned(source, scanned)
        # Preserve the chosen root parser separately from language counts so
        # mixed repositories and future Tree-sitter indexing stay auditable.
        metadata = dict(profile.metadata)
        metadata["parser_registry_selection"] = parser.name
        return replace(profile, metadata=metadata)
