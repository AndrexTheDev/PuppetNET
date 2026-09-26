"""Source adapter registry: ``adapter`` name → implementation class."""

from __future__ import annotations

from typing import Any, Iterable, Type

from .base import AdapterContext, AdapterError, SourceAdapter, build_entity, build_relation
from .icij import IcijLeaksAdapter
from .opencorporates import OpenCorporatesAdapter
from .registers import CompaniesHouseAdapter, RegisterFilesAdapter
from .registry import ALL_SOURCE_IDS, SOURCE_REGISTRY, describe_registry, get_spec, iter_specs
from .rss import RssAdapter
from .wikidata import WikidataAdapter

__all__ = [
    "ADAPTERS",
    "AdapterContext",
    "AdapterError",
    "SourceAdapter",
    "build_entity",
    "build_relation",
    "create_adapter",
    "SOURCE_REGISTRY",
    "ALL_SOURCE_IDS",
    "get_spec",
    "iter_specs",
    "describe_registry",
    "IcijLeaksAdapter",
    "OpenCorporatesAdapter",
    "WikidataAdapter",
    "CompaniesHouseAdapter",
    "RegisterFilesAdapter",
    "RssAdapter",
]

ADAPTERS: dict[str, Type[SourceAdapter]] = {
    adapter.adapter_name: adapter
    for adapter in (
        IcijLeaksAdapter,
        OpenCorporatesAdapter,
        WikidataAdapter,
        CompaniesHouseAdapter,
        RegisterFilesAdapter,
        RssAdapter,
    )
}


def create_adapter(spec: Any, context: AdapterContext) -> SourceAdapter:
    """Instantiate the adapter declared by ``SourceSpec.adapter``."""
    name = str(getattr(spec, "adapter", "") or "").strip().lower()
    adapter_class = ADAPTERS.get(name)
    if adapter_class is None:
        raise AdapterError(f"No adapter registered under the name {name!r} (known: {', '.join(sorted(ADAPTERS))})")
    return adapter_class(spec, context)


def adapter_names() -> tuple[str, ...]:
    return tuple(sorted(ADAPTERS))


def resolve_specs(requested: Iterable[str] | None) -> tuple[Any, ...]:
    """Resolve user-requested source ids/adapters into registry specs."""
    if not requested:
        return tuple(SOURCE_REGISTRY)
    resolved: list[Any] = []
    for item in requested:
        spec = get_spec(item)
        if spec is None:
            resolved.extend(iter_specs([item]))
        elif spec not in resolved:
            resolved.append(spec)
    return tuple(resolved)
