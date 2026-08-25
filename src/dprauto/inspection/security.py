"""Shared repository inspection policy for paths likely to contain credentials."""

from __future__ import annotations

import re
from pathlib import PurePosixPath


_SENSITIVE_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".p8", ".jks", ".keystore", ".kdbx"}
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "credentials.yml",
        "credentials.yaml",
        "secrets.json",
        "secrets.yml",
        "secrets.yaml",
        "secrets.properties",
    }
)
_SENSITIVE_WORD = re.compile(r"(?:^|[._-])(credential|credentials|secret|secrets)(?:[._-]|$)")


def is_sensitive_repository_path(relative_path: str) -> bool:
    """Conservatively identify secret-bearing files without opening them."""

    name = PurePosixPath(relative_path).name.casefold()
    safe_example = any(
        marker in name
        for marker in (
            ".example",
            ".sample",
            ".template",
        )
    )
    if safe_example:
        return False
    if name in _SENSITIVE_NAMES or name.startswith(".env."):
        return True
    if PurePosixPath(name).suffix in _SENSITIVE_SUFFIXES:
        return True
    return bool(_SENSITIVE_WORD.search(name))
