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
    # A legacy ledger with 2 manual entries exceeds the new manual bound and
    # fails closed instead of silently rebalancing.
    path.write_text(json.dumps({"day": "2026-10-19", "total": 2, "scheduled": 0, "manual": 2}))
    with pytest.raises(ValueError, match="Invalid"):
        reserve_run(path, "schedule", "2026-10-19")


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


def test_workflow_is_five_minutes_opt_in_serialized_and_no_payload_shell():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/daily_ingestion.yml").read_text()
    data = yaml.safe_load(text)
    # YAML 1.1 treats 'on' as bool; GitHub uses YAML 1.2.
    triggers = data.get("on", data.get(True))
    # Two bounded cron runs per UTC day, twelve hours apart (since 2026-10-20).
    assert [entry["cron"] for entry in triggers["schedule"]] == ["0 3 * * *", "0 15 * * *"]
    job = data["jobs"]["publish"]
    assert job["timeout-minutes"] == 5
    assert job["env"]["MAX_RUNTIME_SECONDS"] == "90"
    assert "timeout 105s python" in text
    assert "timeout 45s python" in text
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
