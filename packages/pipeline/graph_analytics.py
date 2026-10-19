#!/usr/bin/env python3
"""PuppetNET graph maintenance — deduplication, capacity pruning, centrality.

Runs *after* :mod:`ingest` has written the day's harvest and answers four
questions a property graph cannot answer by itself:

1. **Are these two nodes the same actor?**  Entity resolution with a homonym
   guard: strict identifier hashes merge unconditionally, fuzzy name matches
   (Jaro-Winkler / Levenshtein ≥ ``0.88``) merge only when a contextual
   attribute corroborates them — a shared registration number, a shared
   normalised address, or a co-occurrence inside a 50-word window in the same
   document. "Ivan Petrov" appears in every leak dataset on earth; merging two
   of them because the spelling matched is how a graph starts lying.
2. **Are we about to hit the ceiling?**  AuraDB Free stops accepting writes at
   200 000 nodes / 400 000 relationships. Orphans — degree ≤ 1, weakest edge
   weight < 0.3, untouched for > 180 days — are purged, and if the database is
   still above ``PRUNE_CAPACITY_TARGET`` of the limit the thresholds escalate
   automatically rather than letting the next harvest fail.
3. **Who holds this network together?**  Betweenness centrality (Neo4j GDS when
   the instance has it, a pure-Python Brandes implementation otherwise) plus a
   24 h degree spike and an offshore-cluster ratio, combined into the mandated
   anomaly score::

       score = w1 * betweenness + w2 * degree_spike_24h + w3 * offshore_ratio

4. **Did something new connect two worlds?**  A node that appeared inside the
   alert window, sits on an articulation point, and joins two clusters that were
   separate in the previous run is a *bridge*. Those are written to the report
   that :mod:`telegram_bot` turns into a push alert.

Usage
-----
::

    python graph_analytics.py --all                 # dedupe → prune → centrality
    python graph_analytics.py --dedupe --max-merges 50
    python graph_analytics.py --prune --dry-run     # list orphans, delete nothing
    python graph_analytics.py --centrality --top 10
    python graph_analytics.py --all --json          # machine-readable summary

Exit codes match ``ingest.py``: ``0`` success · ``1`` configuration ·
``2`` runtime failure · ``3`` partial (some stage failed, others completed).

Every stage is **idempotent** and safe to re-run: merges are computed as
equivalence classes so a chain ``A→B→C`` collapses to one winner, calculated
properties are overwritten rather than accumulated, and a stale calculated layer
is pruned. Nothing here can delete a node that still carries evidence —
orphans are only purged when their single edge is weak *and* nobody has looked
at them for six months.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import shim
    sys.path.insert(0, str(REPO_ROOT))

from puppetnet.config import ConfigError, Settings, load_settings  # noqa: E402
from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable, chunked  # noqa: E402
from puppetnet.logging_utils import banner, configure_logging, get_logger, human_int, timed  # noqa: E402
from puppetnet.models import is_safe_relationship_type  # noqa: E402

logger = get_logger("graph.maintenance")

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2
EXIT_PARTIAL = 3

# --------------------------------------------------------------------------- #
# Capacity limits — AuraDB Free
# --------------------------------------------------------------------------- #
#: Hard node ceiling of the free tier. Writes are rejected by the server above it.
AURA_NODE_LIMIT = 200_000
#: Hard relationship ceiling of the free tier.
AURA_EDGE_LIMIT = 400_000
#: Fraction of the ceiling at which pruning starts escalating its thresholds.
CAPACITY_TARGET_DEFAULT = 0.85

# --------------------------------------------------------------------------- #
# Deduplication constants
# --------------------------------------------------------------------------- #
#: Jaro-Winkler / Levenshtein confidence above which two names may be the same
#: actor. Below the mandated 0.88 a fuzzy match is never even considered.
FUZZY_THRESHOLD = 0.88
#: At or above this similarity a *distinctive* (non-generic, non-common) name pair
#: is treated as certain. It changes no decision — the 0.88 gate already admitted
#: the pair — it is recorded in the merge's reasons so a reviewer can tell a
#: near-exact match from a borderline one.
NON_GENERIC_CERTAIN_SIMILARITY = 0.97
#: Two mentions inside this many words count as a co-occurrence. The graph stores
#: character offsets, so the window is converted at ~6 characters per word.
CO_OCCURRENCE_WINDOW_WORDS = 50
CO_OCCURRENCE_WINDOW_CHARS = CO_OCCURRENCE_WINDOW_WORDS * 6
#: A name key shared by more nodes than this is treated as *common*: it needs
#: contextual corroboration even at similarity 1.0.
COMMON_NAME_NODE_COUNT = 6
#: Identifier properties that are globally unique by construction. Two nodes
#: agreeing on one of these are the same actor, whatever their names look like.
STRICT_ID_PROPERTIES: tuple[str, ...] = (
    "reg_number",
    "company_number",
    "wikidata_id",
    "wikipedia_id",
    "lei",
    "imo",
    "mmsi",
    "tail_number",
    "transponder",
    "icao24",
    "opencorporates_url",
)
#: Corporate suffixes, placeholder names and titles that carry no identity. A
#: name made only of these is never merged on spelling alone.
GENERIC_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "ltd", "limited", "llc", "llp", "plc", "inc", "incorporated", "corp", "corporation",
        "company", "co", "gmbh", "ag", "sa", "sarl", "sas", "srl", "spa", "bv", "nv", "as",
        "ab", "oy", "asa", "pty", "pvt", "private", "public", "holdings", "holding", "group",
        "trust", "foundation", "fund", "charity", "trustee", "trustees", "nominee", "nominees",
        "offshore", "international", "global", "general", "trading", "investments", "investment",
        "capital", "assets", "asset", "ventures", "enterprise", "enterprises", "services",
        "service", "consulting", "management", "development", "industries",
        "industry", "resources", "resource", "logistics", "shipping", "marine", "air",
        "unknown", "unavailable", "not available", "n a", "na", "none", "null", "various",
        "multiple", "redacted", "confidential", "anonymous", "unnamed", "other", "others",
        "mr", "mrs", "ms", "miss", "dr", "prof", "sir", "lord", "lady", "sheikh", "haji",
        "president", "director", "owner", "shareholder", "beneficiary", "officer", "manager",
        "secretary", "agent", "representative", "intermediary", "lawyer", "accountant",
        "person", "people", "human", "individual", "entity", "organisation", "organization",
        "bank", "ministry", "government", "state", "federal", "national", "republic",
    }
)
#: Single-token surnames that are common enough to be ambiguous on their own.
AMBIGUOUS_SINGLE_TOKENS: frozenset[str] = frozenset(
    {
        "smith", "jones", "brown", "williams", "taylor", "davies", "evans", "thomas", "roberts",
        "johnson", "walker", "wright", "robinson", "thompson", "white", "hughes", "green",
        "hall", "lewis", "harris", "clarke", "patel", "khan", "singh", "kumar", "ali",
        "mohammed", "mohamed", "ahmed", "hassan", "hussein", "ibrahim", "abdullah", "rahman",
        "kim", "lee", "park", "chen", "wang", "li", "zhang", "liu", "huang", "wu", "yang",
        "ivanov", "petrov", "sidorov", "smirnov", "kuznetsov", "popov", "vasilyev", "sokolov",
        "muller", "müller", "schmidt", "schneider", "fischer", "weber", "meyer", "wagner",
        "becker", "schulz", "hoffmann", "koch", "bauer", "richter", "klein", "wolf", "neumann",
        "garcia", "martinez", "lopez", "gonzalez", "hernandez", "perez", "sanchez", "ramirez",
        "rossi", "russo", "ferrari", "esposito", "bianchi", "romano", "colombo", "ricci",
        "silva", "santos", "oliveira", "souza", "pereira", "costa", "rodrigues", "almeida",
        "yilmaz", "kaya", "demir", "celik", "sahin", "aydin", "ozdemir", "arslan",
    }
)

# --------------------------------------------------------------------------- #
# Pruning constants (all overridable through Settings)
# --------------------------------------------------------------------------- #
ORPHAN_MAX_DEGREE = 1
ORPHAN_MAX_WEIGHT = 0.3
ORPHAN_MIN_AGE_DAYS = 180
#: Escalated thresholds used when the database is above the capacity target.
ORPHAN_ESCALATED = {"max_degree": 2, "max_weight": 0.4, "min_age_days": 90}
#: Never purge a node whose influence score reaches this, however lonely it is.
PRUNE_PROTECT_RISK_SCORE = 0.40

# --------------------------------------------------------------------------- #
# Anomaly scoring
# --------------------------------------------------------------------------- #
#: Score = w1*betweenness + w2*degree_spike + w3*offshore_cluster_ratio.
ANOMALY_WEIGHTS: dict[str, float] = {
    "betweenness": 0.40,
    "degree_spike": 0.35,
    "offshore_ratio": 0.25,
}
#: A degree that grew by this factor saturates the spike component at 1.0.
DEGREE_SPIKE_SATURATION = 3.0
#: Window in which the previous degree snapshot still counts as "24 h ago".
DEGREE_SPIKE_WINDOW_HOURS = 30
#: Bridge alerts kept per run. The strongest ones by anomaly score win; the rest
#: are simply reported by the next run, which is the right behaviour when a
#: maintenance pass finds a hundred new bridges at once.
BRIDGE_ALERT_LIMIT = 10
#: Labels / properties that make a neighbour count as offshore for the ratio.
OFFSHORE_LABELS: frozenset[str] = frozenset({"Offshore", "ShellCompany"})
OFFSHORE_JURISDICTION_CLASSES: frozenset[str] = frozenset({"secrecy", "opacity"})

# --------------------------------------------------------------------------- #
# Cypher — capacity
# --------------------------------------------------------------------------- #

CAPACITY_COUNTS = """
CALL {
  MATCH (e:Entity)
  RETURN count(e) AS entities
}
CALL {
  MATCH ()-[r]->()
  WHERE type(r) <> 'HANDLED' AND type(r) <> 'PROCESSED' AND type(r) <> 'FROM_SOURCE'
    AND type(r) <> 'MENTIONS'
  RETURN count(r) AS semantic_edges
}
CALL {
  MATCH ()-[r]->()
  RETURN count(r) AS all_edges
}
CALL {
  MATCH (n)
  RETURN count(n) AS all_nodes
}
CALL {
  MATCH (e:Entity)
  WHERE e.is_shell = true OR 'ShellCompany' IN labels(e) OR 'Offshore' IN labels(e)
  RETURN count(e) AS offshore_nodes
}
RETURN entities, semantic_edges, all_edges, all_nodes, offshore_nodes
"""

# --------------------------------------------------------------------------- #
# Cypher — entity resolution
# --------------------------------------------------------------------------- #

#: Everything a merge decision can depend on, in one read. Ordered by the
#: blocking key so the client streams neighbours together.
ENTITY_ROWS_FOR_DEDUP = """
MATCH (e:Entity)
WHERE e.canonical_key IS NOT NULL
WITH e
ORDER BY e.canonical_key
LIMIT $limit
RETURN e.canonical_key AS canonical_key,
       e.name AS name,
       e.entity_type AS entity_type,
       e.aliases AS aliases,
       e.mention_count AS mention_count,
       e.confidence AS confidence,
       e.first_seen AS first_seen,
       e.last_seen AS last_seen,
       e.reg_number AS reg_number,
       e.company_number AS company_number,
       e.wikidata_id AS wikidata_id,
       e.wikipedia_id AS wikipedia_id,
       e.lei AS lei,
       e.imo AS imo,
       e.mmsi AS mmsi,
       e.tail_number AS tail_number,
       e.transponder AS transponder,
       e.icao24 AS icao24,
       e.opencorporates_url AS opencorporates_url,
       e.address_key AS address_key,
       e.jurisdiction AS jurisdiction,
       e.jurisdiction_class AS jurisdiction_class,
       e.risk_score AS risk_score,
       labels(e) AS labels,
       size((e)--()) AS degree
"""

#: Nodes agreeing on one globally unique identifier. Grouped server-side so a
#: 200k-node database does not have to be shipped to the runner.
STRICT_ID_GROUPS = """
MATCH (e:Entity)
WHERE e.%s IS NOT NULL AND e.%s <> ''
WITH e.%s AS id_value, collect({key: e.canonical_key, name: e.name,
                                entity_type: e.entity_type, degree: size((e)--()),
                                mention_count: coalesce(e.mention_count, 0),
                                confidence: coalesce(e.confidence, 0),
                                first_seen: e.first_seen}) AS members
WHERE size(members) > 1
RETURN id_value AS id_value, members AS members
LIMIT $limit
"""

#: Two entities mentioned inside one 50-word window of the same document. The
#: graph stores character offsets on `MENTIONS`, so the window is expressed in
#: characters (~6 per word) — an honest approximation of "within 50 words" that
#: costs one indexed scan instead of re-reading every article.
CO_OCCURRENCE_PAIRS = """
MATCH (d:Document)-[m1:MENTIONS]->(a:Entity)
MATCH (d)-[m2:MENTIONS]->(b:Entity)
WHERE a.canonical_key < b.canonical_key
  AND m1.first_offset IS NOT NULL AND m2.first_offset IS NOT NULL
  AND abs(m1.first_offset - m2.first_offset) <= $window_chars
  AND d.fetched_at >= $since
RETURN a.canonical_key AS subject_key, b.canonical_key AS object_key,
       d.doc_id AS doc_id, d.url AS url
LIMIT $limit
"""

#: Nodes sharing a normalised address — the second contextual attribute the
#: homonym rule accepts.
SHARED_ADDRESS_PAIRS = """
MATCH (a:Entity), (b:Entity)
WHERE a.address_key IS NOT NULL AND a.address_key = b.address_key
  AND a.canonical_key < b.canonical_key
RETURN a.canonical_key AS subject_key, b.canonical_key AS object_key,
       a.address_key AS address_key
LIMIT $limit
"""

#: The relationship types actually attached to a set of losers, so the merge
#: only repoints what exists instead of iterating all 46 predicates.
LOSER_RELATIONSHIP_TYPES = """
UNWIND $loser_keys AS loser_key
MATCH (loser:Entity {canonical_key: loser_key})-[r]-(other)
WHERE NOT other:Document AND NOT other:IngestRun AND NOT other:Source
RETURN DISTINCT type(r) AS predicate
"""

#: Documents mentioning a loser are re-pointed at the winner.
REPOINT_MENTIONS = """
UNWIND $rows AS row
MATCH (loser:Entity {canonical_key: row.loser_key})
MATCH (winner:Entity {canonical_key: row.winner_key})
MATCH (d:Document)-[m:MENTIONS]->(loser)
WITH d, winner, loser, m, properties(m) AS props
MERGE (d)-[nm:MENTIONS]->(winner)
ON CREATE SET nm = props
ON MATCH SET nm.count = coalesce(nm.count, 0) + coalesce(props.count, 1),
             nm.confidence = 1 - (1 - coalesce(nm.confidence, 0)) * (1 - coalesce(props.confidence, 0)),
             nm.first_offset = CASE
                 WHEN nm.first_offset IS NULL THEN props.first_offset
                 WHEN props.first_offset IS NULL THEN nm.first_offset
                 ELSE CASE WHEN props.first_offset < nm.first_offset THEN props.first_offset ELSE nm.first_offset END
               END,
             nm.first_seen = CASE
                 WHEN coalesce(props.first_seen, '') < coalesce(nm.first_seen, '9999')
                 THEN props.first_seen ELSE nm.first_seen END,
             nm.last_seen = CASE
                 WHEN coalesce(props.last_seen, '') > coalesce(nm.last_seen, '')
                 THEN props.last_seen ELSE nm.last_seen END,
             nm.surface_forms = (coalesce(nm.surface_forms, []) +
                                 [s IN coalesce(props.surface_forms, [])
                                  WHERE NOT s IN coalesce(nm.surface_forms, [])])[..8]
