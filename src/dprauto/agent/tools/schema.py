"""Small dependency-free validator for the JSON-schema subset used by Agent tools."""

from __future__ import annotations

from typing import Any, Mapping

from dprauto.errors import ToolExecutionError


EMPTY_OBJECT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def validate_tool_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> None:
    """Validate arguments before side effects and report all top-level contract errors."""

    violations: list[str] = []
    if schema.get("type") != "object":
        raise ToolExecutionError(f"tool {tool_name} has an invalid argument schema")
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
        raise ToolExecutionError(f"tool {tool_name} has an invalid argument schema")
    for key in required:
        if key not in arguments:
            violations.append(f"missing required argument {key!r}")
    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                violations.append(f"unexpected argument {key!r}")
    for key, value in arguments.items():
        candidate = properties.get(key)
        if isinstance(candidate, Mapping):
            violation = _value_violation(value, candidate, key)
            if violation:
                violations.append(violation)
    if violations:
        raise ToolExecutionError(
            f"invalid arguments for tool {tool_name}: " + "; ".join(violations),
            details={"tool": tool_name, "violations": tuple(violations)},
        )


def _value_violation(value: Any, schema: Mapping[str, Any], path: str) -> str:
    expected = schema.get("type")
    valid = True
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, (list, tuple))
    elif expected == "object":
        valid = isinstance(value, Mapping)
    if not valid:
        return f"argument {path!r} must be {expected}"
    if isinstance(value, str) and len(value) < int(schema.get("minLength", 0)):
        return f"argument {path!r} must not be empty"
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            return f"argument {path!r} must be >= {minimum}"
    if isinstance(value, (list, tuple)):
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"argument {path!r} must contain at least {minimum} item(s)"
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                violation = _value_violation(item, item_schema, f"{path}[{index}]")
                if violation:
                    return violation
    return ""
