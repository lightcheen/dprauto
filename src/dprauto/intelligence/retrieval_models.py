"""Immutable contracts for semantic repository search and multi-turn context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from dprauto.errors import ModelValidationError
from dprauto.intelligence.models import KnowledgeNode, KnowledgeNodeKind


def _required(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ModelValidationError(f"{field_name} must not be empty")


def _relative_path(value: str, field_name: str) -> None:
    if value == ".":
        return
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ModelValidationError(f"{field_name} must be repository-relative")


@dataclass(frozen=True, slots=True)
class SemanticSearchQuery:
    text: str
    kinds: tuple[KnowledgeNodeKind, ...] = ()
    path_prefix: str = "."
    languages: tuple[str, ...] = ()
    limit: int = 8
    min_score: float = 0.08
    excluded_node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.text, "semantic query.text")
        _relative_path(self.path_prefix, "semantic query.path_prefix")
        if not 1 <= self.limit <= 20:
            raise ModelValidationError("semantic query.limit must be between 1 and 20")
        if not 0.0 <= self.min_score <= 1.0:
            raise ModelValidationError("semantic query.min_score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class SemanticSearchHit:
    node: KnowledgeNode
    score: float
    vector_score: float
    lexical_score: float
    graph_score: float
    matched_terms: tuple[str, ...] = ()
    related_nodes: tuple[KnowledgeNode, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("score", self.score),
            ("vector_score", self.vector_score),
            ("lexical_score", self.lexical_score),
            ("graph_score", self.graph_score),
        ):
            if not 0.0 <= value <= 1.0:
                raise ModelValidationError(f"semantic hit.{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ContextRetrievalTurn:
    number: int
    query: SemanticSearchQuery
    hits: tuple[SemanticSearchHit, ...] = ()
    context_characters: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ModelValidationError("context retrieval turn.number must be positive")
        if self.context_characters < 0:
            raise ModelValidationError(
                "context retrieval turn.context_characters must not be negative"
            )


@dataclass(frozen=True, slots=True)
class RepositoryContextSession:
    session_id: str
    graph_id: str
    turns: tuple[ContextRetrievalTurn, ...] = ()
    seen_node_ids: tuple[str, ...] = ()
    context: str = ""
    truncated: bool = False

    def __post_init__(self) -> None:
        _required(self.session_id, "repository context session.session_id")
        _required(self.graph_id, "repository context session.graph_id")
        if len(set(self.seen_node_ids)) != len(self.seen_node_ids):
            raise ModelValidationError(
                "repository context session.seen_node_ids must be unique"
            )


@dataclass(frozen=True, slots=True)
class ContextRetrievalResult:
    session: RepositoryContextSession
    turn: ContextRetrievalTurn