DELETE m
RETURN count(nm) AS repointed
"""

#: Outgoing semantic edges move from the loser to the winner. The relationship
#: type is interpolated, so it is validated against the closed vocabulary first.
REPOINT_OUTGOING = """
UNWIND $rows AS row
MATCH (loser:Entity {{canonical_key: row.loser_key}})
MATCH (winner:Entity {{canonical_key: row.winner_key}})
MATCH (loser)-[r:`{predicate}`]->(other:Entity)
WHERE other.canonical_key <> row.winner_key
WITH winner, other, r, properties(r) AS props
MERGE (winner)-[nr:`{predicate}`]->(other)
ON CREATE SET nr = props
ON MATCH SET nr.observations = coalesce(nr.observations, 0) + coalesce(props.observations, 1),
             nr.confidence = 1 - (1 - coalesce(nr.confidence, 0)) * (1 - coalesce(props.confidence, 0)),
             nr.source_weight = CASE
                 WHEN coalesce(props.source_weight, 0) > coalesce(nr.source_weight, 0)
                 THEN props.source_weight ELSE nr.source_weight END,
             nr.weight = CASE
                 WHEN coalesce(props.weight, 0) > coalesce(nr.weight, 0)
                 THEN props.weight ELSE nr.weight END,
             nr.method = CASE
                 WHEN coalesce(props.method, '') IN ['structured', 'dependency'] THEN props.method
                 ELSE coalesce(nr.method, props.method) END,
             nr.evidence = (coalesce(nr.evidence, []) +
                            [e IN coalesce(props.evidence, [])
                             WHERE NOT e IN coalesce(nr.evidence, [])])[..5],
             nr.first_seen = CASE
                 WHEN coalesce(props.first_seen, '') < coalesce(nr.first_seen, '9999')
                 THEN props.first_seen ELSE nr.first_seen END,
             nr.last_seen = CASE
                 WHEN coalesce(props.last_seen, '') > coalesce(nr.last_seen, '')
                 THEN props.last_seen ELSE nr.last_seen END
DELETE r
RETURN count(nr) AS repointed
"""

#: Incoming semantic edges, same merge arithmetic, opposite direction.
REPOINT_INCOMING = """
UNWIND $rows AS row
MATCH (loser:Entity {{canonical_key: row.loser_key}})
MATCH (winner:Entity {{canonical_key: row.winner_key}})
MATCH (other:Entity)-[r:`{predicate}`]->(loser)
WHERE other.canonical_key <> row.winner_key
WITH winner, other, r, properties(r) AS props
MERGE (other)-[nr:`{predicate}`]->(winner)
ON CREATE SET nr = props
ON MATCH SET nr.observations = coalesce(nr.observations, 0) + coalesce(props.observations, 1),
             nr.confidence = 1 - (1 - coalesce(nr.confidence, 0)) * (1 - coalesce(props.confidence, 0)),
             nr.source_weight = CASE
                 WHEN coalesce(props.source_weight, 0) > coalesce(nr.source_weight, 0)
                 THEN props.source_weight ELSE nr.source_weight END,
             nr.weight = CASE
                 WHEN coalesce(props.weight, 0) > coalesce(nr.weight, 0)
                 THEN props.weight ELSE nr.weight END,
             nr.evidence = (coalesce(nr.evidence, []) +
                            [e IN coalesce(props.evidence, [])
                             WHERE NOT e IN coalesce(nr.evidence, [])])[..5],
             nr.first_seen = CASE
                 WHEN coalesce(props.first_seen, '') < coalesce(nr.first_seen, '9999')
                 THEN props.first_seen ELSE nr.first_seen END,
             nr.last_seen = CASE
                 WHEN coalesce(props.last_seen, '') > coalesce(nr.last_seen, '')
                 THEN props.last_seen ELSE nr.last_seen END
DELETE r
RETURN count(nr) AS repointed
"""

#: The winner absorbs the loser's identity evidence: aliases, sources, documents,
#: mention counts, the higher confidence and the earlier first sighting. A
#: `merged_from` audit trail keeps the discarded keys resolvable forever.
MERGE_ENTITY_PROPERTIES = """
UNWIND $rows AS row
MATCH (loser:Entity {canonical_key: row.loser_key})
MATCH (winner:Entity {canonical_key: row.winner_key})
SET winner.aliases = (coalesce(winner.aliases, []) +
                      [a IN (coalesce(loser.aliases, []) + [loser.name])
                       WHERE a IS NOT NULL AND a <> '' AND NOT a IN coalesce(winner.aliases, [])])[..64],
    winner.mention_count = coalesce(winner.mention_count, 0) + coalesce(loser.mention_count, 0),
    winner.confidence = 1 - (1 - coalesce(winner.confidence, 0)) * (1 - coalesce(loser.confidence, 0)),
    winner.source_ids = (coalesce(winner.source_ids, []) +
                         [s IN coalesce(loser.source_ids, [])
                          WHERE NOT s IN coalesce(winner.source_ids, [])])[..32],
    winner.doc_ids = (coalesce(winner.doc_ids, []) +
                      [d IN coalesce(loser.doc_ids, [])
                       WHERE NOT d IN coalesce(winner.doc_ids, [])])[..64],
    winner.first_seen = CASE
        WHEN loser.first_seen IS NULL THEN winner.first_seen
        WHEN winner.first_seen IS NULL THEN loser.first_seen
        WHEN loser.first_seen < winner.first_seen THEN loser.first_seen
        ELSE winner.first_seen END,
    winner.last_seen = CASE
        WHEN loser.last_seen IS NULL THEN winner.last_seen
        WHEN winner.last_seen IS NULL THEN loser.last_seen
        WHEN loser.last_seen > winner.last_seen THEN loser.last_seen
        ELSE winner.last_seen END,
    winner.merged_from = (coalesce(winner.merged_from, []) + [loser.canonical_key])[..32],
    winner.merged_at = $merged_at,
    winner.risk_score = CASE
        WHEN coalesce(loser.risk_score, 0) > coalesce(winner.risk_score, 0)
        THEN loser.risk_score ELSE winner.risk_score END
RETURN winner.canonical_key AS winner_key, loser.canonical_key AS loser_key
"""

#: Anything still attached to a loser after the known vocabulary was repointed.
#: This should always be empty; a non-zero count is logged as a warning because
#: it means the graph carries a relationship type this module does not know.
LOSER_LEFTOVER_RELATIONSHIPS = """
UNWIND $rows AS row
MATCH (loser:Entity {canonical_key: row.loser_key})-[r]-(other)
WHERE NOT other:Document AND NOT other:IngestRun AND NOT other:Source
RETURN row.loser_key AS loser_key, type(r) AS predicate, count(r) AS edges
"""

#: The loser disappears once every edge and mention has been moved.
DELETE_MERGED_ENTITY = """
UNWIND $rows AS row
MATCH (loser:Entity {canonical_key: row.loser_key})
DETACH DELETE loser
RETURN row.loser_key AS deleted
"""

# --------------------------------------------------------------------------- #
# Cypher — pruning
# --------------------------------------------------------------------------- #

#: Orphan candidates: lonely, weakly attached, and untouched for months.
#: Structural relationships (Document/Source/IngestRun) are excluded from the
#: degree count so a node mentioned by exactly one document is still an orphan
#: candidate — mentions are bookkeeping, not evidence of connection.
ORPHAN_CANDIDATES = """
MATCH (e:Entity)
WHERE e.last_seen IS NOT NULL AND e.last_seen < $cutoff
WITH e, [(e)-[r]-(other:Entity) | coalesce(r.weight, 0)] AS semantic_weights,
     size([(e)-[r]-(other:Entity) | r]) AS semantic_degree
WHERE semantic_degree <= $max_degree
  AND (semantic_degree = 0 OR
       reduce(lowest = 1.0, w IN semantic_weights | CASE WHEN w < lowest THEN w ELSE lowest END) < $max_weight)
RETURN e.canonical_key AS canonical_key, e.name AS name, labels(e) AS labels,
       e.entity_type AS entity_type, e.last_seen AS last_seen, e.first_seen AS first_seen,
       e.risk_score AS risk_score, e.shell_risk AS shell_risk,
       e.source_ids AS source_ids, e.mention_count AS mention_count,
       semantic_degree AS degree, semantic_weights AS weights
ORDER BY semantic_degree ASC, e.last_seen ASC
LIMIT $limit
"""

#: Batched purge. `DETACH DELETE` because the single weak edge goes with it.
#:
#: The count is aggregated *before* the delete: `DETACH DELETE e RETURN count(e)`
#: is valid Cypher that always answers 0, because the variable it counts no
#: longer exists by the time the projection runs.
PURGE_ORPHANS = """
UNWIND $rows AS row
MATCH (e:Entity {canonical_key: row.canonical_key})
WITH collect(e) AS nodes, count(e) AS purged
UNWIND nodes AS node
DETACH DELETE node
RETURN purged
LIMIT 1
"""

#: Second-order cleanup after a purge: documents that no longer mention anything
#: and runs that no longer processed anything are pure bookkeeping weight.
PURGE_DETACHED_DOCUMENTS = """
MATCH (d:Document)
WHERE NOT (d)-[:MENTIONS]->() AND d.fetched_at < $cutoff
WITH collect(d) AS nodes, count(d) AS purged
UNWIND nodes AS node
DETACH DELETE node
RETURN purged
LIMIT 1
"""

# --------------------------------------------------------------------------- #
# Cypher — centrality / clustering / anomaly scoring
# --------------------------------------------------------------------------- #

#: The projected graph: every semantic edge with the properties the score needs.
CENTRALITY_EDGES = """
MATCH (a:Entity)-[r]->(b:Entity)
WHERE r.confidence >= $min_confidence
RETURN a.canonical_key AS subject_key, b.canonical_key AS object_key,
       type(r) AS predicate, coalesce(r.weight, 0.3) AS weight,
       coalesce(r.confidence, 0) AS confidence,
       coalesce(r.observations, 1) AS observations
LIMIT $limit
"""

#: Node attributes for the projection, restricted to the nodes that matter.
CENTRALITY_NODES = """
UNWIND $keys AS key
MATCH (e:Entity {canonical_key: key})
RETURN e.canonical_key AS canonical_key, e.name AS name, e.entity_type AS entity_type,
       labels(e) AS labels, e.jurisdiction_class AS jurisdiction_class,
       e.jurisdiction AS jurisdiction, e.is_shell AS is_shell,
       e.shell_risk AS shell_risk, e.risk_score AS risk_score,
       e.first_seen AS first_seen, e.last_seen AS last_seen,
       e.cluster_id AS cluster_prev, e.degree AS degree_prev,
       e.metrics_at AS metrics_at, size([(e)-[r]-(o:Entity) | r]) AS degree
"""

#: Write the calculated layer back onto the nodes. `degree` becomes next run's
#: `degree_prev`, which is what makes the 24 h spike measurable at all.
WRITE_NODE_METRICS = """
UNWIND $rows AS row
MATCH (e:Entity {canonical_key: row.canonical_key})
SET e.betweenness = row.betweenness,
    e.degree = row.degree,
    e.degree_prev = row.degree_prev,
    e.degree_spike = row.degree_spike,
    e.offshore_neighbour_ratio = row.offshore_neighbour_ratio,
    e.offshore_cluster_ratio = row.offshore_cluster_ratio,
    e.anomaly_score = row.anomaly_score,
    e.anomaly_betweenness = row.anomaly_betweenness,
    e.anomaly_degree_spike = row.anomaly_degree_spike,
    e.anomaly_offshore_cluster_ratio = row.anomaly_offshore_cluster_ratio,
    e.anomaly_offshore_neighbour_ratio = row.anomaly_offshore_neighbour_ratio,
    e.anomaly_reasons = row.anomaly_reasons,
    e.cluster_id = row.cluster_id,
    e.cluster_size = row.cluster_size,
    e.is_bridge_candidate = row.is_bridge_candidate,
    e.metrics_at = row.metrics_at,
    e.metrics_run_id = row.run_id
RETURN count(e) AS scored
"""

#: Highest anomaly scores inside the alert window — the daily digest source.
TOP_ANOMALIES = """
MATCH (e:Entity)
WHERE e.anomaly_score IS NOT NULL AND e.metrics_at >= $since
RETURN e.canonical_key AS canonical_key, e.name AS name, e.entity_type AS entity_type,
       labels(e) AS labels, e.anomaly_score AS anomaly_score,
       e.anomaly_betweenness AS anomaly_betweenness,
       e.anomaly_degree_spike AS anomaly_degree_spike,
       e.anomaly_offshore_cluster_ratio AS anomaly_offshore_cluster_ratio,
       e.anomaly_offshore_neighbour_ratio AS anomaly_offshore_neighbour_ratio,
       e.anomaly_reasons AS anomaly_reasons,
       e.betweenness AS betweenness, e.degree AS degree, e.degree_prev AS degree_prev,
       e.degree_spike AS degree_spike, e.offshore_cluster_ratio AS offshore_cluster_ratio,
       e.risk_score AS risk_score, e.jurisdiction_class AS jurisdiction_class,
       e.cluster_id AS cluster_id, e.cluster_size AS cluster_size, e.metrics_at AS metrics_at
ORDER BY e.anomaly_score DESC, e.betweenness DESC
LIMIT $limit
"""

#: Nodes that appeared inside the window — one half of the bridge test.
NEW_ENTITIES = """
MATCH (e:Entity)
WHERE e.first_seen IS NOT NULL AND e.first_seen >= $since
RETURN e.canonical_key AS canonical_key, e.name AS name, labels(e) AS labels,
       e.entity_type AS entity_type, e.first_seen AS first_seen,
       e.source_ids AS source_ids, size([(e)-[r]-(o:Entity) | r]) AS degree
LIMIT $limit
"""

#: Calculated properties go stale; drop anything not refreshed recently.
PRUNE_STALE_METRICS = """
MATCH (e:Entity)
WHERE e.metrics_at IS NOT NULL AND e.metrics_at < $cutoff
REMOVE e.anomaly_score, e.betweenness, e.degree_spike, e.anomaly_betweenness,
       e.anomaly_degree_spike, e.anomaly_offshore_cluster_ratio,
       e.anomaly_offshore_neighbour_ratio, e.anomaly_reasons, e.is_bridge_candidate
