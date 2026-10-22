"""Offline contracts for local-first NLP, public export and daily reservations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from src.export_static_slices import build_public_graph, export_slices, reserve_run
from src.token_saver_nlp import GeminiFallback, TokenSaverEngine, clean_html


def graph():
    return {"nodes": [
        {"id": "../../person:a", "name": "Anna", "type": "Person", "public": True,
         "updatedAt": "2026-10-19", "risk_score": .9, "private_notes": "SECRET", "doc_ids": ["doc:1", "secret"]},
        {"id": "org:b", "name": "Beispiel GmbH", "type": "Organization", "public": True},
        {"id": "secret", "name": "Private Person", "type": "Person", "public": False},
    ], "edges": [
        {"source": "../../person:a", "target": "org:b", "type": "FOUNDED", "public": True},
        {"source": "../../person:a", "target": "secret", "type": "OWNS", "public": True},
        {"source": "org:b", "target": "../../person:a", "type": "OWNS", "public": False},
    ], "evidence": [{"id": "doc:1", "title": "Public register", "public": True, "url": "https://example.org/"},
                    {"id": "secret", "title": "Private document", "public": False}]}


def test_export_approved_only_allowlist_and_reference_integrity(tmp_path):
    data = export_slices(graph(), tmp_path)
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    assert data["nodes"][0]["evidenceIds"] == ["doc:1"]
    assert "SECRET" not in json.dumps(data)
    assert "risk_score" not in json.dumps(data)
    assert "Private document" not in json.dumps(data)
    assert len(list((tmp_path / "entities").glob("*.json"))) == 2
    assert not (tmp_path.parent / "person:a.json").exists()
    manifest = json.loads((tmp_path / "static-manifest.json").read_text())
    assert manifest["revision"] == data["revision"]
    assert set(manifest["entityIds"]) == {node["id"] for node in data["nodes"]}


def test_export_is_stable_and_stale_files_removed(tmp_path):
    first = export_slices(graph(), tmp_path)
    mtime = (tmp_path / "graph_latest.json").stat().st_mtime_ns
    assert export_slices(graph(), tmp_path) == first
    assert (tmp_path / "graph_latest.json").stat().st_mtime_ns == mtime
    raw = graph()
    raw["nodes"][0]["public_released"] = False
    export_slices(raw, tmp_path)
    assert len(list((tmp_path / "entities").glob("*.json"))) == 1
    assert json.loads((tmp_path / "graph_latest.json").read_text())["edges"] == []


def analytics_graph():
    """Two disconnected triangles: deterministic communities, degrees and dates."""
    return {"nodes": [
        {"id": "person:a", "name": "Anna", "type": "Person", "public": True,
         "doc_ids": ["doc:2", "doc:1"]},
        {"id": "org:b", "name": "Beta GmbH", "type": "Organization", "public": True},
        {"id": "org:c", "name": "Centaur AG", "type": "Organization", "public": True},
        {"id": "loc:d", "name": "Davos", "type": "Location", "public": True},
        {"id": "loc:e", "name": "Engadin", "type": "Location", "public": True},
        {"id": "loc:f", "name": "Flims", "type": "Location", "public": True},
    ], "edges": [
        {"source": "person:a", "target": "org:b", "type": "FOUNDED", "public": True},
        {"source": "person:a", "target": "org:c", "type": "OWNS", "public": True},
        {"source": "org:b", "target": "org:c", "type": "ASSOCIATED_WITH", "public": True},
        {"source": "loc:d", "target": "loc:e", "type": "ASSOCIATED_WITH", "public": True},
        {"source": "loc:e", "target": "loc:f", "type": "ASSOCIATED_WITH", "public": True},
        {"source": "loc:f", "target": "loc:d", "type": "ASSOCIATED_WITH", "public": True},
    ], "evidence": [
        {"id": "doc:1", "title": "Register", "public": True, "date": "2026-03-01T10:00:00Z"},
        {"id": "doc:2", "title": "Bericht", "public": True, "date": "2026-01-15"},
        {"id": "doc:bad", "title": "Ohne Datum", "public": True, "date": "unknown"},
    ]}


def test_export_analytics_communities_degrees_and_temporal_spans(tmp_path):
    data = export_slices(analytics_graph(), tmp_path)
    by_id = {node["id"]: node for node in data["nodes"]}
    analytics = data["analytics"]
    # Two disconnected triangles => exactly two communities, stable naming.
    assert analytics["communities"] == 2
    assert analytics["largestCommunity"] == 3
    assert {by_id["person:a"]["community"], by_id["loc:d"]["community"]} == {"c000", "c001"}
    assert by_id["org:b"]["community"] == by_id["person:a"]["community"]
    assert by_id["loc:e"]["community"] == by_id["loc:d"]["community"]
    # Undirected degrees over published edges only.
    assert by_id["person:a"]["degree"] == 2
    assert by_id["org:b"]["degree"] == 2
    # PageRank: normalized, positive, hub list sorted by rank descending.
    assert abs(sum(node["rank"] for node in data["nodes"]) - 1.0) < 1e-3
    assert all(node["rank"] > 0 for node in data["nodes"])
    ranks = [hub["rank"] for hub in analytics["hubs"]]
    assert ranks == sorted(ranks, reverse=True) and len(analytics["hubs"]) == 6
    assert analytics["nodes"] == 6 and analytics["edges"] == 6
    assert analytics["density"] == round(6 / 30, 6)
    # Temporal spans aggregate ISO dates of published evidence only.
    assert by_id["person:a"]["firstSeen"] == "2026-01-15"
    assert by_id["person:a"]["lastSeen"] == "2026-03-01"
    assert "firstSeen" not in by_id["loc:d"] and "lastSeen" not in by_id["loc:d"]
    assert analytics["temporalRange"] == {"first": "2026-01-15", "last": "2026-03-01"}
    # Entity slices inherit the enrichment.
    entity_files = list((tmp_path / "entities").glob("*.json"))
    assert len(entity_files) == 6
    assert all("rank" in json.loads(path.read_text())["entity"] for path in entity_files)


def test_export_analytics_are_deterministic_and_handle_empty_graphs(tmp_path):
    first = export_slices(analytics_graph(), tmp_path)
    second_dir = tmp_path / "again"
    second = export_slices(analytics_graph(), second_dir)
    assert first == second  # stable revision digest requires reproducible analytics
    assert (tmp_path / "graph_latest.json").read_bytes() == (second_dir / "graph_latest.json").read_bytes()
    empty = export_slices({"nodes": [], "edges": []}, tmp_path / "empty", allow_empty=True)
    assert empty["analytics"] == {"version": 1, "nodes": 0, "edges": 0, "communities": 0,
                                  "largestCommunity": 0, "density": 0.0, "hubs": []}


def test_export_analytics_scale_is_bounded(tmp_path):
    import time
    count = 1500
    nodes = [{"id": f"org:{i}", "name": f"Org {i}", "type": "Organization", "public": True}
             for i in range(count)]
    edges = [{"source": f"org:{i}", "target": f"org:{(i * 7 + 1) % count}", "type": "ASSOCIATED_WITH",
              "public": True} for i in range(count)]
    edges += [{"source": f"org:{i}", "target": f"org:{(i + 1) % count}", "type": "ASSOCIATED_WITH",
               "public": True} for i in range(count)]
    started = time.monotonic()
    data = export_slices({"nodes": nodes, "edges": edges, "evidence": []}, tmp_path)
    assert time.monotonic() - started < 10  # export budget is 45s; analytics stay far below
    assert data["analytics"]["nodes"] == count
    assert all("rank" in node and "community" in node and "degree" in node for node in data["nodes"])


def insights_graph():
    """One pattern per Wave-4 correlation rule; ids chosen so export LPA stays empirically stable."""
    nodes = [
        {"id": "person:p", "name": "Proxy Person", "type": "Person", "public": True},
        {"id": "org:m", "name": "Mittler AG", "type": "Organization", "public": True},
        {"id": "org:t", "name": "Target Ltd", "type": "Organization", "public": True},
        {"id": "person:o", "name": "Officer Person", "type": "Person", "public": True},
        {"id": "org:x", "name": "X GmbH", "type": "Organization", "public": True},
        {"id": "org:y", "name": "Y GmbH", "type": "Organization", "public": True},
        {"id": "org:h1", "name": "Stern Hub AG", "type": "Organization", "public": True},
    ]
    edges = [
        {"source": "person:p", "target": "org:m", "type": "NOMINEE_OF", "public": True, "confidence": .8},
        {"source": "org:m", "target": "org:t", "type": "OWNS", "public": True, "confidence": .9},
        {"source": "person:o", "target": "org:x", "type": "OFFICER_OF", "public": True, "confidence": .9},
        {"source": "person:o", "target": "org:y", "type": "DIRECTOR_OF", "public": True, "confidence": .9},
        {"source": "org:x", "target": "org:y", "type": "CONTRACTED_WITH", "public": True, "confidence": .8},
    ]
    for i in range(8):  # star: hub with eight unconnected spokes
        nodes.append({"id": f"org:h1-s{i}", "name": f"Spoke {i}", "type": "Organization", "public": True})
        edges.append({"source": "org:h1", "target": f"org:h1-s{i}", "type": "ASSOCIATED_WITH", "public": True, "confidence": .8})
    for side in ("aa", "bb"):  # bridge: two 4-cliques joined by exactly one edge
        keys = [f"org:{side}-{j}" for j in range(4)]
        nodes += [{"id": key, "name": key, "type": "Organization", "public": True} for key in keys]
        edges += [{"source": a, "target": b, "type": "ASSOCIATED_WITH", "public": True, "confidence": .8}
                  for index, a in enumerate(keys) for b in keys[index + 1:]]
    edges.append({"source": "org:aa-0", "target": "org:bb-0", "type": "ASSOCIATED_WITH", "public": True, "confidence": .85})
    return {"nodes": nodes, "edges": edges, "evidence": []}


def test_export_insights_find_one_pattern_per_rule_family(tmp_path):
    data = export_slices(insights_graph(), tmp_path)
    insights = data["insights"]
    assert insights["version"] == 1
    assert insights["rules"] == {"nominee-chain": 1, "hub-spoke": 1, "community-bridge": 1, "shared-officer": 1}
    items = insights["items"]
    assert sorted(item["rule"] for item in items) == ["community-bridge", "hub-spoke", "nominee-chain", "shared-officer"]
    edge_ids = {edge["id"] for edge in data["edges"]}
    node_ids = {node["id"] for node in data["nodes"]}
    for item in items:
        assert item["title"] and item["description"] and 0 <= item["support"] <= 1
        assert all(reference in edge_ids for reference in item["evidence"]), "insights must cite real edges"
        assert all(entity["id"] in node_ids and entity["name"] for entity in item["entities"])
    chain = next(item for item in items if item["rule"] == "nominee-chain")
    assert [entity["id"] for entity in chain["entities"]] == ["person:p", "org:m", "org:t"]
    assert "Strohmann" in chain["description"] and len(chain["evidence"]) == 2
    officer = next(item for item in items if item["rule"] == "shared-officer")
    assert len(officer["evidence"]) == 3
    hub = next(item for item in items if item["rule"] == "hub-spoke")
    assert hub["entities"][0]["id"] == "org:h1" and len(hub["entities"]) == 5
    assert hub["evidence"] == []


def test_export_insights_are_deterministic_and_absent_without_patterns(tmp_path):
    first = export_slices(insights_graph(), tmp_path / "a")
    second = export_slices(insights_graph(), tmp_path / "b")
    assert first["insights"] == second["insights"]
    assert first["revision"] == second["revision"]
    plain = export_slices(analytics_graph(), tmp_path / "plain")
    assert "insights" not in plain, "graphs without patterns stay byte-identical to pre-Wave-4 exports"
    empty = export_slices({"nodes": [], "edges": []}, tmp_path / "empty", allow_empty=True)
    assert "insights" not in empty


def test_export_insights_stay_bounded_under_a_flood_of_patterns(tmp_path):
    from collections import Counter
    nodes, edges = [], []
    for i in range(10):  # ten nominee chains
        nodes += [{"id": f"person:p{i}", "name": f"P{i}", "type": "Person", "public": True},
                  {"id": f"org:m{i}", "name": f"M{i}", "type": "Organization", "public": True},
                  {"id": f"org:t{i}", "name": f"T{i}", "type": "Organization", "public": True}]
        edges += [{"source": f"person:p{i}", "target": f"org:m{i}", "type": "NOMINEE_OF", "public": True, "confidence": .8},
                  {"source": f"org:m{i}", "target": f"org:t{i}", "type": "OWNS", "public": True, "confidence": .9}]
    for i in range(10):  # ten shared-officer triples
        nodes += [{"id": f"person:o{i}", "name": f"O{i}", "type": "Person", "public": True},
                  {"id": f"org:x{i}", "name": f"X{i}", "type": "Organization", "public": True},
                  {"id": f"org:y{i}", "name": f"Y{i}", "type": "Organization", "public": True}]
        edges += [{"source": f"person:o{i}", "target": f"org:x{i}", "type": "OFFICER_OF", "public": True, "confidence": .9},
                  {"source": f"person:o{i}", "target": f"org:y{i}", "type": "DIRECTOR_OF", "public": True, "confidence": .9},
                  {"source": f"org:x{i}", "target": f"org:y{i}", "type": "OWNS", "public": True, "confidence": .8}]
    for i in range(10):  # ten clique-pair bridges
        for side in ("a", "b"):
            keys = [f"org:{side}{i}-{j}" for j in range(4)]
            nodes += [{"id": key, "name": key, "type": "Organization", "public": True} for key in keys]
            edges += [{"source": a, "target": b, "type": "ASSOCIATED_WITH", "public": True, "confidence": .8}
                      for index, a in enumerate(keys) for b in keys[index + 1:]]
        edges += [{"source": f"org:a{i}-0", "target": f"org:b{i}-0", "type": "ASSOCIATED_WITH", "public": True, "confidence": .85}]
    nodes.append({"id": "org:hub", "name": "HUB", "type": "Organization", "public": True})
    for i in range(8):  # one star
        nodes.append({"id": f"org:hub-s{i}", "name": f"S{i}", "type": "Organization", "public": True})
        edges.append({"source": "org:hub", "target": f"org:hub-s{i}", "type": "ASSOCIATED_WITH", "public": True, "confidence": .8})
    data = export_slices({"nodes": nodes, "edges": edges, "evidence": []}, tmp_path)
    insights = data["insights"]
    assert insights["rules"] == {"nominee-chain": 10, "hub-spoke": 1, "community-bridge": 10, "shared-officer": 10}
    assert len(insights["items"]) == 24, "8 per rule and 24 overall, honest caps"
    assert Counter(item["rule"] for item in insights["items"]) == Counter(
        {"nominee-chain": 8, "hub-spoke": 1, "community-bridge": 8, "shared-officer": 7})


def test_empty_refuses_to_erase_existing(tmp_path):
    export_slices(graph(), tmp_path)
    original = (tmp_path / "graph_latest.json").read_bytes()
    with pytest.raises(ValueError, match="No releasable"):
        export_slices({"nodes": [], "edges": []}, tmp_path)
    assert (tmp_path / "graph_latest.json").read_bytes() == original
    assert export_slices({"nodes": [], "edges": []}, tmp_path, allow_empty=True)["nodes"] == []


@pytest.mark.parametrize("field", ["private", "noindex"])
def test_public_flag_never_overrides_private_or_noindex(field):
    raw = graph()
    raw["nodes"][0][field] = True
    assert len(build_public_graph(raw)["nodes"]) == 1
    assert build_public_graph(raw)["edges"] == []


def test_reviewed_edges_only_and_no_calculated_accusation():
    raw = graph()
    raw["edges"][0]["requires_review"] = True
    assert build_public_graph(raw)["edges"] == []
    raw["edges"][0].update(requires_review=False, type="PUPPET_MASTER_OF")
    assert build_public_graph(raw)["edges"] == []


def test_duplicate_node_rejected_and_limits_closed():
    raw = graph()
    raw["nodes"].append(dict(raw["nodes"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        build_public_graph(raw)
    with pytest.raises(ValueError, match="Expected"):
        build_public_graph({})


def test_daily_quota_is_durable_and_shared(tmp_path):
    path = tmp_path / "budget.json"
    # Two cron slots per UTC day guarantee the twice-daily publication ...
    assert reserve_run(path, "schedule", "2026-10-19")
    assert reserve_run(path, "schedule", "2026-10-19")
    assert not reserve_run(path, "schedule", "2026-10-19")
    # ... plus exactly one manual slot; afterwards the day is closed.
    assert reserve_run(path, "repository_dispatch", "2026-10-19")
    assert not reserve_run(path, "workflow_dispatch", "2026-10-19")
    assert json.loads(path.read_text()) == {"day": "2026-10-19", "total": 3, "scheduled": 2, "manual": 1}
    # A new UTC day replenishes; a corrupt ledger still fails closed.
    assert reserve_run(path, "schedule", "2026-10-20")
    path.write_text("CORRUPT")
    with pytest.raises(ValueError):
        reserve_run(path, "schedule")


def test_daily_quota_accepts_legacy_two_total_ledger_and_rejects_manual_overflow(tmp_path):
    path = tmp_path / "budget.json"
    # Old policy shape (total 2 = 1 cron + 1 manual) stays valid: the second
    # cron slot may still be taken, the manual slot is already spent.
    path.write_text(json.dumps({"day": "2026-10-19", "total": 2, "scheduled": 1, "manual": 1}))
    assert not reserve_run(path, "workflow_dispatch", "2026-10-19")
    assert reserve_run(path, "schedule", "2026-10-19")
    # A ledger with 2 manual entries exceeds the conservative manual bound: the
    # rest of the UTC day closes (fail closed) instead of silently rebalancing,
    # and the file is not rewritten. (Valid after a mid-day profile switch.)
    legacy = json.dumps({"day": "2026-10-19", "total": 2, "scheduled": 0, "manual": 2})
    path.write_text(legacy)
    assert not reserve_run(path, "schedule", "2026-10-19")
    assert not reserve_run(path, "workflow_dispatch", "2026-10-19")
    assert path.read_text() == legacy
    # Beyond the hard ceilings it is corruption and raises.
    path.write_text(json.dumps({"day": "2026-10-19", "total": 5, "scheduled": 0, "manual": 5}))
    with pytest.raises(ValueError, match="Invalid"):
        reserve_run(path, "schedule", "2026-10-19")


def test_daily_quota_public_profile_slots_and_hard_ceilings(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    monkeypatch.setenv("DAILY_SCHEDULED_SLOTS", "8")
    monkeypatch.setenv("DAILY_MANUAL_SLOTS", "3")
    assert all(reserve_run(path, "schedule", "2026-10-19") for _ in range(8))
    assert not reserve_run(path, "schedule", "2026-10-19")
    assert all(reserve_run(path, "workflow_dispatch", "2026-10-19") for _ in range(3))
    assert not reserve_run(path, "repository_dispatch", "2026-10-19")
    assert json.loads(path.read_text()) == {"day": "2026-10-19", "total": 11, "scheduled": 8, "manual": 3}
    # Switching back to the conservative profile mid-day closes the day.
    monkeypatch.delenv("DAILY_SCHEDULED_SLOTS")
    monkeypatch.delenv("DAILY_MANUAL_SLOTS")
    assert not reserve_run(path, "schedule", "2026-10-19")
    for bad in ("13", "0", "-1", "x"):
        monkeypatch.setenv("DAILY_SCHEDULED_SLOTS", bad)
        with pytest.raises(ValueError):
            reserve_run(path, "schedule", "2026-10-20")
    monkeypatch.setenv("DAILY_SCHEDULED_SLOTS", "2")
    monkeypatch.setenv("DAILY_MANUAL_SLOTS", "5")
    with pytest.raises(ValueError):
        reserve_run(path, "schedule", "2026-10-20")


def test_cleanup_removes_boilerplate_and_deduplicates():
    html = '<nav>Login</nav><script>SECRET</script><main><p>Anna gründet Acme.</p><p>Anna gründet Acme.</p></main><footer>Ads</footer>'
    assert clean_html(html) == "Anna gründet Acme."
    assert len(clean_html("a" * 500_000)) == 20_000
    assert "x@example.org" not in clean_html("Mail x@example.org")


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class Session:
    def __init__(self, count=80, answer="2"):
        self.count = count
        self.answer = answer
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith(":countTokens"):
            return Response({"totalTokens": self.count})
        return Response({"usageMetadata": {"totalTokenCount": 82},
                         "candidates": [{"content": {"parts": [{"text": self.answer}]}}]})


def cloud(tmp_path, monkeypatch, session=None):
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    monkeypatch.setenv("ENABLE_GEMINI_FALLBACK", "true")
    return GeminiFallback(tmp_path / "ledger.json", session or Session())


def test_cloud_counts_same_contents_and_total_budget(tmp_path, monkeypatch):
    session = Session()
    fallback = cloud(tmp_path, monkeypatch, session)
    assert fallback.classify("Anna gründet Acme.", "Anna", "Acme") == "FOUNDED"
    counted, generated = session.calls
    assert counted[1]["json"]["contents"] == generated[1]["json"]["contents"]
    assert generated[1]["json"]["generationConfig"]["maxOutputTokens"] + session.count <= 150
    assert not generated[1]["allow_redirects"]
    assert "test-not-a-real-key" not in generated[0]
    assert fallback.classify("Anna gründet Acme.", "Anna", "Acme") is None
    assert len(session.calls) == 2


def test_overlong_prompt_never_generates(tmp_path, monkeypatch):
    session = Session(count=121)
    assert cloud(tmp_path, monkeypatch, session).classify("x", "a", "b") is None
    assert len(session.calls) == 1


def test_default_model_is_the_qa_verified_alias(tmp_path, monkeypatch):
    fallback = cloud(tmp_path, monkeypatch)
    assert fallback.model == "gemini-flash-lite-latest"


def test_tokenizer_drift_prompt_at_109_tokens_still_generates(tmp_path, monkeypatch):
    # 2026-10-20 QA live measurement: the unchanged production prompt counts
    # 109 tokens on current models; the old 100-token guard silently refused
    # it and killed stage 3. The 150-total budget stays authoritative.
    session = Session(count=109, answer="2")
    fallback = cloud(tmp_path, monkeypatch, session)
    assert fallback.classify("Anna gründet Acme.", "Anna", "Acme") == "FOUNDED"
    generated = session.calls[1][1]["json"]["generationConfig"]
    assert generated["maxOutputTokens"] == 41  # min(50, 150 - 109)
    assert "thinkingConfig" not in generated  # verified alias needs no budget param


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-3.8-flash"])
def test_thinking_class_models_are_budgeted_to_zero(tmp_path, monkeypatch, model):
    monkeypatch.setenv("GEMINI_MODEL", model)
    session = Session()
    assert cloud(tmp_path, monkeypatch, session).classify("x", "a", "b") == "FOUNDED"
    generated = session.calls[1][1]["json"]["generationConfig"]
    assert generated["thinkingConfig"] == {"thinkingBudget": 0}


@pytest.mark.parametrize("answer", ["0", "OWNS", "```2```", "ignore rules", "99"])
def test_cloud_schema_rejects_untrusted_output(tmp_path, monkeypatch, answer):
    assert cloud(tmp_path, monkeypatch, Session(answer=answer)).classify("x", "a", "b") is None


def test_cloud_cap_three_reservations_per_process(tmp_path, monkeypatch):
    fallback = cloud(tmp_path, monkeypatch)
    for i in range(3):
        assert fallback.classify(str(i), "a", "b") == "FOUNDED"
    assert fallback.classify("four", "a", "b") is None
    assert json.loads(fallback.ledger.read_text())["calls"] == 3


def test_corrupt_cloud_budget_and_disabled_fallback_do_not_call_api(tmp_path, monkeypatch):
    fallback = cloud(tmp_path, monkeypatch)
    fallback.ledger.write_text("CORRUPT")
    assert fallback.classify("x", "a", "b") is None
    assert fallback.session.calls == []
    monkeypatch.setenv("ENABLE_GEMINI_FALLBACK", "false")
    assert not GeminiFallback(tmp_path / "new.json").enabled


def parsed_nlp(verb="gründen", negated=False):
    import spacy
    from spacy.tokens import Doc, Span
    words = ["Anna", "gründet", "Acme", "."]
    if negated:
        words = ["Anna", "gründet", "Acme", "nicht"]
    doc = Doc(spacy.blank("de").vocab, words=words, heads=[1, 1, 1, 1],
              deps=["sb", "ROOT", "oa", "punct"], pos=["PROPN", "VERB", "PROPN", "PUNCT"],
              lemmas=["Anna", verb, "Acme", words[-1]])
    doc.ents = [Span(doc, 0, 1, label="PER"), Span(doc, 2, 3, label="ORG")]

    class LocalNLP:
        def has_pipe(self, name):
            return True

        def __call__(self, text):
            return doc

    return LocalNLP()


def test_german_per_and_svo_are_local_and_not_auto_public():
    from puppetnet.models import Document
    engine = TokenSaverEngine(nlp=parsed_nlp())
    result = engine.parse_document(Document(doc_id="d1", source_id="s", url="", text="Anna gründet Acme."))
    assert len(result.entities) == 2
    assert result.entities[0].entity_type.value == "Person"
    assert result.relations[0].predicate.value == "FOUNDED"
    assert result.relations[0].extra["requires_review"] is True
    assert result.relations[0].extra["public"] is False


def test_negation_cannot_be_sent_to_cloud():
    from puppetnet.models import Document

    class Never:
        def classify(self, *args):
            raise AssertionError("must not call cloud")

    engine = TokenSaverEngine(nlp=parsed_nlp(negated=True), fallback=Never())
    assert engine.parse_document(Document(doc_id="d1", source_id="s", url="", text="x")).relations == []


def synthetic_nlp(words, heads, deps, pos, lemmas, ents):
    """A dependency tree as a spaCy ``Doc`` — the offline stand-in for de_core_news_sm."""
    import spacy
    from spacy.tokens import Doc, Span
    doc = Doc(spacy.blank("de").vocab, words=words, heads=heads, deps=deps, pos=pos, lemmas=lemmas)
    doc.ents = [Span(doc, start, end, label=label) for start, end, label in ents]

    class LocalNLP:
        def has_pipe(self, name):
            return True

        def __call__(self, text):
            return doc

    return LocalNLP()


class NeverCloud:
    """Proves the Wave-4 German copula path never reaches the Gemini fallback."""
    enabled = True

    def classify(self, *args):
        raise AssertionError("must not call cloud")


def test_wave4_german_lexicons_stay_inside_the_closed_vocabulary():
    from src.token_saver_nlp import KINSHIP_OR_CORPORATE, REVERSED_VERBS, ROLE_NOUNS, VERBS

    from puppetnet.models import RelationType
    names = set(RelationType.names())
    assert all(predicate in names for predicate in VERBS.values())
    assert all(predicate in names for predicate in REVERSED_VERBS.values())
    assert all(predicate in names for predicate in ROLE_NOUNS.values())
    for family, corporate in KINSHIP_OR_CORPORATE.values():
        assert family in names and (not corporate or corporate in names)
    assert len(VERBS) >= 30 and len(ROLE_NOUNS) >= 40, "Wave 4 promised substantial German lexicons"
    assert VERBS["gründen"] == "FOUNDED" and REVERSED_VERBS["sanktionieren"] == "SANCTIONED_BY"


def test_copula_role_noun_becomes_typed_officer_edge_without_cloud():
    from puppetnet.models import Document
    engine = TokenSaverEngine(nlp=synthetic_nlp(
        ["Anna", "ist", "Geschäftsführerin", "der", "Acme", "GmbH"],
        [1, 1, 1, 4, 1, 4], ["sb", "ROOT", "pd", "nk", "ag", "nk"],
        ["PROPN", "AUX", "NOUN", "DET", "PROPN", "PROPN"],
        ["Anna", "sein", "geschäftsführerin", "der", "Acme", "GmbH"],
        [(0, 1, "PER"), (4, 6, "ORG")]), fallback=NeverCloud())
    result = engine.parse_document(Document(doc_id="d1", source_id="s", url="", text="Anna ist Geschäftsführerin der Acme GmbH."))
    assert [(r.subject.name, r.predicate.value, r.obj.name) for r in result.relations] == [("Anna", "OFFICER_OF", "Acme GmbH")]
    relation = result.relations[0]
    assert relation.extra["role"] == "geschäftsführerin"
    assert relation.extra["public"] is False and relation.extra["requires_review"] is True


def test_copula_von_pp_and_kinship_disambiguation_by_entity_type():
    from puppetnet.models import Document
    tree = ([1, 1, 1, 2, 3], ["sb", "ROOT", "pd", "op", "nk"],
            ["PROPN", "AUX", "NOUN", "ADP", "PROPN"])

    def parse(words, lemmas, ents):
        heads, deps, pos = tree
        engine = TokenSaverEngine(nlp=synthetic_nlp(words, heads, deps, pos, lemmas, ents), fallback=NeverCloud())
        result = engine.parse_document(Document(doc_id="d1", source_id="s", url="", text="x"))
        return [(r.subject.name, r.predicate.value, r.obj.name) for r in result.relations]

    # Same grammar, two organisations: the corporate reading wins.
    assert parse(["Scania", "ist", "Tochter", "von", "VW"],
                 ["Scania", "sein", "tochter", "von", "VW"],
                 [(0, 1, "ORG"), (4, 5, "ORG")]) == [("Scania", "SUBSIDIARY_OF", "VW")]
    # Same grammar, two people: the family reading wins.
    assert parse(["Anna", "ist", "Tochter", "von", "Maria"],
                 ["Anna", "sein", "tochter", "von", "Maria"],
                 [(0, 1, "PER"), (4, 5, "PER")]) == [("Anna", "FAMILY_OF", "Maria")]


def test_copula_without_role_or_with_negation_yields_nothing_and_never_calls_cloud():
    from puppetnet.models import Document
    bare = TokenSaverEngine(nlp=synthetic_nlp(
        ["Anna", "ist", "klug"], [1, 1, 1], ["sb", "ROOT", "pd"],
        ["PROPN", "AUX", "ADJ"], ["Anna", "sein", "klug"], [(0, 1, "PER")]), fallback=NeverCloud())
    assert bare.parse_document(Document(doc_id="d1", source_id="s", url="", text="x")).relations == []
    negated = TokenSaverEngine(nlp=synthetic_nlp(
        ["Anna", "ist", "nicht", "Geschäftsführerin", "der", "Acme", "GmbH"],
        [1, 1, 1, 1, 6, 1, 6], ["sb", "ROOT", "adv", "pd", "nk", "ag", "nk"],
        ["PROPN", "AUX", "ADV", "NOUN", "DET", "PROPN", "PROPN"],
        ["Anna", "sein", "nicht", "geschäftsführerin", "der", "Acme", "GmbH"],
        [(0, 1, "PER"), (5, 7, "ORG")]), fallback=NeverCloud())
    assert negated.parse_document(Document(doc_id="d2", source_id="s", url="", text="x")).relations == []


def test_german_reversed_and_direct_verbs_keep_canonical_direction():
    from puppetnet.models import Document

    def triples(words, lemmas, ents):
        engine = TokenSaverEngine(nlp=synthetic_nlp(words, [1, 1, 1], ["sb", "ROOT", "oa"],
                                                    ["PROPN", "VERB", "PROPN"], lemmas, ents), fallback=NeverCloud())
        result = engine.parse_document(Document(doc_id="d", source_id="s", url="", text="x"))
        return [(r.subject.name, r.predicate.value, r.obj.name) for r in result.relations]

    orgs = [(0, 1, "ORG"), (2, 3, "ORG")]
    assert triples(["USA", "sanktionieren", "Rosneft"], ["USA", "sanktionieren", "Rosneft"], orgs) == [("Rosneft", "SANCTIONED_BY", "USA")]
    assert triples(["Siemens", "übernimmt", "Acme"], ["Siemens", "übernehmen", "Acme"], orgs) == [("Siemens", "ACQUIRED", "Acme")]


def test_workflow_is_profile_bounded_opt_in_serialized_and_no_payload_shell():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/daily_ingestion.yml").read_text()
    data = yaml.safe_load(text)
    # YAML 1.1 treats 'on' as bool; GitHub uses YAML 1.2.
    triggers = data.get("on", data.get(True))
    # Two conservative cron runs twelve hours apart plus a public-profile-only
    # slot list (off the top of the hour) that needs a verified public repo.
    public_cron = "30 0,6,9,12,18,21 * * *"
    assert [entry["cron"] for entry in triggers["schedule"]] == ["0 3 * * *", "0 15 * * *", public_cron]
    job = data["jobs"]["publish"]
    profile = data["jobs"]["profile"]
    public = "needs.profile.outputs.public == 'true'"
    assert job["needs"] == "profile"
    assert "!cancelled()" in job["if"] and f"github.event.schedule != '{public_cron}' || {public}" in job["if"]
    assert "vars.PIPELINE_PROFILE == 'public'" in profile["if"] and "refs/heads/main" in profile["if"]
    detect = profile["steps"][0]["run"]
    assert "gh api" in detect and '[ "$private" = "false" ]' in detect and "public=false" in detect
    assert "github.event.repository.private" not in job["if"] + profile["if"]
    # Conservative values are the fallback of every profile expression.
    for key, (public_value, default) in {
            "timeout-minutes": (20, 5)}.items():
        assert job[key] == f"${{{{ {public} && {public_value} || {default} }}}}"
    for key, (public_value, default) in {
            "DAILY_SCHEDULED_SLOTS": ("8", "2"), "DAILY_MANUAL_SLOTS": ("3", "1"),
            "MAX_DOCUMENTS_TOTAL": ("30", "10"), "MAX_RUNTIME_SECONDS": ("600", "90"),
            "INGEST_TIMEOUT_SECONDS": ("660", "105"), "EXPORT_TIMEOUT_SECONDS": ("120", "45"),
            "GEMINI_RUNS_PER_DAY": ("8", "2")}.items():
        assert job["env"][key] == f"${{{{ {public} && '{public_value}' || '{default}' }}}}", key
    assert 'timeout "${INGEST_TIMEOUT_SECONDS}s" python' in text
    assert '--limit "$MAX_DOCUMENTS_TOTAL"' in text
    assert 'timeout "${EXPORT_TIMEOUT_SECONDS}s" python' in text
    # Gemini broker is opt-in and its ledger is persisted even after failures.
    assert job["env"]["ENABLE_GEMINI_BROKER"] == "${{ vars.ENABLE_GEMINI_BROKER || 'false' }}"
    assert job["env"]["GEMINI_MODE"] == "${{ vars.GEMINI_MODE || 'pair' }}"
    assert job["env"]["GEMINI_AUTO_RELEASE"] == "${{ vars.GEMINI_AUTO_RELEASE || 'false' }}"
    assert job["env"]["GEMINI_BROKER_LEDGER"] == ".pipeline-state/gemini-broker.json"
    persist = next(step for step in job["steps"] if step.get("name", "").startswith("Persist cloud"))
    assert persist["if"].startswith("always()") and ".pipeline-state/gemini-broker.json" in persist["run"]
    assert "ENABLE_DAILY_STATIC" in job["if"]
    assert data["concurrency"]["cancel-in-progress"] is False
    assert all("client_payload" not in step.get("run", "") for step in job["steps"])
    assert "client_payload.query" in job["env"]["INGEST_QUERY"]
    assert 'git push origin "HEAD:$PUBLISH_BRANCH"' in text
    assert job["env"]["PUBLISH_BRANCH"] == "main"
    assert text.index("--reserve-run") < text.index("pip install")
    assert 'git add -- apps/web/public/data/' in text


def test_slug_matches_existing_frontend_contract():
    from src.export_static_slices import entity_slug
    assert entity_slug("loc:hamburg", "Hamburg") == "hamburg-00yfzd7"
    assert entity_slug("id:😀", "Straße München").startswith("strasse-munchen-")


def test_cloud_unknown_model_never_generates(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-unknown-thinking-model")
    session = Session()
    fallback = cloud(tmp_path, monkeypatch, session)
    assert fallback.classify("x", "a", "b") is None
    assert len(session.calls) == 1


def test_german_backend_overrides_registry_english_feeds(settings):
    from puppetnet.models import IngestStats, SourceSpec, SourceType
    from puppetnet.sources.base import AdapterContext
    from puppetnet.sources.rss import RssAdapter
    settings.nlp_backend = "token_saver"
    settings.rss_feeds = ["https://example.org/de/feed", "https://example.org/de/feed"]
    spec = SourceSpec(id="news_world", name="news", kind=SourceType.UNSTRUCTURED,
                      adapter="rss", options={"feeds": ["https://example.org/en/feed"]})
    adapter = RssAdapter(spec, AdapterContext(settings=settings, client=None, stats=IngestStats()))
    assert list(adapter._feed_urls()) == ["https://example.org/de/feed"]


def test_cloud_usage_logged_without_secret_or_source_text(tmp_path, monkeypatch, caplog):
    import logging
    fallback = cloud(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO):
        assert fallback.classify("Sensitive sentence", "Alice", "Acme") == "FOUNDED"
    assert fallback.reported_tokens == 82
    assert fallback.unknown_usage_calls == 0
    assert "reported_tokens=82" in caplog.text
    assert "test-not-a-real-key" not in caplog.text
    assert "Sensitive sentence" not in caplog.text


def test_export_accepts_neo4j_user_secret(monkeypatch):
    from src.export_static_slices import read_neo4j
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.test")
    monkeypatch.setenv("NEO4J_USER", "new-user")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-password")

    def connect(uri, *, auth, **kwargs):
        assert auth == ("new-user", "test-password")
        raise RuntimeError("offline connection stub reached")

    monkeypatch.setattr("neo4j.GraphDatabase.driver", connect)
    with pytest.raises(RuntimeError, match="offline connection stub reached"):
        read_neo4j()


def test_cleaning_removes_marketing_filler_not_negation():
    assert clean_html("<p>Jetzt kostenlos abonnieren!</p><p>Anna besitzt Acme nicht.</p>") == "Anna besitzt Acme nicht."


@pytest.mark.parametrize("confidence,released", [(.75, True), (.7499, False), (1, True),
                                                    (float("nan"), False), (float("inf"), False),
                                                    (True, False), ("bad", False), (2, False), (-1, False)])
def test_auto_release_confidence_boundary(confidence, released):
    raw = graph()
    row = raw["nodes"][0]
    row.pop("public")
    row["confidence"] = confidence
    output = build_public_graph(raw)
    node = next((node for node in output["nodes"] if node["name"] == "Anna"), None)
    assert bool(node) is released
    if node:
        assert node["public_released"] is True


def test_verified_feed_is_exact_operator_allowlist_not_boolean(monkeypatch):
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://trusted.example/rss")
    raw = graph()
    raw["nodes"][0].update(public=False, confidence=.1, feed_urls=["https://trusted.example/rss"])
    raw["edges"][0].update(public=False, confidence=.1, requires_review=True,
                            feed_urls=["https://trusted.example/rss"])
    output = build_public_graph(raw)
    assert len(output["nodes"]) == 2
    assert len(output["edges"]) == 1
    assert output["edges"][0]["public_released"] is True
    raw["nodes"][0].update(feed_urls=["https://trusted.example/rss.evil"], verified_rss=True)
    assert len(build_public_graph(raw)["nodes"]) == 1


@pytest.mark.parametrize("flag", ["private", "noindex", "release_blocked", "negated", "hedged", "calculated"])
def test_auto_release_never_overrides_vetoes(flag, monkeypatch):
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://trusted.example/rss")
    raw = graph()
    raw["nodes"][0].update(confidence=.99, feed_url="https://trusted.example/rss", **{flag: True})
    assert len(build_public_graph(raw)["nodes"]) == 1
    raw = graph()
    raw["edges"][0].update(confidence=.99, feed_url="https://trusted.example/rss", **{flag: True})
    assert build_public_graph(raw)["edges"] == []


def test_auto_release_emits_valid_complete_slices_and_withdrawal(tmp_path):
    raw = graph()
    for row in [*raw["nodes"][:2], raw["edges"][0]]:
        row.update(public=False, confidence=.75, requires_review=True)
    output = export_slices(raw, tmp_path)
    assert len(output["nodes"]) == 2 and len(output["edges"]) == 1
    assert all(row["public_released"] for row in [*output["nodes"], *output["edges"]])
    files = list((tmp_path / "entities").glob("*.json"))
    assert len(files) == 2
    assert all(json.loads(file.read_text())["entity"]["public_released"] for file in files)
    raw["nodes"][0]["public_released"] = False
    output = export_slices(raw, tmp_path)
    assert len(output["nodes"]) == 1
    assert len(list((tmp_path / "entities").glob("*.json"))) == 1
    assert output["edges"] == []


def test_rss_provenance_survives_document_storage():
    from puppetnet.models import Document
    doc = Document(doc_id="d", source_id="news_world", url="https://article.example/a", text="text",
                   extra={"feed_url": "https://trusted.example/rss"})
    assert doc.to_node_properties()["feed_url"] == "https://trusted.example/rss"


def test_workflow_main_uses_pat_no_branch_dependency_and_only_expected_staging():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/daily_ingestion.yml").read_text()
    data = yaml.safe_load(text)
    job = data["jobs"]["publish"]
    checkout = next(step for step in job["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["token"] == "${{ secrets.GH_PAT }}"
    assert checkout["with"]["ref"] == "main"
    assert job["env"]["NEO4J_USER"] == "${{ secrets.NEO4J_USER }}"
    assert job["env"]["VERIFIED_RSS_FEEDS"] == "${{ vars.VERIFIED_RSS_FEEDS }}"
    assert "arena/" not in text
    assert "refs/heads/main" in job["if"]
    assert "--force" not in text
    assert "git add ." not in text


def test_all_withdrawals_publish_empty_snapshot_when_explicitly_allowed(tmp_path):
    export_slices(graph(), tmp_path)
    raw = graph()
    for row in raw["nodes"]:
        row["public_released"] = False
    output = export_slices(raw, tmp_path, allow_empty=True)
    assert output["nodes"] == [] and output["edges"] == []
    assert list((tmp_path / "entities").glob("*.json")) == []


def test_neo4j_release_uses_stored_document_provenance(monkeypatch):
    from src.export_static_slices import read_neo4j
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.test")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-password")
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://trusted.example/rss")

    class Transaction:
        def run(self, query, **params):
            if "AS node" in query:
                assert "WHERE e.public = true" not in query
                return [{"node": {"canonical_key": "a", "name": "Anna", "entity_type": "Person", "confidence": .2, "doc_ids": ["d"]}},
                        {"node": {"canonical_key": "b", "name": "Acme", "entity_type": "Organization", "confidence": .75}}]
            if "AS edge" in query:
                return [{"edge": {"source": "a", "target": "b", "type": "FOUNDED", "confidence": .2,
                                  "requires_review": True, "doc_ids": ["d"]}}]
            assert params["ids"] == ["d"]
            return [{"doc": {"id": "d", "title": "Register", "feed_url": "https://trusted.example/rss"}}]

    class Driver:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def session(self, **kwargs):
            return self

        def execute_read(self, function):
            return function(Transaction())

    monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *a, **kw: Driver())
    output = build_public_graph(read_neo4j())
    assert len(output["nodes"]) == 2 and len(output["edges"]) == 1
    assert output["nodes"][0]["public_released"] is True
    assert output["evidence"][0]["id"] == "d"


def test_misc_company_spans_are_promoted_and_sentence_dots_dropped():
    """Live probe 2026-10-21: de_core_news_sm returned 'Acme GmbH.' as MISC."""
    import spacy
    from spacy.tokens import Doc, Span

    from puppetnet.models import Document, EntityType
    words = ["Olaf", "Scholz", "besucht", "Acme", "GmbH.", "und", "die", "Bundesliga", "."]
    doc = Doc(spacy.blank("de").vocab, words=words, sent_starts=[True] + [False] * (len(words) - 1))
    doc.ents = [Span(doc, 0, 2, label="PER"), Span(doc, 3, 5, label="MISC"), Span(doc, 7, 8, label="MISC")]

    class LocalNLP:
        def has_pipe(self, name):
            return True

        def __call__(self, text):
            return doc

    result = TokenSaverEngine(nlp=LocalNLP()).parse_document(
        Document(doc_id="d1", source_id="s", url="", text=" ".join(words)))
    found = {(e.name, e.entity_type) for e in result.entities}
    assert found == {("Olaf Scholz", EntityType.PERSON), ("Acme GmbH", EntityType.ORGANIZATION)}


def test_entity_name_cleanup_keeps_abbreviation_dots():
    from src.token_saver_nlp import clean_entity_name, entity_kind

    from puppetnet.models import EntityType
    assert clean_entity_name("Atlantis Shipping Ltd.") == "Atlantis Shipping Ltd."
    assert clean_entity_name("Kunstverein e.V.") == "Kunstverein e.V."
    assert clean_entity_name("Berlin,") == "Berlin"
    assert entity_kind("MISC", "Wagner-Gruppe") is EntityType.ORGANIZATION
    assert entity_kind("MISC", "iPhone") is None


def _isolated_person_graph(**person):
    node = {"id": "person:solo", "name": "Erika Solo", "type": "Person", "confidence": .9,
            "feed_url": "https://news.example/rss"}
    node.update(person)
    return {"nodes": [
        node,
        {"id": "org:solo", "name": "Allein GmbH", "type": "Organization", "confidence": .9},
        {"id": "loc:solo", "name": "Kiel", "type": "Location", "confidence": .9},
    ], "edges": []}


def test_isolated_persons_need_an_operator_release(monkeypatch):
    # Operator decision 2026-10-22: high confidence or a verified feed may publish
    # a connected person, but a person with no published relationship gets a
    # public profile page only after an explicit public_released=true.
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://news.example/rss")
    kept = {node["id"] for node in build_public_graph(_isolated_person_graph())["nodes"]}
    assert kept == {"org:solo", "loc:solo"}, "organizations and places keep the normal policy"
    released = build_public_graph(_isolated_person_graph(public_released=True))["nodes"]
    assert "person:solo" in {node["id"] for node in released}
    withdrawn = build_public_graph(_isolated_person_graph(public_released=False))["nodes"]
    assert "person:solo" not in {node["id"] for node in withdrawn}


def test_a_person_counts_as_connected_only_through_a_published_edge(monkeypatch):
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://news.example/rss")
    raw = _isolated_person_graph()
    raw["edges"] = [{"source": "person:solo", "target": "org:solo", "type": "OFFICER_OF", "confidence": .9}]
    assert "person:solo" in {node["id"] for node in build_public_graph(raw)["nodes"]}
    # The only relationship is unreleased (cloud model, no operator release):
    # it is not published, so the person is isolated in the public graph.
    raw["edges"][0]["method"] = "llm"
    monkeypatch.delenv("GEMINI_AUTO_RELEASE", raising=False)
    graph = build_public_graph(raw)
    assert graph["edges"] == []
    assert "person:solo" not in {node["id"] for node in graph["nodes"]}
    # The other endpoint being withheld also leaves the person isolated.
    raw["edges"][0].pop("method")
    raw["nodes"][1]["private"] = True
    assert "person:solo" not in {node["id"] for node in build_public_graph(raw)["nodes"]}


def test_export_never_leaves_a_dangling_edge_after_dropping_isolated_persons(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFIED_RSS_FEEDS", "https://news.example/rss")
    raw = _isolated_person_graph()
    raw["nodes"].append({"id": "person:linked", "name": "Max Linked", "type": "Person", "confidence": .9})
    raw["edges"] = [{"source": "person:linked", "target": "loc:solo", "type": "LOCATED_IN", "confidence": .9}]
    data = export_slices(raw, tmp_path)
    ids = {node["id"] for node in data["nodes"]}
    assert ids == {"person:linked", "org:solo", "loc:solo"}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in data["edges"])
