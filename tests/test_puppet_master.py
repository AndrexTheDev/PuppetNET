"""Cypher analytics: PageRank, betweenness blend, ownership cycles, free-tier guard.

Every statement is asserted as text against a scripted client. The graph
algorithms (Tarjan SCC, Brandes betweenness) are exercised on hand-built graphs
whose answers are known by inspection, and the Cypher statements are checked for
the clauses that carry the contract — ``MERGE (p)-[r:PUPPET_MASTER_OF]->(t)``,
the weight parameters, the 180 000-node stop.
"""

from __future__ import annotations

import dataclasses
import json

import graph_analytics as ga
from puppetnet.config import Settings
from puppetnet.graph import puppet_master as pm


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class StubClient:
    def __init__(self, *, reads=None, write_results=None, dry_run: bool = False) -> None:
        self.reads = dict(reads or {})
        self.write_results = dict(write_results or {})
        self.dry_run = dry_run
        self.read_queries: list[str] = []
        self.read_params: list[dict] = []
        self.writes: list[tuple[str, dict, str]] = []

    def read(self, query, params=None):
        self.read_queries.append(query)
        self.read_params.append(dict(params or {}))
        for fragment, rows in self.reads.items():
            if fragment in query:
                return list(rows)
        return []

    def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
        self.writes.append((query, dict(params or {}), kind))
        for fragment, result in self.write_results.items():
            if fragment in query:
                return list(result)
        return []


def analytics(settings, client, **overrides):
    base = {
        "centrality_engine": "python",
        "puppet_master_max_nodes": 100,
        "puppet_master_pagerank_iterations": 3,
        "neo4j_batch_size": 50,
    }
    base.update(overrides)
    return pm.CypherAnalytics(dataclasses.replace(settings, **base), client, run_id="cypher-test")


def edge(source, target, predicate="OWNS", confidence=0.9):
    return {
        "source": source,
        "target": target,
        "predicate": predicate,
        "confidence": confidence,
        "source_name": source.title(),
        "target_name": target.title(),
    }


# --------------------------------------------------------------------------- #
# Algorithms
# --------------------------------------------------------------------------- #
def test_scc_finds_a_three_cycle_and_ignores_the_tail():
    adjacency = {"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]}
    components = pm.strongly_connected_components(["a", "b", "c", "d"], adjacency)
    assert components[0] == ["a", "b", "c"]
    assert ["d"] in components


def test_scc_handles_a_self_loop():
    components = pm.strongly_connected_components(["a", "b"], {"a": ["a"], "b": []})
    assert ["a"] in components


def test_shortest_cycle_returns_a_closed_auditable_path():
    adjacency = {"a": ["b"], "b": ["c"], "c": ["a"]}
    path = pm.shortest_cycle_through(["a", "b", "c"], adjacency)
    assert path == ["a", "b", "c", "a"]


def test_brandes_puts_the_barbell_bridge_at_the_top():
    nodes = ["a1", "a2", "a3", "b1", "b2", "b3"]
    adjacency = {node: [] for node in nodes}
    for group in (["a1", "a2", "a3"], ["b1", "b2", "b3"]):
        for index, first in enumerate(group):
            for second in group[index + 1:]:
                adjacency[first].append((second, 1.0))
                adjacency[second].append((first, 1.0))
    adjacency["a3"].append(("b1", 1.0))
    adjacency["b1"].append(("a3", 1.0))
    scores = pm.brandes_betweenness(nodes, adjacency)
    assert max(scores, key=scores.get) in {"a3", "b1"}
    assert all(0.0 <= value <= 1.0 for value in scores.values())


# --------------------------------------------------------------------------- #
# PageRank / betweenness / in-degree / blend
# --------------------------------------------------------------------------- #
def test_pagerank_runs_init_steps_and_commit_in_cypher(settings):
    client = StubClient()
    engine = analytics(settings, client)
    assert engine.pagerank(["A", "B"], iterations=4, damping=0.8) == 2
    assert any("SET n.pagerank = 1.0" in query for query, _, _ in client.writes)
    steps = [entry for entry in client.writes if "SET n.pagerank_next" in entry[0]]
    assert len(steps) == 4
    assert all(entry[1]["damping"] == 0.8 for entry in steps)
    assert any("SET n.pagerank = coalesce(n.pagerank_next" in query for query, _, _ in client.writes)
    assert all(entry[2] == "analytics" for entry in client.writes)


def test_pagerank_iteration_uses_out_degree_not_raw_neighbour_rank(settings):
    assert "m.pagerank_out_degree" in pm.PAGERANK_STEP
    assert "pagerank_next" in pm.PAGERANK_STEP
    # The degree counts only edges inside the bounded projection; an edge to a
    # node that is never iterated would otherwise absorb rank silently.
    assert "WHERE m.canonical_key IN $keys" in pm.PAGERANK_INIT


def test_ownership_direction_is_normalised_before_scoring(settings):
    """``(child)-[:SUBSIDIARY_OF]->(parent)`` must rank like ``parent OWNS child``."""
    client = StubClient()
    engine = analytics(settings, client)
    engine.analysis_edges(["A", "B"])
    assert "CASE WHEN type(r) IN $inverse_types" in pm.ANALYSIS_EDGES
    assert client.read_params[-1]["inverse_types"] == ["OWNED_BY", "SUBSIDIARY_OF"]

    engine.ownership_edges()
    assert "CASE WHEN type(r) IN $inverse_types" in pm.OWNERSHIP_EDGES
    assert client.read_params[-1]["inverse_types"] == ["OWNED_BY", "SUBSIDIARY_OF"]


def test_betweenness_falls_back_to_brandes_without_gds(settings):
    client = StubClient(
        reads={
            "owner.canonical_key AS source": [
                {"source": "a", "target": "b", "predicate": "OWNS", "confidence": 0.9},
                {"source": "b", "target": "c", "predicate": "OWNS", "confidence": 0.9},
            ]
        }
    )
    engine = analytics(settings, client)
    scores, engine_name = engine.betweenness(["a", "b", "c"], engine.analysis_edges(["a", "b", "c"]))
    assert engine_name == "brandes"
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]