RETURN count(e) AS cleared
"""


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    """Timezone-aware UTC now — every timestamp this module writes is ISO-8601 Z."""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    """Tolerant ISO-8601 parse; ``None`` when the value is missing or junk."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(str(value).strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


#: Typographic characters NFKD leaves alone. Offshore-leak CSVs and Wikidata
#: labels are full of them, and one stray curly quote is enough to make two
#: spellings of the same name fold differently — which silently splits an
#: equivalence class in two.
_TYPOGRAPHIC_TABLE = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2039": "'", "\u203a": "'", "\u00ab": '"', "\u00bb": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2011": "-",
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u202f": " ",
    "\u2028": " ", "\u2029": " ", "\u200b": "",
    "\uff06": "&", "\uff20": "@",
})
#: Quotes and brackets carry no identity and differ between sources.
_DECORATIVE_RE = re.compile(r"""["'`()\[\]{}]""")


def fold_name(name: str) -> str:
    """Accent-folded, case-folded, punctuation-normalised comparison form.

    Deliberately close to ``puppetnet.graph.resolver._fold``: the resolver built
    the canonical keys this module deduplicates, and a name that folds one way
    there and another way here would split an equivalence class in two.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(_TYPOGRAPHIC_TABLE)
    text = text.replace("&", " and ").replace("@", " at ")
    text = _DECORATIVE_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def name_tokens(name: str) -> tuple[str, ...]:
    """Alphanumeric tokens of a folded name, generic suffixes removed."""
    folded = fold_name(name)
    if not folded:
        return ()
    raw = [t for t in _NON_ALNUM_RE.split(folded) if t]
    kept = [t for t in raw if t not in GENERIC_NAME_TOKENS]
    return tuple(kept or raw)


def is_generic_name(name: str) -> bool:
    """True when the name is *only* corporate noise or a placeholder."""
    folded = fold_name(name)
    if not folded:
        return True
    tokens = [t for t in _NON_ALNUM_RE.split(folded) if t]
    if not tokens:
        return True
    if all(t in GENERIC_NAME_TOKENS for t in tokens):
        return True
    return folded in GENERIC_NAME_TOKENS


#: Frequent given names. Together with :data:`AMBIGUOUS_SINGLE_TOKENS` (surnames)
#: and :data:`GENERIC_NAME_TOKENS` (corporate noise) these decide whether a name
#: carries any identifying information at all: "John Smith" is two of the most
#: common tokens in the English-speaking world and must never merge on spelling
#: alone, however high its similarity to another "John Smith".
COMMON_GIVEN_NAMES: frozenset[str] = frozenset(
    {
        # English / Germanic
        "john", "james", "robert", "michael", "william", "david", "richard", "charles",
        "thomas", "christopher", "daniel", "matthew", "anthony", "mark", "steven",
        "paul", "andrew", "joshua", "kevin", "brian", "george", "edward", "peter",
        "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "jessica",
        "sarah", "karen", "nancy", "lisa", "margaret", "betty", "sandra", "ashley",
        "hans", "klaus", "wolfgang", "stefan", "andreas", "martin",
        "anna", "marie", "maria", "elisabeth", "katrin", "andrea", "petra", "sabine",
        # Romance
        "jose", "josé", "juan", "carlos", "luis", "miguel", "jesus", "francisco",
        "manuel", "antonio", "pedro", "jorge", "fernando", "alejandro", "rafael",
        "maría", "carmen", "rosa", "isabel", "lucia", "lucía", "pilar",
        "joao", "joão", "paulo", "ricardo", "bruno", "tiago", "ana", "claudia",
        "jean", "pierre", "michel", "alain", "philippe", "nicolas", "julien",
        "jeanne", "sophie", "camille", "isabelle", "natalie", "emilie",
        # Slavic
        "ivan", "aleksandr", "alexander", "dmitry", "sergey", "sergei", "andrey",
        "andrei", "vladimir", "nikolay", "yuri", "oleg", "igor", "mikhail", "pavel",
        "olga", "elena", "tatiana", "natalia", "irina", "svetlana", "marina", "petr", "jiri", "jan", "karel", "milan", "zofia", "agnieszka",
        # Arabic / Persian / Turkish
        "mohamed", "mohammed", "muhammad", "ahmed", "ahmad", "ali", "hassan", "hussein",
        "omar", "khalid", "yousef", "yusuf", "ibrahim", "abdul", "abdullah", "saeed",
        "fatima", "fatimah", "aisha", "layla", "leila", "maryam", "zahra", "noor",
        "mehmet", "mustafa", "murat", "emre", "ayse", "fatma", "zeynep", "elif",
        # South / East / Southeast Asia
        "raj", "rajesh", "amit", "sanjay", "vikram", "arun", "suresh", "vijay",
        "priya", "anita", "sunita", "deepa", "kavita", "pooja", "lakshmi",
        "wei", "ming", "jun", "lei", "hao", "xin", "yan", "liang", "fang", "ying",
        "min", "ji", "hyun", "soo", "yeong", "tae", "jiyoung", "minjun", "seojun",
        "hiroshi", "takashi", "kenji", "shin", "yuki", "haruki", "sota", "ren",
        "yui", "sakura", "akiko", "naoko", "mai", "aoi",
        "nguyen", "minh", "hung", "linh", "trang", "huong", "thanh",
        # African
        "kwame", "kofi", "chinedu", "emeka", "ade", "ademola", "olusegun", "bola",
        "abubakar", "musu", "grace", "joy", "faith", "mercy", "esther", "ruth",
    }
)


#: Nobiliary and patronymic particles. They carry no identity at all — "van",
#: "de" and "bin" attach to whatever follows — but they do keep a name from being
#: recognised as common if they are not accounted for ("Nguyen Van Minh" is three
#: of the most frequent tokens in Vietnam).
NAME_PARTICLES: frozenset[str] = frozenset(
    {
        "van", "von", "der", "den", "ter", "ten", "op", "de", "del", "della", "di",
        "da", "das", "dos", "du", "la", "le", "los", "y", "e", "i",
        "bin", "binti", "ibn", "ben", "al", "el", "abu", "abd", "oul",
        "st", "saint", "sainte", "san", "santa", "santo", "mac", "mc", "o", "ap",
        "af", "av", "nic", "ni", "fitz", "ha", "bte", "binte",
    }
)


def is_common_name(name: str, *, graph_count: int = 0) -> bool:
    """True when a name carries no identifying information on its own.

    Two independent tests, because either alone misses real cases:

    * **Graph frequency** — more than :data:`COMMON_NAME_NODE_COUNT` nodes share
      the folded name, so the name cannot pick one of them out.
    * **Lexical commonness** — *every* identity token is a common given name, a
      common surname or corporate noise. "John Smith" has six nodes in one graph
      and six million in the world; the lexical test catches it on the first
      occurrence, before the graph is large enough for the frequency test.

    A name with even one distinctive token ("Kastelion Overseas Ltd",
    "Gazprom") is *not* common: the distinctive token is what identifies it.
    """
    if graph_count > COMMON_NAME_NODE_COUNT:
        return True
    tokens = name_tokens(name)
    if not tokens:
        return True
    common_tokens = (
        AMBIGUOUS_SINGLE_TOKENS | GENERIC_NAME_TOKENS | COMMON_GIVEN_NAMES | NAME_PARTICLES
    )
    return all(token in common_tokens for token in tokens)


def block_keys(name: str, entity_type: str) -> tuple[str, ...]:
    """Blocking keys for candidate generation.

    Comparing every pair of 200 000 nodes is 2×10¹⁰ string comparisons, so
    candidates are generated inside *blocks* that any two spellings of one name
    must share:

    * ``t:<type>:<first 3 folded characters>`` — catches typos and truncations;
    * ``s:<type>:<sorted token initials>`` — catches word-order variants
      (``OJSC Rosneft`` / ``Rosneft OJSC``);
    * ``n:<type>:<sorted tokens>`` — catches inserted middle names and suffixes.
    """
    folded = fold_name(name)
    if not folded:
        return ()
    etype = (entity_type or "unknown").lower()
    tokens = name_tokens(name)
    keys = [f"t:{etype}:{folded[:3]}"]
    if tokens:
        keys.append("s:{}:{}".format(etype, "".join(sorted(t[0] for t in tokens))))
        keys.append("n:{}:{}".format(etype, "|".join(sorted(tokens))))
    return tuple(dict.fromkeys(keys))


# --------------------------------------------------------------------------- #
# String similarity — Jaro, Jaro-Winkler and Levenshtein, dependency-free
# --------------------------------------------------------------------------- #
def levenshtein_distance(left: str, right: str) -> int:
    """Classic edit distance with two rolling rows (O(min(len)) memory)."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) < len(right):
        left, right = right, left

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current[j] = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
        previous = current
    return previous[len(right)]


def levenshtein_ratio(left: str, right: str) -> float:
    """``1 - distance / max(len)`` — 1.0 identical, 0.0 nothing in common.

    Two empty strings are mathematically identical but must never count as a
    name match: an entity with no name is a data defect, not a duplicate.
    """
    if not left or not right:
        return 0.0
    longest = max(len(left), len(right))
    if longest == 0:
        return 1.0
    return max(0.0, 1.0 - levenshtein_distance(left, right) / longest)


