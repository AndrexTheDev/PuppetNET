"""Source adapter tests: the harvest framework, the registry and three adapters.

Everything runs against a scripted fake of :class:`FetchClient`, so no HTTP is
performed. The two invariants that matter most for the graph are asserted
everywhere:

* structured sources (ICIJ, OpenCorporates, Wikidata, official registers) carry
  an edge confidence weight of **1.0**;
* unstructured sources (news/blogs/RSS) carry **0.4**.
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import timedelta
from typing import Any, Iterator

import pytest

from puppetnet.models import (
    Document,
    Entity,
    EntityType,
    ExtractionMethod,
    IngestStats,
    RelationType,
    SourceSpec,
    SourceType,
    utcnow,
)
from puppetnet.net.proxy_client import FetchResult
from puppetnet.sources import ADAPTERS, AdapterContext, AdapterError, SourceAdapter, create_adapter
from puppetnet.sources.base import build_entity, build_relation
from puppetnet.sources.registry import (
    ALL_SOURCE_IDS,
    SOURCE_REGISTRY,
    describe_registry,
    get_spec,
    iter_specs,
    specs_for_adapter,
)
from puppetnet.sources.rss import RssAdapter, _strip_html
from puppetnet.sources.wikidata import WIKIDATA_QUERIES, WikidataAdapter, _looks_like_person, _qid
from puppetnet.sources.opencorporates import OpenCorporatesAdapter

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeFetchClient:
    """Scripted stand-in for :class:`puppetnet.net.proxy_client.FetchClient`."""

    def __init__(self, responses: dict[str, Any] | None = None, *, default: Any = None) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        scripted = self.responses.get(url, self.default)
        if isinstance(scripted, FetchResult):
            return scripted
        if isinstance(scripted, dict):
            return FetchResult(url=url, status=200, ok=True, **scripted)
        if scripted is None:
            return FetchResult(url=url, status=404, ok=False, error="not-scripted")
        raise AssertionError(f"unexpected scripted response for {url}: {scripted!r}")

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    def kwargs_for(self, url: str) -> dict[str, Any]:
        for called_url, kwargs in self.calls:
            if called_url == url:
                return kwargs
        raise AssertionError(f"{url} was never requested")


def ok_json(url: str, payload: Any) -> FetchResult:
    return FetchResult(
        url=url,
        status=200,
        ok=True,
        content_type="application/json",
        text=json.dumps(payload),
        content=json.dumps(payload).encode("utf-8"),
    )


def ok_text(url: str, text: str, content_type: str = "text/html") -> FetchResult:
    return FetchResult(
        url=url,
        status=200,
        ok=True,
        content_type=content_type,
        text=text,
        content=text.encode("utf-8"),
    )


class MinimalAdapter(SourceAdapter):
    """Concrete adapter used to exercise the shared framework."""

    adapter_name = "minimal"

    def harvest(self) -> Iterator[Document]:
        error = self.spec.options.get("error")
        if error is not None:
            raise error
        for index in range(int(self.spec.options.get("count", 3))):
            yield self.make_document(
                f"https://example.test/{index}",
                title=f"Record {index}",
                text=f"Document {index} about Gazprom and Rosneft, with enough words to be usable.",
                external_id=f"rec-{index}",
            )


def spec(
    source_id: str = "test_source",
    kind: SourceType = SourceType.STRUCTURED,
    adapter: str = "minimal",
    **options: Any,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("_", " ").title(),
        kind=kind,
        adapter=adapter,
        base_url="https://example.test",
        max_documents=int(options.pop("max_documents", 25)),
        respect_robots=bool(options.pop("respect_robots", True)),
        cache_ttl_seconds=int(options.pop("cache_ttl_seconds", 60)),
        options=options,
    )


def context(settings, client=None, *, source_spec=None, deadline: float | None = None, known_hashes=None) -> AdapterContext:
    return AdapterContext(
        settings=settings,
        client=client if client is not None else FakeFetchClient(),
        stats=IngestStats(),
        spec=source_spec,
        run_id="run-test",
        known_hashes=set(known_hashes or ()),
        deadline=time.monotonic() + 3600 if deadline is None else deadline,
    )


def adapter_for(settings, source_spec=None, client=None, adapter_class=MinimalAdapter, **options):
    source_spec = source_spec or spec(**options)
    ctx = context(settings, client, source_spec=source_spec)
    return adapter_class(source_spec, ctx), ctx


@pytest.fixture()
def env(settings, tmp_path):
    return dataclasses.replace(
        settings,
        dry_run=True,
        state_dir=str(tmp_path / "state"),
        report_dir=str(tmp_path / "reports"),
        sources_file=str(tmp_path / "none.yaml"),
        spacy_models=[],
        dedupe_window_days=30,
    )


ARTICLE = """<html><head><title>Sanctions yacht seized in Fiji</title></head>
<body><nav>menu</nav><article><p>The superyacht Amadea was seized in Fiji after a
long investigation. Prosecutors say Igor Sechin is the beneficial owner of the
vessel, which sailed from Russia last month. The vessel is registered in Malta
and carries the IMO number 9876543.</p></article><footer>ads</footer></body></html>"""

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>World news</title>
<item>
  <guid>tag:example.test,2026:story-1</guid>
  <link>https://example.test/story-1</link>
  <title>Sanctions yacht seized in Fiji</title>
  <description>Short summary of the story.</description>
  <pubDate>Wed, 24 Sep 2026 08:00:00 GMT</pubDate>
  <dc:creator>Jane Reporter</dc:creator>
</item>
<item>
  <link>https://example.test/too-short</link>
  <title>Tiny</title>
  <description>Not enough words here.</description>
</item>
</channel></rss>"""


