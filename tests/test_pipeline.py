"""End-to-end pipeline tests: harvest → parse → weight → (dry-run) Neo4j.

The pipeline is driven with a stub source adapter registered under the name
``stub`` and a stub source registry, so no HTTP, no spaCy model download and no
database are involved. Graph writes go through the real :class:`GraphWriter`
against a dry-run :class:`Neo4jClient`, which records every statement — that is
what the assertions inspect.
"""

from __future__ import annotations

import dataclasses
import json
import time

import pytest

from puppetnet import pipeline as pipeline_module
from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable
from puppetnet.graph.schema import ensure_schema_statements
from puppetnet.models import (
    Cadence,
    Document,
    Entity,
    EntityType,
    ExtractionMethod,
    IngestStats,
    Relation,
    RelationType,
    SourceSpec,
    SourceType,
)
from puppetnet.pipeline import IngestPipeline, PipelineOptions, RunReport, run_pipeline
from puppetnet.sources import ADAPTERS, SourceAdapter
from puppetnet.sources.base import build_relation

# --------------------------------------------------------------------------- #
# Stub source
# --------------------------------------------------------------------------- #


class StubAdapter(SourceAdapter):
    """Yields the documents parked on ``spec.options`` (or raises on demand)."""

    adapter_name = "stub"

    def harvest(self):
        error = self.spec.options.get("error")
        if error is not None:
            raise error
        yield from self.spec.options.get("documents", ())  # pragma: no branch


@pytest.fixture(autouse=True)
def stub_adapter(monkeypatch):
    registry = dict(ADAPTERS)
    registry[StubAdapter.adapter_name] = StubAdapter
    monkeypatch.setitem(ADAPTERS, StubAdapter.adapter_name, StubAdapter)
    yield registry


def document(source_id: str, url: str, text: str = "", *, title: str = "", entities=(), relations=()) -> Document:
    return Document(
        doc_id="",
        source_id=source_id,
        url=url,
        title=title or url.rsplit("/", 1)[-1],
        text=text,
        entities=list(entities),
        relations=list(relations),
    )


def stub_spec(
    source_id: str,
    kind: SourceType = SourceType.UNSTRUCTURED,
    *,
    documents=(),
    error: Exception | None = None,
    max_documents: int = 10,
    enabled: bool = True,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("_", " ").title(),
        kind=kind,
        adapter="stub",
        base_url="https://example.test",
        max_documents=max_documents,
        enabled=enabled,
        options={"documents": list(documents), "error": error},
    )


