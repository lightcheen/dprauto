"""Component discovery port."""

from typing import Protocol, runtime_checkable

from dprauto.domain.workspace import ComponentGraph, RepositoryScan


@runtime_checkable
class ComponentDiscoverer(Protocol):
    def discover(self, scan: RepositoryScan) -> ComponentGraph:
        """Discover and rank buildable components without executing project code."""
        ...
