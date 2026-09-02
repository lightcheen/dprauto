"""Bounded, deterministic, language-neutral project file scanning."""

from __future__ import annotations

import os
from pathlib import Path

from dprauto.domain.workspace import RepositoryScan
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

    def scan(self, root: Path) -> RepositoryScan:
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

        return RepositoryScan(
            root=root,
            files=tuple(sorted(discovered)),
            skipped_files=skipped,
            truncated=truncated,
            max_text_bytes=self.max_text_bytes,
        )
