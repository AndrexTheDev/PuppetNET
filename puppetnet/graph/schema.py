"""Neo4j schema definition (constraints, indexes, Cypher templates).

The graph model
---------------
::

    (:IngestRun)-[:PROCESSED]->(:Document)-[:FROM_SOURCE]->(:Source)
                                (:Document)-[:MENTIONS {count}]->(:Entity)
    (:Entity:Person|Organization|Location|Craft)-[<TYPED_REL> {confidence, ...}]->(:Entity)

Design notes
~~~~~~~~~~~~
* ``Entity`` carries the uniqueness constraint (``canonical_key``) while the
  type is a *secondary* label, so ``MATCH (e:Person)`` and
  ``MATCH (e:Entity)`` are both cheap.
* Relationship types come from a closed vocabulary
  (:class:`~puppetnet.models.RelationType`). Types are interpolated into Cypher
  after allowlist validation, which is what keeps a dynamic-type writer safe
  without APOC (not available on AuraDB Free).
* Repeated observations of the same edge merge with a noisy-OR on
  ``confidence`` and keep the five most recent evidence strings.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..models import EntityType, RelationType, is_safe_relationship_type
from ..logging_utils import get_logger

__all__ = [
    "CONSTRAINTS",
    "INDEXES",
    "FULLTEXT_INDEXES",
    "ENTITY_UPSERT_TEMPLATE",
    "RELATION_UPSERT_TEMPLATE",
    "DOCUMENT_UPSERT",
    "SOURCE_UPSERT",
    "RUN_UPSERT",
    "MENTION_UPSERT",
    "build_entity_upsert",
    "build_relation_upsert",
    "ensure_schema_statements",
]

logger = get_logger("graph.schema")

# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #

CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT puppetnet_entity_key IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.canonical_key IS UNIQUE",
    "CREATE CONSTRAINT puppetnet_document_id IF NOT EXISTS "
    "FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT puppetnet_source_id IF NOT EXISTS "
    "FOR (s:Source) REQUIRE s.source_id IS UNIQUE",
    "CREATE CONSTRAINT puppetnet_run_id IF NOT EXISTS "
    "FOR (r:IngestRun) REQUIRE r.run_id IS UNIQUE",
)

INDEXES: tuple[str, ...] = (
    "CREATE INDEX puppetnet_entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE INDEX puppetnet_entity_type IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
    "CREATE INDEX puppetnet_entity_confidence IF NOT EXISTS FOR (e:Entity) ON (e.confidence)",
    "CREATE INDEX puppetnet_entity_aliases IF NOT EXISTS FOR (e:Entity) ON (e.aliases)",
    "CREATE INDEX puppetnet_person_name IF NOT EXISTS FOR (p:Person) ON (p.name)",
    "CREATE INDEX puppetnet_org_name IF NOT EXISTS FOR (o:Organization) ON (o.name)",
    "CREATE INDEX puppetnet_location_name IF NOT EXISTS FOR (l:Location) ON (l.name)",
    "CREATE INDEX puppetnet_craft_name IF NOT EXISTS FOR (c:Craft) ON (c.name)",
    "CREATE INDEX puppetnet_document_hash IF NOT EXISTS FOR (d:Document) ON (d.content_hash)",
    "CREATE INDEX puppetnet_document_source IF NOT EXISTS FOR (d:Document) ON (d.source_id)",
    "CREATE INDEX puppetnet_document_published IF NOT EXISTS FOR (d:Document) ON (d.published_at)",
    "CREATE INDEX puppetnet_document_fetched IF NOT EXISTS FOR (d:Document) ON (d.fetched_at)",
    "CREATE INDEX puppetnet_run_started IF NOT EXISTS FOR (r:IngestRun) ON (r.started_at)",
)

FULLTEXT_INDEXES: tuple[str, ...] = (
    "CREATE FULLTEXT INDEX puppetnet_entity_search IF NOT EXISTS "
    "FOR (e:Entity) ON EACH [e.name, e.aliases]",
    "CREATE FULLTEXT INDEX puppetnet_document_search IF NOT EXISTS "
    "FOR (d:Document) ON EACH [d.title, d.url]",
)

# --------------------------------------------------------------------------- #
# Cypher templates
# --------------------------------------------------------------------------- #

SOURCE_UPSERT = """
UNWIND $rows AS row
MERGE (s:Source {source_id: row.source_id})
SET s.name = row.name,
    s.kind = row.kind,
    s.confidence = row.confidence,
    s.base_url = row.base_url,
    s.description = row.description,
    s.enabled = row.enabled,
    s.updated_at = row.updated_at
