"""Ordered deterministic strategy selection."""

from __future__ import annotations

from collections.abc import Iterable

from dprauto.domain.models import ProjectProfile
from dprauto.errors import BuildPlanningError
from dprauto.ports.build import BuildStrategy


class StrategyRegistry:
    def __init__(self, strategies: Iterable[BuildStrategy]) -> None:
        self.strategies = tuple(strategies)
        if not self.strategies:
            raise BuildPlanningError("at least one build strategy is required")

    def select(self, profile: ProjectProfile) -> BuildStrategy:
        for strategy in self.strategies:
            if strategy.supports(profile):
                return strategy
        raise BuildPlanningError(f"no deterministic build strategy supports {profile.project_id}")

    def candidates(self, profile: ProjectProfile) -> tuple[BuildStrategy, ...]:
        """Return every usable strategy in explicit registry priority order.

        ``available`` is an optional capability for strategies backed by an
        external tool such as Pack. An unavailable optional tool is skipped so
        it cannot hide an earlier, actionable project failure.
        """

        candidates: list[BuildStrategy] = []
        for strategy in self.strategies:
            if not strategy.supports(profile):
                continue
            availability = getattr(strategy, "available", None)
            if callable(availability) and not availability():
                continue
            candidates.append(strategy)
        if not candidates:
            raise BuildPlanningError(
                f"no available deterministic build strategy supports {profile.project_id}"
            )
        return tuple(candidates)
