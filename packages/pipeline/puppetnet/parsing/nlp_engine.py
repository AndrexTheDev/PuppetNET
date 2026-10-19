"""spaCy-backed NLP engine: named entities + dependency-parsed SVO triples.

Pipeline
--------
::

    Document.text
      → chunking (bounded memory for transformer models)
      → spaCy pipeline: tok2vec/transformer → tagger → parser → NER (+ CRAFT ruler)
      → per sentence:
           • entity spans (PERSON / ORG / LOC / CRAFT)
           • subject-verb-object triples from the dependency parse
                – active:   nsubj ← VERB → dobj/obj/attr/pobj
                – passive:  nsubjpass + obl:agent  → restored to active order
                – conjunction expansion on both argument slots
           • relation typing via :class:`~puppetnet.parsing.relations.RelationMapper`
           • evidence score (distance, clause depth, passivity, hedging)
      → if a sentence yields no triple but has ≥2 entities
           • sentence-level co-occurrence fallback with a 0.2 weight penalty
      → entity resolution/normalisation → (Entity, Relation) objects

Model resolution
----------------
``SPACY_MODELS`` is an ordered preference list (default ``en_core_web_trf``,
``en_core_web_lg``, ``en_core_web_md``, ``en_core_web_sm``). If none is
installed the engine drops to a spaCy *blank* pipeline (real tokenizer +
EntityRuler gazetteers, no statistical parse) and finally to a pure-Python
pattern backend. Backends without a dependency parser take the co-occurrence
path, which is exactly where the 0.2 penalty applies.
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..logging_utils import get_logger
from ..models import (
    COOCCURRENCE_FACTOR,
    Document,
    Entity,
    EntityMention,
    EntityType,
    ExtractionMethod,
    Relation,
    RelationType,
    canonical_key,
    compose_confidence,
    normalize_name,
)
from .craft import CraftDetector, CraftMatch
from .relations import RelationDecision, RelationMapper

__all__ = [
    "NLPEngine",
    "ParseResult",
    "SpanEntity",
    "RawTriple",
    "SentenceView",
    "NOISE_ENTITIES",
    "KEEP_LABELS",
]

logger = get_logger("parsing.nlp")

# --------------------------------------------------------------------------- #
# Vocabulary / filters
# --------------------------------------------------------------------------- #

#: spaCy labels we promote into the graph (``GPE``/``LOC``/``FAC`` → Location).
KEEP_LABELS: frozenset[str] = frozenset({"PERSON", "ORG", "GPE", "LOC", "FAC", "CRAFT", "PRODUCT"})

#: Labels that never become nodes.
DROP_LABELS: frozenset[str] = frozenset(
    {"DATE", "TIME", "PERCENT", "MONEY", "QUANTITY", "ORDINAL", "CARDINAL", "EVENT", "WORK_OF_ART", "LAW", "LANGUAGE"}
)

#: Entities that produce hub nodes and carry no investigative signal.
NOISE_ENTITIES: frozenset[str] = frozenset(
    {
        "reuters", "associated press", "the associated press", "ap", "afp", "agence france-presse",
        "bloomberg", "bloomberg news", "getty images", "getty", "dpa", "tass", "interfax", "ria novosti",
        "the new york times", "new york times", "the guardian", "the washington post", "washington post",
        "bbc", "bbc news", "cnn", "al jazeera", "the telegraph", "financial times", "wall street journal",
        "the wall street journal", "vice news", "buzzfeed", "buzzfeed news", "the times", "le monde",
        "el pais", "der spiegel", "la repubblica", "twitter", "x", "facebook", "instagram", "linkedin",
        "youtube", "telegram", "whatsapp", "wikipedia", "google", "internet", "web", "email",
        "the company", "the government", "the state", "the president", "the minister", "the official",
        "the officials", "the source", "the sources", "anonymous", "the court", "the judge", "the police",
        "the military", "the army", "the navy", "the ministry", "the committee", "the board", "the bank",
        "the firm", "the group", "the organisation", "the organization", "the agency", "the department",
        "the country", "the city", "the region", "the world", "the united states", "united states",
        "the european union", "european union", "united nations", "the un", "n/a", "unknown", "none",
    }
)

SUBJECT_DEPS: frozenset[str] = frozenset({"nsubj", "csubj", "nsubjpass", "csubjpass", "nsubj:pass", "csubj:pass"})
OBJECT_DEPS: frozenset[str] = frozenset({"dobj", "obj", "attr", "acomp", "oprd", "npadvmod"})
AGENT_DEPS: frozenset[str] = frozenset({"agent", "obl"})
PASSIVE_DEPS: frozenset[str] = frozenset({"nsubjpass", "csubjpass", "nsubj:pass", "csubj:pass"})
#: Prepositions whose object is a plausible graph argument.
ARGUMENT_PREPS: frozenset[str] = frozenset(
    {"of", "with", "for", "in", "to", "from", "by", "at", "on", "into", "onto", "aboard", "over", "under", "between", "among", "against", "via", "through", "toward", "towards", "near", "within", "without"}
)
NEGATION_DEPS: frozenset[str] = frozenset({"neg"})
NEGATION_TOKENS: frozenset[str] = frozenset({"not", "no", "never", "neither", "nor", "n't", "without", "denied", "denies", "refused"})

#: Copular lemmas. They carry no verb semantics, but they *do* root nominal
#: predications ("Sechin is the director of Rosneft"), so they are routed to
#: :meth:`_nominal_predication` instead of being discarded outright.
COPULA_LEMMAS: frozenset[str] = frozenset({"be", "become", "remain", "serve"})

#: Verbs that are pure copula/aux and never carry relation semantics on their own.
SKIP_VERB_LEMMAS: frozenset[str] = frozenset({"be", "been", "being", "am", "is", "are", "was", "were", "do", "does", "did", "have", "has", "had", "will", "would", "shall", "should", "can", "could", "may", "might", "must"})

#: Dependency labels that indicate the verb is inside an embedded clause.
EMBEDDED_VERB_DEPS: frozenset[str] = frozenset({"relcl", "acl", "advcl", "ccomp", "xcomp", "pcomp"})

#: Directional prepositions override a generic travel predicate.
_PREPOSITION_TRAVEL_MAP: dict[str, RelationType] = {
    "to": RelationType.TRAVELED_TO,
    "into": RelationType.TRAVELED_TO,
    "toward": RelationType.TRAVELED_TO,
    "towards": RelationType.TRAVELED_TO,
    "from": RelationType.ARRIVED_FROM,
    "in": RelationType.LOCATED_IN,
    "at": RelationType.LOCATED_IN,
    "near": RelationType.LOCATED_IN,
    "within": RelationType.LOCATED_IN,
}


@dataclass(frozen=True)
class SpanEntity:
    """An entity span, backend-agnostic."""

    text: str
    label: str
    start_char: int
    end_char: int
    start_token: int = -1
    end_token: int = -1
    confidence: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def entity_type(self) -> EntityType:
        if self.label == "CRAFT":
            return EntityType.CRAFT
        return EntityType.from_spacy(self.label)


@dataclass(frozen=True)
class RawTriple:
    """A syntactic triple before relation typing."""

    subject: SpanEntity
    obj: SpanEntity
    verb_lemma: str
    verb_surface: str
    verb_dep: str = "ROOT"
    passive: bool = False
    #: True when the arguments are still in *surface* order for a by-passive
    #: ("X is owned by Y") and must be flipped to reach the canonical direction.
    #: The dependency path restores active order itself, so it leaves this False.
    passive_flip: bool = False
    negated: bool = False
    hedged: bool = False
    distance: int = 0
    clause_text: str = ""
    sentence_index: int = -1
    phrasal: tuple[str, ...] = ()
    preposition: str = ""
    subject_span: tuple[int, int] = (-1, -1)
    obj_span: tuple[int, int] = (-1, -1)


@dataclass
class SentenceView:
    """Everything we know about one sentence."""

    index: int
    text: str
    entities: list[SpanEntity]
    triples: list[RawTriple]
    parse_ok: bool = True
    note: str = ""

    @property
    def start_char(self) -> int:
        return min((e.start_char for e in self.entities), default=-1)


@dataclass
class ParseResult:
    """Output of :meth:`NLPEngine.parse_document`."""

    doc_id: str
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    sentences: int = 0
    characters: int = 0
    dependency_triples: int = 0
    cooccurrence_triples: int = 0
    pattern_triples: int = 0
    craft_mentions: int = 0
    negated_dropped: int = 0
    backend: str = "uninitialised"
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "backend": self.backend,
            "sentences": self.sentences,
            "characters": self.characters,
            "entities": len(self.entities),
            "relations": len(self.relations),
            "dependency_triples": self.dependency_triples,
            "cooccurrence_triples": self.cooccurrence_triples,
            "pattern_triples": self.pattern_triples,
            "craft_mentions": self.craft_mentions,
            "negated_dropped": self.negated_dropped,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "warnings": self.warnings[:10],
        }


#: Place gazetteer used by the model-less backends for EntityRuler patterns and
#: for entity-type guessing.
JURISDICTION_GAZETTEER: tuple[str, ...] = (
    "British Virgin Islands", "Cayman Islands", "Panama", "Seychelles", "Belize", "Bahamas",
    "Bermuda", "Jersey", "Guernsey", "Isle of Man", "Luxembourg", "Switzerland", "Liechtenstein",
    "Cyprus", "Malta", "Gibraltar", "Curacao", "Aruba", "Marshall Islands", "Vanuatu", "Samoa",
    "Singapore", "Hong Kong", "United Arab Emirates", "Saudi Arabia", "Qatar", "Kuwait",
    "Bahrain", "Oman", "Lebanon", "Jordan", "Turkey", "Russia", "Ukraine", "Belarus", "Kazakhstan",
    "Azerbaijan", "Armenia", "Georgia", "Moldova", "Latvia", "Lithuania", "Estonia", "Poland",
    "Czech Republic", "Hungary", "Romania", "Bulgaria", "Serbia", "Croatia", "Slovenia", "Greece",
    "China", "India", "Pakistan", "Iran", "Iraq", "Syria", "Israel", "Egypt", "Libya", "Tunisia",
    "Algeria", "Morocco", "Nigeria", "Kenya", "South Africa", "Brazil", "Argentina", "Chile",
    "Colombia", "Peru", "Venezuela", "Mexico", "United States", "United Kingdom", "Canada",
    "Australia", "New Zealand", "Japan", "South Korea", "Indonesia", "Malaysia", "Thailand",
    "Vietnam", "Philippines", "Netherlands", "Belgium", "France", "Germany", "Italy", "Spain",
    "Portugal", "Austria", "Sweden", "Norway", "Finland", "Denmark", "Ireland", "Iceland",
    "European Union", "United Nations", "Vatican", "San Marino", "Monaco", "Andorra",
    "Moscow", "London", "Washington", "New York", "Paris", "Berlin", "Madrid", "Rome", "Kyiv",
    "Minsk", "Istanbul", "Ankara", "Doha", "Riyadh", "Beijing", "Shanghai", "Tokyo", "Seoul",
    "Delhi", "Mumbai", "Lagos", "Nairobi", "Johannesburg", "Sao Paulo", "Buenos Aires",
    "Geneva", "Zurich", "Vienna", "Prague", "Warsaw", "Budapest", "Bucharest", "Sofia",
    "Belgrade", "Zagreb", "Ljubljana", "Athens", "Lisbon", "Brussels", "Amsterdam",
    "Copenhagen", "Stockholm", "Oslo", "Helsinki", "Dublin", "Reykjavik", "Tallinn", "Riga",
    "Vilnius", "Tbilisi", "Yerevan", "Baku", "Astana", "Tashkent", "Fiji", "Nassau",
    "Road Town", "George Town", "Valletta", "Nicosia", "Tehran", "Baghdad", "Damascus",
    "Beirut", "Amman", "Jerusalem", "Cairo", "Tripoli", "Abuja", "Accra", "Jakarta",
    "Manila", "Bangkok", "Hanoi", "Kuala Lumpur", "Karachi", "Kabul", "Sochi", "Sevastopol",
    "Yalta", "Kaliningrad", "Vladivostok", "Novosibirsk", "Yekaterinburg", "Kazan",
    "St Petersburg", "Dubai",
)

#: Every token of the gazetteer, for single-word place membership tests.
PLACE_TOKENS: frozenset[str] = frozenset(
    token for name in JURISDICTION_GAZETTEER for token in name.split()
) | frozenset(JURISDICTION_GAZETTEER)


# --------------------------------------------------------------------------- #
# Sentence splitting for the no-model backends
# --------------------------------------------------------------------------- #

#: Abbreviations that never terminate a sentence.
#:
#: Corporate suffixes (Ltd., Inc., GmbH) are deliberately *absent*: in news copy
#: they overwhelmingly do end the sentence, and treating them as non-terminal
#: merges "…Midea Holdings Ltd. Igor Sechin…" into one runaway entity.
_ABBREVIATIONS = frozenset(
    """mr mrs ms messrs dr prof profs sr jr st mt no nos vs etc al ibid gov sen rep gen col
    capt lt maj adm rev hon pres supt det insp fig figs vol vols pp para ch sec art
    jan feb mar apr jun jul aug sep sept oct nov dec
    u.s u.k u.s.a u.e d.c a.m p.m e.g i.e cf approx""".split()
)

_SENTENCE_TERMINATOR_RE = re.compile(r"[.!?][\"\')\]]*(?=\s|$)")
_SENTENCE_START_RE = re.compile(r"[A-Z0-9\"\'“‘(\[]")


def split_sentences(text: str, max_sentences: int = 1500) -> list[tuple[int, int, str]]:
    """Regex sentence splitter that preserves absolute character offsets.

    Paragraph breaks are hard boundaries; within a paragraph a terminator only
    ends a sentence when the token before it is not a known abbreviation and
    the following token looks like a sentence start.
    """
    spans: list[tuple[int, int, str]] = []
    if not text:
        return spans
    for paragraph in re.finditer(r"\S[^\n]*", text):
        para = paragraph.group(0)
        base = paragraph.start()
        boundaries = [0]
        for match in _SENTENCE_TERMINATOR_RE.finditer(para):
            preceding = para[max(0, match.start() - 30):match.start() + 1]
            tail_words = re.findall(r"[A-Za-z][A-Za-z.]*$", preceding)
            if tail_words and tail_words[0].lower().rstrip(".") in _ABBREVIATIONS:
                continue
            rest = para[match.end():].lstrip()
            if rest and not _SENTENCE_START_RE.match(rest):
                continue
            boundaries.append(match.end())
        boundaries.append(len(para))
        for start, end in zip(boundaries, boundaries[1:], strict=False):  # paired offsets, one shorter by design
            chunk = para[start:end].strip()
            if not chunk:
                continue
            offset = start + (len(para[start:end]) - len(para[start:end].lstrip()))
            spans.append((base + offset, base + offset + len(chunk), chunk))
            if len(spans) >= max_sentences:
                return spans
    return spans


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def _label_for_type(entity_type: EntityType) -> str:
    """Map a PuppetNET entity type back onto a spaCy-style label."""
    return {
        EntityType.PERSON: "PERSON",
        EntityType.ORGANIZATION: "ORG",
        EntityType.LOCATION: "GPE",
        EntityType.CRAFT: "CRAFT",
        EntityType.UNKNOWN: "ORG",
    }[entity_type]


#: Consumer-software surfaces that a capitalisation-driven NER would otherwise
#: promote to ORGANIZATION. Vessel prefixes (``MS``/``MV``) make this acute:
#: "MS Windows" is an operating system, "MS Amadea" is a motor ship.
PRODUCT_SURFACE_RE = re.compile(
    r"""^\s*(?:
          (?:ms|microsoft)\s+(?:windows|office|word|excel|outlook|teams|azure|dynamics|vista|xp|edge|sql\s+server)
        | windows\s+(?:vista|xp|server|10|11|7|8|95|98|nt|phone)
        | (?:apple\s+)?(?:iphone|ipad|ipod|macbook|imac|mac\s+pro|airpods|watchos|ios|macos)
        | google\s+(?:chrome|android|pixel|docs|sheets|meet|workspace)
        | playstation(?:\s*[2-6])?|ps[2-6]|xbox(?:\s+(?:one|360|series\s+[xs]))?|nintendo\s+(?:switch|wii)
        | android|chromebook|kubernetes|docker|postgresql|mysql|mariadb
      )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _looks_like_product(text: str) -> bool:
    """True for consumer-software names that must never become graph entities."""
    surface = (text or "").strip()
    if not surface:
        return False
    if PRODUCT_SURFACE_RE.match(surface):
        return True
    words = surface.split()
    if len(words) == 1:
        return bool(re.fullmatch(r"(?:PS[2-6]|XBOX|iOS|Windows|Android|Chromebook)", words[0], re.IGNORECASE))
    # "MS <product>" / "SS <product>" style vendor-prefixed names.
    if words[0].upper().replace(".", "/") in {"MS", "M/S"} and len(words) > 1:
        from .craft import _is_product_name

        return _is_product_name(" ".join(words[1:]), "MS")
    return False


