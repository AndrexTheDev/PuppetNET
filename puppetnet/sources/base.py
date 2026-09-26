"""Source adapter framework.

An adapter turns one real-world data source into a stream of
:class:`~puppetnet.models.Document` objects. Two families exist:

* **Structured** (``ICIJ``, ``OpenCorporates``, ``Wikidata``, official
  registers) — the adapter already knows the semantics, so it attaches resolved
  :class:`~puppetnet.models.Entity` / :class:`~puppetnet.models.Relation`
  objects directly to the document. Edge confidence weight = ``1.0``.
* **Unstructured** (RSS/news/blogs) — the adapter only produces text; the NLP
  engine extracts entities and triples later. Edge confidence weight = ``0.4``.

Every adapter inherits the same politeness, dedupe, budget and error-accounting
behaviour from :class:`SourceAdapter` so a new source is ~50 lines of mapping
code and nothing else.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from ..logging_utils import get_logger
from ..models import (
    Document,
    Entity,
    EntityMention,
    EntityType,
    ExtractionMethod,
    IngestStats,
    Relation,
    RelationType,
    SourceSpec,
    SourceType,
    canonical_key,
    compose_confidence,
    content_hash,
    document_id,
    utcnow,
)

__all__ = ["AdapterContext", "SourceAdapter", "AdapterError", "build_entity", "build_relation"]

logger = get_logger("sources")


class AdapterError(RuntimeError):
    """A source could not be harvested (logged, counted, never fatal)."""


@dataclass
class AdapterContext:
    """Everything an adapter needs, injected by the pipeline."""

    settings: Any
    client: Any                       # puppetnet.net.proxy_client.FetchClient
    stats: IngestStats
    spec: SourceSpec | None = None
    run_id: str = ""
    known_hashes: set[str] = field(default_factory=set)
    nlp: Any = None                   # NLPEngine (optional; unstructured only)
    deadline: float = 0.0             # time.monotonic() when the run must stop
    seen_documents: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ #
    @property
    def time_left_seconds(self) -> float:
        if not self.deadline:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def budget_exhausted(self) -> bool:
        return self.deadline > 0 and time.monotonic() >= self.deadline

    def remember_hash(self, digest: str) -> None:
        self.known_hashes.add(digest)
        self.seen_documents.add(digest)


def build_entity(
    name: str,
    entity_type: EntityType | str,
    *,
    source_id: str,
    doc_id: str = "",
    confidence: float = 1.0,
    aliases: Iterable[str] = (),
    properties: dict[str, Any] | None = None,
    detector: str = "structured",
    mention_context: str = "",
) -> Entity:
    """Construct a resolved entity from a structured record."""
    etype = entity_type if isinstance(entity_type, EntityType) else EntityType(entity_type)
    surface = (name or "").strip()
    entity = Entity(
        name=surface,
        entity_type=etype,
        canonical_key=canonical_key(surface, etype),
        aliases={surface, *(a.strip() for a in aliases if a and a.strip())},
        properties=dict(properties or {}),
        source_ids={source_id},
        doc_ids={doc_id} if doc_id else set(),
        confidence=float(confidence),
    )
    entity.mentions.append(
        EntityMention(
            text=surface,
            entity_type=etype,
            confidence=float(confidence),
            detector=detector,
            context=mention_context[:500],
        )
    )
    return entity


def build_relation(
    subject: Entity,
    predicate: RelationType | str,
    obj: Entity,
    *,
    source_id: str,
    doc_id: str = "",
    source_weight: float = 1.0,
    method: ExtractionMethod = ExtractionMethod.STRUCTURED,
    evidence: str = "",
    evidence_score: float = 1.0,
    extra: dict[str, Any] | None = None,
) -> Relation:
    """Construct a weighted edge; confidence = source_weight × method × evidence."""
    confidence = compose_confidence(source_weight, method, evidence_score)
    relation = Relation(
        subject=subject,
        predicate=RelationType.coerce(predicate) if not isinstance(predicate, RelationType) else predicate,
        obj=obj,
        confidence=confidence,
        method=method,
        source_id=source_id,
        doc_id=doc_id,
        source_weight=source_weight,
        evidence=evidence[:1000],
        extra={"evidence_score": evidence_score, "rule": f"structured:{source_id}", **(extra or {})},
    )
    return relation


class SourceAdapter(abc.ABC):
    """Base class for all harvesters."""

    #: Registry key — must match ``SourceSpec.adapter``.
    adapter_name: str = "base"

    def __init__(self, spec: SourceSpec, context: AdapterContext) -> None:
        self.spec = spec
        self.ctx = context
        self.settings = context.settings
        self.client = context.client
        self.stats = context.stats
        self.log = get_logger(f"sources.{spec.id}")
        self.source_weight = float(spec.confidence)
        self.method = ExtractionMethod.STRUCTURED if spec.kind is SourceType.STRUCTURED else ExtractionMethod.DEPENDENCY
        self._documents_emitted = 0
        self._started = 0.0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def run(self, limit: int | None = None) -> Iterator[Document]:
        """Harvest with budget, limit, dedupe and error accounting applied."""
        self._started = time.perf_counter()
        cap = int(limit or self.spec.max_documents or self.settings.max_documents_per_source)
        self.log.info("harvesting %s (%s, weight=%.2f, cap=%d)", self.spec.name, self.spec.kind.value, self.source_weight, cap)
        try:
            for document in self.harvest():
                if document is None:
                    continue
                if self.ctx.budget_exhausted():
                    self.log.warning("global time budget exhausted — stopping %s early", self.spec.id)
                    break
                if self._documents_emitted >= cap:
                    self.log.info("reached the document cap (%d) for %s", cap, self.spec.id)
                    break
                prepared = self._prepare(document)
                if prepared is None:
                    continue
                self._documents_emitted += 1
                self.stats.documents_fetched += 1
                self.stats.bump_source(self.spec.id, "documents")
                self.stats.bump_source(self.spec.id, "entities", len(prepared.entities))
                self.stats.bump_source(self.spec.id, "relations", len(prepared.relations))
                yield prepared
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            self.stats.documents_failed += 1
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"{exc.__class__.__name__}: {exc}")
            self.log.exception("source %s failed: %s", self.spec.id, exc)
            raise AdapterError(str(exc)) from exc
        finally:
            elapsed = time.perf_counter() - self._started
            self.log.info(
                "%s finished: %d documents in %.1fs (entities=%d relations=%d)",
                self.spec.id, self._documents_emitted, elapsed,
                self.stats.per_source.get(self.spec.id, {}).get("entities", 0),
                self.stats.per_source.get(self.spec.id, {}).get("relations", 0),
            )

    @abc.abstractmethod
    def harvest(self) -> Iterator[Document]:
        """Yield raw documents; the framework handles the rest."""

    # ------------------------------------------------------------------ #
    # Hooks & helpers
    # ------------------------------------------------------------------ #
    def _prepare(self, document: Document) -> Document | None:
        """Normalise, dedupe and stamp a document before it is emitted."""
        document.source_id = document.source_id or self.spec.id
        document.source_weight = self.source_weight
        if not document.doc_id:
            document.doc_id = document_id(document.source_id, document.url, document.external_id)
        if not document.content_hash:
            document.content_hash = content_hash(document.text or document.title or document.url)
        if not document.published_at:
            document.published_at = document.fetched_at

        if self.settings.only_new_documents:
            if document.content_hash in self.ctx.known_hashes:
                self.stats.documents_skipped_duplicate += 1
                self.stats.bump_source(self.spec.id, "duplicates")
                self.log.debug("skipping duplicate content %s (%s)", document.doc_id, document.content_hash[:12])
                return None
            if document.doc_id in self.ctx.seen_documents:
                self.stats.documents_skipped_duplicate += 1
                return None

        if not document.text and not document.relations and not document.entities:
            self.log.debug("skipping empty document %s", document.doc_id)
            return None

        self.ctx.remember_hash(document.content_hash)
        self.ctx.seen_documents.add(document.doc_id)
        document.extra.setdefault("source_kind", self.spec.kind.value)
        document.extra.setdefault("source_confidence", self.source_weight)
        return document

    def fetch(self, url: str, **kwargs: Any) -> Any:
        """Fetch through the shared polite client, attributed to this source."""
        kwargs.setdefault("source", self.spec)
        kwargs.setdefault("source_id", self.spec.id)
        kwargs.setdefault("respect_robots", self.spec.respect_robots)
        kwargs.setdefault("cache_ttl_seconds", self.spec.cache_ttl_seconds)
        return self.client.request(url, **kwargs)

    def fetch_json(self, url: str, *, params: dict[str, Any] | None = None, mode: str = "bot", headers: dict[str, str] | None = None) -> Any | None:
        """Fetch + parse JSON, returning ``None`` (and counting) on failure."""
        result = self.fetch(url, params=params, mode=mode, headers=headers or {}, accept="application/json")
        if not result.ok:
            self.stats.documents_failed += 1
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"{result.status} {url}: {result.error}")
            self.log.warning("JSON fetch failed (%s) for %s", result.status or result.error, url)
            return None
        try:
            return result.json()
        except ValueError as exc:
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"invalid JSON from {url}: {exc}")
            self.log.warning("invalid JSON from %s: %s", url, exc)
            return None

    def make_document(
        self,
        url: str,
        *,
        title: str = "",
        text: str = "",
        content_type: str = "text/plain",
        published_at: datetime | None = None,
        author: str = "",
        external_id: str | None = None,
        language: str = "en",
        entities: Sequence[Entity] = (),
        relations: Sequence[Relation] = (),
        extra: dict[str, Any] | None = None,
    ) -> Document:
        """Build a document stamped with this source's identity and weight."""
        doc = Document(
            doc_id=document_id(self.spec.id, url, external_id),
            source_id=self.spec.id,
            url=url or f"{self.spec.id}://{external_id or title or 'record'}",
            title=title,
            text=text,
            content_type=content_type,
            language=language,
            published_at=published_at,
            author=author,
            external_id=external_id,
            source_weight=self.source_weight,
            entities=list(entities),
            relations=list(relations),
            extra={**(extra or {}), "source_kind": self.spec.kind.value},
        )
        # Adapters build their entities and edges before the document id exists,
        # so stamp it back onto them: nodes keep per-document provenance and the
        # MENTIONS writer (which skips rows without a doc_id) gets its rows.
        for stamped in doc.entities:
            stamped.doc_ids.add(doc.doc_id)
        for edge in doc.relations:
            if not edge.doc_id:
                edge.doc_id = doc.doc_id
        return doc

    def entity(
        self,
        name: str,
        entity_type: EntityType | str,
        *,
        confidence: float | None = None,
        doc_id: str = "",
        properties: dict[str, Any] | None = None,
        aliases: Iterable[str] = (),
    ) -> Entity:
        return build_entity(
            name,
            entity_type,
            source_id=self.spec.id,
            doc_id=doc_id,
            confidence=self.source_weight if confidence is None else confidence,
            properties=properties,
            aliases=aliases,
            detector=f"structured:{self.spec.id}",
        )

    def relation(
        self,
        subject: Entity,
        predicate: RelationType | str,
        obj: Entity,
        *,
        doc_id: str = "",
        evidence: str = "",
        evidence_score: float = 1.0,
        extra: dict[str, Any] | None = None,
    ) -> Relation:
        return build_relation(
            subject,
            predicate,
            obj,
            source_id=self.spec.id,
            doc_id=doc_id,
            source_weight=self.source_weight,
            method=ExtractionMethod.STRUCTURED,
            evidence=evidence,
            evidence_score=evidence_score,
            extra=extra,
        )

    # ------------------------------------------------------------------ #
    # Small shared utilities
    # ------------------------------------------------------------------ #
    @staticmethod
    def absolute_url(base: str, maybe_relative: str) -> str:
        if not maybe_relative:
            return base
        if urlparse(maybe_relative).scheme:
            return maybe_relative
        return urljoin(base, maybe_relative)

    @staticmethod
    def parse_datetime(value: Any) -> datetime | None:
        """Tolerant timestamp parsing (ISO 8601, RFC 822, epoch, dateutil)."""
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            number = int(text)
            if number > 10**12:  # milliseconds
                number //= 1000
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            from dateutil import parser as date_parser

            parsed = date_parser.parse(text, fuzzy=True)
        except (ImportError, ValueError, OverflowError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def within_window(moment: datetime | None, days: int) -> bool:
        """True when ``moment`` is inside the last ``days`` days (or unknown)."""
        if moment is None or days <= 0:
            return True
        return moment >= utcnow() - timedelta(days=days)

    def option(self, key: str, default: Any = None) -> Any:
        """Read an adapter option from the spec (YAML/env-overridable)."""
        value = (self.spec.options or {}).get(key, default)
        return default if value is None else value

    def describe(self) -> dict[str, Any]:
        return {
            "source_id": self.spec.id,
            "adapter": self.adapter_name,
            "kind": self.spec.kind.value,
            "confidence": self.source_weight,
            "documents": self._documents_emitted,
            "seconds": round(time.perf_counter() - self._started, 3) if self._started else 0.0,
        }
