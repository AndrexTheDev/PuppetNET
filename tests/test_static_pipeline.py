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
    raw["nodes"][0]["public"] = False
    export_slices(raw, tmp_path)
    assert len(list((tmp_path / "entities").glob("*.json"))) == 1
    assert json.loads((tmp_path / "graph_latest.json").read_text())["edges"] == []


def test_empty_refuses_to_erase_existing(tmp_path):
    export_slices(graph(), tmp_path)
    original = (tmp_path / "graph_latest.json").read_bytes()
    with pytest.raises(ValueError, match="No explicitly public"):
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
    assert reserve_run(path, "schedule", "2026-10-19")
    assert not reserve_run(path, "schedule", "2026-10-19")
    assert reserve_run(path, "repository_dispatch", "2026-10-19")
    assert not reserve_run(path, "repository_dispatch", "2026-10-19")
    assert reserve_run(path, "schedule", "2026-10-20")
    path.write_text("CORRUPT")
    with pytest.raises(ValueError):
        reserve_run(path, "schedule")


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
    session = Session(count=101)
    assert cloud(tmp_path, monkeypatch, session).classify("x", "a", "b") is None
    assert len(session.calls) == 1


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


def test_workflow_is_ten_minutes_opt_in_serialized_and_no_payload_shell():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/daily_ingestion.yml").read_text()
    data = yaml.safe_load(text)
    # YAML 1.1 treats 'on' as bool; GitHub uses YAML 1.2.
    triggers = data.get("on", data.get(True))
    assert triggers["schedule"][0]["cron"] == "0 3 * * *"
    job = data["jobs"]["publish"]
    assert job["timeout-minutes"] == 10
    assert job["env"]["MAX_RUNTIME_SECONDS"] == "300"
    assert "timeout 315s python" in text
    assert "timeout 60s python" in text
    assert "ENABLE_DAILY_STATIC" in job["if"]
    assert data["concurrency"]["cancel-in-progress"] is False
    assert all("client_payload" not in step.get("run", "") for step in job["steps"])
    assert "client_payload.query" in job["env"]["INGEST_QUERY"]
    assert 'git push origin "HEAD:$PUBLISH_BRANCH"' in text
    assert job["env"]["PUBLISH_BRANCH"] == "arena/5fb0fe0b-puppetnet"
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