# --------------------------------------------------------------------------- #
# Confidence weighting — the contract from the specification
# --------------------------------------------------------------------------- #


def test_structured_sources_weigh_one(env):
    source_spec = spec(kind=SourceType.STRUCTURED)
    adapter, _ = adapter_for(env, source_spec=source_spec)
    assert adapter.source_weight == 1.0
    assert adapter.method is ExtractionMethod.STRUCTURED


def test_unstructured_sources_weigh_four_tenths(env):
    source_spec = spec(kind=SourceType.UNSTRUCTURED)
    adapter, _ = adapter_for(env, source_spec=source_spec)
    assert adapter.source_weight == 0.4
    assert adapter.method is ExtractionMethod.DEPENDENCY


def test_every_registered_source_carries_its_family_weight():
    for source in SOURCE_REGISTRY:
        expected = 1.0 if source.kind is SourceType.STRUCTURED else 0.4
        assert source.confidence == expected, source.id


def test_the_four_structured_families_are_present():
    """ICIJ, OpenCorporates, Wikidata and official registers — all at 1.0."""
    structured = {s.id for s in SOURCE_REGISTRY if s.kind is SourceType.STRUCTURED}
    assert {"icij_leaks", "opencorporates", "wikidata", "companies_house", "register_files"} <= structured


def test_news_and_rss_sources_are_unstructured():
    unstructured = {s.id for s in SOURCE_REGISTRY if s.kind is SourceType.UNSTRUCTURED}
    assert {"news_world", "aviation_news", "occrp_rss", "icij_stories"} <= unstructured


def test_structured_relation_confidence_is_the_full_weight(env):
    adapter, _ = adapter_for(env, source_spec=spec(kind=SourceType.STRUCTURED))
    subject = adapter.entity("Gazprom", EntityType.ORGANIZATION)
    obj = adapter.entity("Rosneft", EntityType.ORGANIZATION)
    edge = adapter.relation(subject, RelationType.OWNS, obj, evidence="registry row")
    assert edge.confidence == pytest.approx(1.0)
    assert edge.source_weight == 1.0
    assert edge.method is ExtractionMethod.STRUCTURED
    assert edge.extra["rule"] == "structured:test_source"


def test_unstructured_relation_confidence_is_capped_at_the_source_weight(env):
    adapter, _ = adapter_for(env, source_spec=spec(kind=SourceType.UNSTRUCTURED))
    subject = adapter.entity("Gazprom", EntityType.ORGANIZATION)
    obj = adapter.entity("Rosneft", EntityType.ORGANIZATION)
    edge = adapter.relation(subject, RelationType.OWNS, obj)
    assert edge.confidence == pytest.approx(0.4)
    assert edge.source_weight == 0.4


def test_entity_confidence_defaults_to_the_source_weight(env):
    structured, _ = adapter_for(env, source_spec=spec(kind=SourceType.STRUCTURED))
    unstructured, _ = adapter_for(env, source_spec=spec(kind=SourceType.UNSTRUCTURED))
    assert structured.entity("Gazprom", EntityType.ORGANIZATION).confidence == 1.0
    assert unstructured.entity("Gazprom", EntityType.ORGANIZATION).confidence == pytest.approx(0.4)
    assert structured.entity("Gazprom", EntityType.ORGANIZATION).mentions[0].detector == "structured:test_source"


def test_build_helpers_compose_confidence_from_weight_method_and_evidence():
    node = build_entity("Gazprom", EntityType.ORGANIZATION, source_id="s", confidence=0.5)
    assert node.confidence == 0.5
    edge = build_relation(node, "OWNS", node, source_id="s", source_weight=1.0, evidence_score=0.5)
    assert edge.confidence == pytest.approx(0.5)
    assert edge.predicate is RelationType.OWNS


# --------------------------------------------------------------------------- #
# Framework: run(), _prepare(), fetch()
# --------------------------------------------------------------------------- #


