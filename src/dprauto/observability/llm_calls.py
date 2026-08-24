"""Hourly JSONL audit log for LLM questions and responses."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


class HourlyLLMCallLogger:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def record(
        self,
        *,
        request: Mapping[str, Any],
        response: Mapping[str, Any] | None,
        metadata: Mapping[str, Any],
        duration_seconds: float,
        error: str = "",
        timestamp: datetime | None = None,
    ) -> Path:
        occurred_at = timestamp or self.clock()
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        path = self.root / f"{occurred_at.astimezone().strftime('%Y-%m-%d-%H')}.log"
        entry = {
            "timestamp": occurred_at.isoformat(),
            "metadata": dict(metadata),
            "request": dict(request),
            "response": dict(response) if response is not None else None,
            "duration_seconds": round(duration_seconds, 6),
            "error": error,
        }
        encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode()
        with self._lock:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)
        return path