#: When one surface form is registered under two types, specificity breaks ties
#: an identifier or confidence score cannot settle.
_TYPE_PRIORITY: dict[Any, int] = {
    EntityType.CRAFT: 4,
    EntityType.ORGANIZATION: 3,
    EntityType.PERSON: 2,
    EntityType.LOCATION: 1,
    EntityType.UNKNOWN: 0,
}

#: Properties that can only come from a hard craft identifier, never from a
#: capitalisation guess. A span carrying one is never demoted.
_HARD_CRAFT_KEYS = frozenset(
    {"registration", "imo", "mmsi", "flight_number", "registry_country", "vessel_prefix", "airline_code"}
)


def _has_hard_craft_id(entity: Entity) -> bool:
    return any(entity.properties.get(key) for key in _HARD_CRAFT_KEYS)


def _mirror_entity(target: Entity, source: Entity) -> None:
    """Rewrite ``target`` to be an exact copy of ``source``.

    Relations keep direct references to the entity objects they were built
    with, so a loser of type reconciliation has to *become* the winner rather
    than be discarded — otherwise the edge still points at the dropped key.
    """
    target.name = source.name
    target.entity_type = source.entity_type
    target.canonical_key = source.canonical_key
    target.aliases = set(source.aliases)
    target.mentions = list(source.mentions)
    target.properties = dict(source.properties)
    target.source_ids = set(source.source_ids)
    target.doc_ids = set(source.doc_ids)
    target.confidence = source.confidence


def trim_surface_at_boundary(start: int, surface: str) -> tuple[int, int, str]:
    """Cut a greedy capitalised run at the first sentence-terminal ". ".

    Both the lexical NER regexes and the relation patterns allow "." inside a
    token, so a corporate suffix can swallow the next sentence: "Midea Holdings
    Ltd. Igor Sechin". Trimming here keeps one organisation and one person
    instead of a hybrid entity that then renames the organisation.
    """
    text = (surface or "").strip()
    cut = text.find(". ")
    if cut > 0:
        text = text[:cut]
    text = text.rstrip(" .,;:")
    return start, start + len(text), text


