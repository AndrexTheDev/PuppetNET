"""Degraded-NLP tests: the no-model lexical path and the confidence contract.

CI cannot download ``en_core_web_lg`` (egress to the spaCy model host is often
blocked), and production must survive the same situation — a runner with a
broken model cache still has to ingest structured sources. These tests pin the
behaviour of that fallback: gazetteer + lexical NER, regex relation patterns,
and every edge priced at the co-occurrence rate (the mandated 0.2 penalty).
"""

from __future__ import annotations

import logging

import pytest

from puppetnet.config import load_settings
from puppetnet.models import (
    COOCCURRENCE_PENALTY,
    Document,
    EntityType,
    ExtractionMethod,
    RelationType,
)
from puppetnet.parsing.nlp_engine import NLPEngine, split_sentences

logging.disable(logging.CRITICAL)

BASE = {"DRY_RUN": "true", "LOG_LEVEL": "ERROR", "SPACY_MODEL": ""}


@pytest.fixture(scope="module")
def engine() -> NLPEngine:
    """A blank-pipeline engine: no statistical model, no dependency parser."""
    return NLPEngine(load_settings(dict(BASE)))


SANCTIONS_TEXT = """Suleiman Kerimov owns Midea Holdings Ltd, a company registered in the British Virgin Islands.
Igor Sechin is the director of Rosneft and met with Vladimir Putin in Moscow last week.
The superyacht Amadea sailed from Fiji to Istanbul. Nord Stream AG is owned by Gazprom.
Aircraft 9H-VUC landed in Malta yesterday."""


# --------------------------------------------------------------------------- #
# Backend selection / graceful degradation
# --------------------------------------------------------------------------- #
def test_engine_is_available_without_a_model(engine):
    assert engine.available is True
    assert engine.backend.startswith("spacy:blank")
    assert engine.has_statistical_ner is False
    assert engine.has_parser is False


def test_describe_is_serialisable(engine):
    import json

    json.dumps(engine.describe(), default=str)


def test_model_preference_order_is_reported(engine):
    summary = engine.describe()
    assert "en_core_web_trf" in str(summary) or "models" in summary


# --------------------------------------------------------------------------- #
# Sentence splitting
# --------------------------------------------------------------------------- #
def test_split_sentences_handles_abbreviations():
    text = "Dr. Ivanov met Mr. Smith in Moscow. Gazprom paid 1.5 billion USD in 2019."
    sentences = [piece for _, _, piece in split_sentences(text)]
    assert len(sentences) == 2
    assert sentences[0].startswith("Dr. Ivanov")
    assert sentences[1].startswith("Gazprom")


def test_corporate_suffixes_end_a_sentence():
    """``Ltd.`` is terminal; treating it otherwise merges two claims into one."""
    text = "Suleiman Kerimov owns Midea Holdings Ltd. Igor Sechin is the director of Rosneft."
    sentences = [piece for _, _, piece in split_sentences(text)]
    assert len(sentences) == 2
    assert "Igor Sechin" not in sentences[0]


def test_split_sentences_preserves_offsets():
    text = "First sentence. Second sentence."
    for start, end, piece in split_sentences(text):
        assert text[start:end].strip() == piece.strip()


def test_split_sentences_respects_the_cap():
    text = ". ".join(f"Sentence {i}" for i in range(50))
    assert len(split_sentences(text, max_sentences=5)) == 5


def test_empty_text_yields_nothing(engine):
    result = engine.parse_text("")
    assert result.sentences == 0
    assert result.entities == [] and result.relations == []


# --------------------------------------------------------------------------- #
# Entity extraction without a statistical model
# --------------------------------------------------------------------------- #
def test_persons_organisations_and_locations_are_recognised(engine):
    result = engine.parse_text(SANCTIONS_TEXT)
    by_type: dict[EntityType, set[str]] = {}
    for entity in result.entities:
        by_type.setdefault(entity.entity_type, set()).add(entity.name)

    assert {"Suleiman Kerimov", "Igor Sechin", "Vladimir Putin"} <= by_type[EntityType.PERSON]
    assert {"Rosneft", "Gazprom", "Midea Holdings", "Nord Stream AG"} <= by_type[EntityType.ORGANIZATION]
    assert {"Moscow", "Fiji", "Istanbul", "Malta"} <= by_type[EntityType.LOCATION]


