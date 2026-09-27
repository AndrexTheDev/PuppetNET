"""Domain model tests: weights, enums, identity, merge arithmetic."""

from __future__ import annotations

import logging

import pytest

from puppetnet.models import (
    COOCCURRENCE_PENALTY,
    EDGE_DOC_ID_CAP,
    STRUCTURED_SOURCE_WEIGHT,
    UNSTRUCTURED_SOURCE_WEIGHT,
    Cadence,
    Document,
    Entity,
    EntityType,
    ExtractionMethod,
    Relation,
    RelationType,
    SourceSpec,
    SourceType,
    canonical_key,
    compose_confidence,
    content_hash,
    document_id,
    is_safe_relationship_type,
    noisy_or,
    relation_evidence_is_new,
    remember_edge_doc_id,
)

logging.disable(logging.CRITICAL)


# --------------------------------------------------------------------------- #
# The confidence contract from the specification
# --------------------------------------------------------------------------- #
def test_source_weights_match_spec():
    assert STRUCTURED_SOURCE_WEIGHT == 1.0
    assert UNSTRUCTURED_SOURCE_WEIGHT == 0.4
    assert COOCCURRENCE_PENALTY == 0.2


def test_extraction_method_factors():
    assert ExtractionMethod.STRUCTURED.factor == pytest.approx(1.0)
    assert ExtractionMethod.DEPENDENCY.factor == pytest.approx(1.0)
    # The mandated 0.2 penalty on the co-occurrence fallback.
    assert ExtractionMethod.COOCCURRENCE.factor == pytest.approx(0.8)


def test_structured_dependency_edge_is_full_confidence():
    assert compose_confidence(1.0, ExtractionMethod.STRUCTURED, 1.0) == pytest.approx(1.0)
    assert compose_confidence(1.0, ExtractionMethod.DEPENDENCY, 1.0) == pytest.approx(1.0)


def test_news_edge_weights():
    # News + dependency parse: 0.4 × 1.0 × evidence
    assert compose_confidence(0.4, ExtractionMethod.DEPENDENCY, 0.9) == pytest.approx(0.36)
    # News + co-occurrence fallback: 0.4 × 0.8 × evidence
    assert compose_confidence(0.4, ExtractionMethod.COOCCURRENCE, 1.0) == pytest.approx(0.32)


def test_confidence_is_clamped_and_rounded():
    assert compose_confidence(5.0, ExtractionMethod.DEPENDENCY, 5.0) == 1.0
    assert compose_confidence(-1.0, ExtractionMethod.DEPENDENCY, 1.0) == 0.0
    assert compose_confidence(0.123456789, ExtractionMethod.DEPENDENCY, 1.0) == pytest.approx(0.123457)


def test_source_kind_drives_the_weight():
    assert SourceType.STRUCTURED.confidence == 1.0
    assert SourceType.UNSTRUCTURED.confidence == 0.4
    spec = SourceSpec(id="x", name="X", kind=SourceType.UNSTRUCTURED, adapter="rss")
    assert spec.confidence == 0.4
    assert SourceSpec(id="y", name="Y", kind=SourceType.STRUCTURED, adapter="icij").confidence == 1.0


# --------------------------------------------------------------------------- #
# Bounded vocabulary (Cypher injection surface)
# --------------------------------------------------------------------------- #
def test_relation_type_is_a_closed_vocabulary():
    names = RelationType.names()
    assert "OWNS" in names and "DIRECTOR_OF" in names and "TRAVELED_WITH" in names
    assert "ASSOCIATED_WITH" in names
    assert len(set(names)) == len(names)


def test_relation_type_coerce_handles_enum_member_and_string():
    # RelationType subclasses str: str(member) is "RelationType.OWNS", which
    # used to collapse every predicate to ASSOCIATED_WITH.
    assert RelationType.coerce(RelationType.OWNS) is RelationType.OWNS
    assert RelationType.coerce("OWNS") is RelationType.OWNS
    assert RelationType.coerce("owns") is RelationType.OWNS
    assert RelationType.coerce("  director_of ") is RelationType.DIRECTOR_OF
    assert RelationType.coerce("INVENTED_BY_TEST") is RelationType.ASSOCIATED_WITH


def test_relation_post_init_normalises_predicate_and_method():
    entity = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)
    relation = Relation(subject=entity, predicate="owns", obj=entity, confidence=0.5, method="cooccurrence")
    assert relation.predicate is RelationType.OWNS
    assert relation.rel_type == "OWNS"
    assert relation.method is ExtractionMethod.COOCCURRENCE


@pytest.mark.parametrize(
    "candidate",
    ["OWNS", "DIRECTOR_OF", "TRAVELED_TO", "ASSOCIATED_WITH"],
)
def test_safe_relationship_types(candidate):
    assert is_safe_relationship_type(candidate) is True


@pytest.mark.parametrize(
    "candidate",
    ["OWNS}-[]->(b", "DROP", "owns; MATCH", "", "OW NS", "A" * 80],
)
def test_unsafe_relationship_types_are_rejected(candidate):
    assert is_safe_relationship_type(candidate) is False


def test_entity_type_coercion():
    assert EntityType.coerce("Person") is EntityType.PERSON
    assert EntityType.coerce(EntityType.CRAFT) is EntityType.CRAFT
    assert EntityType.coerce("nonsense") is EntityType.UNKNOWN
    assert EntityType.from_spacy("ORG") is EntityType.ORGANIZATION
    assert EntityType.from_spacy("GPE") is EntityType.LOCATION
    assert EntityType.from_spacy("CRAFT") is EntityType.CRAFT
    assert EntityType.from_spacy("MONEY") is EntityType.UNKNOWN


