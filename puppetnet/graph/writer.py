"""Graph writer: turns parse results into batched, idempotent Cypher writes.

Write order matters — sources, then documents, then entities, then mentions,
then relationships — because each step ``MATCH``es what the previous one
created. Everything is ``MERGE``-based and keyed on deterministic ids, so
re-running the same day (or replaying a failed run) converges instead of
duplicating.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domain import domain_labels, stale_domain_labels
from ..logging_utils import get_logger
from ..models import (
    MIN_EDGE_CONFIDENCE,
    Document,
    Entity,
    EntityType,
    IngestStats,
    Relation,
    SourceSpec,
    is_safe_relationship_type,
    iso,
    normalize_name,
    relation_evidence_is_new,
    utcnow,
)
from . import schema
from .neo4j_client import Neo4jClient
from .resolver import EntityResolver, merge_entities_by_key

__all__ = ["GraphWriter", "WriteSummary"]

logger = get_logger("graph.writer")


@dataclass
class WriteSummary:
    """Per-run write counters."""

    sources: int = 0
    documents: int = 0
    entities: int = 0
    mentions: int = 0
    relations: int = 0
    relations_dropped: int = 0
    #: Entity nodes refused by the AuraDB Free node budget (and the edges that
    #: would have pointed at them).
    entities_capped: int = 0
    #: Entities refused because their name has no comparison form (see
    #: ``GraphWriter._drop_unnamed``).
    entities_dropped_unnamed: int = 0
    relations_capped: int = 0
    relations_by_type: dict[str, int] = field(default_factory=dict)
    entities_by_type: dict[str, int] = field(default_factory=dict)
    #: Calculated layer: ``PUPPET_MASTER_OF`` edges written / pruned and the
    #: number of ``:Person`` nodes whose ``risk_score`` was refreshed.
    puppet_master_edges: int = 0
    puppet_master_pruned: int = 0
    risk_scores_updated: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["seconds"] = round(self.seconds, 3)
        return data


class GraphWriter:
    """Batched Neo4j writer bound to one ingest run."""

    def __init__(
        self,
        client: Neo4jClient,
        settings: Any,
        *,
        stats: IngestStats | None = None,
        resolver: EntityResolver | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.stats = stats if stats is not None else IngestStats()
        self.resolver = resolver or EntityResolver(limit=int(getattr(settings, "entity_resolver_limit", 200_000)))
        self.summary = WriteSummary()
        self.run_id = settings.run_id or ""
        #: Cached ``MATCH (e:Entity) RETURN count(e)`` — probed once per run.
        self._node_count: int | None = None
        #: Keys refused by the node budget; edges pointing at them are dropped.
        self._capped_keys: set[str] = set()

    # ------------------------------------------------------------------ #
    # Schema / lifecycle
    # ------------------------------------------------------------------ #
    def ensure_schema(self) -> None:
        if not self.settings.neo4j_ensure_schema:
            logger.info("schema management disabled (NEO4J_ENSURE_SCHEMA=false)")
            return
        self.client.ensure_schema(schema.ensure_schema_statements())

    def begin_run(self, run_id: str, *, extra: dict[str, Any] | None = None) -> str:
        self.run_id = run_id
        properties = {
            "run_id": run_id,
            "started_at": iso(self.stats.started_at),
            "status": "running",
            "github_run_id": self.settings.github_run_id or None,
            "github_sha": self.settings.github_sha or None,
            "github_workflow": self.settings.github_workflow or None,
            "dry_run": bool(self.settings.dry_run),
            "backend": str((extra or {}).get("backend", "")) or None,
            "settings": {k: v for k, v in (extra or {}).items() if isinstance(v, (str, int, float, bool))},
        }
        self.client.write(schema.RUN_UPSERT, {"run_id": run_id, "properties": _flat(properties)}, kind="run")
        logger.info("ingest run %s opened", run_id)
        return run_id

    def finish_run(self, *, status: str = "completed") -> None:
        stats = self.stats
        stats.finished_at = utcnow()
        properties = {
            "status": status,
            "finished_at": iso(stats.finished_at),
            "duration_seconds": stats.duration_seconds,
            "documents_fetched": stats.documents_fetched,
            "documents_skipped_duplicate": stats.documents_skipped_duplicate,
            "documents_failed": stats.documents_failed,
            "sentences_processed": stats.sentences_processed,
            "characters_processed": stats.characters_processed,
            "entities_extracted": stats.entities_extracted,
            "entities_written": stats.entities_written,
            "relations_extracted": stats.relations_extracted,
            "relations_written": stats.relations_written,
            "relations_dropped_low_confidence": stats.relations_dropped_low_confidence,
            "entities_capped_node_budget": self.summary.entities_capped,
            "entities_dropped_unnamed": self.summary.entities_dropped_unnamed,
            "relations_capped_node_budget": self.summary.relations_capped,
            "dependency_triples": stats.dependency_triples,
            "cooccurrence_triples": stats.cooccurrence_triples,
            "craft_entities": stats.craft_entities,
            "http_requests": stats.http_requests,
            "http_via_worker": stats.http_via_worker,
            "http_direct_fallback": stats.http_direct_fallback,
            "http_rate_limited": stats.http_rate_limited,
            "http_deferred_to_queue": stats.http_deferred_to_queue,
            "http_errors": stats.http_errors,
            "seconds_throttled": round(stats.seconds_throttled, 3),
            "error_count": len(stats.errors),
        }
        self.client.write(
            schema.RUN_SUMMARY,
            {"run_id": self.run_id, "properties": properties, "finished_at": iso(stats.finished_at)},
            kind="run",
        )
        if stats.per_source:
            rows = [
                {
                    "source_id": source_id,
                    "documents": counters.get("documents", 0),
                    "entities": counters.get("entities", 0),
                    "relations": counters.get("relations", 0),
                    "errors": counters.get("errors", 0),
                }
                for source_id, counters in stats.per_source.items()
            ]
            self.client.write(schema.RUN_SOURCE_STATS, {"run_id": self.run_id, "rows": rows}, rows=len(rows), kind="run")
        logger.info("ingest run %s closed with status=%s (%.1fs)", self.run_id, status, stats.duration_seconds)

    # ------------------------------------------------------------------ #
    # Calculated layer (PUPPET_MASTER_OF + person risk scores)
    # ------------------------------------------------------------------ #
    def write_puppet_master(self, rows: Sequence[dict[str, Any]]) -> int:
        """Persist calculated influence edges. ``score`` overwrites, never merges."""
        if not rows:
            return 0
        # ``kind="analytics"`` keeps the calculated layer distinguishable from
        # harvested writes in the dry-run report and the recorder summary.
        written = self.client.execute_batches(schema.PUPPET_MASTER_UPSERT, list(rows), kind="analytics", label="analytics:puppet_master")
        self.summary.puppet_master_edges += written
        self.stats.bump_source("analytics", "puppet_master_edges", written)
        logger.info("wrote %d PUPPET_MASTER_OF edge(s)", written)
        return written

    def update_risk_scores(self, rows: Sequence[dict[str, Any]]) -> int:
        """Write per-person ``risk_score`` back onto the ``:Person`` nodes."""
        if not rows:
            return 0
        written = self.client.execute_batches(schema.PERSON_RISK_UPDATE, list(rows), kind="analytics", label="analytics:risk_scores")
        self.summary.risk_scores_updated += written
        logger.info("updated risk_score on %d person node(s)", written)
        return written

    def prune_puppet_master(self, cutoff_iso: str) -> int:
        """Drop calculated edges not refreshed since ``cutoff_iso``."""
        rows = self.client.read(schema.PUPPET_MASTER_PRUNE, {"cutoff": cutoff_iso})
        pruned = int(rows[0].get("pruned", 0)) if rows else 0
        if pruned:
            self.summary.puppet_master_pruned += pruned
            logger.info("pruned %d stale PUPPET_MASTER_OF edge(s) older than %s", pruned, cutoff_iso)
        return pruned

    # ------------------------------------------------------------------ #
    # Sources & documents
    # ------------------------------------------------------------------ #
    def upsert_sources(self, specs: Sequence[SourceSpec]) -> int:
        rows = [
            {
                "source_id": spec.id,
                "name": spec.name,
                "kind": spec.kind.value,
                "confidence": spec.confidence,
                "base_url": spec.base_url,
                "description": spec.description[:1000],
                "enabled": bool(spec.enabled),
                "updated_at": iso(utcnow()),
            }
            for spec in specs
        ]
        if not rows:
            return 0
        written = self.client.execute_batches(schema.SOURCE_UPSERT, rows, label="sources")
        self.summary.sources = written
        return written

    def write_documents(self, documents: Sequence[Document]) -> int:
        rows = []
        for document in documents:
            props = document.to_node_properties()
            props["run_id"] = self.run_id or None
            rows.append(props)
        if not rows:
            return 0
        written = self.client.execute_batches(schema.DOCUMENT_UPSERT, rows, label="documents")
        self.summary.documents = written
        return written

    # ------------------------------------------------------------------ #
    # Entities
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # AuraDB Free node budget
    # ------------------------------------------------------------------ #
    def entity_node_count(self) -> int | None:
        """How many :Entity nodes the graph already holds (``None`` if unknown).

        Probed once per run and only when a cap is configured: in dry-run mode
        there is no database to ask, and an unreadable count must not block a
        harvest, so both cases degrade to "no cap enforced".
        """
        if self._node_count is not None:
            return self._node_count
        if self.client.dry_run or int(getattr(self.settings, "aura_node_cap", 0) or 0) <= 0:
            return None
        try:
            rows = self.client.read(schema.ENTITY_NODE_COUNT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not count :Entity nodes (%s) — node budget not enforced", exc)
            return None
        try:
            self._node_count = int(rows[0]["nodes"]) if rows else 0
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("unreadable :Entity node count — node budget not enforced")
            return None
        logger.info("entity node population: %d", self._node_count)
        return self._node_count

    def _apply_node_cap(self, resolved: Sequence[Entity]) -> list[Entity]:
        """Drop the weakest *new* nodes once the Aura node budget is spent.

        Existing nodes are never refused — updating a node costs nothing against
        the ceiling, and refusing it would fork the graph. When the budget is
        exhausted the strongest newcomers (by confidence, then mention count)
        are admitted and the rest are reported, so a long-running deployment
        degrades gracefully instead of failing every write.
        """
        cap = int(getattr(self.settings, "aura_node_cap", 0) or 0)
        if cap <= 0 or not resolved:
            return list(resolved)
        count = self.entity_node_count()
        if count is None:
            return list(resolved)

        existing = [entity for entity in resolved if self.resolver.knows(entity.canonical_key)]
        newcomers = [entity for entity in resolved if not self.resolver.knows(entity.canonical_key)]
        headroom = max(0, cap - count)
        if len(newcomers) <= headroom:
            self._node_count = count + len(newcomers)
            return list(resolved)

        newcomers.sort(key=lambda entity: (round(float(entity.confidence), 6), entity.mention_count), reverse=True)
        admitted = newcomers[:headroom]
        refused = newcomers[headroom:]
        self._capped_keys.update(entity.canonical_key for entity in refused)
        self.summary.entities_capped += len(refused)
        self._node_count = count + len(admitted)
        logger.warning(
            "AuraDB node budget reached (%d/%d): refusing %d new entity node(s), keeping the %d strongest",
            count, cap, len(refused), len(admitted),
        )
        return existing + admitted

    def _drop_unnamed(self, entities: list[Entity]) -> list[Entity]:
        """Refuse entities whose name has no comparison form.

        ``normalize_name`` strips punctuation, accents and legal suffixes, so a
        name that leaves nothing behind — ``""``, ``"   "``, ``"!!!"``, ``"()"`` —
        has no identity to key on: every such mention would MERGE into the single
        node ``TYPE:unknown-<digest>`` and accumulate mentions, aliases and
        confidence from unrelated documents. The NLP layer already refuses spans
        shorter than two characters, so this catches what structured adapters can
        still hand over (a registry row with an empty name field) and keeps a
        missing upstream check from becoming a wrong graph fact.

        Counterpart to ``relations_dropped_low_confidence``: the run report says
        what was refused, so a silent zero is distinguishable from a silent drop.

        The filter runs *before* the resolver and the merge, because two unnamed
        entities share the key ``TYPE:unknown-<digest>`` — merging them first would
        hide one of the two refusals and, worse, hand the resolver a key it would
        have kept.
        """
        kept: list[Entity] = []
        for entity in entities:
            if normalize_name(entity.name, entity.entity_type):
                kept.append(entity)
                continue
            self.stats.entities_dropped_unnamed += 1
            self.summary.entities_dropped_unnamed += 1
            logger.warning(
                "dropping entity with no usable name (type=%s, key=%s, mentions=%d)",
                entity.entity_type.value, entity.canonical_key, entity.mention_count,
            )
        return kept

    def _mark_new_entity_evidence(self, rows: list[dict[str, Any]]) -> None:
        """Set ``row["is_new"]`` per entity row from what the graph already holds.

        ``True`` means the row carries at least one document the entity's
        ``doc_ids`` does not list yet, so counting this row's mentions is not
        counting evidence the graph already has. A read failure leaves every row
        ``is_new`` (the pre-existing behaviour) rather than freezing the counters:
        an unreachable index must not silently stop the counters from ever moving.
        """
        keys = [row["canonical_key"] for row in rows if row.get("canonical_key")]
        if not keys:
            return
        try:
            known = {
                record["canonical_key"]: record.get("doc_ids") or []
                for record in self.client.read(schema.ENTITY_DOC_IDS, {"keys": keys})
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read entity provenance (%s) — counting every row as new", exc)
            return
        for row in rows:
            known_docs = known.get(row["canonical_key"]) or []
            # An entity row carries *documents*, not a document — and the guard
            # asks about one — so the row is new evidence when any of its documents
            # is one the node has not counted yet. (Reading the row's ``doc_id``
            # here instead was the first version of this method: entity rows do not
            # have that key, so every row looked new and the guard did nothing.)
            documents = [str(item) for item in (row.get("doc_ids") or []) if str(item)]
            row["is_new"] = any(relation_evidence_is_new(known_docs, document) for document in documents)

    def write_entities(self, entities: Sequence[Entity]) -> tuple[int, list[Entity]]:
        """Resolve, group by type and upsert entities. Returns (rows, resolved)."""
        if not entities:
            return 0, []
        # Before anything keys on them: an unnamed entity must not enter the
        # resolver, the alias index or the merge (see _drop_unnamed).
        usable = self._drop_unnamed(list(entities))
        if not usable:
            return 0, []
        if not self.resolver.loaded:
            self.resolver.load(self.client)
        key_map = self.resolver.resolve_batch(usable)
        resolved = merge_entities_by_key(usable, key_map)
        resolved = self._apply_node_cap(resolved)
        if not resolved:
            return 0, []

        # Group by (entity type, domain labels): the type label is part of the
        # MERGE key, the domain labels are SET/REMOVE'd after it. Grouping means
        # one statement per label combination instead of one per node.
        by_shape: dict[tuple[EntityType, tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
        for entity in resolved:
            row = schema.properties_for_entity_row(entity, run_id=self.run_id)
            labels = domain_labels(entity)
            stale = stale_domain_labels(entity)
            by_shape.setdefault((entity.entity_type, labels, stale), []).append(row)

        total = 0
        for (entity_type, labels, stale), rows in by_shape.items():
            label = entity_type.value if entity_type is not EntityType.UNKNOWN else "Unknown"
            query = schema.build_entity_upsert(label, labels=labels, remove=stale)
            self._mark_new_entity_evidence(rows)
            written = self.client.execute_batches(query, rows, label=f"entities:{label}")
            total += written
            self.summary.entities_by_type[label] = self.summary.entities_by_type.get(label, 0) + written
            for domain_label in labels:
                if domain_label in {"Entity", label}:
                    continue
                self.summary.entities_by_type[domain_label] = (
                    self.summary.entities_by_type.get(domain_label, 0) + written
                )

        self.summary.entities = total
        # Cumulative across flushes: the pipeline writes one batch per source,
        # so overwriting here reported only the last source's rows.
        self.stats.entities_written += total
        return total, resolved

    def _mark_new_relation_evidence(self, rel_type: str, rows: list[dict[str, Any]]) -> None:
        """Set ``row["is_new"]`` per relation row from the edge's recorded documents.

        Same rule as the entity version, per edge: ``r.doc_ids`` holds the
        documents whose observation has already been folded into this edge's
        confidence, so a row from a document that is already in that list must not
        fold it in again. Failure to read is not failure to write — the batch goes
        out with every row new, which is what the writer did before this existed.
        """
        pairs = [
            {"subject_key": row["subject_key"], "object_key": row["object_key"]}
            for row in rows
            if row.get("subject_key") and row.get("object_key")
        ]
        if not pairs:
            return
        try:
            records = self.client.read(schema.build_relation_doc_ids(rel_type), {"keys": pairs})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "could not read existing %s evidence (%s) — counting every observation as new",
                rel_type, exc,
            )
            return
        known = {
            (record["subject_key"], record["object_key"]): record.get("doc_ids") or []
            for record in records
        }
        for row in rows:
            existing = known.get((row.get("subject_key"), row.get("object_key")))
            row["is_new"] = relation_evidence_is_new(existing, row.get("doc_id", ""))

    def write_mentions(self, entities: Sequence[Entity]) -> int:
        rows = schema.properties_for_mention_rows(entities)
        if not rows:
            return 0
        written = self.client.execute_batches(schema.MENTION_UPSERT, rows, label="mentions")
        self.summary.mentions = written
        return written

    # ------------------------------------------------------------------ #
    # Relationships
    # ------------------------------------------------------------------ #
    def write_relations(self, relations: Sequence[Relation]) -> int:
        """Upsert typed relationships, grouped so each statement has one type."""
        threshold = float(self.settings.min_edge_confidence or MIN_EDGE_CONFIDENCE)
        accepted: list[Relation] = []
        for relation in relations:
            if relation.subject.canonical_key == relation.obj.canonical_key:
                self.summary.relations_dropped += 1
                continue
            if relation.subject.canonical_key in self._capped_keys or relation.obj.canonical_key in self._capped_keys:
                # Its endpoint was refused by the node budget, so the MATCH in
                # the upsert would silently write nothing. Count it instead.
                self.summary.relations_capped += 1
                self.summary.relations_dropped += 1
                continue
            if relation.confidence < threshold:
                self.summary.relations_dropped += 1
                self.stats.relations_dropped_low_confidence += 1
                continue
            if not is_safe_relationship_type(relation.rel_type):
                logger.warning("dropping relation with unsafe type %r", relation.rel_type)
                self.summary.relations_dropped += 1
                continue
            accepted.append(relation)

        if not accepted:
            return 0

        grouped: dict[str, list[dict[str, Any]]] = {}
        for relation in accepted:
            row = schema.properties_for_relation_row(relation, run_id=self.run_id)
            grouped.setdefault(relation.rel_type, []).append(row)

        total = 0
        for rel_type, rows in sorted(grouped.items(), key=lambda item: -len(item[1])):
            query = schema.build_relation_upsert(rel_type)
            self._mark_new_relation_evidence(rel_type, rows)
            written = self.client.execute_batches(query, rows, label=f"relations:{rel_type}")
            total += written
            self.summary.relations_by_type[rel_type] = self.summary.relations_by_type.get(rel_type, 0) + written

        self.summary.relations = total
        self.stats.relations_written += total
        logger.info(
            "wrote %d relations across %d predicate types (%d dropped)",
            total, len(grouped), self.summary.relations_dropped,
        )
        return total

    # ------------------------------------------------------------------ #
    # Convenience: one call per harvested batch
    # ------------------------------------------------------------------ #
    def persist(
        self,
        documents: Sequence[Document],
        entities: Sequence[Entity],
        relations: Sequence[Relation],
    ) -> WriteSummary:
        started = time.perf_counter()
        self.write_documents(documents)
        _, resolved = self.write_entities(entities)
        self.write_mentions(resolved)
        self.write_relations(relations)
        self.summary.seconds = time.perf_counter() - started
        return self.summary

    # ------------------------------------------------------------------ #
    # Read helpers used by the pipeline
    # ------------------------------------------------------------------ #
    def recent_content_hashes(self, days: int | None = None) -> set[str]:
        """Content hashes already ingested within the dedupe window."""
        window = self.settings.dedupe_window_days if days is None else days
        since = iso(utcnow().replace(microsecond=0)) if window <= 0 else _days_ago_iso(window)
        try:
            rows = self.client.read(schema.RECENT_CONTENT_HASHES, {"since": since})
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read the dedupe index (%s) — treating everything as new", exc)
            return set()
        hashes = {row["content_hash"] for row in rows if row.get("content_hash")}
        logger.info("dedupe index: %d known document hashes (window=%sd)", len(hashes), window)
        return hashes

    def top_edges(self, limit: int = 50, min_confidence: float = 0.2) -> list[dict[str, Any]]:
        try:
            return self.client.read(schema.TOP_EDGES, {"limit": limit, "min_confidence": min_confidence})
        except Exception as exc:  # noqa: BLE001
            logger.debug("top_edges query failed: %s", exc)
            return []

    def run_history(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            return self.client.read(schema.RUN_HISTORY, {"limit": limit})
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_history query failed: %s", exc)
            return []


def _days_ago_iso(days: int) -> str:
    from datetime import timedelta

    return iso(utcnow() - timedelta(days=max(0, int(days))))


def _flat(properties: dict[str, Any]) -> dict[str, Any]:
    """Neo4j rejects nested maps as property values — stringify them."""
    flat: dict[str, Any] = {}
    for key, value in properties.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (str, int, float, bool)) or sub_value is None:
                    flat[f"{key}_{sub_key}"] = sub_value
        elif isinstance(value, (list, tuple, set)):
            cleaned = [v for v in value if isinstance(v, (str, int, float, bool))]
            if cleaned:
                flat[key] = cleaned[:64]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            flat[key] = value
    return flat


def summarise_relations(relations: Iterable[Relation]) -> dict[str, dict[str, Any]]:
    """Aggregate relation stats for the run report (no DB access)."""
    out: dict[str, dict[str, Any]] = {}
    for relation in relations:
        bucket = out.setdefault(
            relation.rel_type,
            {"count": 0, "confidence_sum": 0.0, "max_confidence": 0.0, "methods": {}},
        )
        bucket["count"] += 1
        bucket["confidence_sum"] += relation.confidence
        bucket["max_confidence"] = max(bucket["max_confidence"], relation.confidence)
        bucket["methods"][relation.method.value] = bucket["methods"].get(relation.method.value, 0) + 1
    for bucket in out.values():
        count = bucket["count"] or 1
        bucket["avg_confidence"] = round(bucket["confidence_sum"] / count, 4)
        bucket["confidence_sum"] = round(bucket["confidence_sum"], 4)
    return out
