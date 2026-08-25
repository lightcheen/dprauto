"""Immutable, backend-neutral repository knowledge graph models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping

from dprauto.domain.models import SourceReference
from dprauto.errors import ModelValidationError


class KnowledgeNodeKind(str, Enum):
    REPOSITORY = "repository"
    DIRECTORY = "directory"
    FILE = "file"
    AST = "ast"
    DECLARATION = "declaration"
    TEXT = "text"


class KnowledgeEdgeKind(str, Enum):
    CONTAINS = "contains"
    HAS_AST = "has_ast"
    PARENT_OF = "parent_of"
    DECLARES = "declares"
    HAS_TEXT = "has_text"
    NEXT_CHUNK = "next_chunk"


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ModelValidationError(f"{field_name} must not be empty")


def _validate_path(value: str, field_name: str) -> None:
    if value == ".":
        return
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ModelValidationError(f"{field_name} must be a safe repository-relative path")


@dataclass(frozen=True, slots=True)
class KnowledgeNode:
    node_id: str
    kind: KnowledgeNodeKind
    path: str
    language: str = ""
    node_type: str = ""
    symbol: str = ""
    start_line: int | None = None
    end_line: int | None = None
    start_column: int | None = None
    end_column: int | None = None
    text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.node_id, "knowledge node.node_id")
        _validate_path(self.path, "knowledge node.path")
        positions = (
            self.start_line,
            self.end_line,
            self.start_column,
            self.end_column,
        )
        if any(value is not None and value < 0 for value in positions):
            raise ModelValidationError("knowledge node source positions must not be negative")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ModelValidationError("knowledge node.end_line precedes start_line")


@dataclass(frozen=True, slots=True)
class KnowledgeEdge:
    source_id: str
    target_id: str
    kind: KnowledgeEdgeKind
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.source_id, "knowledge edge.source_id")
        _require_text(self.target_id, "knowledge edge.target_id")
        if self.source_id == self.target_id:
            raise ModelValidationError("knowledge graph self-edges are not allowed")


@dataclass(frozen=True, slots=True)
class RepositoryKnowledgeGraph:
    graph_id: str
    source: SourceReference
    source_fingerprint: str
    root_node_id: str
    nodes: tuple[KnowledgeNode, ...]
    edges: tuple[KnowledgeEdge, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.graph_id, "knowledge graph.graph_id")
        _require_text(self.source_fingerprint, "knowledge graph.source_fingerprint")
        _require_text(self.root_node_id, "knowledge graph.root_node_id")
        if not self.nodes:
            raise ModelValidationError("knowledge graph.nodes must not be empty")
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ModelValidationError("knowledge graph node IDs must be unique")
        if self.root_node_id not in node_ids:
            raise ModelValidationError("knowledge graph root node is missing")
        edge_keys = {(edge.source_id, edge.target_id, edge.kind) for edge in self.edges}
        if len(edge_keys) != len(self.edges):
            raise ModelValidationError("knowledge graph edges must be unique")
        for edge in self.edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise ModelValidationError("knowledge graph edge references an unknown node")


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    kinds: tuple[KnowledgeNodeKind, ...] = ()
    path_prefix: str = "."
    languages: tuple[str, ...] = ()
    node_types: tuple[str, ...] = ()
    text: str = ""
    limit: int = 20

    def __post_init__(self) -> None:
        _validate_path(self.path_prefix, "knowledge query.path_prefix")
        if not 1 <= self.limit <= 200:
            raise ModelValidationError("knowledge query.limit must be between 1 and 200")
