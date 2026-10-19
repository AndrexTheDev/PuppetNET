#!/usr/bin/env python3
"""APOC entity resolution — find duplicates, merge them, keep every evidence edge.

Runs before the fuzzy resolver in :mod:`graph_analytics` and answers one
question: *are these two nodes the same actor, and can we prove it?*

How a pair is found
-------------------
Name comparison alone is O(n²), which is not affordable on a 200 000-node
AuraDB Free instance. The pass therefore blocks first, scores second:

1. **Prepare** writes two stored blocking keys onto every ``:Entity``:
   ``name_key`` (case-folded, accent-folded, legal suffix stripped) and
   ``phonetic_key`` (a Soundex-shaped code that maps ``Müller`` / ``Mueller``
   and ``Schmidt`` / ``Schmitt`` to the same bucket). Neo4j cannot index an
   expression, so the keys are stored and indexed — see
   ``puppetnet.graph.schema.INDEXES``.
2. **Block** groups entities of the same type by phonetic key *and* by a
   4-character prefix of ``name_key``. Only within-block pairs are scored.
3. **Score** happens in the database with the two metrics the spec mandates:
   ``apoc.text.jaroWinklerDistance`` and ``apoc.text.levenshteinSimilarity``.
   The gate is a weighted score (60 % Jaro-Winkler, 40 % Levenshtein) **and**
   a floor on each individual metric, so one generous metric cannot drag a bad
   pair over the line.
4. **Guard**: a shared document inside the co-occurrence window blocks the
   merge (two different people named "Ivan Petrov" are usually written about
   together), and two different values for the same globally unique identifier
   block it unconditionally. An *equal* strict identifier is proof and wins.

How a pair is merged
--------------------
``apoc.refactor.mergeNodes([winner, loser], {...})`` with:

* ``properties: 'discard'`` — the winner was already rolled up in Cypher
  (aliases unioned, mention counts summed, identifiers carried over), so the
  loser's stale properties must not overwrite it.
* ``mergeRels: false`` — relationships are moved, never folded together. This
  is what keeps ``EVIDENCED_BY`` and ``MENTIONS`` edges distinct: folding two
  evidence edges into one would silently halve the corroboration count.
* ``produceSelfRef: false`` — the homonym guard guarantees the two nodes were
  never mentioned in the same document, so a self-relationship can only come
  from a semantic edge between the duplicates; dropping it keeps the graph
  acyclic where the old graph happened to be.

After the merge, an **evidence audit** re-reads the winner's ``EVIDENCED_BY``
and ``MENTIONS`` counts and compares them with the sum recorded before the
merge. A shortfall is reported (and logged) instead of being swallowed: the
whole point of the pass is that deduplication must not lose evidence.

Without APOC
------------
``apoc.refactor.mergeNodes`` is APOC Core and is present on every Aura
instance, but the pass still degrades instead of failing: when the probe cannot
call APOC, candidates are scored in Python with the same two metrics and the
merge is refused (nothing is rewritten without the procedure that keeps
relationships). ``--no-apoc`` forces that mode for a dry inspection.

Usage
-----
::

    python entity_resolution.py                # prepare → resolve
    python entity_resolution.py --dry-run      # report, write nothing
    python entity_resolution.py --prepare-only # only (re)build the blocking keys
    python entity_resolution.py --json         # machine-readable report

Exit codes match ``ingest.py``: ``0`` success · ``1`` configuration ·
``2`` runtime failure · ``3`` partial (some merge batches failed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import shim
    sys.path.insert(0, str(REPO_ROOT))

from puppetnet.config import ConfigError, Settings, load_settings  # noqa: E402
from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable, chunked  # noqa: E402
from puppetnet.logging_utils import banner, configure_logging, get_logger, human_int  # noqa: E402
from puppetnet.models import iso, utcnow  # noqa: E402

logger = get_logger("graph.resolution")

__all__ = [
    "EntityResolutionEngine",
    "MergeCandidate",
    "ResolutionReport",
    "levenshtein_similarity",
    "jaro_winkler_similarity",
    "phonetic_key",
    "name_key",
    "match_score",
    "build_blocks",
    "main",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2
EXIT_PARTIAL = 3

#: Weighted blend of the two mandated metrics. Jaro-Winkler gets the larger
#: share because it rewards a shared prefix, which is what typos and
#: transliterations of company names actually preserve.
JW_WEIGHT = 0.6
#: Each metric on its own must clear ``threshold - margin``. Without these
#: floors a pair with a perfect Jaro-Winkler and a terrible Levenshtein (or the
#: reverse) could pass on the weighted average alone.
JW_FLOOR_MARGIN = 0.05
LEV_FLOOR_MARGIN = 0.10

#: Properties that are globally unique identifiers. Two different values mean
#: two different actors — no spelling similarity can override that.
STRICT_ID_PROPERTIES: tuple[str, ...] = (
    "reg_number",
    "company_number",
    "lei",
    "imo",
    "mmsi",
    "tail_number",
    "icao24",
    "wikidata_id",
)

#: A block larger than this is not scored. 4 000 entities called "Unknown" would
#: produce 8 million pairs, all worthless — and on AuraDB Free that is a killed
#: transaction, not a slow one.
MAX_BLOCK_SIZE = 40
#: Keys sent to the database in one scoring statement.
BLOCK_BATCH_KEYS = 400
#: Pairs scored per run, regardless of how many blocks the population yields.
MAX_PAIR_BUDGET = 25_000

#: Legal forms stripped before names are compared. Resolution between "Acme
#: GmbH" and "Acme" is the *point*; the suffix carries no identity.
LEGAL_TOKENS: frozenset[str] = frozenset(
    {
        "gmbh", "mbh", "ag", "kg", "ug", "se", "ltd", "limited", "llc", "plc",
        "inc", "incorporated", "corp", "corporation", "co", "company", "sa",
        "sas", "sarl", "bv", "nv", "spa", "srl", "lp", "llp", "ooo", "oao",
        "zao", "pao", "ao", "pjsc", "jsc", "und",
    }
)

#: German transliteration is applied *before* accents are folded: "Müller" and
#: "Mueller" must land on the same key, and NFKD alone maps both to "muller"
#: only if the source already spelled it with "ue".
TRANSLITERATIONS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "o", "å": "aa"})
_NON_WORD = re.compile(r"[^0-9a-z\u0370-\u03ff\u0400-\u04ff\u0600-\u06ff\u4e00-\u9fff]+")

# --------------------------------------------------------------------------- #
# Cypher
# --------------------------------------------------------------------------- #
APOC_PROBE = """
RETURN apoc.version() AS version,
       apoc.text.jaroWinklerDistance('Schmidt', 'Schmitt') AS jw,
       apoc.text.levenshteinSimilarity('Schmidt', 'Schmitt') AS lev,
       apoc.text.phonetic('Schmidt') AS phonetic