# --------------------------------------------------------------------------- #
# Identity: canonical keys and content hashes
# --------------------------------------------------------------------------- #
def test_canonical_key_folds_legal_suffixes_and_case():
    assert canonical_key("GAZPROM PJSC", EntityType.ORGANIZATION) == canonical_key("Gazprom", EntityType.ORGANIZATION)
    assert canonical_key("  Rosneft OAO ", EntityType.ORGANIZATION) == canonical_key("rosneft", EntityType.ORGANIZATION)


def test_canonical_key_keeps_distinct_organisations_apart():
    assert canonical_key("Gazprom", EntityType.ORGANIZATION) != canonical_key("Gazprombank", EntityType.ORGANIZATION)


def test_canonical_key_is_namespaced_by_type():
    assert canonical_key("Amadea", EntityType.CRAFT) != canonical_key("Amadea", EntityType.ORGANIZATION)
    assert canonical_key("Amadea", EntityType.CRAFT).startswith("CRAFT:")


def test_entity_post_init_assigns_a_key_and_type():
    entity = Entity(name="Igor Sechin", entity_type="Person")
    assert entity.entity_type is EntityType.PERSON
    assert entity.canonical_key.startswith("PERSON:")
    assert entity.canonical_key == canonical_key("Igor Sechin", EntityType.PERSON)


def test_content_hash_is_stable_and_order_sensitive():
    first = content_hash("Kerimov owns Midea")
    assert first == content_hash("Kerimov owns Midea")
    assert first != content_hash("Kerimov owns Midea Holdings")
    assert len(first) == 64  # sha256 hex


def test_document_id_is_deterministic():
    assert document_id("rss", "https://example.test/a") == document_id("rss", "https://example.test/a")
    assert document_id("rss", "https://example.test/a") != document_id("rss", "https://example.test/b")


# --------------------------------------------------------------------------- #
# Merge arithmetic
# --------------------------------------------------------------------------- #
def test_cadence_parses_and_refuses_junk():
    """A tier is a schedule decision, so a typo must not silently mean 'all'."""
    assert Cadence.coerce("HOURLY") is Cadence.HOURLY
    assert Cadence.coerce(Cadence.WEEKLY) is Cadence.WEEKLY
    assert Cadence.coerce("") is None and Cadence.coerce(None) is None
    assert Cadence.coerce("houry") is None, "junk is refused, not defaulted"
    assert Cadence.coerce(None, default=Cadence.ALL) is Cadence.ALL


def test_a_reread_document_is_not_new_evidence():
    """The guard on the noisy-OR: same document, no second corroboration."""
    assert relation_evidence_is_new(["doc-1", "doc-2"], "doc-1") is False
    assert relation_evidence_is_new(["doc-1"], "doc-2") is True
    assert relation_evidence_is_new([], "doc-1") is True, "no history means the edge is new"
    assert relation_evidence_is_new(None, "doc-1") is True
    assert relation_evidence_is_new(["doc-1"], "") is True, (
        "a source that attributes no document cannot be checked, and refusing it would "
        "silently drop structured sources' edges"
    )


def test_remembering_a_document_is_a_deduplicated_bounded_list():
    kept = remember_edge_doc_id(["a", "b"], "b")
    assert kept == ["a", "b"], "the same document is not listed twice"
    kept = remember_edge_doc_id(["a"], "b")
    assert kept == ["a", "b"], "newest last"
    grown = remember_edge_doc_id([f"doc-{i}" for i in range(EDGE_DOC_ID_CAP + 5)], "newest")
    assert len(grown) == EDGE_DOC_ID_CAP, "the list is bounded"
    assert grown[-1] == "newest", "and keeps the newest entries"


def test_noisy_or_is_monotonic_and_bounded():
    merged = noisy_or(0.4, 0.4)
    assert merged == pytest.approx(1 - (0.6 * 0.6))
    assert noisy_or(0.99, 0.99) <= 1.0
    assert noisy_or(0.0, 0.5) == pytest.approx(0.5)
    assert noisy_or(0.5, 0.0) == pytest.approx(0.5)


def test_relation_signature_ignores_confidence():
    subject = Entity(name="A", entity_type=EntityType.PERSON)
    obj = Entity(name="B", entity_type=EntityType.ORGANIZATION)
    first = Relation(subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.3)
    second = Relation(subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.9)
    assert first.signature() == second.signature()

    other = Relation(subject=obj, predicate=RelationType.OWNS, obj=subject, confidence=0.3)
    assert other.signature() != first.signature()


def test_relation_edge_properties_are_serialisable_and_bounded():
    subject = Entity(name="A", entity_type=EntityType.PERSON)
    obj = Entity(name="B", entity_type=EntityType.ORGANIZATION)
    relation = Relation(
        subject=subject,
        predicate=RelationType.OWNS,
        obj=obj,
        confidence=0.4242424,
        source_id="rss:x",
        doc_id="d1",
        evidence="e" * 5000,
    )
    props = relation.to_edge_properties(run_id="run-1")
    assert props["confidence"] == pytest.approx(0.424242)
    assert len(props["evidence"]) == 1000  # truncated for Neo4j
    assert props["run_id"] == "run-1"
    assert props["observations"] == 1


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def test_document_defaults():
    document = Document(doc_id="d", source_id="s", url="https://example.test")
    assert document.content_type == "text/plain"
    assert document.language == "en"
    assert document.source_weight == pytest.approx(UNSTRUCTURED_SOURCE_WEIGHT)
    assert document.entities == [] and document.relations == []
    assert document.fetched_at is not None
