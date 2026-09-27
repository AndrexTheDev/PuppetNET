"""End-to-end dry run: real registry, real adapters, real NLP, scripted HTTP.

This is the closest thing to a production rehearsal the suite can offer without
network access. The pipeline, source registry, YAML overlay, adapters, NLP
engine, weighting, resolver and graph writer are all the real implementations;
only two things are substituted:

* ``FetchClient.request`` — answered from a routing table (no sockets);
* Neo4j — the real client in dry-run mode, so every statement is recorded, and
  ``execute_batches`` is spied on to capture the exact rows that would be
  written.

The assertions therefore check the contract the specification asks for:
structured sources produce edges at their declared weight — 1.0 for the official
registers and leaks, 0.9 for the second-hand aggregators Wikidata and
OpenCorporates, 0.8 for aviation telemetry — with no method or evidence penalty;
news produces co-occurrence-priced edges (0.4 source weight × the 0.2 penalty);
CRAFT identifiers survive all the way into a ``:Craft`` upsert row; and a replay
of the same day writes nothing new.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from puppetnet import pipeline as pipeline_module
from puppetnet.graph.neo4j_client import Neo4jClient
from puppetnet.models import RelationType
from puppetnet.net.proxy_client import FetchClient, FetchResult
from puppetnet.pipeline import IngestPipeline, PipelineOptions
from puppetnet.sources.registry import SOURCE_REGISTRY

# --------------------------------------------------------------------------- #
# Canned upstream payloads
# --------------------------------------------------------------------------- #

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
OC_COMPANIES = "https://api.opencorporates.com/v0.4/companies/search"
OC_OFFICERS = "https://api.opencorporates.com/v0.4/officers/search"
FEED_URL = "https://feeds.example.test/world.xml"
ARTICLE_URL = "https://example.test/story-1"

SPARQL_RESPONSE = {
    "head": {"vars": ["company", "companyLabel", "owner", "ownerLabel"]},
    "results": {
        "bindings": [
            {
                "company": {"type": "uri", "value": "http://www.wikidata.org/entity/Q217798"},
                "companyLabel": {"type": "literal", "value": "Nord Stream AG"},
                "owner": {"type": "uri", "value": "http://www.wikidata.org/entity/Q9357320"},
                "ownerLabel": {"type": "literal", "value": "Gazprom"},
            },
            {
                "company": {"type": "uri", "value": "http://www.wikidata.org/entity/Q9357320"},
                "companyLabel": {"type": "literal", "value": "Gazprom"},
                "owner": {"type": "uri", "value": "http://www.wikidata.org/entity/Q5"},
                "ownerLabel": {"type": "literal", "value": "Igor Sechin"},
            },
        ]
    },
}

OC_RESPONSE = {
    "api_version": "0.4",
    "results": {
        "companies": [
            {
                "company": {
                    "name": "Nord Stream AG",
                    "company_number": "CHE-113.243.076",
                    "jurisdiction_code": "ch",
                    "opencorporates_url": "https://opencorporates.com/companies/ch/CHE-113.243.076",
                    "incorporation_date": "2006-04-03",
                    "current_status": "Active",
                    "registered_address_in_full": "Bahnhofstrasse 1, Zug, Switzerland",
                    "parent_company": {"name": "Gazprom", "jurisdiction_code": "ru"},
                    "retrieved_at": "2026-09-20T00:00:00Z",
                }
            }
        ]
    },
}

FEED_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>World news</title>
<item>
  <guid>tag:example.test,2026:story-1</guid>
  <link>https://example.test/story-1</link>
  <title>Sanctioned superyacht Amadea detained in Fiji</title>
  <description>Authorities in Fiji detained the vessel.</description>
  <pubDate>Sat, 26 Sep 2026 06:30:00 GMT</pubDate>
</item>
</channel></rss>"""

