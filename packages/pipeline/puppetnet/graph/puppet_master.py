"""Cypher analytics: ``PUPPET_MASTER_OF``, ownership cycles and the free-tier guard.

Why this lives beside :mod:`puppetnet.graph.analytics`
------------------------------------------------------
``analytics.py`` scores influence in Python from the entities and relations the
pipeline already has in memory. That is the right design for the *ingestion*
path — it costs no extra database round-trips — but it can only see one run's
harvest. This module answers the same question **against the whole graph** and
writes the answer back with Cypher, which is what the spec asks for:

1. **PageRank** — a damped power iteration executed by the database
   (``PUPPET_MASTER_PAGERANK_INIT/STEP/COMMIT``). AuraDB Free has no GDS
   PageRank, so the iteration is plain Cypher over a bounded subgraph; the
   damping factor and iteration count are configuration, not magic numbers.
2. **Betweenness** — ``gds.betweenness.stream`` when the instance actually has
   GDS, otherwise Brandes over the same bounded edge set. Both are normalised
   to [0, 1] before they leave Python.
3. **In-degree** — counted in Cypher with ``count(DISTINCT r)``.
4. **The blend** — one Cypher statement normalises each component by its
   maximum and writes ``Entity.puppet_master_score``. The relationship
   ``(Person)-[:PUPPET_MASTER_OF]->(Entity)`` is then derived in Cypher from the
   influence score and the strength of the control edge. Nothing is computed
   in Python except betweenness (because a graph algorithm is not expressible
   in Cypher without GDS).

Circular ownership
------------------
A circular ownership structure is exactly a strongly connected component with
more than one member (or a self-loop). The detector loads the ownership edge
set in one bounded read and runs Tarjan's SCC in Python — exact, linear, and
free-tier friendly. The alternative, ``MATCH p=(a)-[*2..6]->(a)``, enumerates
every path and is the classic way to OOM a small instance. Each cycle is
reported with a concrete path and marked on its member nodes; stale marks are
cleared when a cycle is broken.

The 180 000-node stop
---------------------
AuraDB Free refuses writes at 200 000 nodes. :func:`capacity_guard` counts
**all** nodes (not only ``:Entity`` — documents and runs consume the same
budget) and reports ``stop_engaged`` at 90 % of the ceiling. The writer enforces
the same threshold for new entity nodes, and ``graph_analytics.py --guard``
exits with code 4 so a workflow can stop before the database does.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger, human_int
from ..models import RelationType, iso, utcnow
from .algorithms import brandes_betweenness, shortest_cycle_through, strongly_connected_components
from .neo4j_client import Neo4jClient, chunked

logger = get_logger("graph.puppet_master")

__all__ = [
    "AURA_NODE_LIMIT",
    "AURA_NODE_STOP",
    "OWNERSHIP_TYPES",
    "INVERSE_OWNERSHIP_TYPES",
    "CONTROL_TYPES",
    "CypherAnalytics",
    "PuppetMasterReport",
    "CycleReport",
    "CapacityGuardReport",
    "capacity_guard",
]

#: AuraDB Free ceilings. Kept here as well as in ``graph_analytics.py`` so the
#: package does not import the CLI script (which would be a circular import).
AURA_NODE_LIMIT = 200_000
AURA_EDGE_LIMIT = 400_000
#: 90 % of the node ceiling: the point at which new writes stop, leaving room
#: for the merges and repairs that are supposed to fix an over-full graph.
AURA_NODE_STOP = 180_000

#: Predicates that express ownership/control *in one direction*. A cycle over
#: these edges is a circular ownership structure regardless of which spelling
#: (``OWNS`` vs ``OWNED_BY``) each hop used.
OWNERSHIP_TYPES: tuple[str, ...] = (
    RelationType.OWNS.value,
    RelationType.OWNED_BY.value,
    RelationType.CONTROLS.value,
    RelationType.SUBSIDIARY_OF.value,
    RelationType.PARENT_OF.value,
    RelationType.ACQUIRED.value,
    RelationType.SHAREHOLDER_OF.value,
    RelationType.INTERMEDIARY_FOR.value,
    RelationType.NOMINEE_OF.value,
    RelationType.BENEFICIARY_OF.value,
    RelationType.TRUSTEE_OF.value,
)

#: Ownership predicates stored in the *inverse* direction: ``(child)-[:SUBSIDIARY_OF]->(parent)``
#: means "parent owns child". Direction is normalised before PageRank and cycle
#: detection, otherwise a legitimately written pair (A OWNS B, B SUBSIDIARY_OF A)
#: would look like a two-node ownership cycle.
INVERSE_OWNERSHIP_TYPES: tuple[str, ...] = (
    RelationType.OWNED_BY.value,
    RelationType.SUBSIDIARY_OF.value,
)

#: Edges that let a person *direct* an entity, used to derive PUPPET_MASTER_OF.
CONTROL_TYPES: tuple[str, ...] = (
    *OWNERSHIP_TYPES,
    RelationType.DIRECTOR_OF.value,
    RelationType.OFFICER_OF.value,
    RelationType.EMPLOYS.value,
    RelationType.FOUNDED.value,
    RelationType.APPOINTED_BY.value,
    RelationType.MEMBER_OF.value,
)

#: Every value above is from the closed enum; this guard makes an accidental
#: edit that interpolates something non-literal fail at import, not at runtime.
for _name in (*OWNERSHIP_TYPES, *CONTROL_TYPES):
    if not _name.replace("_", "").isalnum() or not _name.isupper():
        raise RuntimeError(f"unsafe relationship type in a Cypher pattern: {_name!r}")


# --------------------------------------------------------------------------- #
# Cypher
# --------------------------------------------------------------------------- #
GDS_PROBE = "RETURN gds.version() AS version"

#: The bounded analysis subgraph: the strongest (highest-degree) entities with
#: at least one relationship. AuraDB Free does not have the memory for a
#: 200 000-node PageRank, and the ranking of the top influencers does not
#: change because the thousandth leaf node was omitted.
SUBGRAPH_NODES = """
MATCH (n:Entity)
WHERE n.canonical_key IS NOT NULL
OPTIONAL MATCH (n)-[r]-()
WITH n, count(r) AS degree
WHERE degree > 0
RETURN n.canonical_key AS canonical_key,
       n.name AS name,
       n.entity_type AS entity_type,
       n.mention_count AS mention_count,
       degree
