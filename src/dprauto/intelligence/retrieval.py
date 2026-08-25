"""Bounded hybrid semantic search and multi-turn repository context retrieval."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Mapping

from dprauto.errors import ModelValidationError
from dprauto.intelligence.models import (
    KnowledgeNode,
    KnowledgeNodeKind,
    RepositoryKnowledgeGraph,
)
from dprauto.intelligence.retrieval_models import (
    ContextRetrievalResult,
    ContextRetrievalTurn,
    RepositoryContextSession,
    SemanticSearchHit,
    SemanticSearchQuery,
)
from dprauto.intelligence.terms import semantic_terms
from dprauto.ports.intelligence import SemanticEncoder


@dataclass(frozen=True, slots=True)
class RepositoryRetrievalConfig:
    max_index_nodes: int = 20_000
    max_document_characters: int = 4_000
    max_results_per_path: int = 3
    max_related_nodes: int = 3
    max_session_turns: int = 4
    max_session_nodes: int = 32
    max_context_characters: int = 12_000
    max_context_characters_per_hit: int = 1_600
    max_cached_indexes: int = 16
    max_sessions: int = 64

    def __post_init__(self) -> None:
        values = (
            self.max_index_nodes,
            self.max_document_characters,
            self.max_results_per_path,
            self.max_related_nodes,
            self.max_session_turns,
            self.max_session_nodes,
            self.max_context_characters,
            self.max_context_characters_per_hit,
            self.max_cached_indexes,
            self.max_sessions,
        )
        if any(value <= 0 for value in values):
            raise ValueError("repository retrieval limits must be positive")
        if self.max_context_characters_per_hit > self.max_context_characters:
            raise ValueError("per-hit context limit exceeds total context limit")


@dataclass(frozen=True, slots=True)
class _IndexedNode:
    node: KnowledgeNode
    document: str
    folded_document: str
    terms: frozenset[str]
    vector: Mapping[int, float]


@dataclass(frozen=True, slots=True)
class _RepositoryIndex:
    graph_id: str
    entries: tuple[_IndexedNode, ...]
    nodes: Mapping[str, KnowledgeNode]
    adjacency: Mapping[str, tuple[str, ...]]


class RepositorySemanticRetriever:
    """Rank code/config/document nodes using vectors, lexical evidence, and edges."""

    _KIND_PRIORITY = {
        KnowledgeNodeKind.DECLARATION: 0,
        KnowledgeNodeKind.TEXT: 1,
        KnowledgeNodeKind.FILE: 2,
        KnowledgeNodeKind.AST: 3,
    }
    _KIND_BOOST = {
        KnowledgeNodeKind.DECLARATION: 0.05,
        KnowledgeNodeKind.TEXT: 0.04,
        KnowledgeNodeKind.FILE: 0.03,
        KnowledgeNodeKind.AST: 0.02,
    }

    def __init__(
        self,
        encoder: SemanticEncoder,
        config: RepositoryRetrievalConfig | None = None,
    ) -> None:
        self.encoder = encoder
        self.config = config or RepositoryRetrievalConfig()
        self._indexes: dict[str, _RepositoryIndex] = {}
        self._lock = RLock()

    def search(
        self,
        graph: RepositoryKnowledgeGraph,
        query: SemanticSearchQuery,
    ) -> tuple[SemanticSearchHit, ...]:
        index = self._index(graph)
        query_terms = frozenset(semantic_terms(query.text))
        query_vector = self.encoder.encode(query.text)
        expected_languages = {value.casefold() for value in query.languages}
        excluded = set(query.excluded_node_ids)
        all_scores: dict[str, tuple[float, float, float, tuple[str, ...]]] = {}

        for entry in index.entries:
            node = entry.node
            if node.node_id in excluded:
                continue
            if not _path_matches(node.path, query.path_prefix):
                continue
            if expected_languages and node.language.casefold() not in expected_languages:
                continue
            vector_score = max(0.0, min(1.0, _dot(query_vector, entry.vector)))
            lexical_score, matched = _lexical_score(
                query.text,
                query_terms,
                entry.folded_document,
                entry.terms,
            )
            base = min(1.0, 0.68 * vector_score + 0.32 * lexical_score)
            all_scores[node.node_id] = (
                base,
                vector_score,
                lexical_score,
                matched,
            )

        scored: list[SemanticSearchHit] = []
        for node_id, (base, vector_score, lexical_score, matched) in all_scores.items():
            node = index.nodes[node_id]
            if query.kinds and node.kind not in query.kinds:
                continue
            graph_score = max(
                (
                    all_scores[neighbor][0]
                    for neighbor in index.adjacency.get(node_id, ())
                    if neighbor in all_scores
                ),
                default=0.0,
            )
            score = min(
                1.0,
                0.88 * base
                + 0.07 * graph_score
                + self._KIND_BOOST.get(node.kind, 0.0),
            )
            if score < query.min_score:
                continue
            related = tuple(
                index.nodes[neighbor]
                for neighbor in index.adjacency.get(node_id, ())
                if neighbor in index.nodes
                and index.nodes[neighbor].kind
                not in {KnowledgeNodeKind.REPOSITORY, KnowledgeNodeKind.DIRECTORY}
            )[: self.config.max_related_nodes]
            scored.append(
                SemanticSearchHit(
                    node=node,
                    score=score,
                    vector_score=vector_score,
                    lexical_score=lexical_score,
                    graph_score=graph_score,
                    matched_terms=matched,
                    related_nodes=related,
                )
            )

        scored.sort(
            key=lambda hit: (
                -hit.score,
                self._KIND_PRIORITY.get(hit.node.kind, 99),
                hit.node.path,
                hit.node.start_line or 0,
                hit.node.node_id,
            )
        )
        selected: list[SemanticSearchHit] = []
        per_path: dict[str, int] = {}
        for hit in scored:
            count = per_path.get(hit.node.path, 0)
            if count >= self.config.max_results_per_path:
                continue
            selected.append(hit)
            per_path[hit.node.path] = count + 1
            if len(selected) >= query.limit:
                break
        return tuple(selected)

    def _index(self, graph: RepositoryKnowledgeGraph) -> _RepositoryIndex:
        with self._lock:
            cached = self._indexes.get(graph.graph_id)
            if cached is not None:
                return cached

        nodes = {node.node_id: node for node in graph.nodes}
        adjacency_values: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in graph.edges:
            adjacency_values[edge.source_id].add(edge.target_id)
            adjacency_values[edge.target_id].add(edge.source_id)
        adjacency = {
            node_id: tuple(sorted(neighbors))
            for node_id, neighbors in adjacency_values.items()
        }
        candidates = self._candidate_nodes(graph)
        entries = []
        for node in candidates:
            document = _node_document(node)[: self.config.max_document_characters]
            entries.append(
                _IndexedNode(
                    node=node,
                    document=document,
                    folded_document=document.casefold(),
                    terms=frozenset(semantic_terms(document)),
                    vector=self.encoder.encode(document),
                )
            )
        built = _RepositoryIndex(graph.graph_id, tuple(entries), nodes, adjacency)
        with self._lock:
            self._indexes[graph.graph_id] = built
            while len(self._indexes) > self.config.max_cached_indexes:
                self._indexes.pop(next(iter(self._indexes)))
        return built

    def _candidate_nodes(
        self,
        graph: RepositoryKnowledgeGraph,
    ) -> tuple[KnowledgeNode, ...]:
        eligible = tuple(
            node
            for node in graph.nodes
            if node.kind in self._KIND_PRIORITY and _node_document(node).strip()
        )
        by_kind = {
            kind: sorted(
                (node for node in eligible if node.kind is kind),
                key=lambda node: (
                    node.path,
                    node.start_line or 0,
                    node.node_id,
                ),
            )
            for kind in self._KIND_PRIORITY
        }
        limit = self.config.max_index_nodes
        fractions = {
            KnowledgeNodeKind.DECLARATION: 0.45,
            KnowledgeNodeKind.TEXT: 0.20,
            KnowledgeNodeKind.FILE: 0.10,
            KnowledgeNodeKind.AST: 0.25,
        }
        selected: list[KnowledgeNode] = []
        for kind in self._KIND_PRIORITY:
            quota = max(1, int(limit * fractions[kind]))
            selected.extend(by_kind[kind][:quota])
        selected = selected[:limit]
        selected_ids = {node.node_id for node in selected}
        if len(selected) < limit:
            remaining = sorted(
                (node for node in eligible if node.node_id not in selected_ids),
                key=lambda node: (
                    self._KIND_PRIORITY[node.kind],
                    node.path,
                    node.start_line or 0,
                    node.node_id,
                ),
            )
            selected.extend(remaining[: limit - len(selected)])
        return tuple(selected)


class MultiTurnRepositoryContext:
    """Accumulate novel semantic results under strict turn/node/character budgets."""

    def __init__(
        self,
        retriever: RepositorySemanticRetriever,
        config: RepositoryRetrievalConfig | None = None,
    ) -> None:
        self.retriever = retriever
        self.config = config or retriever.config
        self._sessions: dict[str, RepositoryContextSession] = {}
        self._lock = RLock()

    def query(
        self,
        session_id: str,
        graph: RepositoryKnowledgeGraph,
        query: SemanticSearchQuery,
    ) -> ContextRetrievalResult:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.graph_id != graph.graph_id:
                session = RepositoryContextSession(session_id, graph.graph_id)
            if len(session.turns) >= self.config.max_session_turns:
                raise ModelValidationError(
                    "repository context session reached its maximum query turns"
                )
            remaining_nodes = self.config.max_session_nodes - len(session.seen_node_ids)
            if remaining_nodes <= 0:
                raise ModelValidationError(
                    "repository context session reached its maximum unique nodes"
                )
            bounded_query = replace(
                query,
                limit=min(query.limit, remaining_nodes),
                excluded_node_ids=tuple(
                    dict.fromkeys((*query.excluded_node_ids, *session.seen_node_ids))
                ),
            )
            candidates = self.retriever.search(graph, bounded_query)
            remaining_characters = (
                self.config.max_context_characters - len(session.context)
            )
            hits: list[SemanticSearchHit] = []
            fragments: list[str] = []
            truncated = False
            for hit in candidates:
                if remaining_characters <= 0:
                    truncated = True
                    break
                fragment = self._render_hit(hit)
                if len(fragment) > remaining_characters:
                    if remaining_characters < 160:
                        truncated = True
                        break
                    fragment = fragment[:remaining_characters]
                    truncated = True
                hits.append(hit)
                fragments.append(fragment)
                remaining_characters -= len(fragment)
                if truncated:
                    break
            added_context = "\n\n".join(fragments)
            combined_context = session.context
            if added_context:
                combined_context = (
                    f"{combined_context}\n\n{added_context}"
                    if combined_context
                    else added_context
                )
            turn = ContextRetrievalTurn(
                number=len(session.turns) + 1,
                query=bounded_query,
                hits=tuple(hits),
                context_characters=len(added_context),
                truncated=truncated,
            )
            seen = tuple(
                dict.fromkeys(
                    (*session.seen_node_ids, *(hit.node.node_id for hit in hits))
                )
            )
            updated = RepositoryContextSession(
                session_id=session.session_id,
                graph_id=graph.graph_id,
                turns=(*session.turns, turn),
                seen_node_ids=seen,
                context=combined_context,
                truncated=session.truncated or truncated,
            )
            self._sessions.pop(session_id, None)
            self._sessions[session_id] = updated
            while len(self._sessions) > self.config.max_sessions:
                self._sessions.pop(next(iter(self._sessions)))
            return ContextRetrievalResult(updated, turn)

    def load(self, session_id: str) -> RepositoryContextSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _render_hit(self, hit: SemanticSearchHit) -> str:
        node = hit.node
        location = node.path
        if node.start_line is not None:
            location += f":{node.start_line}"
            if node.end_line is not None and node.end_line != node.start_line:
                location += f"-{node.end_line}"
        label = node.symbol or node.node_type or node.kind.value
        text = node.text.strip() or node.symbol or node.path
        text = text[: self.config.max_context_characters_per_hit]
        related = ", ".join(
            dict.fromkeys(item.path for item in hit.related_nodes if item.path != node.path)
        )
        related_line = f"\nrelated: {related}" if related else ""
        return (
            f"[{location} | {node.kind.value} | {label} | score={hit.score:.3f}]"
            f"{related_line}\n{text}"
        )


def _node_document(node: KnowledgeNode) -> str:
    return "\n".join(
        value
        for value in (
            node.path,
            node.language,
            node.node_type,
            node.symbol,
            node.text,
        )
        if value
    )


def _path_matches(path: str, prefix: str) -> bool:
    if prefix == ".":
        return True
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _dot(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def _lexical_score(
    query_text: str,
    query_terms: frozenset[str],
    folded_document: str,
    document_terms: frozenset[str],
) -> tuple[float, tuple[str, ...]]:
    if not query_terms:
        return 0.0, ()
    matched = tuple(sorted(query_terms & document_terms))
    overlap = len(matched) / len(query_terms)
    phrase = query_text.casefold().strip()
    exact = 1.0 if phrase and phrase in folded_document else 0.0
    return min(1.0, 0.85 * overlap + 0.15 * exact), matched