ARTICLE_RESPONSE = """<html><head><title>Sanctioned superyacht Amadea detained in Fiji</title></head>
<body><nav>Home World Business</nav><article>
<p>The superyacht Amadea was detained in Fiji on Saturday after a joint
investigation by United States and Fijian authorities. Prosecutors allege that
Igor Sechin is the beneficial owner of the vessel, which is registered in Malta
under the flag prefix 9H and carries IMO number 9876543.</p>
<p>The charter aircraft 9H-VUC landed in Malta two days before the seizure,
according to flight tracking data. Gazprom denied any connection to the vessel.
Fiji police confirmed the yacht remains anchored off Suva pending a court
hearing next month.</p>
</article><footer>Advertisement</footer></body></html>"""

SOURCES_OVERLAY = """
sources:
  - id: wikidata
    options:
      queries: ["ownership"]
      limit_per_query: 25
      maxlag: 0
  - id: opencorporates
    options:
      queries: ["Nord Stream"]
      jurisdictions: ["ch"]
      include_officers: false
      include_groupings: false
      allow_anonymous: true
  - id: news_world
    options:
      fetch_full_articles: true
"""


# --------------------------------------------------------------------------- #
# Scripted transport
# --------------------------------------------------------------------------- #


def scripted_client(routes: dict[str, Any]) -> type[FetchClient]:
    """Build a FetchClient subclass whose transport is a routing table."""

    class ScriptedFetchClient(FetchClient):
        def __init__(self, settings: Any, **kwargs: Any) -> None:
            super().__init__(settings, **kwargs)
            self.requested: list[tuple[str, dict[str, Any]]] = []

        def request(self, url: str, **kwargs: Any) -> FetchResult:  # type: ignore[override]
            self.requested.append((url, kwargs))
            self.stats.http_requests += 1
            for pattern, response in routes.items():
                if url == pattern or url.startswith(pattern):
                    if isinstance(response, FetchResult):
                        return dataclasses.replace(response, url=url)
                    if callable(response):
                        return response(url, kwargs)
                    payload = json.dumps(response)
                    return FetchResult(
                        url=url,
                        status=200,
                        ok=True,
                        content_type="application/json",
                        text=payload,
                        content=payload.encode("utf-8"),
                    )
            return FetchResult(url=url, status=404, ok=False, error=f"no route scripted for {url}")

    return ScriptedFetchClient


def text_result(url: str, text: str, content_type: str) -> FetchResult:
    return FetchResult(
        url=url, status=200, ok=True, content_type=content_type, text=text, content=text.encode("utf-8")
    )