def _is_same_claim(left: Relation, right: Relation) -> bool:
    """True when two relations restate one clause instead of corroborating it.

    Independence is what justifies noisy-OR merging. Two triples drawn from the
    same sentence whose argument spans overlap are one claim read twice, so the
    stronger reading wins rather than inflating confidence.
    """
    if left.doc_id != right.doc_id or left.sentence_index != right.sentence_index:
        return False

    def overlaps(a: tuple[int, int] | None, b: tuple[int, int] | None) -> bool:
        if a is None or b is None:
            return True  # no offsets to compare — assume the same clause
        return a[0] < b[1] and b[0] < a[1]

    return overlaps(left.subject_span, right.subject_span) and overlaps(left.obj_span, right.obj_span)


class NLPEngine:
    """Load spaCy once, parse many documents."""

    def __init__(
        self,
        settings: Any,
        *,
        mapper: RelationMapper | None = None,
        craft_detector: CraftDetector | None = None,
        eager_load: bool = True,
    ) -> None:
        self.settings = settings
        self.mapper = mapper or RelationMapper()
        self.craft = craft_detector or CraftDetector(extra_terms=tuple(getattr(settings, "craft_gazetteer_extra", ()) or ()))
        self.nlp: Any = None
        self.backend = "uninitialised"
        self.has_parser = False
        self.has_ner = False
        #: True only for pretrained models — the blank pipeline's EntityRuler is
        #: a gazetteer, not a statistical recogniser.
        self.has_statistical_ner = False
        self.model_candidates: list[str] = list(settings.spacy_models)
        self.load_errors: list[str] = []
        self._loaded = False
        if eager_load:
            self.load()

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def load(self) -> NLPEngine:
        """Resolve the best available spaCy pipeline. Idempotent."""
        if self._loaded:
            return self
        started = time.perf_counter()
        try:
            import spacy  # noqa: F401
        except ImportError as exc:
            self.load_errors.append(f"spaCy not importable: {exc}")
            logger.warning("spaCy is unavailable (%s) — using the pure-Python pattern backend", exc)
            self.backend = "pattern"
            self.has_parser = False
            self.has_ner = False
            self.has_statistical_ner = False
            self._loaded = True
            return self

        for name in self.model_candidates:
            try:
                import spacy

                nlp = spacy.load(name)
                self._configure(nlp, name)
                self.backend = f"spacy:{name}"
                self.has_parser = nlp.has_pipe("parser")
                self.has_ner = nlp.has_pipe("ner") or nlp.has_pipe("entity_ruler")
                self.has_statistical_ner = nlp.has_pipe("ner")
                logger.info(
                    "NLP backend ready: %s (parser=%s ner=%s pipes=%s) in %.1fs",
                    name, self.has_parser, self.has_ner, [p for p in nlp.pipe_names], time.perf_counter() - started,
                )
                self._loaded = True
                return self
            except (ImportError, OSError, ValueError) as exc:
                self.load_errors.append(f"{name}: {exc.__class__.__name__}: {exc}")
                logger.warning("could not load spaCy model %s: %s", name, exc)
                continue

        # No statistical model → blank pipeline with gazetteer NER.
        try:
            import spacy

            nlp = spacy.blank("en")
            nlp.add_pipe("sentencizer")
            ruler = nlp.add_pipe("entity_ruler")
            ruler.add_patterns(self._gazetteer_patterns())
            self._configure(nlp, "blank")
            self.backend = "spacy:blank+gazetteer"
            self.has_parser = False
            self.has_ner = True
            self.has_statistical_ner = False
            logger.warning(
                "No pretrained model available (%s). Using spaCy blank pipeline: "
                "gazetteer entities only, all relations fall back to co-occurrence with a %.2f penalty.",
                "; ".join(self.load_errors)[:300],
                1.0 - COOCCURRENCE_FACTOR,
            )
            self._loaded = True
            return self
        except Exception as exc:  # pragma: no cover - spaCy import already failed
            self.load_errors.append(f"blank: {exc}")
            logger.error("spaCy blank pipeline could not be built (%s) — using the pure-Python backend", exc)
            self.backend = "pattern"
            self.has_parser = False
            self.has_ner = False
            self.has_statistical_ner = False
            self._loaded = True
            return self

    def _configure(self, nlp: Any, name: str) -> None:
        nlp.max_length = max(2_000_000, self.settings.nlp_max_chars_per_doc * 4)
        nlp.default_chunk_size = getattr(self.settings, "nlp_batch_size", 16) * 512

        # Register the CRAFT extension before any span uses it.
        try:
            from spacy.tokens import Doc, Span, Token

            if not Span.has_extension("craft_props"):
                Span.set_extension("craft_props", default=None)
            if not Doc.has_extension("parse_backend"):
                Doc.set_extension("parse_backend", default=name)
            if not Token.has_extension("is_hedge"):
                Token.set_extension("is_hedge", default=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("could not register span extensions: %s", exc)

        # CRAFT gazetteer as a real pipeline component so craft spans take part
        # in the dependency parse (they can be subjects and objects).
        if self.settings.enable_craft_detection and nlp.has_pipe("ner"):
            try:
                ruler = nlp.add_pipe("entity_ruler", before="ner", name="puppetnet_craft_ruler")
                ruler.add_patterns(self.craft.spacy_patterns())
                logger.info("CRAFT EntityRuler installed with %d patterns", len(ruler.patterns))
            except Exception as exc:
                logger.warning("could not install the CRAFT EntityRuler: %s", exc)
        elif self.settings.enable_craft_detection:
            try:
                ruler = nlp.add_pipe("entity_ruler", name="puppetnet_craft_ruler")
                ruler.add_patterns(self.craft.spacy_patterns())
            except Exception as exc:
                logger.debug("blank pipeline ruler install failed: %s", exc)

        # Disable pipes that do not contribute to entities or triples.
        # Only pipes that contribute nothing to entities or triples are dropped.
        # The lemmatizer stays: verb lemmas are the join key into the relation
        # lexicon, and losing it would silently degrade every edge.
        disable = [pipe for pipe in ("entity_linker", "textcat", "morphanalyzer") if nlp.has_pipe(pipe)]
        if disable:
            try:
                nlp.disable_pipes(*disable)
            except Exception as exc:  # pragma: no cover
                logger.debug("could not disable pipes %s: %s", disable, exc)

        if not nlp.has_pipe("parser") and not nlp.has_pipe("sentencizer"):
            nlp.add_pipe("sentencizer")
        self.nlp = nlp

    @staticmethod
    def _gazetteer_patterns() -> list[dict[str, Any]]:
        """EntityRuler patterns used by the blank backend.

        Covers the vocabulary that matters most for OSINT graphs: offshore
        jurisdictions, capital cities and organisation suffixes. Personal names
        cannot be gazetteered, so the blank backend leans on capitalisation
        heuristics for PERSON and reports that limitation in ``describe()``.
        """
        patterns: list[dict[str, Any]] = []
        for term in JURISDICTION_GAZETTEER:
            patterns.append({"label": "GPE", "pattern": term})
        org_suffix_terms = [
            "Offshore", "Holdings", "Capital", "Investments", "Trading", "Enterprises", "International",
            "Petroleum", "Energy", "Bank", "Trust", "Foundation", "Limited", "Group", "Partners",
        ]
        for term in org_suffix_terms:
            patterns.append({
                "label": "ORG",
                "pattern": [{"IS_TITLE": True}, {"IS_TITLE": True, "LOWER": term.lower()}],
            })
        return patterns

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self.backend not in {"uninitialised"}

    def parse_document(self, document: Document) -> ParseResult:
        """Extract entities and weighted relations from one document."""
        if not self._loaded:
            self.load()
        started = time.perf_counter()
        result = ParseResult(doc_id=document.doc_id, backend=self.backend)
        text = (document.text or "").strip()
        if not text:
            result.warnings.append("empty-document")
            result.elapsed_seconds = time.perf_counter() - started
            return result

        max_chars = int(self.settings.nlp_max_chars_per_doc)
        if len(text) > max_chars:
            text = text[:max_chars]
            result.warnings.append(f"truncated-to-{max_chars}-chars")

        source_weight = float(document.source_weight)
        registry: dict[str, Entity] = {}
        sentence_offset = 0
        sentence_cap = int(self.settings.nlp_max_sentences_per_doc)

        for chunk in self._iter_chunks(text, max_chars=max(20_000, max_chars // 4)):
            if sentence_offset > sentence_cap:
                break
            for view in self._sentences_for_chunk(chunk):
                view.index = sentence_offset
                sentence_offset += 1
                if sentence_offset > sentence_cap:
                    result.warnings.append("sentence-cap-reached")
                    break
                result.sentences += 1

                # -- entities --------------------------------------------
                span_entities = self._normalise_spans(view.entities)
                span_entities = self._craft_supplement(span_entities, view.text)
                result.craft_mentions += sum(1 for e in span_entities if e.label == "CRAFT")
                usable = [e for e in span_entities if self._keep_entity(e)]
                for span in usable:
                    self._register_entity(registry, span, document, view.index)

                # -- triples ---------------------------------------------
                triples: list[RawTriple] = []
                produced_any = False
                if self.has_parser:
                    result.negated_dropped += sum(1 for t in view.triples if t.negated)
                    triples = [t for t in view.triples if not t.negated]
                    # A parse that *understood* the sentence — even one whose
                    # only triple was negated — suppresses the co-occurrence
                    # fallback. Otherwise "X does not own Y" would reappear as a
                    # weak ASSOCIATED_WITH edge, contradicting the source.
                    if view.triples:
                        produced_any = True
                    for triple in triples:
                        relation = self._build_relation(
                            triple, registry, document, source_weight, ExtractionMethod.DEPENDENCY, view
                        )
                        if relation is not None:
                            result.relations.append(relation)
                            result.dependency_triples += 1
                            produced_any = True
                else:
                    # No dependency parse is available (blank pipeline or no
                    # spaCy at all). Lexical patterns still recover real
                    # triples, but without a parse we cannot verify argument
                    # structure, so every edge is priced at the co-occurrence
                    # rate — the mandated 0.2 penalty.
                    for triple in self._pattern_triples(view):
                        if triple.negated:
                            result.negated_dropped += 1
                            continue
                        relation = self._build_relation(
                            triple, registry, document, source_weight, ExtractionMethod.COOCCURRENCE, view
                        )
                        if relation is not None:
                            result.relations.append(relation)
                            result.pattern_triples += 1
                            produced_any = True

                # -- co-occurrence fallback -------------------------------
                if not produced_any and self.settings.enable_cooccurrence_fallback and len(usable) >= 2:
                    for relation in self._cooccurrence_relations(usable, registry, document, source_weight, view):
                        result.relations.append(relation)
                        result.cooccurrence_triples += 1

        result.characters = len(text)
        self._reconcile_entity_types(registry)
        result.entities = list(registry.values())
        result.relations = self._dedupe_relations(result.relations)
        result.elapsed_seconds = time.perf_counter() - started
        logger.debug(
            "parsed %s with %s: %d entities, %d relations (%d dep / %d cooc) in %.2fs",
            document.doc_id, self.backend, len(result.entities), len(result.relations),
            result.dependency_triples, result.cooccurrence_triples, result.elapsed_seconds,
        )
        return result

    def parse_text(self, text: str, *, source_weight: float = 0.4, doc_id: str = "inline", source_id: str = "inline") -> ParseResult:
        """Convenience wrapper used by tests and the ad-hoc CLI."""
        document = Document(doc_id=doc_id, source_id=source_id, url=f"inline://{doc_id}", text=text, source_weight=source_weight)
        return self.parse_document(document)

    # ------------------------------------------------------------------ #
    # Backend dispatch
    # ------------------------------------------------------------------ #
    def _iter_chunks(self, text: str, *, max_chars: int) -> Iterator[str]:
        """Split long documents on paragraph/sentence boundaries."""
        if len(text) <= max_chars:
            yield text
            return
        position = 0
        while position < len(text):
            end = min(position + max_chars, len(text))
            if end < len(text):
                # Prefer a paragraph break, then a sentence break, then a space.
                window = text[position:end]
                cut = window.rfind("\n\n")
                if cut < max_chars * 0.5:
                    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
                if cut < max_chars * 0.5:
                    cut = window.rfind(" ")
                if cut > 0:
                    end = position + cut + 1
            yield text[position:end]
            position = end

    def _sentences_for_chunk(self, chunk: str) -> list[SentenceView]:
        if self.nlp is not None:
            return self._sentences_spacy(chunk)
        return self._sentences_patterns(chunk)

    def _sentences_spacy(self, chunk: str) -> list[SentenceView]:
        try:
            doc = self.nlp(chunk)
        except (MemoryError, ValueError) as exc:
            logger.warning("spaCy failed on a %d-char chunk (%s) — falling back to regex splitting", len(chunk), exc)
            return self._sentences_patterns(chunk)

        entity_lookup = self._index_entities(doc)
        views: list[SentenceView] = []
        for index, sent in enumerate(doc.sents):
            spans = [
                self._to_sentence_relative(entity_lookup[i], sent.start_char)
                for i in range(sent.start, sent.end)
                if i in entity_lookup
            ]
            if not self.has_statistical_ner:
                spans = self._merge_span_entities(spans, self._lexical_spans(sent.text))
            triples: list[RawTriple] = []
            parse_ok = True
            if self.has_parser:
                try:
                    triples = self._dependency_triples(doc, sent, spans, index)
                except Exception as exc:  # pragma: no cover - defensive
                    parse_ok = False
                    logger.warning("dependency parsing failed on sentence %d: %s", index, exc)
            views.append(SentenceView(index=index, text=sent.text.strip(), entities=spans, triples=triples, parse_ok=parse_ok))
        return views

    def _lexical_spans(self, sentence_text: str) -> list[SpanEntity]:
        """Capitalisation/gazetteer spans for pipelines without statistical NER."""
        spans: list[SpanEntity] = []
        for start, end, surface, label, confidence, properties in self._pattern_entities(sentence_text):
            spans.append(
                SpanEntity(
                    text=surface,
                    label=label,
                    start_char=start,
                    end_char=end,
                    start_token=-1,
                    end_token=-1,
                    confidence=confidence,
                    properties=properties,
                )
            )
        return spans

    @staticmethod
    def _merge_span_entities(existing: Sequence[SpanEntity], candidates: Sequence[SpanEntity]) -> list[SpanEntity]:
        """Add candidates that do not overlap an already-detected span.

        An overlap is not automatically a loss. The blank pipeline's CRAFT
        EntityRuler emits spans with no properties, while the craft detector
        knows the registry country, IMO or airline code for the same offsets;
        and a statistical model may label "superyacht Amadea" ORG where the
        detector is certain it is a vessel. Overlapping candidates therefore
        *enrich* or *promote* the span they collide with.
        """
        merged = list(existing)
        for candidate in candidates:
            if candidate.start_char < 0:
                merged.append(candidate)
                continue
            collision = next(
                (
                    index
                    for index, span in enumerate(merged)
                    if span.start_char >= 0 and candidate.start_char < span.end_char and candidate.end_char > span.start_char
                ),
                None,
            )
            if collision is None:
                merged.append(candidate)
                continue

            current = merged[collision]
            if candidate.label == "CRAFT" and current.label != "CRAFT":
                merged[collision] = candidate  # a positive craft ID beats a guess
            elif candidate.properties and not current.properties:
                merged[collision] = replace(current, properties=dict(candidate.properties))
            elif candidate.properties and current.properties:
                combined = {**candidate.properties, **current.properties}
                merged[collision] = replace(current, properties=combined)
        return merged

    @staticmethod
    def _to_sentence_relative(span: SpanEntity, sentence_start_char: int) -> SpanEntity:
        """Re-base entity offsets so every span in a view is sentence-relative.

        The craft detector, the co-occurrence distance metric and the stored
        ``subject_span``/``obj_span`` evidence offsets all assume this frame.
        """
        if span.start_char < 0:
            return span
        return SpanEntity(
            text=span.text,
            label=span.label,
            start_char=max(0, span.start_char - sentence_start_char),
            end_char=max(0, span.end_char - sentence_start_char),
            start_token=span.start_token,
            end_token=span.end_token,
            confidence=span.confidence,
            properties=span.properties,
        )

    @staticmethod
    def _index_entities(doc: Any) -> dict[int, SpanEntity]:
        """Map each sentence-start token index to its entity span."""
        out: dict[int, SpanEntity] = {}
        for ent in getattr(doc, "ents", ()) or ():
            props: dict[str, Any] = {}
            try:
                craft_props = ent._.craft_props
                if craft_props:
                    props.update(craft_props)
            except (AttributeError, ValueError):
                pass
            span = SpanEntity(
                text=ent.text.strip(),
                label=ent.label_,
                start_char=ent.start_char,
                end_char=ent.end_char,
                start_token=ent.start,
                end_token=ent.end,
                confidence=1.0,
                properties=props,
            )
            out.setdefault(ent.start, span)
        return out

    # ------------------------------------------------------------------ #
    # Dependency-based SVO extraction
    # ------------------------------------------------------------------ #
    def _dependency_triples(self, doc: Any, sent: Any, spans: Sequence[SpanEntity], sentence_index: int) -> list[RawTriple]:
        """Extract subject-verb-object triples from one sentence's parse."""
        triples: list[RawTriple] = []
        ent_by_token = self._entity_token_index(spans)
        sent_text = sent.text.strip()

        verbs: list[Any] = []
        copulas: list[Any] = []
        for token in sent:
            if token.is_space:
                continue
            if token.pos_ not in {"VERB", "AUX"} and not token.tag_.startswith("VB"):
                continue
            if token.dep_ in {"aux", "auxpass", "mark", "cop"}:
                continue
            lemma = (token.lemma_ or token.text).lower()
            # A copula is the *root* of a nominal predication ("X is the
            # director of Y"), so it must reach _nominal_predication even
            # though it carries no verb semantics of its own.
            if lemma in COPULA_LEMMAS:
                copulas.append(token)
                continue
            if lemma in SKIP_VERB_LEMMAS:
                continue
            verbs.append(token)

        if not verbs and not copulas:
            return triples

        for verb in verbs:
            negated = self._verb_negated(verb)
            hedged = self._verb_hedged(verb)
            phrasal = tuple(child.orth_.lower() for child in verb.children if child.dep_ in {"prt", "advmod"} and child.orth_.isalpha())

            subjects = self._argument_entities(verb, SUBJECT_DEPS, ent_by_token, doc)
            agents = self._agent_entities(verb, ent_by_token, doc)
            objects = self._argument_entities(verb, OBJECT_DEPS, ent_by_token, doc)
            prep_objects = self._prepositional_objects(verb, ent_by_token, doc)

            passive = any(dep in PASSIVE_DEPS for dep, _ in subjects) or bool(agents)

            # Full passive with an explicit agent → restore active order so the
            # canonical direction of every predicate stays consistent.
            if passive and agents:
                # "Nord Stream was acquired by Gazprom": the by-phrase agent is
                # the real actor (subject) and the grammatical subject is the
                # patient (object). Dropping the patient here would silently
                # delete every full-passive triple.
                patients = [slot for slot in subjects if slot[0] in PASSIVE_DEPS]
                other_subjects = [slot for slot in subjects if slot[0] not in PASSIVE_DEPS]
                subject_slots = agents
                object_slots = patients + objects + prep_objects + other_subjects
            else:
                subject_slots = subjects
                object_slots = objects + prep_objects

            if not subject_slots or not object_slots:
                continue

            for (_, subject_span), (_, object_span) in itertools.product(subject_slots[:3], object_slots[:3]):
                if subject_span.text == object_span.text and subject_span.start_char == object_span.start_char:
                    continue
                distance = abs(object_span.start_token - subject_span.start_token)
                if distance > self.settings.max_argument_distance:
                    continue
                triples.append(
                    RawTriple(
                        subject=subject_span,
                        obj=object_span,
                        verb_lemma=(verb.lemma_ or verb.text).lower(),
                        verb_surface=verb.text,
                        verb_dep=verb.dep_,
                        passive=passive,
                        negated=negated,
                        hedged=hedged,
                        distance=distance,
                        clause_text=self._clause_text(verb, sent_text),
                        sentence_index=sentence_index,
                        phrasal=phrasal,
                        subject_span=(subject_span.start_char, subject_span.end_char),
                        obj_span=(object_span.start_char, object_span.end_char),
                    )
                )

            # Copular / nominal predication: "X is the director of Y".
            for copula_triple in self._nominal_predication(verb, ent_by_token, doc, sentence_index, sent_text, negated, hedged):
                triples.append(copula_triple)

        for copula in copulas:
            triples.extend(
                self._nominal_predication(
                    copula,
                    ent_by_token,
                    doc,
                    sentence_index,
                    sent_text,
                    self._verb_negated(copula),
                    self._verb_hedged(copula),
                )
            )

        return triples

    @staticmethod
    def _entity_token_index(spans: Sequence[SpanEntity]) -> dict[int, SpanEntity]:
        index: dict[int, SpanEntity] = {}
        for span in spans:
            if span.start_token < 0:
                continue
            for token_index in range(span.start_token, span.end_token):
                index.setdefault(token_index, span)
        return index

    def _argument_entities(
        self,
        verb: Any,
        deps: frozenset[str],
        ent_by_token: dict[int, SpanEntity],
        doc: Any,
    ) -> list[tuple[str, SpanEntity]]:
        """Resolve a verb's argument slot(s) to entity spans, expanding conjunctions."""
        found: list[tuple[str, SpanEntity]] = []
        for child in verb.children:
            if child.dep_ not in deps:
                continue
            slot = child.dep_

            for token in self._conjunct_expansion(child):
                span = self._resolve_entity(token, ent_by_token, doc)
                if span is not None and all(span.text != existing.text or span.start_char != existing.start_char for _, existing in found):
                    found.append((slot, span))
        return found

    def _agent_entities(self, verb: Any, ent_by_token: dict[int, SpanEntity], doc: Any) -> list[tuple[str, SpanEntity]]:
        """Find the ``by``-phrase of a passive verb, whatever the label scheme.

        spaCy v2 English used ``agent``; v3 UD-style parses use ``obl`` with a
        ``prep`` child; some models attach ``pobj`` directly. Accepting all
        three keeps passive handling portable across ``en_core_web_*`` releases.
        """
        found: list[tuple[str, SpanEntity]] = []
        for child in verb.children:
            if child.dep_ not in AGENT_DEPS and child.dep_ not in {"prep", "pobj"}:
                continue
            is_agent = False
            if child.dep_ in AGENT_DEPS:
                preps = [t.text.lower() for t in child.subtree if t.dep_ in {"prep", "case"}]
                is_agent = child.dep_ == "agent" or "by" in preps or child.text.lower() == "by"
            elif child.dep_ == "prep" and child.text.lower() == "by" and any(
                t.dep_ in PASSIVE_DEPS for t in verb.children
            ):
                is_agent = True
            elif child.dep_ == "pobj":
                head = getattr(child, "head", None)
                is_agent = bool(head is not None and head.dep_ in {"prep", "agent"} and head.text.lower() == "by")
            if not is_agent:
                continue
            anchor = child
            if child.text.lower() == "by":
                descendants = [t for t in child.subtree if t.i != child.i]
                anchor = descendants[0] if descendants else child
            for token in self._conjunct_expansion(anchor):
                span = self._resolve_entity(token, ent_by_token, doc)
                if span is not None:
                    found.append(("agent", span))
        return found

    @staticmethod
    def _conjunct_expansion(token: Any) -> list[Any]:
        """``X, Y and Z`` → all three argument heads."""
        tokens = [token]
        for child in token.children:
            if child.dep_ in {"conj", "appos"} and child.pos_ in {"NOUN", "PROPN", "ADJ", "NUM"}:
                tokens.append(child)
        return tokens[:4]

    @staticmethod
    def _resolve_entity(token: Any, ent_by_token: dict[int, SpanEntity], doc: Any) -> SpanEntity | None:
        """Find the entity that an argument token refers to.

        Preference order: the token itself → an entity inside the token's
        subtree (``the board of Gazprom`` → *Gazprom* when reached via ``of``)
        → an entity containing the token's head noun chunk.
        """
        direct = ent_by_token.get(token.i)
        if direct is not None:
            return direct
        try:
            subtree = list(token.subtree)
        except Exception:  # pragma: no cover
            subtree = [token]
        for descendant in subtree[:40]:
            span = ent_by_token.get(descendant.i)
            if span is not None:
                return span
        chunk = getattr(token, "chunk", None) if hasattr(token, "chunk") else None
        if chunk is not None:
            for candidate in range(chunk.start, chunk.end):
                span = ent_by_token.get(candidate)
                if span is not None:
                    return span
        head = getattr(token, "head", None)
        if head is not None and head.i != token.i:
            return ent_by_token.get(head.i)
        return None

    def _prepositional_objects(self, verb: Any, ent_by_token: dict[int, SpanEntity], doc: Any) -> list[tuple[str, SpanEntity]]:
        """``VERB → prep → pobj`` argument pairs (travelled *to* Moscow)."""
        found: list[tuple[str, SpanEntity]] = []
        for child in verb.children:
            if child.dep_ not in {"prep", "agent"}:
                continue
            prep = child.text.lower()
            if prep not in ARGUMENT_PREPS:
                continue
            for grandchild in child.children:
                if grandchild.dep_ not in {"pobj", "pcomp"}:
                    continue
                for token in self._conjunct_expansion(grandchild):
                    span = self._resolve_entity(token, ent_by_token, doc)
                    if span is not None:
                        found.append((f"prep:{prep}", span))
        return found

    def _nominal_predication(
        self, verb: Any, ent_by_token: dict[int, SpanEntity], doc: Any, sentence_index: int, sent_text: str, negated: bool, hedged: bool
    ) -> Iterator[RawTriple]:
        """Handle ``X is/was <NP> of Y`` copular constructions."""
        if verb.lemma_.lower() not in {"be", "become", "remain", "serve"}:
            return
        attrs = [child for child in verb.children if child.dep_ in {"attr", "acomp", "nsubj", "npadvmod"}]
        subject = self._argument_entities(verb, SUBJECT_DEPS, ent_by_token, doc)
        if not subject:
            return
        for attr in attrs:
            attr_span = self._resolve_entity(attr, ent_by_token, doc)
            prep_objects = []
            for child in attr.children:
                if child.dep_ == "prep" and child.text.lower() in ARGUMENT_PREPS:
                    for grandchild in child.children:
                        if grandchild.dep_ in {"pobj", "pcomp"}:
                            span = self._resolve_entity(grandchild, ent_by_token, doc)
                            if span is not None:
                                prep_objects.append((child.text.lower(), span))
            # Apposition: the attribute noun itself may name a role ("director")
            role_lemma = (attr.lemma_ or attr.text).lower()
            for prep, obj_span in prep_objects:
                for _, subj_span in subject:
                    if subj_span.text == obj_span.text:
                        continue
                    distance = abs(obj_span.start_token - subj_span.start_token)
                    yield RawTriple(
                        subject=subj_span,
                        obj=obj_span,
                        verb_lemma=role_lemma,
                        verb_surface=attr.text,
                        verb_dep="attr",
                        passive=False,
                        negated=negated,
                        hedged=hedged,
                        distance=distance,
                        clause_text=self._clause_text(verb, sent_text),
                        sentence_index=sentence_index,
                        preposition=prep,
                        subject_span=(subj_span.start_char, subj_span.end_char),
                        obj_span=(obj_span.start_char, obj_span.end_char),
                    )
            # ``_resolve_entity`` walks the attribute's subtree, so a role noun
            # ("the director of Rosneft") resolves to *Rosneft* too. Emitting
            # both the prep-object triple and that subtree triple duplicates the
            # same edge with a weaker, preposition-less reading.
            prep_object_keys = {(span.start_char, span.end_char) for _, span in prep_objects}
            if attr_span is not None and (attr_span.start_char, attr_span.end_char) not in prep_object_keys:
                for _, subj_span in subject:
                    if subj_span.text == attr_span.text:
                        continue
                    yield RawTriple(
                        subject=subj_span,
                        obj=attr_span,
                        verb_lemma=role_lemma,
                        verb_surface=attr.text,
                        verb_dep="attr",
                        passive=False,
                        negated=negated,
                        hedged=hedged,
                        distance=abs(attr_span.start_token - subj_span.start_token),
                        clause_text=self._clause_text(verb, sent_text),
                        sentence_index=sentence_index,
                        subject_span=(subj_span.start_char, subj_span.end_char),
                        obj_span=(attr_span.start_char, attr_span.end_char),
                    )

    @staticmethod
    def _clause_text(verb: Any, sentence_text: str) -> str:
        """The clause containing ``verb`` — used for hedging/negation scope."""
        try:
            subtree = sorted(verb.subtree, key=lambda t: t.i)
            if not subtree:
                return sentence_text
            start = subtree[0].idx
            end = subtree[-1].idx + len(subtree[-1].text)
            clause = verb.doc.text[start:end].strip()
            return clause or sentence_text
        except Exception:  # pragma: no cover
            return sentence_text

    @staticmethod
    def _verb_negated(verb: Any) -> bool:
        for child in verb.children:
            if child.dep_ == "neg" or child.orth_.lower() in NEGATION_TOKENS:
                return True
        head = getattr(verb, "head", None)
        if head is not None and head.i != verb.i:
            for child in head.children:
                if child.dep_ == "neg":
                    return True
        return False

    @staticmethod
    def _verb_hedged(verb: Any) -> bool:
        for child in verb.children:
            if child.dep_ in {"aux", "advmod"} and child.orth_.lower() in {"allegedly", "reportedly", "may", "might", "could", "purportedly", "supposedly", "apparently", "claimed"}:
                return True
        head = getattr(verb, "head", None)
        if head is not None and head.i != verb.i and head.orth_.lower() in {"may", "might", "could", "would", "should"}:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Pattern backend (no statistical model available)
    # ------------------------------------------------------------------ #
    _PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?P<v>owns|owned|controls|controlled|holds|held)\s+(?:a\s+|an\s+|the\s+)?(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-&]+){0,5})\b"), "verb", False),
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?i:(?P<hedge>allegedly|reportedly|purportedly|supposedly|apparently|previously|formerly|recently|later|also|once)\s+)?(?P<v>founded|established|created|registered|incorporated|launched|acquired|bought|sold|funded|financed|sanctioned|appointed|employed|hired|met|visited|boarded|chartered|operates|operated|owns|controls|holds)\s+(?:(?P<prep>with|to|from|in|at|into|for)\s+)?(?:a\s+|an\s+|the\s+)?(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-&]+){0,5})\b"), "verb", False),
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?i:(?P<hedge>allegedly|reportedly|purportedly|supposedly|apparently|previously|formerly|recently|later|also|once|may have|might have|could have)\s+)?(?P<v>traveled|travelled|flew|sailed|arrived|departed|landed|docked)\s+(?P<prep>to|from|in|into|at)\s+(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\b"), "verb", False),
        # Case-insensitivity is scoped to the role/verb group only: a global
        # re.IGNORECASE would let the [A-Z]-anchored argument groups swallow
        # lowercase connectives ("Rosneft and met with Vladimir Putin").
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4}),\s+(?i:(?P<role>director|chairman|founder|owner|shareholder|chief executive|ceo|president|trustee|beneficiary))\s+of\s+(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-&]+){0,5})\b"), "apposition", False),
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?i:is|was)\s+(?:the\s+|a\s+|an\s+)?(?i:(?P<role>director|chairman|founder|owner|shareholder|chief executive|ceo|president|trustee|subsidiary|parent))\s+of\s+(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-&]+){0,5})\b"), "apposition", False),
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?P<v>(?i:is based in|is registered in|is located in|is domiciled in|operates in|is headquartered in))\s+(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,3})\b"), "verb", False),
        (re.compile(r"\b(?P<s>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,4})\s+(?P<v>(?i:is owned by|is controlled by|is funded by|is sanctioned by|was acquired by|is operated by|was founded by))\s+(?P<o>[A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-&]+){0,5})\b"), "verb", True),
    )

    def _sentences_patterns(self, chunk: str) -> list[SentenceView]:
        """Regex backend: sentences + gazetteer/craft entities, no parse."""
        views: list[SentenceView] = []
        entities = self._pattern_entities(chunk)
        for index, (start, end, text) in enumerate(split_sentences(chunk, max_sentences=self.settings.nlp_max_sentences_per_doc)):
            spans = [
                SpanEntity(
                    text=surface,
                    label=label,
                    start_char=start + rel_start,
                    end_char=start + rel_end,
                    start_token=-1,
                    end_token=-1,
                    confidence=confidence,
                    properties=properties,
                )
                for (rel_start, rel_end, surface, label, confidence, properties) in entities
                if start <= start + rel_start < end
            ]
            views.append(SentenceView(index=index, text=text.strip(), entities=spans, triples=[], parse_ok=False, note="no-parser"))
        return views

    def _pattern_entities(self, text: str) -> list[tuple[int, int, str, str, float, dict[str, Any]]]:
        """Capitalisation + gazetteer NER for the model-less backend."""
        found: list[tuple[int, int, str, str, float, dict[str, Any]]] = []
        for match in self.craft.iter_matches(text):
            props = dict(match.properties)
            found.append((match.start, match.end, props.get("display_name") or match.text, "CRAFT", match.confidence, props))

        org_suffix_re = re.compile(
            r"\b([A-Z][\w.'’&\-]*(?:\s+[A-Z][\w.'’&\-]*){0,6}\s+"
            r"(?:Ltd|Limited|Inc|Incorporated|LLC|LLP|PLC|Corp|Corporation|Company|Co|GmbH|AG|SA|SARL|SRL|SAS|"
            r"NV|BV|AB|AS|OY|OJSC|PJSC|JSC|OOO|PAO|Holdings|Holding|Group|Bank|Trust|Foundation|Partners|"
            r"Enterprises|International|Investments|Capital|Trading|Petroleum|Energy|Logistics|Shipping|"
            r"Airlines|Airways|Air|Marine|Offshore|Consulting|Consultants|Technologies|Industries))\b"
        )
        for match in org_suffix_re.finditer(text):
            start, end, surface = trim_surface_at_boundary(match.start(), match.group(1).strip())
            if len(surface.split()) >= 2:
                found.append((start, end, surface, "ORG", 0.8, {}))

        person_re = re.compile(
            r"\b(?:Mr|Mrs|Ms|Dr|Prof|President|Minister|Senator|Director|Chairman|Captain|General|Sheikh|Mr\.|Mrs\.)\s+"
            r"([A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,3})\b"
        )
        for match in person_re.finditer(text):
            found.append((match.start(1), match.end(1), match.group(1).strip(), "PERSON", 0.85, {}))

        two_name_re = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z.'’\-]{2,}){1,2})\b")
        for match in two_name_re.finditer(text):
            surface = match.group(1)
            if any(match.start() < f[1] and match.end() > f[0] for f in found):
                continue
            if surface.lower() in NOISE_ENTITIES:
                continue
            start, end, surface = trim_surface_at_boundary(match.start(), surface)
            if surface:
                found.append((start, end, surface, "PERSON", 0.5, {}))

        place_re = re.compile(
            r"\b(?:in|near|from|to|at|between)\s+([A-Z][\w.'’\-]+(?:\s+[A-Z][\w.'’\-]+){0,3})\b"
        )
        for match in place_re.finditer(text):
            if any(match.start(1) < f[1] and match.end(1) > f[0] for f in found):
                continue
            found.append((match.start(1), match.end(1), match.group(1), "GPE", 0.55, {}))

        found.sort(key=lambda item: item[0])
        return found

    def _pattern_triples(self, view: SentenceView) -> Iterator[RawTriple]:
        """Regex SVO extraction for the model-less backend.

        The blank pipeline's sentencizer keeps "Midea Holdings Ltd. Igor Sechin"
        in one sentence, and a greedy argument group then swallows the next
        clause's subject — which also hides it from ``finditer``, because scans
        resume after the previous match. Re-segmenting with the abbreviation-aware
        splitter keeps each pattern match inside one clause.
        """
        text = view.text
        segments = [(start, end, piece) for start, end, piece in split_sentences(text)] or [(0, len(text), text)]
        for segment_start, _segment_end, segment_text in segments:
            yield from self._pattern_triples_in(segment_text, segment_start, view)

    def _pattern_triples_in(self, text: str, offset: int, view: SentenceView) -> Iterator[RawTriple]:
        """Pattern triples for one clause; ``offset`` re-bases char offsets."""
        for regex, _kind, passive in self._PATTERNS:
            for match in regex.finditer(text):
                groups = match.groupdict()
                subject_text = (groups.get("s") or "").strip()
                object_text = (groups.get("o") or "").strip()
                if not subject_text or not object_text or subject_text.lower() == object_text.lower():
                    continue
                # A greedy argument group can run past "Ltd." into the next
                # sentence; trim before resolving either argument to an entity.
                subject_start, subject_end, subject_text = trim_surface_at_boundary(offset + match.start("s"), subject_text)
                object_start, object_end, object_text = trim_surface_at_boundary(offset + match.start("o"), object_text)
                if not subject_text or not object_text or subject_text.lower() == object_text.lower():
                    continue
                verb = (groups.get("v") or groups.get("role") or "associate").strip().lower()
                subject_span = self._surface_span(subject_text, subject_start, subject_end, view.entities)
                object_span = self._surface_span(object_text, object_start, object_end, view.entities)
                preposition = (groups.get("prep") or "").lower()
                hedge = (groups.get("hedge") or "").lower()
                yield RawTriple(
                    subject=subject_span,
                    obj=object_span,
                    verb_lemma=verb,
                    verb_surface=verb,
                    verb_dep="ROOT",
                    passive=passive,
                    passive_flip=passive,
                    preposition=preposition,
                    negated=bool(re.search(r"\b(not|never|denied|refused|rejected)\b", text, re.IGNORECASE)),
                    hedged=bool(hedge or re.search(r"\b(allegedly|reportedly|purportedly|may|might|could)\b", text, re.IGNORECASE)),
                    distance=abs(object_start - subject_start) // 6,
                    clause_text=text,
                    sentence_index=view.index,
                    subject_span=(subject_start, subject_end),
                    obj_span=(object_start, object_end),
                )

    def _surface_span(self, surface: str, start: int, end: int, entities: Sequence[SpanEntity]) -> SpanEntity:
        """Prefer an already-detected entity span for a pattern-matched surface.

        This stops ``superyacht Amadea`` (CRAFT, from the craft detector) from
        also being written as ``Amadea`` (guessed ORGANIZATION) in the same
        sentence — one entity, one canonical key.
        """
        needle = surface.strip().lower()
        # Longest detected span first, so "Midea Holdings" wins over "Midea"
        # when the pattern group over-ran the entity.
        for span in sorted(entities, key=lambda candidate: len(candidate.text), reverse=True):
            candidate = span.text.strip().lower()
            if candidate == needle or needle in candidate or candidate.endswith(needle) or needle.startswith(candidate + " "):
                return SpanEntity(
                    text=span.text,
                    label=span.label,
                    start_char=span.start_char,
                    end_char=span.end_char,
                    confidence=max(span.confidence, 0.7),
                    properties=dict(span.properties),
                )
        label = _label_for_type(self._guess_type(surface))
        return SpanEntity(text=surface, label=label, start_char=start, end_char=end, confidence=0.6)

    @staticmethod
    def _guess_type(surface: str) -> EntityType:
        """Best-effort typing for the model-less backends.

        Single tokens are the hard case: they are far more often organisations
        or places than people, so PERSON is only assigned to runs of 2–4
        capitalised words.
        """
        tokens = [t for t in surface.split() if t]
        lowered = surface.lower()
        if re.search(
            r"\b(ltd|limited|inc|llc|llp|corp|corporation|company|gmbh|bank|holdings|holding|group|"
            r"airlines|airways|shipping|foundation|trust|plc|ojsc|pjsc|ooo|sarl|sas|nv|bv|partners|"
            r"enterprises|international|investments|capital|petroleum|energy|industries)\b",
            lowered,
        ):
            return EntityType.ORGANIZATION
        if any(term in lowered for term in ("yacht", "vessel", "tanker", "aircraft", "jet", "helicopter", "ship", "plane", "convoy", "motorcade", "limousine", "barge", "trawler")):
            return EntityType.CRAFT
        if surface in PLACE_TOKENS:
            return EntityType.LOCATION
        if len(tokens) == 1:
            return EntityType.ORGANIZATION
        if 2 <= len(tokens) <= 4 and all(t[:1].isupper() for t in tokens):
            return EntityType.PERSON
        if len(tokens) > 4:
            return EntityType.ORGANIZATION
        return EntityType.LOCATION

    # ------------------------------------------------------------------ #
    # Entity handling
    # ------------------------------------------------------------------ #
    def _normalise_spans(self, spans: Sequence[SpanEntity]) -> list[SpanEntity]:
        """Drop unusable spans and repair obvious label mistakes."""
        out: list[SpanEntity] = []
        for span in spans:
            text = span.text.strip().strip(".,;:'\"()[]{}«»“”‘’")
            if len(text) < 2 or len(text) > 120:
                continue
            if span.label in DROP_LABELS:
                continue
            if not re.search(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff]", text):
                continue
            if text.lower() in NOISE_ENTITIES:
                continue
            if span.label not in KEEP_LABELS:
                continue
            label = span.label
            if label == "PRODUCT":
                # Only promote products that are actually craft.
                if not self.craft.find_all(text):
                    continue
                label = "CRAFT"
            if label == "NORP":
                continue
            out.append(
                SpanEntity(
                    text=text,
                    label=label,
                    start_char=span.start_char,
                    end_char=span.end_char,
                    start_token=span.start_token,
                    end_token=span.end_token,
                    confidence=span.confidence,
                    properties=dict(span.properties),
                )
            )
        return out

    def _craft_supplement(self, spans: Sequence[SpanEntity], sentence_text: str) -> list[SpanEntity]:
        """Add CRAFT spans the statistical model missed (tail numbers, IMOs...)."""
        if not self.settings.enable_craft_detection or not sentence_text:
            return list(spans)
        merged = list(spans)
        occupied: list[tuple[int, int]] = [(s.start_char, s.end_char) for s in merged if s.start_char >= 0]
        for match in self.craft.iter_matches(sentence_text):
            if any(match.start < end and match.end > start for start, end in occupied):
                continue
            display = str(match.properties.get("display_name") or match.text)
            merged.append(
                SpanEntity(
                    text=display,
                    label="CRAFT",
                    start_char=match.start,
                    end_char=match.end,
                    start_token=-1,
                    end_token=-1,
                    confidence=match.confidence,
                    properties=dict(match.properties),
                )
            )
            occupied.append((match.start, match.end))
        return merged

    def _keep_entity(self, span: SpanEntity) -> bool:
        text = span.text.strip()
        if len(text) < 2:
            return False
        if text.lower() in NOISE_ENTITIES:
            return False
        # Consumer software is capitalised exactly like an organisation, and a
        # capitalisation-only NER cannot tell "MS Windows" from "Midea Holdings".
        # The blank pipeline's CRAFT EntityRuler is purely token-pattern based,
        # so it needs the same filter — unless the span carries a hard
        # identifier (registration / IMO / MMSI / flight number / vessel prefix),
        # which can never be a product name.
        has_hard_craft_id = any(
            span.properties.get(key)
            for key in ("registration", "imo_number", "mmsi", "flight_number", "vessel_prefix", "vin")
        )
        if not has_hard_craft_id and _looks_like_product(text):
            return False
        if re.fullmatch(r"[\W\d_]+", text):
            return False
        words = [w for w in re.split(r"\s+", text) if w]
        if len(words) == 1 and words[0].islower():
            return False
        if len(words) == 1 and len(words[0]) <= 2 and span.label != "CRAFT":
            return False
        # Sentence-initial single capitalised tokens are usually not entities.
        if span.label in {"PERSON", "ORG"} and len(words) == 1 and words[0] in {"The", "This", "That", "These", "Those", "However", "Meanwhile", "But", "And", "Also", "Then"}:
            return False
        return True

    def _register_entity(self, registry: dict[str, Entity], span: SpanEntity, document: Document, sentence_index: int) -> Entity:
        entity_type = span.entity_type
        key = canonical_key(span.text, entity_type)
        entity = registry.get(key)
        mention = EntityMention(
            text=span.text,
            entity_type=entity_type,
            start_char=span.start_char,
            end_char=span.end_char,
            sentence_index=sentence_index,
            confidence=span.confidence,
            detector=self.backend if span.label != "CRAFT" else "craft-detector",
            context="",
        )
        if entity is None:
            entity = Entity(
                name=span.text,
                entity_type=entity_type,
                canonical_key=key,
                aliases={span.text},
                mentions=[mention],
                properties=dict(span.properties),
                source_ids={document.source_id},
                doc_ids={document.doc_id},
                confidence=float(span.confidence),
            )
            registry[key] = entity
        else:
            entity.mentions.append(mention)
            entity.aliases.add(span.text)
            entity.source_ids.add(document.source_id)
            entity.doc_ids.add(document.doc_id)
            entity.confidence = max(entity.confidence, float(span.confidence))
            for prop_key, prop_value in span.properties.items():
                entity.properties.setdefault(prop_key, prop_value)
            if len(span.text) > len(entity.name) and entity_type is not EntityType.CRAFT:
                entity.name = span.text
        return entity

    def _reconcile_entity_types(self, registry: dict[str, Entity]) -> None:
        """Collapse one surface form that was registered under two types.

        Type assignment in the degraded backend is heuristic, so "Rosneft sold
        Yugansk to Gazprom" can register Gazprom as a LOCATION (destination
        heuristic) while another sentence registers it as an ORGANIZATION. The
        canonical key embeds the type, so both readings would be written as two
        separate Neo4j nodes for one real-world actor.

        The surviving reading is the one carrying a hard craft identifier, then
        the most confident, then the most specific type. Losers are folded into
        the winner and rewritten in place so relations that already reference
        them resolve to the surviving key.
        """
        groups: dict[str, list[Entity]] = {}
        for entity in registry.values():
            groups.setdefault(normalize_name(entity.name, EntityType.UNKNOWN), []).append(entity)

        for group in groups.values():
            if len({entity.entity_type for entity in group}) < 2:
                continue
            winner = max(
                group,
                key=lambda candidate: (
                    _has_hard_craft_id(candidate),
                    round(float(candidate.confidence), 6),
                    _TYPE_PRIORITY.get(candidate.entity_type, 0),
                    candidate.mention_count,
                ),
            )
            for loser in group:
                if loser is winner:
                    continue
                winner.merge(loser)
                winner.canonical_key = canonical_key(winner.name, winner.entity_type)
                _mirror_entity(loser, winner)
            logger.debug(
                "reconciled %r to %s (%d readings)",
                winner.name, winner.entity_type.value, len(group),
            )

        reconciled: dict[str, Entity] = {}
        for entity in registry.values():
            reconciled.setdefault(entity.canonical_key, entity)
        registry.clear()
        registry.update(reconciled)

    # ------------------------------------------------------------------ #
    # Relation building
    # ------------------------------------------------------------------ #
    def _build_relation(
        self,
        triple: RawTriple,
        registry: dict[str, Entity],
        document: Document,
        source_weight: float,
        method: ExtractionMethod,
        view: SentenceView,
    ) -> Relation | None:
        subject = self._lookup_entity(registry, triple.subject) or self._register_entity(
            registry, triple.subject, document, view.index
        )
        obj = self._lookup_entity(registry, triple.obj) or self._register_entity(
            registry, triple.obj, document, view.index
        )
        if subject is None or obj is None or subject.canonical_key == obj.canonical_key:
            return None
        if triple.negated:
            return None

        decision = self._decide(triple, subject.entity_type, obj.entity_type, view.text)
        if not decision.usable:
            return None

        flip = decision.reverse or triple.passive_flip
        subject_entity, object_entity = (obj, subject) if flip else (subject, obj)
        if subject_entity.canonical_key == object_entity.canonical_key:
            return None

        evidence = self._evidence_score(decision, triple)
        confidence = compose_confidence(source_weight, method, evidence)
        if confidence < float(self.settings.min_edge_confidence):
            return None

        subject_key, object_key, predicate, flipped = self.mapper.canonicalise(
            subject_entity.canonical_key, object_entity.canonical_key, decision.predicate
        )
        if flipped:
            subject_entity, object_entity = object_entity, subject_entity

        return Relation(
            subject=subject_entity,
            predicate=predicate,
            obj=object_entity,
            confidence=confidence,
            method=method,
            source_id=document.source_id,
            doc_id=document.doc_id,
            source_weight=source_weight,
            evidence=(triple.clause_text or view.text)[:1000],
            verb=triple.verb_lemma,
            negated=False,
            sentence_index=view.index,
            subject_span=triple.subject_span if triple.subject_span[0] >= 0 else None,
            obj_span=triple.obj_span if triple.obj_span[0] >= 0 else None,
            extra={
                "evidence_score": evidence,
                "rule": decision.rule_note,
                "matched_on": decision.matched_on,
                "passive": triple.passive,
                "hedged": triple.hedged or decision.hedged,
                "distance": triple.distance,
                "verb_dep": triple.verb_dep,
                "preposition": triple.preposition,
                "surface_subject": subject.name,
                "surface_object": obj.name,
            },
        )

    def _decide(self, triple: RawTriple, subject_type: EntityType, object_type: EntityType, sentence_text: str) -> RelationDecision:
        if triple.verb_dep == "attr" and triple.preposition:
            role = self.mapper.normalize_verb(triple.verb_lemma)
            noun_decision = self.mapper.from_noun(role, subject_type, object_type, sentence=triple.clause_text or sentence_text)
            if noun_decision.matched_on == "noun":
                return noun_decision
            return self.mapper.from_preposition(triple.preposition, subject_type, object_type, sentence=triple.clause_text or sentence_text)
        if triple.verb_dep == "attr":
            return self.mapper.from_noun(self.mapper.normalize_verb(triple.verb_lemma), subject_type, object_type, sentence=triple.clause_text or sentence_text)
        decision = self.mapper.from_verb(
            triple.verb_lemma,
            subject_type,
            object_type,
            sentence=triple.clause_text or sentence_text,
            passive=triple.passive,
            phrasal=triple.phrasal,
            preposition=triple.preposition,
        )
        if decision.matched_on == "type_default" and triple.preposition:
            prep_decision = self.mapper.from_preposition(triple.preposition, subject_type, object_type, sentence=triple.clause_text or sentence_text)
            if prep_decision.matched_on == "preposition":
                return prep_decision
        if decision.matched_on == "type_default":
            noun_decision = self.mapper.from_noun(self.mapper.normalize_verb(triple.verb_lemma), subject_type, object_type, sentence=triple.clause_text or sentence_text)
            if noun_decision.matched_on == "noun":
                return noun_decision
        # A captured directional preposition overrides a generic travel verb:
        # "sailed *from* Fiji" is ARRIVED_FROM, not TRAVELED_TO.
        if triple.preposition and decision.predicate in {
            RelationType.TRAVELED_TO,
            RelationType.TRAVELED_WITH,
            RelationType.ASSOCIATED_WITH,
            RelationType.LOCATED_IN,
        }:
            refined = _PREPOSITION_TRAVEL_MAP.get(triple.preposition)
            if refined is not None and refined is not decision.predicate:
                decision = RelationDecision(
                    predicate=refined,
                    reverse=decision.reverse,
                    evidence=round(decision.evidence * 0.98, 4),
                    rule_note=f"{decision.rule_note}|prep:{triple.preposition}",
                    hedged=decision.hedged,
                    negated=decision.negated,
                    matched_on=decision.matched_on,
                )

        if triple.hedged:
            decision = RelationDecision(
                predicate=decision.predicate,
                reverse=decision.reverse,
                evidence=round(decision.evidence * 0.75, 4),
                rule_note=decision.rule_note,
                hedged=True,
                negated=decision.negated,
                matched_on=decision.matched_on,
            )
        return decision

    def _evidence_score(self, decision: RelationDecision, triple: RawTriple) -> float:
        """Distance + clause-embedding decay on top of the rule's base score."""
        evidence = float(decision.evidence)
        distance = max(0, int(triple.distance))
        if distance > 6:
            evidence *= max(0.6, 1.0 - 0.02 * (distance - 6))
        if triple.verb_dep in EMBEDDED_VERB_DEPS:
            evidence *= 0.9
        if triple.passive:
            evidence *= 0.95
        return round(max(0.05, min(1.0, evidence)), 4)

    @staticmethod
    def _lookup_entity(registry: dict[str, Entity], span: SpanEntity) -> Entity | None:
        if span is None:
            return None
        key = canonical_key(span.text, span.entity_type)
        return registry.get(key)

    # ------------------------------------------------------------------ #
    # Co-occurrence fallback
    # ------------------------------------------------------------------ #
    def _cooccurrence_relations(
        self,
        spans: Sequence[SpanEntity],
        registry: dict[str, Entity],
        document: Document,
        source_weight: float,
        view: SentenceView,
    ) -> Iterator[Relation]:
        """Sentence-level co-occurrence with the mandated 0.2 weight penalty."""
        entities: list[Entity] = []
        seen_keys: set[str] = set()
        for span in spans:
            entity = self._lookup_entity(registry, span)
            if entity is None or entity.canonical_key in seen_keys:
                continue
            seen_keys.add(entity.canonical_key)
            entities.append(entity)

        if len(entities) < 2:
            return

        pairs = list(itertools.combinations(entities, 2))
        # Keep the closest pairs when a sentence is entity-dense.
        if len(pairs) > 12:
            pairs.sort(key=lambda pair: abs(self._span_distance(pair[0], pair[1], spans)))
            pairs = pairs[:12]

        for left, right in pairs:
            subject_key, object_key, predicate, flipped = self.mapper.canonicalise(
                left.canonical_key, right.canonical_key, self.mapper.cooccurrence(left.entity_type, right.entity_type).predicate
            )
            subject, obj = (right, left) if flipped else (left, right)
            decision = self.mapper.cooccurrence(
                subject.entity_type, obj.entity_type, distance=self._span_distance(subject, obj, spans)
            )
            confidence = compose_confidence(source_weight, ExtractionMethod.COOCCURRENCE, decision.evidence)
            if confidence < float(self.settings.min_edge_confidence):
                continue
            yield Relation(
                subject=subject,
                predicate=predicate,
                obj=obj,
                confidence=confidence,
                method=ExtractionMethod.COOCCURRENCE,
                source_id=document.source_id,
                doc_id=document.doc_id,
                source_weight=source_weight,
                evidence=view.text[:1000],
                verb="",
                negated=False,
                sentence_index=view.index,
                extra={
                    "evidence_score": decision.evidence,
                    "rule": decision.rule_note,
                    "matched_on": "cooccurrence",
                    "penalty": round(1.0 - COOCCURRENCE_FACTOR, 4),
                    "passive": False,
                    "hedged": False,
                    "surface_subject": subject.name,
                    "surface_object": obj.name,
                },
            )

    @staticmethod
    def _span_distance(left: Entity, right: Entity, spans: Sequence[SpanEntity]) -> int:
        def offset(entity: Entity) -> int:
            for span in spans:
                if span.text == entity.name or canonical_key(span.text, span.entity_type) == entity.canonical_key:
                    return span.start_char if span.start_char >= 0 else 0
            return 0

        return abs(offset(left) - offset(right)) // 6  # ≈ word distance

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedupe_relations(relations: Sequence[Relation]) -> list[Relation]:
        """Merge repeats of the same edge inside one document (noisy-OR)."""
        from ..models import noisy_or

        best: dict[str, Relation] = {}
        for relation in relations:
            key = relation.signature()
            existing = best.get(key)
            if existing is None:
                best[key] = relation
                continue
            if _is_same_claim(existing, relation):
                # Two readings of one clause (a pattern rule plus the dependency
                # parse, or overlapping patterns) are not independent evidence:
                # noisy-OR would double-count the same sentence.
                existing.confidence = max(existing.confidence, relation.confidence)
            else:
                existing.confidence = noisy_or(existing.confidence, relation.confidence)
            if relation.method is ExtractionMethod.DEPENDENCY and existing.method is not ExtractionMethod.DEPENDENCY:
                existing.method = relation.method
                existing.verb = relation.verb or existing.verb
                existing.extra.update({k: v for k, v in relation.extra.items() if k not in existing.extra})
            if len(relation.evidence) > len(existing.evidence):
                existing.evidence = relation.evidence
        return list(best.values())

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "has_parser": self.has_parser,
            "has_ner": self.has_ner,
            "has_statistical_ner": self.has_statistical_ner,
            "pipes": list(self.nlp.pipe_names) if self.nlp is not None else [],
            "model_candidates": self.model_candidates,
            "load_errors": self.load_errors,
            "mapper": self.mapper.describe(),
            "craft_detection": self.settings.enable_craft_detection,
            "cooccurrence_penalty": round(1.0 - COOCCURRENCE_FACTOR, 4),
        }

    def close(self) -> None:
        self.nlp = None
        self._loaded = False


def craft_matches(text: str, detector: CraftDetector | None = None) -> list[CraftMatch]:
    """Convenience helper used by the CLI and tests."""
    return (detector or CraftDetector()).find_all(text)
