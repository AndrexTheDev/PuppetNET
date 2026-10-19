"""CPU GLiNER entity recognition with the existing evidence/relation writer contract.

GLiNER predicts entities, NOT relations. Relations use the existing conservative
pattern/co-occurrence path (no dependency parser, including its confidence penalty).
Explicit GLiNER selection fails on load errors; it never silently switches models.
"""
from __future__ import annotations

import math
from typing import Any

from .nlp_engine import NLPEngine, SentenceView, SpanEntity, split_sentences

LABELS = {"person": "PERSON", "organization": "ORG", "location": "LOC", "aircraft": "CRAFT", "vessel": "CRAFT"}


class GLiNEREngine(NLPEngine):
    def __init__(self, settings: Any, *, model: Any = None, eager_load: bool = True) -> None:
        self.model = model
        super().__init__(settings, eager_load=False)
        if eager_load:
            self.load()

    def load(self) -> GLiNEREngine:
        if self._loaded:
            return self
        if self.model is None:
            try:
                from gliner import GLiNER

                self.model = GLiNER.from_pretrained(self.settings.gliner_model)
                self.model.to("cpu")
                self.model.eval()
            except Exception as exc:
                raise RuntimeError("GLiNER model load failed; install pipeline[gliner] and cache/download the model") from exc
        self.backend = f"gliner:{self.settings.gliner_model}"
        self.has_ner = self.has_statistical_ner = True
        self.has_parser = False
        self._loaded = True
        return self

    def _sentences_for_chunk(self, chunk: str) -> list[SentenceView]:
        views = []
        # Short windows avoid model truncation and bound CPU/memory. A long
        # sentence may lose cross-window relations; no invented dependency parse.
        for _, _, sentence in split_sentences(chunk, max_sentences=self.settings.nlp_max_sentences_per_doc):
            for offset in range(0, len(sentence), 400):
                if len(views) >= self.settings.nlp_max_sentences_per_doc:
                    return views
                text = sentence[offset:offset + 400]
                spans = []
                for hit in self.model.predict_entities(text, list(LABELS), threshold=self.settings.gliner_threshold):
                    try:
                        start, end = int(hit["start"]), int(hit["end"])
                        score = float(hit["score"])
                        label = LABELS[hit["label"]]
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not (0 <= start < end <= len(text)) or not math.isfinite(score):
                        continue
                    if not self.settings.gliner_threshold <= score <= 1:
                        continue
                    spans.append(SpanEntity(text[start:end], label, start, end, confidence=score))
                    if len(spans) >= self.settings.nlp_max_entities_per_doc:
                        break
                views.append(SentenceView(len(views), text, spans, [], parse_ok=False, note="gliner-no-parser"))
        return views

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "model": self.settings.gliner_model,
                "threshold": self.settings.gliner_threshold, "device": "cpu", "window_chars": 400}

    def close(self) -> None:
        self.model = None
        super().close()