RETURN count(s) AS sources
"""

RUN_UPSERT = """
MERGE (r:IngestRun {run_id: $run_id})
SET r += $properties
RETURN r.run_id AS run_id
"""

DOCUMENT_UPSERT = """
UNWIND $rows AS row
MERGE (d:Document {doc_id: row.doc_id})
ON CREATE SET
    d.source_id = row.source_id,
    d.url = row.url,
    d.title = row.title,
    d.author = row.author,
    d.content_type = row.content_type,
    d.language = row.language,
    d.content_hash = row.content_hash,
    d.word_count = row.word_count,
    d.source_weight = row.source_weight,
    d.published_at = row.published_at,
    d.fetched_at = row.fetched_at,
    d.external_id = row.external_id,
    d.first_ingested_at = row.fetched_at,
    d.ingest_count = 1
ON MATCH SET
    d.title = CASE WHEN size(coalesce(row.title,'')) > size(coalesce(d.title,'')) THEN row.title ELSE d.title END,
    d.word_count = row.word_count,
    d.content_hash = row.content_hash,
    d.published_at = coalesce(d.published_at, row.published_at),
    d.last_ingested_at = row.fetched_at,
    d.ingest_count = coalesce(d.ingest_count, 0) + 1
WITH d, row
MERGE (d)-[fs:FROM_SOURCE]->(s:Source {source_id: row.source_id})
ON CREATE SET fs.first_seen = row.fetched_at, fs.last_seen = row.fetched_at
ON MATCH SET fs.last_seen = row.fetched_at
WITH d, row
FOREACH (run_id IN CASE WHEN row.run_id IS NULL THEN [] ELSE [row.run_id] END |
    MERGE (r:IngestRun {run_id: run_id})
    MERGE (r)-[:PROCESSED]->(d)
)
RETURN count(d) AS documents
"""

MENTION_UPSERT = """
UNWIND $rows AS row
MATCH (d:Document {doc_id: row.doc_id})
MATCH (e:Entity {canonical_key: row.canonical_key})
MERGE (d)-[m:MENTIONS]->(e)
ON CREATE SET
    m.count = row.count,
    m.first_offset = row.first_offset,
    m.confidence = row.confidence,
    m.surface_forms = row.surface_forms,
    m.first_seen = row.seen_at,
    m.last_seen = row.seen_at
ON MATCH SET
    m.count = coalesce(m.count, 0) + row.count,
    m.confidence = 1 - (1 - coalesce(m.confidence, 0)) * (1 - row.confidence),
    m.surface_forms = (coalesce(m.surface_forms, []) + row.surface_forms)[-8..],
    m.last_seen = row.seen_at
RETURN count(m) AS mentions
"""

#: ``{label}`` is substituted with a whitelisted secondary label.
ENTITY_UPSERT_TEMPLATE = """
UNWIND $rows AS row
MERGE (e:Entity:{label} {{canonical_key: row.canonical_key}})
ON CREATE SET
    e.name = row.name,
    e.entity_type = row.entity_type,
    e.aliases = row.aliases,
    e.mention_count = row.mention_count,
    e.confidence = row.confidence,
    e.source_ids = row.source_ids,
    e.doc_ids = row.doc_ids,
    e.first_seen = row.first_seen,
    e.last_seen = row.last_seen,
    e += row.properties
ON MATCH SET
    e.name = CASE WHEN size(coalesce(row.name, '')) > size(coalesce(e.name, '')) THEN row.name ELSE e.name END,
    e.aliases = reduce(acc = coalesce(e.aliases, []), a IN row.aliases |
                       CASE WHEN a IN acc THEN acc ELSE acc + a END)[0..64],
    e.mention_count = coalesce(e.mention_count, 0) + row.mention_count,
    e.confidence = 1 - (1 - coalesce(e.confidence, 0)) * (1 - row.confidence),
    e.source_ids = reduce(acc = coalesce(e.source_ids, []), s IN row.source_ids |
                          CASE WHEN s IN acc THEN acc ELSE acc + s END)[0..32],
    e.doc_ids = (coalesce(e.doc_ids, []) + row.doc_ids)[-64..],
    e.last_seen = row.last_seen,
    e += row.properties