ORDER BY degree DESC, n.canonical_key
LIMIT $limit
"""

PAGERANK_INIT = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
OPTIONAL MATCH (n)-[r]->(m:Entity)
WHERE m.canonical_key IN $keys
WITH n, count(r) AS out_degree
SET n.pagerank = 1.0,
    n.pagerank_out_degree = out_degree,
    n.pagerank_run_id = $run_id
RETURN count(n) AS nodes
"""

#: One damped iteration. ``pagerank_next`` is read by the next step, so an
#: interrupted run leaves the previous complete score in ``pagerank`` (a torn
#: half-iteration is visible as ``pagerank_next`` without a commit, and the
#: next run overwrites it).
PAGERANK_STEP = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
OPTIONAL MATCH (m:Entity)-[r]->(n)
WHERE m.canonical_key IN $keys
WITH n, sum(CASE WHEN coalesce(m.pagerank_out_degree, 0) > 0
                 THEN coalesce(m.pagerank_next, m.pagerank, 0.0) / m.pagerank_out_degree
                 ELSE 0.0 END) AS incoming
SET n.pagerank_next = (1.0 - $damping) + $damping * incoming
RETURN count(n) AS nodes
"""

PAGERANK_COMMIT = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
SET n.pagerank = coalesce(n.pagerank_next, n.pagerank, 0.0),
    n.pagerank_iterations = $iterations,
    n.pagerank_computed_at = $computed_at
REMOVE n.pagerank_next
RETURN count(n) AS nodes
"""

ANALYSIS_EDGES = """
UNWIND $keys AS key
MATCH (a:Entity {canonical_key: key})-[r]->(b:Entity)
WHERE b.canonical_key IN $keys
  AND type(r) IN $types
WITH a, b, r,
     CASE WHEN type(r) IN $inverse_types THEN b ELSE a END AS owner,
     CASE WHEN type(r) IN $inverse_types THEN a ELSE b END AS owned
RETURN owner.canonical_key AS source,
       owned.canonical_key AS target,
       type(r) AS predicate,
       coalesce(r.confidence, 0.5) AS confidence
LIMIT $limit
"""

