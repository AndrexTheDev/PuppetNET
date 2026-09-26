"""Core domain model for PuppetNET's harvesting / NLP pipeline.

Every object crossing a module boundary is defined here so the fetchers, the
NLP engine and the Neo4j writer never disagree about field names, label
vocabularies or how confidence is composed.

Confidence model
----------------
The confidence stored on a relationship is the product of three independent
factors, clamped to ``[0, 1]``:

``confidence = source_weight * method_factor * evidence_score``

* ``source_weight``   — provenance trust. Structured registers/databases
                        (ICIJ Offshore Leaks, OpenCorporates, Wikidata,
                        official company registers) = ``1.0``; unstructured
                        news / blogs / RSS = ``0.4``.
* ``method_factor``   — extraction reliability. Grammatical dependency
                        parsing (subject-verb-object) = ``1.0``; sentence-level
                        co-occurrence fallback = ``0.8`` (a ``0.2`` penalty);
                        deterministic structured mapping = ``1.0``.
* ``evidence_score``  — syntactic/lexical quality of the individual triple in
                        ``[0, 1]`` (verb directness, argument distance,
                        label plausibility). Defaults to ``1.0`` for structured
                        sources.

Repeated observations reinforce each other with a noisy-OR at write time
(see :mod:`puppetnet.graph.writer`), so a rumour mentioned by one blog stays
weak while the same claim surfaced by three independent feeds climbs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "STRUCTURED_SOURCE_WEIGHT",
    "UNSTRUCTURED_SOURCE_WEIGHT",
    "COOCCURRENCE_PENALTY",
    "COOCCURRENCE_FACTOR",
    "MIN_EDGE_CONFIDENCE",
    "SourceType",
    "EntityType",
    "ExtractionMethod",
    "RelationType",
    "SourceSpec",
    "Document",
    "EntityMention",
    "Entity",
    "Relation",
    "IngestStats",
    "canonical_key",
    "document_id",
    "content_hash",
    "compose_confidence",
    "noisy_or",
    "utcnow",
    "is_safe_relationship_type",
    "has_organizational_marker",
    "iso",
]

# --------------------------------------------------------------------------- #
# Weighting constants (single source of truth for the whole project)
# --------------------------------------------------------------------------- #

#: Structured databases: ICIJ, OpenCorporates, Wikidata, official registers.
STRUCTURED_SOURCE_WEIGHT: float = 1.0
#: Unstructured news, blogs and RSS feeds.
UNSTRUCTURED_SOURCE_WEIGHT: float = 0.4
#: Penalty applied when dependency parsing fails and we fall back to
#: sentence-level co-occurrence.
COOCCURRENCE_PENALTY: float = 0.2
#: Multiplicative form of :data:`COOCCURRENCE_PENALTY`.
COOCCURRENCE_FACTOR: float = 1.0 - COOCCURRENCE_PENALTY
#: Edges below this confidence are dropped before they reach Neo4j.
MIN_EDGE_CONFIDENCE: float = 0.05


class SourceType(str, Enum):
    """Provenance class of a source; determines its confidence weight."""

    STRUCTURED = "structured"
    UNSTRUCTURED = "unstructured"

    @property
    def confidence(self) -> float:
        return STRUCTURED_SOURCE_WEIGHT if self is SourceType.STRUCTURED else UNSTRUCTURED_SOURCE_WEIGHT


class EntityType(str, Enum):
    """Neo4j secondary label applied to ``:Entity`` nodes."""

    PERSON = "Person"
    ORGANIZATION = "Organization"
    LOCATION = "Location"
    CRAFT = "Craft"
    UNKNOWN = "Unknown"

    @classmethod
    def coerce(cls, value: EntityType | str) -> EntityType:
        if isinstance(value, cls):
            return value
        text = getattr(value, "value", value)
        try:
            return cls(str(text))
        except ValueError:
            return cls.UNKNOWN

    @classmethod
    def from_spacy(cls, label: str) -> EntityType:
        """Map a spaCy entity label onto the PuppetNET label vocabulary.

        ``GPE``/``LOC``/``FAC`` collapse into ``Location``; the ``CRAFT`` label
        is produced by :mod:`puppetnet.parsing.craft`, which extends the stock
        model (spaCy has no vehicle/aircraft class of its own).
        """
        upper = str(label).upper()
        mapping = {
            "PERSON": cls.PERSON,
            "PER": cls.PERSON,
            "ORG": cls.ORGANIZATION,
            "ORGANIZATION": cls.ORGANIZATION,
            "LOC": cls.LOCATION,
            "LOCATION": cls.LOCATION,
            "GPE": cls.LOCATION,
            "FAC": cls.LOCATION,
            "CRAFT": cls.CRAFT,
            "VEHICLE": cls.CRAFT,
            "AIRCRAFT": cls.CRAFT,
            "VESSEL": cls.CRAFT,
            "PRODUCT": cls.CRAFT,
            "LAW": cls.UNKNOWN,
            "NORP": cls.UNKNOWN,
            "WORK_OF_ART": cls.UNKNOWN,
        }
        return mapping.get(upper, cls.UNKNOWN)


class ExtractionMethod(str, Enum):
    """How a triple was derived — drives the method factor."""

    DEPENDENCY = "dependency"        # subject-verb-object from the parse tree
    COOCCURRENCE = "cooccurrence"    # sentence-level fallback (0.2 penalty)
    STRUCTURED = "structured"        # direct field mapping from an API/dataset
    PATTERN = "pattern"              # EntityRuler / apposition / possessive rules
    GAZETTEER = "gazetteer"          # craft identifier regex + gazetteer match

    @property
    def factor(self) -> float:
        if self is ExtractionMethod.COOCCURRENCE:
            return COOCCURRENCE_FACTOR
        return 1.0


class RelationType(str, Enum):
    """Bounded relationship vocabulary.

    The set is intentionally closed: relationship types are interpolated into
    Cypher, so an allowlist is what keeps a dynamic-type writer safe and stops
    schema explosion in Neo4j.
    """

    # Ownership & control
    OWNS = "OWNS"
    OWNED_BY = "OWNED_BY"
    CONTROLS = "CONTROLS"
    SUBSIDIARY_OF = "SUBSIDIARY_OF"
    PARENT_OF = "PARENT_OF"
    ACQUIRED = "ACQUIRED"
    SHAREHOLDER_OF = "SHAREHOLDER_OF"
    INTERMEDIARY_FOR = "INTERMEDIARY_FOR"

    # Governance & employment
    DIRECTOR_OF = "DIRECTOR_OF"
    OFFICER_OF = "OFFICER_OF"
    EMPLOYED_BY = "EMPLOYED_BY"
    EMPLOYS = "EMPLOYS"
    MEMBER_OF = "MEMBER_OF"
    FOUNDED = "FOUNDED"
    APPOINTED_BY = "APPOINTED_BY"

    # Money flows
    FUNDED = "FUNDED"
    FUNDED_BY = "FUNDED_BY"
    INVESTED_IN = "INVESTED_IN"
    PAID_TO = "PAID_TO"
    CONTRACTED_WITH = "CONTRACTED_WITH"
    TRANSFERRED_TO = "TRANSFERRED_TO"

    # Jurisdiction & geography
    LOCATED_IN = "LOCATED_IN"
    REGISTERED_IN = "REGISTERED_IN"
    NATIONAL_OF = "NATIONAL_OF"
    OPERATES_IN = "OPERATES_IN"

    # Movement (the CRAFT-aware part of the graph)
    TRAVELED_WITH = "TRAVELED_WITH"
    TRAVELED_TO = "TRAVELED_TO"
    OPERATES = "OPERATES"
    REGISTERED_TO = "REGISTERED_TO"
    ARRIVED_FROM = "ARRIVED_FROM"

    # Social / adversarial
    MET_WITH = "MET_WITH"
    FAMILY_OF = "FAMILY_OF"
    AFFILIATED_WITH = "AFFILIATED_WITH"
    SANCTIONED_BY = "SANCTIONED_BY"
    INVESTIGATED_BY = "INVESTIGATED_BY"
    ACCUSED_OF = "ACCUSED_OF"
    LINKED_OFFSHORE = "LINKED_OFFSHORE"

    # Fallbacks
    ASSOCIATED_WITH = "ASSOCIATED_WITH"

    @classmethod
    def coerce(cls, value: RelationType | str) -> RelationType:
        """Tolerant lookup.

        ``RelationType`` subclasses ``str``, so ``str(member)`` yields
        ``"RelationType.OWNS"`` rather than ``"OWNS"`` — always read ``.value``
        first or every predicate silently collapses to ASSOCIATED_WITH.
        """
        if isinstance(value, cls):
            return value
        text = getattr(value, "value", None)
        if text is None:
            text = value
        try:
            return cls(str(text).strip().upper())
        except ValueError:
            return cls.ASSOCIATED_WITH

    @classmethod
    def names(cls) -> Sequence[str]:
        return tuple(member.value for member in cls)


_VALID_REL_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def is_safe_relationship_type(name: str) -> bool:
    """Guard used before interpolating a relationship type into Cypher."""
    return bool(_VALID_REL_NAME.match(str(name))) and name in RelationType.names()


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #

_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor", "sir", "dame", "lord",
    "lady", "hon", "reverend", "rev", "senator", "sen", "representative", "rep",
    "governor", "gov", "minister", "president", "pres", "captain", "capt",
    "general", "gen", "colonel", "col", "major", "maj", "lieutenant", "lt",
    "sheikh", "sheik", "shaikh", "haji", "hajj", "ayatollah", "pope", "king",
    "queen", "prince", "princess", "duke", "duchess", "baron", "baroness",
    "count", "countess", "czar", "tsar",
}

_ORG_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "llc", "llp", "lp", "plc", "gmbh", "ag", "sa", "sarl", "srl",
    "sas", "sca", "nv", "bv", "oy", "ab", "as", "asa", "a s", "hf", "ehf",
    "pty", "pvt", "private", "public", "holdings", "holding", "group",
    "enterprises", "international", "foundation", "trust", "trustee",
    "stiftung", "anstalt", "est", "sro", "sp z o o", "spol",
    "kk", "kkk", "ooo", "oao", "pjsc", "jsc", "ojsc", "jscs", "cjsc", "pao",
    "doo", "d o o", "ao", "to", "bvba", "nv sa", "oyj", "tbk",
    "bhd", "sdn", "pte", "l l c", "ltda", "sa de cv", "cv", "aps", "ks",
}

_PARTICLE_TOKENS = {"de", "del", "de la", "van", "von", "der", "den", "di", "da", "du", "al", "bin", "ibn", "el", "la", "le", "los", "las"}

_ACCENT_RE = re.compile(r"[^A-Za-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str, entity_type: EntityType | str = EntityType.UNKNOWN) -> str:
    """Produce a deterministic, accent-folded comparison form of a name.

    The result is used for deduplication and canonical keying only — surface
    forms are preserved separately on the node.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ").replace("@", " at ")
    text = _WHITESPACE_RE.sub(" ", text).strip().strip("\"'“”‘’«»`")

    tokens = [t for t in re.split(r"[\s,;.\-_/\\|]+", text) if t]
    etype = EntityType(entity_type) if isinstance(entity_type, str) else entity_type

    if etype is EntityType.ORGANIZATION:
        while len(tokens) > 1 and tokens[-1].lower().strip("().,") in _ORG_SUFFIXES:
            tokens.pop()
        while len(tokens) > 1 and tokens[-1].lower().strip("()") in {"(", ")"}:
            tokens.pop()
    elif etype is EntityType.PERSON:
        while tokens and tokens[0].lower().strip(".,") in _HONORIFICS:
            tokens.pop(0)
        while len(tokens) > 2 and tokens[-1].lower().strip(".,") in {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"}:
            tokens.pop()

    folded = " ".join(tokens).lower()
    folded = _ACCENT_RE.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", folded).strip()


#: Institutional words that are not legal-form suffixes but still mark an
#: organisation rather than a person.
_ORG_MARKERS = frozenset(
    {
        "bank", "ministry", "department", "agency", "commission", "committee",
        "university", "college", "institute", "charity", "party", "airlines",
        "airways", "shipping", "media", "newspaper", "channel", "club",
        "federation", "union", "association", "consortium", "fund", "authority",
        "bureau", "services", "military", "army", "navy", "police", "court",
        "parliament", "congress", "senate", "government", "municipality",
        "council", "directorate", "enterprise", "enterprises", "manufacturer",
        "refinery", "pipeline", "terminal", "holdings", "holding", "group",
    }
)

#: Two-letter sports-club prefixes ("FC Zenit", "SC Bastia").
_CLUB_PREFIXES = frozenset({"fc", "sc", "cf", "ac", "cd", "if", "bk", "sk", "rc"})


def has_organizational_marker(name: str) -> bool:
    """True when a surface form carries a corporate or institutional marker.

    One shared answer keeps the Wikidata person heuristic, name normalisation
    and the lexical NER agreeing about what "PJSC" or "GmbH" means — a second,
    shorter marker list is how "Gazprom PJSC" ends up typed as a PERSON.
    """
    if not name:
        return False
    tokens = [token for token in normalize_name(name, EntityType.UNKNOWN).split(" ") if token]
    if not tokens:
        return False
    if any(token in _ORG_SUFFIXES or token in _ORG_MARKERS for token in tokens):
        return True
    return len(tokens) > 1 and tokens[0] in _CLUB_PREFIXES


def canonical_key(name: str, entity_type: EntityType | str) -> str:
    """Stable cross-run identifier for an entity node.

    Format ``TYPE:slug-hash`` where the slug keeps the key human-readable in
    the browser and the 8-hex suffix (over the accent-folded full name) keeps
    keys unique when two entities share a truncated slug.
    """
    etype = EntityType.coerce(entity_type)
    folded = normalize_name(name, etype)
    digest = hashlib.sha1(f"{etype.value}|{folded}".encode()).hexdigest()[:8]
    slug = _ACCENT_RE.sub("-", folded).strip("-")[:48].strip("-") or "unknown"
    return f"{etype.value.upper()}:{slug}-{digest}"


def content_hash(text: str) -> str:
    """SHA-256 of whitespace-normalised text — the cross-run dedupe key."""
    collapsed = _WHITESPACE_RE.sub(" ", str(text or "")).strip().lower()
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def document_id(source_id: str, url: str, external_id: str | None = None) -> str:
    """Deterministic document key (stable across runs → idempotent writes)."""
    basis = external_id or url or ""
    digest = hashlib.sha256(f"{source_id}|{basis}".encode()).hexdigest()[:20]
    return f"{source_id}:{digest}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def compose_confidence(
    source_weight: float,
    method: ExtractionMethod | str = ExtractionMethod.DEPENDENCY,
    evidence_score: float = 1.0,
) -> float:
    """Combine the three confidence factors and clamp to ``[0, 1]``."""
    factor = method.factor if isinstance(method, ExtractionMethod) else ExtractionMethod(str(method)).factor
    value = float(source_weight) * float(factor) * float(evidence_score)
    return max(0.0, min(1.0, round(value, 6)))


