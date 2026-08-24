"""Static tox and nox matrix inspection without executing project code."""

from __future__ import annotations

import ast
import configparser
import re
from typing import Any, Mapping


_QUALITY = re.compile(
    r"(?:^|[-_.])(docs?|lint|format|type(?:check)?|mypy|ruff|flake|style|"
    r"release|publish|schema|package)(?:[-_.]|$)",
    re.IGNORECASE,
)
_EXPENSIVE = re.compile(
    r"(?:^|[-_.\s])(fuzz|benchmark|bench|performance|slow)(?=[-_.\s]|$)",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(
    r"(?:^|[-_./\s])(integration|e2e|remote|postgres(?:ql)?|mysql|mariadb|"
    r"cockroach|redis|mongo(?:db)?|oracle|elasticsearch|docker)(?=[-_./\s]|$)",
    re.IGNORECASE,
)
_UNIT_EVIDENCE = re.compile(r"\b(pytest|py\.test|unittest|unit tests?|test suite)\b", re.I)
_SPECIAL = re.compile(
    r"(?:^|[-_.])(run[-_]?self|generate|schema|build|package|release|publish)"
    r"(?:[-_.]|$)",
    re.IGNORECASE,
)


def tox_environment_metadata(content: str) -> tuple[Mapping[str, Any], ...]:
    """Return bounded, expanded tox environments and their static risk class."""

    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(content)
    except configparser.Error:
        return ()

    declared: list[str] = []
    if parser.has_section("tox"):
        for option in ("env_list", "envlist"):
            declared.extend(_expand_envlist(parser.get("tox", option, fallback="")))

    section_patterns: list[tuple[str, str]] = []
    for section in parser.sections():
        if not section.casefold().startswith("testenv:"):
            continue
        pattern = section.split(":", 1)[1].strip()
        for expanded in _brace_expand(pattern):
            section_patterns.append((expanded, section))
            declared.append(expanded)

    base_text = _section_text(parser, "testenv")
    environments: list[Mapping[str, Any]] = []
    for name in tuple(dict.fromkeys(item for item in declared if item))[:64]:
        specific = "\n".join(
            _section_text(parser, section)
            for expanded, section in section_patterns
            if expanded == name
        )
        evidence = "\n".join((name, base_text, specific))
        kind = _environment_kind(name, evidence)
        environments.append(
            {
                "name": name,
                "kind": kind,
                "safe": kind in {"unit", "generic"},
                "python_version": _python_version_from_name(name),
            }
        )
    return tuple(environments)


def tox_pytest_parallel_metadata(content: str) -> Mapping[str, Any]:
    """Return a safe pytest-xdist factor declared by tox, if one exists."""

    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(content)
    except configparser.Error:
        return {}
    if not parser.has_section("tox") or not parser.has_section("testenv"):
        return {}

    env_list = "\n".join(
        parser.get("tox", option, fallback="") for option in ("env_list", "envlist")
    )
    dependencies = parser.get("testenv", "deps", fallback="")
    commands = parser.get("testenv", "commands", fallback="")
    for line in dependencies.splitlines():
        dependency = re.match(
            r"^\s*([A-Za-z0-9_-]*parallel[A-Za-z0-9_-]*)\s*:\s*"
            r"pytest-xdist(?:\s*[<>=!~].*)?\s*$",
            line,
            re.IGNORECASE,
        )
        if not dependency:
            continue
        factor = dependency.group(1)
        if not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(factor)}(?![A-Za-z0-9_])",
            env_list,
            re.IGNORECASE,
        ):
            continue
        argument = re.search(
            rf"(?m)^\s*{re.escape(factor)}\s*:\s*"
            r"(?:(--numprocesses)\s*(?:=\s*)?|(-n)\s+)"
            r"(auto|[1-9][0-9]?)\b",
            commands,
            re.IGNORECASE,
        )
        if argument:
            return {
                "runner": "pytest",
                "factor": factor,
                "dependency": "pytest-xdist",
                "argument": "--numprocesses",
                "requested_workers": argument.group(3).casefold(),
                "source": "tox.ini",
            }
    return {}


