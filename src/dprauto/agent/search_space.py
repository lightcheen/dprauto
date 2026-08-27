"""Deterministically bound repair tools before asking an LLM to plan."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from dprauto.domain.enums import BuildStage, FailureCategory
from dprauto.domain.models import FailureInfo

_KNOWN_REPAIR_TOOLS = {
    "modify_build_script",
    "patch_base_image",
    "patch_build_script",
    "patch_python_dependencies",
    "patch_system_packages",
    "patch_verification_dependencies",
}

_PYTHON_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "importerror: cannot import name",
    "distributionnotfound",
    "packagenotfounderror",
    "resolutionimpossible",
    "versionconflict",
    "could not find a version that satisfies",
    "no matching distribution found",
)
_SYSTEM_MARKERS = (
    "command not found",
    "executable file not found",
    "fatal error:",
    "cannot find -l",
    "pkg-config",
)
_RUNTIME_MARKERS = (
    "requires-python",
    "unsupported python version",
    "python version is not supported",
    "invalid base image",
    "manifest unknown",
)
_PLUGIN_MARKER = re.compile(r"fixture\s+['\"][^'\"]+['\"]\s+not found")
_IMPORT_DISTRIBUTIONS = {
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "git": "gitpython",
    "pil": "pillow",
    "pytest_mock": "pytest-mock",
    "requests_cache": "requests-cache",
    "requests_ratelimiter": "requests-ratelimiter",
    "yaml": "pyyaml",
}


def bounded_repair_search_space(
    failure: FailureInfo,
    specifications: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return auditable search metadata and the failure-relevant tool subset."""

    mutating = {
        name: specification
        for name, specification in specifications.items()
        if isinstance(specification, Mapping)
        and specification.get("effect") == "mutate"
    }
    evidence = "\n".join((*failure.evidence, failure.message, failure.key_log)).casefold()
    candidates = dependency_candidates(failure)
    signals: list[str] = []
    preferred: list[str] = []

    if failure.failure_stage is BuildStage.TEST:
        direct_dependency = any(marker in evidence for marker in _PYTHON_MARKERS)
        plugin_failure = "unrecognized arguments:" in evidence or bool(
            _PLUGIN_MARKER.search(evidence)
        )
        compatibility_warning = "deprecationwarning" in evidence and (
            ("please use" in evidence and "import" in evidence)
            or "deprecated" in evidence
        )
        managed_runner_conflict = (
            "resolutionimpossible" in evidence
            and bool(candidates)
            and set(candidates) <= {"nox", "pytest", "pytest-xdist", "tox"}
        )
        if managed_runner_conflict:
            allowed = set()
            signals.append(
                "controlled test-runner conflict requires deterministic dependency selection"
            )
        elif direct_dependency or plugin_failure or compatibility_warning:
            allowed = {"patch_verification_dependencies"}
            preferred.append("patch_verification_dependencies")
            if direct_dependency:
                signals.append("test-only dependency evidence")
            elif plugin_failure:
                signals.append("pytest plugin or fixture evidence")
            else:
                signals.append("collection-time dependency compatibility warning")
        else:
            allowed = set()
            signals.append("test failure has no dependency-shaped repair evidence")
    elif _is_cost_timeout(failure):
        allowed = {"patch_build_script", "modify_build_script"}
        preferred.append("patch_build_script")
        signals.append("build/install timeout permits scope reduction only")
    elif failure.category in {
        FailureCategory.PYTHON_DEPENDENCY,
        FailureCategory.PACKAGE_DEPENDENCY,
        FailureCategory.DEPENDENCY_CONFLICT,
    } or any(marker in evidence for marker in _PYTHON_MARKERS):
        allowed = {
            "patch_python_dependencies",
            "patch_build_script",
            "modify_build_script",
        }
        preferred.append("patch_python_dependencies")
        signals.append("Python package dependency evidence")
    elif failure.category in {
        FailureCategory.SYSTEM_DEPENDENCY,
        FailureCategory.TOOLCHAIN,
    } or any(marker in evidence for marker in _SYSTEM_MARKERS):
        allowed = {
            "patch_system_packages",
            "patch_build_script",
            "modify_build_script",
        }
        preferred.append("patch_system_packages")
        signals.append("system package or executable evidence")
    elif failure.category is FailureCategory.RUNTIME_VERSION or any(
        marker in evidence for marker in _RUNTIME_MARKERS
    ):
        allowed = {"patch_base_image", "patch_build_script", "modify_build_script"}
        preferred.append("patch_base_image")
        signals.append("runtime or base-image compatibility evidence")
    else:
        allowed = set(_KNOWN_REPAIR_TOOLS) - {"patch_verification_dependencies"}
        signals.append("generic environment failure retains bounded build tools")

    selected = {
        name: specification
        for name, specification in mutating.items()
        if name in allowed
    }
    excluded = {
        name: _exclusion_reason(name, failure)
        for name in sorted(mutating)
        if name not in selected
    }
    metadata = {
        "failure_stage": failure.failure_stage.value,
        "failure_category": failure.category.value,
        "allowed_tools": tuple(selected),
        "preferred_tools": tuple(name for name in preferred if name in selected),
        "excluded_tools": excluded,
        "evidence_signals": tuple(signals),
        "dependency_candidates": candidates,
    }
    return metadata, selected