RETURN count(e) AS entities
"""

#: ``{rel_type}`` is substituted with a whitelisted relationship type.
RELATION_UPSERT_TEMPLATE = """
UNWIND $rows AS row
MATCH (a:Entity {{canonical_key: row.subject_key}})
MATCH (b:Entity {{canonical_key: row.object_key}})
MERGE (a)-[r:{rel_type}]->(b)
ON CREATE SET
    r.confidence = row.confidence,
    r.source_weight = row.source_weight,
    r.method = row.method,
    r.evidence = [row.evidence],
    r.evidence_scores = [row.evidence_score],
    r.verb = row.verb,
    r.source_id = row.source_id,
    r.doc_id = row.doc_id,
    r.run_id = row.run_id,
    r.negated = row.negated,
    r.hedged = row.hedged,
    r.passive = row.passive,
    r.rule = row.rule,
    r.observations = 1,
    r.first_seen = row.first_seen,
    r.last_seen = row.last_seen
ON MATCH SET
    r.confidence = 1 - (1 - coalesce(r.confidence, 0)) * (1 - row.confidence),
    r.observations = coalesce(r.observations, 1) + 1,
    r.source_weight = CASE WHEN row.source_weight > coalesce(r.source_weight, 0)
                           THEN row.source_weight ELSE r.source_weight END,
    r.method = CASE WHEN row.method = 'dependency' OR coalesce(r.method, '') = 'dependency'
                    THEN 'dependency' ELSE row.method END,
    r.evidence = (coalesce(r.evidence, []) + [row.evidence])[-5..],
    r.evidence_scores = (coalesce(r.evidence_scores, []) + [row.evidence_score])[-5..],
    r.verb = CASE WHEN coalesce(row.verb, '') <> '' THEN row.verb ELSE r.verb END,
    r.source_id = row.source_id,
    r.doc_id = row.doc_id,
    r.run_id = row.run_id,
    r.last_seen = row.last_seen
RETURN count(r) AS relations
"""

RUN_SUMMARY = """
MATCH (r:IngestRun {run_id: $run_id})
SET r += $properties, r.finished_at = $finished_at
RETURN r.run_id AS run_id
"""

RUN_SOURCE_STATS = """
UNWIND $rows AS row
MATCH (r:IngestRun {run_id: $run_id})
MATCH (s:Source {source_id: row.source_id})
MERGE (r)-[h:HANDLED]->(s)
SET h.documents = row.documents,
    h.entities = row.entities,
    h.relations = row.relations,
    h.errors = row.errors
RETURN count(h) AS handled
"""

RECENT_CONTENT_HASHES = """
MATCH (d:Document)
WHERE d.fetched_at >= $since OR d.last_ingested_at >= $since
RETURN d.content_hash AS content_hash, d.doc_id AS doc_id
"""

ENTITY_ALIAS_INDEX = """
MATCH (e:Entity)
WHERE e.aliases IS NOT NULL OR e.name IS NOT NULL
RETURN e.canonical_key AS canonical_key,
       e.name AS name,
       e.aliases AS aliases,
       e.entity_type AS entity_type,
       e.mention_count AS mention_count
LIMIT $limit
"""

RUN_HISTORY = """
MATCH (r:IngestRun)
RETURN r.run_id AS run_id, r.started_at AS started_at, r.finished_at AS finished_at,
       r.status AS status, r.documents_fetched AS documents
ORDER BY r.started_at DESC
LIMIT $limit
"""

TOP_EDGES = """
MATCH (a:Entity)-[r]->(b:Entity)
WHERE type(r) <> 'MENTIONS' AND r.confidence >= $min_confidence
RETURN a.name AS subject, type(r) AS predicate, b.name AS object,
       r.confidence AS confidence, r.observations AS observations,
       r.method AS method
