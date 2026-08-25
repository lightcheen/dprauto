"""Language-neutral tokenization shared by semantic retrieval adapters."""

from __future__ import annotations

import re


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]*|[0-9]+")


def semantic_terms(text: str) -> tuple[str, ...]:
    """Split prose, paths, snake_case, and camelCase into stable search terms."""

    expanded = _CAMEL_BOUNDARY.sub(" ", text.replace("_", " "))
    terms: list[str] = []
    for match in _TOKEN.finditer(expanded):
        value = match.group(0).casefold().strip(".-")
        if value and (len(value) > 1 or value in {"c", "r"}):
            terms.append(value)
    return tuple(terms)