def test_craft_entities_are_detected(engine):
    result = engine.parse_text(SANCTIONS_TEXT)
    craft = {e.name for e in result.entities if e.entity_type is EntityType.CRAFT}
    assert "9H-VUC" in craft
    assert result.craft_mentions >= 1


def test_craft_properties_reach_the_entity(engine):
    result = engine.parse_text("Aircraft 9H-VUC landed in Malta yesterday.")
    craft = [e for e in result.entities if e.entity_type is EntityType.CRAFT][0]
    assert craft.properties.get("registry_country") == "Malta"


def test_products_are_not_organisations(engine):
    """The lexical NER must not promote software names to entities."""
    result = engine.parse_text("MS Windows and the PS5 console shipped to Malta.")
    names = {e.name for e in result.entities}
    assert "MS Windows" not in names
    assert "PS5" not in names


def test_entities_carry_provenance(engine):
    result = engine.parse_text(SANCTIONS_TEXT, source_id="rss:occrp", doc_id="doc-9")
    entity = result.entities[0]
    assert entity.canonical_key
    assert entity.mentions, "every entity keeps at least one mention"
    assert entity.mentions[0].detector


def test_entity_cap_is_enforced():
    settings = load_settings(dict(BASE, NLP_MAX_ENTITIES_PER_DOC="5"))
    engine = NLPEngine(settings)
    text = ". ".join(
        f"{name} met officials in Moscow" for name in
        ("Kerimov", "Sechin", "Putin", "Prigozhin", "Usmanov", "Deripaska", "Vekselberg")
    )
    assert len(engine.parse_text(text).entities) <= 5


# --------------------------------------------------------------------------- #
# Relations on the degraded path
# --------------------------------------------------------------------------- #
def test_predicates_survive_the_no_model_path(engine):
    result = engine.parse_text(SANCTIONS_TEXT)
    predicates = {(r.subject.name, r.rel_type, r.obj.name) for r in result.relations}
    flattened = {p for _, p, _ in predicates}

    assert "OWNS" in flattened
    assert "DIRECTOR_OF" in flattened
    assert any(p in {"ARRIVED_FROM", "TRAVELED_TO"} for p in flattened)
    # nothing collapses into the generic fallback
    assert flattened != {"ASSOCIATED_WITH"}


def test_passive_ownership_is_direction_correct(engine):
    """"Nord Stream AG is owned by Gazprom" ⇒ Gazprom OWNS Nord Stream AG."""
    result = engine.parse_text("Nord Stream AG is owned by Gazprom.")
    owns = [r for r in result.relations if r.rel_type == "OWNS"]
    assert owns, result.relations
    assert owns[0].subject.name == "Gazprom"
    assert owns[0].obj.name == "Nord Stream AG"


def test_director_relation_points_at_the_company(engine):
    result = engine.parse_text("Igor Sechin is the director of Rosneft.")
    directors = [r for r in result.relations if r.rel_type == "DIRECTOR_OF"]
    assert directors
    assert directors[0].subject.name == "Igor Sechin"
    assert directors[0].obj.name == "Rosneft"


def test_hedged_claims_are_discounted(engine):
    plain = engine.parse_text("Yevgeny Prigozhin funded the Wagner Group.")
    hedged = engine.parse_text("Yevgeny Prigozhin allegedly funded the Wagner Group.")
    plain_edge = next(r for r in plain.relations if r.rel_type == "FUNDED")
    hedged_edge = next(r for r in hedged.relations if r.rel_type == "FUNDED")
    assert hedged_edge.confidence < plain_edge.confidence
    assert hedged_edge.extra.get("hedged") is True


def test_negated_claims_produce_no_edge(engine):
    result = engine.parse_text("Suleiman Kerimov does not own Rosneft.")
    assert result.relations == []


