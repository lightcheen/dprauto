"""Pluggable language and build strategy registrations."""
"""Deterministic build strategies."""

from dprauto.strategies.cnb import CNBStrategy
from dprauto.strategies.common import RecordedBuildRunner
from dprauto.strategies.docker import DockerStrategy
from dprauto.strategies.registry import StrategyRegistry
from dprauto.strategies.template import TemplateStrategy

__all__ = [
    "CNBStrategy",
    "DockerStrategy",
    "RecordedBuildRunner",
    "StrategyRegistry",
    "TemplateStrategy",
]
