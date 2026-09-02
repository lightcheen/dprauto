"""Deterministic build-marker indexing and evidence-ranked component discovery."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import re
from typing import Iterable

from dprauto.adapters.multilang.detector import RepositoryLanguageDetector
from dprauto.domain.workspace import (
    BuildMarker,
    BuildMarkerIndex,
    ComponentCandidate,
    ComponentEvidence,
    ComponentGraph,
    ComponentRelation,
    RepositoryScan,
)


@dataclass(frozen=True, slots=True)
class BuildMarkerRule:
    filename: str
    build_system: str
    kind: str = "build"
    case_sensitive: bool = True

    def matches(self, filename: str) -> bool:
        if self.case_sensitive:
            return filename == self.filename
        return filename.casefold() == self.filename.casefold()


DEFAULT_MARKER_RULES = (
    BuildMarkerRule("CMakeLists.txt", "cmake"),
    BuildMarkerRule("Makefile", "make"),
    BuildMarkerRule("build.gradle", "gradle"),
    BuildMarkerRule("build.gradle.kts", "gradle"),
    BuildMarkerRule("configure", "autotools"),
    BuildMarkerRule("configure.ac", "autotools"),
    BuildMarkerRule("configure.in", "autotools"),
    BuildMarkerRule("meson.build", "meson"),
    BuildMarkerRule("pom.xml", "maven"),
    BuildMarkerRule("pyproject.toml", "python-packaging"),
    BuildMarkerRule("setup.cfg", "setuptools"),
    BuildMarkerRule("setup.py", "setuptools"),
    BuildMarkerRule("gradlew", "gradle", kind="supporting"),
    BuildMarkerRule("mvnw", "maven", kind="supporting"),
    BuildMarkerRule("settings.gradle", "gradle", kind="supporting"),
    BuildMarkerRule("settings.gradle.kts", "gradle", kind="supporting"),
    BuildMarkerRule("poetry.lock", "poetry", kind="supporting"),
)

_VENDOR_SEGMENTS = frozenset(
    {
        "3rdparty",
        "deps",
        "external",
        "subprojects",
        "third-party",
        "third_party",
        "vendor",
        "vendors",
    }
)
_ROLE_SEGMENTS = (
    (frozenset({"binding", "bindings"}), "binding", -80),
    (frozenset({"example", "examples", "sample", "samples"}), "example", -100),
    (frozenset({"test", "tests", "testing"}), "test", -120),
    (frozenset({"tool", "tools"}), "tool", -40),
    (frozenset({"doc", "docs", "documentation"}), "documentation", -140),
)
_DOCUMENT_NAMES = frozenset(
    {"building.md", "build.md", "contributing.md", "readme", "readme.md", "readme.rst"}
)


class BuildMarkerIndexer:
    def __init__(self, rules: Iterable[BuildMarkerRule] = DEFAULT_MARKER_RULES) -> None:
        self.rules = tuple(rules)
        if not self.rules:
            raise ValueError("at least one build marker rule is required")

    def index(self, scan: RepositoryScan) -> BuildMarkerIndex:
        markers: list[BuildMarker] = []
        for path in scan.files:
            filename = PurePosixPath(path).name
            root_path = PurePosixPath(path).parent
            component_root = root_path.as_posix()
            for rule in self.rules:
                if rule.matches(filename):
                    markers.append(
                        BuildMarker(
                            path,
                            component_root,
                            rule.build_system,
                            rule.kind,
                        )
                    )
        markers.sort(key=lambda item: (item.path, item.build_system, item.kind))
        return BuildMarkerIndex(tuple(markers))


class EvidenceRankedComponentDiscoverer:
    """Discover components from recursive markers and rank them using repository evidence."""

    def __init__(
        self,
        indexer: BuildMarkerIndexer | None = None,
        language_detector: RepositoryLanguageDetector | None = None,
    ) -> None:
        self.indexer = indexer or BuildMarkerIndexer()
        self.language_detector = language_detector or RepositoryLanguageDetector()

    def discover(self, scan: RepositoryScan) -> ComponentGraph:
        index = self.indexer.index(scan)
        grouped: dict[str, list[BuildMarker]] = defaultdict(list)
        for marker in index.markers:
            grouped[marker.component_root].append(marker)

        component_roots = {
            root
            for root, markers in grouped.items()
            if any(self._is_component_entry(scan, marker) for marker in markers)
        }
        candidates = tuple(
            self._candidate(scan, root, tuple(markers))
            for root, markers in sorted(grouped.items())
            if root in component_roots
        )
        relations = self._relations(scan, candidates)
        eligible = [candidate for candidate in candidates if candidate.primary_eligible]
        primary = (
            sorted(
                eligible,
                key=lambda item: (
                    -item.score,
                    len(PurePosixPath(item.root).parts),
                    item.root,
                    item.component_id,
                ),
            )[0]
            if eligible
            else None
        )
        return ComponentGraph(
            candidates,
            relations,
            primary.component_id if primary else None,
            scan_truncated=scan.truncated,
        )

    @classmethod
    def _is_component_entry(cls, scan: RepositoryScan, marker: BuildMarker) -> bool:
        if marker.kind != "build":
            return False
        segments = {part.casefold() for part in PurePosixPath(marker.component_root).parts}
        if segments & {"binding", "bindings"}:
            return True
        content = scan.read_text(marker.path)
        if marker.build_system == "cmake":
            return bool(re.search(r"(?im)^\s*project\s*\(", content))
        if marker.build_system == "meson":
            return bool(re.search(r"(?im)^\s*project\s*\(", content))
        return True

    def _candidate(
        self,
        scan: RepositoryScan,
        root: str,
        markers: tuple[BuildMarker, ...],
    ) -> ComponentCandidate:
        build_markers = tuple(marker for marker in markers if marker.kind == "build")
        poetry_project = any(marker.build_system == "poetry" for marker in markers)

        def effective_system(marker: BuildMarker) -> str:
            if poetry_project and marker.build_system == "python-packaging":
                return "poetry"
            return marker.build_system

        systems = tuple(dict.fromkeys(effective_system(marker) for marker in build_markers))
        evidence: list[ComponentEvidence] = [
            ComponentEvidence("build_marker", marker.path, marker.build_system, 100)
            for marker in build_markers
        ]
        role, role_score, primary_eligible = self._role(root)
        score = 100 + role_score
        if root == ".":
            score += 100
            evidence.append(ComponentEvidence("repository_root", build_markers[0].path, weight=100))
        else:
            score -= 5 * len(PurePosixPath(root).parts)

        subtree = scan.subtree(root)
        language_counts = self.language_detector.counts(subtree)
        score += min(sum(language_counts.values()), 30)
        documentation = self._documentation_evidence(scan, root)
        evidence.extend(documentation)
        score += sum(item.weight for item in documentation)
        ordered_systems = self._order_systems(scan, systems)
        ordered_entries = tuple(
            marker.path
            for system in ordered_systems
            for marker in build_markers
            if effective_system(marker) == system
        )
        return ComponentCandidate(
            self._component_id(root),
            root,
            ordered_systems,
            ordered_entries,
            languages=self.language_detector.languages(subtree),
            role=role,
            evidence=tuple(evidence),
            score=score,
            primary_eligible=primary_eligible,
        )

    @staticmethod
    def _component_id(root: str) -> str:
        if root == ".":
            return "component-root"
        slug = re.sub(r"[^a-z0-9]+", "-", root.casefold()).strip("-") or "component"
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
        return f"component-{slug}-{digest}"

    @staticmethod
    def _role(root: str) -> tuple[str, int, bool]:
        if root == ".":
            return "primary", 0, True
        segments = {segment.casefold() for segment in PurePosixPath(root).parts}
        if segments & _VENDOR_SEGMENTS:
            return "vendor", -1_000, False
        for names, role, penalty in _ROLE_SEGMENTS:
            if segments & names:
                return role, penalty, True
        return "candidate", 0, True

    @staticmethod
    def _evidence_files(scan: RepositoryScan) -> tuple[str, ...]:
        result = []
        for path in scan.files:
            posix = PurePosixPath(path)
            name = posix.name.casefold()
            is_ci = ".github/workflows" in path or name in {
                ".circleci.yml",
                ".gitlab-ci.yml",
                ".travis.yml",
                "appveyor.yml",
                "azure-pipelines.yml",
            }
            if name in _DOCUMENT_NAMES or is_ci:
                result.append(path)
        return tuple(result)

    @classmethod
    def _documentation_evidence(
        cls,
        scan: RepositoryScan,
        root: str,
    ) -> tuple[ComponentEvidence, ...]:
        if root == ".":
            return ()
        escaped = re.escape(root)
        pattern = re.compile(
            rf"(?im)(?:^|[;&|]\s*|\$\s*)(?:cd|pushd)\s+(?:\./)?{escaped}(?:/[^\s;&|]+)?"
        )
        evidence = []
        for path in cls._evidence_files(scan):
            if pattern.search(scan.read_text(path)):
                kind = "ci_working_directory" if ".yml" in path or ".yaml" in path else "docs"
                weight = 90 if kind == "ci_working_directory" else 80
                evidence.append(ComponentEvidence(kind, path, f"enters {root}", weight))
        return tuple(evidence)

    @classmethod
    def _order_systems(
        cls,
        scan: RepositoryScan,
        systems: tuple[str, ...],
    ) -> tuple[str, ...]:
        patterns = {
            "autotools": re.compile(r"(?im)(?:\./configure|autoreconf\s|autogen\.sh)"),
            "cmake": re.compile(r"(?im)(?:^|[$;]\s*)cmake\s+(?:-S\s+|\.\.?\s|--build\s)"),
            "gradle": re.compile(r"(?im)(?:^|[$;]\s*)(?:\./)?gradlew?\s+"),
            "make": re.compile(r"(?im)(?:^|[$;]\s*)make(?:\s|$)"),
            "maven": re.compile(r"(?im)(?:^|[$;]\s*)(?:\./)?mvnw?\s+"),
            "meson": re.compile(r"(?im)(?:^|[$;]\s*)meson\s+(?:setup|compile|install|test)\s"),
            "poetry": re.compile(r"(?im)(?:^|[$;]\s*)poetry\s+(?:install|build|run)\s"),
            "python-packaging": re.compile(r"(?im)(?:^|[$;]\s*)(?:python\s+-m\s+)?pip\s+install\s"),
            "setuptools": re.compile(r"(?im)(?:^|[$;]\s*)python\s+setup\.py\s"),
        }
        text = "\n".join(scan.read_text(path) for path in cls._evidence_files(scan))
        scores = {
            system: len(patterns.get(system, re.compile(r"(?!x)x")).findall(text))
            for system in systems
        }
        return tuple(sorted(systems, key=lambda system: (-scores[system], systems.index(system))))

    @classmethod
    def _relations(
        cls,
        scan: RepositoryScan,
        candidates: tuple[ComponentCandidate, ...],
    ) -> tuple[ComponentRelation, ...]:
        relations: list[ComponentRelation] = []
        by_root = {candidate.root: candidate for candidate in candidates}
        for parent in candidates:
            for child in candidates:
                if parent is child:
                    continue
                contains = parent.root == "." or child.root.startswith(f"{parent.root}/")
                if contains:
                    relations.append(
                        ComponentRelation(parent.component_id, child.component_id, "contains")
                    )

        for source in candidates:
            subtree = scan.subtree(source.root)
            for path in subtree.by_name(("configure.ac", "configure.in")):
                content = subtree.read_text(path)
                for match in re.finditer(r"AC_CONFIG_SUBDIRS\s*\(\s*\[?([^\]\)]+)", content):
                    for value in match.group(1).split():
                        target_path = cls._resolve_component_path(source.root, value)
                        target = by_root.get(target_path)
                        if target is not None and target.component_id != source.component_id:
                            repository_path = cls._repository_path(source.root, path)
                            relations.append(
                                ComponentRelation(
                                    source.component_id,
                                    target.component_id,
                                    "depends_on",
                                    (
                                        ComponentEvidence(
                                            "build_dependency",
                                            repository_path,
                                            value,
                                            20,
                                        ),
                                    ),
                                )
                            )
        identities = set()
        unique = []
        for relation in relations:
            identity = (relation.source_id, relation.target_id, relation.relation_type)
            if identity not in identities:
                identities.add(identity)
                unique.append(relation)
        return tuple(
            sorted(
                unique,
                key=lambda item: (item.source_id, item.target_id, item.relation_type),
            )
        )

    @staticmethod
    def _resolve_component_path(source_root: str, relative: str) -> str:
        source = PurePosixPath() if source_root == "." else PurePosixPath(source_root)
        parts: list[str] = list(source.parts)
        for part in PurePosixPath(relative).parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        return PurePosixPath(*parts).as_posix() if parts else "."

    @staticmethod
    def _repository_path(component_root: str, path: str) -> str:
        return path if component_root == "." else f"{component_root}/{path}"
