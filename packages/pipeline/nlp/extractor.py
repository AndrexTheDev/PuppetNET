"""GLiNER ``gliner_multi-v2.1`` entity extraction with 384-token chunking.

The extractor is intentionally usable as a standalone library as well as from
:class:`puppetnet.parsing.gliner_engine.GLiNEREngine`:

>>> extractor = GLiNERExtractor()                 # loads the multilingual model
>>> result = extractor.extract("Alice trifft Acme in Berlin.", language="de")
>>> [(span.text, span.label) for span in result.entities]
[('Alice', 'Person'), ('Acme', 'Organization'), ('Berlin', 'Location')]

Design constraints
------------------
* **PyTorch or ONNX Runtime.** ``runtime='torch'`` is the default. ``runtime=
  'onnx'`` loads a pre-exported ``model.onnx`` through GLiNER's ONNX Runtime
  backend; the model repository must contain that artefact (or a local export).
  No silent fallback between runtimes: a requested runtime that cannot load
  raises ``RuntimeError``.
* **384-token context with sentence overlap.** See :mod:`nlp.chunking`.
* **Extractive only.** Every span is a substring of the input at the offsets
  reported by the model. No string is normalised, translated or invented; the
  original surface form is what reaches the graph.
* **Bounded.** ``max_chars``, ``max_sentences`` and ``max_entities`` stop one
  hostile or pathological document from exhausting a GitHub-hosted runner.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from puppetnet.models import EntityType

from .chunking import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    TextChunk,
    TokenCounter,
    iter_chunks,
)
from .labels import (
    OSINT_LABELS,
    canonical_label,
    entity_type_for_label,
    graph_label,
    properties_for_label,
)

__all__ = [
    "DEFAULT_GLINER_MODEL",
    "EntitySpan",
    "ExtractionResult",
    "GLiNEExtractor",
    "GLiNERExtractor",
    "ExtractionError",
]

DEFAULT_GLINER_MODEL = "urchade/gliner_multi-v2.1"
_SUPPORTED_RUNTIMES = frozenset({"torch", "onnx"})


class ExtractionError(RuntimeError):
    """Raised when the model cannot be loaded or produces an unusable result."""


@dataclass(frozen=True, slots=True)
class EntitySpan:
    """One entity mention with absolute offsets in the input text."""

    text: str
    label: str
    start_char: int
    end_char: int
    score: float
    chunk_index: int = 0
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def entity_type(self) -> EntityType:
        return entity_type_for_label(self.label)

    @property
    def is_identifier(self) -> bool:
        return bool(self.properties.get("identifier"))

    def to_span_entity(self, *, sentence_start: int = 0):
        """Convert to ``puppetnet.parsing.nlp_engine.SpanEntity``.

        Offsets are re-based to ``sentence_start`` because that is the frame
        ``NLPEngine`` uses for its relation evidence. The conversion is
        mechanical: the caller decides whether a span belongs to that sentence
        (``start_char >= 0 and end_char <= sentence_length``). Moving a span into
        an unrelated sentence would corrupt its evidence.
        """

        from puppetnet.parsing.nlp_engine import SpanEntity

        return SpanEntity(
            text=self.text,
            label=graph_label(self.label),
            start_char=self.start_char - sentence_start,
            end_char=self.end_char - sentence_start,
            start_token=-1,
            end_token=-1,
            confidence=self.score,
            properties=dict(self.properties),
        )


@dataclass
class ExtractionResult:
    """All entity mentions and the chunk plan used to obtain them."""

    entities: list[EntitySpan] = field(default_factory=list)
    chunks: list[TextChunk] = field(default_factory=list)
    text_length: int = 0
    language: str = ""
    model_id: str = DEFAULT_GLINER_MODEL
    runtime: str = "torch"
    labels: tuple[str, ...] = OSINT_LABELS
    threshold: float = 0.5
    elapsed_seconds: float = 0.0
    dropped_predictions: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def entity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity in self.entities:
            counts[entity.label] = counts.get(entity.label, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "runtime": self.runtime,
            "language": self.language,
            "text_length": self.text_length,
            "chunks": len(self.chunks),
            "chunk_tokens": sum(chunk.token_count for chunk in self.chunks),
            "entities": len(self.entities),
            "entity_counts": self.entity_counts,
            "dropped_predictions": self.dropped_predictions,
            "truncated": self.truncated,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "warnings": self.warnings[:10],
        }


class GLiNERExtractor:
    """Multilingual GLiNER extraction with deterministic chunking.

    ``model`` can be injected for offline tests and for applications that manage
    model lifetime themselves. Injected models are never moved or re-loaded.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_GLINER_MODEL,
        *,
        model: Any = None,
        tokenizer: Any = None,
        runtime: str = "torch",
        onnx_model_file: str = "model.onnx",
        threshold: float = 0.5,
        labels: Sequence[str] = OSINT_LABELS,
        max_tokens: int = DEFAULT_CONTEXT_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
        max_chars: int = 400_000,
        max_sentences: int = 1_500,
        max_entities: int = 5_000,
        device: str = "cpu",
        cache_dir: str | None = None,
        eager_load: bool = True,
    ) -> None:
        runtime = str(runtime or "torch").strip().lower()
        if runtime not in _SUPPORTED_RUNTIMES:
            raise ValueError(f"runtime must be one of {sorted(_SUPPORTED_RUNTIMES)}, got {runtime!r}")
        if not 0.0 < float(threshold) <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if max_tokens < 8:
            raise ValueError("max_tokens must be at least 8")
        if not 0 <= overlap_tokens < max_tokens:
            raise ValueError("overlap_tokens must be within [0, max_tokens)")
        if max_chars < 1 or max_sentences < 1 or max_entities < 1:
            raise ValueError("max_chars, max_sentences and max_entities must be positive")

        canonical_labels: list[str] = []
        for label in labels:
            resolved = canonical_label(label)
            if resolved is None:
                raise ValueError(f"unknown OSINT label {label!r}; expected one of {OSINT_LABELS}")
            if resolved not in canonical_labels:
                canonical_labels.append(resolved)
        if not canonical_labels:
            raise ValueError("at least one OSINT label is required")

        self.model_id = str(model_id).strip() or DEFAULT_GLINER_MODEL
        self.model = model
        self._tokenizer = _as_tokenizer(tokenizer) if tokenizer is not None else None
        self.runtime = runtime
        self.onnx_model_file = onnx_model_file
        self.threshold = float(threshold)
        self.labels: tuple[str, ...] = tuple(canonical_labels)
        self.max_tokens = int(max_tokens)
        self.overlap_tokens = int(overlap_tokens)
        self.max_chars = int(max_chars)
        self.max_sentences = int(max_sentences)
        self.max_entities = int(max_entities)
        self.device = device
        self.cache_dir = cache_dir
        self.backend = "gliner:unloaded"
        self.load_errors: list[str] = []
        self._loaded = False
        if eager_load:
            self.load()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def load(self) -> GLiNERExtractor:
        """Load the requested runtime once. Fails closed (no fallback)."""

        if self._loaded:
            return self
        if self.model is None:
            try:
                from gliner import GLiNER
            except ImportError as exc:  # pragma: no cover - depends on install profile
                raise ExtractionError(
                    "GLiNER is not installed; install packages/pipeline[gliner] or [onnx]"
                ) from exc
            try:
                self.model = self._load_model(GLiNER)
            except Exception as exc:  # noqa: BLE001 - surface a single, actionable error
                self.load_errors.append(f"{type(exc).__name__}: {exc}")
                raise ExtractionError(
                    f"GLiNER model load failed for {self.model_id!r} (runtime={self.runtime}); "
                    "install the matching extra and make the model available"
                ) from exc
        if self._tokenizer is None:
            self._tokenizer = _discover_tokenizer(self.model)
        self.counter = TokenCounter(self._tokenizer)
        self.backend = f"gliner:{self.model_id}:{self.runtime}" if self.runtime != "onnx" else f"gliner:{self.model_id}:onnxruntime"
        self._loaded = True
        return self

    def _load_model(self, gliner_class: Any) -> Any:
        if self.runtime == "onnx":
            # GLiNER exposes both the modern `runtime=` switch and the legacy
            # `load_onnx_model=` flag. Try the modern API first; older pins get
            # the legacy call without changing the caller-facing contract.
            try:
                model = gliner_class.from_pretrained(
                    self.model_id,
                    runtime="onnxruntime",
                    runtime_model_file=self.onnx_model_file,
                    cache_dir=self.cache_dir,
                )
            except TypeError:
                model = gliner_class.from_pretrained(
                    self.model_id,
                    load_onnx_model=True,
                    onnx_model_file=self.onnx_model_file,
                    cache_dir=self.cache_dir,
                )
            return model
        model = gliner_class.from_pretrained(
            self.model_id,
            map_location=self.device,
            cache_dir=self.cache_dir,
        )
        if hasattr(model, "to"):
            model.to(self.device)
        if hasattr(model, "eval"):
            model.eval()
        return model

    def close(self) -> None:
        self.model = None
        self._loaded = False
        self.backend = "gliner:closed"

    def __enter__(self) -> GLiNERExtractor:
        return self.load()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def available(self) -> bool:
        return self._loaded and self.model is not None

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def extract(self, text: str, *, language: str | None = None) -> ExtractionResult:
        """Extract OSINT entities from ``text`` with sentence overlap."""

        if not self._loaded:
            self.load()
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        started = time.perf_counter()
        result = ExtractionResult(
            text_length=len(text),
            language=str(language or ""),
            model_id=self.model_id,
            runtime=self.runtime,
            labels=self.labels,
            threshold=self.threshold,
        )
        if not text.strip():
            result.warnings.append("empty-text")
            result.elapsed_seconds = time.perf_counter() - started
            return result

        if len(text) > self.max_chars:
            text = text[: self.max_chars]
            result.truncated = True
            result.text_length = len(text)
            result.warnings.append(f"truncated-to-{self.max_chars}-chars")

        chunks = list(
            iter_chunks(
                text,
                tokenizer=self.counter,
                max_tokens=self.max_tokens,
                overlap_tokens=self.overlap_tokens,
                max_sentences=self.max_sentences,
            )
        )
        result.chunks = chunks
        if not chunks:
            result.warnings.append("no-sentences")
            result.elapsed_seconds = time.perf_counter() - started
            return result
        if chunks[-1].end_char < len(text):
            result.warnings.append("sentence-cap-reached")
        split = sum(1 for chunk in chunks if chunk.split_sentence)
        if split:
            result.warnings.append(f"oversized-sentence-split:{split}")

        spans: list[EntitySpan] = []
        for chunk_index, chunk in enumerate(chunks):
            for hit in self._predict(chunk):
                span = self._normalise_hit(hit, chunk, chunk_index, result)
                if span is not None:
                    spans.append(span)
                    if len(spans) >= self.max_entities:
                        result.warnings.append("entity-cap-reached")
                        break
            if len(spans) >= self.max_entities:
                break

        result.entities = self._dedupe(spans)
        result.elapsed_seconds = time.perf_counter() - started
        return result

    def extract_batch(self, texts: Iterable[str], *, language: str | None = None) -> list[ExtractionResult]:
        return [self.extract(text, language=language) for text in texts]

    def _predict(self, chunk: TextChunk) -> list[Any]:
        if self.model is None:  # pragma: no cover - guarded by load()
            raise ExtractionError("model is not loaded")
        try:
            predictions = self.model.predict_entities(
                chunk.text,
                list(self.labels),
                threshold=self.threshold,
            )
        except TypeError:
            # Very old GLiNER releases took threshold positionally.
            predictions = self.model.predict_entities(chunk.text, list(self.labels), self.threshold)
        return list(predictions or [])

    def _normalise_hit(
        self,
        hit: Any,
        chunk: TextChunk,
        chunk_index: int,
        result: ExtractionResult,
    ) -> EntitySpan | None:
        if not isinstance(hit, dict):
            result.dropped_predictions += 1
            return None
        try:
            start, end = int(hit["start"]), int(hit["end"])
            score = float(hit["score"])
            label = canonical_label(hit["label"])
        except (KeyError, TypeError, ValueError):
            result.dropped_predictions += 1
            return None
        if label is None:
            result.dropped_predictions += 1
            return None
        if not math.isfinite(score) or not self.threshold <= score <= 1.0:
            result.dropped_predictions += 1
            return None
        if not (0 <= start < end <= len(chunk.text)):
            result.dropped_predictions += 1
            return None

        surface = chunk.text[start:end]
        leading = len(surface) - len(surface.lstrip())
        trailing = len(surface) - len(surface.rstrip())
        start += leading
        end -= trailing
        surface = chunk.text[start:end]
        if (
            len(surface) < 2
            or len(surface) > 200
            or "\n" in surface
            or not any(char.isalnum() for char in surface)
        ):
            result.dropped_predictions += 1
            return None

        properties = properties_for_label(label)
        properties["chunk_index"] = chunk_index
        return EntitySpan(
            text=surface,
            label=label,
            start_char=chunk.start_char + start,
            end_char=chunk.start_char + end,
            score=score,
            chunk_index=chunk_index,
            properties=properties,
        )

    @staticmethod
    def _dedupe(spans: Sequence[EntitySpan]) -> list[EntitySpan]:
        """Remove exact and high-overlap duplicates from overlapping windows.

        Overlap re-runs the model on the same sentence, so the same span can be
        returned twice with slightly different scores. Only same-label, >=80%
        overlap is folded; two genuinely different entities in the same tokens
        (e.g. a person inside an organisation name) remain separate.
        """

        exact: dict[tuple[int, int, str], EntitySpan] = {}
        for span in spans:
            key = (span.start_char, span.end_char, span.label)
            current = exact.get(key)
            if current is None or span.score > current.score:
                exact[key] = span

        kept: list[EntitySpan] = []
        buckets: dict[tuple[str, int], list[EntitySpan]] = {}
        for span in sorted(exact.values(), key=lambda item: (-item.score, item.start_char)):
            bucket_start = span.start_char // 128
            bucket_end = span.end_char // 128
            duplicate = False
            for bucket in range(bucket_start - 1, bucket_end + 2):
                for other in buckets.get((span.label, bucket), ()):
                    overlap = min(span.end_char, other.end_char) - max(span.start_char, other.start_char)
                    if overlap <= 0:
                        continue
                    smaller = min(span.end_char - span.start_char, other.end_char - other.start_char)
                    if smaller > 0 and overlap / smaller >= 0.8:
                        duplicate = True
                        break
                if duplicate:
                    break
            if duplicate:
                continue
            kept.append(span)
            buckets.setdefault((span.label, bucket_start), []).append(span)
        return sorted(kept, key=lambda item: (item.start_char, item.end_char, item.label))

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model_id,
            "runtime": self.runtime,
            "device": self.device,
            "threshold": self.threshold,
            "labels": list(self.labels),
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "tokenizer": type(self._tokenizer).__name__ if self._tokenizer is not None else "approximate",
            "loaded": self._loaded,
            "load_errors": list(self.load_errors),
        }


# Compatibility alias: keep the shorter spelling accepted by earlier drafts.
GLiNEExtractor = GLiNERExtractor


class _CallableTokenizer:
    """Adapt an ``encode``-only tokenizer to the transformers call shape."""

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def __call__(self, text: str, **kwargs: Any) -> dict[str, Any]:
        if "add_special_tokens" in kwargs:
            return {"input_ids": self._tokenizer.encode(text, add_special_tokens=kwargs["add_special_tokens"])}
        return {"input_ids": self._tokenizer.encode(text)}


def _as_tokenizer(tokenizer: Any) -> Any:
    if callable(tokenizer):
        return tokenizer
    if hasattr(tokenizer, "encode"):
        return _CallableTokenizer(tokenizer)
    return None


def _discover_tokenizer(model: Any) -> Any:
    """Find GLiNER's Hugging Face tokenizer without relying on one internals path."""

    for candidate in (
        getattr(model, "tokenizer", None),
        getattr(getattr(model, "data_processor", None), "transformer_tokenizer", None),
        getattr(getattr(model, "data_processor", None), "tokenizer", None),
    ):
        adapted = _as_tokenizer(candidate)
        if adapted is not None:
            return adapted
    return None
