"""Application service for repository intelligence indexing and retrieval."""

from __future__ import annotations

from pathlib import Path

from dprauto.adapters.intelligence import (
    InMemoryKnowledgeGraphStore,
    TreeSitterSyntaxParser,
)
from dprauto.domain.models import SourceReference
from dprauto.intelligence.builder import (
    KnowledgeGraphBuildConfig,
    RepositoryKnowledgeGraphBuilder,
)
from dprauto.intelligence.models import (
    KnowledgeNode,
    KnowledgeQuery,
    RepositoryKnowledgeGraph,
)
from dprauto.ports.intelligence import KnowledgeGraphStore, SyntaxTreeParser


class RepositoryIntelligenceService:
    """Coordinate bounded repository indexing with a replaceable graph store."""

    def __init__(
        self,
        builder: RepositoryKnowledgeGraphBuilder,
        store: KnowledgeGraphStore,
    ) -> None:
        self.builder = builder
        self.store = store

    def index(
        self,
        source: SourceReference,
        workspace: Path,
    ) -> RepositoryKnowledgeGraph:
        graph = self.builder.build(source, workspace.expanduser().resolve())
        self.store.replace(graph)
        return graph

    def load(self, graph_id: str) -> RepositoryKnowledgeGraph | None:
        return self.store.load(graph_id)

    def query(
        self,
        graph_id: str,
        query: KnowledgeQuery,
    ) -> tuple[KnowledgeNode, ...]:
        return self.store.query(graph_id, query)

    def delete(self, graph_id: str) -> None:
        self.store.delete(graph_id)


def create_repository_intelligence(
    store: KnowledgeGraphStore | None = None,
    config: KnowledgeGraphBuildConfig | None = None,
    syntax_parser: SyntaxTreeParser | None = None,
) -> RepositoryIntelligenceService:
    """Wire the bounded Tree-sitter builder to memory or a caller-owned store."""

    selected_store = store if store is not None else InMemoryKnowledgeGraphStore()
    selected_parser = syntax_parser if syntax_parser is not None else TreeSitterSyntaxParser()
    return RepositoryIntelligenceService(
        RepositoryKnowledgeGraphBuilder(selected_parser, config),
        selected_store,
    )
