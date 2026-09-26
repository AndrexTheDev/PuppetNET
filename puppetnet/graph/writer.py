"""Graph writer: turns parse results into batched, idempotent Cypher writes.

Write order matters — sources, then documents, then entities, then mentions,
then relationships — because each step ``MATCH``es what the previous one
created. Everything is ``MERGE``-based and keyed on deterministic ids, so
re-running the same day (or replaying a failed run) converges instead of
duplicating.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..logging_utils import get_logger
from ..models import (
    Document,
    Entity,
    EntityType,
    IngestStats,
    MIN_EDGE_CONFIDENCE,
    Relation,
    SourceSpec,
    iso,
    is_safe_relationship_type,
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
    relations_by_type: dict[str, int] = field(default_factory=dict)
    entities_by_type: dict[str, int] = field(default_factory=dict)
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
    def write_entities(self, entities: Sequence[Entity]) -> tuple[int, list[Entity]]:
        """Resolve, group by type and upsert entities. Returns (rows, resolved)."""
        if not entities:
            return 0, []
        if not self.resolver.loaded:
            self.resolver.load(self.client)
        key_map = self.resolver.resolve_batch(entities)
        resolved = merge_entities_by_key(entities, key_map)

        by_type: dict[EntityType, list[dict[str, Any]]] = {}
        for entity in resolved:
            row = schema.properties_for_entity_row(entity, run_id=self.run_id)
            by_type.setdefault(entity.entity_type, []).append(row)

        total = 0
        for entity_type, rows in by_type.items():
            label = entity_type.value if entity_type is not EntityType.UNKNOWN else "Unknown"
            query = schema.build_entity_upsert(label if label != "Unknown" else EntityType.UNKNOWN.value)
            written = self.client.execute_batches(query, rows, label=f"entities:{label}")
            total += written
            self.summary.entities_by_type[label] = self.summary.entities_by_type.get(label, 0) + written

        self.summary.entities = total
        self.stats.entities_written = total
        return total, resolved

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
            written = self.client.execute_batches(query, rows, label=f"relations:{rel_type}")
            total += written
            self.summary.relations_by_type[rel_type] = self.summary.relations_by_type.get(rel_type, 0) + written

        self.summary.relations = total
        self.stats.relations_written = total
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
