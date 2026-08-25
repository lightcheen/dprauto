"""Thread-safe in-memory knowledge graph store for tests and local fallback."""

from __future__ import annotations

from threading import RLock

from dprauto.intelligence.models import (
    KnowledgeNode,
    KnowledgeQuery,
    RepositoryKnowledgeGraph,
)


class InMemoryKnowledgeGraphStore:
    def __init__(self) -> None:
        self._graphs: dict[str, RepositoryKnowledgeGraph] = {}
        self._lock = RLock()

    def replace(self, graph: RepositoryKnowledgeGraph) -> None:
        with self._lock:
            self._graphs[graph.graph_id] = graph

    def load(self, graph_id: str) -> RepositoryKnowledgeGraph | None:
        with self._lock:
            return self._graphs.get(graph_id)

    def delete(self, graph_id: str) -> None:
        with self._lock:
            self._graphs.pop(graph_id, None)

    def query(self, graph_id: str, query: KnowledgeQuery) -> tuple[KnowledgeNode, ...]:
        with self._lock:
            graph = self._graphs.get(graph_id)
            if graph is None:
                return ()
            nodes = graph.nodes

        expected_text = query.text.casefold()
        expected_languages = {language.casefold() for language in query.languages}
        expected_types = {node_type.casefold() for node_type in query.node_types}
        matches = []
        for node in nodes:
            if query.kinds and node.kind not in query.kinds:
                continue
            if not _path_matches(node.path, query.path_prefix):
                continue
            if expected_languages and node.language.casefold() not in expected_languages:
                continue
            if expected_types and node.node_type.casefold() not in expected_types:
                continue
            haystack = "\n".join((node.path, node.node_type, node.symbol, node.text)).casefold()
            if expected_text and expected_text not in haystack:
                continue
            matches.append(node)
            if len(matches) >= query.limit:
                break
        return tuple(matches)


def _path_matches(path: str, prefix: str) -> bool:
    if prefix == ".":
        return True
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")