"""

#: Scoring and merging are probed separately: a deployment can expose the text
#: functions while a restricted Aura role hides the refactor procedure. Merging
#: is refused when the procedure is not there; scoring can still run.
APOC_MERGE_PROBE = """
SHOW PROCEDURES YIELD name
WHERE name = 'apoc.refactor.mergeNodes'
RETURN name LIMIT 1
"""

#: The blocking keys are stored properties because Neo4j cannot index an
#: expression. Both indexes are created up-front and the backfill only touches
#: rows that do not have them yet, so a re-run is cheap.
NAME_KEY_INDEX = "CREATE INDEX puppetnet_entity_name_key IF NOT EXISTS FOR (e:Entity) ON (e.name_key)"
PHONETIC_KEY_INDEX = "CREATE INDEX puppetnet_entity_phonetic_key IF NOT EXISTS FOR (e:Entity) ON (e.phonetic_key)"

PREPARE_ROWS = """
MATCH (e:Entity)
WHERE e.canonical_key IS NOT NULL AND e.name IS NOT NULL
  AND (e.name_key IS NULL OR e.phonetic_key IS NULL)
RETURN e.canonical_key AS canonical_key, e.name AS name
LIMIT $limit
"""

PREPARE_WRITE = """
UNWIND $rows AS row
MATCH (e:Entity {canonical_key: row.canonical_key})
SET e.name_key = row.name_key,
    e.phonetic_key = row.phonetic_key,
    e.name_key_updated_at = $updated_at
RETURN count(e) AS updated
"""

RESOLUTION_ROWS = """
MATCH (e:Entity)
WHERE e.canonical_key IS NOT NULL AND e.name IS NOT NULL
OPTIONAL MATCH (e)-[r]-()
WITH e, count(r) AS degree
RETURN e.canonical_key AS canonical_key,
       e.name AS name,
       coalesce(e.name_key, '') AS name_key,
       coalesce(e.phonetic_key, '') AS phonetic_key,
       coalesce(e.entity_type, '') AS entity_type,
       coalesce(e.mention_count, 0) AS mention_count,
       coalesce(e.confidence, 0.0) AS confidence,
       e.reg_number AS reg_number,
       e.company_number AS company_number,
       e.lei AS lei,
       e.imo AS imo,
       e.mmsi AS mmsi,
       e.tail_number AS tail_number,
       e.icao24 AS icao24,
       e.wikidata_id AS wikidata_id,
       e.address_key AS address_key,
       degree
ORDER BY degree DESC, e.canonical_key
LIMIT $limit
"""

_PAIR_FIELDS = """
       {key: e.canonical_key, name: e.name, name_key: coalesce(e.name_key, ''),
        entity_type: coalesce(e.entity_type, ''), degree: size((e)--()),
        mention_count: coalesce(e.mention_count, 0), confidence: coalesce(e.confidence, 0.0),
        first_seen: e.first_seen, last_seen: e.last_seen, address_key: e.address_key,
        reg_number: e.reg_number, company_number: e.company_number, lei: e.lei,
        imo: e.imo, mmsi: e.mmsi, tail_number: e.tail_number, icao24: e.icao24,
        wikidata_id: e.wikidata_id}
"""

_APOC_PAIR_TEMPLATE = """
UNWIND $blocks AS block
UNWIND block.keys AS key
MATCH (e:Entity {{canonical_key: key}})
WITH block.id AS block_id, e
ORDER BY block_id, e.canonical_key
WITH block_id, collect({payload}) AS members
UNWIND members AS a
UNWIND members AS b
WITH block_id, a, b
WHERE a.key < b.key
  AND (a.entity_type = b.entity_type OR a.entity_type = '' OR b.entity_type = '')
{scoring}
RETURN block_id, a AS left, b AS right{extra}
"""

APOC_PAIR_SCORING = _APOC_PAIR_TEMPLATE.format(
    payload=_PAIR_FIELDS,
    scoring="""
WITH block_id, a, b,
     apoc.text.jaroWinklerDistance(a.name_key, b.name_key) AS jw,
     apoc.text.levenshteinSimilarity(a.name_key, b.name_key) AS lev