IN_DEGREE = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
OPTIONAL MATCH (m:Entity)-[r]->(n)
WHERE type(r) IN $types
RETURN n.canonical_key AS canonical_key, count(DISTINCT r) AS in_degree
"""

#: Betweenness has no Cypher equivalent on AuraDB Free; its values are written
#: back through this statement, and the *blend* below is Cypher.
WRITE_BETWEENNESS = """
UNWIND $rows AS row
MATCH (n:Entity {canonical_key: row.canonical_key})
SET n.betweenness = row.score,
    n.betweenness_engine = $engine,
    n.betweenness_run_id = $run_id
RETURN count(n) AS nodes
"""

BLEND_SCORE = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
WITH n,
     toFloat(coalesce(n.pagerank, 0.0)) AS pr,
     toFloat(coalesce(n.betweenness, 0.0)) AS bc,
     toFloat(coalesce(n.in_degree, 0.0)) AS idg
WITH collect({node: n, pr: pr, bc: bc, idg: idg}) AS rows,
     max(pr) AS max_pr, max(bc) AS max_bc, max(idg) AS max_id
UNWIND rows AS row
WITH row.node AS n, row.pr AS pr, row.bc AS bc, row.idg AS idg, max_pr, max_bc, max_id
SET n.puppet_master_components = {pagerank: pr, betweenness: bc, in_degree: idg},
    n.puppet_master_score = round((
        $weights['pagerank'] * CASE WHEN max_pr > 0.0 THEN pr / max_pr ELSE 0.0 END +
        $weights['betweenness'] * CASE WHEN max_bc > 0.0 THEN bc / max_bc ELSE 0.0 END +
        $weights['in_degree'] * CASE WHEN max_id > 0.0 THEN idg / max_id ELSE 0.0 END
    ) * 1000000.0) / 1000000.0,
    n.puppet_master_run_id = $run_id,
    n.puppet_master_computed_at = $computed_at
RETURN count(n) AS nodes
"""

#: The influence edge itself: a person with a high composite score and a strong
#: control edge becomes the puppet master of the target. Both halves of the
#: product are already in the graph, so this is a pure Cypher derivation.
PUPPET_MASTER_EDGES = """
MATCH (p:Person)-[ctl]->(t:Entity)
WHERE p.canonical_key IN $keys
  AND type(ctl) IN $control_types
  AND coalesce(ctl.confidence, 0.5) >= $min_confidence
// One row per (person, target): a person who is both DIRECTOR_OF and
// SHAREHOLDER_OF the same company must not consume two slots of $limit.
WITH p, t, collect(DISTINCT type(ctl)) AS predicates,
     max(coalesce(ctl.weight, ctl.confidence, 0.5)) AS strength
WITH p, t, predicates, strength, coalesce(p.puppet_master_score, 0.0) AS influence
WITH p, t, predicates, strength,
     round((0.65 * influence + 0.35 * strength) * 1000000.0) / 1000000.0 AS score
WHERE score >= $min_score
WITH p, t, predicates, strength, score, coalesce(p.puppet_master_components, {}) AS components
ORDER BY score DESC, p.canonical_key, t.canonical_key
LIMIT $limit
MERGE (p)-[r:PUPPET_MASTER_OF]->(t)
SET r.score = score,
    r.weight = score,
    r.confidence = strength,
    r.components = components,
    r.predicates = predicates,
    r.pagerank = coalesce(p.pagerank, 0.0),
    r.betweenness = coalesce(p.betweenness, 0.0),
    r.in_degree = coalesce(p.in_degree, 0),
    r.reasons = ['cypher_composite', 'control_edge'],
    r.evidence = 'control edge ' + predicates[0] + ' -> ' + coalesce(t.name, t.canonical_key),
    r.run_id = $run_id,
    r.computed_at = $computed_at,
    r.last_seen = $computed_at,
    r.first_seen = coalesce(r.first_seen, $computed_at)
RETURN count(r) AS edges
"""

TOP_SCORES = """
UNWIND $keys AS key
MATCH (n:Entity {canonical_key: key})
RETURN n.canonical_key AS canonical_key,
       n.name AS name,
       coalesce(n.puppet_master_score, 0.0) AS score,
       coalesce(n.puppet_master_components, {}) AS components,
       coalesce(n.in_degree, 0) AS in_degree
ORDER BY score DESC, canonical_key
LIMIT $limit
"""

