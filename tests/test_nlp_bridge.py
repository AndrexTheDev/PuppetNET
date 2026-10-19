"""The bridge is the only place a model response can become a graph write."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from nlp.extractor import EntitySpan
from nlp.graph_bridge import graph_from_triplets, triplet_predicate_counts
from nlp.triplet_extractor import Triplet, TripletExtractionResult
from puppetnet.models import Document, EntityType, ExtractionMethod, RelationType
from puppetnet.parsing.nlp_engine import ParseResult
from puppetnet.pipeline import IngestPipeline

TEXT = "Die Acme GmbH gehört der Globex Ltd. Beide sitzen in Berlin."


def document() -> Document:
    return Document(
        doc_id="doc-1",
        source_id="news_world",
        url="https://example.org/a",
        title="Acme",
        text=TEXT,
        language="de",
    )


def triplets(*items: Triplet, model: str = "@cf/meta/llama-3.1-8b-instruct") -> TripletExtractionResult:
    return TripletExtractionResult(triplets=list(items), model=model)


def test_triplets_become_entities_and_relations_with_conservative_confidence():
    extraction = [
        EntitySpan("Acme GmbH", "Organization", TEXT.index("Acme GmbH"), TEXT.index("Acme GmbH") + 9, 0.9),
        EntitySpan("Globex Ltd", "Organization", TEXT.index("Globex Ltd"), TEXT.index("Globex Ltd") + 10, 0.9),
    ]
    result = triplets(
        Triplet(
            subject="Acme GmbH",
            predicate="OWNS",
            obj="Globex Ltd",
            confidence=0.9,
            evidence="Die Acme GmbH gehört der Globex Ltd.",
            subject_char=TEXT.index("Acme GmbH"),
            object_char=TEXT.index("Globex Ltd"),
        )
    )
    entities, relations = graph_from_triplets(
        document(),
        result,
        known_entities=extraction,
        source_id="news_world",
    )
    assert {entity.name for entity in entities} == {"Acme GmbH", "Globex Ltd"}
    assert {entity.entity_type for entity in entities} == {EntityType.ORGANIZATION}
    assert all(entity.doc_ids == {"doc-1"} for entity in entities)
    assert all(entity.source_ids == {"news_world"} for entity in entities)
    assert all(entity.mentions and entity.mentions[0].detector == "workers_ai" for entity in entities)

    assert len(relations) == 1
    relation = relations[0]
    assert relation.predicate is RelationType.OWNS
    assert relation.method is ExtractionMethod.PATTERN
    # 0.4 unstructured source weight × 0.9 model confidence; never above a
    # parsed dependency triple.
    assert relation.confidence == 0.36
    assert relation.doc_id == "doc-1"
    assert relation.evidence == "Die Acme GmbH gehört der Globex Ltd."
    assert relation.extra["extractor"] == "cloudflare-workers-ai"
    assert relation.extra["llm_confidence"] == 0.9


def test_unknown_arguments_stay_unknown_instead_of_becoming_people():
    result = triplets(Triplet(subject="Q-7741", predicate="LINKED_OFFSHORE", obj="Acme GmbH", confidence=0.7))
    entities, relations = graph_from_triplets(document(), result)
    assert {entity.name: entity.entity_type for entity in entities} == {
        "Q-7741": EntityType.UNKNOWN,
        "Acme GmbH": EntityType.UNKNOWN,
    }
    assert relations[0].predicate is RelationType.LINKED_OFFSHORE


def test_self_edges_and_duplicate_signatures_are_collapsed():
    result = triplets(
        Triplet("Acme GmbH", "OWNS", "Globex Ltd", 0.5),
        Triplet("Acme GmbH", "OWNS", "Globex Ltd", 0.9),
        Triplet("Acme GmbH", "OWNS", "Acme GmbH", 0.9),
    )
    entities, relations = graph_from_triplets(document(), result)
    assert len(entities) == 2
    assert len(relations) == 1
    assert relations[0].extra["llm_confidence"] == 0.9


def test_gl_surfaces_keep_the_model_labels_used_for_the_graph():
    result = triplets(
        Triplet(
            subject="Acme GmbH",
            predicate="LOCATED_IN",
            obj="Berlin",
            confidence=0.8,
            subject_label="Organization",
            object_label="Location",
        )
    )
    entities, _ = graph_from_triplets(document(), result)
    assert {entity.name: entity.entity_type for entity in entities} == {
        "Acme GmbH": EntityType.ORGANIZATION,
        "Berlin": EntityType.LOCATION,
    }


def test_predicate_histogram_exposes_the_associated_with_fallback():
    counts = triplet_predicate_counts(
        [
            Triplet("A", "OWNS", "B"),
            Triplet("A", "invented_verb", "B"),
            Triplet("A", "related to", "B"),
        ]
    )
    assert counts["OWNS"] == 1
    assert counts["ASSOCIATED_WITH"] == 2


class FakeTripletExtractor:
    def __init__(self, result: TripletExtractionResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def extract(self, text, *, language=None, entities=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def pipeline_stub(extractor: FakeTripletExtractor, *, cap: int = 2) -> IngestPipeline:
    pipeline = IngestPipeline.__new__(IngestPipeline)
    pipeline.settings = SimpleNamespace(cf_ai_max_documents=cap, nlp_language="de", cf_ai_model="model-x")
    pipeline.logger = logging.getLogger("test.nlp.bridge")
    pipeline.triplets = extractor
    pipeline._triplet_documents = 0
    pipeline._triplet_cap_warned = False
    return pipeline


def test_pipeline_augmentation_is_opt_in_and_capped():
    result = triplets(
        Triplet(
            subject="Acme GmbH",
            predicate="OWNS",
            obj="Globex Ltd",
            confidence=0.9,
            subject_char=TEXT.index("Acme GmbH"),
            object_char=TEXT.index("Globex Ltd"),
        )
    )
    extractor = FakeTripletExtractor(result)
    pipeline = pipeline_stub(extractor, cap=2)
    spec = SimpleNamespace(id="news_world")

    first = ParseResult(doc_id="doc-1", backend="gliner")
    pipeline._augment_with_triplets(document(), first, spec)
    assert len(first.entities) == 2
    assert len(first.relations) == 1
    assert first.pattern_triples == 1
    assert first.relations[0].source_id == "news_world"

    second = ParseResult(doc_id="doc-2", backend="gliner")
    pipeline._augment_with_triplets(document(), second, spec)
    assert len(second.relations) == 1

    third = ParseResult(doc_id="doc-3", backend="gliner")
    pipeline._augment_with_triplets(document(), third, spec)
    assert third.entities == [] and third.relations == []
    assert extractor.calls == 2


def test_pipeline_failure_is_fail_soft():
    extractor = FakeTripletExtractor(RuntimeError("Workers AI returned HTTP 503"))
    pipeline = pipeline_stub(extractor)
    parsed = ParseResult(doc_id="doc-1", backend="gliner")
    pipeline._augment_with_triplets(document(), parsed, SimpleNamespace(id="news_world"))
    assert parsed.entities == [] and parsed.relations == []
    assert any("Workers AI failed" in warning for warning in parsed.warnings)


def test_pipeline_does_nothing_without_an_extractor():
    pipeline = pipeline_stub(FakeTripletExtractor(triplets()))
    pipeline.triplets = None
    parsed = ParseResult(doc_id="doc-1", backend="gliner")
    pipeline._augment_with_triplets(document(), parsed, SimpleNamespace(id="news_world"))
    assert parsed.entities == [] and parsed.relations == []