def test_prepare_stamps_identity_weight_and_hash(env):
    adapter, _ = adapter_for(env)
    raw = Document(doc_id="", source_id="", url="https://example.test/x", text="Some sufficiently long text for the document.")
    prepared = adapter._prepare(raw)

    assert prepared is not None
    assert prepared.source_id == "test_source"
    assert prepared.source_weight == 1.0
    assert prepared.doc_id
    assert prepared.content_hash
    assert prepared.published_at is not None
    assert prepared.extra["source_kind"] == "structured"
    assert prepared.extra["source_confidence"] == 1.0


def test_prepare_skips_known_content_hashes(env):
    adapter, ctx = adapter_for(env)
    first = Document(doc_id="", source_id="test_source", url="https://example.test/a", text="Identical body text here.")
    prepared = adapter._prepare(first)
    assert prepared is not None
    assert prepared.content_hash in ctx.known_hashes

    duplicate = Document(doc_id="", source_id="test_source", url="https://example.test/b", text="Identical body text here.")
    assert adapter._prepare(duplicate) is None
    assert adapter.stats.documents_skipped_duplicate == 1
    assert adapter.stats.per_source["test_source"]["duplicates"] == 1


def test_prepare_skips_documents_without_payload(env):
    adapter, _ = adapter_for(env)
    empty = Document(doc_id="", source_id="test_source", url="https://example.test/empty")
    assert adapter._prepare(empty) is None


def test_prepare_honours_the_dedupe_switch(env):
    env.only_new_documents = False
    adapter, ctx = adapter_for(env)
    body = Document(doc_id="", source_id="test_source", url="https://example.test/a", text="Identical body text here.")
    assert adapter._prepare(body) is not None
    ctx.known_hashes.add(adapter._prepare(body).content_hash)
    again = Document(doc_id="", source_id="test_source", url="https://example.test/b", text="Identical body text here.")
    assert adapter._prepare(again) is not None, "dedupe disabled means duplicates pass"


def test_run_counts_documents_and_per_source_stats(env):
    adapter, _ = adapter_for(env, source_spec=spec(count=3))
    documents = list(adapter.run())
    assert len(documents) == 3
    assert adapter.stats.documents_fetched == 3
    assert adapter.stats.per_source["test_source"]["documents"] == 3


def test_run_respects_the_limit(env):
    adapter, _ = adapter_for(env, source_spec=spec(count=10))
    assert len(list(adapter.run(limit=2))) == 2


def test_run_respects_the_spec_cap(env):
    adapter, _ = adapter_for(env, source_spec=spec(count=10, max_documents=4))
    assert len(list(adapter.run())) == 4


def test_run_stops_when_the_budget_is_gone(env):
    adapter, _ = adapter_for(env, source_spec=spec(count=5))
    adapter.ctx.deadline = time.monotonic() - 1
    assert list(adapter.run()) == []


def test_run_converts_adapter_failures_into_adapter_errors(env):
    adapter, _ = adapter_for(env, source_spec=spec(error=RuntimeError("upstream exploded")))
    with pytest.raises(AdapterError, match="upstream exploded"):
        list(adapter.run())
    assert adapter.stats.documents_failed == 1
    assert adapter.stats.per_source["test_source"]["errors"] == 1
    assert any("upstream exploded" in error for error in adapter.stats.errors)


def test_fetch_forwards_politeness_settings(env):
    client = FakeFetchClient({"https://example.test/page": ok_text("https://example.test/page", "<p>hi</p>")})
    adapter, _ = adapter_for(env, client=client, source_spec=spec(respect_robots=False, cache_ttl_seconds=900))
    adapter.fetch("https://example.test/page", mode="browser")

    kwargs = client.kwargs_for("https://example.test/page")
    assert kwargs["source"] is adapter.spec
    assert kwargs["source_id"] == "test_source"
    assert kwargs["respect_robots"] is False
    assert kwargs["cache_ttl_seconds"] == 900
    assert kwargs["mode"] == "browser"


def test_fetch_json_returns_the_payload(env):
    url = "https://example.test/data"
    client = FakeFetchClient({url: ok_json(url, {"hello": "world"})})
    adapter, _ = adapter_for(env, client=client)
    assert adapter.fetch_json(url) == {"hello": "world"}


def test_fetch_json_records_a_failed_fetch(env):
    url = "https://example.test/missing"
    client = FakeFetchClient({url: FetchResult(url=url, status=503, ok=False, error="rate-limited")})
    adapter, _ = adapter_for(env, client=client)
    assert adapter.fetch_json(url) is None
    assert adapter.stats.documents_failed == 1
    assert any("503" in error for error in adapter.stats.errors)


def test_fetch_json_records_malformed_json(env):
    url = "https://example.test/broken"
    client = FakeFetchClient({url: ok_text(url, "{not json", content_type="application/json")})
    adapter, _ = adapter_for(env, client=client)
    assert adapter.fetch_json(url) is None
    assert any("invalid JSON" in error for error in adapter.stats.errors)