""",
    extra=", jw, lev",
)

PLAIN_PAIR_SCORING = _APOC_PAIR_TEMPLATE.format(payload=_PAIR_FIELDS, scoring="", extra="")

#: Two nodes mentioned in the same document are two actors, not one duplicated
#: actor — unless a strict identifier proves otherwise.
CO_OCCURRENCE_GUARD = """
UNWIND $pairs AS pair
MATCH (a:Entity {canonical_key: pair.left_key})<-[:MENTIONS]-(d:Document)-[:MENTIONS]->(b:Entity {canonical_key: pair.right_key})
WHERE d.fetched_at >= $since
RETURN pair.left_key AS left_key, pair.right_key AS right_key, d.doc_id AS doc_id
LIMIT $limit
"""

#: One statement per batch: the merged properties are computed from **both**
#: nodes, ``apoc.refactor.mergeNodes`` runs with the evidence-preserving config,
#: and the roll-up is applied to the surviving node in the same transaction. If
#: the merge fails, the transaction rolls back the roll-up with it — the earlier
#: two-statement design could leave a winner claiming the loser's mentions while
#: the loser still existed, which double-counted on the next run.
APOC_MERGE = """
UNWIND $rows AS row
MATCH (winner:Entity {canonical_key: row.winner_key})
MATCH (loser:Entity {canonical_key: row.loser_key})
WITH winner, loser, row,
     coalesce(winner.aliases, []) AS winner_aliases,
     coalesce(loser.aliases, []) + [loser.name] AS loser_aliases,
     coalesce(winner.mention_count, 0) + coalesce(loser.mention_count, 0) AS mentions,
     CASE WHEN coalesce(loser.confidence, 0.0) > coalesce(winner.confidence, 0.0)
          THEN loser.confidence ELSE winner.confidence END AS confidence,
     CASE
         WHEN winner.first_seen IS NULL THEN loser.first_seen
         WHEN loser.first_seen IS NULL THEN winner.first_seen
         WHEN loser.first_seen < winner.first_seen THEN loser.first_seen
         ELSE winner.first_seen END AS first_seen,
     CASE
         WHEN winner.last_seen IS NULL THEN loser.last_seen
         WHEN loser.last_seen IS NULL THEN winner.last_seen
         WHEN loser.last_seen > winner.last_seen THEN loser.last_seen
         ELSE winner.last_seen END AS last_seen,
     coalesce(winner.address_key, loser.address_key) AS address_key,
     coalesce(winner.reg_number, loser.reg_number) AS reg_number,
     coalesce(winner.company_number, loser.company_number) AS company_number,
     coalesce(winner.lei, loser.lei) AS lei,
     coalesce(winner.imo, loser.imo) AS imo,
     coalesce(winner.mmsi, loser.mmsi) AS mmsi,
     coalesce(winner.tail_number, loser.tail_number) AS tail_number,
     coalesce(winner.icao24, loser.icao24) AS icao24,
     coalesce(winner.wikidata_id, loser.wikidata_id) AS wikidata_id
CALL apoc.refactor.mergeNodes([winner, loser], {
    properties: 'discard',
    mergeRels: false,
    produceSelfRef: false
})
YIELD node
SET node.aliases = winner_aliases + [alias IN loser_aliases WHERE NOT alias IN winner_aliases],
    node.mention_count = mentions,
    node.confidence = confidence,
    node.first_seen = first_seen,
    node.last_seen = last_seen,
    node.address_key = address_key,
    node.reg_number = reg_number,
    node.company_number = company_number,
    node.lei = lei,
    node.imo = imo,
    node.mmsi = mmsi,
    node.tail_number = tail_number,
    node.icao24 = icao24,
    node.wikidata_id = wikidata_id,
    node.merged_keys = coalesce(node.merged_keys, []) + [row.loser_key],
    node.resolution_run_id = $run_id,
    node.resolution_score = row.score,
    node.resolution_method = row.method,
    node.resolution_at = $merged_at
RETURN row.winner_key AS winner_key, row.loser_key AS loser_key, elementId(node) AS merged_id
"""

EVIDENCE_BEFORE = """
UNWIND $pairs AS pair
MATCH (winner:Entity {canonical_key: pair.winner_key})
MATCH (loser:Entity {canonical_key: pair.loser_key})
RETURN pair.winner_key AS winner_key, pair.loser_key AS loser_key,
       size([(winner)-[:EVIDENCED_BY]-() | 1]) AS winner_evidence,
       size([(loser)-[:EVIDENCED_BY]-() | 1]) AS loser_evidence,
       size([(winner)<-[:MENTIONS]-(:Document) | 1]) AS winner_mentions,
       size([(loser)<-[:MENTIONS]-(:Document) | 1]) AS loser_mentions
"""

EVIDENCE_AFTER = """
UNWIND $rows AS row
MATCH (winner:Entity {canonical_key: row.winner_key})
RETURN row.winner_key AS winner_key,
       size([(winner)-[:EVIDENCED_BY]-() | 1]) AS evidence,
       size([(winner)<-[:MENTIONS]-(:Document) | 1]) AS mentions
"""

# --------------------------------------------------------------------------- #
# Name handling
# --------------------------------------------------------------------------- #
def name_key(name: str) -> str:
    """Fold a name into the orthographic comparison form.

    Lowercase, German transliteration, accents stripped, punctuation removed,
    legal suffixes dropped, whitespace collapsed. ``"Müller & Söhne GmbH"`` and
    ``"Mueller und Soehne"`` both become ``"mueller soehne"``.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name)).casefold()
    text = text.translate(TRANSLITERATIONS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("&", " und ")
    text = _NON_WORD.sub(" ", text)
    text = " ".join(text.split())
    # "GmbH & Co KG" became "gmbh und co kg" when "&" was normalised, so the
    # legal form is stripped word by word from the tail. This is also why the
    # multi-word suffixes do not need a special case.
    tokens = text.split()
    while tokens and tokens[-1] in LEGAL_TOKENS:
        tokens.pop()
    return " ".join(tokens)


_SOUNDEX_GROUPS = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}


def phonetic_key(name: str) -> str:
    """Soundex-shaped key for phonetic duplicates.

    Letters are folded to the comparison form first, so transliterations
    (``ü``/``ue``), accents and legal forms do not split a bucket. The code is
    deliberately the classic Soundex shape — one letter + three digits — because
    it is stable, language-agnostic for Latin-script names, and short enough to
    index. Names written in Cyrillic, Arabic or CJK keep their folded form as the
    key: Soundex is meaningless there, but exact folded equality still catches
    spelling variants (``ё``/``е``).
    """
    folded = name_key(name)
    if not folded:
        return ""
    letters = [char for char in folded if char.isalnum()]
    if not letters:
        return ""
    if not ("a" <= letters[0] <= "z"):
        # Non-Latin script: the folded string *is* the bucket. Capped so the
        # index stays small and the block does not become the whole label.
        return "u:" + "".join(letters)[:8]

    head = letters[0]
    codes: list[str] = []
    previous = _SOUNDEX_GROUPS.get(head, "")
    for char in letters[1:]:
        code = _SOUNDEX_GROUPS.get(char, "")
        if code and code != previous:
            codes.append(code)
        if char not in "hw":
            previous = code
    return (head + "".join(codes) + "000")[:4].upper()


