"""Bounded repository language detection shared by multilingual parsers."""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
from typing import Mapping

from dprauto.domain.workspace import RepositoryScan


# Keep this mapping aligned with the language family inherited from HerAgent's
# Tree-sitter adapter. Detection itself has no optional parser dependency.
LANGUAGE_SUFFIXES: Mapping[str, str] = {
    ".bash": "Bash",
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".csh": "Bash",
    ".cxx": "C++",
    ".go": "Go",
    ".groovy": "Groovy",
    ".h": "C",
    ".hh": "C++",
    ".hpp": "C++",
    ".hxx": "C++",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Bash",
    ".sql": "SQL",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".yaml": "YAML",
    ".yml": "YAML",
}

LANGUAGE_ORDER = (
    "Python",
    "Java",
    "Kotlin",
    "Groovy",
    "C",
    "C++",
    "C#",
    "Go",
    "Rust",
    "JavaScript",
    "TypeScript",
    "PHP",
    "Ruby",
    "Bash",
    "SQL",
    "YAML",
)


class RepositoryLanguageDetector:
    """Count recognized source/config files without reading file contents."""

    def counts(self, scanned: RepositoryScan) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for relative_path in scanned.files:
            path = PurePosixPath(relative_path)
            language = LANGUAGE_SUFFIXES.get(path.suffix.casefold())
            if language:
                counts[language] += 1
            elif path.name.casefold() in {"bashrc", "profile"}:
                counts["Bash"] += 1
        return {
            language: counts[language]
            for language in LANGUAGE_ORDER
            if counts[language]
        }

    def languages(self, scanned: RepositoryScan) -> tuple[str, ...]:
        counts = self.counts(scanned)
        return tuple(
            sorted(
                counts,
                key=lambda language: (
                    -counts[language],
                    LANGUAGE_ORDER.index(language),
                ),
            )
        )