def noisy_or(existing: float, incoming: float) -> float:
    """Probabilistic merge for repeated observations of the same edge.

    ``P(A or B) = 1 - (1 - A)(1 - B)`` — independent weak corroborations
    accumulate without ever reaching certainty, and a single ``1.0`` structured
    observation pins the edge at ``1.0``.
    """
    a = max(0.0, min(1.0, float(existing or 0.0)))
    b = max(0.0, min(1.0, float(incoming or 0.0)))
    return round(1.0 - (1.0 - a) * (1.0 - b), 6)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    """Static description of a harvestable source."""

    id: str
    name: str
    kind: SourceType
    adapter: str
    base_url: str = ""
    description: str = ""
    #: Per-host politeness for the relay's token bucket.
    rate_per_sec: float = 0.5
    burst: int = 3
    respect_robots: bool = True
    cache_ttl_seconds: int = 0
    timeout_ms: int = 25_000
    max_documents: int = 200
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        return self.kind.confidence

    def with_options(self, **overrides: Any) -> SourceSpec:
        data = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "adapter": self.adapter,
            "base_url": self.base_url,
            "description": self.description,
            "rate_per_sec": self.rate_per_sec,
            "burst": self.burst,
            "respect_robots": self.respect_robots,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "timeout_ms": self.timeout_ms,
            "max_documents": self.max_documents,
            "enabled": self.enabled,
            "options": dict(self.options),
        }
        for key, value in overrides.items():
            if key == "options" and isinstance(value, Mapping):
                data["options"].update(value)
            elif key in data:
                data[key] = value
            else:
                data["options"][key] = value
        return SourceSpec(**data)


