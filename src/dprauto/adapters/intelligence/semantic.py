"""Dependency-free, code-aware semantic feature hashing for offline retrieval."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Iterable, Mapping

from dprauto.intelligence.terms import semantic_terms


_SEMANTIC_GROUPS = (
    ("build", "compile", "compiler", "make", "package", "wheel"),
    ("test", "tests", "testing", "pytest", "unittest", "ctest", "doctest"),
    ("run", "runtime", "start", "startup", "launch", "entrypoint", "command"),
    ("dependency", "dependencies", "requirement", "requirements", "package", "module"),
    ("import", "module", "package", "dependency"),
    ("optional", "extra", "plugin", "extension"),
    ("config", "configuration", "setting", "settings", "environment", "env"),
    (
        "database",
        "postgres",
        "postgresql",
        "psycopg",
        "psycopg2",
        "sql",
        "driver",
    ),
    ("cache", "redis"),
    ("service", "daemon", "server", "process"),
    ("directory", "path", "workspace", "workdir", "cwd"),
    ("repository", "repo", "monorepo", "project", "module"),
    ("install", "installation", "setup", "bootstrap", "provision"),
    ("error", "failure", "failed", "exception"),
    ("native", "system", "executable", "binary"),
)


def _synonyms() -> Mapping[str, tuple[str, ...]]:
    indexed: dict[str, set[str]] = defaultdict(set)
    for group in _SEMANTIC_GROUPS:
        for term in group:
            indexed[term].update(item for item in group if item != term)
    return {key: tuple(sorted(values)) for key, values in indexed.items()}


_SYNONYMS = _synonyms()


class CodeAwareHashingEncoder:
    """Produce normalized sparse vectors without models, network, or global state."""

    def __init__(self, *, dimensions: int = 2_048, max_terms: int = 2_000) -> None:
        if dimensions < 128:
            raise ValueError("semantic encoder dimensions must be >= 128")
        if max_terms <= 0:
            raise ValueError("semantic encoder max_terms must be positive")
        self.dimensions = dimensions
        self.max_terms = max_terms

    def encode(self, text: str) -> Mapping[int, float]:
        terms = semantic_terms(text)[: self.max_terms]
        features: dict[int, float] = defaultdict(float)
        self._add_features(features, terms, weight=1.0, namespace="term")
        self._add_features(
            features,
            (f"{left}::{right}" for left, right in zip(terms, terms[1:])),
            weight=0.55,
            namespace="bigram",
        )
        synonym_terms = (
            synonym
            for term in dict.fromkeys(terms)
            for synonym in _SYNONYMS.get(term, ())
        )
        self._add_features(
            features,
            synonym_terms,
            weight=0.35,
            namespace="term",
        )
        magnitude = math.sqrt(sum(value * value for value in features.values()))
        if magnitude == 0.0:
            return {}
        return {
            index: value / magnitude
            for index, value in sorted(features.items())
        }

    def _add_features(
        self,
        features: dict[int, float],
        values: Iterable[str],
        *,
        weight: float,
        namespace: str,
    ) -> None:
        for value in values:
            digest = hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            features[index] += sign * weight