def test_gds_probe_failure_is_not_an_error(settings):
    class NoGds(StubClient):
        def read(self, query, params=None):
            if "gds.version" in query:
                raise RuntimeError("no gds")
            return super().read(query, params)

    engine = analytics(settings, NoGds(), centrality_engine="auto")
    assert engine.gds_betweenness(["a", "b"]) is None


def test_betweenness_is_written_back_before_the_blend(settings):
    client = StubClient()
    engine = analytics(settings, client)
    written = engine.write_betweenness({"A": 0.5, "B": 0.25}, engine="brandes")
    assert written == 2
    query, params, kind = client.writes[0]
    assert "SET n.betweenness = row.score" in query
    assert params["engine"] == "brandes"
    assert kind == "analytics"


def test_in_degree_is_counted_in_cypher_and_stored(settings):
    client = StubClient(reads={"count(DISTINCT r) AS in_degree": [{"canonical_key": "A", "in_degree": 7}]})
    engine = analytics(settings, client)
    assert engine.in_degree(["A"]) == {"A": 7}
    assert any("SET n.in_degree = row.in_degree" in query for query, _, _ in client.writes)
    assert any("CONTROLS" in str(params.get("types")) for params in client.read_params)


def test_blend_normalises_the_configured_weights(settings):
    client = StubClient()
    engine = analytics(
        settings,
        client,
        puppet_master_pagerank_weight=2.0,
        puppet_master_betweenness_weight=1.0,
        puppet_master_in_degree_weight=1.0,
    )
    weights = engine.blend(["A"])
    assert weights == {"pagerank": 0.5, "betweenness": 0.25, "in_degree": 0.25}
    query, params, _ = client.writes[0]
    assert "max_pr" in query and "max_bc" in query and "max_id" in query
    assert params["weights"] == weights


def test_puppet_master_edges_are_derived_in_cypher(settings):
    client = StubClient(write_results={"MERGE (p)-[r:PUPPET_MASTER_OF]->(t)": [{"edges": 4}]})
    engine = analytics(settings, client)
    assert engine.write_edges(["A", "B"]) == 4
    query, params, _ = client.writes[0]
    assert "MATCH (p:Person)-[ctl]->(t:Entity)" in query
    assert "MERGE (p)-[r:PUPPET_MASTER_OF]->(t)" in query
    assert "0.65 * influence + 0.35 * strength" in query
    assert params["control_types"][0] == "OWNS"
    assert params["min_score"] == 0.40


