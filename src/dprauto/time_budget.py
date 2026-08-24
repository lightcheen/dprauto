"""Shared helpers for enforcing workflow-level wall-clock budgets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def deadline_from(started_at: datetime, seconds: int) -> datetime:
    return normalize_utc(started_at) + timedelta(seconds=seconds)


def remaining_seconds(deadline_at: datetime | None, *, now: datetime | None = None) -> float | None:
    if deadline_at is None:
        return None
    current = normalize_utc(now or utc_now())
    return max(0.0, (normalize_utc(deadline_at) - current).total_seconds())


def time_budget_exhausted(
    deadline_at: datetime | None,
    *,
    minimum_seconds: float = 1.0,
    now: datetime | None = None,
) -> bool:
    remaining = remaining_seconds(deadline_at, now=now)
    return remaining is not None and remaining < minimum_seconds


def clamped_timeout_seconds(
    requested_seconds: int,
    deadline_at: datetime | None,
    *,
    minimum_seconds: int = 1,
    now: datetime | None = None,
) -> int:
    if requested_seconds <= 0:
        raise ValueError("requested_seconds must be positive")
    if minimum_seconds <= 0:
        raise ValueError("minimum_seconds must be positive")
    remaining = remaining_seconds(deadline_at, now=now)
    if remaining is None:
        return requested_seconds
    if remaining < minimum_seconds:
        return 0
    return max(minimum_seconds, min(requested_seconds, int(remaining)))
