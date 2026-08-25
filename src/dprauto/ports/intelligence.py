"""Ports for repository syntax parsing and knowledge graph persistence."""

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dprauto.intelligence.models import (
    KnowledgeNode,
    KnowledgeQuery,
    RepositoryKnowledgeGraph,
)


@runtime_checkable
class SyntaxTreeParser(Protocol):
    def supports_path(self, path: Path) -> bool:
        """Return whether this adapter can parse the file as source/config AST."""
        ...

    def language_for(self, path: Path) -> str:
        """Return the normalized Tree-sitter language name for a supported path."""
        ...

    def parse_bytes(self, path: Path, content: bytes) -> Any:
        """Parse bytes and return a Tree-sitter-compatible root node."""
        ...


@runtime_checkable
class KnowledgeGraphStore(Protocol):
    def replace(self, graph: RepositoryKnowledgeGraph) -> None:
        """Atomically replace one graph identity without affecting other graphs."""
        ...

    def load(self, graph_id: str) -> RepositoryKnowledgeGraph | None:
        """Load a complete graph, or return None when it is absent."""
        ...

    def query(self, graph_id: str, query: KnowledgeQuery) -> tuple[KnowledgeNode, ...]:
        """Return bounded nodes matching structured repository context filters."""
        ...

    def delete(self, graph_id: str) -> None:
        """Delete exactly one graph identity."""
        ...
