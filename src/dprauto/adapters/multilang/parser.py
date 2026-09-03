"""Priority-ordered, language-neutral project parser registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Protocol

from dprauto.adapters.multilang.jvm import JVMProjectParser
from dprauto.adapters.multilang.native import NativeProjectParser
from dprauto.adapters.python import PythonProjectParser
from dprauto.domain.models import ProjectProfile, SourceReference
from dprauto.domain.workspace import ComponentCandidate, ComponentGraph, RepositoryScan
from dprauto.errors import ProjectParsingError
from dprauto.inspection.components import EvidenceRankedComponentDiscoverer
from dprauto.inspection.scanner import FileScanner
from dprauto.ports.discovery import ComponentDiscoverer


class RepositoryScanParser(Protocol):
    name: str
    priority: int

    def supports(self, scanned: RepositoryScan) -> bool: ...

    def parse_scanned(
        self,
        source: SourceReference,
        scanned: RepositoryScan,
        component: ComponentCandidate,
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
        component: ComponentCandidate,
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
        discoverer: ComponentDiscoverer | None = None,
        registry: ProjectParserRegistry | None = None,
    ) -> None:
        self.scanner = scanner or FileScanner()
        self.discoverer = discoverer or EvidenceRankedComponentDiscoverer()
        self.registry = registry or ProjectParserRegistry(
            (
                JVMProjectParser(),
                NativeProjectParser(),
                PythonRepositoryScanParser(),
            )
        )

    def parse(self, source: SourceReference, workspace: Path) -> ProjectProfile:
        scanned = self.scanner.scan(workspace)
        graph = self.discoverer.discover(scanned)
        component = graph.primary
        if component is None:
            raise ProjectParsingError(
                f"no buildable component discovered in repository workspace {scanned.root}"
            )
        component_scan = scanned.subtree(component.root)
        parser = self.registry.select(component_scan)
        profile = parser.parse_scanned(source, component_scan, component)
        # Preserve the chosen root parser separately from language counts so
        # mixed repositories and future Tree-sitter indexing stay auditable.
        metadata = dict(profile.metadata)
        metadata["parser_registry_selection"] = parser.name
        metadata["component_id"] = component.component_id
        metadata["component_root"] = component.root
        metadata["component_role"] = component.role
        metadata["primary_build_system"] = component.build_systems[0]
        metadata["component_candidates"] = self._component_records(graph)
        metadata["component_relations"] = self._relation_records(graph)
        metadata["repository_scan_file_count"] = len(scanned.files)
        metadata["repository_scan_skipped_files"] = scanned.skipped_files
        metadata["repository_scan_truncated"] = scanned.truncated
        package_managers = tuple(
            dict.fromkeys((*component.build_systems, *profile.package_managers))
        )
        return replace(profile, package_managers=package_managers, metadata=metadata)

    @staticmethod
    def _component_records(graph: ComponentGraph) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "component_id": item.component_id,
                "root": item.root,
                "build_systems": item.build_systems,
                "build_entries": item.build_entries,
                "languages": item.languages,
                "role": item.role,
                "score": item.score,
                "primary_eligible": item.primary_eligible,
                "selected": item.component_id == graph.primary_component_id,
            }
            for item in graph.ranked()
        )

    @staticmethod
    def _relation_records(graph: ComponentGraph) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "source_id": item.source_id,
                "target_id": item.target_id,
                "relation_type": item.relation_type,
            }
            for item in graph.relations
        )
