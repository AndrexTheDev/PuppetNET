"""Dependency-parsing (SVO) extraction tests.

These exercise the *primary* extraction route — spaCy dependency triples — using
the API-compatible doubles in :mod:`tests.fakes`, so they run without a
downloaded model. Each tree mirrors how ``en_core_web_lg`` labels the sentence.
"""

from __future__ import annotations

import logging
import os

import pytest

from puppetnet.config import load_settings
from puppetnet.models import EntityType, ExtractionMethod, RelationType
from puppetnet.parsing.nlp_engine import NLPEngine, SpanEntity

from .fakes import FakeSpan, Tok, make_tree

logging.disable(logging.CRITICAL)


@pytest.fixture()
def engine() -> NLPEngine:
    settings = load_settings(
        {
            "DRY_RUN": "true",
            "LOG_LEVEL": "ERROR",
            "SPACY_MODEL": "",
            "NLP_BACKEND": "spacy",
        }
    )
    return NLPEngine(settings)


def spans(doc, *specs) -> list[SpanEntity]:
    """Build ``SpanEntity`` objects for (start_token, end_token, label)."""
    out: list[SpanEntity] = []
    for start, end, label in specs:
        first, last = doc.tokens[start], doc.tokens[end - 1]
        out.append(
            SpanEntity(
                text=" ".join(t.text for t in doc.tokens[start:end]),
                label=label,
                start_char=first.idx,
                end_char=last.idx + len(last.text),
                start_token=start,
                end_token=end,
            )
        )
    return out


def predicates(triples) -> list[tuple[str, str, str]]:
    return sorted((t.subject.text, t.verb_lemma, t.obj.text) for t in triples)


