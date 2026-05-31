"""Aggregator registry/factory keyed by name.

Aggregator modules call `register("digikuntz", DigikuntzAggregator)` at import.
The server resolves an aggregator by name via `get(name)`.
"""

from core.base import Aggregator

_REGISTRY: dict[str, type[Aggregator]] = {}


def register(name: str, cls: type[Aggregator]) -> None:
    _REGISTRY[name] = cls


def get(name: str) -> type[Aggregator] | None:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY.keys())
