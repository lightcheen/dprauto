"""Structured, minimal Dockerfile mutations for common environment repairs."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dprauto.agent.models import ToolContext, ToolResult
from dprauto.agent.tools.filesystem import (
    ModifyBuildScriptTool,
    _decode_utf8,
    _safe_target,
    _sha256_bytes,
    _source_sha256,
    _text_argument,
)
from dprauto.config import SecurityConfig
from dprauto.domain.enums import ChangeKind, RiskLevel
from dprauto.domain.models import DependencyChange, EnvironmentDiff
from dprauto.errors import ToolExecutionError
from dprauto.ports.storage import Storage
from dprauto.verification.overlay import (
    VERIFICATION_REQUIREMENTS_PATH,
    load_verification_requirements,
    render_verification_requirements,
)


_SYSTEM_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*(?:=[A-Za-z0-9+_.:~-]+)?$")
_PYTHON_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9.*+!_-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|>=|<=|>|<)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)
_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")
_CONTROLLED_VERIFICATION_RUNNERS = frozenset(
    {"pytest", "pytest-xdist", "tox", "nox"}
)
_FROM = re.compile(
    r"(?im)^(?P<prefix>\s*FROM\s+(?:--platform=\S+\s+)?)"
    r"(?P<image>[^\s]+)(?P<suffix>[^\n]*)$"
)


def _package_list(arguments: Mapping[str, Any], *, python: bool) -> tuple[str, ...]:
    values = arguments.get("packages")
    if (
        not isinstance(values, (list, tuple))
        or not values
        or len(values) > 12
        or any(not isinstance(item, str) or not item.strip() for item in values)
    ):
        raise ToolExecutionError("packages must contain between 1 and 12 non-empty strings")
    normalized = tuple(dict.fromkeys(item.strip() for item in values))
    pattern = _PYTHON_REQUIREMENT if python else _SYSTEM_PACKAGE
    invalid = tuple(item for item in normalized if not pattern.fullmatch(item))
    if invalid:
        kind = "Python requirement" if python else "system package"
        raise ToolExecutionError(
            f"invalid structured {kind} value(s): {', '.join(invalid)}"
        )
    return normalized


class _StructuredDockerfileTool:
    effect = "mutate"

    def __init__(
        self,
        storage: Storage,
        security: SecurityConfig | None = None,
    ) -> None:
        self.mutator = ModifyBuildScriptTool(storage, security)

    @staticmethod
    def _dockerfile(
        arguments: Mapping[str, Any], context: ToolContext
    ) -> tuple[str, str, str]:
        path = _text_argument(arguments, "path")
        if not PurePosixPath(path).name.casefold().startswith("dockerfile"):
            raise ToolExecutionError("structured environment patches require a Dockerfile path")
        _, target = _safe_target(context.workspace, path, require_file=True)
        source_bytes = target.read_bytes()
        return path, _decode_utf8(source_bytes, path), _sha256_bytes(source_bytes)

    def _replace(
        self,
        path: str,
        content: str,
        source_sha256: str,
        context: ToolContext,
        *,
        dimension: str,
        summary: str,
        dependencies: tuple[DependencyChange, ...] = (),
        risk_level: RiskLevel,
    ) -> ToolResult:
        result = self.mutator.invoke(
            {
                "path": path,
                "content": content,
                "source_sha256": source_sha256,
            },
            context,
        )
        base = result.environment_diff or EnvironmentDiff()
        if not result.data.get("changed", False):
            structured = base
        else:
            structured = replace(
                base,
                dependencies=dependencies,
                system_packages=(
                    dependencies if dimension == "system_packages" else ()
                ),
                python_dependencies=(
                    dependencies if dimension == "python_dependencies" else ()
                ),
                risk_level=risk_level,
                summary=summary,
            )
        return ToolResult(
            self.name,
            result.succeeded,
            summary if result.data.get("changed", False) else result.summary,
            data={**result.data, "structured_dimension": dimension},
            artifacts=result.artifacts,
            environment_diff=structured,
        )

    @staticmethod
    def _insert_after_final_from(content: str, block: str) -> str:
        matches = tuple(_FROM.finditer(content))
        if not matches:
            raise ToolExecutionError("Dockerfile has no FROM instruction to patch")
        insertion = matches[-1].end()
        return content[:insertion] + "\n" + block + content[insertion:]


class PatchSystemPackagesTool(_StructuredDockerfileTool):
    name = "patch_system_packages"
    description = (
        "Add a bounded list of literal OS package names to one generated Dockerfile block. "
        "No shell fragments or package-manager options are accepted."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "package_manager": {"type": "string", "minLength": 1},
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
        },
        "required": ["path", "package_manager", "packages"],
        "additionalProperties": False,
    }
    _MARKER = re.compile(
        r"(?m)^# dprauto structured-system-packages "
        r"manager=(?P<manager>[a-z]+) packages=(?P<packages>\[[^\n]*\])\n"
        r"RUN [^\n]*\n?"
    )

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path, content, source_sha256 = self._dockerfile(arguments, context)
        manager = _text_argument(arguments, "package_manager").casefold()
        if manager not in {"apt", "apk", "dnf", "yum"}:
            raise ToolExecutionError("package_manager must be one of apt, apk, dnf, yum")
        requested = _package_list(arguments, python=False)
        match = self._MARKER.search(content)
        existing: tuple[str, ...] = ()
        if match:
            if match.group("manager") != manager:
                raise ToolExecutionError(
                    "structured system-package block already uses a different package manager"
                )
            existing = tuple(json.loads(match.group("packages")))
        packages = tuple(dict.fromkeys((*existing, *requested)))
        block = self._block(manager, packages)
        if match:
            updated = content[: match.start()] + block + content[match.end() :]
        else:
            updated = self._insert_after_final_from(content, block)
        added = tuple(item for item in packages if item not in existing)
        changes = tuple(
            DependencyChange("system", self._name(item), ChangeKind.ADDED)
            for item in added
        )
        return self._replace(
            path,
            updated,
            source_sha256,
            context,
            dimension="system_packages",
            summary=f"added structured system packages via {manager}: {', '.join(added)}",
            dependencies=changes,
            risk_level=RiskLevel.MEDIUM,
        )

    @staticmethod
    def _block(manager: str, packages: tuple[str, ...]) -> str:
        rendered = " ".join(shlex.quote(item) for item in packages)
        marker = (
            "# dprauto structured-system-packages "
            f"manager={manager} packages={json.dumps(packages, separators=(',', ':'))}\n"
        )
        if manager == "apt":
            command = (
                "RUN apt-get update && apt-get install -y --no-install-recommends "
                f"{rendered} && rm -rf /var/lib/apt/lists/*\n"
            )
        elif manager == "apk":
            command = f"RUN apk add --no-cache {rendered}\n"
        else:
            command = f"RUN {manager} install -y {rendered} && {manager} clean all\n"
        return marker + command

    @staticmethod
    def _name(value: str) -> str:
        return value.split("=", 1)[0].casefold()


class PatchPythonDependenciesTool(_StructuredDockerfileTool):
    name = "patch_python_dependencies"
    description = (
        "Add bounded literal PEP-508-style package requirements to one generated pip-install "
        "Dockerfile block. URLs, markers, options and shell fragments are rejected."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
        },
        "required": ["path", "packages"],
        "additionalProperties": False,
    }
    _MARKER = re.compile(
        r"(?m)^# dprauto structured-python-dependencies "
        r"packages=(?P<packages>\[[^\n]*\])\nRUN [^\n]*\n?"
    )

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path, content, source_sha256 = self._dockerfile(arguments, context)
        requested = _package_list(arguments, python=True)
        match = self._MARKER.search(content)
        existing = tuple(json.loads(match.group("packages"))) if match else ()
        packages = tuple(dict.fromkeys((*existing, *requested)))
        block = self._block(packages)
        if match:
            updated = content[: match.start()] + block + content[match.end() :]
        else:
            updated = self._insert_after_final_from(content, block)
        added = tuple(item for item in packages if item not in existing)
        changes = tuple(
            DependencyChange(
                "python",
                self._name(item),
                ChangeKind.ADDED,
                after=self._constraint(item),
            )
            for item in added
        )
        return self._replace(
            path,
            updated,
            source_sha256,
            context,
            dimension="python_dependencies",
            summary=f"added structured Python dependencies: {', '.join(added)}",
            dependencies=changes,
            risk_level=RiskLevel.MEDIUM,
        )

    @staticmethod
    def _block(packages: tuple[str, ...]) -> str:
        marker = (
            "# dprauto structured-python-dependencies "
            f"packages={json.dumps(packages, separators=(',', ':'))}\n"
        )
        rendered = " ".join(shlex.quote(item) for item in packages)
        return marker + f"RUN python -m pip install --no-cache-dir {rendered}\n"

    @staticmethod
    def _name(value: str) -> str:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
        return match.group(0).replace("_", "-").casefold() if match else value.casefold()

    @staticmethod
    def _constraint(value: str) -> str | None:
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^]]+\])?", value)
        remainder = value[name.end() :].strip() if name else ""
        return remainder or None


class PatchVerificationDependenciesTool:
    """Add Python packages to the ephemeral Testability layer, not the image."""

    name = "patch_verification_dependencies"
    effect = "mutate"
    description = (
        "Add bounded literal Python requirements to the Testability-only overlay after a "
        "successful image build. The fixed overlay is installed only in temporary verification "
        "containers and never in the final runtime image. DPRAuto-managed test runners "
        "(pytest, pytest-xdist, tox and nox) cannot be changed through this tool."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
        },
        "required": ["packages"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        storage: Storage,
        security: SecurityConfig | None = None,
    ) -> None:
        self.mutator = ModifyBuildScriptTool(storage, security)

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        requested = _package_list(arguments, python=True)
        controlled = tuple(
            item
            for item in requested
            if PatchPythonDependenciesTool._name(item)
            in _CONTROLLED_VERIFICATION_RUNNERS
        )
        if controlled:
            raise ToolExecutionError(
                "verification overlay cannot override DPRAuto-managed test runner(s): "
                + ", ".join(controlled)
            )
        _, overlay_target = _safe_target(
            context.workspace,
            VERIFICATION_REQUIREMENTS_PATH,
            require_file=False,
        )
        overlay_sha256 = _source_sha256(overlay_target)
        try:
            existing = load_verification_requirements(Path(context.workspace))
            packages = tuple(dict.fromkeys((*existing, *requested)))
            content = render_verification_requirements(packages)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolExecutionError(str(exc)) from exc
        result = self.mutator.invoke(
            {
                "path": VERIFICATION_REQUIREMENTS_PATH,
                "content": content,
                "source_sha256": overlay_sha256,
            },
            context,
        )
        added = tuple(item for item in packages if item not in existing)
        changes = tuple(
            DependencyChange(
                "verification-python",
                PatchPythonDependenciesTool._name(item),
                ChangeKind.ADDED,
                after=PatchPythonDependenciesTool._constraint(item),
            )
            for item in added
        )
        base = result.environment_diff or EnvironmentDiff()
        structured = (
            base
            if not result.data.get("changed", False)
            else replace(
                base,
                dependencies=changes,
                python_dependencies=changes,
                risk_level=RiskLevel.LOW,
                summary=(
                    "added Testability-only Python dependencies: "
                    + ", ".join(added)
                ),
            )
        )
        return ToolResult(
            self.name,
            result.succeeded,
            structured.summary if result.data.get("changed", False) else result.summary,
            data={
                **result.data,
                "structured_dimension": "verification_python_dependencies",
                "overlay_path": VERIFICATION_REQUIREMENTS_PATH,
                "packages": packages,
            },
            artifacts=result.artifacts,
            environment_diff=structured,
        )


class PatchBaseImageTool(_StructuredDockerfileTool):
    name = "patch_base_image"
    description = (
        "Replace exactly one expected Dockerfile FROM image with one explicit tagged or "
        "digest-pinned image. The current image is a required precondition."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "current_image": {"type": "string", "minLength": 1},
            "replacement_image": {"type": "string", "minLength": 1},
        },
        "required": ["path", "current_image", "replacement_image"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path, content, source_sha256 = self._dockerfile(arguments, context)
        current = _text_argument(arguments, "current_image")
        replacement = _text_argument(arguments, "replacement_image")
        for label, image in (("current_image", current), ("replacement_image", replacement)):
            if not _IMAGE_REFERENCE.fullmatch(image) or image.endswith(":latest"):
                raise ToolExecutionError(
                    f"{label} must be a literal non-latest image tag or digest"
                )
        matches = tuple(
            match
            for match in _FROM.finditer(content)
            if match.group("image") == current
        )
        if len(matches) != 1:
            raise ToolExecutionError(
                f"current_image must match exactly one FROM instruction, found {len(matches)}"
            )
        match = matches[0]
        updated = content[: match.start("image")] + replacement + content[match.end("image") :]
        return self._replace(
            path,
            updated,
            source_sha256,
            context,
            dimension="runtime",
            summary=f"replaced base image {current} with {replacement}",
            risk_level=RiskLevel.HIGH,
        )