def failure_family(failure: FailureInfo | None) -> str:
    """Normalize volatile fingerprints into a causal no-progress identity."""

    if failure is None:
        return "succeeded"
    evidence = "\n".join((*failure.evidence, failure.message, failure.key_log)).casefold()
    missing = re.search(r"no module named\s+['\"]?([a-z0-9_.-]+)", evidence)
    if missing:
        detail = f"missing-module:{missing.group(1).split('.', 1)[0]}"
    else:
        exception = re.search(r"\b([a-z][a-z0-9_]*(?:error|exception))\b", evidence)
        detail = exception.group(1) if exception else failure.message.casefold()[:120]
        detail = re.sub(r"\b(?:0x[0-9a-f]+|\d+(?:\.\d+)?)\b", "#", detail)
        detail = re.sub(r"\s+", " ", detail).strip()
    return ":".join(
        (
            failure.failure_stage.value,
            failure.category.value,
            failure.kind.value,
            detail,
        )
    )


def dependency_candidates(failure: FailureInfo) -> tuple[str, ...]:
    """Extract bounded package candidates named by Testability evidence."""

    evidence = "\n".join((*failure.evidence, failure.message, failure.key_log)).casefold()
    roots: list[str] = []
    patterns = (
        r"no module named\s+['\"]?([a-z0-9_.-]+)",
        r"please use\s+`?import\s+([a-z0-9_.-]+)",
        r"importerror:\s+cannot import name[^\n]+from\s+['\"]([a-z0-9_.-]+)",
        r"try running[^\n]*pip\s+install\s+['\"]?([a-z0-9][a-z0-9_.-]*)",
        r"cannot install\s+([a-z0-9][a-z0-9_.-]*)",
    )
    for pattern in patterns:
        roots.extend(match.split(".", 1)[0] for match in re.findall(pattern, evidence))
    distributions = []
    for root in roots:
        normalized = root.replace("-", "_")
        distribution = _IMPORT_DISTRIBUTIONS.get(normalized, normalized.replace("_", "-"))
        if distribution not in distributions:
            distributions.append(distribution)
    return tuple(distributions[:16])


def _is_cost_timeout(failure: FailureInfo) -> bool:
    if failure.failure_stage not in {
        BuildStage.BUILD,
        BuildStage.DEPENDENCY_INSTALLATION,
    }:
        return False
    evidence = "\n".join((*failure.evidence, failure.message)).casefold()
    return "timed out" in evidence or "timeout_profile=" in evidence


def _exclusion_reason(name: str, failure: FailureInfo) -> str:
    if failure.failure_stage is BuildStage.TEST:
        return "runtime/build mutation is outside the Testability overlay scope"
    if _is_cost_timeout(failure):
        return "timeout repair may narrow cost but may not expand dependencies or runtime"
    return "tool does not match the classified failure evidence"