ORDER BY r.confidence DESC, r.observations DESC
LIMIT $limit
"""


def build_entity_upsert(entity_type: EntityType | str) -> str:
    """Entity upsert Cypher for one secondary label."""
    label = EntityType(entity_type).value if not isinstance(entity_type, EntityType) else entity_type.value
    if not label or not label.isidentifier():
        raise ValueError(f"Unsafe entity label: {label!r}")
    return ENTITY_UPSERT_TEMPLATE.format(label=label)


def build_relation_upsert(rel_type: RelationType | str) -> str:
    """Relationship upsert Cypher for one whitelisted predicate."""
    predicate = RelationType.coerce(rel_type) if not isinstance(rel_type, RelationType) else rel_type
    if not is_safe_relationship_type(predicate.value):
        raise ValueError(f"Refusing to build Cypher for relationship type {rel_type!r}")
    return RELATION_UPSERT_TEMPLATE.format(rel_type=predicate.value)


def ensure_schema_statements() -> list[str]:
    """All DDL statements, in dependency order."""
    return list(CONSTRAINTS) + list(INDEXES) + list(FULLTEXT_INDEXES)


def entity_labels() -> Sequence[str]:
    return tuple(t.value for t in EntityType if t is not EntityType.UNKNOWN)


def relation_types() -> Sequence[str]:
    return RelationType.names()


def properties_for_entity_row(entity: Any, *, run_id: str = "") -> dict[str, Any]:
    """Flatten an :class:`~puppetnet.models.Entity` into an upsert row."""
    props = entity.to_node_properties()
    reserved = {
        "canonical_key", "name", "entity_type", "aliases", "mention_count",
        "confidence", "source_ids", "doc_ids", "first_seen", "last_seen",
    }
    extra = {k: v for k, v in props.items() if k not in reserved and v not in (None, "", [], {})}
    # Neo4j rejects nested maps as property values — flatten to dotted keys.
    flat: dict[str, Any] = {}
    for key, value in extra.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, (str, int, float, bool)) or sub_value is None:
                    flat[f"{key}_{sub_key}"] = sub_value
        elif isinstance(value, (list, tuple, set)):
            cleaned = [v for v in value if isinstance(v, (str, int, float, bool))]
            if cleaned:
                flat[key] = cleaned[:64]
        elif isinstance(value, (str, int, float, bool)):
            flat[key] = value
    return {
        "canonical_key": props["canonical_key"],
        "name": props["name"],
        "entity_type": props["entity_type"],
        "aliases": props["aliases"],
        "mention_count": props["mention_count"],
        "confidence": props["confidence"],
        "source_ids": props["source_ids"],
        "doc_ids": props["doc_ids"],
        "first_seen": props["first_seen"],
        "last_seen": props["last_seen"],
        "properties": flat,
        "run_id": run_id,
    }


def properties_for_relation_row(relation: Any, *, run_id: str = "") -> dict[str, Any]:
    """Flatten a :class:`~puppetnet.models.Relation` into an upsert row."""
    props = relation.to_edge_properties(run_id)
    extra = relation.extra or {}
    return {
        "subject_key": relation.subject.canonical_key,
        "object_key": relation.obj.canonical_key,
        "rel_type": relation.rel_type,
        "confidence": props["confidence"],
        "source_weight": props["source_weight"],
        "method": props["method"],
        "evidence": props["evidence"] or relation.subject.name,
        "evidence_score": float(extra.get("evidence_score", 1.0)),
        "verb": props["verb"] or "",
        "source_id": props["source_id"],
        "doc_id": props["doc_id"],
        "run_id": props["run_id"] or run_id,
        "negated": bool(props["negated"]),
        "hedged": bool(extra.get("hedged", False)),
        "passive": bool(extra.get("passive", False)),
        "rule": str(extra.get("rule", ""))[:120],
        "first_seen": props["first_seen"],
        "last_seen": props["last_seen"],
    }


def properties_for_mention_rows(entities: Iterable[Any]) -> list[dict[str, Any]]:
    """Build MENTIONS rows for every document an entity appeared in."""
    from ..models import iso, utcnow

    rows: list[dict[str, Any]] = []
    now = iso(utcnow())
    for entity in entities:
        surfaces = sorted({m.text for m in entity.mentions})[:8]
        per_doc: dict[str, dict[str, Any]] = {}
        for mention in entity.mentions:
            doc_id = getattr(mention, "doc_id", None) or (sorted(entity.doc_ids)[0] if entity.doc_ids else "")
            bucket = per_doc.setdefault(doc_id, {"count": 0, "first_offset": -1, "confidence": 0.0})
            bucket["count"] += 1
            if bucket["first_offset"] < 0 and mention.start_char >= 0:
                bucket["first_offset"] = mention.start_char
            bucket["confidence"] = max(bucket["confidence"], float(mention.confidence))
        for doc_id, bucket in per_doc.items():
            if not doc_id:
                continue
            rows.append(
                {
                    "doc_id": doc_id,
                    "canonical_key": entity.canonical_key,
                    "count": bucket["count"],
                    "first_offset": bucket["first_offset"],
                    "confidence": bucket["confidence"] or float(entity.confidence),
                    "surface_forms": surfaces,
                    "seen_at": now,
                }
            )
    return rows
