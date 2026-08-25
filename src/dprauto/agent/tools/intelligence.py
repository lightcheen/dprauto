"""Read-only semantic repository context tool for bounded Agent investigation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from dprauto.adapters.intelligence import CodeAwareHashingEncoder, TreeSitterSyntaxParser
from dprauto.agent.models import ToolContext, ToolResult
from dprauto.domain.models import SourceReference
from dprauto.errors import ToolExecutionError
from dprauto.intelligence.builder import (
    KnowledgeGraphBuildConfig,
    RepositoryKnowledgeGraphBuilder,
)
from dprauto.intelligence.models import KnowledgeNodeKind, RepositoryKnowledgeGraph
from dprauto.intelligence.retrieval import (
    MultiTurnRepositoryContext,
    RepositoryRetrievalConfig,
    RepositorySemanticRetriever,
)
from dprauto.intelligence.retrieval_models import SemanticSearchQuery


class QueryRepositoryContextTool:
    name = "query_repository_context"
    effect = "observe"
    description = (
        "Search code, configuration, and documentation through the bounded Tree-sitter "
        "repository graph. Repeated calls accumulate novel multi-turn context and avoid "
        "returning the same nodes. It never executes project code."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "path_prefix": {"type": "string", "minLength": 1},
            "languages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    _ALLOWED_KINDS = frozenset(
        {
            KnowledgeNodeKind.FILE,
            KnowledgeNodeKind.AST,
            KnowledgeNodeKind.DECLARATION,
            KnowledgeNodeKind.TEXT,
        }
    )

    def __init__(
        self,
        builder: RepositoryKnowledgeGraphBuilder | None = None,
        context_retriever: MultiTurnRepositoryContext | None = None,
        *,
        max_query_characters: int = 1_000,
        max_results: int = 12,
        max_cached_graphs: int = 16,
    ) -> None:
        if min(max_query_characters, max_results, max_cached_graphs) <= 0:
            raise ValueError("repository context tool limits must be positive")
        retrieval_config = RepositoryRetrievalConfig()
        self.builder = (
            builder
            if builder is not None
            else RepositoryKnowledgeGraphBuilder(
                TreeSitterSyntaxParser(),
                KnowledgeGraphBuildConfig(
                    max_files=2_000,
                    max_depth=16,
                    max_ast_depth=8,
                    max_ast_nodes_per_file=500,
                    max_total_nodes=20_000,
                ),
            )
        )
        self.context_retriever = (
            context_retriever
            if context_retriever is not None
            else MultiTurnRepositoryContext(
                RepositorySemanticRetriever(
                    CodeAwareHashingEncoder(),
                    retrieval_config,
                ),
                retrieval_config,
            )
        )
        self.max_query_characters = max_query_characters
        self.max_results = max_results
        self.max_cached_graphs = max_cached_graphs
        self._graphs: dict[tuple[str, int, str], RepositoryKnowledgeGraph] = {}
        self._lock = RLock()

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        query_text = arguments.get("query")
        if not isinstance(query_text, str) or not query_text.strip():
            raise ToolExecutionError("repository context query must not be empty")
        if len(query_text) > self.max_query_characters:
            raise ToolExecutionError(
                "repository context query exceeds maximum character limit"
            )
        raw_limit = arguments.get("limit", min(8, self.max_results))
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
            raise ToolExecutionError("repository context limit must be an integer")
        limit = raw_limit
        if not 1 <= limit <= self.max_results:
            raise ToolExecutionError(
                f"repository context limit must be between 1 and {self.max_results}"
            )
        languages = _bounded_strings(arguments.get("languages", ()), "languages", 8)
        kind_values = _bounded_strings(arguments.get("kinds", ()), "kinds", 4)
        try:
            kinds = tuple(KnowledgeNodeKind(value.casefold()) for value in kind_values)
        except ValueError as exc:
            raise ToolExecutionError(
                "repository context kinds must be file, ast, declaration, or text"
            ) from exc
        if any(kind not in self._ALLOWED_KINDS for kind in kinds):
            raise ToolExecutionError(
                "repository context kinds must be file, ast, declaration, or text"
            )
        workspace = Path(context.workspace).expanduser().resolve()
        graph = self._graph(context, workspace)
        session_id = hashlib.sha256(
            f"{context.run_id}\0{workspace}".encode("utf-8")
        ).hexdigest()[:24]
        result = self.context_retriever.query(
            session_id,
            graph,
            SemanticSearchQuery(
                text=query_text,
                kinds=kinds,
                path_prefix=str(arguments.get("path_prefix", ".")),
                languages=languages,
                limit=limit,
            ),
        )
        hits = tuple(_hit_payload(hit) for hit in result.turn.hits)
        return ToolResult(
            self.name,
            True,
            f"retrieved {len(hits)} new repository context nodes on turn "
            f"{result.turn.number}",
            data={
                "query": query_text,
                "graph_id": graph.graph_id,
                "session_id": session_id,
                "turn": result.turn.number,
                "hits": hits,
                "context": result.session.context,
                "new_context_characters": result.turn.context_characters,
                "seen_node_count": len(result.session.seen_node_ids),
                "truncated": result.session.truncated,
                "graph_truncated": bool(graph.metadata.get("graph_truncated")),
                "scan_truncated": bool(graph.metadata.get("scan_truncated")),
            },
        )

    def _graph(
        self,
        context: ToolContext,
        workspace: Path,
    ) -> RepositoryKnowledgeGraph:
        key = (context.run_id, context.attempt_number, str(workspace))
        with self._lock:
            cached = self._graphs.get(key)
            if cached is not None:
                return cached
        source = (
            context.project_profile.source
            if context.project_profile is not None
            else SourceReference("agent-workspace")
        )
        graph = self.builder.build(source, workspace)
        with self._lock:
            self._graphs[key] = graph
            while len(self._graphs) > self.max_cached_graphs:
                self._graphs.pop(next(iter(self._graphs)))
        return graph


def _bounded_strings(value: Any, name: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ToolExecutionError(f"repository context {name} must be an array")
    if len(value) > limit:
        raise ToolExecutionError(f"repository context {name} exceeds maximum of {limit}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ToolExecutionError(f"repository context {name} must contain non-empty strings")
    return tuple(item.strip() for item in value)


def _hit_payload(hit: Any) -> Mapping[str, Any]:
    node = hit.node
    return {
        "node_id": node.node_id,
        "path": node.path,
        "kind": node.kind.value,
        "language": node.language,
        "node_type": node.node_type,
        "symbol": node.symbol,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "score": round(hit.score, 6),
        "vector_score": round(hit.vector_score, 6),
        "lexical_score": round(hit.lexical_score, 6),
        "graph_score": round(hit.graph_score, 6),
        "matched_terms": hit.matched_terms,
        "related_paths": tuple(
            dict.fromkeys(item.path for item in hit.related_nodes if item.path != node.path)
        ),
    }
