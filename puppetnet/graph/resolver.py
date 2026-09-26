"""Cross-run entity resolution.

Canonical keys already collapse trivial variants (``GAZPROM PJSC`` and
``Gazprom`` both normalise to ``gazprom``). The resolver adds the second half:
aliases learned from *previous* runs. Once the graph knows that
``OJSC Rosneft`` is ``ROSNEFT``, a fresh document mentioning either surface
form lands on the same node instead of forking the graph.

The index is bounded (``ENTITY_RESOLVER_LIMIT`` rows) and rebuilt at the start
of each run, so memory stays flat no matter how large the graph grows.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..logging_utils import get_logger
from ..models import Entity, EntityType, canonical_key, normalize_name

__all__ = ["EntityResolver", "ResolverStats"]

logger = get_logger("graph.resolver")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class ResolverStats:
    lookups: int = 0
    alias_hits: int = 0
    type_conflicts: int = 0
    new_keys: int = 0
    index_size: int = 0
    merged_mentions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _fold(text: str) -> str:
    """Aggressive comparison form: accent-folded, punctuation-free, lowercase."""
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", folded.lower())


@dataclass
class _IndexEntry:
    canonical_key: str
    entity_type: EntityType
    name: str
    mention_count: int = 0


class EntityResolver:
    """Map surface forms onto existing graph identities."""

    def __init__(self, limit: int = 200_000) -> None:
        self.limit = max(1000, int(limit))
        self._by_folded: dict[str, _IndexEntry] = {}
        self._by_canonical: dict[str, _IndexEntry] = {}
        #: Canonical keys the graph already held when :meth:`load` ran. Kept
        #: separate from ``_by_canonical``, which also accumulates entities
        #: registered *during* this run and therefore cannot answer "is this
        #: node new?" for the writer's node-budget guard.
        self._graph_keys: set[str] = set()
        self.stats = ResolverStats()
        self.loaded = False

    # ------------------------------------------------------------------ #
    def load(self, client: Any) -> EntityResolver:
        """Populate the index from Neo4j (no-op in dry-run mode)."""
        from .schema import ENTITY_ALIAS_INDEX

        self._by_folded.clear()
        self._by_canonical.clear()
        self._graph_keys.clear()
        try:
            rows = client.read(ENTITY_ALIAS_INDEX, {"limit": self.limit})
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not load the entity alias index (%s) — starting cold", exc)
            self.loaded = True
            return self

        for row in rows or []:
            key = row.get("canonical_key")
            if not key:
                continue
            try:
                entity_type = EntityType(row.get("entity_type") or "Unknown")
            except ValueError:
                entity_type = EntityType.UNKNOWN
            entry = _IndexEntry(
                canonical_key=key,
                entity_type=entity_type,
                name=row.get("name") or "",
                mention_count=int(row.get("mention_count") or 0),
            )
            self._by_canonical[key] = entry
            self._graph_keys.add(key)
            surfaces = [entry.name] + list(row.get("aliases") or [])
            for surface in surfaces:
                folded = _fold(surface)
                if len(folded) < 3:
                    continue
                existing = self._by_folded.get(folded)
                # Keep the better-attested identity when two collide.
                if existing is None or existing.mention_count < entry.mention_count:
                    self._by_folded[folded] = entry
        self.loaded = True
        self.stats.index_size = len(self._by_folded)
        logger.info("entity resolver index loaded: %d surfaces, %d nodes", len(self._by_folded), len(self._by_canonical))
        return self

    # ------------------------------------------------------------------ #
    def resolve(self, entity: Entity) -> str:
        """Return the canonical key ``entity`` should be written under."""
        self.stats.lookups += 1
        folded = _fold(entity.name)
        if len(folded) >= 3:
            entry = self._by_folded.get(folded)
            if entry is not None:
                if entry.entity_type is entity.entity_type or entry.entity_type is EntityType.UNKNOWN:
                    if entry.canonical_key != entity.canonical_key:
                        self.stats.alias_hits += 1
                        logger.debug(
                            "resolved alias %r → %s (was %s)",
                            entity.name, entry.canonical_key, entity.canonical_key,
                        )
                    return entry.canonical_key
                self.stats.type_conflicts += 1
                logger.debug(
                    "type conflict for %r: index says %s, extraction says %s — keeping extraction",
                    entity.name, entry.entity_type.value, entity.entity_type.value,
                )

        # Fall back to the entity's own key, but remember it for this run.
        if entity.canonical_key not in self._by_canonical:
            self.stats.new_keys += 1
        self.register(entity)
        return entity.canonical_key

    def knows(self, canonical_key: str) -> bool:
        """True when the graph already held a node under this key at load time.

        Used by the writer's node-budget guard: updating an existing node costs
        nothing against the ceiling, creating one is what the AuraDB Free limit
        restricts. The answer is as good as the alias index snapshot, which is
        bounded by ``ENTITY_RESOLVER_LIMIT`` rows.
        """
        return bool(canonical_key) and canonical_key in self._graph_keys

    def register(self, entity: Entity, canonical_key_override: str | None = None) -> None:
        """Add (or refresh) an entity in the in-run index."""
        key = canonical_key_override or entity.canonical_key
        entry = _IndexEntry(
            canonical_key=key,
            entity_type=entity.entity_type,
            name=entity.name,
            mention_count=entity.mention_count,
        )
        self._by_canonical[key] = entry
        for surface in [entity.name, *entity.aliases]:
            folded = _fold(surface)
            if len(folded) >= 3:
                self._by_folded.setdefault(folded, entry)

    def resolve_batch(self, entities: Iterable[Entity]) -> dict[str, str]:
        """Resolve many entities, merging duplicates that collapse together."""
        mapping: dict[str, str] = {}
        for entity in entities:
            mapping[entity.canonical_key] = self.resolve(entity)
        return mapping

    # ------------------------------------------------------------------ #
    @property
    def size(self) -> int:
        return len(self._by_folded)

    def describe(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "index_size": len(self._by_folded),
            "nodes_indexed": len(self._by_canonical),
            "graph_nodes_known": len(self._graph_keys),
            "stats": self.stats.to_dict(),
        }


def merge_entities_by_key(entities: Iterable[Entity], key_map: dict[str, str]) -> list[Entity]:
    """Collapse entities whose resolved keys match (within one run)."""
    merged: dict[str, Entity] = {}
    for entity in entities:
        target_key = key_map.get(entity.canonical_key, entity.canonical_key)
        existing = merged.get(target_key)
        if existing is None:
            clone = Entity(
                name=entity.name,
                entity_type=entity.entity_type,
                canonical_key=target_key,
                aliases=set(entity.aliases),
                mentions=list(entity.mentions),
                properties=dict(entity.properties),
                source_ids=set(entity.source_ids),
                doc_ids=set(entity.doc_ids),
                confidence=entity.confidence,
            )
            merged[target_key] = clone
        else:
            existing.merge(entity)
    return list(merged.values())


def normalize_surface(surface: str) -> str:
    """Public helper reused by the source adapters."""
    return normalize_name(surface)


def key_for(surface: str, entity_type: EntityType) -> str:
    """Public helper reused by the source adapters."""
    return canonical_key(surface, entity_type)
