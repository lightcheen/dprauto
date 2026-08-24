"""Bounded, deterministic, language-neutral project file scanning."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from dprauto.errors import ProjectParsingError


DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)


@dataclass(frozen=True, slots=True)
class ScannedProject:
    root: Path
    files: tuple[str, ...]
    skipped_files: int = 0
    truncated: bool = False
    max_text_bytes: int = 512 * 1024

    def has(self, relative_path: str) -> bool:
        return PurePosixPath(relative_path).as_posix() in self.files

    def select(self, predicate: Callable[[str], bool]) -> tuple[str, ...]:
        return tuple(path for path in self.files if predicate(path))

    def by_name(self, names: Iterable[str], *, case_sensitive: bool = False) -> tuple[str, ...]:
        expected = set(names if case_sensitive else (name.lower() for name in names))
        return self.select(
            lambda path: (
                PurePosixPath(path).name
                if case_sensitive
                else PurePosixPath(path).name.lower()
            )
            in expected
        )

    def read_text(self, relative_path: str) -> str:
        """Read a known project file without allowing path traversal or oversized input."""

        normalized = PurePosixPath(relative_path).as_posix()
        if normalized not in self.files:
            return ""
        target = (self.root / normalized).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            return ""
        try:
            if target.stat().st_size > self.max_text_bytes:
                return ""
            return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            return ""


class FileScanner:
    """Scan repository paths without interpreting any programming language."""

    def __init__(
        self,
        *,
        ignored_directories: frozenset[str] = DEFAULT_IGNORED_DIRECTORIES,
        max_files: int = 20_000,
        max_depth: int = 12,
        max_text_bytes: int = 512 * 1024,
    ) -> None:
        if max_files <= 0 or max_depth <= 0 or max_text_bytes <= 0:
            raise ValueError("scanner limits must be positive")
        self.ignored_directories = ignored_directories
        self.max_files = max_files
        self.max_depth = max_depth
        self.max_text_bytes = max_text_bytes

    def scan(self, root: Path) -> ScannedProject:
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise ProjectParsingError(f"project workspace is not a directory: {root}")

        discovered: list[str] = []
        skipped = 0
        truncated = False

        for current_root, directories, filenames in os.walk(root, followlinks=False):
            current = Path(current_root)
            relative_root = current.relative_to(root)
            depth = len(relative_root.parts)
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in self.ignored_directories
                and not (current / directory).is_symlink()
            )
            if depth >= self.max_depth:
                skipped += len(directories)
                directories[:] = []

            for filename in sorted(filenames):
                path = current / filename
                if path.is_symlink():
                    skipped += 1
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    skipped += 1
                    continue
                discovered.append(relative)
                if len(discovered) >= self.max_files:
                    truncated = True
                    directories[:] = []
                    break
            if truncated:
                break

        return ScannedProject(
            root=root,
            files=tuple(sorted(discovered)),
            skipped_files=skipped,
            truncated=truncated,
            max_text_bytes=self.max_text_bytes,
        )
