"""Safe, workspace-persisted dependencies used only by Testability."""

from __future__ import annotations

import re
from pathlib import Path


VERIFICATION_REQUIREMENTS_PATH = ".dprauto/requirements-verification.txt"
_MAX_BYTES = 32 * 1024
_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9.*+!_-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)


def normalize_verification_requirements(
    values: tuple[str, ...] | list[str],
    *,
    require_nonempty: bool = True,
) -> tuple[str, ...]:
    """Validate bounded literal requirements without URLs, markers, or options."""

    if len(values) > 12:
        raise ValueError("verification overlay accepts at most 12 requirements")
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if require_nonempty and not normalized:
        raise ValueError("verification overlay requires at least one requirement")
    invalid = tuple(value for value in normalized if not _REQUIREMENT.fullmatch(value))
    if invalid:
        raise ValueError(
            "invalid verification requirement value(s): " + ", ".join(invalid)
        )
    return normalized


def load_verification_requirements(workspace: Path) -> tuple[str, ...]:
    """Load the fixed overlay path without following symlinks or oversized input."""

    root = workspace.expanduser().resolve()
    target = root / VERIFICATION_REQUIREMENTS_PATH
    if not target.exists():
        return ()
    if target.is_symlink() or not target.is_file():
        raise ValueError("verification requirements overlay is not a regular file")
    resolved = target.resolve()
    if root not in resolved.parents:
        raise ValueError("verification requirements overlay escapes the workspace")
    if target.stat().st_size > _MAX_BYTES:
        raise ValueError("verification requirements overlay exceeds size limit")
    lines = tuple(
        line.strip()
        for line in target.read_text(encoding="utf-8", errors="strict").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return normalize_verification_requirements(lines, require_nonempty=False)


def render_verification_requirements(values: tuple[str, ...]) -> str:
    normalized = normalize_verification_requirements(list(values))
    return "# DPRAuto Testability-only dependency overlay\n" + "\n".join(normalized) + "\n"