def jaro_similarity(left: str, right: str) -> float:
    """Jaro similarity: transposition-aware, the metric Winkler builds on.

    ``0.0`` when either side is empty — see :func:`levenshtein_ratio`.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    match_window = max(len(left), len(right)) // 2 - 1
    if match_window < 0:
        match_window = 0

    left_flags = [False] * len(left)
    right_flags = [False] * len(right)
    matches = 0

    for i, left_char in enumerate(left):
        start = max(0, i - match_window)
        end = min(i + match_window + 1, len(right))
        for j in range(start, end):
            if right_flags[j] or right[j] != left_char:
                continue
            left_flags[i] = True
            right_flags[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i, flagged in enumerate(left_flags):
        if not flagged:
            continue
        while not right_flags[k]:
            k += 1
        if left[i] != right[k]:
            transpositions += 1
        k += 1

    return (
        matches / len(left)
        + matches / len(right)
        + (matches - transpositions / 2) / matches
    ) / 3.0


def jaro_winkler(left: str, right: str, *, prefix_scale: float = 0.1) -> float:
    """Jaro-Winkler: rewards a shared prefix, which is what name variants do.

    The prefix bonus is capped at four characters (Winkler's original bound),
    so ``Jon``/``Jonathan`` cannot be pushed to 1.0 by a long shared head alone.
    """
    jaro = jaro_similarity(left, right)
    if jaro <= 0.7:
        # Winkler's boost only applies above the 0.7 "reasonable match" floor;
        # below it the bonus amplifies noise.
        return jaro
    prefix = 0
    for left_char, right_char in zip(left[:4], right[:4], strict=False):
        if left_char != right_char:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1 - jaro)


def name_similarity(left: str, right: str) -> float:
    """The score the 0.88 threshold is applied to.

    ``max(jaro_winkler, levenshtein_ratio)`` over the folded forms: Jaro-Winkler
    handles transposed and abbreviated names, Levenshtein handles substitutions
    and dropped characters. Taking the max keeps recall without lowering the
    bar — both must be genuinely close for the value to clear 0.88.
    """
    a, b = fold_name(left), fold_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(jaro_winkler(a, b), levenshtein_ratio(a, b))


#: A token-level match also needs *some* string resemblance, so two completely
#: different names that happen to share tokens ("Ivan Ivanov" / "Ivan Petrov")
#: cannot ride the token rule into a merge.
TOKEN_MATCH_STRING_FLOOR = 0.60
#: When *every* identity token is present in both names the pair is a reorder or
#: patronymic variant, and a low string score is expected — "Ivan Petrov" vs
#: "Petrov Ivan" is 0.52 on Jaro-Winkler — so the floor drops. Such pairs are
#: still forced through the homonym guard (see `_decide`).
TOKEN_REORDER_STRING_FLOOR = 0.35
#: Tokens shorter than this carry no identity ("o", "de", "llc").
MIN_IDENTITY_TOKEN_LENGTH = 4
#: A token shared by more nodes than this is a surname like "Kim": generating
#: pairs inside it is quadratic noise, so the token index skips it.
TOKEN_INDEX_CAP = 60
#: Hard ceiling on pairwise string comparisons per run.
MAX_PAIR_COMPARISONS = 250_000


def token_similarity(left: str, right: str) -> float:
    """Containment over identity tokens, with fuzzy token matching.

    ``|shared| / min(|left|, |right|)`` where two tokens count as shared when
    they are identical or Jaro-Winkler ≥ 0.92 (``petrov``/``petrow``,
    ``muller``/``mueller``). Containment rather than Jaccard because a full name
    legitimately *contains* a short one: "Usmanov Alisher Burkhanovich" and
    "Alisher Usmanov" are the same person, and Jaccard would punish the
    patronymic that made the record more precise.
    """
    left_tokens = [t for t in name_tokens(left) if len(t) >= MIN_IDENTITY_TOKEN_LENGTH]
    right_tokens = [t for t in name_tokens(right) if len(t) >= MIN_IDENTITY_TOKEN_LENGTH]
    if not left_tokens or not right_tokens:
        return 0.0

    shared = 0
    used: set[int] = set()
    for token in left_tokens:
        for index, other in enumerate(right_tokens):
            if index in used:
                continue
            if token == other or jaro_winkler(token, other) >= 0.92:
                shared += 1
                used.add(index)
                break
    if shared == 0:
        return 0.0
    return shared / min(len(left_tokens), len(right_tokens))


def match_score(left: str, right: str) -> tuple[float, str]:
    """``(score, signal)`` for a name pair — the score the 0.88 gate applies to.

    ``signal`` is ``"string"`` for a Jaro-Winkler/Levenshtein match and
    ``"token"`` when the names agree on their identity tokens but a pure string
    metric cannot see it (word order, patronymics, transliteration). The token
    path additionally demands ``name_similarity ≥ 0.60`` so unrelated names that
    share one common token cannot use it.

    The string metric is consulted **first**. It is the evidence the resolution
    spec is written against, so a pair that clears 0.88 on characters alone is a
    string match — the token path exists to *rescue* pairs the string metric
    rejects, not to add a second, stricter gate on top of one that already
    passed. Without that ordering "Kastelion Overseas Limited" vs "… Ltd"
    (0.969 on Jaro-Winkler) would be demoted to a token match, because the
    legal-form suffixes are stripped before the tokens are compared.
    """
    string_score = name_similarity(left, right)
    if string_score >= FUZZY_THRESHOLD:
        return string_score, "string"
    token_score = token_similarity(left, right)
    if token_score >= 1.0 and string_score >= TOKEN_REORDER_STRING_FLOOR:
        return max(string_score, token_score), "token"
    if token_score >= FUZZY_THRESHOLD and string_score >= TOKEN_MATCH_STRING_FLOOR:
        return max(string_score, token_score), "token"
    return string_score, "string"


# --------------------------------------------------------------------------- #
# Graph algorithms — pure Python, no GDS, no NetworkX required
# --------------------------------------------------------------------------- #
def brandes_betweenness(
    nodes: Sequence[str],
    adjacency: Mapping[str, Sequence[tuple[str, float]]],
    *,
    deadline: float | None = None,
) -> dict[str, float]:
    """Brandes' unweighted betweenness centrality, O(V·E).

    GDS is not available on AuraDB Free, so the projection is scored here. The
    graph is treated as undirected (an ownership tie connects two actors
    whichever way it was written) and the result is normalised by the number of
    distinct node pairs, which puts every score in [0, 1] and makes it directly
    comparable with the other two anomaly components.

    Deliberately unweighted: betweenness on a weighted graph needs Dijkstra per
    source, and on a 20 000-node projection that is minutes of CPU on a GitHub
    runner for a ranking that rarely changes. Edge *confidence* is applied
    earlier, when the projection is thresholded.
    """
    betweenness = dict.fromkeys(nodes, 0.0)
    total = len(nodes)
    if total < 3:
        return betweenness

    visited = 0
    for source in nodes:
        if deadline is not None and visited and visited % 128 == 0 and time.perf_counter() > deadline:
            logger.warning(
                "betweenness budget exhausted after %d/%d sources — scaling the partial result",
                visited, total,
            )
            break
        visited += 1
        stack: list[str] = []
        predecessors: dict[str, list[str]] = {node: [] for node in nodes}
        sigma = dict.fromkeys(nodes, 0.0)
        sigma[source] = 1.0
        distance = dict.fromkeys(nodes, -1)
        distance[source] = 0

        queue = deque([source])
        while queue:
            current = queue.popleft()
            stack.append(current)
            for neighbour, _weight in adjacency.get(current, ()):  # noqa: B007
                if neighbour not in distance:
                    continue
                if distance[neighbour] < 0:
                    distance[neighbour] = distance[current] + 1
                    queue.append(neighbour)
                if distance[neighbour] == distance[current] + 1:
                    sigma[neighbour] += sigma[current]
                    predecessors[neighbour].append(current)

        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            current = stack.pop()
            for predecessor in predecessors[current]:
                if sigma[current] <= 0:
                    continue
                delta[predecessor] += (sigma[predecessor] / sigma[current]) * (1 + delta[current])
            if current != source:
                betweenness[current] += delta[current]

    # Undirected graphs count every pair twice. A partial run (deadline hit)
    # divided by fewer sources, so it is rescaled by the fraction visited to stay
    # comparable with a complete one.
    norm = (total - 1) * (total - 2)
    if norm <= 0:
        return betweenness
    scale = total / visited if visited else 1.0
    return {node: min(1.0, value * scale / norm) for node, value in betweenness.items()}


def gds_betweenness(client: Neo4jClient, *, node_label: str = "Entity") -> dict[str, float] | None:
    """Betweenness from Neo4j GDS when the instance actually has it.

    Returns ``None`` when GDS is unavailable (AuraDB Free) or when projection
    fails, so the caller falls back to :func:`brandes_betweenness`. The projected
    graph is always dropped again, even on failure — a leaked projection sits in
    the instance's heap and can OOM a free-tier database.
    """
    graph_name = f"puppetnet_maintenance_{int(time.time())}"
    try:
        client.read("RETURN gds.version() AS version")
    except Exception as exc:  # noqa: BLE001
        logger.info("GDS unavailable (%s) — using the built-in Brandes implementation", exc.__class__.__name__)
        return None

    try:
        client.write(
            "CALL gds.graph.project($graphName, $nodeLabel, {ALL: {type: '*', orientation: 'UNDIRECTED'}})",
            {"graphName": graph_name, "nodeLabel": node_label},
            kind="maintenance",
        )
        rows = client.read(
            "CALL gds.betweenness.stream($graphName) "
            "YIELD nodeId, score "
            "RETURN gds.util.asNode(nodeId).canonical_key AS canonical_key, score AS score",
            {"graphName": graph_name},
        )
        scores = {str(row["canonical_key"]): float(row.get("score") or 0.0) for row in rows if row.get("canonical_key")}
        if scores:
            peak = max(scores.values()) or 1.0
            scores = {key: value / peak for key, value in scores.items()}
        logger.info("GDS betweenness scored %d node(s)", len(scores))
        return scores or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("GDS betweenness failed (%s) — falling back to Brandes", exc)
        return None
    finally:
        # A leaked projection sits in the instance heap and can OOM a free-tier
        # database, so it is dropped even when scoring failed.
        with contextlib.suppress(Exception):
            client.write("CALL gds.graph.drop($graphName)", {"graphName": graph_name}, kind="maintenance")


def label_propagation(
    nodes: Sequence[str],
    adjacency: Mapping[str, Sequence[tuple[str, float]]],
    *,
    rng: random.Random,
    max_rounds: int = 8,
) -> dict[str, str]:
    """Deterministic label propagation for cluster ids.

    Each node starts with its own label and repeatedly adopts the label held by
    most of its neighbours; ties break towards the lexicographically smallest
    label so two runs over the same graph produce the same clusters. That
    stability matters: cluster ids are compared against the previous run to find
    bridges, so a clustering that reshuffles on every run would report bridges
    that are really just relabelling.
    """
    labels = {node: node for node in nodes}
    if not nodes:
        return labels

    for _ in range(max_rounds):
        order = list(nodes)
        rng.shuffle(order)
        changed = False
        for node in order:
            neighbours = adjacency.get(node) or ()
            if not neighbours:
                continue
            tally: dict[str, float] = defaultdict(float)
            for neighbour, weight in neighbours:
                if neighbour in labels:
                    tally[labels[neighbour]] += max(0.05, float(weight or 0.0))
            if not tally:
                continue
            best = max(tally.values())
            winners = sorted(label for label, value in tally.items() if value >= best - 1e-9)
            chosen = winners[0]
            if chosen != labels[node]:
                labels[node] = chosen
                changed = True
        if not changed:
            break
    return labels


def articulation_points(nodes: Sequence[str], adjacency: Mapping[str, Sequence[tuple[str, float]]]) -> set[str]:
    """Tarjan's articulation points — the nodes whose removal splits a component.

    This is what turns "a new node touches two clusters" into "a new node is the
    *only* thing connecting them", which is the bridge condition worth alerting
    on. Iterative rather than recursive: a 20 000-node projection would blow the
    Python recursion limit.
    """
    if not nodes:
        return set()

    discovered: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    result: set[str] = set()
    #: DFS roots are special: they are cut vertices only when they have more than
    #: one child in the DFS tree. Applying the low-link rule to them flags every
    #: leaf-end of a path (`p1-p2-p3` → "p1 is a bridge"), which is wrong.
    roots: set[str] = set()
    counter = 0

    for root in nodes:
        if root in discovered:
            continue
        roots.add(root)
        counter += 1
        discovered[root] = counter
        low[root] = counter
        parent[root] = None
        stack: list[tuple[str, Iterator[str]]] = [
            (root, iter({n for n, _ in adjacency.get(root, ()) if n in discovered or n not in discovered}))
        ]
        child_count: dict[str, int] = defaultdict(int)

        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in discovered:
                    counter += 1
                    discovered[child] = counter
                    low[child] = counter
                    parent[child] = node
                    child_count[node] += 1
                    stack.append((child, iter({n for n, _ in adjacency.get(child, ())})))
                    advanced = True
                    break
                if child != parent.get(node):
                    low[node] = min(low[node], discovered[child])
            if advanced:
                continue

            stack.pop()
            ancestor = parent.get(node)
            if ancestor is None:
                if child_count[node] > 1:
                    result.add(node)
            else:
                low[ancestor] = min(low[ancestor], low[node])
                if low[node] >= discovered[ancestor] and ancestor not in roots:
                    result.add(ancestor)
    return result


# --------------------------------------------------------------------------- #
# Report dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class CapacityReport:
    """Where the database stands against the free-tier ceilings."""

    entities: int = 0
    all_nodes: int = 0
    semantic_edges: int = 0
    all_edges: int = 0
    offshore_nodes: int = 0
    node_limit: int = AURA_NODE_LIMIT
    edge_limit: int = AURA_EDGE_LIMIT
    node_utilisation: float = 0.0
    edge_utilisation: float = 0.0
    target: float = CAPACITY_TARGET_DEFAULT
    over_target: bool = False
    headroom_nodes: int = AURA_NODE_LIMIT
    headroom_edges: int = AURA_EDGE_LIMIT
    #: Non-empty when the probe failed: the zeroes then mean "unknown", not
    #: "empty database", and pruning must not escalate on that basis.
    error: str = ""


@dataclass
class MergeDecision:
    """One resolved pair, with the evidence that justified it."""

    winner_key: str
    loser_key: str
    winner_name: str = ""
    loser_name: str = ""
    entity_type: str = ""
    method: str = "strict_id"          # strict_id | fuzzy
    #: Score that admitted the pair: the string metric, or token containment when
    #: the token signal fired.
    similarity: float = 1.0
    #: The spec'd fuzzy metric (Jaro-Winkler / Levenshtein) on its own, so a
    #: reviewer can see how much of the match was word order.
    string_similarity: float = 1.0
    #: `string` | `token` | `` (strict-id matches have no fuzzy signal).
    signal: str = ""
    identifier: str = ""               # which property matched (strict path)
    reasons: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)


@dataclass
class DedupReport:
    """What entity resolution looked at and what it did.

    When :attr:`dry_run` is set the counters describe what *would* happen — the
    same convention :class:`PruneReport` uses — so a dry-run report can be
    reviewed as a plan without reading "1 merge applied" as a fact.
    """

    enabled: bool = True
    status: str = "completed"
    dry_run: bool = False
    entities_examined: int = 0
    strict_id_merges: int = 0
    strict_id_groups: int = 0
    fuzzy_candidates: int = 0
    context_pairs: int = 0
    decisions: int = 0
    merges_applied: int = 0
    mentions_repointed: int = 0
    relationships_repointed: int = 0
    nodes_deleted: int = 0
    homonyms_protected: int = 0
    type_conflicts: int = 0
    capped: bool = False
    threshold: float = FUZZY_THRESHOLD
    engine_seconds: float = 0.0
    error: str = ""
    merges: list[MergeDecision] = field(default_factory=list)
    protected: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PruneReport:
    """Capacity management: what was purged and why."""

    enabled: bool = True
    status: str = "completed"
    max_degree: int = ORPHAN_MAX_DEGREE
    max_weight: float = ORPHAN_MAX_WEIGHT
    min_age_days: int = ORPHAN_MIN_AGE_DAYS
    escalated: bool = False
    candidates: int = 0
    protected_nodes: int = 0
    purged: int = 0
    documents_purged: int = 0
    capped: bool = False
    dry_run: bool = False
    cutoff: str = ""
    seconds: float = 0.0
    error: str = ""
    sample: list[dict[str, Any]] = field(default_factory=list)
    #: Nodes the orphan rule matched but that a protection kept, with the reason.
    #: Pruning is deletion, so the audit trail matters as much as the count.
    protected: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BridgeAlert:
    """A new node that joined two previously isolated clusters."""

    canonical_key: str
    name: str
    entity_type: str = ""
    labels: list[str] = field(default_factory=list)
    first_seen: str = ""
    cluster_id: str = ""
    bridged_clusters: list[str] = field(default_factory=list)
    bridged_cluster_names: list[str] = field(default_factory=list)
    neighbours: list[str] = field(default_factory=list)
    degree: int = 0
    articulation_point: bool = False
    anomaly_score: float = 0.0
    #: The evidence an analyst needs before clicking through: how central the
    #: node is, how offshore its cluster is, where it is registered and why the
    #: scorer flagged it at all.
    betweenness: float = 0.0
    offshore_cluster_ratio: float = 0.0
    jurisdiction: str = ""
    jurisdiction_class: str = ""
    reasons: list[str] = field(default_factory=list)
    dedupe_key: str = ""


@dataclass
class CentralityReport:
    """Centrality, spikes, offshore ratios and the resulting anomaly scores."""

    enabled: bool = True
    status: str = "completed"
    engine: str = "python"             # gds | python
    nodes_projected: int = 0
    edges_projected: int = 0
    nodes_scored: int = 0
    clusters: int = 0
    articulation_points: int = 0
    truncated: bool = False
    max_nodes: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    spike_window_hours: int = DEGREE_SPIKE_WINDOW_HOURS
    seconds: float = 0.0
    error: str = ""
    top: list[dict[str, Any]] = field(default_factory=list)
    bridges: list[BridgeAlert] = field(default_factory=list)


@dataclass
class MaintenanceReport:
    """The whole run — also the contract :mod:`telegram_bot` consumes."""

    run_id: str
    generated_at: str
    dry_run: bool = False
    status: str = "completed"
    database: str = ""
    capacity: CapacityReport = field(default_factory=CapacityReport)
    dedupe: DedupReport = field(default_factory=DedupReport)
    prune: PruneReport = field(default_factory=PruneReport)
    centrality: CentralityReport = field(default_factory=CentralityReport)
    top_anomalies: list[dict[str, Any]] = field(default_factory=list)
    bridge_alerts: list[BridgeAlert] = field(default_factory=list)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict — ``asdict`` walks the nested dataclasses for us."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class GraphMaintenance:
    """Deduplication, pruning and centrality over one Neo4j database.

    Constructed with a :class:`~puppetnet.graph.neo4j_client.Neo4jClient` (or any
    object exposing ``read``/``write``/``execute_batches``/``dry_run``), so the
    whole module is testable against a scripted double.
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
        self.client = client if client is not None else self._build_client(settings)
        self.run_id = run_id or settings.run_id or f"maint-{utcnow():%Y%m%dT%H%M%SZ}"
        self.rng = random.Random(settings.seed)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_client(settings: Settings) -> Neo4jClient:
        return Neo4jClient(settings)

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.client, "dry_run", False))

    def close(self) -> None:
        if self.owns_client:
            with contextlib.suppress(Exception):
                self.client.close()

    def __enter__(self) -> GraphMaintenance:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Capacity
    # ------------------------------------------------------------------ #
    def capacity(self) -> CapacityReport:
        """Read the node/edge counts and compare them with the free-tier limits."""
        report = CapacityReport(
            node_limit=int(getattr(self.settings, "aura_node_cap", AURA_NODE_LIMIT) or AURA_NODE_LIMIT),
            edge_limit=int(getattr(self.settings, "aura_edge_cap", AURA_EDGE_LIMIT) or AURA_EDGE_LIMIT),
            target=float(getattr(self.settings, "prune_capacity_target", CAPACITY_TARGET_DEFAULT) or CAPACITY_TARGET_DEFAULT),
        )
        try:
            rows = self.client.read(CAPACITY_COUNTS)
        except Exception as exc:  # noqa: BLE001
            report.error = f"{exc.__class__.__name__}: {exc}"
            logger.warning("capacity probe failed (%s): %s", exc.__class__.__name__, exc)
            return report
        if not rows:
            logger.info("capacity probe returned no rows (dry run or empty database)")
            return report

        row = rows[0]
        report.entities = int(row.get("entities") or 0)
        report.all_nodes = int(row.get("all_nodes") or 0)
        report.semantic_edges = int(row.get("semantic_edges") or 0)
        report.all_edges = int(row.get("all_edges") or 0)
        report.offshore_nodes = int(row.get("offshore_nodes") or 0)
        report.node_utilisation = round(report.entities / report.node_limit, 4) if report.node_limit else 0.0
        report.edge_utilisation = round(report.all_edges / report.edge_limit, 4) if report.edge_limit else 0.0
        report.headroom_nodes = max(0, report.node_limit - report.entities)
        report.headroom_edges = max(0, report.edge_limit - report.all_edges)
        report.over_target = (
            report.node_utilisation >= report.target or report.edge_utilisation >= report.target
        )
        logger.info(
            "capacity: %s/%s entities (%.1f%%), %s/%s edges (%.1f%%), %s offshore",
            human_int(report.entities), human_int(report.node_limit), report.node_utilisation * 100,
            human_int(report.all_edges), human_int(report.edge_limit), report.edge_utilisation * 100,
            human_int(report.offshore_nodes),
        )
        return report

    # ------------------------------------------------------------------ #
    # Entity resolution
    # ------------------------------------------------------------------ #
    def _entity_rows(self, limit: int) -> list[dict[str, Any]]:
        """Entities to resolve. Read failures propagate.

        Swallowing the exception here would make an unreachable database
        indistinguishable from an empty one in the report — the operator would
        read ``status: empty`` while deduplication was in fact broken.
        """
        rows = self.client.read(ENTITY_ROWS_FOR_DEDUP, {"limit": int(limit)})
        return [dict(row) for row in rows or []]

    def _strict_identifier_groups(self, limit: int) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """Groups of nodes that agree on a globally unique identifier."""
        groups: list[tuple[str, str, list[dict[str, Any]]]] = []
        for property_name in STRICT_ID_PROPERTIES:
            query = STRICT_ID_GROUPS % (property_name, property_name, property_name)
            try:
                rows = self.client.read(query, {"limit": int(limit)})
            except Exception as exc:  # noqa: BLE001
                # One identifier column failing (a missing property, say) must
                # not abort the whole pass, but it is worth a line in the log.
                logger.warning("strict id scan on %s failed (%s): %s", property_name, exc.__class__.__name__, exc)
                continue
            for row in rows or []:
                members = [dict(member) for member in (row.get("members") or [])]
                if len(members) > 1:
                    groups.append((property_name, str(row.get("id_value") or ""), members))
        return groups

    def _context_pairs(self, *, since: datetime, limit: int, window_chars: int) -> dict[tuple[str, str], list[str]]:
        """Pairs that share a contextual attribute — the homonym rule's evidence.

        Keyed by the ordered node-key pair, valued with the reasons. Two sources:
        co-occurrence inside one 50-word window of the same document, and a
        shared normalised address.
        """
        pairs: dict[tuple[str, str], list[str]] = defaultdict(list)
        try:
            rows = self.client.read(
                CO_OCCURRENCE_PAIRS,
                {"window_chars": int(window_chars), "since": iso(since), "limit": int(limit)},
            )
            for row in rows or []:
                key = (str(row.get("subject_key") or ""), str(row.get("object_key") or ""))
                if all(key):
                    pairs[key].append(f"co-occurred within {window_chars // 6} words in {row.get('doc_id') or 'one document'}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("co-occurrence read failed (%s): %s", exc.__class__.__name__, exc)

        try:
            rows = self.client.read(SHARED_ADDRESS_PAIRS, {"limit": int(limit)})
            for row in rows or []:
                key = (str(row.get("subject_key") or ""), str(row.get("object_key") or ""))
                if all(key):
                    pairs[key].append(f"shared address key {row.get('address_key') or ''}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("shared-address read failed (%s): %s", exc.__class__.__name__, exc)

        return {key: reasons for key, reasons in pairs.items()}

    @staticmethod
    def _shared_identifier(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
        """The first strict identifier two rows agree on (empty when none)."""
        for property_name in STRICT_ID_PROPERTIES:
            a = str(left.get(property_name) or "").strip()
            b = str(right.get(property_name) or "").strip()
            if a and a == b:
                return f"{property_name}={a}"
        return ""

    @staticmethod
    def _shared_address(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
        a = str(left.get("address_key") or "").strip()
        b = str(right.get("address_key") or "").strip()
        return f"address_key={a}" if a and a == b else ""

    @staticmethod
    def _winner(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        """Deterministic winner of an equivalence class.

        Most-attested first (mentions), then most confident, then best connected,
        then oldest, then the lexicographically smallest key — so the same graph
        always produces the same merges and a re-run is a no-op.
        """
        return sorted(
            rows,
            key=lambda row: (
                -int(row.get("mention_count") or 0),
                -float(row.get("confidence") or 0.0),
                -int(row.get("degree") or 0),
                str(row.get("first_seen") or "9999"),
                str(row.get("canonical_key") or ""),
            ),
        )[0]

    def _fuzzy_candidates(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        threshold: float,
    ) -> list[tuple[Mapping[str, Any], Mapping[str, Any], float, str]]:
        """Blocked fuzzy matching: compare inside blocks, never all pairs.

        Two candidate generators, because neither alone is enough:

        * **Blocks** — first characters, sorted token initials and the sorted
          token set. Catches typos, truncations, suffix noise and word order.
        * **Inverted token index** — pairs sharing one identity token of four or
          more characters. Catches the transliteration and patronymic cases where
          every block key differs ("Usmanov Alisher Burkhanovich" vs "Alisher
          Usmanov" share no prefix and no initial pattern).

        Both skip degenerate buckets (a three-character prefix shared by 5 000
        nodes, or the surname "Kim"), and the whole pass is bounded by
        ``MAX_PAIR_COMPARISONS`` so a pathological graph slows the run down
        instead of hanging it.
        """
        blocks: dict[str, list[int]] = defaultdict(list)
        token_index: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            name = str(row.get("name") or "")
            for key in block_keys(name, str(row.get("entity_type") or "")):
                blocks[key].append(index)
            for token in set(name_tokens(name)):
                if len(token) >= MIN_IDENTITY_TOKEN_LENGTH:
                    token_index[token].append(index)

        name_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            folded = fold_name(str(row.get("name") or ""))
            if folded:
                name_counts[folded] += 1

        seen: set[tuple[str, str]] = set()
        candidates: list[tuple[Mapping[str, Any], Mapping[str, Any], float, str]] = []
        comparisons = 0
        skipped_buckets = 0

        buckets: list[tuple[str, list[int]]] = [(key, indices) for key, indices in blocks.items()]
        buckets += [(f"tok:{token}", indices) for token, indices in token_index.items()]

        for bucket_key, indices in buckets:
            if len(indices) < 2:
                continue
            cap = 400 if bucket_key.startswith(("t:", "s:", "n:")) else TOKEN_INDEX_CAP
            if len(indices) > cap:
                skipped_buckets += 1
                continue
            unique = sorted(set(indices))
            for position, left_index in enumerate(unique):
                left = rows[left_index]
                left_name = str(left.get("name") or "")
                left_type = str(left.get("entity_type") or "")
                for right_index in unique[position + 1:]:
                    right = rows[right_index]
                    if str(right.get("entity_type") or "") != left_type:
                        continue
                    left_key = str(left.get("canonical_key") or "")
                    right_key = str(right.get("canonical_key") or "")
                    if not left_key or not right_key or left_key == right_key:
                        continue
                    pair = (left_key, right_key) if left_key < right_key else (right_key, left_key)
                    if pair in seen:
                        continue
                    comparisons += 1
                    if comparisons > MAX_PAIR_COMPARISONS:
                        logger.warning(
                            "fuzzy matching hit the %s comparison budget — %d candidate(s) found so far",
                            human_int(MAX_PAIR_COMPARISONS), len(candidates),
                        )
                        return candidates
                    right_name = str(right.get("name") or "")
                    score, signal = match_score(left_name, right_name)
                    if score < threshold:
                        continue
                    seen.add(pair)
                    candidates.append((left, right, score, signal))

        if skipped_buckets:
            logger.info(
                "fuzzy matching skipped %d over-full bucket(s) (a shared prefix or surname is not evidence)",
                skipped_buckets,
            )
        logger.info(
            "fuzzy matching: %s comparison(s) in %d bucket(s) → %d candidate pair(s)",
            human_int(comparisons), len(buckets), len(candidates),
        )

        # Carry the common-name count so the decision step can apply the homonym
        # rule without recomputing it.
        for left, right, _score, _signal in candidates:
            folded = fold_name(str(left.get("name") or ""))
            if name_counts.get(folded, 0) > COMMON_NAME_NODE_COUNT:
                logger.debug("common-name pair queued for context check: %s / %s", left.get("name"), right.get("name"))
        return candidates

    def _decide(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        threshold: float,
        context_pairs: Mapping[tuple[str, str], Sequence[str]],
        since: datetime,
        max_merges: int,
    ) -> tuple[list[MergeDecision], list[dict[str, Any]], int, int, int, int, bool]:
        """Turn candidates into decisions, applying the homonym guard.

        Returns ``(decisions, protected_pairs, type_conflicts, fuzzy_pairs,
        strict_id_groups, strict_id_merges, capped)``.
        """
        by_key = {str(row.get("canonical_key") or ""): row for row in rows}
        name_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            folded = fold_name(str(row.get("name") or ""))
            if folded:
                name_counts[folded] += 1

        # Union-find over merge decisions so A→B and B→C become one class.
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            root = key
            while parent[root] != root:
                root = parent[root]
            while parent[key] != root:  # path compression
                parent[key], key = root, parent[key]
            return root

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                # Deterministic direction: the smaller key becomes the root, so
                # the class structure does not depend on iteration order.
                if right_root < left_root:
                    left_root, right_root = right_root, left_root
                parent[right_root] = left_root

        decisions: list[MergeDecision] = []
        protected: list[dict[str, Any]] = []
        type_conflicts = 0
        strict_groups = 0
        strict_merges = 0

        # 1) Strict identifier matches — merge unconditionally.
        for property_name, id_value, members in self._strict_identifier_groups(max_merges):
            keys = [str(member.get("key") or "") for member in members if member.get("key")]
            if len(keys) < 2:
                continue
            strict_groups += 1
            strict_merges += len(keys) - 1
            types = {str(member.get("entity_type") or "") for member in members}
            if len(types) > 1:
                type_conflicts += 1
                protected.append({
                    "reason": "type_conflict",
                    "identifier": f"{property_name}={id_value}",
                    "keys": keys,
                    "entity_types": sorted(types),
                })
                logger.warning(
                    "identifier %s=%s is shared by different entity types %s — not merging",
                    property_name, id_value, sorted(types),
                )
                continue
            for other in keys[1:]:
                union(keys[0], other)
            decisions.append(MergeDecision(
                winner_key=keys[0],
                loser_key="|".join(keys[1:]),
                method="strict_id",
                similarity=1.0,
                identifier=f"{property_name}={id_value}",
                entity_type=sorted(types)[0] if types else "",
                reasons=[f"identical {property_name}"],
            ))

        # 2) Fuzzy name matches — only with contextual corroboration.
        fuzzy_pairs = 0
        for left, right, gate_score, signal in self._fuzzy_candidates(rows, threshold=threshold):
            left_key = str(left.get("canonical_key") or "")
            right_key = str(right.get("canonical_key") or "")
            if not left_key or not right_key or left_key == right_key:
                continue
            fuzzy_pairs += 1
            ordered = tuple(sorted((left_key, right_key)))
            evidence = list(context_pairs.get(ordered, []))
            shared_id = self._shared_identifier(left, right)
            shared_address = self._shared_address(left, right)
            if shared_id:
                evidence.append(f"shared {shared_id}")
            if shared_address:
                evidence.append(f"shared {shared_address}")

            left_name = str(left.get("name") or "")
            right_name = str(right.get("name") or "")
            string_score = name_similarity(left_name, right_name)
            token_score = token_similarity(left_name, right_name)
            folded = fold_name(left_name)
            common = (
                name_counts.get(folded, 0) > COMMON_NAME_NODE_COUNT
                or is_common_name(left_name)
                or is_common_name(right_name)
            )
            generic = is_generic_name(left_name) or is_generic_name(right_name)
            reasons: list[str] = []
            if generic:
                reasons.append("generic name — context required")
            if signal == "token":
                reasons.append(f"token containment {token_score:.2f} (string {string_score:.2f})")
            elif gate_score >= NON_GENERIC_CERTAIN_SIMILARITY:
                reasons.append(f"distinctive name at {gate_score:.3f} — above the certainty floor")
            ambiguous = (
                len(name_tokens(left_name)) == 1
                and fold_name(left_name).split()[0] in AMBIGUOUS_SINGLE_TOKENS
            )
            # A token signal means the names agree on their words but not on
            # their character sequence ("Ivan Petrov" / "Petrov Ivan"). Those are
            # precisely the pairs a human would want to check — same words, quite
            # possibly different people — so they never merge on name evidence
            # alone, no matter how high the containment is.
            #
            # Otherwise the rule is exactly the one in the brief: a distinctive
            # name above the 0.88 threshold merges on its own, and only generic,
            # common or single-ambiguous-token names demand corroboration.
            # Demanding context from *every* fuzzy pair would empty the fuzzy
            # path of meaning, since most real variants score 0.88–0.97.
            token_only = signal == "token"
            needs_context = common or generic or ambiguous or token_only

            if needs_context and not evidence:
                protected.append({
                    "reason": "homonym_guard",
                    "keys": list(ordered),
                    "names": [left_name, right_name],
                    "similarity": round(gate_score, 4),
                    "string_similarity": round(string_score, 4),
                    "signal": signal,
                    "common_name": common,
                    "generic_name": generic,
                    "ambiguous_single_token": ambiguous,
                    "first_seen": [str(left.get("first_seen") or ""), str(right.get("first_seen") or "")],
                })
                logger.debug(
                    "homonym guard blocked %s / %s (%s similarity %.3f, no shared context)",
                    left_name, right_name, signal, gate_score,
                )
                continue

            union(left_key, right_key)
            decisions.append(MergeDecision(
                winner_key=left_key,
                loser_key=right_key,
                winner_name=left_name,
                loser_name=right_name,
                entity_type=str(left.get("entity_type") or ""),
                method="fuzzy",
                similarity=round(gate_score, 4),
                string_similarity=round(string_score, 4),
                signal=signal,
                reasons=reasons + [f"{signal} similarity {gate_score:.3f} ≥ {threshold:.2f}"],
                context=evidence[:6],
            ))

        # 3) Collapse the union-find into one decision per equivalence class.
        classes: dict[str, list[str]] = defaultdict(list)
        for key in list(parent):
            classes[find(key)].append(key)

        final: list[MergeDecision] = []
        capped = False
        for members in sorted(classes.values()):
            unique = sorted(set(members))
            if len(unique) < 2:
                continue
            rows_in_class = [by_key[key] for key in unique if key in by_key]
            if len(rows_in_class) < 2:
                continue
            winner = self._winner(rows_in_class)
            winner_key = str(winner.get("canonical_key") or "")
            for row in rows_in_class:
                loser_key = str(row.get("canonical_key") or "")
                if loser_key == winner_key:
                    continue
                if len(final) >= max_merges:
                    capped = True
                    break
                source = next(
                    (d for d in decisions
                     if {d.winner_key, *d.loser_key.split("|")} & {winner_key, loser_key}),
                    None,
                )
                final.append(MergeDecision(
                    winner_key=winner_key,
                    loser_key=loser_key,
                    winner_name=str(winner.get("name") or ""),
                    loser_name=str(row.get("name") or ""),
                    entity_type=str(winner.get("entity_type") or row.get("entity_type") or ""),
                    method=source.method if source else "fuzzy",
                    similarity=source.similarity if source else 1.0,
                    string_similarity=source.string_similarity if source else 1.0,
                    signal=source.signal if source else "",
                    identifier=source.identifier if source else "",
                    reasons=list(source.reasons) if source else ["equivalence class"],
                    context=list(source.context) if source else [],
                ))
            if capped:
                break

        if capped:
            logger.warning(
                "dedup hit the max-merges cap of %d — %d equivalence class(es) were left for the next run",
                max_merges, sum(1 for members in classes.values() if len(set(members)) > 1) - len(final),
            )
        logger.info(
            "dedup: %d strict group(s), %d fuzzy pair(s), %d merge decision(s), %d homonym pair(s) protected",
            strict_groups, fuzzy_pairs, len(final), len(protected),
        )
        del since  # the window is applied when context pairs are read
        return final, protected, type_conflicts, fuzzy_pairs, strict_groups, strict_merges, capped

    def _apply_merges(self, decisions: Sequence[MergeDecision]) -> tuple[int, int, int, int]:
        """Execute merges: repoint mentions, repoint edges, absorb, delete.

        Returns ``(merges_applied, mentions, relationships, nodes_deleted)``.
        Relationship types are discovered from the graph rather than assumed, and
        every type is validated against the closed vocabulary before it is
        interpolated into Cypher.
        """
        rows = [{"winner_key": d.winner_key, "loser_key": d.loser_key} for d in decisions]
        if not rows:
            return 0, 0, 0, 0

        loser_keys = [row["loser_key"] for row in rows]
        predicates: list[str] = []
        try:
            found = self.client.read(LOSER_RELATIONSHIP_TYPES, {"loser_keys": loser_keys})
            predicates = sorted({str(row.get("predicate") or "") for row in found or [] if row.get("predicate")})
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not enumerate loser relationship types (%s) — using the full vocabulary", exc)
        if not predicates:
            predicates = sorted({member.value for member in _relation_type_members()})

        safe_predicates = [p for p in predicates if is_safe_relationship_type(p)]
        rejected = [p for p in predicates if not is_safe_relationship_type(p)]
        if rejected:
            logger.warning("skipping unsafe relationship types in merge repointing: %s", rejected)

        mentions = 0
        relationships = 0
        for batch in chunked(rows, int(getattr(self.settings, "neo4j_batch_size", 500) or 500)):
            try:
                result = self.client.write(REPOINT_MENTIONS, {"rows": batch}, rows=len(batch), kind="maintenance")
                mentions += int(result[0].get("repointed") or 0) if result else 0
            except Exception as exc:  # noqa: BLE001
                logger.error("mention repointing failed for a batch of %d: %s", len(batch), exc)

            for predicate in safe_predicates:
                outgoing = REPOINT_OUTGOING.format(predicate=predicate)
                incoming = REPOINT_INCOMING.format(predicate=predicate)
                for template in (outgoing, incoming):
                    try:
                        result = self.client.write(template, {"rows": batch}, rows=len(batch), kind="maintenance")
                        relationships += int(result[0].get("repointed") or 0) if result else 0
                    except Exception as exc:  # noqa: BLE001
                        logger.error("repointing %s failed for a batch of %d: %s", predicate, len(batch), exc)

        applied = 0
        for batch in chunked(rows, int(getattr(self.settings, "neo4j_batch_size", 500) or 500)):
            try:
                self.client.write(MERGE_ENTITY_PROPERTIES, {"rows": batch, "merged_at": iso(utcnow())}, rows=len(batch), kind="maintenance")
                applied += len(batch)
            except Exception as exc:  # noqa: BLE001
                logger.error("property merge failed for a batch of %d: %s", len(batch), exc)

        deleted = 0
        try:
            leftover = self.client.read(LOSER_LEFTOVER_RELATIONSHIPS, {"rows": rows})
            if leftover:
                logger.warning(
                    "%d loser relationship(s) survived repointing and will be dropped with the node: %s",
                    sum(int(row.get("edges") or 0) for row in leftover),
                    sorted({str(row.get("predicate") or "") for row in leftover})[:8],
                )
        except Exception:  # noqa: BLE001
            pass

        for batch in chunked(rows, int(getattr(self.settings, "neo4j_batch_size", 500) or 500)):
            try:
                self.client.write(DELETE_MERGED_ENTITY, {"rows": batch}, rows=len(batch), kind="maintenance")
                deleted += len(batch)
            except Exception as exc:  # noqa: BLE001
                logger.error("deleting merged nodes failed for a batch of %d: %s", len(batch), exc)

        return applied, mentions, relationships, deleted

    def dedupe(self, *, threshold: float | None = None, max_merges: int | None = None) -> DedupReport:
        """Resolve duplicate entities, honouring the homonym guard."""
        report = DedupReport(
            enabled=bool(getattr(self.settings, "dedupe_enabled", True)),
            dry_run=self.dry_run,
            threshold=float(threshold if threshold is not None else getattr(self.settings, "dedupe_fuzzy_threshold", FUZZY_THRESHOLD)),
        )
        if not report.enabled:
            report.status = "disabled"
            logger.info("deduplication disabled by configuration")
            return report

        started = time.perf_counter()
        limit = int(max_merges or getattr(self.settings, "dedupe_max_merges", 500) or 500)
        entity_limit = int(getattr(self.settings, "dedupe_entity_limit", 50_000) or 50_000)
        window_words = int(getattr(self.settings, "dedupe_cooccurrence_window_words", CO_OCCURRENCE_WINDOW_WORDS))
        window_chars = max(60, window_words * 6)
        since = utcnow() - timedelta(days=int(getattr(self.settings, "dedupe_cooccurrence_days", 90) or 90))

        try:
            rows = self._entity_rows(entity_limit)
            report.entities_examined = len(rows)
            if not rows:
                report.status = "empty" if not self.dry_run else "dry_run"
                report.engine_seconds = round(time.perf_counter() - started, 3)
                logger.info("dedup: no entity rows to examine")
                return report

            context = self._context_pairs(since=since, limit=entity_limit, window_chars=window_chars)
            report.context_pairs = len(context)
            decisions, protected, type_conflicts, fuzzy_pairs, strict_groups, strict_merges, capped = self._decide(
                rows, threshold=report.threshold, context_pairs=context, since=since, max_merges=limit,
            )
            report.strict_id_groups = strict_groups
            report.strict_id_merges = strict_merges
            report.capped = capped
            report.fuzzy_candidates = fuzzy_pairs
            report.decisions = len(decisions)
            report.homonyms_protected = len(protected)
            report.type_conflicts = type_conflicts
            report.protected = protected[:50]
            report.merges = decisions[:50]

            if decisions:
                applied, mentions, relationships, deleted = self._apply_merges(decisions)
                report.merges_applied = applied
                report.mentions_repointed = mentions
                report.relationships_repointed = relationships
                report.nodes_deleted = deleted
                if applied < len(decisions):
                    report.capped = True
            logger.info(
                "dedup: merged %d pair(s) — %d mention(s) and %d relationship(s) repointed, %d node(s) removed",
                report.merges_applied, report.mentions_repointed,
                report.relationships_repointed, report.nodes_deleted,
            )
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.error = f"{exc.__class__.__name__}: {exc}"
            logger.error("deduplication failed: %s", exc)
        finally:
            report.engine_seconds = round(time.perf_counter() - started, 3)
        return report

    # ------------------------------------------------------------------ #
    # Pruning / capacity management
    # ------------------------------------------------------------------ #
    def _orphan_pass(
        self,
        *,
        max_degree: int,
        max_weight: float,
        min_age_days: int,
        max_deletions: int,
        protect_risk_score: float,
    ) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
        """One pruning pass at fixed thresholds.

        Returns ``(candidates, purged, kept, sample)`` where ``kept`` lists the
        nodes the rule matched but a protection spared, each with its reason.
        """
        cutoff = iso(utcnow() - timedelta(days=min_age_days))
        try:
            rows = self.client.read(
                ORPHAN_CANDIDATES,
                {
                    "cutoff": cutoff,
                    "max_degree": int(max_degree),
                    "max_weight": float(max_weight),
                    "limit": int(max(max_deletions * 4, 200)),
                },
            )
        except Exception:  # noqa: BLE001
            # Propagated for the same reason as in `_project_graph`.
            raise

        candidates = [dict(row) for row in rows or []]
        keep: list[dict[str, Any]] = []
        purge: list[dict[str, Any]] = []
        for row in candidates:
            risk = float(row.get("risk_score") or 0.0)
            labels = [str(label) for label in (row.get("labels") or [])]
            if risk >= protect_risk_score:
                keep.append({"canonical_key": row.get("canonical_key"), "reason": f"risk_score={risk:.2f}"})
                continue
            if any(label in OFFSHORE_LABELS for label in labels) and int(row.get("degree") or 0) > 0:
                # A shell company with even one tie is evidence of a structure,
                # not litter: the whole point of the graph is to keep it.
                keep.append({"canonical_key": row.get("canonical_key"), "reason": f"offshore label {labels}"})
                continue
            purge.append(row)

        purge = purge[:max_deletions]
        sample = [
            {
                "canonical_key": row.get("canonical_key"),
                "name": row.get("name"),
                "degree": row.get("degree"),
                "weights": [round(float(w or 0.0), 3) for w in (row.get("weights") or [])][:4],
                "last_seen": row.get("last_seen"),
                "labels": row.get("labels"),
            }
            for row in purge[:25]
        ]

        logger.debug("orphan pass: %d candidate(s), %d protected, %d to purge", len(candidates), len(keep), len(purge))
        purged = 0
        if purge and not self.dry_run:
            rows_payload = [{"canonical_key": row.get("canonical_key")} for row in purge if row.get("canonical_key")]
            for batch in chunked(rows_payload, int(getattr(self.settings, "neo4j_batch_size", 500) or 500)):
                try:
                    result = self.client.write(PURGE_ORPHANS, {"rows": batch}, rows=len(batch), kind="maintenance")
                    purged += int(result[0].get("purged") or 0) if result else len(batch)
                except Exception as exc:  # noqa: BLE001
                    logger.error("orphan purge failed for a batch of %d: %s", len(batch), exc)
        else:
            purged = len(purge)

        return len(candidates), purged, keep, sample

    def prune(self, *, capacity: CapacityReport | None = None) -> PruneReport:
        """Purge orphan nodes, escalating thresholds when capacity demands it."""
        report = PruneReport(
            enabled=bool(getattr(self.settings, "prune_enabled", True)),
            dry_run=self.dry_run,
            max_degree=int(getattr(self.settings, "prune_orphan_max_degree", ORPHAN_MAX_DEGREE)),
            max_weight=float(getattr(self.settings, "prune_orphan_max_weight", ORPHAN_MAX_WEIGHT)),
            min_age_days=int(getattr(self.settings, "prune_orphan_min_age_days", ORPHAN_MIN_AGE_DAYS)),
        )
        if not report.enabled:
            report.status = "disabled"
            logger.info("pruning disabled by configuration")
            return report

        started = time.perf_counter()
        if capacity is None:
            # Without this, `--prune` on its own would never escalate: the
            # capacity pass is what tells pruning that the free tier is nearly
            # full. A failed probe is reported, not silently treated as "empty".
            capacity = self.capacity()
        max_deletions = int(getattr(self.settings, "prune_max_deletions", 5_000) or 5_000)
        protect = float(getattr(self.settings, "puppet_master_min_score", PRUNE_PROTECT_RISK_SCORE) or PRUNE_PROTECT_RISK_SCORE)
        report.cutoff = iso(utcnow() - timedelta(days=report.min_age_days))

        try:
            candidates, purged, kept, sample = self._orphan_pass(
                max_degree=report.max_degree,
                max_weight=report.max_weight,
                min_age_days=report.min_age_days,
                max_deletions=max_deletions,
                protect_risk_score=protect,
            )
            report.candidates = candidates
            report.purged = purged
            report.protected_nodes = len(kept)
            report.protected = kept[:50]
            report.sample = sample
            if purged >= max_deletions:
                report.capped = True

            # Capacity escalation: if the database is still near the ceiling after
            # a normal pass, widen the definition of "orphan" and run once more.
            over = bool(capacity.over_target) if capacity is not None else False
            if over:
                report.escalated = True
                escalated = dict(ORPHAN_ESCALATED)
                logger.warning(
                    "capacity above target (%.1f%% nodes, %.1f%% edges) — escalating pruning to degree<=%d, weight<%.2f, age>%dd",
                    (capacity.node_utilisation if capacity else 0) * 100,
                    (capacity.edge_utilisation if capacity else 0) * 100,
                    escalated["max_degree"], escalated["max_weight"], escalated["min_age_days"],
                )
                candidates2, purged2, kept2, sample2 = self._orphan_pass(
                    max_degree=int(escalated["max_degree"]),
                    max_weight=float(escalated["max_weight"]),
                    min_age_days=int(escalated["min_age_days"]),
                    max_deletions=max_deletions,
                    protect_risk_score=protect,
                )
                report.candidates += candidates2
                report.purged += purged2
                report.protected_nodes += len(kept2)
                report.protected = (report.protected + kept2)[:50]
                report.sample = (report.sample + sample2)[:25]
                report.max_degree = int(escalated["max_degree"])
                report.max_weight = float(escalated["max_weight"])
                report.min_age_days = int(escalated["min_age_days"])
                report.cutoff = iso(utcnow() - timedelta(days=report.min_age_days))

            # Documents left with no mentions are bookkeeping, not evidence.
            if not self.dry_run:
                try:
                    cutoff_docs = iso(utcnow() - timedelta(days=max(report.min_age_days, 30)))
                    result = self.client.write(PURGE_DETACHED_DOCUMENTS, {"cutoff": cutoff_docs}, kind="maintenance")
                    report.documents_purged = int(result[0].get("purged") or 0) if result else 0
                except Exception as exc:  # noqa: BLE001
                    logger.debug("detached-document purge skipped (%s)", exc.__class__.__name__)

            logger.info(
                "prune: %d candidate(s), %d purged, %d protected%s%s",
                report.candidates, report.purged, report.protected_nodes,
                " (escalated)" if report.escalated else "",
                " [dry run]" if self.dry_run else "",
            )
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.error = f"{exc.__class__.__name__}: {exc}"
            logger.error("pruning failed: %s", exc)
        finally:
            report.seconds = round(time.perf_counter() - started, 3)
        return report

    # ------------------------------------------------------------------ #
    # Centrality, clustering, anomaly scoring, bridges
    # ------------------------------------------------------------------ #
    def _project_graph(self, *, max_nodes: int, min_confidence: float, edge_limit: int) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, float]]], dict[str, dict[str, Any]]]:
        """Build the in-memory projection the algorithms run on.

        Nodes are chosen by weighted degree (the best-connected first) so the
        projection keeps the backbone of the graph when it has to be truncated —
        betweenness over a random sample of a scale-free network is noise.
        """
        try:
            edge_rows = self.client.read(
                CENTRALITY_EDGES,
                {"min_confidence": float(min_confidence), "limit": int(edge_limit)},
            )
        except Exception:  # noqa: BLE001
            # Propagated: an unreachable database must not be reported as an
            # empty graph, or a broken centrality pass looks like a clean one.
            raise

        degree: dict[str, float] = defaultdict(float)
        edges: list[tuple[str, str, float]] = []
        for row in edge_rows or []:
            subject = str(row.get("subject_key") or "")
            obj = str(row.get("object_key") or "")
            if not subject or not obj or subject == obj:
                continue
            weight = float(row.get("weight") or 0.3)
            strength = max(0.05, min(1.0, weight)) * max(0.1, min(1.0, float(row.get("confidence") or 0.5)))
            edges.append((subject, obj, strength))
            degree[subject] += strength
            degree[obj] += strength

        if not edges:
            return [], {}, {}

        ranked = sorted(degree.items(), key=lambda item: (-item[1], item[0]))
        truncated = len(ranked) > max_nodes
        keep = {key for key, _ in ranked[:max_nodes]}

        try:
            node_rows = []
            for batch in chunked(sorted(keep), 500):
                node_rows.extend(self.client.read(CENTRALITY_NODES, {"keys": batch}) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("centrality node read failed (%s): %s", exc.__class__.__name__, exc)
            node_rows = []

        nodes: dict[str, dict[str, Any]] = {}
        for row in node_rows or []:
            key = str(row.get("canonical_key") or "")
            if key in keep:
                nodes[key] = dict(row)
        for key in keep:
            nodes.setdefault(key, {"canonical_key": key, "name": key, "degree": 0})

        adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        kept_edges = 0
        for subject, obj, strength in edges:
            if subject in keep and obj in keep:
                adjacency[subject].append((obj, strength))
                adjacency[obj].append((subject, strength))
                kept_edges += 1

        logger.info(
            "projection: %d node(s), %d edge(s)%s",
            len(nodes), kept_edges, " (truncated to the best-connected)" if truncated else "",
        )
        ordered = [nodes[key] for key in sorted(nodes)]
        return ordered, dict(adjacency), nodes

    def _offshore_flags(self, nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, bool]:
        """Which projected nodes count as offshore for the cluster ratio."""
        flags: dict[str, bool] = {}
        for key, row in nodes.items():
            labels = {str(label) for label in (row.get("labels") or [])}
            jurisdiction_class = str(row.get("jurisdiction_class") or "").lower()
            flags[key] = bool(
                labels & OFFSHORE_LABELS
                or jurisdiction_class in OFFSHORE_JURISDICTION_CLASSES
                or row.get("is_shell") is True
            )
        return flags

    def centrality(
        self,
        *,
        max_nodes: int | None = None,
        top: int | None = None,
        weights: Mapping[str, float] | None = None,
        engine: str = "",
    ) -> CentralityReport:
        """Betweenness + 24 h degree spike + offshore cluster ratio → anomaly score."""
        report = CentralityReport(
            enabled=bool(getattr(self.settings, "centrality_enabled", True)),
            weights=dict(weights or getattr(self.settings, "anomaly_weights", ANOMALY_WEIGHTS) or ANOMALY_WEIGHTS),
            spike_window_hours=int(getattr(self.settings, "anomaly_degree_spike_window_hours", DEGREE_SPIKE_WINDOW_HOURS)),
        )
        if not report.enabled:
            report.status = "disabled"
            logger.info("centrality disabled by configuration")
            return report

        started = time.perf_counter()
        cap = int(max_nodes or getattr(self.settings, "centrality_max_nodes", 4_000) or 4_000)
        report.max_nodes = cap
        edge_limit = int(getattr(self.settings, "analytics_graph_edge_limit", 50_000) or 50_000)
        min_confidence = float(getattr(self.settings, "min_edge_confidence", 0.05) or 0.05)
        chosen_engine = (engine or getattr(self.settings, "centrality_engine", "auto") or "auto").lower()
        top_n = int(top or getattr(self.settings, "anomaly_top_n", 5) or 5)
        w1 = float(report.weights.get("betweenness", ANOMALY_WEIGHTS["betweenness"]))
        w2 = float(report.weights.get("degree_spike", ANOMALY_WEIGHTS["degree_spike"]))
        w3 = float(report.weights.get("offshore_ratio", ANOMALY_WEIGHTS["offshore_ratio"]))

        try:
            rows, adjacency, nodes = self._project_graph(max_nodes=cap, min_confidence=min_confidence, edge_limit=edge_limit)
            report.nodes_projected = len(rows)
            report.edges_projected = sum(len(neighbours) for neighbours in adjacency.values()) // 2
            if not rows:
                report.status = "empty" if not self.dry_run else "dry_run"
                report.seconds = round(time.perf_counter() - started, 3)
                return report

            keys = [str(row.get("canonical_key") or "") for row in rows]
            scores: dict[str, float] | None = None
            if chosen_engine in ("auto", "gds"):
                scores = gds_betweenness(self.client)
                if scores is not None:
                    report.engine = "gds"
            if scores is None:
                report.engine = "python"
                budget = float(getattr(self.settings, "centrality_max_seconds", 240.0) or 240.0)
                started_scoring = time.perf_counter()
                scores = brandes_betweenness(keys, adjacency, deadline=started_scoring + max(1.0, budget))
                if time.perf_counter() > started_scoring + max(1.0, budget):
                    report.truncated = True

            clusters = label_propagation(keys, adjacency, rng=self.rng)
            cluster_sizes: dict[str, int] = defaultdict(int)
            for label in clusters.values():
                cluster_sizes[label] += 1
            report.clusters = len(cluster_sizes)

            cut = articulation_points(keys, adjacency)
            report.articulation_points = len(cut)

            offshore = self._offshore_flags(nodes)
            cluster_offshore: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for key, label in clusters.items():
                bucket = cluster_offshore[label]
                bucket[1] += 1
                if offshore.get(key):
                    bucket[0] += 1

            now = utcnow()
            window = timedelta(hours=report.spike_window_hours)
            written: list[dict[str, Any]] = []
            for row in rows:
                key = str(row.get("canonical_key") or "")
                betweenness = round(float(scores.get(key, 0.0)), 6)
                degree = int(row.get("degree") or 0)
                previous = row.get("degree_prev")
                previous_at = parse_timestamp(row.get("metrics_at"))
                degree_prev = int(previous) if previous is not None else None

                if degree_prev is None:
                    spike = 0.0
                    spike_reason = "no previous snapshot"
                elif previous_at is not None and now - previous_at > window:
                    spike = 0.0
                    spike_reason = f"previous snapshot {int((now - previous_at).total_seconds() // 3600)}h old (> {report.spike_window_hours}h window)"
                else:
                    growth = (degree - degree_prev) / max(1, degree_prev)
                    spike = max(0.0, min(1.0, growth / DEGREE_SPIKE_SATURATION))
                    spike_reason = f"degree {degree_prev} → {degree}"

                neighbours = [neighbour for neighbour, _ in adjacency.get(key, ())]
                neighbour_ratio = (
                    sum(1 for n in neighbours if offshore.get(n)) / len(neighbours) if neighbours else 0.0
                )
                label = clusters.get(key, key)
                offshore_count, cluster_total = cluster_offshore.get(label, [0, 0])
                cluster_ratio = (offshore_count / cluster_total) if cluster_total else 0.0

                score = w1 * betweenness + w2 * round(spike, 6) + w3 * round(cluster_ratio, 6)
                reasons: list[str] = []
                if betweenness >= 0.01:
                    reasons.append(f"betweenness {betweenness:.4f} (bridges otherwise separate parts of the graph)")
                if spike > 0:
                    reasons.append(f"24h degree spike: {spike_reason}")
                if cluster_ratio >= 0.3:
                    reasons.append(f"{offshore_count}/{cluster_total} of its cluster is offshore or shell")
                if neighbour_ratio >= 0.5 and neighbours:
                    reasons.append(f"{sum(1 for n in neighbours if offshore.get(n))}/{len(neighbours)} direct neighbours are offshore")
                if key in cut:
                    reasons.append("articulation point: removing it would split the network")

                written.append({
                    "canonical_key": key,
                    "betweenness": betweenness,
                    "degree": degree,
                    "degree_prev": degree_prev if degree_prev is not None else -1,
                    "degree_spike": round(spike, 6),
                    "offshore_neighbour_ratio": round(neighbour_ratio, 6),
                    "offshore_cluster_ratio": round(cluster_ratio, 6),
                    "anomaly_score": round(min(1.0, max(0.0, score)), 6),
                    # Flattened: Neo4j rejects map-valued properties, so the
                    # component map lives in the report and the node carries one
                    # property per component.
                    "anomaly_betweenness": betweenness,
                    "anomaly_degree_spike": round(spike, 6),
                    "anomaly_offshore_cluster_ratio": round(cluster_ratio, 6),
                    "anomaly_offshore_neighbour_ratio": round(neighbour_ratio, 6),
                    "anomaly_reasons": reasons[:8],
                    "cluster_id": label,
                    "cluster_size": int(cluster_sizes.get(label, 0)),
                    "is_bridge_candidate": key in cut,
                    "metrics_at": iso(now),
                    "run_id": self.run_id,
                })

            if written and not self.dry_run:
                submitted = self.client.execute_batches(
                    WRITE_NODE_METRICS, written, kind="maintenance", label="maintenance:metrics"
                )
                report.nodes_scored = submitted
            else:
                report.nodes_scored = len(written)

            written.sort(key=lambda item: (-float(item["anomaly_score"]), -float(item["betweenness"])))
            report.top = [
                {
                    "canonical_key": item["canonical_key"],
                    "name": str(nodes.get(item["canonical_key"], {}).get("name") or item["canonical_key"]),
                    "entity_type": str(nodes.get(item["canonical_key"], {}).get("entity_type") or ""),
                    "labels": [str(x) for x in (nodes.get(item["canonical_key"], {}).get("labels") or [])],
                    "anomaly_score": item["anomaly_score"],
                    "components": {
                        "betweenness": item["anomaly_betweenness"],
                        "degree_spike": item["anomaly_degree_spike"],
                        "offshore_cluster_ratio": item["anomaly_offshore_cluster_ratio"],
                        "offshore_neighbour_ratio": item["anomaly_offshore_neighbour_ratio"],
                    },
                    "reasons": item["anomaly_reasons"],
                    "degree": item["degree"],
                    "cluster_id": item["cluster_id"],
                    "cluster_size": item["cluster_size"],
                    "is_bridge_candidate": item["is_bridge_candidate"],
                }
                for item in written[:max(top_n, 25)]
            ]
            report.bridges = self._bridge_alerts(rows, adjacency, clusters, cut, nodes, written)
            logger.info(
                "centrality(%s): %d node(s) scored, %d cluster(s), %d articulation point(s), %d bridge alert(s)",
                report.engine, report.nodes_scored, report.clusters,
                report.articulation_points, len(report.bridges),
            )
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.error = f"{exc.__class__.__name__}: {exc}"
            logger.error("centrality failed: %s", exc)
        finally:
            report.seconds = round(time.perf_counter() - started, 3)
        return report

    def _bridge_alerts(
        self,
        rows: Sequence[Mapping[str, Any]],
        adjacency: Mapping[str, Sequence[tuple[str, float]]],
        clusters: Mapping[str, str],
        cut: set[str],
        nodes: Mapping[str, Mapping[str, Any]],
        scored: Sequence[Mapping[str, Any]],
    ) -> list[BridgeAlert]:
        """New nodes that joined two clusters which were separate last run.

        Three conditions, all required:

        1. the node first appeared inside the alert window;
        2. at least two of its neighbours carry *different* ``cluster_id`` values
           from the previous run (``cluster_prev``) — i.e. it reaches into two
           worlds that were distinct yesterday;
        3. it is an articulation point of the current projection, so it is the
           load-bearing connection rather than a passenger.
        """
        window_hours = int(getattr(self.settings, "anomaly_degree_spike_window_hours", DEGREE_SPIKE_WINDOW_HOURS) or 24)
        since = utcnow() - timedelta(hours=max(1, window_hours))
        metrics = {str(item.get("canonical_key") or ""): dict(item) for item in scored}
        scores = {key: float(item.get("anomaly_score") or 0.0) for key, item in metrics.items()}
        alerts: list[BridgeAlert] = []

        for row in rows:
            key = str(row.get("canonical_key") or "")
            first_seen = parse_timestamp(row.get("first_seen"))
            if not key or first_seen is None or first_seen < since:
                continue
            if key not in cut:
                continue

            previous_clusters: dict[str, list[str]] = defaultdict(list)
            for neighbour, _weight in adjacency.get(key, ()):
                neighbour_row = nodes.get(neighbour) or {}
                previous = str(neighbour_row.get("cluster_prev") or "").strip()
                if previous:
                    previous_clusters[previous].append(str(neighbour_row.get("name") or neighbour))

            if len(previous_clusters) < 2:
                continue

            bridged = sorted(previous_clusters)
            names: list[str] = []
            for cluster in bridged:
                members = previous_clusters[cluster][:2]
                names.append(f"{cluster[:24]} ({', '.join(members)})")

            alerts.append(BridgeAlert(
                canonical_key=key,
                name=str(row.get("name") or key),
                entity_type=str(row.get("entity_type") or ""),
                labels=[str(label) for label in (row.get("labels") or [])],
                first_seen=iso(first_seen),
                cluster_id=str(clusters.get(key, "")),
                bridged_clusters=bridged,
                bridged_cluster_names=names,
                neighbours=[str((nodes.get(n) or {}).get("name") or n) for n, _ in adjacency.get(key, ())][:8],
                degree=int(row.get("degree") or 0),
                articulation_point=True,
                anomaly_score=scores.get(key, 0.0),
                betweenness=float(metrics.get(key, {}).get("betweenness") or 0.0),
                offshore_cluster_ratio=float(metrics.get(key, {}).get("anomaly_offshore_cluster_ratio") or 0.0),
                jurisdiction=str(row.get("jurisdiction") or ""),
                jurisdiction_class=str(row.get("jurisdiction_class") or ""),
                reasons=[str(reason) for reason in (metrics.get(key, {}).get("anomaly_reasons") or [])][:4],
                dedupe_key=f"bridge:{key}:{'|'.join(bridged)}",
            ))

        alerts.sort(key=lambda alert: (-alert.anomaly_score, -alert.degree, alert.name))
        configured = getattr(self.settings, "telegram_bridge_alert_limit", BRIDGE_ALERT_LIMIT)
        limit = BRIDGE_ALERT_LIMIT if configured is None else int(configured)
        if limit < 0:
            limit = 0
        if len(alerts) > limit:
            logger.info("%d bridge alert(s) capped to %d", len(alerts), limit)
        return alerts[:limit]

    def top_anomalies(self, *, limit: int | None = None, hours: int | None = None) -> list[dict[str, Any]]:
        """Highest anomaly scores of the last ``hours`` — the daily digest input."""
        top_n = int(limit or getattr(self.settings, "anomaly_top_n", 5) or 5)
        window = int(hours or getattr(self.settings, "anomaly_degree_spike_window_hours", DEGREE_SPIKE_WINDOW_HOURS) or 24)
        since = iso(utcnow() - timedelta(hours=max(1, window)))
        try:
            rows = self.client.read(TOP_ANOMALIES, {"since": since, "limit": top_n})
        except Exception as exc:  # noqa: BLE001
            logger.warning("top-anomaly read failed (%s): %s", exc.__class__.__name__, exc)
            return []
        # The Cypher carries ORDER BY … LIMIT, but the digest contract is "top
        # N", so the slice is enforced here too: a database that ignores the
        # limit must not be able to flood a channel.
        scored = [dict(row) for row in rows or []]
        scored.sort(key=lambda row: -float(row.get("anomaly_score") or 0.0))
        return scored[:top_n]

    def prune_stale_metrics(self, *, days: int | None = None) -> int:
        """Drop calculated properties that no run has refreshed recently."""
        window = int(days or getattr(self.settings, "analytics_prune_days", 14) or 14)
        cutoff = iso(utcnow() - timedelta(days=window))
        try:
            rows = self.client.write(PRUNE_STALE_METRICS, {"cutoff": cutoff}, kind="maintenance")
            return int(rows[0].get("cleared") or 0) if rows else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("stale-metric cleanup failed (%s): %s", exc.__class__.__name__, exc)
            return 0

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        dedupe: bool = True,
        prune: bool = True,
        centrality: bool = True,
        threshold: float | None = None,
        max_merges: int | None = None,
        top: int | None = None,
    ) -> MaintenanceReport:
        """Run the maintenance stages in dependency order and build the report.

        Order matters: duplicates are merged first (otherwise the same actor is
        scored twice and its degree is split across two nodes), then capacity is
        reclaimed, then centrality is computed on the cleaned graph.
        """
        started = time.perf_counter()
        report = MaintenanceReport(
            run_id=self.run_id,
            generated_at=iso(utcnow()),
            dry_run=self.dry_run,
            database=str(getattr(self.settings, "neo4j_database", "") or ""),
        )

        report.capacity = self.capacity()

        if dedupe:
            report.dedupe = self.dedupe(threshold=threshold, max_merges=max_merges)
            if report.dedupe.status == "failed":
                report.errors.append(f"dedupe: {report.dedupe.error}")
        else:
            # A fresh report defaults to "completed"; saying that about a stage
            # nobody asked for would hide a configuration mistake.
            report.dedupe.status = "skipped"

        if prune:
            report.prune = self.prune(capacity=report.capacity)
            if report.prune.status == "failed":
                report.errors.append(f"prune: {report.prune.error}")
            # The purge changed the counts the digest reports on.
            report.capacity = self.capacity()
        else:
            report.prune.status = "skipped"

        if centrality:
            report.centrality = self.centrality(top=top)
            if report.centrality.status == "failed":
                report.errors.append(f"centrality: {report.centrality.error}")
            report.top_anomalies = self.top_anomalies(limit=max(int(top or 5), 5))
            report.bridge_alerts = report.centrality.bridges
            cleared = self.prune_stale_metrics()
            if cleared:
                logger.info("cleared stale calculated properties on %d node(s)", cleared)
        else:
            report.centrality.status = "skipped"

        attempted = [
            stage
            for enabled, stage in ((dedupe, report.dedupe), (prune, report.prune), (centrality, report.centrality))
            if enabled
        ]
        failed = [stage for stage in attempted if stage.status == "failed"]
        if failed and len(failed) == len(attempted):
            report.status = "failed"
        elif failed:
            report.status = "partial"
        else:
            report.status = "completed"
        report.seconds = round(time.perf_counter() - started, 3)
        logger.info(
            "maintenance %s in %.1fs: %d merge(s), %d purged, %d scored, %d bridge alert(s), %d error(s)",
            report.status, report.seconds, report.dedupe.merges_applied, report.prune.purged,
            report.centrality.nodes_scored, len(report.bridge_alerts), len(report.errors),
        )
        return report

    def write_report(self, report: MaintenanceReport, directory: str | Path | None = None) -> Path:
        """Write ``reports/graph_maintenance_<run_id>.json`` for the alert bot."""
        target = Path(directory or getattr(self.settings, "report_dir", "reports") or "reports")
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"graph_maintenance_{report.run_id}.json"
        path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=False, default=str), encoding="utf-8")
        logger.info("maintenance report written to %s", path)
        return path