def test_run_produces_a_report_with_top_scores(settings):
    nodes = [
        {"canonical_key": "A", "name": "Alpha", "entity_type": "Organization", "mention_count": 5, "degree": 9},
        {"canonical_key": "B", "name": "Beta", "entity_type": "Person", "mention_count": 3, "degree": 4},
    ]
    client = StubClient(
        reads={
            "ORDER BY degree DESC": nodes,
            "owner.canonical_key AS source": [{"source": "B", "target": "A", "predicate": "OWNS", "confidence": 0.9}],
            "count(DISTINCT r) AS in_degree": [{"canonical_key": "A", "in_degree": 1}, {"canonical_key": "B", "in_degree": 0}],
            "n.puppet_master_score": [
                {"canonical_key": "A", "name": "Alpha", "score": 0.9, "components": {"pagerank": 1.0}, "in_degree": 1}
            ],
        },
        write_results={"MERGE (p)-[r:PUPPET_MASTER_OF]->(t)": [{"edges": 2}]},
    )
    report = analytics(settings, client).run(top=5)
    assert report.status == "completed"
    assert report.nodes_considered == 2
    assert report.iterations == 3
    assert report.betweenness_engine == "brandes"
    assert report.scored_nodes == 2
    assert report.edges_written == 2
    assert report.top[0]["canonical_key"] == "A"
    assert report.weights == {"pagerank": 0.5, "betweenness": 0.3, "in_degree": 0.2}


def test_run_is_disabled_by_configuration(settings):
    report = analytics(settings, StubClient(), puppet_master_cypher_enabled=False).run()
    assert report.status == "disabled"
    assert report.edges_written == 0


def test_run_reports_failures_instead_of_raising(settings):
    class Dead(StubClient):
        def read(self, query, params=None):
            raise RuntimeError("database unreachable")

    report = analytics(settings, Dead()).run()
    assert report.status == "failed"
    assert "database unreachable" in report.errors[0]


# --------------------------------------------------------------------------- #
# Ownership cycles
# --------------------------------------------------------------------------- #
def ownership_client(*, rows=None, limit_edges=False):
    rows = rows if rows is not None else [
        edge("A", "B"),
        edge("B", "C"),
        edge("C", "A"),
        edge("D", "E", predicate="SUBSIDIARY_OF"),
    ]
    return StubClient(
        reads={"owner.name AS source_name": rows},
        write_results={"RETURN count(n) AS nodes": [{"nodes": 3}], "RETURN count(n) AS cleared": [{"cleared": 0}]},
    )


def test_detects_a_circular_ownership_structure(settings):
    client = ownership_client()
    report = analytics(settings, client).detect_cycles()

    assert report.status == "completed"
    assert report.edges_examined == 4
    assert report.components == 1
    assert report.cycles == 1
    cycle = report.rows[0]
    assert cycle["length"] == 3
    assert cycle["members"] == ["A", "B", "C"]
    assert cycle["path"][0] == cycle["path"][-1]
    assert {hop["predicates"][0] for hop in cycle["hops"]} == {"OWNS"}
    assert cycle["cycle_id"].startswith("cycle-")


def test_a_relationship_written_in_both_directions_is_not_a_cycle(settings):
    """A OWNS B and B SUBSIDIARY_OF A normalise to the same A→B edge."""
    rows = [edge("A", "B", "OWNS"), edge("A", "B", "SUBSIDIARY_OF")]
    report = analytics(settings, ownership_client(rows=rows)).detect_cycles()
    assert report.components == 0
    assert report.cycles == 0


def test_cycle_marks_are_written_to_the_member_nodes(settings):
    client = ownership_client()
    analytics(settings, client).detect_cycles()
    marks = [entry for entry in client.writes if "SET n.ownership_cycle_id" in entry[0]]
    assert marks
    assert set(marks[0][1]["rows"][0]["members"]) == {"A", "B", "C"}


def test_stale_cycle_marks_are_cleared(settings):
    client = ownership_client()
    analytics(settings, client).detect_cycles()
    clears = [entry for entry in client.writes if "ownership_cycle_id IS NOT NULL" in entry[0]]
    assert clears, "a broken cycle must lose its mark"
    assert clears[0][1]["cycle_ids"][0].startswith("cycle-")


def test_a_truncated_scan_never_clears_marks(settings):
    rows = [edge("A", "B"), edge("B", "A")]
    client = ownership_client(rows=rows)
    analytics(settings, client).detect_cycles(limit=2)
    assert not any("ownership_cycle_id IS NOT NULL" in query for query, _, _ in client.writes)


def test_dry_run_reports_the_cycle_but_marks_nothing(settings):
    client = ownership_client()
    client.dry_run = True
    report = analytics(settings, client).detect_cycles()
    assert report.cycles == 1
    assert report.marked_nodes == 0
    assert report.dry_run is True


def test_cycle_detection_can_be_disabled(settings):
    report = analytics(settings, StubClient(), cycle_detection_enabled=False).detect_cycles()
    assert report.status == "disabled"