OWNERSHIP_EDGES = """
MATCH (a:Entity)-[r]->(b:Entity)
WHERE a.canonical_key IS NOT NULL AND b.canonical_key IS NOT NULL
  AND type(r) IN $ownership_types
  AND coalesce(r.confidence, 0.5) >= $min_confidence
WITH a, b, r,
     CASE WHEN type(r) IN $inverse_types THEN b ELSE a END AS owner,
     CASE WHEN type(r) IN $inverse_types THEN a ELSE b END AS owned
RETURN owner.canonical_key AS source,
       owner.name AS source_name,
       owned.canonical_key AS target,
       owned.name AS target_name,
       type(r) AS predicate,
       coalesce(r.confidence, 0.5) AS confidence
LIMIT $limit
"""

MARK_CYCLES = """
UNWIND $rows AS row
UNWIND row.members AS key
MATCH (n:Entity {canonical_key: key})
SET n.ownership_cycle_id = row.cycle_id,
    n.ownership_cycle_length = row.length,
    n.ownership_cycle_detected_at = $detected_at
RETURN count(n) AS nodes
"""

CLEAR_STALE_CYCLES = """
MATCH (n:Entity)
WHERE n.ownership_cycle_id IS NOT NULL
  AND NOT n.ownership_cycle_id IN $cycle_ids
SET n.ownership_cycle_id = null,
    n.ownership_cycle_length = null,
    n.ownership_cycle_detected_at = null
RETURN count(n) AS cleared
"""

NODE_COUNT = """
MATCH (n)
RETURN count(n) AS nodes
"""

ENTITY_COUNT = """
MATCH (e:Entity)
RETURN count(e) AS entities
"""