def nox_session_metadata(content: str) -> tuple[Mapping[str, Any], ...]:
    """Return nox sessions, including interpreter and parameterization evidence."""

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ()

    sessions: list[Mapping[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = next(
            (item for item in node.decorator_list if _is_nox_decorator(item, "session")),
            None,
        )
        if decorator is None:
            continue
        name = _decorator_string(decorator, "name") or node.name
        versions = _decorator_strings(decorator, "python")
        parameterized = any(
            _is_nox_decorator(item, "parametrize") for item in node.decorator_list
        )
        command_text = " ".join(_session_call_text(item) for item in ast.walk(node))
        evidence = f"{name} {command_text}"
        kind = _environment_kind(name, evidence)
        sessions.append(
            {
                "name": name,
                "kind": kind,
                "safe": kind in {"unit", "generic"} and not parameterized,
                "python_versions": versions,
                "parameterized": parameterized,
            }
        )
        if len(sessions) >= 64:
            break
    return tuple(sessions)


def preferred_matrix_name(
    entries: tuple[Mapping[str, Any], ...],
    *,
    python_version: str = "",
) -> str:
    """Select one safe unit environment, preferring the active interpreter."""

    safe = tuple(item for item in entries if item.get("safe") is True)
    if not safe:
        return ""

    normalized_python = _normalize_python_version(python_version)

    def rank(item: Mapping[str, Any]) -> tuple[int, int, int]:
        versions = tuple(item.get("python_versions", ()))
        version = str(item.get("python_version", ""))
        if normalized_python:
            if (
                version == normalized_python
                or normalized_python
                in {_normalize_python_version(value) for value in versions}
            ):
                runtime_rank = 0
            elif not version and not versions:
                runtime_rank = 1
            else:
                runtime_rank = 2
        else:
            runtime_rank = 0 if version else 1
        name = str(item.get("name", ""))
        name_rank = 0 if re.fullmatch(r"(?:unit-?)?tests?", name, re.I) else 1
        kind_rank = 0 if item.get("kind") == "unit" else 1
        return runtime_rank, name_rank, kind_rank

    return str(min(safe, key=rank).get("name", ""))


def matrix_entry(
    entries: tuple[Mapping[str, Any], ...],
    name: str,
) -> Mapping[str, Any] | None:
    return next((item for item in entries if item.get("name") == name), None)


def _environment_kind(name: str, evidence: str) -> str:
    if _EXTERNAL.search(evidence):
        return "external"
    if _EXPENSIVE.search(evidence):
        return "expensive"
    if _QUALITY.search(name) or _SPECIAL.search(name):
        return "quality"
    if _UNIT_EVIDENCE.search(evidence) or re.search(r"(?:^|[-_.])py\d*", name, re.I):
        return "unit"
    if _QUALITY.search(evidence):
        return "quality"
    return "generic"


def _section_text(parser: configparser.RawConfigParser, section: str) -> str:
    if not parser.has_section(section):
        return ""
    return "\n".join(f"{key}={value}" for key, value in parser.items(section))


def _expand_envlist(value: str) -> tuple[str, ...]:
    expanded: list[str] = []
    for item in _split_top_level(value):
        expanded.extend(_brace_expand(item))
        if len(expanded) >= 64:
            break
    return tuple(expanded[:64])


def _split_top_level(value: str) -> tuple[str, ...]:
    items: list[str] = []
    pending: list[str] = []
    depth = 0
    for character in value:
        if character == "{":
            depth += 1
        elif character == "}" and depth:
            depth -= 1
        if depth == 0 and (character == "," or character.isspace()):
            item = "".join(pending).strip()
            if item:
                items.append(item)
            pending = []
        else:
            pending.append(character)
    item = "".join(pending).strip()
    if item:
        items.append(item)
    return tuple(items)


def _brace_expand(value: str) -> tuple[str, ...]:
    pending = [value.strip()]
    completed: list[str] = []
    for _ in range(8):
        next_pending: list[str] = []
        for item in pending:
            match = re.search(r"\{([^{}]*)\}", item)
            if not match:
                completed.append(item)
                continue
            for alternative in match.group(1).split(","):
                next_pending.append(
                    item[: match.start()] + alternative.strip() + item[match.end() :]
                )
                if len(next_pending) + len(completed) >= 512:
                    break
            if len(next_pending) + len(completed) >= 512:
                break
        pending = next_pending
        if not pending:
            break
    completed.extend(item for item in pending if "{" not in item and "}" not in item)
    # Short, low-factor environments are the safest representatives. Sorting
    # before the metadata cap also prevents a large first Python version from
    # crowding later runtime versions out of a cartesian tox factor matrix.
    unique = tuple(dict.fromkeys(item for item in completed if item))
    return tuple(sorted(unique, key=lambda item: (len(item), item.casefold()))[:256])


def _python_version_from_name(name: str) -> str:
    match = re.search(r"(?:^|[-_.])py(?:thon)?(\d{2,3})(?:[-_.]|$)", name, re.I)
    if not match:
        match = re.fullmatch(r"(?:ci[-_.])?(\d{2,3})", name, re.I)
    return _normalize_python_version(match.group(1)) if match else ""


def _normalize_python_version(value: object) -> str:
    text = str(value).strip().casefold().removeprefix("python").removeprefix("py")
    match = re.fullmatch(r"(\d)\.(\d{1,2})(?:\.\d+)?", text)
    if match:
        return f"{match.group(1)}.{int(match.group(2))}"
    if re.fullmatch(r"\d{2,3}", text):
        return f"{text[0]}.{int(text[1:])}"
    return ""


def _is_nox_decorator(node: ast.expr, name: str) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "nox"
        and target.attr == name
    )


def _decorator_string(node: ast.expr, keyword: str) -> str:
    values = _decorator_strings(node, keyword)
    return values[0] if values else ""


def _decorator_strings(node: ast.expr, keyword: str) -> tuple[str, ...]:
    if not isinstance(node, ast.Call):
        return ()
    value = next((item.value for item in node.keywords if item.arg == keyword), None)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, (ast.List, ast.Tuple)):
        return tuple(
            str(item.value)
            for item in value.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, (str, int, float))
        )
    return ()


def _session_call_text(node: ast.AST) -> str:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return ""
    if node.func.attr not in {"install", "run", "notify"}:
        return ""
    values = [
        str(item.value)
        for item in node.args
        if isinstance(item, ast.Constant) and isinstance(item.value, (str, int, float))
    ]
    return " ".join(values)
