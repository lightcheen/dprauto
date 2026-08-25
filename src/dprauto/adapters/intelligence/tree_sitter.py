"""Lazy, cached Tree-sitter adapter using HerAgent's compatible language bundle."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any

from dprauto.errors import AdapterError


TREE_SITTER_LANGUAGES = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "c_sharp",
    ".csh": "bash",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".yaml": "yaml",
    ".yml": "yaml",
}


class TreeSitterSyntaxParser:
    """Parse supported code/config files without importing grammars at startup."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}
        self._lock = RLock()

    def supports_path(self, path: Path) -> bool:
        return path.suffix.casefold() in TREE_SITTER_LANGUAGES

    def language_for(self, path: Path) -> str:
        language = TREE_SITTER_LANGUAGES.get(path.suffix.casefold())
        if not language:
            raise AdapterError(
                f"Tree-sitter does not support file type: {path.suffix or path.name}"
            )
        return language

    def parse_bytes(self, path: Path, content: bytes) -> Any:
        language = self.language_for(path)
        with self._lock:
            parser = self._parser_for(language)
            try:
                return parser.parse(content).root_node
            except Exception as exc:
                raise AdapterError(f"Tree-sitter failed to parse {path}: {exc}") from exc

    def _parser_for(self, language: str) -> Any:
        with self._lock:
            cached = self._parsers.get(language)
            if cached is not None:
                return cached
            try:
                from tree_sitter_languages import get_parser
            except ImportError as exc:
                raise AdapterError(
                    "Tree-sitter support requires tree-sitter==0.21.3 and "
                    "tree-sitter-languages==1.10.2"
                ) from exc
            try:
                parser = get_parser(language)
            except Exception as exc:
                raise AdapterError(
                    f"Tree-sitter grammar is unavailable for language {language}: {exc}"
                ) from exc
            self._parsers[language] = parser
            return parser