@dataclass
class Document:
    """A harvested artefact: a news story, a PDF report, an API record set."""

    doc_id: str
    source_id: str
    url: str
    title: str = ""
    text: str = ""
    content_type: str = "text/plain"
    language: str = "en"
    published_at: datetime | None = None
    fetched_at: datetime = field(default_factory=utcnow)
    author: str = ""
    external_id: str | None = None
    content_hash: str = ""
    source_weight: float = UNSTRUCTURED_SOURCE_WEIGHT
    #: Structured sources may attach already-resolved triples instead of text.
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_hash:
            basis = self.text or self.title or self.url
            self.content_hash = content_hash(basis)
        if not self.doc_id:
            self.doc_id = document_id(self.source_id, self.url, self.external_id)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_node_properties(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_id": self.source_id,
            "url": self.url,
            "title": (self.title or "")[:1024],
            "author": (self.author or "")[:512],
            "content_type": self.content_type,
            "language": self.language,
            "content_hash": self.content_hash,
            "word_count": self.word_count,
            "source_weight": float(self.source_weight),
            "published_at": iso(self.published_at),
            "fetched_at": iso(self.fetched_at),
            "external_id": self.external_id or None,
        }


@dataclass
class EntityMention:
    """A single surface occurrence of an entity inside a document."""

    text: str
    entity_type: EntityType
    start_char: int = -1
    end_char: int = -1
    sentence_index: int = -1
    confidence: float = 1.0
    detector: str = "spacy"
    context: str = ""