HEALTHY_ROUTES: dict[str, Any] = {
    SPARQL_ENDPOINT: SPARQL_RESPONSE,
    OC_COMPANIES: OC_RESPONSE,
    OC_OFFICERS: {"api_version": "0.4", "results": {"officers": []}},
    FEED_URL: text_result(FEED_URL, FEED_RESPONSE, "application/rss+xml"),
    ARTICLE_URL: text_result(ARTICLE_URL, ARTICLE_RESPONSE, "text/html"),
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def env(settings, tmp_path, monkeypatch):
    overlay = tmp_path / "sources.yaml"
    overlay.write_text(SOURCES_OVERLAY, encoding="utf-8")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return dataclasses.replace(
        settings,
        dry_run=True,
        state_dir=str(tmp_path / "state"),
        report_dir=str(tmp_path / "reports"),
        sources_file=str(overlay),
        run_id="run-e2e-0001",
        rss_feeds=[FEED_URL],
        spacy_models=[],
        nlp_enabled=True,
        only_new_documents=True,
        max_documents_per_source=10,
        max_documents_total=50,
        max_runtime_seconds=600,
        dedupe_window_days=30,
    )


@pytest.fixture()
def captured_batches(monkeypatch):
    """Capture every row batch the writer would have sent to Neo4j."""
    captured: list[dict[str, Any]] = []
    real = Neo4jClient.execute_batches

    def spy(self, query, rows, **kwargs):
        captured.append({"query": query, "rows": list(rows), "label": kwargs.get("label", "")})
        return real(self, query, rows, **kwargs)

    monkeypatch.setattr(Neo4jClient, "execute_batches", spy)
    return captured


def run_e2e(env, monkeypatch, routes: dict[str, Any] | None = None, **options: Any) -> tuple[IngestPipeline, Any]:
    monkeypatch.setattr(pipeline_module, "FetchClient", scripted_client(routes if routes is not None else HEALTHY_ROUTES))
    pipeline = IngestPipeline(
        settings=dataclasses.replace(env),
        options=PipelineOptions(sources=["wikidata", "opencorporates", "news_world"], limit_per_source=5, **options),
    )
    pipeline.run()
    return pipeline, pipeline.fetch


def rows_for(captured: list[dict[str, Any]], fragment: str) -> list[dict[str, Any]]:
    """Collect the rows of every batch that writes ``fragment``.

    ``fragment`` is a label (``documents``, ``relations:OWNS``) or a label clause
    from the statement (``:Organization``). Comments are stripped before the
    match, because the query is otherwise prose as well as code: a Cypher comment
    that happens to contain the word "documents" — one explaining why repeated
    observations must not inflate confidence, say — made every relation batch count
    as a document batch, and ``assert len(documents) == 3`` read 13. A test
    selector must match the statement, not the commentary around it.
    """
    def without_comments(query: str) -> str:
        return "\n".join(line.split("//", 1)[0] for line in str(query).splitlines())

    rows: list[dict[str, Any]] = []
    for batch in captured:
        query = without_comments(batch["query"])
        if fragment in query or fragment == batch["label"]:
            rows.extend(batch["rows"])
    return rows


# --------------------------------------------------------------------------- #
# The rehearsal
# --------------------------------------------------------------------------- #


def test_dry_run_harvests_all_three_source_families(env, monkeypatch, captured_batches):
    pipeline, fetch = run_e2e(env, monkeypatch)
    stats = pipeline.stats

    assert stats.errors == [], stats.errors
    assert stats.documents_fetched == 3
    assert stats.documents_failed == 0
    assert [report["source_id"] for report in pipeline._source_reports] == [
        "opencorporates",
        "wikidata",
        "news_world",
    ], "structured sources are harvested first"

    # Every scripted endpoint was hit through the real politeness layer.
    assert fetch.requested, "no HTTP was attempted"
    assert stats.http_requests == len(fetch.requested)
    assert stats.http_rate_limited == 0


def test_dry_run_extracts_entities_relations_and_craft(env, monkeypatch, captured_batches):
    pipeline, _ = run_e2e(env, monkeypatch)
    stats = pipeline.stats

    assert stats.entities_extracted > 0
    assert stats.relations_extracted > 0
    assert stats.entities_written > 0
    assert stats.relations_written > 0
    assert stats.craft_entities >= 1, "the tail number and IMO must be detected"
    assert stats.dependency_triples == 0, "no statistical model in this environment"
    assert stats.cooccurrence_triples >= 1, "news triples are co-occurrence priced"
    assert stats.sentences_processed >= 3


def test_dry_run_writes_typed_entity_rows(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)

    craft_rows = rows_for(captured_batches, ":Craft")
    names = {row["name"] for row in craft_rows}
    assert "9H-VUC" in names, names
    vuc = next(row for row in craft_rows if row["name"] == "9H-VUC")
    assert vuc["properties"].get("registry_country") == "Malta"
    assert vuc["entity_type"] == "Craft"

    organisation_rows = rows_for(captured_batches, ":Organization")
    assert any(row["name"] == "Gazprom" for row in organisation_rows)
    person_rows = rows_for(captured_batches, ":Person")
    assert any(row["name"] == "Igor Sechin" for row in person_rows)
    location_rows = rows_for(captured_batches, ":Location")
    assert location_rows, "jurisdictions and addresses become Location nodes"


def test_structured_edges_keep_full_weight_and_news_edges_are_penalised(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)

    structured = rows_for(captured_batches, "relations:OWNED_BY") + rows_for(captured_batches, "relations:REGISTERED_IN")
    assert structured, "the structured adapters produced no edges"
    declared = {spec.id: spec.confidence for spec in SOURCE_REGISTRY}
    for row in structured:
        assert row["method"] == "structured"
        # Wikidata and OpenCorporates are second-hand, so they are declared at
        # 0.9 rather than the 1.0 an official register gets.
        assert row["source_weight"] == pytest.approx(declared[row["source_id"]]), row
        # A structured edge keeps the whole declared weight: method factor 1.0,
        # evidence score 1.0.
        assert row["confidence"] == pytest.approx(row["source_weight"]), row

    news = [
        row
        for batch in captured_batches
        if batch["label"].startswith("relations:")
        for row in batch["rows"]
        if row["method"] == "cooccurrence"
    ]
    assert news, "the news article produced no co-occurrence edges"
    for row in news:
        assert row["source_weight"] == pytest.approx(0.4)
        # 0.4 source weight × (1 − 0.2 co-occurrence penalty) is the ceiling.
        assert row["confidence"] <= 0.4 * 0.8 + 1e-9, row


def test_every_edge_row_names_the_document_it_came_from(env, monkeypatch, captured_batches):
    """The evidence ledger is only a ledger if every edge names its document.

    `relation_evidence_is_new` cannot check an empty `doc_id` — there is nothing to
    compare against the edge's `doc_ids` — so an adapter that forgets to stamp its
    relations would let that source's confidence climb on every re-read, which is
    exactly the inflation the ledger exists to stop. Stamping happens in
    `SourceAdapter.document()`, so this check is what keeps every adapter using it
    (or stamping by hand).
    """
    run_e2e(env, monkeypatch)
    rows = [
        row
        for batch in captured_batches
        if str(batch.get("label", "")).startswith("relations:")
        for row in batch.get("rows", [])
    ]
    assert rows, "the fixture harvests structured and news edges — the check needs rows"
    missing = sorted({
        f"{row.get('rel_type')}:{row.get('subject_key')}->{row.get('object_key')}"
        for row in rows
        if not str(row.get("doc_id") or "").strip()
    })
    assert not missing, f"edges written without a document id: {missing[:5]}"


def test_dry_run_records_the_statements_it_would_execute(env, monkeypatch, captured_batches):
    pipeline, _ = run_e2e(env, monkeypatch)
    summary = pipeline.neo4j.recorder.summary()

    assert summary["by_kind"]["schema"] > 0
    assert summary["by_kind"]["run"] == 3
    assert summary["by_kind"]["write"] > 0
    assert summary["rows"] > 0
    assert pipeline.neo4j.describe()["dry_run"] is True


def test_dry_run_writes_a_complete_report(env, monkeypatch, captured_batches, tmp_path):
    pipeline, _ = run_e2e(env, monkeypatch)
    report = json.loads((Path(env.report_dir) / "run-e2e-0001.json").read_text(encoding="utf-8"))

    assert report["run_id"] == "run-e2e-0001"
    assert report["graph"]["status"] == "completed"
    assert report["graph"]["summary"]["entities"] == pipeline.stats.entities_written
    assert {source["source_id"] for source in report["sources"]} == {"wikidata", "opencorporates", "news_world"}
    assert report["nlp"]["backend"].startswith("spacy:blank")
    assert report["nlp"]["has_parser"] is False
    assert report["relations_by_type"], "the report must aggregate predicates"
    assert RelationType.OWNED_BY.value in report["relations_by_type"]
    assert report["stats"]["craft_entities"] >= 1
    assert (Path(env.report_dir) / "run-e2e-0001.md").exists()


def test_replaying_the_same_day_writes_nothing_new(env, monkeypatch, captured_batches):
    first, _ = run_e2e(env, monkeypatch)
    assert first.stats.documents_fetched == 3

    captured_batches.clear()
    second, _ = run_e2e(env, monkeypatch)

    assert second.stats.documents_fetched == 0
    assert second.stats.documents_skipped_duplicate == 3
    assert second.stats.errors == []
    assert rows_for(captured_batches, ":Entity") == []


def test_a_total_network_failure_still_completes_the_run(env, monkeypatch, captured_batches):
    def refuse(url: str, kwargs: dict) -> FetchResult:
        return FetchResult(url=url, status=503, ok=False, error="upstream unavailable")

    routes = {pattern: refuse for pattern in HEALTHY_ROUTES}
    pipeline, _ = run_e2e(env, monkeypatch, routes=routes)
    stats = pipeline.stats

    assert stats.documents_fetched == 0
    assert stats.errors, "every source must report its failure"
    assert {report["source_id"] for report in pipeline._source_reports} == {"wikidata", "opencorporates", "news_world"}
    assert all(report["documents"] == 0 for report in pipeline._source_reports)

    report = json.loads((Path(env.report_dir) / "run-e2e-0001.json").read_text(encoding="utf-8"))
    assert report["graph"]["status"] == "completed", "a bad harvest is not a crashed run"
    assert report["graph"]["summary"]["entities"] == 0


def test_a_partially_failing_harvest_keeps_the_healthy_sources(env, monkeypatch, captured_batches):
    routes = dict(HEALTHY_ROUTES)
    routes[FEED_URL] = FetchResult(url=FEED_URL, status=429, ok=False, error="rate-limited")
    pipeline, _ = run_e2e(env, monkeypatch, routes=routes)
    stats = pipeline.stats

    assert stats.documents_fetched == 2
    reports = {report["source_id"]: report for report in pipeline._source_reports}
    assert reports["news_world"]["documents"] == 0
    assert reports["news_world"]["error"] or stats.per_source["news_world"].get("errors")
    assert reports["wikidata"]["documents"] == 1
    assert reports["opencorporates"]["documents"] == 1


def test_unknown_routed_response_is_reported_not_fatal(env, monkeypatch, captured_batches):
    """An endpoint nobody scripted must degrade into a recorded error."""
    routes = dict(HEALTHY_ROUTES)
    del routes[SPARQL_ENDPOINT]
    pipeline, _ = run_e2e(env, monkeypatch, routes=routes)

    assert pipeline.stats.documents_fetched == 2
    assert any("wikidata" in error for error in pipeline.stats.errors)


def test_the_yaml_overlay_reached_the_adapters(env, monkeypatch, captured_batches):
    pipeline, fetch = run_e2e(env, monkeypatch)
    urls = [url for url, _ in fetch.requested]

    # wikidata: one query, not the six in the code registry.
    assert sum(1 for url in urls if url.startswith(SPARQL_ENDPOINT)) == 1
    # news_world: the environment feed, not the five defaults.
    assert sum(1 for url in urls if url.startswith(FEED_URL)) == 1
    assert not any("theguardian.com" in url for url in urls)
    # opencorporates: officers/groupings disabled by the overlay.
    assert not any(url.startswith(OC_OFFICERS) for url in urls)
    assert pipeline._resolve_specs.__name__ == "_resolve_specs"


def test_sparql_is_posted_as_a_form_with_limit_maxlag_and_user_agent(env, monkeypatch, captured_batches):
    _, fetch = run_e2e(env, monkeypatch)
    kwargs = next(k for url, k in fetch.requested if url.startswith(SPARQL_ENDPOINT))

    # POST, not GET: these queries run to kilobytes and a GET URL is truncated
    # long before WDQS's own limits.
    assert kwargs["method"] == "POST"
    assert kwargs["params"] is None
    assert "LIMIT 25" in kwargs["data"]["query"]
    assert kwargs["data"]["format"] == "json"
    # This overlay sets maxlag: 0, which drops the parameter; the default run
    # sends maxlag=5 (asserted in tests/test_sources.py).
    assert "maxlag" not in kwargs["data"]
    assert kwargs["headers"]["User-Agent"]
    assert kwargs["headers"]["Accept"].startswith("application/sparql-results+json")
    assert kwargs["source_id"] == "wikidata"
    assert kwargs["respect_robots"] is False, "Wikidata publishes a UA policy instead"


def test_politeness_metadata_is_forwarded_per_source(env, monkeypatch, captured_batches):
    _, fetch = run_e2e(env, monkeypatch)
    by_source = {kwargs["source_id"]: kwargs for _, kwargs in fetch.requested}

    assert by_source["wikidata"]["source"].kind.value == "structured"
    assert by_source["news_world"]["source"].kind.value == "unstructured"
    assert by_source["opencorporates"]["cache_ttl_seconds"] == 43200
    assert by_source["news_world"]["cache_ttl_seconds"] == 1800


def test_news_article_provenance_survives_to_the_document_node(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)
    documents = rows_for(captured_batches, "documents")

    assert len(documents) == 3
    story = next(row for row in documents if row["url"] == ARTICLE_URL)
    assert story["source_id"] == "news_world"
    assert story["source_weight"] == pytest.approx(0.4)
    assert story["content_type"] == "text/html"
    assert story["word_count"] > 40
    assert story["content_hash"]
    assert story["run_id"] == "run-e2e-0001"

    wikidata_row = next(row for row in documents if row["source_id"] == "wikidata")
    assert wikidata_row["source_weight"] == pytest.approx(0.9)


def test_entity_rows_carry_provenance_and_aliases(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)
    organisations = rows_for(captured_batches, ":Organization")
    gazprom = next(row for row in organisations if row["name"] == "Gazprom")

    assert gazprom["canonical_key"].startswith("ORGANIZATION:")
    assert gazprom["mention_count"] >= 1
    assert gazprom["source_ids"], "every node records which source asserted it"
    assert gazprom["doc_ids"]
    assert gazprom["aliases"]
    assert gazprom["confidence"] > 0
    assert gazprom["run_id"] == "run-e2e-0001"


def test_mention_rows_are_written_for_parsed_documents(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)
    mentions = rows_for(captured_batches, "mentions")
    assert mentions
    assert all(row["canonical_key"] and row["doc_id"] for row in mentions)
    assert any(row["count"] >= 1 for row in mentions)


def test_no_self_loops_or_duplicate_keys_reach_the_graph(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)

    # One statement must never carry the same key twice: MERGE inside a single
    # UNWIND would double-count mention_count. Across flushes a repeat is fine
    # (the same actor asserted by two sources converges idempotently).
    for batch in captured_batches:
        if "MERGE (e:Entity" not in batch["query"]:
            continue
        keys = [row["canonical_key"] for row in batch["rows"]]
        assert len(keys) == len(set(keys)), f"duplicate keys in one batch: {keys}"

    for batch in captured_batches:
        if "-[r:" not in batch["query"]:
            continue
        for row in batch["rows"]:
            assert row["subject_key"] != row["object_key"], row
            assert row["confidence"] >= 0.05


def test_relation_rows_keep_their_evidence(env, monkeypatch, captured_batches):
    run_e2e(env, monkeypatch)
    relation_rows = [row for batch in captured_batches if "-[r:" in batch["query"] for row in batch["rows"]]

    assert relation_rows
    for row in relation_rows:
        assert row["evidence"], "every edge must be traceable to text or a record"
        assert row["doc_id"] and row["source_id"]
        assert row["rel_type"] in RelationType.names()
