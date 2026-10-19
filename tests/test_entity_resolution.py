"""APOC entity resolution: name folding, blocking, guards and evidence integrity.

Nothing here touches a database. ``StubClient`` exposes the three members the
engine uses (``read``/``write``/``dry_run``) and answers reads by matching a
fragment of the statement, so the Cypher is asserted as text and the merge
policy — thresholds, floors, homonym guard, identifier conflicts, winner choice
— is asserted as behaviour.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import entity_resolution as er


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class StubClient:
    """Scripted read/write client with a dry-run switch."""

    def __init__(self, *, reads=None, dry_run: bool = False) -> None:
        self.reads = dict(reads or {})
        self.dry_run = dry_run
        self.read_queries: list[str] = []
        self.read_params: list[dict] = []
        self.writes: list[tuple[str, dict, str]] = []

    def read(self, query, params=None):
        self.read_queries.append(query)
        self.read_params.append(dict(params or {}))
        for fragment, rows in self.reads.items():
            if fragment in query:
                return rows() if callable(rows) else list(rows)
        return []

    def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
        self.writes.append((query, dict(params or {}), kind))
        return []

    def queries(self, fragment: str) -> list[tuple[str, dict, str]]:
        return [entry for entry in self.writes if fragment in entry[0]]


def row(key: str, name: str, *, entity_type="Organization", mentions=1, degree=1, confidence=0.8, **extra):
    base = {
        "canonical_key": key,
        "name": name,
        "name_key": er.name_key(name),
        "phonetic_key": er.phonetic_key(name),
        "entity_type": entity_type,
        "mention_count": mentions,
        "confidence": confidence,
        "degree": degree,
        "reg_number": None,
        "company_number": None,
        "lei": None,
        "imo": None,
        "mmsi": None,
        "tail_number": None,
        "icao24": None,
        "wikidata_id": None,
        "address_key": None,
    }
    base.update(extra)
    return base


def candidate(entity_row):
    """The map the Cypher scoring query projects (``key`` + the row fields)."""
    return {"key": entity_row["canonical_key"], **entity_row}


def pair(left, right, *, jw=0.97, lev=0.95):
    return {"left": left, "right": right, "jaro_winkler": jw, "levenshtein": lev, "score": er.match_score(jw, lev), "method": "apoc"}


def engine(settings, client, **overrides):
    from puppetnet.config import Settings

    base = settings if settings is not None else Settings()
    tuned = dataclasses.replace(base, resolution_entity_limit=100, resolution_max_merges=50, **overrides)
    return er.EntityResolutionEngine(tuned, client, run_id="resolve-test")


def apoc_up(rows=None):
    return {
        "apoc.version": rows if rows is not None else [{"version": "5.26.0", "jw": 0.9, "lev": 0.88, "phonetic": "S530"}],
        "apoc.refactor.mergeNodes": [{"name": "apoc.refactor.mergeNodes"}],
    }


# --------------------------------------------------------------------------- #
# Name folding and metrics
# --------------------------------------------------------------------------- #
def test_name_key_folds_case_accents_legal_forms_and_transliterations():
    assert er.name_key("Müller & Söhne GmbH") == "mueller und soehne"
    assert er.name_key("Mueller und Soehne") == "mueller und soehne"
    assert er.name_key("ACME, Inc.") == "acme"
    assert er.name_key("Acme GmbH & Co. KG") == "acme"
    assert er.name_key("  Société   Générale  SA ") == "societe generale"
    assert er.name_key("") == ""


def test_phonetic_key_buckets_phonetic_and_transliterated_variants():
    assert er.phonetic_key("Müller") == er.phonetic_key("Mueller") == "M460"
    assert er.phonetic_key("Schmidt") == er.phonetic_key("Schmitt") == "S530"
    assert er.phonetic_key("Acme GmbH") != er.phonetic_key("Globex")
    assert er.phonetic_key("") == ""
    # Non-Latin scripts keep their folded form: Soundex is meaningless there,
    # but exact equality still catches ё/е spelling variants.
    assert er.phonetic_key("Ромашка").startswith("u:")
    assert er.phonetic_key("Ромашка") == er.phonetic_key("ромашка")


def test_jaro_winkler_is_the_standard_metric():
    assert er.jaro_winkler_similarity("martha", "marhta") == pytest.approx(0.961111, abs=1e-5)
    assert er.jaro_winkler_similarity("test", "test") == 1.0
    assert er.jaro_winkler_similarity("test", "") == 0.0
    assert er.jaro_winkler_similarity("hello", "world") == pytest.approx(0.4667, abs=1e-3)


def test_levenshtein_similarity_is_normalised():
    assert er.levenshtein_similarity("kitten", "sitting") == pytest.approx(1 - 3 / 7)
    assert er.levenshtein_similarity("same", "same") == 1.0
    assert er.levenshtein_similarity("abc", "") == 0.0


def test_match_score_is_the_weighted_gate():
    assert er.match_score(1.0, 1.0) == 1.0
    assert er.match_score(0.9, 0.8) == pytest.approx(0.6 * 0.9 + 0.4 * 0.8)
    assert er.match_score(0.0, 0.0) == 0.0


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #
def test_build_blocks_groups_by_type_phonetic_key_and_prefix():
    rows = [
        row("A", "Smith GmbH"),
        row("B", "Smyth AG"),
        row("C", "Smith Holdings"),
    ]
    blocks = er.build_blocks(rows)
    phonetic = [block for block in blocks if block["kind"] == "phonetic"]
    prefix = [block for block in blocks if block["kind"] == "prefix"]
    # Schmidt/Schmitt share the Soundex code; Schmidt GmbH and Schmidt Holdings
    # share the folded prefix.
    assert any(set(block["keys"]) == {"A", "B"} for block in phonetic)
    assert any(set(block["keys"]) == {"A", "C"} for block in prefix)
    assert all(block["id"].startswith(("phonetic:", "prefix:")) for block in blocks)


def test_build_blocks_never_mixes_entity_types():
    blocks = er.build_blocks([row("A", "Acme", entity_type="Organization"), row("B", "Acme", entity_type="Location")])
    assert blocks == []


def test_build_blocks_skips_oversized_buckets():
    rows = [row(f"K{index}", "Unknown") for index in range(10)]
    assert er.build_blocks(rows, max_block_size=5) == []


def test_build_blocks_emits_overlapping_groups_once():
    rows = [row("A", "Acme"), row("B", "Acme")]
    blocks = er.build_blocks(rows)
    assert len(blocks) == 1, "phonetic and prefix buckets cover the same pair"


def test_engine_batches_blocks_by_key_count():
    blocks = [{"id": str(index), "keys": [f"K{index}-{n}" for n in range(10)]} for index in range(5)]
    batches = list(er.EntityResolutionEngine._batch_blocks(blocks, max_keys=25))
    assert [sum(len(block["keys"]) for block in batch) for batch in batches] == [20, 20, 10]


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def test_winner_is_the_node_with_more_mentions_then_degree():
    engine_obj = engine(None, StubClient())
    left = row("A", "Schmidt", mentions=3, degree=4)
    right = row("B", "Schmitt", mentions=9, degree=1)
    decisions, conflicts, below = engine_obj._decide([pair(left, right)], threshold=0.9, max_merges=10)
    assert conflicts == below == 0
    assert decisions[0].winner_key == "B"
    assert decisions[0].loser_key == "A"


def test_a_pair_must_clear_the_weighted_score_and_both_metric_floors():
    engine_obj = engine(None, StubClient())
    # Weighted score passes (0.6*0.95 + 0.4*0.60 = 0.81 < 0.9, so lower the
    # threshold to isolate the floor logic).
    good = pair(row("A", "Alpha"), row("B", "Alphi"), jw=0.95, lev=0.90)
    bad_lev = pair(row("C", "Alpha"), row("D", "Alphi"), jw=1.0, lev=0.60)
    decisions, _, below = engine_obj._decide([good, bad_lev], threshold=0.80, max_merges=10)
    assert len(decisions) == 1
    assert below == 1, "the Levenshtein floor must reject the pair with a perfect Jaro-Winkler"


def test_different_strict_identifiers_block_a_merge():
    engine_obj = engine(None, StubClient())
    left = row("A", "Schmidt GmbH", reg_number="HRB 1")
    right = row("B", "Schmitt AG", reg_number="HRB 2")
    decisions, conflicts, below = engine_obj._decide([pair(left, right)], threshold=0.9, max_merges=10)
    assert decisions == [] and conflicts == 1 and below == 0


def test_equal_strict_identifiers_are_proof_and_override_the_threshold():
    engine_obj = engine(None, StubClient())
    left = row("A", "Schmidt GmbH", reg_number="HRB 1")
    right = row("B", "A totally different name", reg_number="HRB 1")
    decisions, conflicts, _ = engine_obj._decide([pair(left, right, jw=0.1, lev=0.1)], threshold=0.99, max_merges=10)
    assert conflicts == 0
    assert len(decisions) == 1
    assert decisions[0].method == "strict_id"
    assert decisions[0].reasons == ["shared_reg_number"]


def test_a_node_is_merged_at_most_once_per_run():
    engine_obj = engine(None, StubClient())
    a, b, c = row("A", "Schmidt"), row("B", "Schmitt"), row("C", "Schmidtt")
    decisions, _, _ = engine_obj._decide(
        [pair(a, b, jw=0.99, lev=0.99), pair(b, c, jw=0.98, lev=0.98)],
        threshold=0.9,
        max_merges=10,
    )
    keys = [key for decision in decisions for key in (decision.winner_key, decision.loser_key)]
    assert len(keys) == len(set(keys))


def test_homonym_guard_protects_pairs_that_share_a_document():
    client = StubClient(reads={"<-[:MENTIONS]-(d:Document)-[:MENTIONS]->": [{"left_key": "A", "right_key": "B", "doc_id": "doc-1"}]})
    engine_obj = engine(None, client)
    decision = er.MergeCandidate(winner_key="A", loser_key="B", winner_name="Ivan Petrov", loser_name="Ivan Petrov", score=0.99)
    kept, protected = engine_obj._guard_homonyms([decision], limit=10)
    assert kept == []
    assert protected[0]["reason"] == "co_occurrence"


def test_homonym_guard_fails_closed_when_it_cannot_run():
    class Broken(StubClient):
        def read(self, query, params=None):
            if "<-[:MENTIONS]-(d:Document)" in query:
                raise RuntimeError("index offline")
            return super().read(query, params)

    engine_obj = engine(None, Broken())
    decision = er.MergeCandidate(winner_key="A", loser_key="B", score=0.99)
    kept, protected = engine_obj._guard_homonyms([decision], limit=10)
    assert kept == []
    assert protected[0]["reason"] == "guard_unavailable"


# --------------------------------------------------------------------------- #
# Prepare / probe
# --------------------------------------------------------------------------- #
def test_prepare_writes_both_keys_before_scoring():
    pending = {"rows": [{"canonical_key": "A", "name": "Müller GmbH"}]}

    def next_batch():
        rows, pending["rows"] = pending["rows"], []
        return rows

    client = StubClient(reads={"e.name_key IS NULL OR e.phonetic_key IS NULL": next_batch})
    engine_obj = engine(None, client)
    prepared = engine_obj.prepare(limit=10)
    assert prepared == 1
    written = [entry for entry in client.writes if "SET e.name_key" in entry[0]]
    assert written and written[0][1]["rows"][0]["name_key"] == "mueller"
    assert written[0][1]["rows"][0]["phonetic_key"] == "M460"
    assert any("puppetnet_entity_name_key" in query for query, _, _ in client.writes)
    assert any("puppetnet_entity_phonetic_key" in query for query, _, _ in client.writes)


def test_probe_reports_apoc_when_all_functions_are_present():
    client = StubClient(reads={**apoc_up()})
    engine_obj = engine(None, client)
    assert engine_obj.probe_apoc() is True
    assert engine_obj.apoc_version == "5.26.0"
    assert engine_obj.apoc_merge_available is True


def test_probe_degrades_when_apoc_is_missing():
    class NoApoc(StubClient):
        def read(self, query, params=None):
            if "apoc.version" in query:
                raise RuntimeError("There is no procedure with the name `apoc.version`")
            return super().read(query, params)

    engine_obj = engine(None, NoApoc())
    assert engine_obj.probe_apoc() is False
    assert engine_obj.apoc_available is False
    assert engine_obj.apoc_merge_available is False


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def resolution_client(*, guard_rows=(), after_evidence=2, after_mentions=2, dry_run=False):
    reads = {
        **apoc_up(),
        "e.name_key IS NULL OR e.phonetic_key IS NULL": [],
        "OPTIONAL MATCH (e)-[r]-()": [
            row("A", "Schmidt GmbH", mentions=4, degree=3),
            row("B", "Schmitt AG", mentions=2, degree=1),
            row("C", "Globex", mentions=1, degree=1),
        ],
        "block_id, a AS left, b AS right": [
            {
                "block_id": "phonetic:Organization:S530",
                "left": candidate(row("A", "Schmidt GmbH", mentions=4, degree=3)),
                "right": candidate(row("B", "Schmitt AG", mentions=2, degree=1)),
                "jw": 0.97,
                "lev": 0.95,
            }
        ],
        "<-[:MENTIONS]-(d:Document)-[:MENTIONS]->": list(guard_rows),
        "winner_evidence": [
            {"winner_key": "A", "loser_key": "B", "winner_evidence": 1, "loser_evidence": 1, "winner_mentions": 1, "loser_mentions": 1}
        ],
        "AS evidence": [{"winner_key": "A", "evidence": after_evidence, "mentions": after_mentions}],
    }
    return StubClient(reads=reads, dry_run=dry_run)


def test_run_merges_duplicates_with_apoc_and_preserves_evidence(settings):
    client = resolution_client()
    report = engine(settings, client).run()

    assert report.status == "completed"
    assert report.engine == "apoc"
    assert report.merges_planned == 1
    assert report.merges_applied == 1
    assert report.nodes_removed == 1
    assert report.evidence_pairs_checked == 1
    assert report.evidence_preserved == 1
    assert report.evidence_missing == 0
    assert report.evidence_integrity is True

    merges = [entry for entry in client.writes if "apoc.refactor.mergeNodes" in entry[0]]
    assert merges, "the merge must go through apoc.refactor.mergeNodes"
    statement = merges[0][0]
    assert "mergeRels: false" in statement, "evidence relationships must be moved, never folded"
    assert "properties: 'discard'" in statement, "the pre-computed roll-up must win"
    assert "produceSelfRef: false" in statement
    # The roll-up and the merge are one statement, so a failed merge cannot
    # leave a winner claiming the loser's mentions.
    assert "coalesce(winner.mention_count, 0) + coalesce(loser.mention_count, 0)" in statement
    assert "node.mention_count = mentions" in statement
    assert len(merges) == 1


def test_run_refuses_to_merge_when_evidence_would_be_lost(settings):
    client = resolution_client(after_evidence=0, after_mentions=0)
    report = engine(settings, client).run()
    assert report.evidence_missing == 1
    assert report.evidence_integrity is False
    assert report.status == "completed", "the merge happened; the audit is what failed"


def test_run_protects_entities_mentioned_in_the_same_document(settings):
    client = resolution_client(guard_rows=[{"left_key": "A", "right_key": "B", "doc_id": "doc-1"}])
    report = engine(settings, client).run()
    assert report.merges_planned == 0
    assert report.homonyms_protected == 1
    assert not any("apoc.refactor.mergeNodes" in query for query, _, _ in client.writes)


def test_scoring_can_use_apoc_while_the_merge_procedure_is_missing(settings):
    client = StubClient(reads={"apoc.version": [{"version": "5.26.0", "jw": 0.9, "lev": 0.88, "phonetic": "S530"}]})
    engine_obj = engine(settings, client)
    assert engine_obj.probe_apoc() is True
    assert engine_obj.apoc_merge_available is False


def test_run_scores_in_python_when_apoc_is_unavailable_and_refuses_to_merge(settings):
    class NoApoc(StubClient):
        def read(self, query, params=None):
            if "apoc.version" in query:
                raise RuntimeError("no apoc")
            return super().read(query, params)

    client = NoApoc(
        reads={
            "e.name_key IS NULL OR e.phonetic_key IS NULL": [],
            "OPTIONAL MATCH (e)-[r]-()": [
                row("A", "Schmidt GmbH", mentions=4, degree=3),
                row("B", "Schmitt AG", mentions=2, degree=1),
            ],
            # The fallback query returns names; scoring happens in Python.
            "block_id, a AS left, b AS right": [
                {
                    "block_id": "phonetic:Organization:S530",
                    "left": candidate(row("A", "Schmidt GmbH", mentions=4)),
                    "right": candidate(row("B", "Schmitt AG", mentions=2)),
                }
            ],
            "<-[:MENTIONS]-(d:Document)-[:MENTIONS]->": [],
        }
    )
    report = engine(settings, client).run()
    assert report.engine == "python"
    assert report.merges_planned == 1
    assert report.merges_applied == 0
    assert any("unavailable" in error for error in report.errors)
    assert not any("apoc.refactor.mergeNodes" in query for query, _, _ in client.writes)


def test_dry_run_plans_merges_without_claiming_them(settings):
    client = resolution_client(dry_run=True)
    report = engine(settings, client).run()

    assert report.dry_run is True
    assert report.merges_planned == 1
    assert report.merges_applied == 0
    assert report.nodes_removed == 0
    # The statements are still recorded, so --dry-run validates the Cypher.
    assert any("apoc.refactor.mergeNodes" in query for query, _, _ in client.writes)


def test_empty_graph_is_reported_as_empty(settings):
    client = StubClient(reads={**apoc_up(), "e.name_key IS NULL OR e.phonetic_key IS NULL": [], "OPTIONAL MATCH (e)-[r]-()": []})
    report = engine(settings, client).run()
    assert report.status == "empty"
    assert report.merges_applied == 0


def test_disabled_resolution_does_not_touch_the_database(settings):
    client = StubClient()
    report = engine(settings, client, resolution_enabled=False).run()
    assert report.status == "disabled"
    assert client.read_queries == [] and client.writes == []


def test_report_round_trips_through_json(settings, tmp_path):
    engine_obj = engine(settings, resolution_client())
    report = engine_obj.run()
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["merges_applied"] == 1
    path = engine_obj.write_report(report, directory=tmp_path)
    assert path.name.startswith("entity_resolution_")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_dry_run_returns_zero():
    assert er.main(["--dry-run", "--json", "--no-report"]) == er.EXIT_OK


def test_cli_prepare_only_dry_run_returns_zero():
    assert er.main(["--prepare-only", "--dry-run", "--no-report"]) == er.EXIT_OK


def test_cli_rejects_a_bad_threshold(monkeypatch):
    monkeypatch.setenv("RESOLUTION_THRESHOLD", "1.5")
    assert er.main(["--dry-run", "--no-report"]) == er.EXIT_CONFIG
