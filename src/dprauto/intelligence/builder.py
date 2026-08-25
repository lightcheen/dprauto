"""Bounded whole-repository AST/text knowledge graph construction."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from dprauto.domain.models import SourceReference
from dprauto.inspection.scanner import FileScanner
from dprauto.intelligence.models import (
    KnowledgeEdge,
    KnowledgeEdgeKind,
    KnowledgeNode,
    KnowledgeNodeKind,
    RepositoryKnowledgeGraph,
)
from dprauto.ports.intelligence import SyntaxTreeParser


TEXT_SUFFIXES = {
    ".adoc",
    ".cfg",
    ".cmake",
    ".conf",
    ".gradle",
    ".ini",
    ".json",
    ".markdown",
    ".md",
    ".properties",
    ".rst",
    ".toml",
    ".txt",
    ".xml",
}
TEXT_FILENAMES = {
    "CMakeLists.txt",
    "Dockerfile",
    "Jenkinsfile",
    "Makefile",
    "Pipfile",
    "configure.ac",
    "configure.in",
    "pom.xml",
}

IMPORT_NODE_TYPES = {
    "import_statement",
    "import_from_statement",
    "aliased_import",
    "import_declaration",
    "package_declaration",
    "module_declaration",
    "use_declaration",
    "extern_crate_declaration",
    "mod_item",
    "package_clause",
    "source",
    "preproc_include",
}
DECLARATION_NODE_TYPES = {
    "class_definition",
    "function_definition",
    "decorated_definition",
    "class_declaration",
    "method_declaration",
    "constructor_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "function_declaration",
    "function_item",
    "struct_item",
    "enum_item",
    "trait_item",
    "impl_item",
    "type_definition",
    "namespace_definition",
    "template_declaration",
}


@dataclass(frozen=True, slots=True)
class KnowledgeGraphBuildConfig:
    max_files: int = 5_000
    max_depth: int = 16
    max_file_bytes: int = 512 * 1024
    max_ast_depth: int = 10
    max_ast_nodes_per_file: int = 2_000
    max_total_nodes: int = 100_000
    max_node_text_characters: int = 2_000
    text_chunk_characters: int = 2_000
    text_chunk_overlap: int = 200

    def __post_init__(self) -> None:
        positive = (
            self.max_files,
            self.max_depth,
            self.max_file_bytes,
            self.max_ast_depth,
            self.max_ast_nodes_per_file,
            self.max_total_nodes,
            self.max_node_text_characters,
            self.text_chunk_characters,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("knowledge graph build limits must be positive")
        if not 0 <= self.text_chunk_overlap < self.text_chunk_characters:
            raise ValueError("text chunk overlap must be smaller than chunk size")


class RepositoryKnowledgeGraphBuilder:
    """Index repository structure, Tree-sitter AST, declarations, and text."""

    def __init__(
        self,
        syntax_parser: SyntaxTreeParser,
        config: KnowledgeGraphBuildConfig | None = None,
    ) -> None:
        self.syntax_parser = syntax_parser
        self.config = config or KnowledgeGraphBuildConfig()
        self.scanner = FileScanner(
            max_files=self.config.max_files,
            max_depth=self.config.max_depth,
            max_text_bytes=self.config.max_file_bytes,
        )

    def build(
        self,
        source: SourceReference,
        workspace: Path,
    ) -> RepositoryKnowledgeGraph:
        scanned = self.scanner.scan(workspace)
        selected_paths = tuple(
            path for path in scanned.files if self._supports_path(Path(path))
        )
        fingerprint, readable_paths, skipped_large = self._fingerprint(
            scanned.root,
            selected_paths,
        )
        graph_id = hashlib.sha256(
            "\0".join((source.locator, source.revision or "", fingerprint)).encode("utf-8")
        ).hexdigest()
        nodes: list[KnowledgeNode] = []
        edges: list[KnowledgeEdge] = []
        root_id = self._node_id(graph_id, "repository:.")
        nodes.append(
            KnowledgeNode(
                node_id=root_id,
                kind=KnowledgeNodeKind.REPOSITORY,
                path=".",
                symbol=scanned.root.name,
                metadata={"source_locator": source.locator, "revision": source.revision or ""},
            )
        )
        directory_ids = {".": root_id}
        language_counts: Counter[str] = Counter()
        ast_truncated_files: list[str] = []
        parse_error_files: list[str] = []
        text_truncated_files: list[str] = []
        text_file_count = 0
        graph_truncated = False

        for path_index, relative_path in enumerate(readable_paths):
            structural_upper_bound = len(PurePosixPath(relative_path).parts)
            if len(nodes) + structural_upper_bound > self.config.max_total_nodes:
                graph_truncated = True
                break
            target = scanned.root / relative_path
            content = self._read_bounded(target)
            if content is None:
                continue
            parent_id = self._ensure_directories(
                graph_id,
                relative_path,
                directory_ids,
                nodes,
                edges,
            )
            file_id = self._node_id(graph_id, f"file:{relative_path}")
            language = (
                self.syntax_parser.language_for(Path(relative_path))
                if self.syntax_parser.supports_path(Path(relative_path))
                else "text"
            )
            language_counts[language] += 1
            nodes.append(
                KnowledgeNode(
                    node_id=file_id,
                    kind=KnowledgeNodeKind.FILE,
                    path=relative_path,
                    language=language,
                    symbol=PurePosixPath(relative_path).name,
                    metadata={"size_bytes": len(content)},
                )
            )
            edges.append(KnowledgeEdge(parent_id, file_id, KnowledgeEdgeKind.CONTAINS))
            if self.syntax_parser.supports_path(Path(relative_path)):
                truncated, has_error = self._add_ast(
                    graph_id,
                    relative_path,
                    file_id,
                    language,
                    content,
                    nodes,
                    edges,
                    self.config.max_total_nodes - len(nodes),
                )
                if truncated:
                    ast_truncated_files.append(relative_path)
                if has_error:
                    parse_error_files.append(relative_path)
            else:
                text_truncated = self._add_text_chunks(
                    graph_id,
                    relative_path,
                    file_id,
                    content,
                    nodes,
                    edges,
                    self.config.max_total_nodes - len(nodes),
                )
                if text_truncated:
                    text_truncated_files.append(relative_path)
                text_file_count += 1
            if (
                len(nodes) >= self.config.max_total_nodes
                and path_index + 1 < len(readable_paths)
            ):
                graph_truncated = True
                break

        return RepositoryKnowledgeGraph(
            graph_id=graph_id,
            source=source,
            source_fingerprint=fingerprint,
            root_node_id=root_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            metadata={
                "builder": "tree-sitter-knowledge-graph-v1",
                "selected_file_count": len(selected_paths),
                "indexed_file_count": len(readable_paths),
                "text_file_count": text_file_count,
                "language_file_counts": dict(sorted(language_counts.items())),
                "skipped_large_files": skipped_large,
                "parse_error_files": tuple(parse_error_files),
                "ast_truncated_files": tuple(ast_truncated_files),
                "text_truncated_files": tuple(text_truncated_files),
                "graph_truncated": graph_truncated,
                "scan_skipped_files": scanned.skipped_files,
                "scan_truncated": scanned.truncated,
                "limits": {
                    "max_files": self.config.max_files,
                    "max_file_bytes": self.config.max_file_bytes,
                    "max_ast_depth": self.config.max_ast_depth,
                    "max_ast_nodes_per_file": self.config.max_ast_nodes_per_file,
                    "max_total_nodes": self.config.max_total_nodes,
                },
            },
        )

    def _supports_path(self, path: Path) -> bool:
        return (
            self.syntax_parser.supports_path(path)
            or path.suffix.casefold() in TEXT_SUFFIXES
            or path.name in TEXT_FILENAMES
            or path.name.startswith("Dockerfile.")
        )

    def _fingerprint(
        self,
        root: Path,
        paths: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...], int]:
        digest = hashlib.sha256()
        readable: list[str] = []
        skipped_large = 0
        for relative_path in paths:
            content = self._read_bounded(root / relative_path)
            if content is None:
                skipped_large += 1
                continue
            readable.append(relative_path)
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
        return digest.hexdigest(), tuple(readable), skipped_large

    def _read_bounded(self, path: Path) -> bytes | None:
        try:
            if not path.is_file() or path.stat().st_size > self.config.max_file_bytes:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def _ensure_directories(
        self,
        graph_id: str,
        relative_path: str,
        directory_ids: dict[str, str],
        nodes: list[KnowledgeNode],
        edges: list[KnowledgeEdge],
    ) -> str:
        parent = PurePosixPath(relative_path).parent
        if parent.as_posix() == ".":
            return directory_ids["."]
        current = PurePosixPath()
        parent_id = directory_ids["."]
        for part in parent.parts:
            current /= part
            path = current.as_posix()
            existing = directory_ids.get(path)
            if existing:
                parent_id = existing
                continue
            node_id = self._node_id(graph_id, f"directory:{path}")
            nodes.append(
                KnowledgeNode(
                    node_id=node_id,
                    kind=KnowledgeNodeKind.DIRECTORY,
                    path=path,
                    symbol=part,
                )
            )
            edges.append(KnowledgeEdge(parent_id, node_id, KnowledgeEdgeKind.CONTAINS))
            directory_ids[path] = node_id
            parent_id = node_id
        return parent_id

    def _add_ast(
        self,
        graph_id: str,
        relative_path: str,
        file_id: str,
        language: str,
        content: bytes,
        nodes: list[KnowledgeNode],
        edges: list[KnowledgeEdge],
        remaining_node_budget: int,
    ) -> tuple[bool, bool]:
        root = self.syntax_parser.parse_bytes(Path(relative_path), content)
        root_has_error = bool(getattr(root, "has_error", False))
        stack: list[tuple[Any, str | None, int, int]] = [(root, None, 0, 0)]
        saved = 0
        truncated = False
        while stack:
            tree_node, parent_id, depth, ordinal = stack.pop()
            if depth > self.config.max_ast_depth:
                truncated = True
                continue
            if saved >= min(self.config.max_ast_nodes_per_file, remaining_node_budget):
                truncated = True
                break
            node_type = str(tree_node.type)
            role = self._node_role(node_type)
            kind = (
                KnowledgeNodeKind.DECLARATION
                if role
                else KnowledgeNodeKind.AST
            )
            identity = (
                f"ast:{relative_path}:{tree_node.start_byte}:{tree_node.end_byte}:"
                f"{node_type}:{depth}:{parent_id or 'root'}:{ordinal}"
            )
            node_id = self._node_id(graph_id, identity)
            raw_text = content[tree_node.start_byte : tree_node.end_byte]
            text, text_truncated = self._bounded_text(raw_text)
            symbol = self._symbol(tree_node, content, text, role)
            metadata = {
                "depth": depth,
                "has_error": bool(getattr(tree_node, "has_error", False)),
                "text_truncated": text_truncated,
            }
            if role:
                metadata["role"] = role
            nodes.append(
                KnowledgeNode(
                    node_id=node_id,
                    kind=kind,
                    path=relative_path,
                    language=language,
                    node_type=node_type,
                    symbol=symbol,
                    start_line=tree_node.start_point[0] + 1,
                    end_line=tree_node.end_point[0] + 1,
                    start_column=tree_node.start_point[1],
                    end_column=tree_node.end_point[1],
                    text=text,
                    metadata=metadata,
                )
            )
            if parent_id is None:
                edges.append(KnowledgeEdge(file_id, node_id, KnowledgeEdgeKind.HAS_AST))
            else:
                edges.append(KnowledgeEdge(parent_id, node_id, KnowledgeEdgeKind.PARENT_OF))
            if role:
                edges.append(KnowledgeEdge(file_id, node_id, KnowledgeEdgeKind.DECLARES))
            saved += 1
            children = tuple(getattr(tree_node, "named_children", ()))
            for child_ordinal, child in reversed(tuple(enumerate(children))):
                stack.append((child, node_id, depth + 1, child_ordinal))
        return truncated, root_has_error

    @staticmethod
    def _node_role(node_type: str) -> str:
        if node_type in IMPORT_NODE_TYPES:
            return "import"
        if node_type in DECLARATION_NODE_TYPES or node_type.endswith(
            ("_declaration", "_definition")
        ):
            return "declaration"
        return ""

    def _symbol(self, tree_node: Any, content: bytes, text: str, role: str) -> str:
        if not role:
            return ""
        name_node = None
        child_by_field_name = getattr(tree_node, "child_by_field_name", None)
        if callable(child_by_field_name):
            name_node = child_by_field_name("name")
        if name_node is not None:
            value = content[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            return value[:200]
        return " ".join(text.split())[:200]

    def _bounded_text(self, content: bytes) -> tuple[str, bool]:
        decoded = content.decode("utf-8", errors="replace")
        limit = self.config.max_node_text_characters
        return decoded[:limit], len(decoded) > limit

    def _add_text_chunks(
        self,
        graph_id: str,
        relative_path: str,
        file_id: str,
        content: bytes,
        nodes: list[KnowledgeNode],
        edges: list[KnowledgeEdge],
        remaining_node_budget: int,
    ) -> bool:
        text = content.decode("utf-8", errors="replace")
        chunk_size = self.config.text_chunk_characters
        step = chunk_size - self.config.text_chunk_overlap
        previous_id: str | None = None
        for index, start in enumerate(range(0, len(text) or 1, step)):
            if index >= remaining_node_budget:
                return True
            chunk = text[start : start + chunk_size]
            if not chunk and text:
                continue
            node_id = self._node_id(graph_id, f"text:{relative_path}:{index}:{start}")
            start_line = text.count("\n", 0, start) + 1
            end_line = start_line + chunk.count("\n")
            nodes.append(
                KnowledgeNode(
                    node_id=node_id,
                    kind=KnowledgeNodeKind.TEXT,
                    path=relative_path,
                    language="text",
                    node_type="text_chunk",
                    start_line=start_line,
                    end_line=end_line,
                    text=chunk,
                    metadata={"chunk_index": index, "start_character": start},
                )
            )
            edges.append(KnowledgeEdge(file_id, node_id, KnowledgeEdgeKind.HAS_TEXT))
            if previous_id:
                edges.append(KnowledgeEdge(previous_id, node_id, KnowledgeEdgeKind.NEXT_CHUNK))
            previous_id = node_id
            if start + chunk_size >= len(text):
                break
        return False

    @staticmethod
    def _node_id(graph_id: str, identity: str) -> str:
        return hashlib.sha256(f"{graph_id}\0{identity}".encode("utf-8")).hexdigest()
