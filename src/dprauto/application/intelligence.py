"""Application service for repository intelligence indexing and retrieval."""

from __future__ import annotations

from pathlib import Path

from dprauto.adapters.intelligence import (
    CodeAwareHashingEncoder,
    InMemoryKnowledgeGraphStore,
    TreeSitterSyntaxParser,
)
from dprauto.errors import AdapterError
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
from dprauto.intelligence.retrieval import (
    MultiTurnRepositoryContext,
    RepositoryRetrievalConfig,
    RepositorySemanticRetriever,
)
from dprauto.intelligence.retrieval_models import (
    ContextRetrievalResult,
    SemanticSearchQuery,
)
from dprauto.ports.intelligence import (
    KnowledgeGraphStore,
    SemanticEncoder,
    SyntaxTreeParser,
)


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


class RepositoryContextRetrievalService:
    """Index repositories and query stored graphs through bounded context sessions."""

    def __init__(
        self,
        intelligence: RepositoryIntelligenceService,
        sessions: MultiTurnRepositoryContext,
    ) -> None:
        self.intelligence = intelligence
        self.sessions = sessions

    def index(
        self,
        source: SourceReference,
        workspace: Path,
    ) -> RepositoryKnowledgeGraph:
        return self.intelligence.index(source, workspace)

    def query(
        self,
        graph_id: str,
        session_id: str,
        query: SemanticSearchQuery,
    ) -> ContextRetrievalResult:
        graph = self.intelligence.load(graph_id)
        if graph is None:
            raise AdapterError(f"repository knowledge graph does not exist: {graph_id}")
        return self.sessions.query(session_id, graph, query)

    def delete(self, graph_id: str) -> None:
        self.intelligence.delete(graph_id)


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


def create_repository_context_retrieval(
    store: KnowledgeGraphStore | None = None,
    graph_config: KnowledgeGraphBuildConfig | None = None,
    retrieval_config: RepositoryRetrievalConfig | None = None,
    syntax_parser: SyntaxTreeParser | None = None,
    semantic_encoder: SemanticEncoder | None = None,
) -> RepositoryContextRetrievalService:
    """Compose graph indexing with offline semantic and multi-turn retrieval."""

    intelligence = create_repository_intelligence(
        store=store,
        config=graph_config,
        syntax_parser=syntax_parser,
    )
    selected_config = retrieval_config or RepositoryRetrievalConfig()
    retriever = RepositorySemanticRetriever(
        (
            semantic_encoder
            if semantic_encoder is not None
            else CodeAwareHashingEncoder()
        ),
        selected_config,
    )
    return RepositoryContextRetrievalService(
        intelligence,
        MultiTurnRepositoryContext(retriever, selected_config),
    )