def test_option_reads_the_spec_with_a_default(env):
    adapter, _ = adapter_for(env, source_spec=spec(per_page=10))
    assert adapter.option("per_page") == 10
    assert adapter.option("missing", "fallback") == "fallback"
    assert adapter.option("nothing") is None


def test_option_treats_an_explicit_none_as_missing(env):
    adapter, _ = adapter_for(env, source_spec=spec(value=None))
    assert adapter.option("value", "default") == "default"


def test_absolute_url_resolves_relative_links(env):
    adapter, _ = adapter_for(env)
    assert adapter.absolute_url("https://example.test/a/b", "/c") == "https://example.test/c"
    assert adapter.absolute_url("https://example.test/a", "https://other.test/x") == "https://other.test/x"
    assert adapter.absolute_url("https://example.test/a", "") == "https://example.test/a"


def test_parse_datetime_accepts_the_common_wire_formats(env):
    adapter, _ = adapter_for(env)
    assert adapter.parse_datetime("2026-09-27T10:00:00Z").year == 2026
    assert adapter.parse_datetime("Wed, 24 Sep 2026 08:00:00 GMT").month == 9
    assert adapter.parse_datetime(1_800_000_000).year >= 2027
    assert adapter.parse_datetime("1800000000000").year >= 2027  # milliseconds
    assert adapter.parse_datetime("not a date") is None
    assert adapter.parse_datetime(None) is None
    assert adapter.parse_datetime("") is None


def test_parse_datetime_adds_utc_to_naive_values(env):
    adapter, _ = adapter_for(env)
    from datetime import datetime

    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert adapter.parse_datetime(naive).tzinfo is not None


def test_within_window_filters_old_items(env):
    adapter, _ = adapter_for(env)
    assert adapter.within_window(utcnow() - timedelta(days=1), 7) is True
    assert adapter.within_window(utcnow() - timedelta(days=30), 7) is False
    assert adapter.within_window(None, 7) is True, "unknown dates must not be dropped"
    assert adapter.within_window(utcnow() - timedelta(days=400), 0) is True


def test_describe_reports_the_adapter_state(env):
    adapter, _ = adapter_for(env, source_spec=spec(count=2))
    list(adapter.run())
    description = adapter.describe()
    assert description["source_id"] == "test_source"
    assert description["adapter"] == "minimal"
    assert description["documents"] == 2
    assert description["confidence"] == 1.0


def test_create_adapter_resolves_by_spec_name(env):
    source_spec = spec(adapter="rss")
    ctx = context(env, source_spec=source_spec)
    assert isinstance(create_adapter(source_spec, ctx), RssAdapter)


def test_create_adapter_rejects_an_unknown_adapter(env):
    source_spec = spec(adapter="does_not_exist")
    with pytest.raises(AdapterError, match="No adapter registered"):
        create_adapter(source_spec, context(env, source_spec=source_spec))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_ids_are_unique():
    assert len(set(ALL_SOURCE_IDS)) == len(ALL_SOURCE_IDS) == len(SOURCE_REGISTRY)


def test_every_registered_adapter_is_implemented():
    assert set(ALL_SOURCE_IDS)
    for source in SOURCE_REGISTRY:
        assert source.adapter in ADAPTERS, f"{source.id} → {source.adapter} has no implementation"


def test_get_spec_matches_id_and_adapter():
    assert get_spec("wikidata").id == "wikidata"
    assert get_spec("WIKIDATA").id == "wikidata"
    assert get_spec("rss").adapter == "rss"
    assert get_spec("nope") is None


def test_iter_specs_filters_by_id_or_adapter():
    assert [s.id for s in iter_specs(["wikidata"])] == ["wikidata"]
    assert len(list(iter_specs(["rss"]))) == len(specs_for_adapter("rss"))
    assert len(list(iter_specs(None))) == len(SOURCE_REGISTRY)


def test_describe_registry_is_serialisable():
    payload = describe_registry()
    assert len(payload) == len(SOURCE_REGISTRY)
    json.dumps(payload)  # must not raise
    assert {"id", "adapter", "kind", "confidence", "options"} <= set(payload[0])


def test_registry_specs_declare_politeness():
    for source in SOURCE_REGISTRY:
        assert source.rate_per_sec > 0
        assert source.burst >= 1
        assert source.max_documents >= 1


# --------------------------------------------------------------------------- #
# RSS adapter (unstructured, weight 0.4)
# --------------------------------------------------------------------------- #


def rss_adapter(env, *, feed_url="https://example.test/feed.xml", responses=None, **options):
    source_spec = SourceSpec(
        id="news_world",
        name="World news",
        kind=SourceType.UNSTRUCTURED,
        adapter="rss",
        base_url="",
        max_documents=20,
        options={"feeds": [feed_url], **options},
    )
    client = FakeFetchClient(responses or {feed_url: ok_text(feed_url, FEED, content_type="application/rss+xml")})
    ctx = context(env, client, source_spec=source_spec)
    return RssAdapter(source_spec, ctx), client


