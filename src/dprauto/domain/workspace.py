"""Language-neutral repository scan and component discovery contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from dprauto.errors import ModelValidationError


__all__ = [
    "BuildMarker",
    "BuildMarkerIndex",
    "ComponentCandidate",
    "ComponentEvidence",
    "ComponentGraph",
    "ComponentRelation",
    "RepositoryScan",
]


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ModelValidationError(f"{field_name} must not be empty")


def _relative_path(value: str, field_name: str, *, allow_root: bool = False) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "/"}:
        raise ModelValidationError(f"{field_name} must be a safe relative path")
    normalized = path.as_posix()
    if normalized == "." and not allow_root:
        raise ModelValidationError(f"{field_name} must identify a file or subdirectory")
    return normalized


@dataclass(frozen=True, slots=True)
class RepositoryScan:
    """A bounded repository file index with safe component-relative views."""

    root: Path
    files: tuple[str, ...]
    skipped_files: int = 0
    truncated: bool = False
    max_text_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if self.skipped_files < 0 or self.max_text_bytes <= 0:
            raise ModelValidationError("repository scan limits must be valid")
        normalized = tuple(_relative_path(path, "repository_scan.file") for path in self.files)
        if normalized != tuple(sorted(set(normalized))):
            raise ModelValidationError("repository scan files must be sorted and unique")

    def has(self, relative_path: str) -> bool:
        return PurePosixPath(relative_path).as_posix() in self.files

    def select(self, predicate: Callable[[str], bool]) -> tuple[str, ...]:
        return tuple(path for path in self.files if predicate(path))

    def by_name(
        self,
        names: Iterable[str],
        *,
        case_sensitive: bool = False,
    ) -> tuple[str, ...]:
        expected = set(names if case_sensitive else (name.lower() for name in names))
        return self.select(
            lambda path: (
                PurePosixPath(path).name
                if case_sensitive
                else PurePosixPath(path).name.lower()
            )
            in expected
        )

    def read_text(self, relative_path: str) -> str:
        """Read an indexed text file without traversal or oversized input."""

        normalized = PurePosixPath(relative_path).as_posix()
        if normalized not in self.files:
            return ""
        target = (self.root / normalized).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            return ""
        try:
            if target.stat().st_size > self.max_text_bytes:
                return ""
            return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            return ""

    def subtree(self, component_root: str) -> RepositoryScan:
        """Return a safe scan whose paths are relative to one component root."""

        normalized = _relative_path(component_root, "component_root", allow_root=True)
        if normalized == ".":
            return self
        prefix = f"{normalized}/"
        files = tuple(path.removeprefix(prefix) for path in self.files if path.startswith(prefix))
        root = (self.root / normalized).resolve()
        repository_root = self.root.resolve()
        if repository_root not in root.parents or not root.is_dir():
            raise ModelValidationError(f"component root is not an indexed directory: {normalized}")
        return RepositoryScan(
            root=root,
            files=files,
            skipped_files=self.skipped_files,
            truncated=self.truncated,
            max_text_bytes=self.max_text_bytes,
        )


@dataclass(frozen=True, slots=True)
class BuildMarker:
    path: str
    component_root: str
    build_system: str
    kind: str = "build"

    def __post_init__(self) -> None:
        _relative_path(self.path, "build_marker.path")
        _relative_path(self.component_root, "build_marker.component_root", allow_root=True)
        _require_text(self.build_system, "build_marker.build_system")
        _require_text(self.kind, "build_marker.kind")


@dataclass(frozen=True, slots=True)
class BuildMarkerIndex:
    markers: tuple[BuildMarker, ...]

    def __post_init__(self) -> None:
        identities = tuple((item.path, item.build_system, item.kind) for item in self.markers)
        if identities != tuple(sorted(set(identities))):
            raise ModelValidationError("build markers must be sorted and unique")

    def for_root(self, component_root: str) -> tuple[BuildMarker, ...]:
        return tuple(item for item in self.markers if item.component_root == component_root)

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(sorted({item.component_root for item in self.markers}))


@dataclass(frozen=True, slots=True)
class ComponentEvidence:
    kind: str
    path: str
    detail: str = ""
    weight: int = 0

    def __post_init__(self) -> None:
        _require_text(self.kind, "component_evidence.kind")
        _relative_path(self.path, "component_evidence.path")


@dataclass(frozen=True, slots=True)
class ComponentCandidate:
    component_id: str
    root: str
    build_systems: tuple[str, ...]
    build_entries: tuple[str, ...]
    languages: tuple[str, ...] = ()
    role: str = "candidate"
    evidence: tuple[ComponentEvidence, ...] = ()
    score: int = 0
    primary_eligible: bool = True

    def __post_init__(self) -> None:
        _require_text(self.component_id, "component.component_id")
        _relative_path(self.root, "component.root", allow_root=True)
        _require_text(self.role, "component.role")
        if not self.build_systems or any(not value.strip() for value in self.build_systems):
            raise ModelValidationError("component.build_systems must not be empty")
        if len(self.build_systems) != len(set(self.build_systems)):
            raise ModelValidationError("component.build_systems must be unique")
        if not self.build_entries:
            raise ModelValidationError("component.build_entries must not be empty")
        for path in self.build_entries:
            _relative_path(path, "component.build_entry")
        if len(self.build_entries) != len(set(self.build_entries)):
            raise ModelValidationError("component.build_entries must be unique")
        if len(self.languages) != len(set(self.languages)):
            raise ModelValidationError("component.languages must be unique")


@dataclass(frozen=True, slots=True)
class ComponentRelation:
    source_id: str
    target_id: str
    relation_type: str
    evidence: tuple[ComponentEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.source_id, "component_relation.source_id")
        _require_text(self.target_id, "component_relation.target_id")
        _require_text(self.relation_type, "component_relation.relation_type")
        if self.source_id == self.target_id:
            raise ModelValidationError("a component cannot relate to itself")


@dataclass(frozen=True, slots=True)
class ComponentGraph:
    candidates: tuple[ComponentCandidate, ...]
    relations: tuple[ComponentRelation, ...] = ()
    primary_component_id: str | None = None
    scan_truncated: bool = False

    def __post_init__(self) -> None:
        component_ids = tuple(item.component_id for item in self.candidates)
        if len(component_ids) != len(set(component_ids)):
            raise ModelValidationError("component IDs must be unique")
        known = set(component_ids)
        if self.primary_component_id is not None and self.primary_component_id not in known:
            raise ModelValidationError("primary component must exist in the graph")
        if self.primary is not None and not self.primary.primary_eligible:
            raise ModelValidationError("primary component must be eligible for selection")
        for relation in self.relations:
            if relation.source_id not in known or relation.target_id not in known:
                raise ModelValidationError("component relation references an unknown component")

    @property
    def primary(self) -> ComponentCandidate | None:
        return next(
            (
                candidate
                for candidate in self.candidates
                if candidate.component_id == self.primary_component_id
            ),
            None,
        )

    def ranked(self) -> tuple[ComponentCandidate, ...]:
        return tuple(
            sorted(
                self.candidates,
                key=lambda item: (-item.score, len(PurePosixPath(item.root).parts), item.root),
            )
        )
