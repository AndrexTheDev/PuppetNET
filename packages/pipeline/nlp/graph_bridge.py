"""Convert Workers AI triplets into the graph's ``Entity``/``Relation`` model.

The LLM stage is deliberately kept outside the Neo4j writer: this bridge is the
only place where a free-text model response can become a graph write. It is a
pure function so the conversion can be tested without a database, and it never
invents a node type: an argument whose label cannot be mapped stays
``EntityType.UNKNOWN`` (the writer stores it as ``:Entity:Unknown``) rather than
being promoted to Person or Organization on a guess.

Confidence reuses :func:`puppetnet.models.compose_confidence` with the
unstructured source weight (0.4) and :class:`ExtractionMethod.PATTERN`, so an
LLM edge can never outrank a parsed dependency triple. That ordering is the
point: the LLM adds recall, not authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from puppetnet.models import (
    UNSTRUCTURED_SOURCE_WEIGHT,
    Document,
    Entity,
    EntityMention,
    EntityType,
    ExtractionMethod,
    Relation,
    RelationType,
    compose_confidence,
)

from .labels import entity_type_for_label
from .triplet_extractor import Triplet, TripletExtractionResult

__all__ = ["graph_from_triplets", "triplet_predicate_counts"]


def graph_from_triplets(
    document: Document,
    triplets: TripletExtractionResult,
    *,
    known_entities: Iterable[Any] = (),
    source_id: str = "",
    model: str = "",
) -> tuple[list[Entity], list[Relation]]:
    """Return deduplicated entities and evidence-backed relations.

    One triplet may mention the same organisation twice; entities are keyed by
    ``Entity.canonical_key`` and merged, while relations keep the strongest
    observation per ``(subject, predicate, object)`` signature.
    """

    known = {str(getattr(item, "name", getattr(item, "text", ""))).casefold(): item for item in known_entities}
    entities: dict[str, Entity] = {}
    relations: dict[str, Relation] = {}

    for triplet in triplets.triplets:
        subject = _entity_for(
            triplet.subject,
            triplet.subject_label,
            triplet.subject_char,
            triplet.confidence,
            document,
            known,
            source_id,
        )
        obj = _entity_for(
            triplet.obj,
            triplet.object_label,
            triplet.object_char,
            triplet.confidence,
            document,
            known,
            source_id,
        )
        if subject.canonical_key == obj.canonical_key:
            continue
        for entity in (subject, obj):
            existing = entities.get(entity.canonical_key)
            if existing is None:
                entities[entity.canonical_key] = entity
            else:
                existing.merge(entity)

        predicate = RelationType.coerce(triplet.predicate)
        confidence = compose_confidence(
            UNSTRUCTURED_SOURCE_WEIGHT,
            ExtractionMethod.PATTERN,
            evidence_score=max(0.0, min(1.0, float(triplet.confidence))),
        )
        relation = Relation(
            subject=subject,
            predicate=predicate,
            obj=obj,
            confidence=confidence,
            method=ExtractionMethod.PATTERN,
            source_id=source_id,
            doc_id=document.doc_id,
            evidence=triplet.evidence,
            verb=str(triplet.predicate).lower(),
            extra={
                "extractor": "cloudflare-workers-ai",
                "model": model or triplets.model,
                "chunk_index": triplet.chunk_index,
                "llm_confidence": round(float(triplet.confidence), 6),
            },
        )
        existing_relation = relations.get(relation.signature())
        if existing_relation is None or relation.confidence > existing_relation.confidence:
            relations[relation.signature()] = relation

    return list(entities.values()), list(relations.values())


def triplet_predicate_counts(items: Iterable[Triplet]) -> dict[str, int]:
    """Predicate histogram, including the ASSOCIATED_WITH fallback count.

    :meth:`RelationType.coerce` is deliberately tolerant, which means a typo or
    an unmodelled verb would silently become ``ASSOCIATED_WITH``. Exposing the
    count in the run report keeps that degradation visible instead of pretending
    the predicate was understood.
    """

    counts: dict[str, int] = {}
    for item in items:
        predicate = RelationType.coerce(item.predicate)
        counts[predicate.value] = counts.get(predicate.value, 0) + 1
    return counts


def _entity_for(
    surface: str,
    label: str,
    char: int,
    confidence: float,
    document: Document,
    known: dict[str, Any],
    source_id: str,
) -> Entity:
    known_entity = known.get(surface.casefold())
    entity_type = entity_type_for_label(label)
    if known_entity is not None:
        osint_label = getattr(known_entity, "label", "")
        if osint_label:
            entity_type = entity_type_for_label(osint_label)
        else:
            known_type = getattr(known_entity, "entity_type", None)
            if isinstance(known_type, EntityType):
                entity_type = known_type
    # Without a mapped model label the writer stores :Unknown, which is honest.
    # We never infer Person from a capital letter.
    entity = Entity(name=surface, entity_type=entity_type, confidence=float(confidence))
    entity.source_ids.add(source_id)
    entity.doc_ids.add(document.doc_id)
    entity.mentions.append(
        EntityMention(
            text=surface,
            entity_type=entity_type,
            start_char=char,
            end_char=char + len(surface) if char >= 0 else -1,
            confidence=float(confidence),
            detector="workers_ai",
            context=document.text[max(0, char - 80): char + len(surface) + 80] if char >= 0 else "",
        )
    )
    if label:
        entity.properties.setdefault("workers_ai_label", label)
    return entity