@pytest.fixture()
def env(settings, tmp_path, monkeypatch):
    """Offline settings whose state/report dirs live inside tmp_path."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return dataclasses.replace(
        settings,
        dry_run=True,
        state_dir=str(tmp_path / "state"),
        report_dir=str(tmp_path / "reports"),
        sources_file=str(tmp_path / "no-such-sources.yaml"),
        run_id="run-test-0001",
        nlp_enabled=True,
        spacy_models=[],
        only_new_documents=True,
        max_documents_per_source=10,
        max_documents_total=100,
        max_runtime_seconds=600,
        fail_on_error=False,
    )


@pytest.fixture()
def use_registry(monkeypatch):
    """Install a stub source registry inside the pipeline module."""

    def _install(*specs: SourceSpec) -> None:
        monkeypatch.setattr(pipeline_module, "SOURCE_REGISTRY", tuple(specs))

    return _install


def make_pipeline(env, options: PipelineOptions | None = None) -> IngestPipeline:
    return IngestPipeline(settings=dataclasses.replace(env), options=options or PipelineOptions())


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #


def test_structured_sources_are_harvested_first(env, use_registry):
    use_registry(
        stub_spec("news_world"),
        stub_spec("icij", SourceType.STRUCTURED),
    )
    pipeline = make_pipeline(env)
    specs = pipeline._resolve_specs()
    assert [spec.id for spec in specs] == ["icij", "news_world"]
    assert specs[0].confidence == 1.0 and specs[1].confidence == 0.4


def test_structured_first_can_be_disabled(env, use_registry):
    use_registry(stub_spec("news_world"), stub_spec("icij", SourceType.STRUCTURED))
    pipeline = make_pipeline(env, PipelineOptions(structured_first=False))
    assert [spec.id for spec in pipeline._resolve_specs()] == ["news_world", "icij"]


def test_requested_sources_filter_the_registry(env, use_registry):
    use_registry(stub_spec("news_world"), stub_spec("icij", SourceType.STRUCTURED))
    pipeline = make_pipeline(env, PipelineOptions(sources=["icij"]))
    assert [spec.id for spec in pipeline._resolve_specs()] == ["icij"]


def test_adapters_can_be_requested_by_adapter_name(env, use_registry):
    use_registry(stub_spec("news_world"), stub_spec("icij", SourceType.STRUCTURED))
    pipeline = make_pipeline(env, PipelineOptions(sources=["stub"]))
    assert len(pipeline._resolve_specs()) == 2


def test_unknown_source_ids_are_reported_not_fatal(env, use_registry):
    use_registry(stub_spec("news_world"))
    pipeline = make_pipeline(env, PipelineOptions(sources=["does_not_exist"]))
    assert pipeline._resolve_specs() == []


def test_disabled_specs_are_dropped(env, use_registry):
    use_registry(stub_spec("news_world"), stub_spec("off", enabled=False))
    pipeline = make_pipeline(env)
    assert [spec.id for spec in pipeline._resolve_specs()] == ["news_world"]


def test_environment_source_configuration_is_applied(env, use_registry):
    """RSS_FEEDS must reach the news adapter without editing the registry."""
    env.rss_feeds = ["https://example.test/feed.xml"]
    use_registry(stub_spec("news_world"))
    pipeline = make_pipeline(env)
    specs = pipeline._resolve_specs()
    # Only the real rss adapter consumes the feeds option; the stub keeps its own.
    assert specs[0].adapter == "stub"
    assert specs[0].options["documents"] == []


def test_per_source_document_cap_is_clamped_by_settings(env, use_registry):
    env.max_documents_per_source = 3
    use_registry(stub_spec("news_world", max_documents=50))
    pipeline = make_pipeline(env)
    assert pipeline._resolve_specs()[0].max_documents == 3


# --------------------------------------------------------------------------- #
# Harvest happy path
# --------------------------------------------------------------------------- #


def test_run_harvests_parses_and_records_writes(env, use_registry, tmp_path):
    structured = stub_spec(
        "icij",
        SourceType.STRUCTURED,
        documents=[
            document(
                "icij",
                "https://example.test/record/1",
                entities=[Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION, confidence=1.0)],
                relations=[],
            )
        ],
    )
    unstructured = stub_spec(
        "news_world",
        documents=[
            document(
                "news_world",
                "https://example.test/story/1",
                text="Gazprom owns Nord Stream AG. Igor Sechin met with Vladimir Putin in Moscow.",
            )
        ],
    )
    use_registry(structured, unstructured)

    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert stats.run_id == "run-test-0001"
    assert stats.documents_fetched == 2
    assert stats.entities_extracted >= 3
    assert stats.relations_extracted >= 1
    assert stats.sentences_processed == 2
    assert stats.characters_processed > 0
    assert stats.cooccurrence_triples >= 1, "no model → every triple is co-occurrence priced"
    assert stats.dependency_triples == 0
    assert stats.errors == []
    assert stats.per_source["icij"]["documents"] == 1
    assert stats.per_source["news_world"]["documents"] == 1
    assert stats.per_source["news_world"]["entities"] >= 3, "NLP output belongs to its source"
    assert stats.per_source["news_world"]["relations"] >= 1

    # Dry-run recorder proves the write ordering actually happened: schema DDL,
    # the run node (open, summary, per-source rows), entity/relation batches and
    # the index reads — the dedupe window and the alias index, plus one provenance
    # read per batch (entity doc_ids, relation doc_ids) that tells the writer which
    # observations are new evidence instead of a re-read.
    recorded = pipeline.neo4j.recorder.summary()
    assert recorded["statements"] > 0
    assert recorded["by_kind"]["schema"] == len(ensure_schema_statements())
    assert recorded["by_kind"]["run"] == 3, "open + summary + per-source rows"
    assert recorded["by_kind"]["write"] > 0
    # The two index reads are still the only reads the pipeline itself needs; the
    # provenance reads happen one per batch (see below), so the assertion is a
    # lower bound plus the semantic check that the provenance reads are the ones
    # that happened — a bare count here would only be brittle.
    assert recorded["by_kind"]["read"] >= 2, recorded["by_kind"]
    provenance_reads = [
        entry for entry in pipeline.neo4j.recorder.statements
        if entry["kind"] == "read"
        and "doc_ids" in entry["query_preview"]
        and entry["params_keys"] == ["keys"]
    ]
    assert provenance_reads, "the writer must read which documents an edge/node has already counted"
    assert pipeline.stats.entities_written >= 3


def test_run_writes_json_and_markdown_reports(env, use_registry, tmp_path):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline.run()

    json_path = tmp_path / "reports" / "run-test-0001.json"
    markdown_path = tmp_path / "reports" / "run-test-0001.md"
    assert json_path.exists() and markdown_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-test-0001"
    assert payload["graph"]["status"] == "completed"
    assert payload["graph"]["dry_run"] is True
    assert payload["stats"]["documents_fetched"] == 1
    assert [source["source_id"] for source in payload["sources"]] == ["news_world"]
    assert "PuppetNET ingest report" in markdown_path.read_text(encoding="utf-8")


def test_report_can_be_skipped(env, use_registry, tmp_path):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env, PipelineOptions(write_report=False))
    pipeline.run()
    assert not (tmp_path / "reports").exists() or list((tmp_path / "reports").iterdir()) == []


def test_github_step_summary_and_outputs_are_published(env, use_registry, tmp_path, monkeypatch):
    summary_file = tmp_path / "step_summary.md"
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))

    pipeline = make_pipeline(env)
    pipeline.run()

    summary = summary_file.read_text(encoding="utf-8")
    outputs = output_file.read_text(encoding="utf-8")
    assert "PuppetNET ingest report" in summary
    assert "run_id=run-test-0001" in outputs
    assert "documents=1" in outputs
    assert "report_path=" in outputs


def test_run_with_no_enabled_sources_is_reported(env, use_registry):
    use_registry()
    pipeline = make_pipeline(env)
    stats = pipeline.run()
    assert stats.documents_fetched == 0
    assert any("no enabled sources" in error for error in stats.errors)
    assert pipeline.neo4j.recorder.summary()["by_kind"].get("run") == 2


# --------------------------------------------------------------------------- #
# Limits, budget, dedupe
# --------------------------------------------------------------------------- #


def test_limit_per_source_caps_the_harvest(env, use_registry):
    docs = [document("news_world", f"https://example.test/{i}", text=f"Story {i} about Gazprom.") for i in range(5)]
    use_registry(stub_spec("news_world", documents=docs, max_documents=5))
    pipeline = make_pipeline(env, PipelineOptions(limit_per_source=2))
    stats = pipeline.run()
    assert stats.documents_fetched == 2


def test_global_document_cap_stops_the_run(env, use_registry):
    env.max_documents_total = 3
    first = [document("a", f"https://example.test/a{i}", text=f"A{i} Gazprom owns Rosneft.") for i in range(3)]
    second = [document("b", f"https://example.test/b{i}", text=f"B{i} Gazprom owns Rosneft.") for i in range(3)]
    use_registry(
        stub_spec("alpha", SourceType.STRUCTURED, documents=first, max_documents=5),
        stub_spec("beta", SourceType.STRUCTURED, documents=second, max_documents=5),
    )
    pipeline = make_pipeline(env)
    stats = pipeline.run()
    assert stats.documents_fetched <= 3


def test_exhausted_time_budget_stops_before_the_first_source(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline.deadline = time.monotonic() - 1
    stats = pipeline.run()
    assert stats.documents_fetched == 0


def test_duplicate_content_is_skipped_on_the_second_run(env, use_registry):
    def specs():
        return stub_spec(
            "news_world",
            documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")],
        )

    use_registry(specs())
    first = make_pipeline(env)
    first.run()
    assert first.stats.documents_fetched == 1

    state_file = env.state_path / "content_hashes.json"
    assert state_file.exists(), "the local dedupe cache must survive the run"
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["count"] == 1 and len(payload["hashes"]) == 1

    use_registry(specs())
    second = make_pipeline(env)
    second.run()
    assert second.stats.documents_fetched == 0
    assert second.stats.documents_skipped_duplicate == 1


def test_dedupe_can_be_turned_off(env, use_registry):
    env.only_new_documents = False
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline._init_graph()
    assert pipeline._load_dedupe_index() == set()


def test_corrupt_local_state_is_ignored(env, use_registry):
    state_dir = env.state_path
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "content_hashes.json").write_text("{not json", encoding="utf-8")
    pipeline = make_pipeline(env)
    assert pipeline._read_local_state() == set()


def test_local_state_accepts_a_bare_list(env):
    state_dir = env.state_path
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "content_hashes.json").write_text(json.dumps(["abc", "def", ""]), encoding="utf-8")
    pipeline = make_pipeline(env)
    assert pipeline._read_local_state() == {"abc", "def"}


def test_local_state_is_bounded(env):
    pipeline = make_pipeline(env)
    docs = [Document(doc_id=f"d{i}", source_id="s", url=f"https://example.test/{i}", content_hash=f"{i:06d}") for i in range(5)]
    pipeline._flush_local_state(docs)
    payload = json.loads(pipeline._state_file().read_text(encoding="utf-8"))
    assert payload["count"] == 5
    assert payload["hashes"] == sorted(payload["hashes"])
    assert "updated_at" in payload


def test_flush_local_state_with_no_documents_writes_nothing(env):
    pipeline = make_pipeline(env)
    pipeline._flush_local_state([])
    assert not pipeline._state_file().exists()


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #


def test_a_failing_source_does_not_abort_the_run(env, use_registry):
    use_registry(
        stub_spec("broken", SourceType.STRUCTURED, error=RuntimeError("upstream 500")),
        stub_spec(
            "healthy",
            SourceType.STRUCTURED,
            documents=[document("healthy", "https://example.test/ok", entities=[Entity(name="Rosneft", entity_type=EntityType.ORGANIZATION)])],
        ),
    )
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert stats.documents_fetched == 1
    assert stats.documents_failed == 1
    assert any("broken" in error for error in stats.errors)
    reports = {report["source_id"]: report for report in pipeline._source_reports}
    assert "upstream 500" in reports["broken"]["error"]
    assert reports["healthy"]["error"] is None
    assert reports["healthy"]["documents"] == 1


def test_adapter_error_is_not_double_counted(env, use_registry):
    from puppetnet.sources import AdapterError

    use_registry(stub_spec("broken", SourceType.STRUCTURED, error=AdapterError("relay refused")))
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    # SourceAdapter.run() already accounted for the failure; the pipeline only
    # records the message on the per-source report.
    assert stats.documents_failed == 1
    assert stats.per_source["broken"]["errors"] == 1
    assert len([error for error in stats.errors if "broken" in error]) == 1


def test_fail_on_error_turns_a_bad_run_into_a_nonzero_exit(env, use_registry):
    env.fail_on_error = True
    use_registry(stub_spec("broken", SourceType.STRUCTURED, error=RuntimeError("boom")))
    pipeline = make_pipeline(env)
    with pytest.raises(SystemExit):
        pipeline.run()


def test_neo4j_unavailability_aborts_the_run(env, use_registry, monkeypatch):
    class DeadClient(Neo4jClient):
        def verify(self) -> bool:
            raise Neo4jUnavailable("Aura instance unreachable")

    monkeypatch.setattr(pipeline_module, "Neo4jClient", DeadClient)
    use_registry(stub_spec("news_world"))
    pipeline = make_pipeline(env)
    with pytest.raises(Neo4jUnavailable):
        pipeline.run()
    assert any("neo4j" in error for error in pipeline.stats.errors)


def test_nlp_failure_on_one_document_is_contained(env, use_registry, monkeypatch):
    use_registry(
        stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")])
    )
    def explode(self, _document):
        raise RuntimeError("model blew up")

    # Patch the class, not an instance: run() builds its own engine.
    monkeypatch.setattr(pipeline_module.NLPEngine, "parse_document", explode)
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert stats.documents_fetched == 1
    assert any("NLP failure" in error for error in stats.errors)
    assert stats.entities_extracted == 0
    assert stats.per_source["news_world"]["errors"] >= 1


def test_empty_documents_are_not_parsed(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", title="Headline only")]))
    pipeline = make_pipeline(env)
    pipeline._init_graph()
    pipeline._init_network()
    specs = pipeline._resolve_specs()
    pipeline._init_nlp(specs)
    result = pipeline._parse_document(Document(doc_id="x", source_id="news_world", url="u", text=""), specs[0])
    assert result.sentences == 0
    assert result.entities == []


def test_skip_nlp_leaves_unstructured_documents_unparsed(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env, PipelineOptions(skip_nlp=True))
    stats = pipeline.run()

    assert pipeline.nlp is None
    assert stats.documents_fetched == 1
    assert stats.sentences_processed == 0
    assert stats.entities_extracted == 0


def test_nlp_is_not_loaded_for_structured_sources_only(env, use_registry):
    use_registry(
        stub_spec("icij", SourceType.STRUCTURED, documents=[document("icij", "https://example.test/1", entities=[Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)])])
    )
    pipeline = make_pipeline(env)
    pipeline.run()
    assert pipeline.nlp is None, "a spaCy load costs seconds — skip it when unused"


def test_a_model_less_environment_is_not_a_run_error(env, use_registry):
    """The blank+gazetteer fallback is supported, so it must not poison stats."""
    env.spacy_models = ["en_core_web_trf", "en_core_web_lg"]  # not installed here
    use_registry(
        stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")])
    )
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert stats.errors == []
    assert pipeline.nlp is not None
    assert pipeline.nlp.backend.startswith("spacy:blank")
    assert pipeline.nlp.load_errors, "the fallbacks stay visible in the run report"
    assert stats.documents_fetched == 1
    assert stats.entities_extracted > 0


def test_nlp_disabled_by_settings(env, use_registry):
    env.nlp_enabled = False
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline.run()
    assert pipeline.nlp is None


def test_skip_graph_forces_the_dry_run_client(env, use_registry):
    env.dry_run = False
    env.neo4j_uri = "neo4j+s://db.example.test:7687"
    env.neo4j_password = "secret"
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env, PipelineOptions(skip_graph=True, dry_run=True))
    pipeline.run()
    assert pipeline.neo4j.dry_run is True
    assert pipeline.neo4j.driver is None


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def test_merge_entities_collapses_repeats_and_keeps_provenance():
    first = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION, source_ids={"a"}, confidence=0.6)
    second = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION, source_ids={"b"}, confidence=0.9)
    merged = IngestPipeline._merge_entities([first, second])
    assert len(merged) == 1
    assert merged[0].source_ids == {"a", "b"}
    assert merged[0].confidence == pytest.approx(0.9)


def test_merge_relations_drops_self_loops():
    node = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)
    edge = Relation(subject=node, predicate=RelationType.OWNS, obj=node, confidence=0.9)
    assert IngestPipeline._merge_relations([edge]) == []


def test_merge_relations_corroborates_with_noisy_or():
    subject = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)
    obj = Entity(name="Rosneft", entity_type=EntityType.ORGANIZATION)
    first = Relation(subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.4)
    second = Relation(subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.4)
    merged = IngestPipeline._merge_relations([first, second])

    assert len(merged) == 1
    assert merged[0].confidence > 0.4
    assert merged[0].extra["merged_observations"] == 2


def test_merge_relations_lets_a_parse_supersede_a_cooccurrence_guess():
    subject = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)
    obj = Entity(name="Rosneft", entity_type=EntityType.ORGANIZATION)
    weak = Relation(
        subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.2,
        method=ExtractionMethod.COOCCURRENCE, evidence="same sentence",
    )
    strong = Relation(
        subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.8,
        method=ExtractionMethod.DEPENDENCY, verb="owns", evidence="Gazprom owns Rosneft",
    )
    merged = IngestPipeline._merge_relations([weak, strong])[0]
    assert merged.method is ExtractionMethod.DEPENDENCY
    assert merged.verb == "owns"
    assert merged.evidence == "Gazprom owns Rosneft"


def test_merge_relations_keeps_distinct_predicates_apart():
    subject = Entity(name="Gazprom", entity_type=EntityType.ORGANIZATION)
    obj = Entity(name="Rosneft", entity_type=EntityType.ORGANIZATION)
    edges = [
        Relation(subject=subject, predicate=RelationType.OWNS, obj=obj, confidence=0.5),
        Relation(subject=obj, predicate=RelationType.OWNED_BY, obj=subject, confidence=0.5),
    ]
    assert len(IngestPipeline._merge_relations(edges)) == 2


# --------------------------------------------------------------------------- #
# Report / lifecycle
# --------------------------------------------------------------------------- #


def test_build_report_shape(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline.run()
    report = pipeline._build_report("completed", time.perf_counter())
    payload = report.to_dict()

    assert isinstance(report, RunReport)
    assert set(payload) >= {"run_id", "generated_at", "stats", "sources", "nlp", "graph", "network", "relations_by_type"}
    assert payload["nlp"]["backend"].startswith("spacy:blank")
    assert payload["graph"]["summary"]["documents"] == 1


def test_cleanup_closes_every_resource(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline.run()
    assert pipeline.neo4j.describe()["connected"] is False
    pipeline._cleanup()  # idempotent


def test_signal_handler_finishes_the_current_source_then_stops(env, use_registry, monkeypatch):
    handlers: dict = {}

    def fake_signal(signum, handler):
        handlers[signum] = handler

    monkeypatch.setattr(pipeline_module.signal, "signal", fake_signal)
    use_registry(
        stub_spec("alpha", SourceType.STRUCTURED, documents=[document("alpha", "https://example.test/a")]),
        stub_spec("beta", SourceType.STRUCTURED, documents=[document("beta", "https://example.test/b")]),
    )
    pipeline = make_pipeline(env)
    pipeline._install_signal_handlers()
    assert handlers, "SIGTERM/SIGINT must be handled for graceful cron shutdown"

    handler = next(iter(handlers.values()))
    handler(15, None)
    assert pipeline._interrupted is True
    assert pipeline.deadline <= time.monotonic()


def test_interrupted_run_is_reported_as_interrupted(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = make_pipeline(env)
    pipeline._interrupted = True
    pipeline.run()
    payload = json.loads((env.report_path / "run-test-0001.json").read_text(encoding="utf-8"))
    assert payload["graph"]["status"] == "interrupted"


def test_run_pipeline_wrapper_returns_stats(env, use_registry):
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    stats = run_pipeline(dataclasses.replace(env), PipelineOptions(write_report=False))
    assert isinstance(stats, IngestStats)
    assert stats.documents_fetched == 1
    assert stats.run_id == "run-test-0001"


def test_options_dry_run_overrides_settings(settings, use_registry, tmp_path):
    settings = dataclasses.replace(
        settings,
        dry_run=False,
        neo4j_uri="neo4j+s://db.example.test:7687",
        neo4j_password="secret",
        state_dir=str(tmp_path / "state"),
        report_dir=str(tmp_path / "reports"),
        sources_file=str(tmp_path / "none.yaml"),
    )
    use_registry(stub_spec("news_world", documents=[document("news_world", "https://example.test/a", text="Gazprom owns Rosneft.")]))
    pipeline = IngestPipeline(settings=settings, options=PipelineOptions(dry_run=True))
    assert pipeline.settings.dry_run is True
    stats = pipeline.run()
    assert stats.documents_fetched == 1


# --------------------------------------------------------------------------- #
# Calculated layer: PUPPET_MASTER_OF scoring runs after the harvest
# --------------------------------------------------------------------------- #


def archetype_document() -> Document:
    """The pattern the scorer exists for: a sanctioned person behind a shell chain.

    Person → BVI shell → Panama shell, one shared address with an associate who
    also flies on the same jet, and an OFAC sanctions edge for context.
    """
    person = Entity(name="Ivan Volkov", entity_type=EntityType.PERSON, confidence=1.0)
    associate = Entity(name="Pyotr Sokolov", entity_type=EntityType.PERSON, confidence=1.0)
    ofac = Entity(name="Office of Foreign Assets Control", entity_type=EntityType.ORGANIZATION, confidence=1.0)
    shell = Entity(
        name="Nordgate Holdings Ltd",
        entity_type=EntityType.ORGANIZATION,
        confidence=1.0,
        properties={"jurisdiction": "vg", "current_status": "dormant", "registered_address": "Craigmuir Chambers, Road Town, Tortola"},
    )
    subshell = Entity(
        name="Meridian Trading SA",
        entity_type=EntityType.ORGANIZATION,
        confidence=1.0,
        properties={"jurisdiction": "pa", "current_status": "dissolved"},
    )
    place = Entity(
        name="Craigmuir Chambers, Road Town, Tortola",
        entity_type=EntityType.LOCATION,
        confidence=1.0,
        properties={"address": "Craigmuir Chambers, Road Town, Tortola", "jurisdiction": "vg"},
    )
    jet = Entity(
        name="N707WA",
        entity_type=EntityType.CRAFT,
        confidence=1.0,
        properties={"craft_kind": "Aircraft", "tail_number": "N707WA", "registration": "N707WA"},
    )

    def edge(subject, predicate, obj, *, evidence="", extra=None) -> Relation:
        return build_relation(
            subject,
            predicate,
            obj,
            source_id="icij",
            source_weight=1.0,
            method=ExtractionMethod.STRUCTURED,
            evidence=evidence or f"{subject.name} {predicate.value} {obj.name}",
            extra=extra,
        )

    return document(
        "icij",
        "https://example.test/offshore/structure",
        title="Offshore structure",
        entities=[person, associate, ofac, shell, subshell, place, jet],
        relations=[
            edge(person, RelationType.SANCTIONED_BY, ofac),
            edge(person, RelationType.OWNS, shell, extra={"weight": 1.0}),
            edge(shell, RelationType.OWNS, subshell, extra={"weight": 1.0}),
            edge(subshell, RelationType.LOCATED_IN, place),
            edge(person, RelationType.SHARES_ADDRESS, associate, extra={"weight": 0.8}),
            edge(person, RelationType.PASSENGER_ON, jet, extra={"weight": 0.8}),
            edge(associate, RelationType.PASSENGER_ON, jet, extra={"weight": 0.8}),
        ],
    )


def archetype_registry(use_registry) -> None:
    use_registry(stub_spec("icij", SourceType.STRUCTURED, documents=[archetype_document()]))


def test_analytics_scores_the_harvest_and_writes_the_calculated_layer(env, use_registry):
    archetype_registry(use_registry)
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert stats.errors == [], "scoring must not add errors to a clean run"
    analytics = pipeline._analytics
    assert analytics["status"] == "completed"
    assert analytics["persons_scored"] >= 1
    assert analytics["nodes_considered"] >= 6
    assert analytics["edges_considered"] >= 7
    assert analytics["edges_written"] >= 1, "the archetype must clear the puppet-master threshold"
    assert analytics["top_persons"][0]["name"] == "Ivan Volkov"
    assert analytics["top_persons"][0]["risk_score"] >= env.puppet_master_min_score
    assert analytics["top_persons"][0]["components"], "scores must be explainable"
    assert analytics["top_persons"][0]["reasons"]

    kinds = pipeline.neo4j.recorder.summary()["by_kind"]
    # The calculated layer is recorded under its own kind, so an operator reading
    # a dry run can tell harvested writes from computed ones.
    assert kinds.get("analytics", 0) == 2, "one PUPPET_MASTER_OF batch + one risk-score batch"
    assert stats.per_source["analytics"]["puppet_master_edges"] >= 1
    assert stats.per_source["analytics"]["persons_scored"] >= 1


def test_analytics_runs_after_the_harvest_is_written(env, use_registry):
    """Ordering matters: the calculated layer reads back what the run wrote."""
    archetype_registry(use_registry)
    pipeline = make_pipeline(env)
    pipeline.run()

    kinds = [entry["kind"] for entry in pipeline.neo4j.recorder.statements]
    assert "write" in kinds and "analytics" in kinds
    assert min(i for i, kind in enumerate(kinds) if kind == "analytics") > max(i for i, kind in enumerate(kinds) if kind == "write")


def test_a_low_signal_harvest_produces_no_puppet_masters(env, use_registry):
    """Two legitimate directorships are not a puppeteering network."""
    person = Entity(name="Anne Director", entity_type=EntityType.PERSON, confidence=1.0)
    company = Entity(
        name="Rolls-Royce plc",
        entity_type=EntityType.ORGANIZATION,
        confidence=1.0,
        properties={"jurisdiction": "gb", "reg_number": "00710072", "website": "https://www.rolls-royce.com"},
    )
    relation = build_relation(
        person, RelationType.DIRECTOR_OF, company, source_id="opencorporates", source_weight=0.9, evidence="directorship"
    )
    use_registry(
        stub_spec(
            "opencorporates",
            SourceType.STRUCTURED,
            documents=[document("opencorporates", "https://example.test/officer/1", entities=[person, company], relations=[relation])],
        )
    )
    pipeline = make_pipeline(env)
    pipeline.run()

    analytics = pipeline._analytics
    assert analytics["status"] == "completed"
    assert analytics["persons_scored"] >= 1
    assert analytics["edges_written"] == 0, "a single real directorship must not be flagged"
    assert analytics["top_persons"][0]["risk_score"] < env.puppet_master_min_score


def test_analytics_can_be_switched_off(env, use_registry):
    archetype_registry(use_registry)
    env.analytics_enabled = False
    pipeline = make_pipeline(env)
    pipeline.run()

    assert pipeline._analytics == {"status": "disabled"}
    assert "analytics" not in pipeline.neo4j.recorder.summary()["by_kind"]


def test_analytics_failure_never_fails_the_run(env, use_registry, monkeypatch):
    """A scoring bug must not discard a completed harvest."""

    class Broken:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("scoring exploded")

    archetype_registry(use_registry)
    monkeypatch.setattr(pipeline_module, "AnalyticsEngine", Broken)
    pipeline = make_pipeline(env)
    stats = pipeline.run()

    assert pipeline._analytics["status"] == "failed"
    assert "scoring exploded" in pipeline._analytics["error"]
    assert any("analytics" in error for error in stats.errors)
    assert stats.documents_fetched == 1, "the harvest itself still completed"
    assert stats.entities_written >= 5, "and was still written to the graph"


def test_analytics_survives_a_graph_that_goes_away_mid_run(env, use_registry, monkeypatch):
    archetype_registry(use_registry)
    pipeline = make_pipeline(env)

    real_run = pipeline.run

    def run_then_break():
        monkeypatch.setattr(pipeline_module.AnalyticsEngine, "write", lambda self, result: (_ for _ in ()).throw(Neo4jUnavailable("gone")))
        return real_run()

    stats = run_then_break()
    assert pipeline._analytics["status"] == "failed"
    assert stats.documents_fetched == 1


def test_the_analytics_section_is_serialised_into_the_report(env, use_registry, tmp_path):
    archetype_registry(use_registry)
    make_pipeline(env).run()

    payload = json.loads((tmp_path / "reports" / "run-test-0001.json").read_text(encoding="utf-8"))
    assert payload["analytics"]["status"] == "completed"
    assert payload["analytics"]["persons_scored"] >= 1
    assert payload["analytics"]["top_persons"][0]["targets"], "edges carry the evidence chain that justified them"


# --------------------------------------------------------------------------- #
# Scheduled tiers
# --------------------------------------------------------------------------- #


def test_every_source_belongs_to_a_scheduled_tier(use_registry):
    """A source needs an explicit cadence, and the tiers must cover the registry.

    A source that is in neither the hourly nor the daily set is only reachable by a
    manual run, which means it is effectively never harvested: the weekly deep run
    would find it, seven days after it should have been seen. The default cadence is
    DAILY precisely so a new source lands in a run that happens, but this asserts the
    property instead of trusting the default.
    """
    from puppetnet.sources.registry import specs_for_tier

    hourly = {spec.id for spec in specs_for_tier("hourly")}
    daily = {spec.id for spec in specs_for_tier("daily")}
    everything = {spec.id for spec in specs_for_tier("all")}

    assert hourly, "the hourly tier cannot be empty"
    assert daily, "the daily tier cannot be empty"
    assert hourly & daily == set(), "a source belongs to one tier, not two"
    assert hourly | daily == everything, f"unreachable source(s): {sorted(everything - (hourly | daily))}"
    assert specs_for_tier("weekly") == specs_for_tier("all"), (
        "the weekly deep run revisits every source — that is what makes it deep"
    )


def test_a_tier_selects_its_sources_and_refuses_an_unknown_one(use_registry):
    from puppetnet.sources.registry import specs_for_tier

    with pytest.raises(ValueError):
        specs_for_tier("houry")


def test_the_pipeline_harvests_only_the_tier_it_was_given(env, use_registry):
    hourly = dataclasses.replace(stub_spec("news_world"), cadence=Cadence.HOURLY)
    daily = dataclasses.replace(stub_spec("icij", SourceType.STRUCTURED), cadence=Cadence.DAILY)
    use_registry(hourly, daily)

    tiered = make_pipeline(env, PipelineOptions(tier="hourly"))
    assert [spec.id for spec in tiered._resolve_specs()] == ["news_world"]

    deep = make_pipeline(env, PipelineOptions(tier="weekly"))
    assert [spec.id for spec in deep._resolve_specs()] == ["icij", "news_world"]


def test_naming_a_source_outside_the_tier_does_not_widen_the_run(env, use_registry):
    """The tier is the outer bound: naming a register in an hourly run harvests nothing."""
    hourly = dataclasses.replace(stub_spec("news_world"), cadence=Cadence.HOURLY)
    daily = dataclasses.replace(stub_spec("icij", SourceType.STRUCTURED), cadence=Cadence.DAILY)
    use_registry(hourly, daily)

    pipeline = make_pipeline(env, PipelineOptions(tier="hourly", sources=["icij"]))
    assert pipeline._resolve_specs() == [], (
        "an hourly run that names a daily source must come up empty and say so, "
        "not quietly harvest the register 24 times a day"
    )

    manual = make_pipeline(env, PipelineOptions(sources=["icij"]))
    assert [spec.id for spec in manual._resolve_specs()] == ["icij"], "--tier all reaches it"