def test_rss_harvest_turns_entries_into_documents(env):
    adapter, client = rss_adapter(env, responses={
        "https://example.test/feed.xml": ok_text("https://example.test/feed.xml", FEED, "application/rss+xml"),
        "https://example.test/story-1": ok_text("https://example.test/story-1", ARTICLE),
    })
    documents = list(adapter.run())

    assert len(documents) == 1, "the two-word entry must be dropped"
    story = documents[0]
    assert story.source_id == "news_world"
    assert story.source_weight == 0.4
    assert story.url == "https://example.test/story-1"
    assert story.title == "Sanctions yacht seized in Fiji"
    assert story.external_id == "tag:example.test,2026:story-1"
    assert story.content_type == "text/html"
    assert "Amadea" in story.text
    assert story.extra["feed_url"] == "https://example.test/feed.xml"
    assert story.extra["source_kind"] == "unstructured"
    # The article, not the two-line summary, is what gets parsed.
    assert client.kwargs_for("https://example.test/story-1")["referer"] == "https://example.test/feed.xml"


def test_rss_can_use_the_feed_text_only(env):
    adapter, client = rss_adapter(env, fetch_full_articles=False)
    documents = list(adapter.run())
    assert documents == [], "a four-word summary is below the parse floor"
    assert all(url == "https://example.test/feed.xml" for url in client.urls()), "no article fetches"


def test_rss_keeps_a_long_feed_summary_without_fetching(env):
    long_summary = (
        "The superyacht Amadea was seized in Fiji after a long investigation into "
        "sanctions evasion by Russian officials and their offshore intermediaries."
    )
    feed = FEED.replace("Short summary of the story.", long_summary)
    adapter, client = rss_adapter(
        env,
        responses={"https://example.test/feed.xml": ok_text("https://example.test/feed.xml", feed, "application/rss+xml")},
        fetch_full_articles=False,
    )
    documents = list(adapter.run())
    assert len(documents) == 1
    assert documents[0].content_type == "text/plain"
    assert long_summary.split()[3] in documents[0].text
    assert client.urls() == ["https://example.test/feed.xml"]


def test_rss_falls_back_to_the_feed_text_when_the_article_fetch_fails(env):
    long_summary = (
        "The superyacht Amadea was seized in Fiji after a long investigation into "
        "sanctions evasion by Russian officials and their offshore intermediaries."
    )
    feed = FEED.replace("Short summary of the story.", long_summary)
    adapter, _ = rss_adapter(
        env,
        responses={
            "https://example.test/feed.xml": ok_text("https://example.test/feed.xml", feed, "application/rss+xml"),
            "https://example.test/story-1": FetchResult(url="https://example.test/story-1", status=403, ok=False, error="forbidden"),
        },
    )
    documents = list(adapter.run())
    assert len(documents) == 1
    assert documents[0].content_type == "text/plain"


def test_rss_drops_entries_outside_the_recency_window(env):
    adapter, _ = rss_adapter(env, recent_days=1)
    assert list(adapter.run()) == [], "a 2026-09-24 entry is older than one day"


def test_rss_records_a_feed_failure(env):
    adapter, _ = rss_adapter(
        env,
        responses={"https://example.test/feed.xml": FetchResult(url="f", status=503, ok=False, error="rate-limited")},
    )
    assert list(adapter.run()) == []
    assert adapter.stats.per_source["news_world"]["errors"] == 1
    assert any("feed fetch failed" in error for error in adapter.stats.errors)


def test_rss_handles_an_empty_feed(env):
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    adapter, _ = rss_adapter(env, responses={"https://example.test/feed.xml": ok_text("https://example.test/feed.xml", empty, "application/rss+xml")})
    assert list(adapter.run()) == []
    assert adapter.stats.errors == [], "an empty feed is not an error"


def test_rss_handles_an_unparseable_feed(env):
    adapter, _ = rss_adapter(env, responses={"https://example.test/feed.xml": ok_text("https://example.test/feed.xml", "<html>not a feed</html>", "text/html")})
    assert list(adapter.run()) == []


def test_rss_without_configured_feeds_harvests_nothing(env):
    source_spec = SourceSpec(id="news_world", name="News", kind=SourceType.UNSTRUCTURED, adapter="rss", options={"feeds": []})
    adapter = RssAdapter(source_spec, context(env, FakeFetchClient(), source_spec=source_spec))
    assert list(adapter.run()) == []


def test_rss_per_feed_limit_caps_entries(env):
    items = "".join(
        f"<item><link>https://example.test/s{i}</link><title>Story {i}</title>"
        f"<description>{'Word ' * 40}about Gazprom and Rosneft and Igor Sechin.</description></item>"
        for i in range(6)
    )
    feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
    adapter, _ = rss_adapter(
        env,
        responses={"https://example.test/feed.xml": ok_text("https://example.test/feed.xml", feed, "application/rss+xml")},
        per_feed_limit=2,
        fetch_full_articles=False,
    )
    assert len(list(adapter.run())) == 2


