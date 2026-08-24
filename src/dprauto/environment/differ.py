"""Pure comparison of normalized environment snapshots."""

from __future__ import annotations

from collections.abc import Mapping

from dprauto.domain.enums import ChangeKind, RiskLevel
from dprauto.domain.models import (
    DependencyChange,
    EnvironmentDiff,
    EnvironmentSnapshot,
    FileChange,
    ValueChange,
)


_RISK_ORDER = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


def compare_environment_snapshots(
    before: EnvironmentSnapshot,
    after: EnvironmentSnapshot,
) -> EnvironmentDiff:
    build_scripts = _file_changes(before.build_scripts, after.build_scripts)
    business_source = _file_changes(before.business_source, after.business_source)
    system_packages = _dependency_changes(
        "system", before.system_packages, after.system_packages
    )
    python_dependencies = _dependency_changes(
        "python", before.python_dependencies, after.python_dependencies
    )
    environment_variables = _value_changes(
        before.environment_variables, after.environment_variables
    )
    startup_arguments = _value_changes(
        before.startup_arguments, after.startup_arguments
    )
    base_image = _single_value("base_image", before.base_image, after.base_image)
    python_version = _single_value(
        "python_version", before.python_version, after.python_version
    )
    all_dependencies = (*system_packages, *python_dependencies)

    risk = RiskLevel.NONE
    if build_scripts:
        risk = _max_risk(risk, RiskLevel.LOW)
    if (
        any(item.kind is ChangeKind.ADDED for item in all_dependencies)
        or environment_variables
        or startup_arguments
    ):
        risk = _max_risk(risk, RiskLevel.MEDIUM)
    if (
        any(item.kind in {ChangeKind.MODIFIED, ChangeKind.REMOVED} for item in all_dependencies)
        or base_image is not None
        or python_version is not None
    ):
        risk = _max_risk(risk, RiskLevel.HIGH)
    if business_source:
        risk = RiskLevel.CRITICAL

    policy_violations = tuple(
        f"business source changed: {item.path}" for item in business_source
    )
    files = (*build_scripts, *business_source)
    parts: list[str] = []
    if build_scripts:
        parts.append(f"{len(build_scripts)} build script change(s)")
    if system_packages:
        parts.append(f"{len(system_packages)} system package change(s)")
    if python_dependencies:
        parts.append(f"{len(python_dependencies)} Python dependency change(s)")
    if environment_variables:
        parts.append(f"{len(environment_variables)} environment variable change(s)")
    if startup_arguments:
        parts.append(f"{len(startup_arguments)} startup argument change(s)")
    if business_source:
        parts.append(f"{len(business_source)} business source change(s); manual review required")
    if base_image:
        parts.append("base image changed")
    if python_version:
        parts.append("Python version changed")

    return EnvironmentDiff(
        files=files,
        dependencies=all_dependencies,
        environment_variables=environment_variables,
        base_image=base_image,
        python_version=python_version,
        system_packages=system_packages,
        python_dependencies=python_dependencies,
        startup_arguments=startup_arguments,
        build_scripts=build_scripts,
        business_source=business_source,
        source_changed=bool(business_source),
        risk_level=risk,
        requires_manual_review=bool(business_source),
        policy_violations=policy_violations,
        summary="; ".join(parts) if parts else "no environment change",
    )


def _file_changes(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        kind = (
            ChangeKind.ADDED
            if old is None
            else ChangeKind.REMOVED
            if new is None
            else ChangeKind.MODIFIED
        )
        changes.append(FileChange(path, kind, old, new))
    return tuple(changes)


def _dependency_changes(
    ecosystem: str,
    before: Mapping[str, str | None],
    after: Mapping[str, str | None],
) -> tuple[DependencyChange, ...]:
    changes: list[DependencyChange] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if name in before and name in after and old == new:
            continue
        kind = (
            ChangeKind.ADDED
            if name not in before
            else ChangeKind.REMOVED
            if name not in after
            else ChangeKind.MODIFIED
        )
        changes.append(DependencyChange(ecosystem, name, kind, old, new))
    return tuple(changes)


def _value_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> tuple[ValueChange, ...]:
    return tuple(
        ValueChange(name, before.get(name), after.get(name))
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    )


def _single_value(name: str, before: str | None, after: str | None) -> ValueChange | None:
    return ValueChange(name, before, after) if before != after else None


def _max_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right
