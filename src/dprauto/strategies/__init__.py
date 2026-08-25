"""Pluggable deterministic language and build strategy registrations."""

from dprauto.strategies.cnb import CNBStrategy
from dprauto.strategies.common import RecordedBuildRunner
from dprauto.strategies.docker import DockerStrategy
from dprauto.strategies.multilang import JVMTemplateStrategy, NativeTemplateStrategy
from dprauto.strategies.registry import StrategyRegistry
from dprauto.strategies.template import TemplateStrategy

__all__ = [
    "CNBStrategy",
    "DockerStrategy",
    "JVMTemplateStrategy",
    "NativeTemplateStrategy",
    "RecordedBuildRunner",
    "StrategyRegistry",
    "TemplateStrategy",
]