def test_rss_feed_urls_prefer_options_then_environment_then_base_url(env):
    env.rss_feeds = ["https://env.test/feed"]
    source_spec = SourceSpec(id="news_world", name="News", kind=SourceType.UNSTRUCTURED, adapter="rss", options={"feeds": ["https://option.test/feed"]})
    adapter = RssAdapter(source_spec, context(env, FakeFetchClient(), source_spec=source_spec))
    urls = list(adapter._feed_urls())
    assert urls[0] == "https://option.test/feed"
    assert "https://env.test/feed" in urls
    assert len(urls) == len(set(urls))


def test_rss_feed_urls_accept_dict_entries(env):
    source_spec = SourceSpec(
        id="press",
        name="Press",
        kind=SourceType.UNSTRUCTURED,
        adapter="rss",
        options={"feeds": [{"url": "https://a.test/feed"}, {"feed": "https://b.test/feed"}, "https://c.test/feed"]},
    )
    adapter = RssAdapter(source_spec, context(env, FakeFetchClient(), source_spec=source_spec))
    assert list(adapter._feed_urls()) == ["https://a.test/feed", "https://b.test/feed", "https://c.test/feed"]


def test_strip_html_removes_markup_and_decodes_entities():
    assert _strip_html("<p>Gazprom &amp; Rosneft</p>") == "Gazprom & Rosneft"
    assert _strip_html("caf&#233; &#x2014; plain") == "café — plain"
    assert _strip_html("") == ""
    assert _strip_html("no markup here") == "no markup here"


# --------------------------------------------------------------------------- #
# Wikidata adapter (structured, weight 1.0)
# --------------------------------------------------------------------------- #


def sparql_payload(bindings: list[dict]) -> dict:
    return {"head": {"vars": ["company", "owner"]}, "results": {"bindings": bindings}}


def binding(subject: str, subject_label: str, obj: str, obj_label: str, **extra: str) -> dict:
    row = {
        "company": {"type": "uri", "value": subject},
        "companyLabel": {"type": "literal", "value": subject_label},
        "owner": {"type": "uri", "value": obj},
        "ownerLabel": {"type": "literal", "value": obj_label},
    }
    row.update({key: {"type": "literal", "value": value} for key, value in extra.items()})
    return row


def wikidata_adapter(env, payload: dict, *, queries=("ownership",), **options):
    source_spec = SourceSpec(
        id="wikidata",
        name="Wikidata",
        kind=SourceType.STRUCTURED,
        adapter="wikidata",
        base_url="https://query.wikidata.org/sparql",
        max_documents=10,
        options={"queries": list(queries), "limit_per_query": 50, **options},
    )
    client = FakeFetchClient({"https://query.wikidata.org/sparql": ok_json("https://query.wikidata.org/sparql", payload)})
    ctx = context(env, client, source_spec=source_spec)
    return WikidataAdapter(source_spec, ctx), client


def test_wikidata_maps_bindings_to_weighted_triples(env):
    payload = sparql_payload(
        [
            binding("http://www.wikidata.org/entity/Q1", "Gazprom", "http://www.wikidata.org/entity/Q2", "Rosneft", lei="1234LEI"),
            binding("http://www.wikidata.org/entity/Q3", "Nord Stream AG", "http://www.wikidata.org/entity/Q5", "Igor Sechin"),
        ]
    )
    adapter, client = wikidata_adapter(env, payload)
    documents = list(adapter.run())

    assert len(documents) == 1
    document = documents[0]
    assert document.source_weight == 1.0
    assert document.external_id == "wikidata-ownership-50"
    assert document.extra["triples"] == 2
    assert document.extra["bindings"] == 2
    assert {edge.predicate for edge in document.relations} == {RelationType.OWNED_BY}
    assert all(edge.confidence == pytest.approx(1.0) for edge in document.relations)
    assert all(edge.method is ExtractionMethod.STRUCTURED for edge in document.relations)

    names = {entity.name for entity in document.entities}
    assert {"Gazprom", "Rosneft", "Nord Stream AG", "Igor Sechin"} <= names
    sechin = next(e for e in document.entities if e.name == "Igor Sechin")
    assert sechin.entity_type is EntityType.PERSON, "a person label must not become an ORG"
    gazprom = next(e for e in document.entities if e.name == "Gazprom")
    assert gazprom.properties["wikidata_id"] == "Q1"
    assert gazprom.properties["lei"] == "1234LEI"

    # SPARQL text must carry the query, the limit and the declared UA.
    sent = client.kwargs_for("https://query.wikidata.org/sparql")
    assert "LIMIT 50" in sent["params"]["query"]
    assert sent["params"]["maxlag"] == 5
    assert sent["headers"]["User-Agent"]