def _relation_type_members() -> Iterable[Any]:
    """The closed predicate vocabulary, imported lazily to keep startup cheap."""
    from puppetnet.models import RelationType  # noqa: PLC0415

    return list(RelationType)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph_analytics.py",
        description="Entity resolution, capacity pruning and centrality scoring for the PuppetNET graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Stages run in dependency order: dedupe → prune → centrality.\n"
            "Exit codes: 0 success · 1 configuration · 2 runtime failure · 3 partial."
        ),
    )
    parser.add_argument("--all", action="store_true", help="Run every stage (default when no stage flag is given).")
    parser.add_argument("--dedupe", action="store_true", help="Resolve duplicate entities.")
    parser.add_argument("--prune", action="store_true", help="Purge orphan nodes and reclaim capacity.")
    parser.add_argument("--centrality", action="store_true", help="Score betweenness, degree spikes and anomalies.")
    parser.add_argument("--bridges", action="store_true", help="Report bridge candidates only (implies --centrality).")
    parser.add_argument("--capacity", action="store_true", help="Report node/edge utilisation and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Compute and report, write nothing to Neo4j.")
    parser.add_argument("--threshold", type=float, default=None, help=f"Fuzzy match threshold (default {FUZZY_THRESHOLD}).")
    parser.add_argument("--max-merges", type=int, default=None, help="Cap on merges per run (default from DEDUPE_MAX_MERGES).")
    parser.add_argument("--max-nodes", type=int, default=None, help="Centrality projection cap (default from CENTRALITY_MAX_NODES).")
    parser.add_argument("--top", type=int, default=None, help="How many top anomalies to report (default 5).")
    parser.add_argument("--engine", default="", choices=["", "auto", "gds", "python"], help="Betweenness engine.")
    parser.add_argument("--weights", default="", help="Anomaly weights as w1,w2,w3 (betweenness,degree_spike,offshore_ratio).")
    parser.add_argument("--report-dir", default="", help="Where to write the JSON report (default: REPORT_DIR).")
    parser.add_argument("--no-report", action="store_true", help="Do not write the report file.")
    parser.add_argument("--run-id", default="", help="Explicit run id (default: generated).")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON on stdout.")
    parser.add_argument("--log-level", default="", help="DEBUG | INFO | WARNING | ERROR.")
    parser.add_argument("--log-json", action="store_true", help="Emit structured JSON logs.")
    parser.add_argument("--version", action="store_true", help="Print version information and exit.")
    return parser