# --------------------------------------------------------------------------- #
# Free-tier guard
# --------------------------------------------------------------------------- #
def guard_client(*, nodes: int, entities: int = 0, edges: int = 0):
    return StubClient(
        reads={
            "MATCH (n)\nRETURN count(n) AS nodes": [{"nodes": nodes}],
            "MATCH (e:Entity)\nRETURN count(e) AS entities": [{"entities": entities}],
            "MATCH ()-[r]->()\nRETURN count(r) AS edges": [{"edges": edges}],
        }
    )


def test_guard_is_clear_below_the_stop():
    report = pm.capacity_guard(guard_client(nodes=179_999, entities=100_000), Settings())
    assert report.stop_threshold == 180_000
    assert report.stop_engaged is False
    assert report.at_hard_limit is False
    assert report.headroom_to_stop == 1


def test_guard_engages_at_ninety_percent():
    report = pm.capacity_guard(guard_client(nodes=180_000), Settings())
    assert report.stop_engaged is True
    assert report.at_hard_limit is False
    assert report.node_utilisation == 0.9


def test_guard_flags_the_physical_limit():
    report = pm.capacity_guard(guard_client(nodes=200_000), Settings())
    assert report.stop_engaged is True
    assert report.at_hard_limit is True
    assert report.headroom_nodes == 0


def test_guard_counts_nodes_not_only_entities():
    """Documents and runs consume the same budget as entities."""
    report = pm.capacity_guard(guard_client(nodes=181_000, entities=10), Settings())
    assert report.stop_engaged is True
    assert report.entities == 10


def test_guard_degrades_when_the_probe_fails():
    class Dead(StubClient):
        def read(self, query, params=None):
            raise RuntimeError("asleep")

    report = pm.capacity_guard(Dead(), Settings())
    assert report.error.startswith("RuntimeError")
    assert report.stop_engaged is False


def test_graph_maintenance_capacity_reports_the_stop(settings):
    class Capacity(StubClient):
        pass

    row = {
        "entities": 100,
        "semantic_edges": 10,
        "all_edges": 20,
        "all_nodes": 180_000,
        "offshore_nodes": 1,
    }
    client = Capacity(reads={"RETURN entities, semantic_edges": [row]})
    tuned = dataclasses.replace(settings, dry_run=False, aura_node_stop=180_000, aura_node_cap=200_000)
    report = ga.GraphMaintenance(tuned, client, run_id="capacity").capacity()
    assert report.stop_threshold == 180_000
    assert report.stop_engaged is True
    assert report.headroom_to_stop == 0
    assert report.over_target is True


def test_guard_cli_exits_four_when_the_stop_is_engaged(monkeypatch):
    report = pm.CapacityGuardReport(checked_at="now", nodes=181_000, stop_engaged=True)
    monkeypatch.setattr(ga, "capacity_guard", lambda *args, **kwargs: report)
    assert ga.main(["--guard", "--dry-run", "--no-report"]) == ga.EXIT_CAPACITY


def test_guard_cli_exits_zero_below_the_stop(monkeypatch):
    report = pm.CapacityGuardReport(checked_at="now", nodes=10, stop_engaged=False)
    monkeypatch.setattr(ga, "capacity_guard", lambda *args, **kwargs: report)
    assert ga.main(["--guard", "--dry-run", "--no-report"]) == ga.EXIT_OK


def test_reports_are_json_serialisable(settings, tmp_path):
    engine = analytics(settings, ownership_client())
    report = engine.detect_cycles()
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["cycles"] == 1
    path = engine.write_report(report, directory=tmp_path)
    assert path.name.startswith("ownership_cycles_")


def test_puppet_master_report_round_trips_through_json(settings):
    report = pm.PuppetMasterReport(run_id="x", generated_at="now", top=[{"canonical_key": "A"}])
    assert json.loads(json.dumps(report.to_dict()))["top"][0]["canonical_key"] == "A"


def test_stop_threshold_honours_zero_and_the_physical_cap(settings):
    assert pm.stop_threshold(settings) == 180_000
    assert pm.stop_threshold(dataclasses.replace(settings, aura_node_stop=150_000)) == 150_000
    assert pm.stop_threshold(dataclasses.replace(settings, aura_node_stop=0)) == 200_000
    assert pm.stop_threshold(
        dataclasses.replace(settings, aura_node_stop=0, aura_node_cap=120_000)
    ) == 120_000
    assert pm.stop_threshold(
        dataclasses.replace(settings, aura_node_stop=0, aura_node_cap=0)
    ) == 200_000