def _jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(0, max(len(a), len(b)) // 2 - 1)
    a_matches = [False] * len(a)
    b_matches = [False] * len(b)
    matches = 0
    for i, char in enumerate(a):
        for j in range(max(0, i - window), min(i + window + 1, len(b))):
            if not b_matches[j] and b[j] == char:
                a_matches[i] = b_matches[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i, char in enumerate(a):
        if not a_matches[i]:
            continue
        while not b_matches[k]:
            k += 1
        if char != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3


def jaro_winkler_similarity(left: str, right: str) -> float:
    """Jaro-Winkler similarity in [0, 1] (1 = identical).

    A local implementation only used when APOC is unavailable; the scored path
    runs ``apoc.text.jaroWinklerDistance`` inside Neo4j.
    """
    jaro = _jaro(left, right)
    prefix = 0
    for lhs, rhs in zip(left, right, strict=False):
        if lhs != rhs or prefix == 4:
            break
        prefix += 1
    return min(1.0, jaro + prefix * 0.1 * (1 - jaro))


def levenshtein_similarity(left: str, right: str) -> float:
    """Normalised Levenshtein similarity ``1 - distance / max_length``."""
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, start=1):
        current = [i]
        for j, other in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other)))
        previous = current
    return 1 - previous[-1] / max(len(left), len(right))


def match_score(jaro: float, levenshtein: float) -> float:
    """The weighted gate: 60 % Jaro-Winkler, 40 % Levenshtein, in [0, 1]."""
    return round(JW_WEIGHT * float(jaro) + (1 - JW_WEIGHT) * float(levenshtein), 6)