# --------------------------------------------------------------------------- #
# The mandated confidence arithmetic
# --------------------------------------------------------------------------- #
def test_degraded_edges_use_the_cooccurrence_rate(engine):
    """No parse ⇒ no dependency pricing: every edge takes the 0.2 penalty."""
    result = engine.parse_text(SANCTIONS_TEXT, source_weight=0.4)
    assert result.dependency_triples == 0
    assert result.pattern_triples + result.cooccurrence_triples > 0
    for relation in result.relations:
        assert relation.method is ExtractionMethod.COOCCURRENCE
        # 0.4 source × 0.8 co-occurrence factor × evidence ≤ 1.0
        assert relation.confidence <= 0.4 * (1 - COOCCURRENCE_PENALTY) + 1e-9
        assert relation.confidence > 0.0


def test_structured_source_weight_doubles_the_edge(engine):
    news = engine.parse_text("Suleiman Kerimov owns Midea Holdings Ltd.", source_weight=0.4)
    structured = engine.parse_text("Suleiman Kerimov owns Midea Holdings Ltd.", source_weight=1.0)
    news_edge = next(r for r in news.relations if r.rel_type == "OWNS")
    structured_edge = next(r for r in structured.relations if r.rel_type == "OWNS")
    assert structured_edge.confidence == pytest.approx(news_edge.confidence / 0.4, rel=0.05)


def test_relations_carry_evidence_text(engine):
    result = engine.parse_text("Suleiman Kerimov owns Midea Holdings Ltd.")
    relation = next(r for r in result.relations if r.rel_type == "OWNS")
    assert relation.evidence
    assert "Kerimov" in relation.evidence
    assert relation.extra.get("rule")


def test_relation_subject_and_object_are_registered_entities(engine):
    result = engine.parse_text("Suleiman Kerimov owns Midea Holdings Ltd.")
    keys = {e.canonical_key for e in result.entities}
    for relation in result.relations:
        assert relation.subject.canonical_key in keys
        assert relation.obj.canonical_key in keys


# --------------------------------------------------------------------------- #
# Document-level API
# --------------------------------------------------------------------------- #
def make_document(text: str, *, source_weight: float = 0.4, doc_id: str = "doc-1") -> Document:
    return Document(
        doc_id=doc_id,
        source_id="rss:occrp",
        url="https://example.test/story",
        title="Sanctions story",
        text=text,
        source_weight=source_weight,
    )


def test_parse_document_populates_metadata(engine):
    result = engine.parse_document(make_document(SANCTIONS_TEXT))
    assert result.doc_id == "doc-1"
    assert result.characters > 0
    assert result.elapsed_seconds >= 0.0
    assert result.relations and all(r.doc_id == "doc-1" for r in result.relations)


def test_parse_document_truncates_huge_inputs():
    settings = load_settings(dict(BASE, NLP_MAX_CHARS_PER_DOC="2000"))
    engine = NLPEngine(settings)
    result = engine.parse_document(make_document("Kerimov owns Midea. " * 500))
    assert "truncated-to-2000-chars" in result.warnings


def test_parse_document_warns_on_empty_text(engine):
    result = engine.parse_document(make_document("   "))
    assert "empty-document" in result.warnings


def test_sentence_cap_is_reported():
    settings = load_settings(dict(BASE, NLP_MAX_SENTENCES_PER_DOC="3"))
    engine = NLPEngine(settings)
    text = " ".join(f"Kerimov met official number {i} in Moscow." for i in range(20))
    result = engine.parse_document(make_document(text))
    assert result.sentences <= 4
    assert "sentence-cap-reached" in result.warnings


def test_relation_types_are_all_in_the_closed_vocabulary(engine):
    result = engine.parse_text(SANCTIONS_TEXT)
    for relation in result.relations:
        assert relation.predicate in RelationType
        assert relation.rel_type in RelationType.names()