@dataclass
class Entity:
    """A resolved graph node candidate."""

    name: str
    entity_type: EntityType
    canonical_key: str = ""
    aliases: set[str] = field(default_factory=set)
    mentions: list[EntityMention] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    #: Provenance: source ids that asserted this entity.
    source_ids: set[str] = field(default_factory=set)
    doc_ids: set[str] = field(default_factory=set)
    #: Best extraction confidence seen for this entity.
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            self.entity_type = EntityType.coerce(self.entity_type)
        if not self.canonical_key:
            self.canonical_key = canonical_key(self.name, self.entity_type)
        if self.name:
            self.aliases.add(self.name)

    @property
    def mention_count(self) -> int:
        return len(self.mentions)

    def merge(self, other: Entity) -> None:
        """Fold a duplicate entity into this one (used by the resolver)."""
        self.aliases.update(other.aliases)
        self.mentions.extend(other.mentions)
        self.source_ids.update(other.source_ids)
        self.doc_ids.update(other.doc_ids)
        self.confidence = max(self.confidence, other.confidence)
        for key, value in other.properties.items():
            if key not in self.properties or not self.properties[key]:
                self.properties[key] = value
        if len(other.name) > len(self.name):
            # Prefer the longest surface form as the display name.
            self.aliases.add(self.name)
            self.name = other.name

    def to_node_properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "canonical_key": self.canonical_key,
            "name": self.name,
            "entity_type": self.entity_type.value,
            "aliases": sorted({a for a in self.aliases if a})[:64],
            "mention_count": self.mention_count,
            "confidence": round(float(self.confidence), 6),
            "source_ids": sorted(self.source_ids)[:32],
            "doc_ids": sorted(self.doc_ids)[:64],
            "first_seen": iso(utcnow()),
            "last_seen": iso(utcnow()),
        }
        for key, value in self.properties.items():
            props.setdefault(key, value)
        return props