EDGE_COUNT = """
MATCH ()-[r]->()
RETURN count(r) AS edges
"""


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@dataclass
class PuppetMasterReport:
    run_id: str
    generated_at: str
    dry_run: bool = False
    status: str = "completed"
    engine: str = "cypher"
    gds_version: str = ""
    nodes_considered: int = 0
    edges_considered: int = 0
    iterations: int = 0
    damping: float = 0.85
    betweenness_engine: str = ""
    scored_nodes: int = 0
    edges_written: int = 0
    weights: dict[str, float] = field(default_factory=dict)
    top: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CycleReport:
    run_id: str
    generated_at: str
    dry_run: bool = False
    status: str = "completed"
    edges_examined: int = 0
    nodes_examined: int = 0
    components: int = 0
    cycles: int = 0
    marked_nodes: int = 0
    cleared_nodes: int = 0
    length_histogram: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapacityGuardReport:
    """Free-tier headroom, measured against **all** nodes and relationships."""

    checked_at: str
    dry_run: bool = False
    nodes: int = 0
    entities: int = 0
    edges: int = 0
    node_limit: int = AURA_NODE_LIMIT
    edge_limit: int = AURA_EDGE_LIMIT
    stop_threshold: int = AURA_NODE_STOP
    node_utilisation: float = 0.0
    edge_utilisation: float = 0.0
    headroom_nodes: int = AURA_NODE_LIMIT
    headroom_to_stop: int = AURA_NODE_STOP
    stop_engaged: bool = False
    at_hard_limit: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class CypherAnalytics:
    """PageRank + betweenness + in-degree → ``PUPPET_MASTER_OF``, in Cypher.

    Constructed with a :class:`~puppetnet.graph.neo4j_client.Neo4jClient` (or a
    double exposing ``read``/``write``/``dry_run``), so every statement is
    assertable as text and the Python-side algorithm (SCC, Brandes) is testable
    on hand-built graphs.
    """

    def __init__(
        self,
        settings: Any,
        client: Neo4jClient | None = None,
        *,
        run_id: str = "",
        owns_client: bool = False,
    ) -> None:
        self.settings = settings
        self.owns_client = owns_client or client is None
        self.client = client if client is not None else Neo4jClient(settings)
        self.run_id = run_id or getattr(settings, "run_id", "") or f"cypher-{utcnow():%Y%m%dT%H%M%SZ}"
        self.gds_version = ""

    # -- lifecycle ------------------------------------------------------
    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.client, "dry_run", False))

    def close(self) -> None:
        if self.owns_client:
            with contextlib.suppress(Exception):
                self.client.close()

    def select_subgraph(self, limit: int | None = None) -> list[dict[str, Any]]:
        cap = int(limit if limit is not None else getattr(self.settings, "puppet_master_max_nodes", 5_000))
        rows = self.client.read(SUBGRAPH_NODES, {"limit": max(2, cap)}) or []
        return [dict(row) for row in rows if row.get("canonical_key")]

    # -- PageRank -------------------------------------------------------
    def pagerank(self, keys: Sequence[str], *, iterations: int | None = None, damping: float | None = None) -> int:
        """Run the damped power iteration; returns the number of committed nodes."""
        if not keys:
            return 0
        rounds = int(iterations if iterations is not None else getattr(self.settings, "puppet_master_pagerank_iterations", 12))
        factor = float(damping if damping is not None else getattr(self.settings, "puppet_master_damping", 0.85))
        payload = list(keys)
        self.client.write(PAGERANK_INIT, {"keys": payload, "run_id": self.run_id}, rows=len(payload), kind="analytics")
        for _round in range(max(1, rounds)):
            self.client.write(PAGERANK_STEP, {"keys": payload, "damping": factor}, rows=len(payload), kind="analytics")
        self.client.write(
            PAGERANK_COMMIT,
            {"keys": payload, "iterations": max(1, rounds), "computed_at": iso(utcnow())},
            rows=len(payload),
            kind="analytics",
        )
        return len(payload)

    # -- betweenness ----------------------------------------------------
    def analysis_edges(self, keys: Sequence[str], *, limit: int | None = None) -> list[dict[str, Any]]:
        cap = int(limit if limit is not None else getattr(self.settings, "analytics_graph_edge_limit", 50_000))
        rows = self.client.read(
            ANALYSIS_EDGES,
            {
                "keys": list(keys),
                "types": list(CONTROL_TYPES),
                "inverse_types": list(INVERSE_OWNERSHIP_TYPES),
                "limit": max(1, cap),
            },
        ) or []
        return [dict(row) for row in rows if row.get("source") and row.get("target")]

    @staticmethod
    def _adjacency(nodes: Sequence[str], edges: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[str, float]]]:
        adjacency: dict[str, list[tuple[str, float]]] = {node: [] for node in nodes}
        for edge in edges:
            source, target = str(edge["source"]), str(edge["target"])
            adjacency.setdefault(source, []).append((target, float(edge.get("confidence") or 0.5)))
            adjacency.setdefault(target, []).append((source, float(edge.get("confidence") or 0.5)))
        return adjacency

    def gds_betweenness(self, keys: Sequence[str]) -> dict[str, float] | None:
        """GDS betweenness when the instance has it (AuraDB Free does not)."""
        if self.dry_run or getattr(self.settings, "centrality_engine", "auto") == "python":
            return None
        graph_name = f"puppetnet_master_{int(time.time())}"
        try:
            rows = self.client.read(GDS_PROBE)
            self.gds_version = str((rows or [{}])[0].get("version") or "")
        except Exception as exc:  # noqa: BLE001
            logger.info("GDS unavailable (%s) — using Brandes", exc.__class__.__name__)
            return None
        try:
            self.client.write(
                "CALL gds.graph.project($graphName, $nodeLabel, {ALL: {type: '*', orientation: 'UNDIRECTED'}})",
                {"graphName": graph_name, "nodeLabel": "Entity"},
                kind="analytics",
            )
            rows = self.client.read(
                "CALL gds.betweenness.stream($graphName) YIELD nodeId, score "
                "RETURN gds.util.asNode(nodeId).canonical_key AS canonical_key, score AS score",
                {"graphName": graph_name},
            ) or []
            scores = {str(row["canonical_key"]): float(row.get("score") or 0.0) for row in rows if row.get("canonical_key")}
            if not scores:
                return None
            allowed = set(keys)
            scores = {key: value for key, value in scores.items() if key in allowed}
            peak = max(scores.values()) or 1.0
            return {key: value / peak for key, value in scores.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("GDS betweenness failed (%s) — using Brandes", exc)
            return None
        finally:
            with contextlib.suppress(Exception):
                self.client.write("CALL gds.graph.drop($graphName)", {"graphName": graph_name}, kind="analytics")

    def betweenness(self, keys: Sequence[str], edges: Sequence[Mapping[str, Any]]) -> tuple[dict[str, float], str]:
        scores = self.gds_betweenness(keys)
        if scores is not None:
            return scores, "gds"
        adjacency = self._adjacency(keys, edges)
        return brandes_betweenness(list(keys), adjacency), "brandes"

    def write_betweenness(self, scores: Mapping[str, float], *, engine: str) -> int:
        if not scores:
            return 0
        rows = [{"canonical_key": key, "score": float(score)} for key, score in scores.items()]
        written = 0
        for batch in chunked(rows, max(1, int(getattr(self.settings, "neo4j_batch_size", 500) or 500))):
            self.client.write(
                WRITE_BETWEENNESS,
                {"rows": batch, "engine": engine, "run_id": self.run_id},
                rows=len(batch),
                kind="analytics",
            )
            written += len(batch)
        return written

    def in_degree(self, keys: Sequence[str]) -> dict[str, int]:
        rows = self.client.read(IN_DEGREE, {"keys": list(keys), "types": list(CONTROL_TYPES)}) or []
        scores = {str(row.get("canonical_key")): int(row.get("in_degree") or 0) for row in rows if row.get("canonical_key")}
        payload = [{"canonical_key": key, "in_degree": score} for key, score in scores.items()]
        if payload:
            for batch in chunked(payload, max(1, int(getattr(self.settings, "neo4j_batch_size", 500) or 500))):
                self.client.write(
                    "UNWIND $rows AS row MATCH (n:Entity {canonical_key: row.canonical_key}) "
                    "SET n.in_degree = row.in_degree RETURN count(n) AS nodes",
                    {"rows": batch},
                    rows=len(batch),
                    kind="analytics",
                )
        return scores

    def blend(self, keys: Sequence[str]) -> dict[str, float]:
        weights = {
            "pagerank": float(getattr(self.settings, "puppet_master_pagerank_weight", 0.5)),
            "betweenness": float(getattr(self.settings, "puppet_master_betweenness_weight", 0.3)),
            "in_degree": float(getattr(self.settings, "puppet_master_in_degree_weight", 0.2)),
        }
        total = sum(weights.values()) or 1.0
        weights = {key: round(value / total, 6) for key, value in weights.items()}
        self.client.write(
            BLEND_SCORE,
            {"keys": list(keys), "weights": weights, "run_id": self.run_id, "computed_at": iso(utcnow())},
            rows=len(keys),
            kind="analytics",
        )
        return weights

    def write_edges(self, keys: Sequence[str]) -> int:
        rows = self.client.write(
            PUPPET_MASTER_EDGES,
            {
                "keys": list(keys),
                "control_types": list(CONTROL_TYPES),
                "min_confidence": float(getattr(self.settings, "min_edge_confidence", 0.0)),
                "min_score": float(getattr(self.settings, "puppet_master_min_score", 0.40)),
                "limit": int(getattr(self.settings, "puppet_master_max_edges", 2_000)),
                "run_id": self.run_id,
                "computed_at": iso(utcnow()),
            },
            kind="analytics",
        ) or []
        if rows:
            return int(rows[0].get("edges") or 0)
        # A dry-run client records the statement and returns []; the planned
        # edge count is unknowable without executing the MERGE, so zero is the
        # honest answer.
        return 0

    def top_scores(self, keys: Sequence[str], *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.client.read(TOP_SCORES, {"keys": list(keys), "limit": max(1, limit)}) or []
        return [dict(row) for row in rows]

    def run(self, *, limit: int | None = None, top: int = 10) -> PuppetMasterReport:
        started = time.perf_counter()
        report = PuppetMasterReport(run_id=self.run_id, generated_at=iso(utcnow()) or "", dry_run=self.dry_run)
        if not bool(getattr(self.settings, "puppet_master_cypher_enabled", True)):
            report.status = "disabled"
            report.engine = "disabled"
            logger.info("Cypher PUPPET_MASTER_OF computation disabled by configuration")
            return report
        try:
            nodes = self.select_subgraph(limit)
            report.nodes_considered = len(nodes)
            keys = [str(node["canonical_key"]) for node in nodes]
            if len(keys) < 2:
                report.status = "empty"
                return report

            report.iterations = int(getattr(self.settings, "puppet_master_pagerank_iterations", 12))
            report.damping = float(getattr(self.settings, "puppet_master_damping", 0.85))
            self.pagerank(keys)

            edges = self.analysis_edges(keys)
            report.edges_considered = len(edges)
            scores, engine = self.betweenness(keys, edges)
            report.betweenness_engine = engine
            report.gds_version = self.gds_version
            self.write_betweenness(scores, engine=engine)
            self.in_degree(keys)
            report.weights = self.blend(keys)
            report.scored_nodes = len(keys)
            report.edges_written = self.write_edges(keys)
            report.top = self.top_scores(keys, limit=max(1, top))
        except Exception as exc:  # noqa: BLE001 - one stage must not kill the report
            report.status = "failed"
            report.errors.append(f"{exc.__class__.__name__}: {exc}")
            logger.error("Cypher PUPPET_MASTER_OF computation failed: %s", exc)
        finally:
            report.seconds = round(time.perf_counter() - started, 3)
        logger.info(
            "puppet master (cypher): %s — %d node(s), %d edge(s), %d scored via %s, %d PUPPET_MASTER_OF edge(s)",
            report.status, report.nodes_considered, report.edges_considered,
            report.scored_nodes, report.betweenness_engine or "n/a", report.edges_written,
        )
        return report

    # -- ownership cycles ----------------------------------------------
    def ownership_edges(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        cap = int(limit if limit is not None else getattr(self.settings, "cycle_edge_limit", 50_000))
        rows = self.client.read(
            OWNERSHIP_EDGES,
            {
                "ownership_types": list(OWNERSHIP_TYPES),
                "inverse_types": list(INVERSE_OWNERSHIP_TYPES),
                "min_confidence": float(getattr(self.settings, "min_edge_confidence", 0.0)),
                "limit": max(1, cap),
            },
        ) or []
        return [dict(row) for row in rows if row.get("source") and row.get("target")]

    @staticmethod
    def _edge_index(edges: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], list[str]]:
        index: dict[tuple[str, str], list[str]] = defaultdict(list)
        for edge in edges:
            index[(str(edge["source"]), str(edge["target"]))].append(str(edge.get("predicate") or ""))
        return index

    @staticmethod
    def _cycle_id(members: Sequence[str]) -> str:
        digest = hashlib.sha1("|".join(sorted(members)).encode("utf-8")).hexdigest()  # noqa: S324 - not a security hash
        return f"cycle-{digest[:16]}"

    def detect_cycles(self, *, limit: int | None = None) -> CycleReport:
        """Find circular ownership structures exactly, via SCCs + a concrete path."""
        started = time.perf_counter()
        report = CycleReport(run_id=self.run_id, generated_at=iso(utcnow()) or "", dry_run=self.dry_run)
        if not bool(getattr(self.settings, "cycle_detection_enabled", True)):
            report.status = "disabled"
            return report
        try:
            edges = self.ownership_edges(limit=limit)
            report.edges_examined = len(edges)
            nodes = sorted({str(edge["source"]) for edge in edges} | {str(edge["target"]) for edge in edges})
            report.nodes_examined = len(nodes)
            adjacency: dict[str, list[str]] = {node: [] for node in nodes}
            for edge in edges:
                adjacency.setdefault(str(edge["source"]), []).append(str(edge["target"]))
            max_length = int(getattr(self.settings, "cycle_max_length", 12))
            starts = int(getattr(self.settings, "cycle_max_starts", 2_000))
            components = strongly_connected_components(nodes, adjacency)
            cyclic = [
                component
                for component in components
                if len(component) > 1 or (component and component[0] in adjacency.get(component[0], ()))
            ][:starts]
            report.components = len(cyclic)
            predicates = self._edge_index(edges)
            rows: list[dict[str, Any]] = []
            histogram: dict[str, int] = defaultdict(int)
            for component in cyclic:
                path = shortest_cycle_through(component, adjacency, max_length=max_length)
                if not path:
                    continue
                length = len(path) - 1
                histogram[str(length)] += 1
                hops = [
                    {
                        "source": path[index],
                        "target": path[index + 1],
                        "predicates": predicates.get((path[index], path[index + 1]), []),
                    }
                    for index in range(len(path) - 1)
                ]
                rows.append(
                    {
                        "cycle_id": self._cycle_id(component),
                        "members": list(component),
                        "path": path,
                        "length": length,
                        "nodes": len(component),
                        "hops": hops,
                    }
                )
            report.rows = rows
            report.cycles = len(rows)
            report.length_histogram = dict(sorted(histogram.items(), key=lambda item: int(item[0])))
            if rows:
                marked = 0
                for batch in chunked(rows, max(1, int(getattr(self.settings, "neo4j_batch_size", 500) or 500))):
                    self.client.write(
                        MARK_CYCLES,
                        {"rows": batch, "detected_at": iso(utcnow())},
                        rows=sum(len(row["members"]) for row in batch),
                        kind="maintenance",
                    )
                    marked += sum(len(row["members"]) for row in batch)
                report.marked_nodes = 0 if self.dry_run else marked
            if not self.dry_run:
                cap = int(limit if limit is not None else getattr(self.settings, "cycle_edge_limit", 50_000))
                if len(edges) >= cap:
                    # A truncated scan cannot prove that a mark is stale: keep it.
                    logger.warning("ownership scan hit its %d-edge limit — stale cycle marks were left in place", cap)
                else:
                    try:
                        cleared = self.client.write(
                            CLEAR_STALE_CYCLES,
                            {"cycle_ids": [row["cycle_id"] for row in rows]},
                            kind="maintenance",
                        ) or []
                        report.cleared_nodes = sum(int(row.get("cleared") or 0) for row in cleared)
                    except Exception as exc:  # noqa: BLE001 - housekeeping, not correctness
                        logger.warning("could not clear stale ownership-cycle marks: %s", exc)
            logger.info(
                "ownership cycles: %d edge(s), %d component(s), %d cycle(s) %s",
                report.edges_examined, report.components, report.cycles,
                dict(report.length_histogram),
            )
        except Exception as exc:  # noqa: BLE001
            report.status = "failed"
            report.errors.append(f"{exc.__class__.__name__}: {exc}")
            logger.error("ownership-cycle detection failed: %s", exc)
        finally:
            report.seconds = round(time.perf_counter() - started, 3)
        return report

    def write_report(self, report: PuppetMasterReport | CycleReport, directory: str | Path | None = None) -> Path:
        target = Path(directory or getattr(self.settings, "report_dir", "reports") or "reports")
        target.mkdir(parents=True, exist_ok=True)
        kind = "puppet_master" if isinstance(report, PuppetMasterReport) else "ownership_cycles"
        path = target / f"{kind}_{report.run_id}.json"
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        logger.info("%s report written to %s", kind, path)
        return path


# --------------------------------------------------------------------------- #
# Free-tier guard
# --------------------------------------------------------------------------- #
def capacity_guard(client: Any, settings: Any, *, run_id: str = "") -> CapacityGuardReport:
    """Measure all-node headroom and decide whether the free-tier stop applies.

    ``stop_engaged`` is true at ``AURA_NODE_STOP`` (180 000 by default) — that is
    the *early* stop. ``at_hard_limit`` is true at the physical ceiling
    (200 000), where every write, including a repair, is refused by Aura.
    """
    del run_id
    report = CapacityGuardReport(
        checked_at=iso(utcnow()) or "",
        dry_run=bool(getattr(client, "dry_run", False)),
        stop_threshold=int(getattr(settings, "aura_node_stop", AURA_NODE_STOP) or AURA_NODE_LIMIT),
        node_limit=int(getattr(settings, "aura_node_cap", AURA_NODE_LIMIT) or AURA_NODE_LIMIT),
        edge_limit=int(getattr(settings, "aura_edge_cap", AURA_EDGE_LIMIT) or AURA_EDGE_LIMIT),
    )
    try:
        nodes = client.read(NODE_COUNT) or []
        entities = client.read(ENTITY_COUNT) or []
        edges = client.read(EDGE_COUNT) or []
        report.nodes = int((nodes[0] if nodes else {}).get("nodes") or 0)
        report.entities = int((entities[0] if entities else {}).get("entities") or 0)
        report.edges = int((edges[0] if edges else {}).get("edges") or 0)
    except Exception as exc:  # noqa: BLE001
        report.error = f"{exc.__class__.__name__}: {exc}"
        logger.warning("capacity guard probe failed: %s", exc)
        return report
    report.node_utilisation = round(report.nodes / report.node_limit, 4) if report.node_limit else 0.0
    report.edge_utilisation = round(report.edges / report.edge_limit, 4) if report.edge_limit else 0.0
    report.headroom_nodes = max(0, report.node_limit - report.nodes)
    report.headroom_to_stop = max(0, report.stop_threshold - report.nodes)
    report.stop_engaged = report.nodes >= report.stop_threshold
    report.at_hard_limit = report.nodes >= report.node_limit
    logger.info(
        "free-tier guard: %s/%s nodes (%.1f%%), stop at %s — %s; %s entities",
        human_int(report.nodes), human_int(report.node_limit), report.node_utilisation * 100,
        human_int(report.stop_threshold), "ENGAGED" if report.stop_engaged else "clear",
        human_int(report.entities),
    )
    return report
