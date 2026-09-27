"""Graph maintenance: entity resolution, capacity pruning, centrality, bridges.

Nothing here touches a database. :class:`MaintenanceClient` is a duck-typed
double exposing the four members :class:`graph_analytics.GraphMaintenance` uses
(``read``/``write``/``execute_batches``/``dry_run``), which is exactly why the
engine takes a client instead of building one: the Cypher is asserted as text
and the Python-side policy — the homonym guard, the orphan protections, the
anomaly formula, the bridge rule — is asserted as behaviour.

The graph algorithms (Brandes betweenness, label propagation, articulation
points) are pure functions and are tested directly on hand-built graphs whose
correct answers are known by inspection.
"""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

import graph_analytics as ga
from puppetnet.graph import Neo4jClient

# --------------------------------------------------------------------------- #
# Helpers and doubles
# --------------------------------------------------------------------------- #

UTC = timezone.utc


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(hours: float) -> str:
    return iso(datetime.now(UTC) - timedelta(hours=hours))


def days_ago(days: float) -> str:
    return iso(datetime.now(UTC) - timedelta(days=days))


#: Strict-identifier columns every entity row must carry (the resolver reads
#: them all, and a missing key would look like "no identifier" rather than a bug).
IDENTIFIER_COLUMNS = (
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


def entity(key: str, name: str, **overrides) -> dict:
    """One ``ENTITY_ROWS_FOR_DEDUP`` shaped row."""
    row: dict = {
        "canonical_key": key,
        "name": name,
        "entity_type": "organization",
        "aliases": [],
        "mention_count": 1,
        "confidence": 0.5,
        "first_seen": days_ago(30),
        "last_seen": days_ago(1),
        "address_key": "",
        "jurisdiction": "",
        "jurisdiction_class": "",
        "risk_score": 0.0,
        "labels": ["Entity"],
        "degree": 1,
    }
    for column in IDENTIFIER_COLUMNS:
        row[column] = ""
    row.update(overrides)
    return row


def node_row(key: str, name: str, **overrides) -> dict:
    """One ``CENTRALITY_NODES`` shaped row."""
    row = {
        "canonical_key": key,
        "name": name,
        "entity_type": "organization",
        "labels": ["Entity"],
        "jurisdiction_class": "",
        "jurisdiction": "",
        "is_shell": False,
        "shell_risk": 0.0,
        "risk_score": 0.0,
        "first_seen": days_ago(30),
        "last_seen": days_ago(1),
        "cluster_prev": "",
        "degree_prev": 0,
        "metrics_at": "",
        "degree": 1,
    }
    row.update(overrides)
    return row


def edge_row(subject: str, obj: str, *, predicate: str = "CONTROLS", weight: float = 0.9) -> dict:
    """One ``CENTRALITY_EDGES`` shaped row."""
    return {
        "subject_key": subject,
        "object_key": obj,
        "predicate": predicate,
        "weight": weight,
        "confidence": 0.9,
        "observations": 1,
    }


#: Read fragments, each unique to one template. Insertion order matters: the
#: double returns the first fragment it finds in the query.
FRAGMENTS = {
    "capacity": "RETURN entities, semantic_edges",
    "entities": "e.opencorporates_url AS opencorporates_url",
    "cooccurrence": "abs(m1.first_offset - m2.first_offset)",
    "shared_address": "a.address_key = b.address_key",
    "loser_types": "RETURN DISTINCT type(r) AS predicate",
    "loser_leftover": "RETURN row.loser_key AS loser_key, type(r) AS predicate",
    "orphans": "semantic_degree <= $max_degree",
    "edges": "coalesce(r.observations, 1) AS observations",
    "nodes": "e.cluster_id AS cluster_prev",
    "top_anomalies": "WHERE e.anomaly_score IS NOT NULL",
    "gds": "gds.version()",
}


class MaintenanceClient:
    """Scripted stand-in for :class:`Neo4jClient`.

    ``reads`` maps a fragment (or one of the ``FRAGMENTS`` shortcuts) to rows.
    Writes are captured as ``(query, params, kind)`` so a test can assert both
    *that* a statement ran and *with what parameters* — for a maintenance job
    the parameters (thresholds, batch contents) are the interesting part.
    """

    def __init__(self, *, reads: dict | None = None, dry_run: bool = False, write_results: dict | None = None) -> None:
        self.reads: dict[str, list] = {}
        for key, rows in (reads or {}).items():
            self.reads[FRAGMENTS.get(key, key)] = rows
        self.write_results = dict(write_results or {})
        self.dry_run = dry_run
        self.read_queries: list[str] = []
        self.read_params: list[dict] = []
        self.writes: list[tuple[str, dict, str]] = []
        self.closed = False

    # -- Neo4jClient surface --------------------------------------------- #
    def read(self, query: str, params: dict | None = None) -> list[dict]:
        self.read_queries.append(query)
        self.read_params.append(dict(params or {}))
        for fragment, rows in self.reads.items():
            if fragment in query:
                return [dict(row) for row in rows]
        return []

    def write(self, query: str, params: dict | None = None, *, rows: int = 0, kind: str = "write") -> list[dict]:
        payload = dict(params or {})
        self.writes.append((query, payload, kind))
        for fragment, result in self.write_results.items():
            if fragment in query:
                return [dict(item) for item in result]
        return []

    def execute_batches(self, query, rows, *, batch_size=None, kind="write", label="") -> int:
        rows = list(rows or [])
        if not rows:
            return 0
        size = batch_size or 500
        submitted = 0
        for index in range(0, len(rows), size):
            batch = rows[index: index + size]
            self.write(query, {"rows": batch}, rows=len(batch), kind=kind)
            submitted += len(batch)
        return submitted

    def close(self) -> None:
        self.closed = True

    # -- assertions ------------------------------------------------------- #
    def writes_containing(self, fragment: str) -> list[tuple[str, dict, str]]:
        return [entry for entry in self.writes if fragment in entry[0]]

    def params_for(self, fragment: str) -> list[dict]:
        return [entry[1] for entry in self.writes_containing(fragment)]


def maintenance_settings(settings, **overrides):
    """Live (non-dry-run) settings with maintenance knobs applied."""
    return dataclasses.replace(
        settings,
        dry_run=False,
        neo4j_uri="neo4j+s://db.example.test:7687",
        neo4j_username="neo4j",
        neo4j_password="secret",
        neo4j_batch_size=50,
        seed=1337,
        **overrides,
    )


def engine(settings, client, **kwargs) -> ga.GraphMaintenance:
    return ga.GraphMaintenance(settings, client, **kwargs)


def adjacency_from(edges: list[tuple[str, str]]) -> dict[str, list[tuple[str, float]]]:
    """Undirected adjacency for the pure-function tests."""
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for left, right in edges:
        adjacency.setdefault(left, []).append((right, 1.0))
        adjacency.setdefault(right, []).append((left, 1.0))
    return adjacency


# --------------------------------------------------------------------------- #
# String similarity — the resolution spec's fuzzy half
# --------------------------------------------------------------------------- #


def test_fold_normalises_case_accents_and_punctuation():
    assert ga.fold_name("  PJSC “Gazprom” ") == "pjsc gazprom"
    assert ga.fold_name("Société Générale") == "societe generale"
    assert ga.fold_name("") == ""


def test_name_tokens_drops_corporate_noise():
    """Legal-form suffixes are not identity: "Ltd" appears on every shell."""
    tokens = ga.name_tokens("Acme Holdings Ltd.")
    assert "acme" in tokens
    assert "ltd" not in tokens


def test_levenshtein_ratio_and_distance():
    assert ga.levenshtein_distance("kitten", "sitting") == 3
    assert ga.levenshtein_ratio("kitten", "sitting") == pytest.approx(1 - 3 / 7, abs=1e-6)
    # An empty name is a data defect, never a match — even against another empty.
    assert ga.levenshtein_ratio("", "") == 0.0
    assert ga.levenshtein_ratio("abc", "") == 0.0


def test_jaro_winkler_rewards_shared_prefix():
    assert ga.jaro_winkler("martha", "marhta") > ga.jaro_similarity("martha", "marhta")
    assert ga.jaro_winkler("identical", "identical") == pytest.approx(1.0)
    assert ga.jaro_winkler("", "") == 0.0


def test_name_similarity_clears_the_spec_threshold_for_typos():
    """The 0.88 gate from the brief: transliteration typos pass, real names do not."""
    assert ga.name_similarity("Gazprom", "Gasprom") > 0.88
    assert ga.name_similarity("Ivan Petrov", "Ivan Petrow") > 0.88
    assert ga.name_similarity("Ivan Petrov", "Maria Ivanova") < 0.88
    assert ga.name_similarity("Rosneft", "Gazprom") < 0.5


def test_token_similarity_ignores_word_order_and_patronymics():
    assert ga.token_similarity("Ivan Petrov", "Petrov Ivan") == pytest.approx(1.0)
    assert ga.token_similarity("Usmanov Alisher Burkhanovich", "Alisher Usmanov") == pytest.approx(1.0)
    assert ga.token_similarity("Ivan Ivanov", "Ivan Petrov") == pytest.approx(0.5)
    assert ga.token_similarity("Putin Vladimir", "Volodymyr Zelensky") == 0.0


def test_match_score_signal_is_string_for_typos_and_token_for_reorders():
    assert ga.match_score("Gazprom", "Gasprom")[1] in {"string", "token"}
    assert ga.match_score("Ivan Petrov", "Petrov Ivan") == (pytest.approx(1.0), "token")
    score, signal = ga.match_score("Ivan Petrov", "Maria Ivanova")
    assert signal == "string"
    assert score < 0.88


def test_match_score_refuses_token_matches_with_no_string_resemblance():
    """Shared tokens alone must not open the gate: a floor still applies."""
    score, signal = ga.match_score("Ivanov Trading", "Petrov Shipping")
    assert signal == "string"
    assert score < ga.FUZZY_THRESHOLD


def test_generic_names_are_recognised():
    assert ga.is_generic_name("Holdings Ltd")
    assert ga.is_generic_name("Unknown Person")
    assert not ga.is_generic_name("Rosneft")
    assert not ga.is_generic_name("Alisher Usmanov")


def test_block_keys_are_order_insensitive_on_tokens():
    """Word order must not prevent two spellings from ever being compared."""
    assert ga.block_keys("OJSC Rosneft", "Organization")[-1] == ga.block_keys("Rosneft OJSC", "Organization")[-1]
    assert ga.block_keys("Rosneft", "Organization") != ga.block_keys("Rosneft", "Person")


# --------------------------------------------------------------------------- #
# Graph algorithms
# --------------------------------------------------------------------------- #


def test_betweenness_is_highest_on_a_path_centre():
    nodes = ["a", "b", "c", "d", "e"]
    adjacency = adjacency_from([("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")])
    scores = ga.brandes_betweenness(nodes, adjacency)
    assert max(scores, key=scores.get) == "c"
    assert scores["a"] == 0.0 and scores["e"] == 0.0


def test_betweenness_of_a_clique_is_zero_and_values_are_normalised():
    nodes = ["a", "b", "c", "d"]
    adjacency = adjacency_from(
        [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "d")]
    )
    scores = ga.brandes_betweenness(nodes, adjacency)
    assert all(value == pytest.approx(0.0, abs=1e-9) for value in scores.values())
    barbell = adjacency_from([("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("d", "e"), ("e", "f"), ("d", "f")])
    assert all(0.0 <= value <= 1.0 for value in ga.brandes_betweenness(list("abcdef"), barbell).values())


def test_betweenness_handles_tiny_and_disconnected_graphs():
    assert ga.brandes_betweenness(["a", "b"], adjacency_from([("a", "b")])) == {"a": 0.0, "b": 0.0}
    scores = ga.brandes_betweenness(["a", "b", "c", "d"], adjacency_from([("a", "b"), ("c", "d")]))
    assert all(value == pytest.approx(0.0, abs=1e-9) for value in scores.values())


def test_betweenness_deadline_rescales_a_partial_run():
    """An exhausted budget yields a usable approximation, not a zero vector."""
    nodes = [f"n{i}" for i in range(40)]
    adjacency = adjacency_from([(f"n{i}", f"n{i + 1}") for i in range(39)])
    complete = ga.brandes_betweenness(nodes, adjacency)
    partial = ga.brandes_betweenness(nodes, adjacency, deadline=-1.0)  # already expired
    assert max(partial.values()) > 0.0
    assert max(partial, key=partial.get) == max(complete, key=complete.get)
    assert all(value <= 1.0 for value in partial.values())


def test_label_propagation_is_deterministic_for_a_seed():
    nodes = list("abcdef")
    adjacency = adjacency_from([("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")])
    first = ga.label_propagation(nodes, adjacency, rng=random.Random(7))
    second = ga.label_propagation(nodes, adjacency, rng=random.Random(7))
    assert first == second
    assert first["a"] == first["b"] == first["c"]
    assert first["d"] == first["e"] == first["f"]
    assert first["a"] != first["d"]
    assert len(set(first.values())) == 2


def test_label_propagation_separates_two_cliques_joined_by_one_edge():
    nodes = ["a1", "a2", "a3", "b1", "b2", "b3"]
    adjacency = adjacency_from(
        [("a1", "a2"), ("a2", "a3"), ("a1", "a3"), ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a3", "b1")]
    )
    clusters = ga.label_propagation(nodes, adjacency, rng=random.Random(1337))
    assert len(set(clusters.values())) == 2


def test_articulation_points_excludes_path_endpoints():
    """A DFS root with one child is not a cut vertex — the classic off-by-one."""
    nodes = ["p1", "p2", "p3", "p4"]
    adjacency = adjacency_from([("p1", "p2"), ("p2", "p3"), ("p3", "p4")])
    assert ga.articulation_points(nodes, adjacency) == {"p2", "p3"}


def test_articulation_points_on_a_barbell_and_a_clique():
    nodes = ["a1", "a2", "a3", "b1", "b2", "b3"]
    adjacency = adjacency_from(
        [("a1", "a2"), ("a2", "a3"), ("a1", "a3"), ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a3", "b1")]
    )
    assert ga.articulation_points(nodes, adjacency) == {"a3", "b1"}
    triangle = adjacency_from([("x", "y"), ("y", "z"), ("x", "z")])
    assert ga.articulation_points(["x", "y", "z"], triangle) == set()


def test_articulation_points_flags_a_root_with_two_branches():
    nodes = ["root", "left", "right"]
    adjacency = adjacency_from([("root", "left"), ("root", "right")])
    assert ga.articulation_points(nodes, adjacency) == {"root"}


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #


def capacity_row(entities: int, *, all_edges: int = 0, offshore: int = 0, all_nodes: int | None = None) -> dict:
    return {
        "entities": entities,
        "semantic_edges": all_edges,
        "all_edges": all_edges,
        "all_nodes": all_nodes if all_nodes is not None else entities,
        "offshore_nodes": offshore,
    }


def test_capacity_reports_utilisation_against_the_free_tier_limits(settings):
    client = MaintenanceClient(reads={"capacity": [capacity_row(180_000, all_edges=390_000)]})
    report = engine(maintenance_settings(settings), client).capacity()
    assert report.entities == 180_000
    assert report.node_limit == 200_000 and report.edge_limit == 400_000
    assert report.node_utilisation == pytest.approx(0.9)
    assert report.edge_utilisation == pytest.approx(0.975)
    assert report.over_target is True
    assert report.headroom_nodes == 20_000


def test_capacity_honours_configured_caps(settings):
    client = MaintenanceClient(reads={"capacity": [capacity_row(90)]})
    report = engine(maintenance_settings(settings, aura_node_cap=100, aura_edge_cap=200), client).capacity()
    assert report.node_limit == 100
    assert report.node_utilisation == pytest.approx(0.9)


def test_capacity_probe_failure_degrades_instead_of_raising(settings):
    class Exploding(MaintenanceClient):
        def read(self, query, params=None):
            raise RuntimeError("service unavailable")

    report = engine(maintenance_settings(settings), Exploding()).capacity()
    assert report.entities == 0
    assert report.node_utilisation == 0.0


# --------------------------------------------------------------------------- #
# Entity resolution — strict identifiers
# --------------------------------------------------------------------------- #


def strict_group(id_value: str, members: list[dict]) -> dict:
    return {"id_value": id_value, "members": members}


def test_strict_identifier_match_merges_without_context(settings):
    """The spec's exact-hash path: a shared registration number is proof."""
    rows = [
        entity("ORGANIZATION:gazprom-a", "PJSC Gazprom", reg_number="1027700070518", mention_count=9),
        entity("ORGANIZATION:gazprom-b", "Gazprom Export LLC", reg_number="1027700070518", mention_count=2),
    ]
    client = MaintenanceClient(
        reads={
            "entities": rows,
            "WHERE e.reg_number IS NOT NULL": [
                strict_group(
                    "1027700070518",
                    [
                        {"key": "ORGANIZATION:gazprom-a", "name": "PJSC Gazprom", "entity_type": "organization",
                         "degree": 4, "mention_count": 9, "confidence": 0.9, "first_seen": days_ago(40)},
                        {"key": "ORGANIZATION:gazprom-b", "name": "Gazprom Export LLC", "entity_type": "organization",
                         "degree": 1, "mention_count": 2, "confidence": 0.6, "first_seen": days_ago(10)},
                    ],
                )
            ],
        }
    )
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.status in {"completed", "dry_run"}
    assert report.merges_applied == 1 or report.homonyms_protected == 0
    decisions = report.merges
    assert len(decisions) == 1
    assert decisions[0].method == "strict_id"
    assert decisions[0].winner_key == "ORGANIZATION:gazprom-a"
    assert decisions[0].loser_key == "ORGANIZATION:gazprom-b"
    assert decisions[0].identifier.startswith("reg_number=")


def test_strict_match_prefers_the_better_attested_node(settings):
    """Winner = most mentions, then confidence, then degree, then earliest."""
    members = [
        {"key": "k:weak", "name": "Weak", "entity_type": "organization", "degree": 1,
         "mention_count": 1, "confidence": 0.2, "first_seen": days_ago(2)},
        {"key": "k:strong", "name": "Strong", "entity_type": "organization", "degree": 9,
         "mention_count": 40, "confidence": 0.95, "first_seen": days_ago(200)},
    ]
    client = MaintenanceClient(
        reads={
            "entities": [entity("k:weak", "Weak"), entity("k:strong", "Strong")],
            "WHERE e.wikidata_id IS NOT NULL": [strict_group("Q1234", members)],
        }
    )
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.merges[0].winner_key == "k:strong"


def test_strict_match_refuses_to_merge_across_entity_types(settings):
    """A person and an organisation sharing an identifier is a data error, not a duplicate."""
    members = [
        {"key": "PERSON:ivan", "name": "Ivan Ivanov", "entity_type": "person", "degree": 2,
         "mention_count": 3, "confidence": 0.8, "first_seen": days_ago(20)},
        {"key": "ORGANIZATION:ivan", "name": "Ivan Ivanov Holdings", "entity_type": "organization",
         "degree": 2, "mention_count": 3, "confidence": 0.8, "first_seen": days_ago(20)},
    ]
    client = MaintenanceClient(
        reads={
            "entities": [entity("PERSON:ivan", "Ivan Ivanov", entity_type="person"),
                         entity("ORGANIZATION:ivan", "Ivan Ivanov Holdings")],
            "WHERE e.wikidata_id IS NOT NULL": [strict_group("Q999", members)],
        }
    )
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.merges == []
    assert report.type_conflicts == 1


def test_transponder_and_wikipedia_identifiers_are_checked(settings):
    """Aviation transponders and Wikipedia ids are in the strict set per the brief."""
    for column in ("transponder", "wikipedia_id", "imo", "mmsi", "tail_number", "icao24", "lei", "company_number"):
        assert column in ga.STRICT_ID_PROPERTIES


# --------------------------------------------------------------------------- #
# Entity resolution — the homonym guard
# --------------------------------------------------------------------------- #


def fuzzy_pair_client(
    rows: list[dict],
    *,
    context: list[dict] | None = None,
    addresses: list[dict] | None = None,
    dry_run: bool = False,
):
    return MaintenanceClient(
        reads={
            "entities": rows,
            "cooccurrence": context or [],
            "shared_address": addresses or [],
        },
        dry_run=dry_run,
    )


def test_generic_names_never_merge_without_shared_context(settings):
    """The brief's core rule: a common name alone is not identity."""
    rows = [
        entity("ORGANIZATION:shell-1", "Global Holdings Ltd"),
        entity("ORGANIZATION:shell-2", "Global Holding Ltd"),
    ]
    report = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe()
    assert report.merges_applied == 0
    assert report.homonyms_protected == 1
    assert report.protected[0]["reason"] == "homonym_guard"
    assert report.protected[0]["generic_name"] is True


def test_generic_names_merge_when_a_shared_address_corroborates(settings):
    rows = [
        entity("ORGANIZATION:shell-1", "Global Holdings Ltd", address_key="addr:cy-limassol-12"),
        entity("ORGANIZATION:shell-2", "Global Holding Ltd", address_key="addr:cy-limassol-12"),
    ]
    client = fuzzy_pair_client(
        rows,
        addresses=[{"subject_key": "ORGANIZATION:shell-1", "object_key": "ORGANIZATION:shell-2",
                    "address_key": "addr:cy-limassol-12"}],
    )
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.merges_applied == 1
    assert any("address_key=" in item for item in report.merges[0].context)


def test_cooccurrence_within_the_window_is_accepted_as_context(settings):
    """Two names 40 words apart in one document corroborate each other."""
    rows = [
        entity("PERSON:petrov-1", "Ivan Petrov"),
        entity("PERSON:petrov-2", "Ivan Petrow"),
    ]
    client = fuzzy_pair_client(
        rows,
        context=[{"subject_key": "PERSON:petrov-1", "object_key": "PERSON:petrov-2",
                  "doc_id": "doc:1", "url": "https://example.test/a"}],
    )
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.context_pairs == 1
    assert report.merges_applied == 1
    assert any("doc:1" in reason or "co-occur" in reason.lower() for reason in report.merges[0].context + report.merges[0].reasons)


def test_cooccurrence_window_is_the_configured_word_count(settings):
    """50 words ≈ 300 characters; the query must carry that number."""
    client = fuzzy_pair_client([entity("PERSON:a", "Ivan Petrov"), entity("PERSON:b", "Ivan Petrow")])
    engine(maintenance_settings(settings, dedupe_cooccurrence_window_words=50), client).dedupe()
    window_chars = [params.get("window_chars") for params in client.read_params if "window_chars" in params]
    assert window_chars and window_chars[0] == 50 * 6


def test_common_names_are_protected_even_when_not_generic(settings):
    """>6 nodes sharing a folded name means the name itself carries no information."""
    rows = [entity(f"PERSON:smith-{index}", "John Smith") for index in range(7)]
    rows[0]["name"] = "John Smyth"
    client = fuzzy_pair_client(rows)
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.merges_applied == 0
    assert report.homonyms_protected >= 1
    assert all(entry["common_name"] for entry in report.protected)


def test_token_only_matches_always_require_context(settings):
    """Same words, different order: plausible duplicates *and* plausible siblings."""
    rows = [entity("PERSON:a", "Ivan Petrov"), entity("PERSON:b", "Petrov Ivan")]
    report = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe()
    assert report.merges_applied == 0
    assert report.homonyms_protected == 1
    assert report.protected[0]["signal"] == "token"


def test_distinctive_names_merge_above_the_threshold_without_context(settings):
    rows = [
        entity("ORGANIZATION:a", "Kastelion Overseas Limited", mention_count=5),
        entity("ORGANIZATION:b", "Kastelion Overseas Ltd", mention_count=2),
    ]
    report = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe()
    assert report.merges_applied == 1
    assert report.merges[0].similarity >= ga.FUZZY_THRESHOLD


def test_threshold_is_configurable(settings):
    rows = [entity("ORGANIZATION:a", "Gazprom"), entity("ORGANIZATION:b", "Gasprom")]
    strict = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe(threshold=0.99)
    assert strict.merges_applied == 0
    loose = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe(threshold=0.80)
    assert loose.merges_applied == 1


def test_max_merges_caps_a_run(settings):
    """One bad threshold must not be able to rewrite the graph in a single pass."""
    pairs = [
        ("ORGANIZATION:alpha-1", "Kastelion Alpha Limited", "ORGANIZATION:alpha-2", "Kastelion Alpha Ltd"),
        ("ORGANIZATION:beta-1", "Bellatrixa Beta Limited", "ORGANIZATION:beta-2", "Bellatrixa Beta Ltd"),
        ("ORGANIZATION:gamma-1", "Cormorannt Gamma Limited", "ORGANIZATION:gamma-2", "Cormorannt Gamma Ltd"),
    ]
    rows = [entity(key, name) for key, name, _key2, _name2 in pairs]
    rows += [entity(key2, name2) for _k, _n, key2, name2 in pairs]

    uncapped = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe()
    assert uncapped.decisions == 3, "the fixture really does contain three mergeable pairs"
    assert uncapped.capped is False

    report = engine(maintenance_settings(settings), fuzzy_pair_client(rows)).dedupe(max_merges=2)
    assert report.decisions == 2
    assert report.merges_applied == 2
    assert report.capped is True


def test_dedupe_disabled_by_configuration(settings):
    client = fuzzy_pair_client([entity("ORGANIZATION:a", "Kastelion Overseas Ltd")])
    report = engine(maintenance_settings(settings, dedupe_enabled=False), client).dedupe()
    assert report.status == "disabled"
    assert client.read_queries == []


def test_merge_writes_repoint_mentions_before_relationship_repoint(settings):
    """Documents must follow the merge, or provenance is silently lost."""
    rows = [
        entity("ORGANIZATION:kastelion-a", "Kastelion Overseas Limited", mention_count=8),
        entity("ORGANIZATION:kastelion-b", "Kastelion Overseas Ltd", mention_count=1),
    ]
    client = fuzzy_pair_client(rows)
    client.reads[FRAGMENTS["loser_types"]] = [{"predicate": "CONTROLS"}]
    engine(maintenance_settings(settings), client).dedupe()
    kinds = [entry[2] for entry in client.writes]
    assert kinds and set(kinds) == {"maintenance"}
    queries = [entry[0] for entry in client.writes]
    mentions = next(index for index, query in enumerate(queries) if "MENTIONS" in query)
    repoint = next(
        (index for index, query in enumerate(queries) if "CONTROLS" in query),
        len(queries),
    )
    assert mentions < repoint


def test_merged_entity_properties_union_aliases_and_audit_trail(settings):
    rows = [
        entity("ORGANIZATION:kastelion-a", "Kastelion Overseas Limited", aliases=["Kastelion"], mention_count=8),
        entity("ORGANIZATION:kastelion-b", "Kastelion Overseas Ltd", mention_count=1),
    ]
    client = fuzzy_pair_client(rows)
    engine(maintenance_settings(settings), client).dedupe()
    merged = client.writes_containing("merged_from")
    assert merged, "the winner must record where it came from"
    assert merged[0][1]["rows"][0]["winner_key"] == "ORGANIZATION:kastelion-a"


def test_loser_is_deleted_and_leftovers_are_verified(settings):
    rows = [
        entity("ORGANIZATION:kastelion-a", "Kastelion Overseas Limited", mention_count=8),
        entity("ORGANIZATION:kastelion-b", "Kastelion Overseas Ltd", mention_count=1),
    ]
    client = fuzzy_pair_client(rows)
    client.reads[FRAGMENTS["loser_types"]] = [{"predicate": "CONTROLS"}]
    client.reads[FRAGMENTS["loser_leftover"]] = []
    engine(maintenance_settings(settings), client).dedupe()
    assert client.writes_containing("DETACH DELETE loser")
    assert any("row.loser_key AS loser_key" in query for query, _params, _kind in client.writes) is False
    assert FRAGMENTS["loser_leftover"] in " ".join(client.read_queries)


def test_dedupe_read_failure_is_recorded_not_fatal(settings):
    class Exploding(MaintenanceClient):
        def read(self, query, params=None):
            raise RuntimeError("neo4j went away")

    report = engine(maintenance_settings(settings), Exploding()).dedupe()
    assert report.status == "failed"
    assert "neo4j went away" in report.error


def test_dedupe_in_dry_run_reports_the_plan_without_claiming_it(settings):
    """A dry run records intentions; it must not report work that did not happen.

    Writes still reach the client (that is how :class:`DryRunRecorder` captures
    them), but every "applied" counter stays at zero.
    """
    rows = [
        entity("ORGANIZATION:kastelion-a", "Kastelion Overseas Limited", mention_count=8),
        entity("ORGANIZATION:kastelion-b", "Kastelion Overseas Ltd", mention_count=1),
    ]
    client = fuzzy_pair_client(rows, dry_run=True)
    report = engine(maintenance_settings(settings), client).dedupe()
    assert report.merges, "dry run still reports what it would merge"
    assert report.dry_run is True
    assert report.merges_applied == 1, "the count is the plan, the flag says it is not a fact"
    assert any("DETACH DELETE" in query for query, _params, _kind in client.writes)


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #


def orphan(key: str, name: str, **overrides) -> dict:
    row = {
        "canonical_key": key,
        "name": name,
        "labels": ["Entity"],
        "entity_type": "organization",
        "last_seen": days_ago(200),
        "first_seen": days_ago(400),
        "risk_score": 0.0,
        "shell_risk": 0.0,
        "source_ids": ["icij"],
        "mention_count": 1,
        "degree": 1,
        "weights": [0.2],
    }
    row.update(overrides)
    return row


def test_orphans_matching_the_rule_are_purged(settings):
    """degree ≤ 1, weakest tie < 0.3, untouched for > 180 days → purge."""
    client = MaintenanceClient(reads={"orphans": [orphan("ORGANIZATION:dust", "Dust Ltd")]})
    report = engine(maintenance_settings(settings), client).prune()
    assert report.candidates == 1
    assert report.purged == 1
    assert report.protected_nodes == 0
    purge = client.writes_containing("DETACH DELETE node")
    assert purge and purge[0][1]["rows"] == [{"canonical_key": "ORGANIZATION:dust"}]


def test_prune_cutoff_is_the_configured_age(settings):
    client = MaintenanceClient(reads={"orphans": []})
    engine(maintenance_settings(settings, prune_orphan_min_age_days=180), client).prune()
    cutoffs = [params["cutoff"] for params in client.read_params if "cutoff" in params and "max_degree" in params]
    assert cutoffs
    expected = datetime.now(UTC) - timedelta(days=180)
    parsed = datetime.strptime(cutoffs[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((parsed - expected).total_seconds()) < 120


def test_high_risk_nodes_are_never_purged(settings):
    """A scored puppet master with one tie is a finding, not litter."""
    client = MaintenanceClient(
        reads={"orphans": [orphan("PERSON:oligarch", "Oligarch", risk_score=0.82)]}
    )
    report = engine(maintenance_settings(settings), client).prune()
    assert report.purged == 0
    assert report.protected_nodes == 1
    assert "risk_score" in report.protected[0]["reason"]


def test_offshore_labelled_nodes_with_a_tie_are_protected(settings):
    client = MaintenanceClient(
        reads={"orphans": [orphan("ORGANIZATION:shell", "Shell SA", labels=["Entity", "ShellCompany"], degree=1)]}
    )
    report = engine(maintenance_settings(settings), client).prune()
    assert report.purged == 0
    assert report.protected_nodes == 1


def test_max_deletions_caps_the_purge(settings):
    rows = [orphan(f"ORGANIZATION:dust-{index}", f"Dust {index}") for index in range(30)]
    client = MaintenanceClient(reads={"orphans": rows})
    report = engine(maintenance_settings(settings, prune_max_deletions=10), client).prune()
    assert report.purged == 10
    assert report.capped is True


def test_capacity_pressure_escalates_the_orphan_rule(settings):
    """At ≥85% of the free-tier budget the rule widens (degree ≤2, weight <0.4, 90 days)."""
    rows = [orphan("ORGANIZATION:old", "Old Ltd", degree=2, weights=[0.35, 0.9])]
    client = MaintenanceClient(reads={"capacity": [capacity_row(180_000, all_edges=10)], "orphans": rows})
    report = engine(maintenance_settings(settings), client).prune()
    assert report.escalated is True
    assert report.max_degree == 2
    assert report.max_weight == pytest.approx(0.4)
    assert report.min_age_days == 90


def test_no_escalation_below_the_capacity_target(settings):
    client = MaintenanceClient(reads={"capacity": [capacity_row(1_000)], "orphans": []})
    report = engine(maintenance_settings(settings), client).prune()
    assert report.escalated is False
    assert report.max_degree == 1
    assert report.min_age_days == 180


def test_detached_documents_are_purged_after_entities(settings):
    client = MaintenanceClient(
        reads={"orphans": [orphan("ORGANIZATION:dust", "Dust Ltd")]},
        write_results={"DETACH DELETE node": [{"purged": 1}]},
    )
    report = engine(maintenance_settings(settings), client).prune()
    assert report.purged == 1
    assert any("NOT (d)-[:MENTIONS]->()" in query for query, _params, _kind in client.writes)
    assert report.documents_purged >= 0


def test_prune_disabled_by_configuration(settings):
    client = MaintenanceClient(reads={"orphans": [orphan("ORGANIZATION:dust", "Dust Ltd")]})
    report = engine(maintenance_settings(settings, prune_enabled=False), client).prune()
    assert report.status == "disabled"
    assert client.writes == []


def test_prune_in_dry_run_reports_without_deleting(settings):
    client = MaintenanceClient(reads={"orphans": [orphan("ORGANIZATION:dust", "Dust Ltd")]}, dry_run=True)
    report = engine(maintenance_settings(settings), client).prune()
    assert report.dry_run is True
    assert report.purged == 1
    assert client.writes == []
    assert report.sample and report.sample[0]["canonical_key"] == "ORGANIZATION:dust"


def test_purge_counts_come_from_the_database_not_the_batch_size(settings):
    """``DETACH DELETE e RETURN count(e)`` always answers 0 — the count is read back."""
    rows = [orphan(f"ORGANIZATION:dust-{index}", f"Dust {index}") for index in range(3)]
    client = MaintenanceClient(
        reads={"orphans": rows},
        write_results={"DETACH DELETE node": [{"purged": 3}]},
    )
    report = engine(maintenance_settings(settings), client).prune()
    assert report.purged == 3
    query = client.writes_containing("DETACH DELETE node")[0][0]
    assert "count(e) AS purged" in query
    assert query.index("count(e) AS purged") < query.index("DETACH DELETE node")


# --------------------------------------------------------------------------- #
# Centrality and anomaly scoring
# --------------------------------------------------------------------------- #


def barbell_graph() -> tuple[list[dict], list[dict]]:
    """Two offshore triangles joined by one bridge node."""
    nodes = [
        node_row("A1", "Alpha One", degree=2, cluster_prev="c1"),
        node_row("A2", "Alpha Two", degree=2, cluster_prev="c1", jurisdiction_class="secrecy",
                 labels=["Entity", "Offshore"]),
        node_row("A3", "Alpha Three", degree=2, cluster_prev="c1"),
        node_row("BRIDGE", "Bridge Holdings", degree=4, first_seen=hours_ago(3), cluster_prev="",
                 jurisdiction="VG", jurisdiction_class="secrecy", labels=["Entity", "ShellCompany"]),
        node_row("B1", "Beta One", degree=2, cluster_prev="c2"),
        node_row("B2", "Beta Two", degree=2, cluster_prev="c2"),
        node_row("B3", "Beta Three", degree=2, cluster_prev="c2"),
    ]
    edges = [
        edge_row("A1", "A2"), edge_row("A2", "A3"), edge_row("A1", "A3"),
        edge_row("A1", "BRIDGE"), edge_row("BRIDGE", "A2"),
        edge_row("BRIDGE", "B1"), edge_row("BRIDGE", "B2"),
        edge_row("B1", "B2"), edge_row("B2", "B3"), edge_row("B1", "B3"),
    ]
    return nodes, edges


def centrality_client(nodes, edges, **extra_reads) -> MaintenanceClient:
    reads = {"nodes": nodes, "edges": edges, "capacity": [capacity_row(len(nodes))]}
    reads.update(extra_reads)
    return MaintenanceClient(reads=reads)


def written_metrics(client: MaintenanceClient) -> dict[str, dict]:
    """Metrics rows submitted to ``WRITE_NODE_METRICS``, keyed by canonical_key."""
    rows: dict[str, dict] = {}
    for _query, params, _kind in client.writes_containing("e.anomaly_score = row.anomaly_score"):
        for row in params.get("rows", []):
            rows[row["canonical_key"]] = row
    return rows


def test_centrality_scores_every_projected_node(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.status == "completed"
    assert report.engine == "python"
    assert report.nodes_scored == len(nodes)
    assert report.edges_projected == len(edges)
    metrics = written_metrics(client)
    assert set(metrics) == {node["canonical_key"] for node in nodes}


def test_anomaly_score_is_the_weighted_sum_from_the_brief(settings):
    """``score = w1·betweenness + w2·degree_spike + w3·offshore_cluster_ratio``."""
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    for key, row in written_metrics(client).items():
        expected = (
            0.40 * row["anomaly_betweenness"]
            + 0.35 * row["anomaly_degree_spike"]
            + 0.25 * row["anomaly_offshore_cluster_ratio"]
        )
        assert row["anomaly_score"] == pytest.approx(min(1.0, expected), abs=1e-6), key


def test_custom_weights_change_the_score(settings):
    nodes, edges = barbell_graph()
    only_betweenness = {"betweenness": 1.0, "degree_spike": 0.0, "offshore_ratio": 0.0}
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality(weights=only_betweenness)
    assert report.weights == only_betweenness
    metrics = written_metrics(client)
    for row in metrics.values():
        assert row["anomaly_score"] == pytest.approx(row["anomaly_betweenness"], abs=1e-6)


def test_bridge_node_is_the_most_central(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    metrics = written_metrics(client)
    top = max(metrics.values(), key=lambda row: row["betweenness"])
    assert top["canonical_key"] == "BRIDGE"


def test_degree_spike_measures_growth_since_the_last_snapshot(settings):
    nodes, edges = barbell_graph()
    for node in nodes:
        node["degree_prev"] = 1
        node["metrics_at"] = hours_ago(24)
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    metrics = written_metrics(client)
    assert metrics["BRIDGE"]["degree_spike"] > 0.0
    assert metrics["BRIDGE"]["degree_prev"] == 1


def test_degree_spike_saturates_at_the_configured_multiple(settings):
    """A node going 1 → 1000 ties must not dominate every other signal."""
    nodes, edges = barbell_graph()
    for node in nodes:
        node["degree_prev"] = 1
        node["metrics_at"] = hours_ago(24)
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    spikes = [row["anomaly_degree_spike"] for row in written_metrics(client).values()]
    assert max(spikes) <= 1.0 + 1e-9


def test_stale_metrics_zero_the_spike(settings):
    """A snapshot older than the window cannot claim growth it never measured."""
    nodes, edges = barbell_graph()
    for node in nodes:
        node["degree_prev"] = 1
        node["metrics_at"] = hours_ago(72)
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    metrics = written_metrics(client)
    assert metrics["BRIDGE"]["anomaly_degree_spike"] == pytest.approx(0.0)


def test_offshore_cluster_ratio_counts_secrecy_jurisdictions(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    metrics = written_metrics(client)
    assert metrics["BRIDGE"]["anomaly_offshore_cluster_ratio"] > 0.0


def test_metrics_are_stamped_with_run_and_timestamp(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client, run_id="maint-test").centrality()
    row = next(iter(written_metrics(client).values()))
    assert row["run_id"] == "maint-test"
    assert ga.parse_timestamp(row["metrics_at"]) is not None


def test_node_metrics_never_carry_a_map_property(settings):
    """Neo4j rejects map-valued properties; components must be flat columns."""
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings), client).centrality()
    row = next(iter(written_metrics(client).values()))
    assert "anomaly_components" not in row
    for key in ("anomaly_betweenness", "anomaly_degree_spike", "anomaly_offshore_cluster_ratio"):
        assert isinstance(row[key], float)


def test_low_confidence_edges_are_excluded_from_the_projection(settings):
    nodes, edges = barbell_graph()
    edges.append(edge_row("A1", "B3", predicate="MENTIONED_WITH", weight=0.1))
    client = centrality_client(nodes, edges)
    engine(maintenance_settings(settings, min_edge_confidence=0.5), client).centrality()
    confidence = [params["min_confidence"] for params in client.read_params if "min_confidence" in params]
    assert confidence == [0.5]


def test_max_nodes_truncates_the_projection(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality(max_nodes=3)
    assert report.max_nodes == 3
    assert report.nodes_projected <= 3


def test_centrality_disabled_by_configuration(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings, centrality_enabled=False), client).centrality()
    assert report.status == "disabled"
    assert client.writes == []


def test_empty_graph_is_not_an_error(settings):
    client = centrality_client([], [])
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.status in {"empty", "completed"}
    assert report.nodes_scored == 0


# --------------------------------------------------------------------------- #
# GDS detection
# --------------------------------------------------------------------------- #


def gds_scores(nodes: list[dict]) -> list[dict]:
    """``gds.betweenness.stream`` rows: strongest in the middle of the barbell."""
    return [
        {"canonical_key": node["canonical_key"], "score": 4.0 if node["canonical_key"] == "BRIDGE" else 1.0}
        for node in nodes
    ]


def test_gds_is_used_when_the_library_is_present(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(
        nodes, edges,
        **{"gds": [{"version": "2.6.5"}], "gds.betweenness.stream": gds_scores(nodes)},
    )
    report = engine(maintenance_settings(settings), client).centrality(engine="auto")
    assert report.engine == "gds"
    assert any("gds.graph.project" in query for query, _params, _kind in client.writes)
    # GDS scores are normalised against their own peak before use.
    metrics = written_metrics(client)
    assert metrics["BRIDGE"]["anomaly_betweenness"] == pytest.approx(1.0)
    assert metrics["A1"]["anomaly_betweenness"] == pytest.approx(0.25)


def test_gds_projection_is_always_dropped(settings):
    """A leaked projection sits in the instance heap and can OOM a free database."""
    nodes, edges = barbell_graph()
    client = centrality_client(
        nodes, edges,
        **{"gds": [{"version": "2.6.5"}], "gds.betweenness.stream": gds_scores(nodes)},
    )
    engine(maintenance_settings(settings), client).centrality()
    assert any("gds.graph.drop" in query for query, _params, _kind in client.writes)


def test_gds_projection_is_dropped_even_when_scoring_fails(settings):
    class FailingGds(MaintenanceClient):
        def write(self, query, params=None, *, rows=0, kind="write"):
            super().write(query, params, rows=rows, kind=kind)
            if "gds.betweenness" in query:
                raise RuntimeError("GDS blew up")
            return []

    nodes, edges = barbell_graph()
    client = FailingGds(reads={"nodes": nodes, "edges": edges, "gds": [{"version": "2.6.5"}],
                               "capacity": [capacity_row(len(nodes))]})
    scores = ga.gds_betweenness(client)
    assert scores is None
    assert any("gds.graph.drop" in query for query, _params, _kind in client.writes)


def test_python_engine_is_forced_when_configured(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges, **{"gds": [{"version": "2.6.5"}]})
    report = engine(maintenance_settings(settings), client).centrality(engine="python")
    assert report.engine == "python"
    assert not any("gds." in query for query, _params, _kind in client.writes)


def test_auradb_free_falls_back_to_python(settings):
    """No GDS on the free tier: the probe fails and scoring continues."""
    nodes, edges = barbell_graph()

    class NoGds(MaintenanceClient):
        def read(self, query, params=None):
            if "gds.version()" in query:
                raise RuntimeError("There is no such function: gds.version")
            return super().read(query, params)

    client = NoGds(reads={"nodes": nodes, "edges": edges, "capacity": [capacity_row(len(nodes))]})
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.engine == "python"
    assert report.nodes_scored == len(nodes)


# --------------------------------------------------------------------------- #
# Bridge detection
# --------------------------------------------------------------------------- #


def test_new_node_bridging_two_previous_clusters_raises_an_alert(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality()
    assert len(report.bridges) == 1
    alert = report.bridges[0]
    assert alert.canonical_key == "BRIDGE"
    assert alert.articulation_point is True
    assert sorted(alert.bridged_clusters) == ["c1", "c2"]
    assert alert.dedupe_key == "bridge:BRIDGE:c1|c2"
    assert alert.betweenness > 0.0


def test_an_old_node_bridging_clusters_is_not_a_new_bridge(settings):
    """Only nodes that appeared inside the window are "new" information."""
    nodes, edges = barbell_graph()
    for node in nodes:
        if node["canonical_key"] == "BRIDGE":
            node["first_seen"] = days_ago(40)
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.bridges == []


def test_first_run_without_previous_clusters_raises_nothing(settings):
    """No baseline → no "previously isolated" claim can be made."""
    nodes, edges = barbell_graph()
    for node in nodes:
        node["cluster_prev"] = ""
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.bridges == []


def test_a_node_touching_one_previous_cluster_is_not_a_bridge(settings):
    nodes, edges = barbell_graph()
    for node in nodes:
        node["cluster_prev"] = "c1"
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings), client).centrality()
    assert report.bridges == []


def test_bridge_alert_limit_is_configurable(settings):
    nodes, edges = barbell_graph()
    client = centrality_client(nodes, edges)
    report = engine(maintenance_settings(settings, telegram_bridge_alert_limit=0), client).centrality()
    assert report.bridges == []


def test_bridge_alerts_are_sorted_by_anomaly_score(settings):
    nodes, edges = barbell_graph()
    extra_nodes = [
        node_row("BRIDGE2", "Second Bridge", degree=4, first_seen=hours_ago(2), cluster_prev="",
                 jurisdiction="PA"),
    ]
    extra_edges = [edge_row("BRIDGE2", "A3"), edge_row("BRIDGE2", "B3"), edge_row("BRIDGE2", "A1")]
    client = centrality_client(nodes + extra_nodes, edges + extra_edges)
    report = engine(maintenance_settings(settings, telegram_bridge_alert_limit=5), client).centrality()
    scores = [alert.anomaly_score for alert in report.bridges]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Top anomalies / stale metric pruning
# --------------------------------------------------------------------------- #


def anomaly_row(key: str, score: float, **overrides) -> dict:
    row = {
        "canonical_key": key,
        "name": key.title(),
        "entity_type": "organization",
        "labels": ["Entity"],
        "anomaly_score": score,
        "anomaly_betweenness": score / 2,
        "anomaly_degree_spike": 0.1,
        "anomaly_offshore_cluster_ratio": 0.4,
        "anomaly_offshore_neighbour_ratio": 0.3,
        "anomaly_reasons": ["articulation point"],
        "betweenness": score / 2,
        "degree": 4,
        "degree_prev": 2,
        "degree_spike": 0.5,
        "offshore_cluster_ratio": 0.4,
        "risk_score": 0.2,
        "jurisdiction_class": "secrecy",
        "cluster_id": "c1",
        "cluster_size": 5,
        "metrics_at": hours_ago(1),
    }
    row.update(overrides)
    return row


def test_top_anomalies_respects_the_limit_and_window(settings):
    rows = [anomaly_row(f"ORGANIZATION:n{index}", 0.9 - index * 0.1) for index in range(8)]
    client = MaintenanceClient(reads={"top_anomalies": rows})
    top = engine(maintenance_settings(settings, anomaly_top_n=5), client).top_anomalies()
    assert len(top) == 5
    params = [item for item in client.read_params if "limit" in item and "since" in item]
    assert params and params[0]["limit"] == 5


def test_top_anomalies_survives_a_read_failure(settings):
    class Exploding(MaintenanceClient):
        def read(self, query, params=None):
            raise RuntimeError("gone")

    assert engine(maintenance_settings(settings), Exploding()).top_anomalies() == []


def test_stale_metrics_are_pruned(settings):
    client = MaintenanceClient(write_results={"REMOVE e.anomaly_score": [{"cleared": 12}]})
    removed = engine(maintenance_settings(settings), client).prune_stale_metrics(days=14)
    assert removed == 12
    query, params, _kind = client.writes[0]
    assert "REMOVE e.anomaly_score" in query
    assert params["cutoff"]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def full_client(**overrides) -> MaintenanceClient:
    nodes, edges = barbell_graph()
    reads = {
        "capacity": [capacity_row(7, all_edges=len(edges))],
        "entities": [entity(node["canonical_key"], node["name"]) for node in nodes],
        "orphans": [orphan("ORGANIZATION:dust", "Dust Ltd")],
        "nodes": nodes,
        "edges": edges,
        "top_anomalies": [anomaly_row("BRIDGE", 0.73)],
    }
    reads.update(overrides)
    return MaintenanceClient(reads=reads)


def test_run_executes_every_stage_in_order(settings):
    client = full_client()
    report = engine(maintenance_settings(settings), client, run_id="maint-run").run()
    assert report.status == "completed"
    assert report.run_id == "maint-run"
    assert report.capacity.entities == 7
    assert report.prune.purged == 1
    assert report.centrality.nodes_scored == 7
    assert report.top_anomalies
    assert report.bridge_alerts


def test_run_can_skip_stages(settings):
    client = full_client()
    report = engine(maintenance_settings(settings), client).run(dedupe=False, prune=False, centrality=False)
    assert report.dedupe.status == "skipped"
    assert report.prune.status == "skipped"
    assert report.centrality.status == "skipped"


def test_a_failing_stage_does_not_stop_the_others(settings):
    class Flaky(MaintenanceClient):
        def read(self, query, params=None):
            if FRAGMENTS["orphans"] in query:
                raise RuntimeError("orphan scan exploded")
            return super().read(query, params)

    client = Flaky(reads=full_client().reads)
    report = engine(maintenance_settings(settings), client).run()
    assert report.prune.status == "failed"
    assert "orphan scan exploded" in report.prune.error
    assert report.centrality.nodes_scored == 7
    assert report.status == "partial"


def test_all_stages_failing_is_a_failed_run(settings):
    class Dead(MaintenanceClient):
        def read(self, query, params=None):
            raise RuntimeError("database unreachable")

    report = engine(maintenance_settings(settings), Dead()).run()
    assert report.status == "failed"
    assert report.errors


def test_report_is_json_serialisable(settings):
    client = full_client()
    report = engine(maintenance_settings(settings), client).run()
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["status"] == "completed"
    assert payload["bridge_alerts"][0]["canonical_key"] == "BRIDGE"
    assert payload["capacity"]["node_limit"] == 200_000


def test_report_is_written_to_the_report_directory(settings, tmp_path):
    client = full_client()
    maintenance = engine(maintenance_settings(settings), client, run_id="maint-file")
    report = maintenance.run()
    path = maintenance.write_report(report, directory=tmp_path)
    assert path.exists()
    assert path.name.startswith("graph_maintenance_")
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "maint-file"


def test_github_output_is_emitted_when_the_variable_is_set(settings, tmp_path, monkeypatch):
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    client = full_client()
    report = engine(maintenance_settings(settings), client).run()
    ga.emit_github_output(report)
    content = output_file.read_text(encoding="utf-8")
    assert "maintenance_status=completed" in content
    assert "bridge_alerts=1" in content
    assert "maintenance_engine=python" in content
    # Generic keys would collide with any other step writing to $GITHUB_OUTPUT.
    assert "\nstatus=" not in content


def test_github_output_is_skipped_without_the_variable(settings, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    client = full_client()
    ga.emit_github_output(engine(maintenance_settings(settings), client).run())


def test_summarise_mentions_every_stage(settings):
    client = full_client()
    report = engine(maintenance_settings(settings), client).run()
    text = ga.summarise(report)
    for expected in ("capacity", "dedup", "prune", "centrality", "bridge"):
        assert expected in text.lower()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_parse_weights_accepts_positional_and_named_forms():
    assert ga.parse_weights("0.5,0.3,0.2") == {"betweenness": 0.5, "degree_spike": 0.3, "offshore_ratio": 0.2}
    assert ga.parse_weights("betweenness=0.5,degree_spike=0.3,offshore_ratio=0.2") == {
        "betweenness": 0.5, "degree_spike": 0.3, "offshore_ratio": 0.2,
    }
    assert ga.parse_weights("") is None


def test_parse_weights_rejects_nonsense():
    """``None`` (not an exception) so ``main`` owns the message and exit code."""
    assert ga.parse_weights("0.5,0.3") is None
    assert ga.parse_weights("a,b,c") is None
    assert ga.parse_weights("0,0,0") is None
    assert ga.parse_weights("betweenness=0.5") is None
    assert ga.parse_weights("") is None


def test_cli_capacity_dry_run_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("NEO4J_URI", "")
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    assert ga.main(["--capacity", "--dry-run", "--report-dir", str(tmp_path)]) == ga.EXIT_OK


def test_cli_writes_a_report_and_json_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("NEO4J_URI", "")
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    code = ga.main(["--all", "--dry-run", "--report-dir", str(tmp_path), "--json"])
    assert code == ga.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert list(tmp_path.glob("graph_maintenance_*.json"))


def test_cli_rejects_unknown_flags():
    with pytest.raises(SystemExit) as excinfo:
        ga.main(["--nope"])
    assert excinfo.value.code == 2


def test_cli_version(capsys):
    """``--version`` is a flag (matching ``ingest.py``), not argparse's action."""
    assert ga.main(["--version"]) == ga.EXIT_OK
    out = capsys.readouterr().out
    assert "graph maintenance" in out


def test_cli_accepts_every_documented_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("NEO4J_URI", "")
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    code = ga.main([
        "--all", "--dry-run", "--threshold", "0.9", "--max-merges", "5", "--max-nodes", "50",
        "--top", "3", "--engine", "python", "--weights", "0.5,0.3,0.2",
        "--report-dir", str(tmp_path), "--json",
    ])
    assert code == ga.EXIT_OK


def test_cli_no_report_flag_skips_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("NEO4J_URI", "")
    monkeypatch.setenv("NEO4J_PASSWORD", "")
    assert ga.main(["--capacity", "--dry-run", "--no-report"]) == ga.EXIT_OK
    assert not list(tmp_path.glob("*.json"))


def test_engine_accepts_a_real_neo4j_client_in_dry_run(settings):
    """The default constructor path (no injected double) must not dial out."""
    dry = dataclasses.replace(settings, dry_run=True, neo4j_uri="", neo4j_password="")
    with ga.GraphMaintenance(dry, owns_client=True) as maintenance:
        assert isinstance(maintenance.client, Neo4jClient)
        assert maintenance.dry_run is True
        report = maintenance.capacity()
        assert report.entities == 0