@dataclass
class Relation:
    """A weighted, evidence-backed directed edge between two entities."""

    subject: Entity
    predicate: RelationType
    obj: Entity
    confidence: float
    method: ExtractionMethod = ExtractionMethod.DEPENDENCY
    source_id: str = ""
    doc_id: str = ""
    source_weight: float = UNSTRUCTURED_SOURCE_WEIGHT
    evidence: str = ""
    verb: str = ""
    negated: bool = False
    sentence_index: int = -1
    subject_span: tuple[int, int] | None = None
    obj_span: tuple[int, int] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, RelationType):
            self.predicate = RelationType.coerce(self.predicate)
        if not isinstance(self.method, ExtractionMethod):
            value = getattr(self.method, "value", self.method)
            self.method = ExtractionMethod(str(value))

    @property
    def rel_type(self) -> str:
        return self.predicate.value

    def signature(self) -> str:
        """Identity of the edge for de-duplication inside a single run."""
        return f"{self.subject.canonical_key}|{self.rel_type}|{self.obj.canonical_key}"

    def to_edge_properties(self, run_id: str = "") -> dict[str, Any]:
        return {
            "confidence": round(float(self.confidence), 6),
            "source_weight": round(float(self.source_weight), 6),
            "method": self.method.value,
            "source_id": self.source_id,
            "doc_id": self.doc_id,
            "evidence": (self.evidence or "")[:1000],
            "verb": (self.verb or "")[:64],
            "negated": bool(self.negated),
            "observations": 1,
            "run_id": run_id,
            "first_seen": iso(utcnow()),
            "last_seen": iso(utcnow()),
        }


