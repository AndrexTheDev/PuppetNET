"""Offline contract tests: no model download, GPU, paid service or database."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from puppetnet.config import ConfigError, load_settings
from puppetnet.parsing.gliner_engine import GLiNEREngine


class Model:
    def __init__(self):
        self.calls = []

    def predict_entities(self, text, labels, threshold):
        self.calls.append((text, labels, threshold))
        return [
            {"start": text.index(name), "end": text.index(name) + len(name), "label": label, "score": 0.9}
            for name, label in [("Alice", "person"), ("Example Holdings", "organization")]
            if name in text
        ]


def test_gliner_reuses_graph_contract_and_conservative_relations(settings):
    model = Model()
    engine = GLiNEREngine(settings, model=model)
    result = engine.parse_text("Alice owns Example Holdings.")
    assert {entity.name for entity in result.entities} == {"Alice", "Example Holdings"}
    assert result.backend.startswith("gliner:")
    assert not engine.has_parser
    assert result.dependency_triples == 0
    assert result.relations
    assert all(relation.confidence <= 0.32 for relation in result.relations)
    assert engine.describe()["device"] == "cpu"
    assert model.calls
    engine.close()
    assert engine.model is None
    assert not engine.available or not engine._loaded


def test_gliner_windows_bound_model_input(settings):
    model = Model()
    engine = GLiNEREngine(settings, model=model)
    engine.parse_text("Alice " + "x " * 1000)
    assert model.calls
    assert all(len(text) <= 400 for text, _, _ in model.calls)


def test_gliner_filters_invalid_predictions(settings):
    model = SimpleNamespace(predict_entities=lambda *args, **kwargs: [
        {"start": 0, "end": 5, "label": "person", "score": float("nan")},
        {"start": -1, "end": 5, "label": "person", "score": 0.9},
        {"start": 0, "end": 5, "label": "person", "score": 0.1},
        {"start": 0, "end": 5, "label": "unknown", "score": 0.9},
        {},
    ])
    engine = GLiNEREngine(settings, model=model)
    assert engine.parse_text("Alice works.").entities == []


def test_gliner_load_fails_closed(settings, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("model unavailable")
    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=fail)))
    with pytest.raises(RuntimeError, match="GLiNER model load failed"):
        GLiNEREngine(settings)


@pytest.mark.parametrize("env", [{"NLP_BACKEND": "typo"}, {"GLINER_THRESHOLD": "0"}, {"GLINER_THRESHOLD": "nan"}])
def test_gliner_settings_reject_invalid_values(env):
    with pytest.raises(ConfigError):
        load_settings({"DRY_RUN": "true", **env})


def test_pipeline_selects_gliner(settings, monkeypatch):
    from puppetnet.models import SourceSpec, SourceType
    from puppetnet.pipeline import IngestPipeline

    sentinel = SimpleNamespace(backend="gliner:fixture", load_errors=[])
    monkeypatch.setattr("puppetnet.parsing.gliner_engine.GLiNEREngine", lambda settings: sentinel)
    settings.nlp_backend = "gliner"
    pipeline = IngestPipeline(settings)
    pipeline._init_nlp([SourceSpec(id="fixture", name="Fixture", adapter="rss", kind=SourceType.UNSTRUCTURED)])
    assert pipeline.nlp is sentinel


def test_gliner_loads_once_on_cpu(settings, monkeypatch):
    class Loaded(Model):
        def __init__(self):
            super().__init__()
            self.device = None
            self.evaluating = False

        def to(self, device):
            self.device = device

        def eval(self):
            self.evaluating = True

    loaded = Loaded()
    requests = []

    def load(name):
        requests.append(name)
        return loaded

    monkeypatch.setitem(sys.modules, "gliner", SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=load)))
    engine = GLiNEREngine(settings)
    engine.load()
    assert requests == [settings.gliner_model]
    assert loaded.device == "cpu"
    assert loaded.evaluating
