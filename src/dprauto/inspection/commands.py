"""Rule-based command extraction from human documentation and CI configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import product

from dprauto.domain.enums import CommandPurpose


_SHELL_FENCE_LANGUAGES = {"", "bash", "console", "shell", "sh", "terminal", "zsh"}
_COMMAND_TOKENS = {
    "./",
    "apt",
    "apt-get",
    "bash",
    "conda",
    "coverage",
    "docker",
    "docker-compose",
    "flask",
    "gunicorn",
    "hatch",
    "make",
    "mypy",
    "nox",
    "npm",
    "pdm",
    "pip",
    "pip3",
    "pipenv",
    "poetry",
    "pytest",
    "python",
    "python3",
    "ruff",
    "sh",
    "streamlit",
    "tox",
    "uv",
    "uvicorn",
}


@dataclass(frozen=True, slots=True)
class ExtractedCommand:
    text: str
    purpose: CommandPurpose
    source: str
    confidence: float


class CommandExtractor:
    """Extract explicit commands; it never invents missing project instructions."""

    def __init__(self, *, max_commands_per_file: int = 100) -> None:
        self.max_commands_per_file = max_commands_per_file

    def extract_markdown(self, source: str, text: str) -> tuple[ExtractedCommand, ...]:
        commands: list[ExtractedCommand] = []
        heading = ""
        previous_nonempty = ""
        in_fence = False
        fence_language = ""
        fence_lines: list[str] = []

        for line in text.splitlines():
            heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if heading_match and not in_fence:
                heading = heading_match.group(1)
                previous_nonempty = heading
                continue

            fence_match = re.match(r"^\s*```\s*([\w+-]*)", line)
            if fence_match:
                if not in_fence:
                    in_fence = True
                    fence_language = fence_match.group(1).lower()
                    fence_lines = []
                else:
                    if fence_language in _SHELL_FENCE_LANGUAGES:
                        commands.extend(self._extract_shell_lines(source, heading, fence_lines, 0.9))
                    in_fence = False
                    fence_language = ""
                    fence_lines = []
                continue

            if in_fence:
                fence_lines.append(line)
                continue

            stripped = line.strip()
            if re.fullmatch(r"[=\-~^`]{3,}", stripped) and previous_nonempty:
                heading = previous_nonempty
                continue
            if stripped.endswith("::"):
                heading = stripped[:-2].strip()

            indented_match = re.match(r"^\s{4,}(\S.*)$", line)
            if indented_match:
                command = self._make_command(indented_match.group(1), source, heading, 0.8)
                if command:
                    commands.append(command)

            prompt_match = re.match(r"^\s*(?:[-*]\s+)?\$\s+(.+)$", line)
            if prompt_match:
                command = self._make_command(prompt_match.group(1), source, heading, 0.85)
                if command:
                    commands.append(command)

            if stripped:
                previous_nonempty = stripped.removesuffix("::").strip()

            for inline in re.findall(r"``([^`\n]+)``|`([^`\n]+)`", line):
                candidate = next(value for value in inline if value)
                command = self._make_command(candidate, source, heading, 0.7)
                if command:
                    commands.append(command)

        return self._deduplicate(commands)

    def extract_ci(self, source: str, text: str) -> tuple[ExtractedCommand, ...]:
        lines = text.splitlines()
        matrix_values = self._ci_matrix_values(lines)
        commands: list[ExtractedCommand] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            match = re.match(r"^(\s*)(?:-\s*)?(run|script|before_script|after_script):\s*(.*)$", line)
            if not match:
                index += 1
                continue

            indentation = len(match.group(1))
            value = match.group(3).strip()
            if value and value not in {"|", ">"}:
                for concrete in self._expand_ci_command(value, matrix_values):
                    command = self._make_command(concrete, source, "ci", 0.98)
                    if command:
                        commands.append(command)
                index += 1
                continue

            block: list[str] = []
            index += 1
            while index < len(lines):
                block_line = lines[index]
                if block_line.strip() and len(block_line) - len(block_line.lstrip()) <= indentation:
                    break
                stripped = block_line.strip()
                if stripped.startswith("- "):
                    stripped = stripped[2:].strip()
                block.append(stripped)
                index += 1
            expanded_block = [
                concrete
                for item in block
                for concrete in self._expand_ci_command(item, matrix_values)
            ]
            commands.extend(self._extract_shell_lines(source, "ci", expanded_block, 0.98))

        return self._deduplicate(commands)

    @staticmethod
    def _ci_matrix_values(lines: list[str]) -> dict[str, tuple[str, ...]]:
        """Parse bounded scalar values from a simple YAML ``strategy.matrix`` block."""

        values: dict[str, tuple[str, ...]] = {}
        for index, line in enumerate(lines):
            matrix = re.match(r"^(\s*)matrix:\s*$", line)
            if not matrix:
                continue
            matrix_indent = len(matrix.group(1))
            child_indent: int | None = None
            cursor = index + 1
            while cursor < len(lines):
                candidate = lines[cursor]
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= matrix_indent:
                    break
                key_match = re.match(
                    r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$",
                    candidate,
                )
                if not key_match:
                    cursor += 1
                    continue
                indentation = len(key_match.group(1))
                if child_indent is None:
                    child_indent = indentation
                if indentation != child_indent:
                    cursor += 1
                    continue
                key = key_match.group(2).casefold()
                inline = key_match.group(3).strip()
                if key in {"include", "exclude"}:
                    cursor += 1
                    continue
                collected = list(CommandExtractor._matrix_scalar_values(inline))
                if not inline:
                    nested = cursor + 1
                    while nested < len(lines):
                        nested_line = lines[nested]
                        nested_indent = len(nested_line) - len(nested_line.lstrip())
                        if nested_line.strip() and nested_indent <= indentation:
                            break
                        item = re.match(r"^\s*-\s*([^:#][^#]*)$", nested_line)
                        if item:
                            collected.extend(
                                CommandExtractor._matrix_scalar_values(item.group(1).strip())
                            )
                        nested += 1
                selected = tuple(dict.fromkeys(collected))[:12]
                if selected:
                    values[key] = selected
                cursor += 1
        return values

    @staticmethod
    def _matrix_scalar_values(value: str) -> tuple[str, ...]:
        if not value:
            return ()
        content = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        candidates = re.findall(r'"([^"]*)"|\'([^\']*)\'|([^,\s]+)', content)
        normalized: list[str] = []
        for quoted, single_quoted, plain in candidates:
            item = (quoted or single_quoted or plain).strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", item):
                normalized.append(item)
        return tuple(normalized)

    @staticmethod
    def _expand_ci_command(
        command: str,
        matrix_values: dict[str, tuple[str, ...]],
    ) -> tuple[str, ...]:
        references = tuple(
            dict.fromkeys(
                match.casefold()
                for match in re.findall(
                    r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}",
                    command,
                )
            )
        )
        if not references or any(name not in matrix_values for name in references):
            return (command,)
        expanded: list[str] = []
        for combination in product(*(matrix_values[name] for name in references)):
            concrete = command
            for name, value in zip(references, combination):
                concrete = re.sub(
                    rf"\$\{{\{{\s*matrix\.{re.escape(name)}\s*\}}\}}",
                    value,
                    concrete,
                    flags=re.IGNORECASE,
                )
            expanded.append(concrete)
            if len(expanded) >= 12:
                break
        return tuple(expanded)

    def _extract_shell_lines(
        self,
        source: str,
        context: str,
        lines: list[str],
        confidence: float,
    ) -> list[ExtractedCommand]:
        logical_lines: list[str] = []
        pending = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if pending:
                line = f"{pending} {line}"
                pending = ""
            if line.endswith("\\"):
                pending = line[:-1].rstrip()
                continue
            # CI scripts often put each side of an && chain on its own YAML line.
            # Keep useful individual candidates without returning dangling operators.
            line = re.sub(r"\s*(?:&&|\|\|)\s*$", "", line)
            if not line:
                continue
            logical_lines.append(line)
        if pending:
            logical_lines.append(pending)

        extracted: list[ExtractedCommand] = []
        for line in logical_lines:
            command = self._make_command(line, source, context, confidence)
            if command:
                extracted.append(command)
        return extracted

    def _make_command(
        self,
        text: str,
        source: str,
        context: str,
        confidence: float,
    ) -> ExtractedCommand | None:
        normalized = re.sub(r"\s+", " ", text.strip())
        normalized = re.sub(r"^(?:\$|>)\s*", "", normalized)
        if not normalized or normalized.startswith("#") or not self._looks_like_command(normalized):
            return None
        return ExtractedCommand(
            text=normalized,
            purpose=self._classify(normalized, context),
            source=source,
            confidence=confidence,
        )

    @staticmethod
    def _base_token(command: str) -> str:
        tokens = command.strip().split()
        while tokens and ("=" in tokens[0] and not tokens[0].startswith(("=", "./"))):
            tokens.pop(0)
        while tokens and tokens[0] in {"env", "sudo", "time"}:
            tokens.pop(0)
        if not tokens:
            return ""
        token = tokens[0].lower()
        return "./" if token.startswith("./") else token

    def _looks_like_command(self, command: str) -> bool:
        token = self._base_token(command)
        return token in _COMMAND_TOKENS or bool(
            re.fullmatch(r"python\d+(?:\.\d+)*", token)
        )

    @staticmethod
    def _classify(command: str, context: str) -> CommandPurpose:
        combined = f"{context} {command}".lower()
        command_lower = command.lower()
        # Installation is checked before tests because dependency commands may
        # legitimately contain package names such as pytest.
        if re.search(
            r"\b(pip3?|poetry|pipenv|conda|uv|pdm|hatch)\s+"
            r"(?:install|sync|add|env\s+create)\b",
            command_lower,
        ):
            return CommandPurpose.INSTALL
        if re.search(
            r"\bpython(?:\d+(?:\.\d+)*)?\s+-m\s+pip\s+install\b",
            command_lower,
        ):
            return CommandPurpose.INSTALL
        if re.search(r"\bmake\s+(?:install|setup|dependencies|deps)\b", command_lower):
            return CommandPurpose.INSTALL
        if re.search(r"\b(pytest|unittest|tox|nox|make\s+(?:test|tests|check)|coverage\s+run)\b", command_lower):
            return CommandPurpose.TEST
        if re.search(r"\b(?:ruff|mypy)\s+(?:check|\.)(?:\s|$)", command_lower):
            return CommandPurpose.TEST
        if re.search(r"\bmake\s+(?:lint|typecheck)\b", command_lower):
            return CommandPurpose.TEST
        if re.search(
            r"\b(docker\s+build|python\d*\s+-m\s+build|make\s+(?:build|doc|docs|package|wheel))\b",
            command_lower,
        ):
            return CommandPurpose.BUILD
        if re.search(r"\b(uvicorn|gunicorn|flask\s+run|runserver|streamlit\s+run|docker\s+(?:run|compose\s+up))\b", command_lower):
            return CommandPurpose.RUN
        if any(word in combined for word in ("test", "testing", "check")):
            return CommandPurpose.TEST
        if any(word in combined for word in ("install", "setup", "dependency", "dependencies")):
            return CommandPurpose.INSTALL
        if any(word in combined for word in ("run", "start", "usage", "quickstart", "quick start")):
            return CommandPurpose.RUN
        if "build" in combined:
            return CommandPurpose.BUILD
        return CommandPurpose.OTHER

    def _deduplicate(self, commands: list[ExtractedCommand]) -> tuple[ExtractedCommand, ...]:
        unique: dict[str, ExtractedCommand] = {}
        for command in commands:
            key = command.text.strip().lower()
            current = unique.get(key)
            if current is None or command.confidence > current.confidence:
                unique[key] = command
            if len(unique) >= self.max_commands_per_file:
                break
        return tuple(unique.values())