@dataclass
class IngestStats:
    """Run counters — serialised into Neo4j and the GitHub Actions summary."""

    run_id: str = ""
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    documents_fetched: int = 0
    documents_skipped_duplicate: int = 0
    documents_failed: int = 0
    characters_processed: int = 0
    sentences_processed: int = 0
    entities_extracted: int = 0
    entities_written: int = 0
    relations_extracted: int = 0
    relations_written: int = 0
    relations_dropped_low_confidence: int = 0
    dependency_triples: int = 0
    cooccurrence_triples: int = 0
    craft_entities: int = 0
    http_requests: int = 0
    http_via_worker: int = 0
    http_direct_fallback: int = 0
    http_rate_limited: int = 0
    http_deferred_to_queue: int = 0
    http_robots_blocked: int = 0
    http_errors: int = 0
    seconds_throttled: float = 0.0
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump_source(self, source_id: str, key: str, amount: int = 1) -> None:
        bucket = self.per_source.setdefault(source_id, {})
        bucket[key] = int(bucket.get(key, 0)) + amount

    def record_error(self, source_id: str, message: str) -> None:
        entry = f"[{source_id}] {message}"
        if len(self.errors) < 200:
            self.errors.append(entry[:500])

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or utcnow()
        return round((end - self.started_at).total_seconds(), 3)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, datetime):
                data[key] = iso(value)
            else:
                data[key] = value
        data["duration_seconds"] = self.duration_seconds
        return data

    def markdown_summary(self) -> str:
        lines = [
            "### PuppetNET ingest report",
            "",
            f"- **Run**: `{self.run_id}`",
            f"- **Duration**: {self.duration_seconds}s",
            f"- **Documents**: {self.documents_fetched} fetched / "
            f"{self.documents_skipped_duplicate} duplicate / {self.documents_failed} failed",
            f"- **Sentences parsed**: {self.sentences_processed} "
            f"({self.characters_processed:,} characters)",
            f"- **Entities**: {self.entities_extracted} extracted → {self.entities_written} written "
            f"({self.craft_entities} CRAFT)",
            f"- **Relations**: {self.relations_extracted} extracted → {self.relations_written} written "
            f"({self.relations_dropped_low_confidence} dropped below {MIN_EDGE_CONFIDENCE})",
            f"- **Triples**: {self.dependency_triples} dependency / {self.cooccurrence_triples} co-occurrence",
            f"- **HTTP**: {self.http_requests} requests "
            f"({self.http_via_worker} via edge relay, {self.http_direct_fallback} direct fallback, "
            f"{self.http_deferred_to_queue} queued, {self.http_rate_limited} rate-limited, "
            f"{self.http_robots_blocked} robots-blocked, "
            f"{self.http_errors} errors, {self.seconds_throttled:.1f}s throttled)",
        ]
        if self.per_source:
            lines += ["", "| Source | Docs | Entities | Relations | Errors |", "| --- | --- | --- | --- | --- |"]
            for source_id, counters in sorted(self.per_source.items()):
                lines.append(
                    f"| `{source_id}` | {counters.get('documents', 0)} | {counters.get('entities', 0)} | "
                    f"{counters.get('relations', 0)} | {counters.get('errors', 0)} |"
                )
        if self.errors:
            lines += ["", "<details><summary>Errors (first 20)</summary>", ""]
            lines += [f"- `{err}`" for err in self.errors[:20]]
            lines += ["", "</details>"]
        return "\n".join(lines)


def merge_documents(docs: Iterable[Document]) -> list[Document]:
    """Drop documents with identical content hashes, keeping the earliest."""
    seen: dict[str, Document] = {}
    for doc in docs:
        key = doc.content_hash or content_hash(doc.text or doc.url)
        existing = seen.get(key)
        if existing is None:
            seen[key] = doc
        else:
            existing.extra.setdefault("duplicate_urls", []).append(doc.url)
    return list(seen.values())


def partition_by_type(entities: Iterable[Entity]) -> dict[EntityType, list[Entity]]:
    buckets: dict[EntityType, list[Entity]] = {t: [] for t in EntityType}
    for entity in entities:
        buckets[entity.entity_type].append(entity)
    return buckets