# --------------------------------------------------------------------------- #
# Active SVO
# --------------------------------------------------------------------------- #
def test_active_svo_ownership(engine):
    text = "Suleiman Kerimov owns Midea Holdings"
    doc = make_tree(
        text,
        [
            Tok("Suleiman", "PROPN", "compound", head=1),
            Tok("Kerimov", "PROPN", "nsubj", head=2),
            Tok("owns", "VERB", "ROOT", head=2, lemma="own"),
            Tok("Midea", "PROPN", "compound", head=4),
            Tok("Holdings", "PROPN", "dobj", head=2),
        ],
    )
    sp = spans(doc, (0, 2, "PERSON"), (3, 5, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert len(triples) == 1
    triple = triples[0]
    assert triple.subject.text == "Suleiman Kerimov"
    assert triple.obj.text == "Midea Holdings"
    assert triple.verb_lemma == "own"
    assert triple.passive is False


def test_verb_maps_to_owns_predicate(engine):
    text = "Suleiman Kerimov owns Midea Holdings"
    doc = make_tree(
        text,
        [
            Tok("Suleiman", "PROPN", "compound", head=1),
            Tok("Kerimov", "PROPN", "nsubj", head=2),
            Tok("owns", "VERB", "ROOT", head=2, lemma="own"),
            Tok("Midea", "PROPN", "compound", head=4),
            Tok("Holdings", "PROPN", "dobj", head=2),
        ],
    )
    sp = spans(doc, (0, 2, "PERSON"), (3, 5, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)
    registry = {s.start_token: s for s in sp}
    decision = engine._decide(triples[0], EntityType.PERSON, EntityType.ORGANIZATION, text)

    assert decision.predicate is RelationType.OWNS
    assert decision.evidence == pytest.approx(0.98, abs=0.02)
    assert registry[0].entity_type is EntityType.PERSON


# --------------------------------------------------------------------------- #
# Passive voice: direction must be restored
# --------------------------------------------------------------------------- #
def test_passive_with_agent_restores_active_order(engine):
    text = "Nord Stream was acquired by Gazprom"
    doc = make_tree(
        text,
        [
            Tok("Nord", "PROPN", "compound", head=1),
            Tok("Stream", "PROPN", "nsubj:pass", head=3),
            Tok("was", "AUX", "auxpass", head=3, lemma="be"),
            Tok("acquired", "VERB", "ROOT", head=3, lemma="acquire"),
            Tok("by", "ADP", "prep", head=3),
            Tok("Gazprom", "PROPN", "pobj", head=4),
        ],
    )
    sp = spans(doc, (0, 2, "ORG"), (5, 6, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert len(triples) == 1
    # agent promoted to subject → one canonical direction
    assert triples[0].subject.text == "Gazprom"
    assert triples[0].obj.text == "Nord Stream"
    assert triples[0].passive is True
    # passive_flip is False here: arguments are already in active order
    assert triples[0].passive_flip is False


def test_passive_agent_maps_to_acquired(engine):
    text = "Nord Stream was acquired by Gazprom"
    doc = make_tree(
        text,
        [
            Tok("Nord", "PROPN", "compound", head=1),
            Tok("Stream", "PROPN", "nsubj:pass", head=3),
            Tok("was", "AUX", "auxpass", head=3, lemma="be"),
            Tok("acquired", "VERB", "ROOT", head=3, lemma="acquire"),
            Tok("by", "ADP", "prep", head=3),
            Tok("Gazprom", "PROPN", "pobj", head=4),
        ],
    )
    sp = spans(doc, (0, 2, "ORG"), (5, 6, "ORG"))
    triple = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)[0]
    decision = engine._decide(triple, EntityType.ORGANIZATION, EntityType.ORGANIZATION, text)

    assert decision.predicate is RelationType.ACQUIRED
    assert decision.reverse is False


# --------------------------------------------------------------------------- #
# Copular nominal predication: DIRECTOR_OF
# --------------------------------------------------------------------------- #
def test_copular_director_of(engine):
    text = "Igor Sechin is the director of Rosneft"
    doc = make_tree(
        text,
        [
            Tok("Igor", "PROPN", "compound", head=1),
            Tok("Sechin", "PROPN", "nsubj", head=2),
            Tok("is", "AUX", "ROOT", head=2, lemma="be"),
            Tok("the", "DET", "det", head=4),
            Tok("director", "NOUN", "attr", head=2, lemma="director"),
            Tok("of", "ADP", "prep", head=4),
            Tok("Rosneft", "PROPN", "pobj", head=5),
        ],
    )
    sp = spans(doc, (0, 2, "PERSON"), (6, 7, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert len(triples) == 1
    triple = triples[0]
    assert triple.subject.text == "Igor Sechin"
    assert triple.obj.text == "Rosneft"
    assert triple.verb_dep == "attr"
    assert triple.verb_lemma == "director"
    assert triple.preposition == "of"

    decision = engine._decide(triple, EntityType.PERSON, EntityType.ORGANIZATION, text)
    assert decision.predicate is RelationType.DIRECTOR_OF
    assert decision.matched_on == "noun"


# --------------------------------------------------------------------------- #
# Prepositional objects: TRAVELED_TO
# --------------------------------------------------------------------------- #
def test_prepositional_object_travel(engine):
    text = "Amadea sailed to Istanbul"
    doc = make_tree(
        text,
        [
            Tok("Amadea", "PROPN", "nsubj", head=1),
            Tok("sailed", "VERB", "ROOT", head=1, lemma="sail"),
            Tok("to", "ADP", "prep", head=1),
            Tok("Istanbul", "PROPN", "pobj", head=2),
        ],
    )
    sp = spans(doc, (0, 1, "ORG"), (3, 4, "GPE"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert len(triples) == 1
    assert triples[0].subject.text == "Amadea"
    assert triples[0].obj.text == "Istanbul"
    decision = engine._decide(triples[0], EntityType.CRAFT, EntityType.LOCATION, text)
    assert decision.predicate is RelationType.TRAVELED_TO


# --------------------------------------------------------------------------- #
# Conjunction expansion
# --------------------------------------------------------------------------- #
def test_conjunction_expansion_multiplies_triples(engine):
    text = "Gazprom and Rosneft sanctioned Nord Stream"
    doc = make_tree(
        text,
        [
            Tok("Gazprom", "PROPN", "nsubj", head=3),
            Tok("and", "CCONJ", "cc", head=0),
            Tok("Rosneft", "PROPN", "conj", head=0),
            Tok("sanctioned", "VERB", "ROOT", head=3, lemma="sanction"),
            Tok("Nord", "PROPN", "compound", head=5),
            Tok("Stream", "PROPN", "dobj", head=3),
        ],
    )
    # conj head points at the first conjunct head (spaCy attaches to "Gazprom")
    doc.tokens[1].head_index = 0
    doc.tokens[2].head_index = 0
    sp = spans(doc, (0, 1, "ORG"), (2, 3, "ORG"), (4, 6, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    pairs = {(t.subject.text, t.obj.text) for t in triples}
    assert ("Gazprom", "Nord Stream") in pairs
    assert ("Rosneft", "Nord Stream") in pairs


# --------------------------------------------------------------------------- #
# Negation and hedging
# --------------------------------------------------------------------------- #
def test_negation_flagged(engine):
    text = "Kerimov does not own Midea"
    doc = make_tree(
        text,
        [
            Tok("Kerimov", "PROPN", "nsubj", head=3),
            Tok("does", "AUX", "aux", head=3, lemma="do"),
            Tok("not", "PART", "neg", head=3),
            Tok("own", "VERB", "ROOT", head=3, lemma="own"),
            Tok("Midea", "PROPN", "dobj", head=3),
        ],
    )
    sp = spans(doc, (0, 1, "PERSON"), (4, 5, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert len(triples) == 1
    assert triples[0].negated is True


def test_hedging_flagged_and_discounted(engine):
    text = "Prigozhin allegedly funded Wagner"
    doc = make_tree(
        text,
        [
            Tok("Prigozhin", "PROPN", "nsubj", head=2),
            Tok("allegedly", "ADV", "advmod", head=2),
            Tok("funded", "VERB", "ROOT", head=2, lemma="fund"),
            Tok("Wagner", "PROPN", "dobj", head=2),
        ],
    )
    sp = spans(doc, (0, 1, "PERSON"), (3, 4, "ORG"))
    triples = engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0)

    assert triples[0].hedged is True
    decision = engine._decide(triples[0], EntityType.PERSON, EntityType.ORGANIZATION, text)
    assert decision.hedged is True
    assert decision.evidence < 0.98


# --------------------------------------------------------------------------- #
# No-argument verbs are ignored
# --------------------------------------------------------------------------- #
def test_verb_without_object_yields_nothing(engine):
    text = "Kerimov arrived"
    doc = make_tree(
        text,
        [
            Tok("Kerimov", "PROPN", "nsubj", head=1),
            Tok("arrived", "VERB", "ROOT", head=1, lemma="arrive"),
        ],
    )
    sp = spans(doc, (0, 1, "PERSON"))
    assert engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0) == []


def test_distance_cap_drops_far_arguments(engine):
    text = " ".join(f"t{i}" for i in range(30))
    doc = make_tree(
        text,
        [Tok(f"t{i}", "NOUN", "dep", head=0) for i in range(30)],
    )
    # rebuild as a proper SVO with a wide gap
    text = "Alpha " + " ".join(f"filler{i}" for i in range(40)) + " owns Beta"
    tokens = [Tok("Alpha", "PROPN", "nsubj", head=41)]
    for i in range(40):
        tokens.append(Tok(f"filler{i}", "NOUN", "dep", head=0))
    tokens.append(Tok("owns", "VERB", "ROOT", head=41, lemma="own"))
    tokens.append(Tok("Beta", "PROPN", "dobj", head=41))
    doc = make_tree(text, tokens)
    sp = spans(doc, (0, 1, "ORG"), (42, 43, "ORG"))
    assert engine._dependency_triples(doc, next(iter(doc.sents)), sp, 0) == []


# --------------------------------------------------------------------------- #
# Full engine integration with a fake pipeline
# --------------------------------------------------------------------------- #
def test_parse_document_uses_dependency_route(engine, monkeypatch):
    """With a statistical model present, triples are priced as DEPENDENCY."""
    from puppetnet.models import Document

    text = "Suleiman Kerimov owns Midea Holdings"
    doc = make_tree(
        text,
        [
            Tok("Suleiman", "PROPN", "compound", head=1),
            Tok("Kerimov", "PROPN", "nsubj", head=2),
            Tok("owns", "VERB", "ROOT", head=2, lemma="own"),
            Tok("Midea", "PROPN", "compound", head=4),
            Tok("Holdings", "PROPN", "dobj", head=2),
        ],
    )
    doc.set_ents([FakeSpan(doc, 0, 2, "PERSON"), FakeSpan(doc, 3, 5, "ORG")])

    # Pretend a transformer model is loaded: real NER + real dependency parse.
    monkeypatch.setattr(engine, "nlp", lambda chunk: doc)
    monkeypatch.setattr(engine, "has_parser", True)
    monkeypatch.setattr(engine, "has_statistical_ner", True)

    document = Document(
        doc_id="doc-1",
        source_id="rss:example",
        url="https://example.test/article",
        title="Sanctions report",
        text=text,
        source_weight=0.4,
    )
    result = engine.parse_document(document)

    assert result.dependency_triples == 1
    assert result.relations, "dependency route should yield a relation"
    relation = result.relations[0]
    assert relation.predicate is RelationType.OWNS
    assert relation.method is ExtractionMethod.DEPENDENCY
    # 0.4 source weight × 1.0 dependency factor × ~0.98 evidence
    assert relation.confidence == pytest.approx(0.392, abs=0.02)
    assert relation.subject.name == "Suleiman Kerimov"
    assert relation.obj.name == "Midea Holdings"


def test_negated_dependency_triple_is_dropped(engine, monkeypatch):
    """Negated SVO triples must never become graph edges."""
    from puppetnet.models import Document

    text = "Kerimov does not own Midea"
    doc = make_tree(
        text,
        [
            Tok("Kerimov", "PROPN", "nsubj", head=3),
            Tok("does", "AUX", "aux", head=3, lemma="do"),
            Tok("not", "PART", "neg", head=3),
            Tok("own", "VERB", "ROOT", head=3, lemma="own"),
            Tok("Midea", "PROPN", "dobj", head=3),
        ],
    )
    doc.set_ents([FakeSpan(doc, 0, 1, "PERSON"), FakeSpan(doc, 4, 5, "ORG")])
    monkeypatch.setattr(engine, "nlp", lambda chunk: doc)
    monkeypatch.setattr(engine, "has_parser", True)
    monkeypatch.setattr(engine, "has_statistical_ner", True)

    document = Document(doc_id="doc-2", source_id="rss:example", url="u", text=text, source_weight=0.4)
    result = engine.parse_document(document)

    assert result.negated_dropped == 1
    assert result.relations == []