#: Canonical weight order for the positional ``--weights`` form.
WEIGHT_KEYS = ("betweenness", "degree_spike", "offshore_ratio")


def parse_weights(raw: str) -> dict[str, float] | None:
    """Parse ``--weights`` into the anomaly weight map; ``None`` when unparsable.

    Two accepted spellings, mirroring ``ANOMALY_WEIGHTS``::

        --weights 0.4,0.35,0.25
        --weights betweenness=0.4,degree_spike=0.35,offshore_ratio=0.25

    Values are normalised to sum to 1.0: they are a convex combination, so a
    typo in one weight changes the *relative* importance of the others rather
    than the scale of the score. ``None`` instead of an exception keeps ``main``
    in charge of the error message and the exit code.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip().strip("{}").replace(";", ",")
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return None

    values: dict[str, float] = {}
    try:
        if "=" in text:
            for part in parts:
                if "=" not in part:
                    return None
                key, _, value = part.partition("=")
                key = key.strip().strip("\"'").lower()
                if key not in WEIGHT_KEYS:
                    return None
                values[key] = max(0.0, float(value))
            if set(values) != set(WEIGHT_KEYS):
                return None
        else:
            if len(parts) != len(WEIGHT_KEYS):
                return None
            for key, part in zip(WEIGHT_KEYS, parts, strict=True):
                values[key] = max(0.0, float(part))
    except ValueError:
        return None

    total = sum(values.values())
    if total <= 0:
        return None
    return {key: round(values[key] / total, 6) for key in WEIGHT_KEYS}


def summarise(report: MaintenanceReport) -> str:
    """Human-readable summary for the console and the GitHub step summary."""
    capacity = report.capacity
    lines = [
        banner(f"graph maintenance — {report.status}"),
        f"run_id            : {report.run_id}",
        f"database          : {report.database or '(unset)'}{' [dry run]' if report.dry_run else ''}",
        f"seconds           : {report.seconds:.1f}",
        "",
        f"capacity          : {human_int(capacity.entities)}/{human_int(capacity.node_limit)} entities "
        f"({capacity.node_utilisation * 100:.1f}%), {human_int(capacity.all_edges)}/{human_int(capacity.edge_limit)} edges "
        f"({capacity.edge_utilisation * 100:.1f}%)",
        f"                    headroom {human_int(capacity.headroom_nodes)} nodes / "
        f"{human_int(capacity.headroom_edges)} edges — "
        f"{'ABOVE' if capacity.over_target else 'within'} the {capacity.target * 100:.0f}% target",
        "",
        f"dedupe            : {report.dedupe.status} — {report.dedupe.entities_examined} examined, "
        f"{report.dedupe.merges_applied} merged ({report.dedupe.mentions_repointed} mentions, "
        f"{report.dedupe.relationships_repointed} relationships repointed), "
        f"{report.dedupe.homonyms_protected} homonym pair(s) protected",
        f"prune             : {report.prune.status} — {report.prune.candidates} candidate(s), "
        f"{report.prune.purged} purged, {report.prune.protected_nodes} protected"
        f"{' (escalated thresholds)' if report.prune.escalated else ''}",
        f"centrality        : {report.centrality.status} — engine={report.centrality.engine}, "
        f"{report.centrality.nodes_scored} scored, {report.centrality.clusters} cluster(s), "
        f"{report.centrality.articulation_points} articulation point(s)",
        f"bridge alerts     : {len(report.bridge_alerts)}",
    ]
    if report.top_anomalies or report.centrality.top:
        lines.append("")
        lines.append("top anomalies     :")
        source = report.top_anomalies or report.centrality.top
        for entry in source[:5]:
            name = str(entry.get("name") or entry.get("canonical_key") or "?")
            score = float(entry.get("anomaly_score") or entry.get("score") or 0.0)
            lines.append(f"  {score:.3f}  {name}")
    if report.errors:
        lines.append("")
        lines.append("errors            :")
        lines.extend(f"  - {error}" for error in report.errors[:10])
    lines.append("=" * 78)
    return "\n".join(lines)


def emit_github_output(report: MaintenanceReport) -> None:
    """Publish the numbers the workflow needs, if we are running inside Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            # Namespaced: $GITHUB_OUTPUT is shared by every step in a job, and a
            # bare `status=` would collide with whatever else writes one.
            values = {
                "maintenance_status": report.status,
                "maintenance_merges": report.dedupe.merges_applied,
                "maintenance_homonyms_protected": report.dedupe.homonyms_protected,
                "maintenance_purged": report.prune.purged,
                "maintenance_prune_escalated": str(report.prune.escalated).lower(),
                "maintenance_scored": report.centrality.nodes_scored,
                "maintenance_engine": report.centrality.engine,
                "bridge_alerts": len(report.bridge_alerts),
                "node_utilisation": report.capacity.node_utilisation,
                "edge_utilisation": report.capacity.edge_utilisation,
                "maintenance_seconds": f"{report.seconds:.2f}",
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

        print(f"PuppetNET graph maintenance {__version__} (python {sys.version.split()[0]})")
        return EXIT_OK

    env_overrides: dict[str, str] = {}
    if args.dry_run:
        env_overrides["DRY_RUN"] = "true"
    if args.log_level:
        env_overrides["LOG_LEVEL"] = args.log_level.upper()
    if args.log_json:
        env_overrides["LOG_JSON"] = "true"
    if args.threshold is not None:
        env_overrides["DEDUPE_FUZZY_THRESHOLD"] = str(args.threshold)
    if args.max_merges is not None:
        env_overrides["DEDUPE_MAX_MERGES"] = str(args.max_merges)
    if args.max_nodes is not None:
        env_overrides["CENTRALITY_MAX_NODES"] = str(args.max_nodes)
    if args.top is not None:
        env_overrides["ANOMALY_TOP_N"] = str(args.top)
    if args.engine:
        env_overrides["CENTRALITY_ENGINE"] = args.engine
    if args.report_dir:
        env_overrides["REPORT_DIR"] = args.report_dir
    if args.run_id:
        env_overrides["RUN_ID"] = args.run_id

    try:
        settings = load_settings({**dict(os.environ), **env_overrides})
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    # With --json the only thing on stdout is the report: log lines go to stderr
    # so `graph_analytics.py --json | jq` works instead of dying on the banner.
    configure_logging(settings.log_level, settings.log_json, stream=sys.stderr if args.json else None)

    weights = parse_weights(args.weights)
    if args.weights and weights is None:
        print("--weights expects three numbers, e.g. --weights 0.4,0.35,0.25", file=sys.stderr)
        return EXIT_CONFIG
    if weights is not None:
        settings = _with_anomaly_weights(settings, weights)

    run_dedupe = bool(args.dedupe or args.all or not (args.prune or args.centrality or args.bridges or args.capacity))
    run_prune = bool(args.prune or args.all or not (args.dedupe or args.centrality or args.bridges or args.capacity))
    run_centrality = bool(args.centrality or args.bridges or args.all or not (args.dedupe or args.prune or args.capacity))

    try:
        engine = GraphMaintenance(settings, run_id=args.run_id)
    except Exception as exc:  # noqa: BLE001
        print(f"could not initialise the graph client: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        if args.capacity and not (args.dedupe or args.prune or args.centrality or args.bridges or args.all):
            capacity = engine.capacity()
            print(json.dumps(asdict(capacity), indent=2))
            return EXIT_OK

        with timed(logger, "graph maintenance"):
            report = engine.run(
                dedupe=run_dedupe,
                prune=run_prune,
                centrality=run_centrality,
                threshold=args.threshold,
                max_merges=args.max_merges,
                top=args.top,
            )

        if args.json:
            print(json.dumps(report.to_dict(), indent=2, default=str))
        else:
            print(summarise(report))

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


def _with_anomaly_weights(settings: Settings, weights: Mapping[str, float]) -> Settings:
    """Attach parsed CLI weights to the settings object without mutating globals."""
    try:
        import dataclasses  # noqa: PLC0415

        if any(field.name == "anomaly_weights" for field in dataclasses.fields(settings)):
            return dataclasses.replace(settings, anomaly_weights=dict(weights))
    except Exception:  # noqa: BLE001
        pass
    settings.anomaly_weights = dict(weights)
    return settings


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
