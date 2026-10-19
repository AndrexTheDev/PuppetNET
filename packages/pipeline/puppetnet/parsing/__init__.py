"""Parsing layer: text extraction, CRAFT detection, relation typing, spaCy NLP."""

from __future__ import annotations

from .craft import CraftDetector, CraftMatch
from .nlp_engine import NLPEngine, ParseResult, RawTriple, SentenceView, SpanEntity
from .relations import RelationDecision, RelationMapper, RelationRule
from .text_extract import ExtractedText, clean_text, extract_text, html_to_text, pdf_to_text

__all__ = [
    "NLPEngine",
    "ParseResult",
    "SpanEntity",
    "RawTriple",
    "SentenceView",
    "CraftDetector",
    "CraftMatch",
    "RelationMapper",
    "RelationRule",
    "RelationDecision",
    "ExtractedText",
    "extract_text",
    "html_to_text",
    "pdf_to_text",
    "clean_text",
]