def test_duplicate_readings_of_one_clause_do_not_stack(engine):
    """Two patterns firing on one clause must not noisy-OR into inflation.

    ``owns`` appears in two of the engine's verb patterns, so a single clause is
    read twice. One claim must survive at single-reading confidence, while the
    *same* claim restated in an independent sentence is corroboration and must
    raise confidence.
    """
    once = engine.parse_text("Gazprom owns Nord Stream AG.")
    single_clause = [r for r in once.relations if r.rel_type == "OWNS"]
    assert len(single_clause) == 1, "one clause must not publish two edges"
    single = single_clause[0].confidence

    views = list(engine._sentences_for_chunk("Gazprom owns Nord Stream AG."))
    assert sum(1 for t in engine._pattern_triples(views[0])) > 1, "fixture must be read twice"

    corroborated = engine.parse_text(
        "Gazprom owns Nord Stream AG. Reuters confirms Gazprom owns Nord Stream AG."
    )
    stacked = [r for r in corroborated.relations if r.rel_type == "OWNS"]
    assert len(stacked) == 1
    assert stacked[0].confidence > single, "independent sentences must corroborate"
    assert stacked[0].confidence < 1.0


def test_craft_type_word_does_not_become_an_entity(engine):
    """A bare type ("superyacht") is context, not an identity."""
    result = engine.parse_text("The superyacht Amadea sailed from Fiji to Istanbul.")
    names = {e.name for e in result.entities}
    assert "superyacht" not in names
    assert "Amadea" in names
    craft = next(e for e in result.entities if e.name == "Amadea")
    assert craft.entity_type.value == "Craft"
    assert craft.properties.get("craft_type") == "superyacht"


def test_ruler_only_asserts_craft_from_identifiers(engine):
    """Flight numbers need an assigned airline code; type words are not asserted."""
    result = engine.parse_text("Flight BA286 arrived. ZZ123 is not a flight number.")
    names = {e.name for e in result.entities}
    assert "BA286" in " ".join(names) or "flight BA286" in names
    assert not any("ZZ123" in name and "9H" in name for name in names)
    assert "ZZ123" not in names


def test_corporate_suffix_does_not_swallow_the_next_sentence(engine):
    """``Ltd.`` must not glue an organisation to the following person."""
    result = engine.parse_text(
        "Gazprom owns Midea Holdings Ltd. Igor Sechin met with Vladimir Putin in Moscow."
    )
    names = [e.name for e in result.entities]
    assert "Midea Holdings Ltd. Igor Sechin" not in names
    assert any(name.startswith("Midea Holdings") for name in names)
    assert "Igor Sechin" in names and "Vladimir Putin" in names
    relations = {(r.subject.name, r.rel_type, r.obj.name) for r in result.relations}
    assert ("Gazprom", "OWNS", "Midea Holdings") in relations
    assert ("Igor Sechin", "MET_WITH", "Vladimir Putin") in relations


def test_one_surface_form_yields_one_node(engine):
    """A heuristic LOCATION reading must not fork an ORGANIZATION node."""
    result = engine.parse_text(
        "Nord Stream AG is owned by Gazprom. Rosneft sold Yugansk to Gazprom."
    )
    gazprom = [e for e in result.entities if e.name == "Gazprom"]
    assert len(gazprom) == 1, gazprom
    assert gazprom[0].entity_type.value == "Organization"
    assert gazprom[0].mention_count >= 2
    keys = [e.canonical_key for e in result.entities]
    assert len(set(keys)) == len(keys), "canonical keys must be unique per document"


def test_relations_reference_the_reconciled_entity(engine):
    """Endpoints must resolve to the surviving key, not the dropped reading."""
    result = engine.parse_text(
        "Nord Stream AG is owned by Gazprom. Rosneft sold Yugansk to Gazprom."
    )
    published = {e.canonical_key for e in result.entities}
    for relation in result.relations:
        assert relation.subject.canonical_key in published
        assert relation.obj.canonical_key in published


def test_verb_plus_preposition_maps_to_a_specific_predicate(engine):
    """``met with`` / ``sold to`` must not degrade to ASSOCIATED_WITH."""
    result = engine.parse_text("Igor Sechin met with Vladimir Putin in Moscow.")
    assert ("Igor Sechin", "MET_WITH", "Vladimir Putin") in {
        (r.subject.name, r.rel_type, r.obj.name) for r in result.relations
    }
