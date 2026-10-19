"""Information-extraction pipeline for PuppetNET.

Two complementary stages, both designed for zero-cost GitHub Actions runners:

``nlp.extractor``
    Multilingual entity extraction with GLiNER ``gliner_multi-v2.1`` (PyTorch or
    ONNX Runtime) over sentence-aligned 384-token windows. Labels follow the
    OSINT vocabulary in :mod:`nlp.labels`.

``nlp.triplet_extractor``
    Relation extraction through Cloudflare Workers AI, constrained to the
    graph's closed :class:`~puppetnet.models.RelationType` predicate set, with
    grounding checks and a 10,000-neuron/day free-tier ledger.

``nlp.graph_bridge``
    The single conversion point from model output to ``Entity``/``Relation``
    rows; imported by :class:`puppetnet.pipeline.IngestPipeline` only when
    ``TRIPLET_BACKEND=cloudflare``.

Importing this package never imports ``torch``, ``gliner`` or ``transformers``:
the heavy dependencies are loaded only when an extractor is instantiated.
"""

from __future__ import annotations

from .chunking import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    Sentence,
    TextChunk,
    TokenCounter,
    chunk_text,
    iter_chunks,
    split_sentences,
)
from .extractor import (
    DEFAULT_GLINER_MODEL,
    EntitySpan,
    ExtractionError,
    ExtractionResult,
    GLiNEExtractor,
    GLiNERExtractor,
)
from .graph_bridge import graph_from_triplets, triplet_predicate_counts
from .labels import (
    OSINT_LABELS,
    canonical_label,
    entity_type_for_label,
    graph_label,
    is_graph_label,
    properties_for_label,
)
from .triplet_extractor import (
    CloudflareAIError,
    CloudflareTripletExtractor,
    DailyNeuronBudgetExceeded,
    NeuronBudget,
    Triplet,
    TripletExtractionResult,
)

__all__ = [
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_GLINER_MODEL",
    "DEFAULT_OVERLAP_TOKENS",
    "CloudflareAIError",
    "CloudflareTripletExtractor",
    "DailyNeuronBudgetExceeded",
    "EntitySpan",
    "ExtractionError",
    "ExtractionResult",
    "GLiNEExtractor",
    "GLiNERExtractor",
    "NeuronBudget",
    "OSINT_LABELS",
    "Sentence",
    "TextChunk",
    "TokenCounter",
    "Triplet",
    "TripletExtractionResult",
    "canonical_label",
    "chunk_text",
    "entity_type_for_label",
    "graph_from_triplets",
    "graph_label",
    "is_graph_label",
    "iter_chunks",
    "properties_for_label",
    "split_sentences",
    "triplet_predicate_counts",
]

__version__ = "1.6.0"