def test_wikidata_skips_rows_without_labels_or_self_references(env):
    payload = sparql_payload(
        [
            {"company": {"value": "http://www.wikidata.org/entity/Q1"}},  # no labels
            binding("http://www.wikidata.org/entity/Q2", "Gazprom", "http://www.wikidata.org/entity/Q2", "gazprom"),
            binding("http://www.wikidata.org/entity/Q3", "Rosneft", "http://www.wikidata.org/entity/Q4", "Gazprom Neft"),
        ]
    )
    adapter, _ = wikidata_adapter(env, payload)
    documents = list(adapter.run())
    assert len(documents) == 1
    assert documents[0].extra["triples"] == 1


def test_wikidata_returns_nothing_for_empty_bindings(env):
    adapter, _ = wikidata_adapter(env, sparql_payload([]))
    assert list(adapter.run()) == []
    assert adapter.stats.errors == []


def test_wikidata_reports_a_failed_query(env):
    adapter, _ = wikidata_adapter(
        env,
        {},
    )
    adapter.client = FakeFetchClient({"https://query.wikidata.org/sparql": FetchResult(url="q", status=429, ok=False, error="maxlag")})
    assert list(adapter.run()) == []
    assert adapter.stats.per_source["wikidata"]["errors"] == 1


def test_wikidata_skips_unknown_query_names(env):
    adapter, _ = wikidata_adapter(env, sparql_payload([]), queries=("not_a_real_query",))
    assert list(adapter.run()) == []
    assert adapter.stats.per_source["wikidata"]["errors"] == 1


def test_wikidata_defaults_to_three_queries_when_none_are_configured(env):
    source_spec = SourceSpec(id="wikidata", name="Wikidata", kind=SourceType.STRUCTURED, adapter="wikidata", options={"queries": []})
    client = FakeFetchClient({"https://query.wikidata.org/sparql": ok_json("https://query.wikidata.org/sparql", sparql_payload([]))})
    adapter = WikidataAdapter(source_spec, context(env, client, source_spec=source_spec))
    env.wikidata_queries = []
    assert list(adapter.run()) == []
    assert len(client.calls) == 3, "ownership, subsidiaries, board_members"


def test_wikidata_query_catalogue_is_well_formed():
    assert {"ownership", "subsidiaries", "board_members"} <= set(WIKIDATA_QUERIES)
    for name, query in WIKIDATA_QUERIES.items():
        assert query.name == name
        assert "%LIMIT%" in query.sparql or name == "custom"
        assert query.predicate in RelationType
        assert query.subject_type in EntityType
        assert query.description


def test_qid_and_person_helpers():
    assert _qid("http://www.wikidata.org/entity/Q1234") == "Q1234"
    assert _qid("") == ""
    assert _looks_like_person("Igor Sechin") is True
    assert _looks_like_person("Gazprom PJSC") is False
    assert _looks_like_person("Ministry of Finance") is False


# --------------------------------------------------------------------------- #
# OpenCorporates adapter (structured, weight 1.0)
# --------------------------------------------------------------------------- #


COMPANY_PAYLOAD = {
    "api_version": "0.4",
    "results": {
        "companies": [
            {
                "company": {
                    "name": "Nord Stream AG",
                    "company_number": "CHE-123.456.789",
                    "jurisdiction_code": "ch",
                    "opencorporates_url": "https://opencorporates.com/companies/ch/CHE-123.456.789",
                    "incorporation_date": "2006-04-03",
                    "current_status": "Active",
                    "registered_address_in_full": "Bahnhofstrasse 1, Zug, Switzerland",
                    "industry_codes": [{"industry_code": {"code": "35.21", "description": "Manufacture of gas"}}],
                    "parent_company": {"name": "Gazprom", "jurisdiction_code": "ru"},
                    "previous_names": [{"company_name": "North European Gas Pipeline"}],
                    "retrieved_at": "2026-09-20T00:00:00Z",
                }
            },
            {"company": {"name": "", "jurisdiction_code": "gb"}},
        ]
    },
}


def opencorporates_adapter(env, payload: dict, *, queries=("Nord Stream",), **options):
    source_spec = SourceSpec(
        id="opencorporates",
        name="OpenCorporates",
        kind=SourceType.STRUCTURED,
        adapter="opencorporates",
        base_url="https://api.opencorporates.com/v0.4",
        max_documents=20,
        options={
            "queries": list(queries),
            "jurisdictions": ["ch"],
            "per_page": 10,
            "include_officers": False,
            "include_groupings": False,
            **options,
        },
    )
    client = FakeFetchClient(
        {
            "https://api.opencorporates.com/v0.4/companies/search": ok_json("companies/search", payload),
            "https://api.opencorporates.com/v0.4/officers/search": ok_json("officers/search", {"api_version": "0.4", "results": {"officers": []}}),
        }
    )
    ctx = context(env, client, source_spec=source_spec)
    return OpenCorporatesAdapter(source_spec, ctx), client


