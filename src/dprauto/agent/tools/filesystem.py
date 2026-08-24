"""Bounded project inspection and policy-controlled file mutation tools."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dprauto.agent.models import ToolContext, ToolResult
from dprauto.config import SecurityConfig
from dprauto.domain.enums import ChangeKind, RiskLevel
from dprauto.domain.models import EnvironmentDiff, FileChange, SourceReference, ValueChange
from dprauto.errors import PolicyViolationError, ToolExecutionError
from dprauto.inspection.scanner import FileScanner
from dprauto.ports.parser import ProjectParser
from dprauto.ports.storage import Storage


def _text_argument(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"tool argument {name!r} must be a non-empty string")
    return value


def _safe_target(workspace: str, relative_path: str, *, require_file: bool) -> tuple[Path, Path]:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ToolExecutionError(f"tool workspace is not a directory: {root}")
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() == ".":
        raise ToolExecutionError(f"tool path must be a safe relative path: {relative_path!r}")
    candidate = root / relative.as_posix()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ToolExecutionError(f"tool path contains a symbolic link: {relative_path!r}")
    target = candidate.resolve()
    if target != root and root not in target.parents:
        raise ToolExecutionError(f"tool path escapes workspace: {relative_path!r}")
    if require_file and not target.is_file():
        raise ToolExecutionError(f"project file does not exist: {relative_path}")
    return root, target


def _is_sensitive_project_path(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name.casefold()
    safe_example = name.endswith((".example", ".sample", ".template"))
    if name == ".env" or (name.startswith(".env.") and not safe_example):
        return True
    return PurePosixPath(name).suffix in {".key", ".pem", ".p12", ".pfx"}


class InspectProjectTool:
    name = "inspect_project"
    effect = "observe"
    description = "Parse the workspace into a normalized ProjectProfile without executing it."
    argument_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "minLength": 1},
            "revision": {"type": "string", "minLength": 1},
            "subdirectory": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }

    def __init__(self, parser: ProjectParser) -> None:
        self.parser = parser

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        source = (
            context.project_profile.source
            if context.project_profile is not None
            else SourceReference(
                str(arguments.get("source", "agent-workspace")),
                revision=(
                    str(arguments["revision"])
                    if arguments.get("revision") is not None
                    else None
                ),
                subdirectory=(
                    str(arguments["subdirectory"])
                    if arguments.get("subdirectory") is not None
                    else None
                ),
            )
        )
        profile = self.parser.parse(source, Path(context.workspace))
        return ToolResult(
            self.name,
            True,
            f"identified {profile.project_type.value} project with "
            f"{len(profile.build_files)} build files",
            data={"project_profile": profile},
        )


class ListProjectFilesTool:
    name = "list_project_files"
    effect = "observe"
    description = "List bounded workspace-relative project file paths without reading content."
    argument_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, scanner: FileScanner | None = None, *, max_files: int = 2_000) -> None:
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        self.scanner = scanner or FileScanner()
        self.max_files = max_files

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        scanned = self.scanner.scan(Path(context.workspace))
        files = tuple(scanned.files[: self.max_files])
        truncated = scanned.truncated or len(scanned.files) > len(files)
        return ToolResult(
            self.name,
            True,
            f"listed {len(files)} bounded project files",
            data={
                "files": files,
                "truncated": truncated,
                "skipped_files": scanned.skipped_files,
            },
        )


class ReadFileTool:
    name = "read_file"
    effect = "observe"
    description = (
        "Read up to 400 lines from one UTF-8 project file using a safe workspace-relative "
        "path. Use optional inclusive start_line/end_line to page through large files; the "
        "result reports total_lines, truncation, and next_start_line."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, *, max_bytes: int = 256 * 1024, max_lines: int = 400) -> None:
        if max_bytes <= 0 or max_lines <= 0:
            raise ValueError("read file bounds must be positive")
        self.max_bytes = max_bytes
        self.max_lines = max_lines

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path = _text_argument(arguments, "path")
        if _is_sensitive_project_path(path):
            raise PolicyViolationError(f"refused to read sensitive project file: {path}")
        _, target = _safe_target(context.workspace, path, require_file=True)
        if target.stat().st_size > self.max_bytes:
            raise ToolExecutionError(f"project file exceeds read limit: {path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if not lines:
            return ToolResult(
                self.name,
                True,
                f"read empty file {path}",
                data={
                    "path": path,
                    "content": "",
                    "start_line": 0,
                    "end_line": 0,
                    "total_lines": 0,
                    "truncated": False,
                    "next_start_line": None,
                },
            )
        start_line = int(arguments.get("start_line", 1))
        requested_end = int(arguments.get("end_line", total_lines))
        if requested_end < start_line:
            raise ToolExecutionError("read_file end_line must be >= start_line")
        if start_line > total_lines:
            raise ToolExecutionError(
                f"read_file start_line {start_line} exceeds {path} total lines {total_lines}"
            )
        if requested_end - start_line + 1 > self.max_lines:
            if "end_line" in arguments:
                raise ToolExecutionError(
                    f"read_file range exceeds maximum of {self.max_lines} lines"
                )
            requested_end = start_line + self.max_lines - 1
        end_line = min(total_lines, requested_end)
        selected = "".join(lines[start_line - 1 : end_line])
        truncated = start_line > 1 or end_line < total_lines
        return ToolResult(
            self.name,
            True,
            f"read {path} lines {start_line}-{end_line} of {total_lines}",
            data={
                "path": path,
                "content": selected,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "truncated": truncated,
                "next_start_line": end_line + 1 if end_line < total_lines else None,
            },
        )


class SearchProjectTool:
    name = "search_project"
    effect = "observe"
    description = (
        "Search bounded project text files for a literal query without running shell commands."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "case_sensitive": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        scanner: FileScanner | None = None,
        *,
        max_matches: int = 50,
    ) -> None:
        if max_matches <= 0:
            raise ValueError("max_matches must be positive")
        self.scanner = scanner or FileScanner()
        self.max_matches = max_matches

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        query = _text_argument(arguments, "query")
        case_sensitive = bool(arguments.get("case_sensitive", False))
        scanned = self.scanner.scan(Path(context.workspace))
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        for path in scanned.files:
            if _is_sensitive_project_path(path):
                continue
            content = scanned.read_text(path)
            for number, line in enumerate(content.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append({"path": path, "line": number, "text": line[:500]})
                    if len(matches) >= self.max_matches:
                        break
            if len(matches) >= self.max_matches:
                break
        return ToolResult(
            self.name,
            True,
            f"found {len(matches)} bounded matches for {query!r}",
            data={"query": query, "matches": tuple(matches)},
        )


class ModifyBuildScriptTool:
    name = "modify_build_script"
    effect = "mutate"
    description = (
        "Replace a policy-allowed build script (normally Dockerfile or setup.sh) and persist "
        "a unified diff. Business source is denied by default."
    )
    argument_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    _APT_NETWORK_OPTIONS = (
        "-o Acquire::Retries=0 "
        "-o Acquire::http::Timeout=15 "
        "-o Acquire::https::Timeout=15"
    )
    _APT_DNS_OPTIONS = 'RES_OPTIONS="attempts:1 timeout:2"'

    def __init__(
        self,
        storage: Storage,
        security: SecurityConfig | None = None,
        *,
        max_bytes: int = 512 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.storage = storage
        self.security = security or SecurityConfig()
        self.max_bytes = max_bytes

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        path = _text_argument(arguments, "path")
        content = _text_argument(arguments, "content")
        requested_content = content
        content = self._bound_apt_network_waits(path, content)
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ToolExecutionError(f"replacement exceeds mutation limit: {path}")
        is_build_script = any(
            fnmatch.fnmatchcase(PurePosixPath(path).as_posix(), pattern)
            for pattern in self.security.allowed_mutation_globs
        )
        if not is_build_script and not self.security.allow_source_changes:
            raise PolicyViolationError(
                f"business source mutation is disabled; refused to modify {path}"
            )

        _, target = _safe_target(context.workspace, path, require_file=False)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ToolExecutionError(f"mutation target is not a regular file: {path}")
        before = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if before == content:
            return ToolResult(
                self.name,
                True,
                f"{path} already has the requested content",
                data={
                    "path": path,
                    "changed": False,
                    "bounded_apt_network_retries": content != requested_content,
                },
                environment_diff=EnvironmentDiff(summary=f"no change to {path}"),
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        previous_mode = target.stat().st_mode if target.exists() else None
        descriptor, temporary_name = tempfile.mkstemp(prefix=".dprauto-agent-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if previous_mode is not None:
                os.chmod(temporary, previous_mode)
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ToolExecutionError(f"failed to modify {path}: {exc}") from exc

        before_digest = hashlib.sha256(before.encode()).hexdigest() if before else None
        after_digest = hashlib.sha256(encoded).hexdigest()
        patch = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path)
        artifact = self.storage.save(
            f"agent-runs/{context.run_id}/rounds/{context.attempt_number:02d}/"
            f"diffs/{safe_name}-{after_digest[:12]}.patch",
            patch.encode(),
            media_type="text/x-diff; charset=utf-8",
        )
        is_dockerfile = target.name.lower().startswith("dockerfile")
        before_image = self._base_image(before) if is_dockerfile else None
        after_image = self._base_image(content) if is_dockerfile else None
        file_change = FileChange(
            path=PurePosixPath(path).as_posix(),
            kind=ChangeKind.MODIFIED if before else ChangeKind.ADDED,
            before_digest=before_digest,
            after_digest=after_digest,
        )
        source_changed = not is_build_script
        environment_diff = EnvironmentDiff(
            files=(file_change,),
            base_image=(
                ValueChange("base_image", before_image, after_image)
                if before_image != after_image
                else None
            ),
            build_scripts=(file_change,) if is_build_script else (),
            business_source=(file_change,) if source_changed else (),
            source_changed=source_changed,
            risk_level=(
                RiskLevel.CRITICAL
                if source_changed
                else RiskLevel.HIGH
                if before_image != after_image
                else RiskLevel.LOW
            ),
            requires_manual_review=source_changed,
            policy_violations=(f"business source changed: {path}",) if source_changed else (),
            summary=f"updated {path}",
        )
        return ToolResult(
            self.name,
            True,
            f"updated {path} and recorded unified diff",
            data={
                "path": path,
                "changed": True,
                "bounded_apt_network_retries": content != requested_content,
            },
            artifacts=(artifact,),
            environment_diff=environment_diff,
        )

    @staticmethod
    def _base_image(content: str) -> str | None:
        match = re.search(r"(?im)^\s*FROM\s+([^\s]+)", content)
        return match.group(1) if match else None

    @classmethod
    def _bound_apt_network_waits(cls, path: str, content: str) -> str:
        if not PurePosixPath(path).name.lower().startswith("dockerfile"):
            return content
        pattern = re.compile(r"\b(apt-get|apt)\s+(update|install)\b")
        lines = []
        for line in content.splitlines(keepends=True):
            if line.lstrip().startswith("#"):
                lines.append(line)
                continue
            lines.append(
                pattern.sub(
                    lambda match: (
                        f"{cls._APT_DNS_OPTIONS} {match.group(1)} "
                        f"{cls._APT_NETWORK_OPTIONS} {match.group(2)}"
                    ),
                    line,
                )
            )
        return "".join(lines)
