"""Live multilingual extraction + Workers AI integration tests.

Deselected by default (``-m 'not integration'``). Enable deliberately:

    RUN_GLINER_INTEGRATION=1 pytest -m integration tests/test_nlp_integration.py -k gliner

Torch and the 209M-parameter checkpoint are downloaded on first use (~1 GB),
which is exactly why the offline unit suite never touches this path.

Workers AI:

    CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... \\
        pytest -m integration tests/test_nlp_integration.py -k workers_ai

The free-tier test spends a few neurons; it never retries past the daily cap.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_network():
    """Override the hermetic-suite socket block for these live tests."""

    yield


def _require_gliner():
    pytest.importorskip("gliner", reason="install packages/pipeline[gliner] for live GLiNER")
    if os.environ.get("RUN_GLINER_INTEGRATION") != "1":
        pytest.skip("set RUN_GLINER_INTEGRATION=1 to download and run gliner_multi-v2.1")


def _require_workers_ai() -> tuple[str, str]:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account or not token:
        pytest.skip("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required")
    return account, token


MULTILINGUAL_SAMPLES: dict[str, dict[str, object]] = {
    "de": {
        "text": "Dr. Anna Müller von der Acme GmbH traf sich in Berlin mit dem Vorstand.",
        "persons": {"anna", "müller"},
        "organizations": {"acme"},
        "locations": {"berlin"},
    },
    "ru": {
        "text": "Иван Петров работает в компании Ромашка в Москве.",
        "persons": {"иван", "петров"},
        "organizations": {"ромашка"},
        "locations": {"москве", "москва"},
    },
    "ar": {
        "text": "التقى محمد علي بشركة النور في دبي.",
        "persons": {"محمد"},
        "organizations": {"النور"},
        "locations": {"دبي"},
    },
    "fr": {
        "text": "Jean-Luc Picard dirige la société Étoile Bleue à Paris.",
        "persons": {"picard", "jean-luc"},
        "organizations": {"étoile", "bleue"},
        "locations": {"paris"},
    },
    "es": {
        "text": "Lucía Fernández viajó a Madrid el 3 de octubre de 2020.",
        "persons": {"lucía", "fernández"},
        "organizations": set(),
        "locations": {"madrid"},
    },
    "en": {
        "text": "MV Nordic Star (IMO 9176187) docked in Rotterdam on 3 October 2020.",
        "persons": set(),
        "organizations": set(),
        "locations": {"rotterdam"},
    },
}


def test_gliner_multi_v21_extracts_multilingual_entities():
    _require_gliner()
    from nlp.extractor import GLiNERExtractor

    extractor = GLiNERExtractor(
        "urchade/gliner_multi-v2.1",
        threshold=0.35,
        runtime="torch",
        eager_load=False,
    )
    assert extractor.load().available
    try:
        for language, sample in MULTILINGUAL_SAMPLES.items():
            result = extractor.extract(str(sample["text"]), language=language)
            assert result.entities, f"{language}: no entities at all"
            assert all(str(sample["text"])[e.start_char:e.end_char] == e.text for e in result.entities)
            labels = {entity.label for entity in result.entities}
            assert "Person" in labels or language == "en", f"{language}: {labels}"

            def surfaces(label: str, entities=result.entities) -> set[str]:
                return {entity.text.casefold() for entity in entities if entity.label == label}

            for expected in sample["persons"]:
                assert any(expected in surface for surface in surfaces("Person")), f"{language}: person {expected}"
            for expected in sample["organizations"]:
                assert any(expected in surface for surface in surfaces("Organization")), f"{language}: org {expected}"
            for expected in sample["locations"]:
                assert any(expected in surface for surface in surfaces("Location")), f"{language}: loc {expected}"
    finally:
        extractor.close()


def test_gliner_multi_v21_honours_the_384_token_window():
    _require_gliner()
    from nlp.extractor import GLiNERExtractor

    text = " ".join(
        f"Absatz {index}: Anna Müller traf Peter Schmidt in Berlin." for index in range(120)
    )
    extractor = GLiNERExtractor("urchade/gliner_multi-v2.1", threshold=0.4, eager_load=False)
    try:
        result = extractor.extract(text, language="de")
        assert len(result.chunks) > 1
        assert all(chunk.token_count <= 384 for chunk in result.chunks)
        assert result.entity_counts.get("Person", 0) >= 1
    finally:
        extractor.close()


def test_onnx_runtime_extracts_when_exported_model_is_available():
    _require_gliner()
    pytest.importorskip("onnxruntime", reason="install packages/pipeline[onnx]")
    from nlp.extractor import ExtractionError, GLiNEExtractor

    extractor = GLiNEExtractor("urchade/gliner_multi-v2.1", runtime="onnx", eager_load=False)
    try:
        extractor.load()
    except ExtractionError as exc:
        pytest.skip(f"checkpoint has no exported model.onnx: {exc}")
    try:
        result = extractor.extract("Anna Müller works at Acme in Berlin.", language="en")
        assert result.entities
        assert all(0.0 <= entity.score <= 1.0 for entity in result.entities)
    finally:
        extractor.close()


def test_workers_ai_returns_grounded_triplets_for_german_text():
    account, token = _require_workers_ai()
    from nlp.triplet_extractor import CloudflareTripletExtractor

    text = (
        "Die Acme GmbH mit Sitz in München gehört der Familie Müller. "
        "Frau Müller ist Geschäftsführerin der Acme GmbH."
    )
    with CloudflareTripletExtractor(
        account,
        token,
        model=os.environ.get("CF_AI_MODEL", "@cf/meta/llama-3.1-8b-instruct"),
        max_chunks=2,
        max_triplets_per_chunk=8,
    ) as extractor:
        result = extractor.extract(text, language="de")
        assert result.calls >= 1
        assert result.neurons_used <= 10_000
        assert result.triplets, "the model returned no usable triplet"
        folded = text.casefold()
        for item in result.triplets:
            assert item.subject.casefold() in folded
            assert item.obj.casefold() in folded
            assert item.predicate in extractor.predicates
        status = extractor.budget_status()
        assert status["daily_limit"] == 10_000
        assert status["used"] <= status["daily_limit"]