def test_opencorporates_maps_a_company_record_to_weighted_triples(env):
    adapter, _ = opencorporates_adapter(env, COMPANY_PAYLOAD)
    documents = list(adapter.run())

    assert len(documents) == 1, "the nameless company must be dropped"
    document = documents[0]
    assert document.source_weight == 1.0
    assert document.external_id == "oc-company-ch-CHE-123.456.789"
    assert document.title == "OpenCorporates — Nord Stream AG"

    predicates = {edge.predicate for edge in document.relations}
    assert RelationType.REGISTERED_IN in predicates
    assert RelationType.SUBSIDIARY_OF in predicates
    assert all(edge.confidence == pytest.approx(1.0) for edge in document.relations)

    names = {entity.name for entity in document.entities}
    assert "Nord Stream AG" in names and "Gazprom" in names
    company = next(e for e in document.entities if e.name == "Nord Stream AG")
    assert company.properties["company_number"] == "CHE-123.456.789"
    assert company.properties["jurisdiction_code"] == "ch"
    assert "North European Gas Pipeline" in company.aliases


def test_opencorporates_include_officers_gates_the_officer_search(env):
    """Turning officers off must not spend rate budget on that endpoint."""
    adapter, client = opencorporates_adapter(env, COMPANY_PAYLOAD, include_officers=False, include_groupings=False)
    documents = list(adapter.run())
    assert len(documents) == 1
    assert not any(url.endswith("/officers/search") for url in client.urls()), client.urls()


def test_opencorporates_fetches_officers_when_enabled(env):
    adapter, client = opencorporates_adapter(env, COMPANY_PAYLOAD, include_officers=True, include_groupings=False)
    list(adapter.run())
    assert any(url.endswith("/officers/search") for url in client.urls())


def test_make_document_backfills_document_provenance(env):
    """Structured entities are built before the doc id exists; it must be stamped."""
    adapter, _ = adapter_for(env, source_spec=spec(kind=SourceType.STRUCTURED))
    subject = adapter.entity("Gazprom", EntityType.ORGANIZATION)
    obj = adapter.entity("Rosneft", EntityType.ORGANIZATION)
    edge = adapter.relation(subject, RelationType.OWNS, obj)

    document = adapter.make_document(
        "https://example.test/record/1",
        title="Registry row",
        text="Gazprom owns Rosneft.",
        entities=[subject, obj],
        relations=[edge],
    )

    assert document.doc_id
    assert subject.doc_ids == {document.doc_id}
    assert obj.doc_ids == {document.doc_id}
    assert edge.doc_id == document.doc_id
    # The MENTIONS writer skips rows without a doc id — this is what feeds it.
    from puppetnet.graph import schema

    rows = schema.properties_for_mention_rows([subject])
    assert rows and rows[0]["doc_id"] == document.doc_id


def test_opencorporates_requires_configured_queries(env):
    adapter, client = opencorporates_adapter(env, COMPANY_PAYLOAD, queries=[])
    assert list(adapter.run()) == []
    assert client.calls == []


def test_opencorporates_sends_the_api_token_when_configured(env):
    env.opencorporates_api_token = "oc-token-123"
    adapter, client = opencorporates_adapter(env, COMPANY_PAYLOAD)
    list(adapter.run())
    params = client.kwargs_for("https://api.opencorporates.com/v0.4/companies/search")["params"]
    assert params["api_token"] == "oc-token-123"
    assert params["q"] == "Nord Stream"
    assert params["jurisdiction_code"] == "ch"


def test_opencorporates_records_a_failed_search(env):
    source_spec = SourceSpec(
        id="opencorporates",
        name="OpenCorporates",
        kind=SourceType.STRUCTURED,
        adapter="opencorporates",
        base_url="https://api.opencorporates.com/v0.4",
        options={"queries": ["Gazprom"], "jurisdictions": [], "include_groupings": False, "include_officers": False},
    )
    client = FakeFetchClient(default=FetchResult(url="x", status=403, ok=False, error="quota"))
    adapter = OpenCorporatesAdapter(source_spec, context(env, client, source_spec=source_spec))
    assert list(adapter.run()) == []
    assert adapter.stats.per_source["opencorporates"]["errors"] >= 1


def test_opencorporates_can_be_disabled_without_a_token(env):
    env.opencorporates_api_token = ""
    source_spec = SourceSpec(
        id="opencorporates",
        name="OpenCorporates",
        kind=SourceType.STRUCTURED,
        adapter="opencorporates",
        options={"queries": ["Gazprom"], "allow_anonymous": False},
    )
    client = FakeFetchClient()
    adapter = OpenCorporatesAdapter(source_spec, context(env, client, source_spec=source_spec))
    assert list(adapter.run()) == []
    assert client.calls == []
