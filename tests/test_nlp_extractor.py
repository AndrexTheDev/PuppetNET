"""GLiNER multi-v2.1 extractor: chunking, grounding, runtime selection.

Everything here runs offline with an injected model. The live ``gliner_multi-v2.1``
run lives in ``test_nlp_integration.py`` behind the ``integration`` marker.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from nlp.extractor import (
    DEFAULT_GLINER_MODEL,
    EntitySpan,
    ExtractionError,
    GLiNEExtractor,
)
from nlp.labels import OSINT_LABELS
from tests.nlp_fakes import CharTokenizer, FakeGLiNER, Mention, WordTokenizer

MULTILINGUAL_TEXT = "\n".join(
    [
        "Dr. Anna Müller arbeitet bei der Acme GmbH in München.",
        "Иван Петров встретился с Марией Соколовой в Москве.",
        "التقى محمد علي بشركة النور في دبي.",
        "Jean-Luc Picard dirige la société Étoile Bleue à Paris.",
        "Lucía Fernández viajó a Madrid el 3 de octubre de 2020.",
        "Die MV Nordic Star (IMO 9176187) legte in Rotterdam an.",
        "Tail number N123AB was seen at Heathrow.",
        "Das Konto DE89 3704 0044 0532 0130 00 wurde eingefroren.",
        "Wallet 0x52908400098527886E0F7030069857D2E4169EE7 wurde geleert.",
    ]
)

MULTILINGUAL_MENTIONS = [
    Mention("Anna Müller", "Person", 0.94),
    Mention("Acme GmbH", "Organization", 0.91),
    Mention("München", "Location", 0.89),
    Mention("Иван Петров", "Person", 0.93),
    Mention("Марией Соколовой", "Person", 0.9),
    Mention("Москве", "Location", 0.88),
    Mention("محمد علي", "Person", 0.92),
    Mention("شركة النور", "Organization", 0.87),
    Mention("دبي", "Location", 0.9),
    Mention("Jean-Luc Picard", "Person", 0.95),
    Mention("Étoile Bleue", "Organization", 0.9),
    Mention("Paris", "Location", 0.86),
    Mention("Lucía Fernández", "Person", 0.94),
    Mention("Madrid", "Location", 0.89),
    Mention("3 de octubre de 2020", "Date", 0.84),
    Mention("MV Nordic Star", "Vehicle", 0.9),
    Mention("IMO 9176187", "IMO_Number", 0.93),
    Mention("IMO 9176187", "Vehicle", 0.5),
    Mention("N123AB", "Aircraft_Registration", 0.96),
    Mention("Heathrow", "Location", 0.85),
    Mention("DE89 3704 0044 0532 0130 00", "Bank_Account", 0.97),
    Mention("0x52908400098527886E0F7030069857D2E4169EE7", "Crypto_Address", 0.98),
]


def test_every_documented_label_is_extracted_from_multilingual_text():
    model = FakeGLiNER(MULTILINGUAL_MENTIONS)
    extractor = GLiNEExtractor(
        model=model,
        tokenizer=WordTokenizer(),
        max_tokens=80,
        overlap_tokens=16,
        threshold=0.5,
    )
    result = extractor.extract(MULTILINGUAL_TEXT, language="mul")

    assert result.model_id == DEFAULT_GLINER_MODEL == "urchade/gliner_multi-v2.1"
    assert result.labels == OSINT_LABELS
    labels = {entity.label for entity in result.entities}
    assert labels == set(OSINT_LABELS)
    assert "Date" in result.entity_counts
    assert result.chunks and all(chunk.token_count <= 80 for chunk in result.chunks)
    assert result.dropped_predictions == 0
    assert {"IMO_Number", "Vehicle"} <= {e.label for e in result.entities if e.text == "IMO 9176187"}
    assert all(MULTILINGUAL_TEXT[e.start_char:e.end_char] == e.text for e in result.entities)
    assert all(0.5 <= e.score <= 1.0 for e in result.entities)
    assert all(e.properties["osint_label"] == e.label for e in result.entities)
    assert model.kwargs and model.kwargs[0]["threshold"] == 0.5
    assert model.kwargs[0]["labels"] == list(OSINT_LABELS)


def test_entity_offsets_survive_language_switches():
    model = FakeGLiNER(MULTILINGUAL_MENTIONS)
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer(), max_tokens=80, overlap_tokens=16)
    result = extractor.extract(MULTILINGUAL_TEXT)
    by_text = {entity.text: entity for entity in result.entities}
    assert by_text["Иван Петров"].start_char == MULTILINGUAL_TEXT.index("Иван Петров")
    assert by_text["محمد علي"].start_char == MULTILINGUAL_TEXT.index("محمد علي")
    assert by_text["Étoile Bleue"].label == "Organization"
    assert by_text["N123AB"].label == "Aircraft_Registration"


def test_overlapping_windows_do_not_duplicate_entities():
    text = " ".join(f"Alice meets Acme in Berlin on day {index}." for index in range(40))
    model = FakeGLiNER(
        [
            Mention("Alice", "Person", 0.9),
            Mention("Acme", "Organization", 0.9),
            Mention("Berlin", "Location", 0.9),
        ]
    )
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer(), max_tokens=40, overlap_tokens=12)
    result = extractor.extract(text)
    assert len(result.chunks) > 5
    addresses = [(entity.start_char, entity.end_char, entity.label) for entity in result.entities]
    assert len(addresses) == len(set(addresses))
    assert sum(1 for entity in result.entities if entity.text == "Alice") == 40


def test_lower_case_model_labels_are_canonicalised():
    model = FakeGLiNER([Mention("Alice", "Person", 0.9, echo_label="person")])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer())
    result = extractor.extract("Alice works at Acme.")
    assert [entity.label for entity in result.entities] == ["Person"]


def test_threshold_and_invalid_spans_are_enforced():
    mentions = [
        Mention("Alice", "Person", 0.3),
        Mention("Berlin", "Location", 0.9),
    ]
    extractor = GLiNEExtractor(model=FakeGLiNER(mentions), tokenizer=WordTokenizer())
    result = extractor.extract("Alice works in Berlin.")
    assert [entity.text for entity in result.entities] == ["Berlin"]

    permissive = GLiNEExtractor(model=FakeGLiNER(mentions), tokenizer=WordTokenizer(), threshold=0.2)
    assert {entity.text for entity in permissive.extract("Alice works in Berlin.").entities} == {
        "Alice",
        "Berlin",
    }


@pytest.mark.parametrize(
    "hit",
    [
        {"start": -1, "end": 5, "label": "Person", "score": 0.9},
        {"start": 0, "end": 999, "label": "Person", "score": 0.9},
        {"start": 0, "end": 5, "label": "Spaceship", "score": 0.9},
        {"start": 0, "end": 5, "label": "Person", "score": float("nan")},
        {"start": 0, "end": 5, "label": "Person", "score": 0.1},
        {},
        "not-a-dict",
    ],
)
def test_unusable_predictions_are_dropped_and_counted(hit):
    model = SimpleNamespace(predict_entities=lambda *args, **kwargs: [hit])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer())
    result = extractor.extract("Alice works.")
    assert result.entities == []
    assert result.dropped_predictions == 1


def test_span_type_converts_to_the_existing_nlp_engine_contract():
    span = EntitySpan("Alice", "Person", 10, 15, 0.9)
    converted = span.to_span_entity(sentence_start=10)
    assert converted.text == "Alice"
    assert converted.label == "PERSON"
    assert (converted.start_char, converted.end_char) == (0, 5)
    assert converted.confidence == 0.9
    assert span.to_span_entity(sentence_start=0).start_char == 10
    assert EntitySpan("1.1.2020", "Date", 0, 8, 0.8).to_span_entity().label == "DATE"


def test_truncation_and_empty_documents_are_reported_not_hidden():
    model = FakeGLiNER([Mention("Alice", "Person")])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer(), max_chars=20)
    result = extractor.extract("Alice " + "x " * 100)
    assert result.truncated is True
    assert "truncated-to-20-chars" in result.warnings
    empty = extractor.extract("   ")
    assert empty.entities == []
    assert "empty-text" in empty.warnings


def test_entity_cap_bounds_hostile_documents():
    text = " ".join(f"Alice number {index}." for index in range(30))
    model = FakeGLiNER([Mention("Alice", "Person")])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer(), max_entities=5)
    result = extractor.extract(text)
    assert len(result.entities) == 5
    assert "entity-cap-reached" in result.warnings


def test_invalid_configuration_is_rejected_early():
    with pytest.raises(ValueError, match="runtime"):
        GLiNEExtractor(model=FakeGLiNER([]), runtime="tensorflow")
    with pytest.raises(ValueError, match="threshold"):
        GLiNEExtractor(model=FakeGLiNER([]), threshold=0)
    with pytest.raises(ValueError, match="overlap"):
        GLiNEExtractor(model=FakeGLiNER([]), max_tokens=16, overlap_tokens=16)
    with pytest.raises(ValueError, match="OSINT label"):
        GLiNEExtractor(model=FakeGLiNER([]), labels=["Person", "Email"])


def test_batch_extraction_keeps_documents_independent():
    model = FakeGLiNER([Mention("Alice", "Person")])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer())
    results = extractor.extract_batch(["Alice left.", "No names here.", "Alice returned."])
    assert [len(result.entities) for result in results] == [1, 0, 1]


def test_missing_gliner_dependency_is_an_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "gliner", None)
    extractor = GLiNEExtractor(eager_load=False)
    with pytest.raises(ExtractionError, match="not installed"):
        extractor.load()


def test_torch_runtime_is_requested_explicitly(monkeypatch):
    calls: dict[str, object] = {}

    def fake_from_pretrained(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return SimpleNamespace()

    module = SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=fake_from_pretrained))
    monkeypatch.setitem(sys.modules, "gliner", module)
    extractor = GLiNEExtractor(eager_load=False)
    extractor.load()
    assert calls["args"] == (DEFAULT_GLINER_MODEL,)
    assert calls["kwargs"]["map_location"] == "cpu"
    assert "runtime" not in calls["kwargs"]
    assert extractor.backend == f"gliner:{DEFAULT_GLINER_MODEL}:torch"


def test_onnx_runtime_uses_the_model_export_and_never_falls_back(monkeypatch):
    calls: dict[str, object] = {}

    def fake_from_pretrained(*args, **kwargs):
        calls["kwargs"] = kwargs
        return SimpleNamespace()

    module = SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=fake_from_pretrained))
    monkeypatch.setitem(sys.modules, "gliner", module)
    extractor = GLiNEExtractor(runtime="onnx", eager_load=False, onnx_model_file="custom.onnx")
    extractor.load()
    assert calls["kwargs"]["runtime"] == "onnxruntime"
    assert calls["kwargs"]["runtime_model_file"] == "custom.onnx"
    assert extractor.backend.endswith(":onnxruntime")


def test_onnx_runtime_falls_back_only_to_the_legacy_signature(monkeypatch):
    attempts: list[dict[str, object]] = []

    def fake_from_pretrained(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise TypeError("unexpected keyword argument 'runtime'")
        return SimpleNamespace()

    module = SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=fake_from_pretrained))
    monkeypatch.setitem(sys.modules, "gliner", module)
    GLiNEExtractor(runtime="onnx", eager_load=False).load()
    assert attempts[0]["runtime"] == "onnxruntime"
    assert attempts[1]["load_onnx_model"] is True


def test_model_load_failure_is_wrapped_with_context(monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("checkpoint unavailable")

    module = SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=explode))
    monkeypatch.setitem(sys.modules, "gliner", module)
    extractor = GLiNEExtractor(model_id="missing/model", eager_load=False)
    with pytest.raises(ExtractionError, match="model load failed"):
        extractor.load()
    assert extractor.load_errors and "checkpoint unavailable" in extractor.load_errors[0]


def test_describe_reports_the_bound_model_and_window():
    model = FakeGLiNER([])
    extractor = GLiNEExtractor(model=model, tokenizer=WordTokenizer(), device="cpu")
    described = extractor.describe()
    assert described["model"] == DEFAULT_GLINER_MODEL
    assert described["runtime"] == "torch"
    assert described["device"] == "cpu"
    assert described["max_tokens"] == 384
    assert described["overlap_tokens"] == 64
    assert described["tokenizer"] == "WordTokenizer"
    assert described["labels"] == list(OSINT_LABELS)


def test_character_tokenizer_forces_many_windows():
    model = FakeGLiNER([Mention("Alice", "Person")])
    extractor = GLiNEExtractor(model=model, tokenizer=CharTokenizer(), max_tokens=60, overlap_tokens=10)
    result = extractor.extract("Alice " * 30)
    assert len(result.chunks) > 1