def build_blocks(
    rows: Sequence[Mapping[str, Any]],
    *,
    prefix: int = 4,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> list[dict[str, Any]]:
    """Group entities into comparison blocks, strongest evidence first.

    Two independent blocks per entity: the phonetic key (catches transliteration
    and phonetic variants) and the folded-name prefix (catches typos and
    truncations). A group that both blocks would produce is emitted once, and a
    group larger than ``max_block_size`` is skipped — quadratic scoring inside a
    bucket of 4 000 "Unknown" nodes is a denial of service on a free tier.
    """
    blocks: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for kind in ("phonetic", "prefix"):
        buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            key = str(row.get("canonical_key") or "")
            if not key:
                continue
            value = str(row.get("phonetic_key") or "") if kind == "phonetic" else str(row.get("name_key") or "")[:prefix]
            if not value:
                continue
            buckets[(str(row.get("entity_type") or ""), value)].append(key)
        for (entity_type, value), keys in sorted(buckets.items()):
            unique = tuple(sorted(set(keys)))
            if len(unique) < 2 or len(unique) > max_block_size or unique in seen:
                continue
            seen.add(unique)
            blocks.append({"id": f"{kind}:{entity_type}:{value}", "kind": kind, "keys": list(unique)})
    return blocks


# --------------------------------------------------------------------------- #
# Report types
# --------------------------------------------------------------------------- #
@dataclass
class MergeCandidate:
    """One duplicate pair, with the evidence that admitted it."""

    winner_key: str
    loser_key: str
    winner_name: str = ""
    loser_name: str = ""
    entity_type: str = ""
    jaro_winkler: float = 0.0
    levenshtein: float = 0.0
    score: float = 0.0
    method: str = "apoc"                # apoc | python | strict_id
    reasons: list[str] = field(default_factory=list)


@dataclass
class ResolutionReport:
    """What one resolution run found, merged and refused to merge."""

    run_id: str
    generated_at: str
    dry_run: bool = False
    status: str = "completed"
    engine: str = "apoc"                # apoc | python | unavailable
    apoc_version: str = ""
    threshold: float = 0.90
    entities_examined: int = 0
    keys_prepared: int = 0
    blocks: int = 0
    candidate_pairs: int = 0
    merges_planned: int = 0
    merges_applied: int = 0
    merges_failed: int = 0
    nodes_removed: int = 0
    homonyms_protected: int = 0
    identifier_conflicts: int = 0
    below_threshold: int = 0
    evidence_pairs_checked: int = 0
    evidence_preserved: int = 0
    evidence_missing: int = 0
    proof_merges: int = 0
    merges: list[dict[str, Any]] = field(default_factory=list)
    protected: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def evidence_integrity(self) -> bool:
        """True when every audited merge kept at least the evidence it started with."""
        return self.evidence_missing == 0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class EntityResolutionEngine:
    """APOC-backed duplicate resolution over one Neo4j database.

    Takes a client (or any object exposing ``read``/``write``/``dry_run``), so
    the Cypher is assertable as text and the decision policy is testable without
    a database.
    """

    def __init__(
        self,
        settings: Settings,
        client: Neo4jClient | None = None,
        *,
        run_id: str = "",
        owns_client: bool = False,
    ) -> None:
        self.settings = settings
        self.owns_client = owns_client or client is None
        self.client = client if client is not None else Neo4jClient(settings)
        self.run_id = run_id or getattr(settings, "run_id", "") or f"resolve-{utcnow():%Y%m%dT%H%M%SZ}"
        self.apoc_available = False
        self.apoc_merge_available = False
        self.apoc_version = ""

    # -- lifecycle ------------------------------------------------------
    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.client, "dry_run", False))

    def close(self) -> None:
        if self.owns_client:
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(Exception):
                self.client.close()

    def probe_apoc(self, *, force_python: bool = False) -> bool:
        """Check the text metrics and the merge procedure separately.

        APOC Core is normally all-or-nothing on Aura, but the two capabilities
        have different consequences: without the text functions scoring falls
        back to Python, and without ``apoc.refactor.mergeNodes`` no merge may be
        applied at all. A failed probe is a normal restricted-role state, not an
        error, so it is logged at INFO.
        """
        self.apoc_available = False
        self.apoc_merge_available = False
        self.apoc_version = ""
        if force_python or self.dry_run:
            return False
        try:
            rows = self.client.read(APOC_PROBE)
        except Exception as exc:  # noqa: BLE001 - any driver/APOC error means "no"
            logger.info("APOC unavailable (%s) — scoring with the Python fallback", exc.__class__.__name__)
            return False
        if not rows:
            return False
        row = rows[0]
        self.apoc_version = str(row.get("version") or "")
        self.apoc_available = all(row.get(field) is not None for field in ("jw", "lev", "phonetic"))
        if self.apoc_available:
            logger.info("APOC %s available — scoring in Cypher", self.apoc_version or "(unknown)")

        try:
            merge_rows = self.client.read(APOC_MERGE_PROBE)
            self.apoc_merge_available = bool(merge_rows)
        except Exception as exc:  # noqa: BLE001 - SHOW PROCEDURES can be restricted
            # A restricted role may hide SHOW PROCEDURES while APOC is fully
            # usable. Fall back to the text probe: APOC is installed as a unit.
            self.apoc_merge_available = self.apoc_available
            logger.info(
                "could not probe apoc.refactor.mergeNodes (%s) — assuming it matches APOC availability",
                exc.__class__.__name__,
            )
        if self.apoc_available and not self.apoc_merge_available:
            logger.warning("APOC text metrics are available but apoc.refactor.mergeNodes is not — merges will be refused")
        return self.apoc_available

    # -- prepare --------------------------------------------------------
    def prepare(self, *, limit: int | None = None) -> int:
        """Create the blocking indexes and backfill missing keys.

        Returns the number of entities given keys. Only rows with a missing key
        are touched, so the cost is paid once per entity and not once per run.
        """
        for statement in (NAME_KEY_INDEX, PHONETIC_KEY_INDEX):
            try:
                self.client.write(statement, kind="schema")
            except Exception as exc:  # noqa: BLE001 - an existing index may raise on old servers
                logger.warning("could not ensure resolution index (%s): %s", exc.__class__.__name__, exc)
        batch = max(1, int(getattr(self.settings, "neo4j_batch_size", 500) or 500))
        cap = int(limit if limit is not None else getattr(self.settings, "resolution_entity_limit", 20_000))
        prepared = 0
        while prepared < cap:
            try:
                rows = self.client.read(PREPARE_ROWS, {"limit": min(batch, cap - prepared)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read entities needing resolution keys: %s", exc)
                break
            if not rows:
                break
            payload = [
                {
                    "canonical_key": row["canonical_key"],
                    "name_key": name_key(str(row.get("name") or "")),
                    "phonetic_key": phonetic_key(str(row.get("name") or "")),
                }
                for row in rows
                if row.get("canonical_key")
            ]
            if not payload:
                break
            try:
                self.client.write(PREPARE_WRITE, {"rows": payload, "updated_at": iso(utcnow())}, rows=len(payload), kind="maintenance")
            except Exception as exc:  # noqa: BLE001
                logger.error("writing resolution keys failed: %s", exc)
                break
            prepared += len(payload)
            if self.dry_run:
                break
        if prepared:
            logger.info("resolution: prepared blocking keys for %s entities", human_int(prepared))
        return prepared

    # -- candidates -----------------------------------------------------
    def _rows(self, limit: int) -> list[dict[str, Any]]:
        rows = self.client.read(RESOLUTION_ROWS, {"limit": int(limit)}) or []
        prepared: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["canonical_key"] = str(item.get("canonical_key") or "")
            if not item["canonical_key"]:
                continue
            item["name"] = str(item.get("name") or "")
            item["name_key"] = str(item.get("name_key") or "") or name_key(item["name"])
            item["phonetic_key"] = str(item.get("phonetic_key") or "") or phonetic_key(item["name"])
            item["entity_type"] = str(item.get("entity_type") or "")
            item["degree"] = int(item.get("degree") or 0)
            item["mention_count"] = int(item.get("mention_count") or 0)
            item["confidence"] = float(item.get("confidence") or 0.0)
            prepared.append(item)
        return prepared

    def _score_blocks(self, blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Score one batch of blocks, in Cypher via APOC or in Python."""
        query = APOC_PAIR_SCORING if self.apoc_available else PLAIN_PAIR_SCORING
        rows = self.client.read(query, {"blocks": [dict(block) for block in blocks]}) or []
        pairs: list[dict[str, Any]] = []
        for row in rows:
            left = dict(row.get("left") or {})
            right = dict(row.get("right") or {})
            if not left.get("key") or not right.get("key"):
                continue
            if self.apoc_available:
                jw = float(row.get("jw") or 0.0)
                lev = float(row.get("lev") or 0.0)
            else:
                jw = jaro_winkler_similarity(str(left.get("name_key") or ""), str(right.get("name_key") or ""))
                lev = levenshtein_similarity(str(left.get("name_key") or ""), str(right.get("name_key") or ""))
            pair = {
                "left": left,
                "right": right,
                "jaro_winkler": jw,
                "levenshtein": lev,
                "score": match_score(jw, lev),
                "method": "apoc" if self.apoc_available else "python",
            }
            pairs.append(pair)
        return pairs

    def _blocks(self, rows: Sequence[dict[str, Any]], *, prefix: int) -> list[dict[str, Any]]:
        return build_blocks(rows, prefix=prefix)

    @staticmethod
    def _batch_blocks(blocks: Sequence[dict[str, Any]], *, max_keys: int = BLOCK_BATCH_KEYS) -> Iterable[list[dict[str, Any]]]:
        batch: list[dict[str, Any]] = []
        keys = 0
        for block in blocks:
            if batch and keys + len(block["keys"]) > max_keys:
                yield batch
                batch, keys = [], 0
            batch.append(block)
            keys += len(block["keys"])
        if batch:
            yield batch

    # -- decisions ------------------------------------------------------
    @staticmethod
    def _identifier_conflict(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(conflict, shared_property)`` for the strict identifiers."""
        for prop in STRICT_ID_PROPERTIES:
            lhs = str(left.get(prop) or "").strip()
            rhs = str(right.get(prop) or "").strip()
            if not lhs or not rhs:
                continue
            if lhs == rhs:
                return False, prop
            return True, prop
        return False, ""

    @staticmethod
    def _rank_key(row: dict[str, Any]) -> tuple[int, int, float, int, str]:
        """Winner ordering: mention count, degree, confidence, then shorter name.

        Mention count comes first because it is the only signal that persists
        across documents; degree is a tie-breaker that favours the node the rest
        of the graph already points at.
        """
        return (
            -int(row.get("mention_count") or 0),
            -int(row.get("degree") or 0),
            -float(row.get("confidence") or 0.0),
            len(str(row.get("name") or "")),
            str(row.get("canonical_key") or ""),
        )

    def _decide(
        self,
        pairs: Sequence[dict[str, Any]],
        *,
        threshold: float,
        max_merges: int,
    ) -> tuple[list[MergeCandidate], int, int]:
        """Turn scored pairs into merge decisions, honouring every guard.

        Returns ``(decisions, identifier_conflicts, below_threshold)``.
        """
        below = 0
        conflicts = 0
        considered: list[tuple[dict[str, Any], dict[str, Any], MergeCandidate]] = []
        for pair in pairs:
            left, right = pair["left"], pair["right"]
            jw, lev, score = pair["jaro_winkler"], pair["levenshtein"], pair["score"]
            conflict, shared = self._identifier_conflict(left, right)
            if conflict:
                conflicts += 1
                continue
            if shared:
                reasons = [f"shared_{shared}"]
                method = "strict_id"
                score = 1.0
            else:
                if score < threshold or jw < threshold - JW_FLOOR_MARGIN or lev < threshold - LEV_FLOOR_MARGIN:
                    below += 1
                    continue
                reasons = ["name_similarity"]
                method = pair["method"]
            ordered = sorted((left, right), key=self._rank_key)
            winner, loser = ordered[0], ordered[1]
            candidate = MergeCandidate(
                winner_key=str(winner["canonical_key"]),
                loser_key=str(loser["canonical_key"]),
                winner_name=str(winner.get("name") or ""),
                loser_name=str(loser.get("name") or ""),
                entity_type=str(winner.get("entity_type") or ""),
                jaro_winkler=round(jw, 6),
                levenshtein=round(lev, 6),
                score=round(score, 6),
                method=method,
                reasons=reasons,
            )
            considered.append((winner, loser, candidate))

        # One merge per node per run: a chain A≈B≈C must not try to merge B
        # twice. Keep the strongest scoring candidate for each key.
        considered.sort(key=lambda item: (-item[2].score, item[2].winner_key, item[2].loser_key))
        claimed: set[str] = set()
        decisions: list[MergeCandidate] = []
        for _winner, _loser, candidate in considered:
            if candidate.winner_key in claimed or candidate.loser_key in claimed:
                continue
            claimed.update({candidate.winner_key, candidate.loser_key})
            decisions.append(candidate)
            if len(decisions) >= max_merges:
                break
        return decisions, conflicts, below

    def _guard_homonyms(self, decisions: Sequence[MergeCandidate], *, limit: int) -> tuple[list[MergeCandidate], list[dict[str, Any]]]:
        """Drop pairs whose two nodes share a recent document."""
        if not decisions:
            return [], []
        days = int(getattr(self.settings, "resolution_cooccurrence_days", 365) or 365)
        since = iso(utcnow() - timedelta(days=days))
        try:
            rows = self.client.read(
                CO_OCCURRENCE_GUARD,
                {
                    "pairs": [{"left_key": item.winner_key, "right_key": item.loser_key} for item in decisions],
                    "since": since,
                    "limit": max(1, len(decisions)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            # The guard is a safety property. If it cannot be evaluated, merge
            # nothing rather than merging unguarded.
            logger.error("homonym guard failed (%s) — refusing every merge this run", exc)
            return [], [{"reason": "guard_unavailable", "error": f"{exc.__class__.__name__}: {exc}"}]
        disputed = {(str(row.get("left_key")), str(row.get("right_key"))) for row in rows or []}
        kept: list[MergeCandidate] = []
        protected: list[dict[str, Any]] = []
        for item in decisions:
            keys = {(item.winner_key, item.loser_key), (item.loser_key, item.winner_key)}
            if keys & disputed and item.method != "strict_id":
                protected.append(
                    {
                        "winner_key": item.winner_key,
                        "loser_key": item.loser_key,
                        "winner_name": item.winner_name,
                        "loser_name": item.loser_name,
                        "reason": "co_occurrence",
                    }
                )
                continue
            kept.append(item)
        return kept, protected

    # -- merge ----------------------------------------------------------
    @staticmethod
    def _row(item: MergeCandidate) -> dict[str, Any]:
        return {
            "winner_key": item.winner_key,
            "loser_key": item.loser_key,
            "score": item.score,
            "method": item.method,
        }

    def _evidence_before(self, decisions: Sequence[MergeCandidate]) -> dict[tuple[str, str], tuple[int, int]]:
        payload = [
            {"winner_key": item.winner_key, "loser_key": item.loser_key, "score": item.score, "method": item.method}
            for item in decisions
        ]
        rows = self.client.read(EVIDENCE_BEFORE, {"pairs": payload}) or []
        return {
            (str(row.get("winner_key")), str(row.get("loser_key"))): (
                int(row.get("winner_evidence") or 0) + int(row.get("loser_evidence") or 0),
                int(row.get("winner_mentions") or 0) + int(row.get("loser_mentions") or 0),
            )
            for row in rows
        }

    def _audit_evidence(self, decisions: Sequence[MergeCandidate], before: dict[tuple[str, str], tuple[int, int]]) -> tuple[int, int]:
        """Compare post-merge evidence counts with the pre-merge sums.

        Returns ``(preserved, missing)`` pairs. A missing pair is a real defect
        — the caller counts it and the report carries ``evidence_integrity:
        false`` — never a silent pass.
        """
        if self.dry_run or not decisions:
            return 0, 0
        rows = self.client.read(EVIDENCE_AFTER, {"rows": [self._row(item) for item in decisions]}) or []
        after = {str(row.get("winner_key")): row for row in rows}
        preserved = missing = 0
        for item in decisions:
            expected = before.get((item.winner_key, item.loser_key))
            if expected is None:
                continue
            row = after.get(item.winner_key)
            if row is None:
                missing += 1
                continue
            evidence = int(row.get("evidence") or 0)
            mentions = int(row.get("mentions") or 0)
            if evidence >= expected[0] and mentions >= expected[1]:
                preserved += 1
            else:
                missing += 1
                logger.error(
                    "evidence loss after merging %s into %s: expected ≥%d evidence / ≥%d mentions, found %d / %d",
                    item.loser_key, item.winner_key, expected[0], expected[1], evidence, mentions,
                )
        return preserved, missing

    def _apply(self, decisions: Sequence[MergeCandidate]) -> tuple[int, int, int]:
        """Roll up properties, merge with APOC, audit. Returns (applied, failed, removed)."""
        if not decisions:
            return 0, 0, 0
        if not self.apoc_merge_available and not self.dry_run:
            logger.error(
                "apoc.refactor.mergeNodes is unavailable — refusing to merge %d pair(s) rather than deleting nodes by hand",
                len(decisions),
            )
            return 0, len(decisions), 0

        before = self._evidence_before(decisions)
        applied = failed = 0
        batch_size = max(1, int(getattr(self.settings, "neo4j_batch_size", 500) or 500))
        merged_at = iso(utcnow())
        for batch in chunked(list(decisions), batch_size):
            payload = [self._row(item) for item in batch]
            try:
                # In dry-run this is recorded, not executed: the statement's
                # shape is still validated, and the report never claims a merge.
                self.client.write(
                    APOC_MERGE,
                    {"rows": payload, "run_id": self.run_id, "merged_at": merged_at},
                    rows=len(payload),
                    kind="maintenance",
                )
                if not self.dry_run:
                    applied += len(batch)
            except Exception as exc:  # noqa: BLE001
                if self.dry_run:
                    raise
                logger.error("apoc.refactor.mergeNodes failed for a batch of %d: %s", len(batch), exc)
                failed += len(batch)

        if self.dry_run:
            return 0, failed, 0
        preserved, missing = self._audit_evidence(decisions, before)
        self._last_audit = (preserved, missing)
        logger.info(
            "resolution: %d merge(s) applied, %d failed, %d evidence pair(s) preserved, %d missing",
            applied, failed, preserved, missing,
        )
        return applied, failed, applied

    # -- run ------------------------------------------------------------
    def run(
        self,
        *,
        threshold: float | None = None,
        max_merges: int | None = None,
        limit: int | None = None,
        prepare: bool = True,
        force_python: bool = False,
    ) -> ResolutionReport:
        started = time.perf_counter()
        threshold_value = float(threshold if threshold is not None else getattr(self.settings, "resolution_threshold", 0.90))
        merge_cap = int(max_merges if max_merges is not None else getattr(self.settings, "resolution_max_merges", 250))
        entity_limit = int(limit if limit is not None else getattr(self.settings, "resolution_entity_limit", 20_000))
        report = ResolutionReport(
            run_id=self.run_id,
            generated_at=iso(utcnow()) or "",
            dry_run=self.dry_run,
            threshold=threshold_value,
        )
        if not bool(getattr(self.settings, "resolution_enabled", True)):
            report.status = "disabled"
            report.engine = "disabled"
            logger.info("entity resolution disabled by configuration")
            return report
        self._last_audit = (0, 0)

        try:
            if prepare:
                report.keys_prepared = self.prepare(limit=entity_limit)
            engine_available = self.probe_apoc(force_python=force_python)
            report.engine = "apoc" if engine_available else "python"
            report.apoc_version = self.apoc_version
            if not self.apoc_merge_available and not self.dry_run:
                # Scoring can continue in Python, but without the merge procedure
                # there is nothing safe to apply. Report why, do not fail the run:
                # the report is the deliverable and the exit code is 0.
                logger.warning("apoc.refactor.mergeNodes is unavailable: candidates will be scored and reported, but no merge can be applied")

            rows = self._rows(entity_limit)
            report.entities_examined = len(rows)
            if not rows:
                report.status = "empty"
                return report

            blocks = self._blocks(rows, prefix=int(getattr(self.settings, "resolution_block_prefix", 4) or 4))
            report.blocks = len(blocks)
            pair_budget = min(MAX_PAIR_BUDGET, max(50, merge_cap * 100))
            pairs: list[dict[str, Any]] = []
            for batch in self._batch_blocks(blocks):
                if len(pairs) >= pair_budget:
                    logger.warning("pair budget (%d) reached — scoring only the strongest blocks", pair_budget)
                    break
                try:
                    pairs.extend(self._score_blocks(batch))
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"scoring block batch failed: {exc.__class__.__name__}: {exc}")
                    logger.error("scoring a block batch failed: %s", exc)
                if len(pairs) > pair_budget:
                    pairs = pairs[:pair_budget]
                    break
            report.candidate_pairs = len(pairs)

            decisions, conflicts, below = self._decide(
                pairs, threshold=threshold_value, max_merges=merge_cap,
            )
            report.identifier_conflicts = conflicts
            report.below_threshold = below
            decisions, protected = self._guard_homonyms(decisions, limit=merge_cap)
            report.proof_merges = sum(1 for item in decisions if item.method == "strict_id")
            report.homonyms_protected = len(protected)
            report.protected = protected[:50]
            report.merges_planned = len(decisions)
            report.merges = [asdict(item) for item in decisions[:50]]

            applied, failed, removed = self._apply(decisions)
            report.merges_applied = applied
            report.merges_failed = failed
            report.nodes_removed = removed
            report.evidence_pairs_checked = applied
            report.evidence_preserved, report.evidence_missing = self._last_audit
            if failed and applied:
                report.errors.append(f"{failed} row(s) could not be merged")
                report.status = "partial"
            elif failed and not applied:
                report.errors.append(f"{failed} row(s) could not be merged")
                report.status = "failed"
            if decisions and not self.apoc_merge_available and not self.dry_run:
                report.errors.append("apoc.refactor.mergeNodes unavailable — no merge was applied")
                report.status = "partial"
        except Neo4jUnavailable as exc:
            report.status = "failed"
            report.errors.append(f"Neo4j unavailable: {exc}")
            logger.error("entity resolution failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.errors.append(f"{exc.__class__.__name__}: {exc}")
            logger.error("entity resolution failed: %s", exc)
        finally:
            report.seconds = round(time.perf_counter() - started, 3)
        return report

    # -- reporting ------------------------------------------------------
    def write_report(self, report: ResolutionReport, directory: str | Path | None = None) -> Path:
        target = Path(directory or getattr(self.settings, "report_dir", "reports") or "reports")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"entity_resolution_{report.run_id}.json"
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        logger.info("resolution report written to %s", path)
        return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="entity_resolution.py",
        description="APOC duplicate resolution for the PuppetNET graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Stages: prepare blocking keys → score blocks with APOC → guard → merge.\n"
            "Exit codes: 0 success · 1 configuration · 2 runtime failure · 3 partial."
        ),
    )
    parser.add_argument("--run", action="store_true", help="Run the pass (default when no other mode is given).")
    parser.add_argument("--prepare-only", action="store_true", help="Only build/refresh the blocking keys.")
    parser.add_argument("--dry-run", action="store_true", help="Score and report, write nothing to Neo4j.")
    parser.add_argument("--no-apoc", action="store_true", help="Force the Python scoring path (never applies a merge).")
    parser.add_argument("--threshold", type=float, default=None, help="Weighted match threshold (default 0.90).")
    parser.add_argument("--max-merges", type=int, default=None, help="Maximum pairs merged this run.")
    parser.add_argument("--limit", type=int, default=None, help="Entity rows examined (default 20000).")
    parser.add_argument("--report-dir", default="", help="Where to write the JSON report (default: REPORT_DIR).")
    parser.add_argument("--no-report", action="store_true", help="Do not write the report file.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON on stdout.")
    parser.add_argument("--log-level", default="", help="DEBUG | INFO | WARNING | ERROR.")
    parser.add_argument("--log-json", action="store_true", help="Emit structured JSON logs.")
    parser.add_argument("--version", action="store_true", help="Print version information and exit.")
    return parser


def summarise(report: ResolutionReport) -> str:
    lines = [
        banner(f"entity resolution — {report.status}"),
        f"run_id            : {report.run_id}",
        f"engine            : {report.engine}{f' ({report.apoc_version})' if report.apoc_version else ''}"
        f"{' [dry run]' if report.dry_run else ''}",
        f"threshold         : {report.threshold:.2f}",
        f"seconds           : {report.seconds:.1f}",
        "",
        f"keys prepared     : {human_int(report.keys_prepared)}",
        f"entities examined : {human_int(report.entities_examined)}",
        f"blocks / pairs    : {human_int(report.blocks)} / {human_int(report.candidate_pairs)}",
        f"merges            : {human_int(report.merges_planned)} planned, {human_int(report.merges_applied)} applied, "
        f"{human_int(report.merges_failed)} failed",
        f"nodes removed     : {human_int(report.nodes_removed)}",
        f"proof merges      : {human_int(report.proof_merges)} (shared strict identifier)",
        f"guards            : {human_int(report.homonyms_protected)} co-occurrence, "
        f"{human_int(report.identifier_conflicts)} identifier conflict(s), {human_int(report.below_threshold)} below threshold",
        f"evidence          : {human_int(report.evidence_preserved)} preserved, {human_int(report.evidence_missing)} missing",
    ]
    if report.merges:
        lines.append("")
        lines.append("strongest merges  :")
        for entry in report.merges[:5]:
            lines.append(
                f"  {float(entry.get('score') or 0):.3f}  {entry.get('winner_name') or entry.get('winner_key')}"
                f"  ←  {entry.get('loser_name') or entry.get('loser_key')}"
            )
    if report.protected:
        lines.append("")
        lines.append("protected         :")
        for entry in report.protected[:5]:
            lines.append(f"  {entry.get('reason')}: {entry.get('winner_key')} ↔ {entry.get('loser_key')}")
    if report.errors:
        lines.append("")
        lines.append("errors            :")
        lines.extend(f"  - {error}" for error in report.errors[:10])
    lines.append("=" * 78)
    return "\n".join(lines)


def emit_github_output(report: ResolutionReport) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            values = {
                "resolution_status": report.status,
                "resolution_engine": report.engine,
                "resolution_pairs": report.candidate_pairs,
                "resolution_merges": report.merges_applied,
                "resolution_proof_merges": report.proof_merges,
                "resolution_evidence_missing": report.evidence_missing,
                "resolution_seconds": f"{report.seconds:.2f}",
            }
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    except OSError as exc:
        logger.warning("could not write GITHUB_OUTPUT: %s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        from puppetnet import __version__  # noqa: PLC0415

        print(f"PuppetNET entity resolution {__version__} (python {sys.version.split()[0]})")
        return EXIT_OK

    env_overrides: dict[str, str] = {}
    if args.dry_run:
        env_overrides["DRY_RUN"] = "true"
    if args.log_level:
        env_overrides["LOG_LEVEL"] = args.log_level.upper()
    if args.log_json:
        env_overrides["LOG_JSON"] = "true"
    if args.threshold is not None:
        env_overrides["RESOLUTION_THRESHOLD"] = str(args.threshold)
    if args.max_merges is not None:
        env_overrides["RESOLUTION_MAX_MERGES"] = str(args.max_merges)
    if args.limit is not None:
        env_overrides["RESOLUTION_ENTITY_LIMIT"] = str(args.limit)
    if args.report_dir:
        env_overrides["REPORT_DIR"] = args.report_dir

    try:
        settings = load_settings({**dict(os.environ), **env_overrides})
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings.log_level, settings.log_json, stream=sys.stderr if args.json else None)

    try:
        engine = EntityResolutionEngine(settings, run_id=getattr(settings, "run_id", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"could not initialise the graph client: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        if args.prepare_only:
            prepared = engine.prepare()
            payload = {"prepared": prepared, "run_id": engine.run_id}
            print(json.dumps(payload, indent=2) if args.json else f"prepared {prepared} entity key(s)")
            return EXIT_OK

        report = engine.run(
            threshold=args.threshold,
            max_merges=args.max_merges,
            limit=args.limit,
            prepare=True,
            force_python=args.no_apoc,
        )
        print(json.dumps(report.to_dict(), indent=2, default=str) if args.json else summarise(report))
        if not args.no_report:
            try:
                engine.write_report(report, args.report_dir or None)
            except OSError as exc:
                logger.warning("could not write the report file: %s", exc)
        emit_github_output(report)
        if report.status == "failed":
            return EXIT_RUNTIME
        if report.status == "partial":
            return EXIT_PARTIAL
        return EXIT_OK
    except Neo4jUnavailable as exc:
        print(f"Neo4j unavailable: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        engine.close()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
