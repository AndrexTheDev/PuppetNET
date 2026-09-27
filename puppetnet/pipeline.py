"""End-to-end ingest pipeline: harvest → parse → NLP → weight → Neo4j.

Run order
---------
1. Resolve configuration + source registry (YAML/env overlays applied).
2. Open Neo4j, verify connectivity (waking an Aura free-tier instance) and
   ensure constraints/indexes exist.
3. Load the dedupe index (recent content hashes from the graph, plus a local
   state file so a dry run still dedupes).
4. Build the polite HTTP client (Cloudflare relay first, token-bucket fallback).
5. For each enabled source, in registry order (structured first):
      a. run the adapter, which yields :class:`Document` objects;
      b. structured documents already carry entities/relations;
         unstructured documents go through :class:`NLPEngine`;
      c. flush that source's slice to Neo4j (MERGE-based, idempotent);
      d. update run counters.
6. Close the run node with final statistics and write a JSON + Markdown report.

A failing source never aborts the run — it is counted, logged and skipped,
which is the behaviour you want from an unattended daily cron job.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, load_settings, resolve_source_specs
from .graph.analytics import AnalyticsEngine
from .graph.neo4j_client import Neo4jClient, Neo4jUnavailable
from .graph.resolver import EntityResolver
from .graph.writer import GraphWriter, WriteSummary, summarise_relations
from .logging_utils import banner, configure_logging, get_logger, timed
from .models import (
    Document,
    Entity,
    ExtractionMethod,
    IngestStats,
    Relation,
    SourceSpec,
    SourceType,
    iso,
    utcnow,
)
from .net.headers import HeaderFactory, parse_extra_headers
from .net.proxy_client import FetchClient
from .net.token_bucket import DelayQueue
from .parsing.nlp_engine import NLPEngine, ParseResult
from .sources import AdapterContext, AdapterError, create_adapter
from .sources.registry import SOURCE_REGISTRY, specs_for_tier

__all__ = ["IngestPipeline", "PipelineOptions", "run_pipeline", "RunReport"]

logger = get_logger("pipeline")


@dataclass
class PipelineOptions:
    """CLI-level knobs (override ``Settings`` for a single invocation)."""

    sources: list[str] = field(default_factory=list)
    #: Scheduled tier to harvest (``hourly`` / ``daily`` / ``weekly`` / ``all``).
    #: An explicit ``sources`` list wins: naming a source is the more specific
    #: instruction, and a workflow that lists sources on a tiered schedule means
    #: those sources, not the tier's set.
    tier: str = ""
    limit_per_source: int | None = None
    dry_run: bool | None = None
    skip_nlp: bool = False
    skip_graph: bool = False
    flush_after_each_source: bool = True
    write_report: bool = True
    report_dir: str = ""
    structured_first: bool = True
    fail_on_error: bool | None = None


@dataclass
class RunReport:
    """Serialisable summary of one run."""

    run_id: str
    stats: IngestStats
    sources: list[dict[str, Any]]
    nlp: dict[str, Any]
    graph: dict[str, Any]
    network: dict[str, Any]
    relations_by_type: dict[str, Any]
    #: The calculated layer: PUPPET_MASTER_OF scoring for this run.
    analytics: dict[str, Any] = field(default_factory=dict)
    report_path: str = ""
    markdown_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": iso(utcnow()),
            "stats": self.stats.to_dict(),
            "sources": self.sources,
            "nlp": self.nlp,
            "graph": self.graph,
            "network": self.network,
            "relations_by_type": self.relations_by_type,
            "analytics": self.analytics,
            "report_path": self.report_path,
            "markdown_path": self.markdown_path,
        }


class IngestPipeline:
    """The daily harvest."""

    def __init__(self, settings: Settings | None = None, options: PipelineOptions | None = None) -> None:
        self.options = options or PipelineOptions()
        if self.options.dry_run is not None:
            os.environ["DRY_RUN"] = "true" if self.options.dry_run else "false"
        self.settings = settings or load_settings()
        if self.options.dry_run:
            self.settings.dry_run = True
        if self.options.report_dir:
            self.settings.report_dir = self.options.report_dir
        if self.options.fail_on_error is not None:
            self.settings.fail_on_error = self.options.fail_on_error

        self.stats = IngestStats()
        self.run_id = self.settings.run_id or f"run-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        self.settings.run_id = self.run_id
        self.stats.run_id = self.run_id
        self.logger = get_logger("pipeline")

        self.neo4j: Neo4jClient | None = None
        self.writer: GraphWriter | None = None
        self.resolver = EntityResolver(limit=int(getattr(self.settings, "entity_resolver_limit", 200_000)))
        self.fetch: FetchClient | None = None
        self.nlp: NLPEngine | None = None
        self.deadline = time.monotonic() + float(self.settings.max_runtime_seconds)
        self._interrupted = False
        self._source_reports: list[dict[str, Any]] = []
        self._relation_pool: list[Relation] = []
        self._relation_pool_cap = 50_000
        self._cumulative_summary = WriteSummary()
        self._analytics: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self) -> IngestStats:
        """Execute the whole harvest and return run statistics."""
        self._install_signal_handlers()
        started = time.perf_counter()
        self.logger.info(banner(f"PuppetNET ingest {self.run_id}"))
        self.logger.info(
            "config: dry_run=%s worker=%s neo4j=%s sources=%s budget=%ss",
            self.settings.dry_run,
            self.settings.worker_url or "disabled",
            self.settings.neo4j_uri,
            ",".join(self.options.sources) or "all",
            self.settings.max_runtime_seconds,
        )

        status = "completed"
        try:
            self._init_graph()
            self._init_network()
            specs = self._resolve_specs()
            if not specs:
                self.logger.warning("no sources enabled — nothing to do")
                self.stats.record_error("pipeline", "no enabled sources")
                status = "no_sources"
            else:
                self._init_nlp(specs)
                known_hashes = self._load_dedupe_index()
                self._harvest(specs, known_hashes)
                self._flush_pending(final=True)
                self._analyse_graph()
        except Neo4jUnavailable as exc:
            status = "failed"
            self.logger.error("Neo4j unavailable — aborting run: %s", exc)
            self.stats.record_error("pipeline", f"neo4j: {exc}")
            raise
        except KeyboardInterrupt:  # pragma: no cover
            status = "interrupted"
            self.logger.warning("interrupted by operator")
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            self.logger.exception("pipeline failed: %s", exc)
            self.stats.record_error("pipeline", f"{exc.__class__.__name__}: {exc}")
            if self.settings.fail_on_error:
                raise
        finally:
            if self._interrupted:
                status = "interrupted"
            self.stats.finished_at = utcnow()
            self._close_run(status)
            report = self._build_report(status, started)
            if self.options.write_report:
                self._write_reports(report)
            self._cleanup()

        if self.settings.fail_on_error and self.stats.errors:
            raise SystemExit(f"run finished with {len(self.stats.errors)} recorded error(s)")
        return self.stats

    # ------------------------------------------------------------------ #
    # Initialisation
    # ------------------------------------------------------------------ #
    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            self._interrupted = True
            self.logger.warning("received signal %s — finishing the current source and stopping", signum)
            self.deadline = time.monotonic()  # force the budget check to trip

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            # signal() raises ValueError/OSError when this is not the main thread.
            with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                signal.signal(sig, handler)

    def _init_graph(self) -> None:
        if self.options.skip_graph:
            self.logger.info("graph writes skipped (--skip-graph)")
            self.neo4j = Neo4jClient(self.settings, dry_run=True)
        else:
            self.neo4j = Neo4jClient(self.settings, dry_run=self.settings.dry_run)
        self.neo4j.verify()
        self.writer = GraphWriter(self.neo4j, self.settings, stats=self.stats, resolver=self.resolver)
        self.writer.ensure_schema()
        self.writer.begin_run(
            self.run_id,
            extra={
                "backend": "pending",
                "max_documents_total": self.settings.max_documents_total,
                "worker_url": self.settings.worker_url,
                "nlp_models": ",".join(self.settings.spacy_models),
            },
        )

    def _init_network(self) -> None:
        delay_queue = DelayQueue(
            rate_per_sec=self.settings.token_bucket_rate_per_sec,
            burst=self.settings.token_bucket_burst,
            jitter_seconds=self.settings.token_bucket_jitter_seconds,
            global_rate_per_sec=1.0 / max(0.05, self.settings.global_min_interval_seconds),
            min_interval_seconds=self.settings.global_min_interval_seconds,
        )
        headers = HeaderFactory(
            bot_user_agent=self.settings.http_user_agent,
            run_salt=self.run_id,
            extra_headers=parse_extra_headers(self.settings.extra_headers_json),
        )
        self.fetch = FetchClient(
            self.settings,
            delay_queue=delay_queue,
            headers_factory=headers,
            stats=self.stats,
        )
        if self.settings.worker_configured:
            self.logger.info("edge relay enabled: %s", self.settings.worker_url)
        else:
            self.logger.warning(
                "edge relay not configured (PROXY_WORKER_URL/PROXY_AUTH_TOKEN missing) — "
                "using the direct token-bucket path only"
            )

    def _resolve_specs(self) -> list[SourceSpec]:
        requested = self.options.sources or self.settings.enabled_sources
        # The tier is the outer bound; an explicit source list (CLI or
        # ENABLED_SOURCES) narrows it *within* the tier. Deliberately the strict
        # direction: a scheduled hourly run that names a register by accident must
        # come up empty and say so, not quietly harvest the register 24 times a
        # day. `--tier all` (the default) is how you reach a source that belongs to
        # another tier.
        registry: tuple[SourceSpec, ...] = specs_for_tier(self.options.tier or None, SOURCE_REGISTRY)
        if self.options.tier:
            self.logger.info(
                "tier %s: %d source(s) — %s",
                self.options.tier, len(registry), ", ".join(spec.id for spec in registry),
            )
        if requested:
            wanted = {item.strip().lower() for item in requested}
            everything = tuple(SOURCE_REGISTRY)
            registry = tuple(spec for spec in registry if spec.id in wanted or spec.adapter in wanted)
            matched = {spec.id for spec in registry} | {spec.adapter for spec in registry}
            missing = wanted - matched
            outside = {item for item in missing if item in {spec.id for spec in everything}
                       or item in {spec.adapter for spec in everything}}
            unknown = missing - outside
            if outside and self.options.tier:
                self.logger.warning(
                    "source(s) %s are not harvested by the %s tier — use --tier all for a manual run",
                    ", ".join(sorted(outside)), self.options.tier,
                )
            if unknown:
                self.logger.warning("unknown source id(s) requested: %s", ", ".join(sorted(unknown)))
        specs = resolve_source_specs(self.settings, registry)

        # Apply environment-level source configuration (RSS_FEEDS, dataset URLs…).
        configured: list[SourceSpec] = []
        for spec in specs:
            if spec.adapter == "rss" and self.settings.rss_feeds and spec.id == "news_world":
                spec = spec.with_options(options={"feeds": list(self.settings.rss_feeds)})
            if spec.adapter == "icij" and self.settings.icij_dataset_urls:
                spec = spec.with_options(options={"dataset_urls": list(self.settings.icij_dataset_urls)})
            if spec.adapter == "wikidata" and self.settings.wikidata_queries:
                spec = spec.with_options(options={"queries": list(self.settings.wikidata_queries)})
            if spec.adapter == "register_files" and self.settings.register_files:
                spec = spec.with_options(options={"files": list(self.settings.register_files)})
            configured.append(spec)

        if self.options.structured_first:
            configured.sort(key=lambda s: (0 if s.kind is SourceType.STRUCTURED else 1, s.id))
        self.logger.info(
            "enabled sources (%d): %s",
            len(configured),
            ", ".join(f"{s.id}[{s.kind.value}:{s.confidence:.1f}]" for s in configured),
        )
        if self.writer is not None:
            self.writer.upsert_sources(configured)
        return configured

    def _init_nlp(self, specs: Sequence[SourceSpec]) -> None:
        needs_nlp = any(spec.kind is SourceType.UNSTRUCTURED for spec in specs)
        if self.options.skip_nlp:
            self.logger.info("NLP disabled (--skip-nlp); unstructured sources will produce documents only")
            return
        if not needs_nlp:
            self.logger.info("no unstructured sources enabled — skipping the spaCy load")
            return
        if not self.settings.nlp_enabled:
            self.logger.info("NLP_ENABLED=false — skipping the spaCy load")
            return
        with timed(self.logger, "spaCy pipeline load"):
            self.nlp = NLPEngine(self.settings)
        self.logger.info("NLP backend: %s", self.nlp.backend)
        if self.nlp.load_errors:
            if self.nlp.available:
                # No statistical model could be loaded, but the blank+gazetteer
                # backend is a supported degraded mode: it still extracts
                # entities and co-occurrence-priced triples. Recording that as a
                # run *error* would make --fail-on-error and the exit codes
                # punish an environment that is behaving exactly as designed, so
                # it is logged and surfaced in the report instead.
                for error in self.nlp.load_errors:
                    self.logger.warning("NLP degraded (falling back to %s): %s", self.nlp.backend, error)
            else:
                for error in self.nlp.load_errors:
                    self.stats.record_error("nlp", error)

    def _load_dedupe_index(self) -> set[str]:
        hashes: set[str] = set()
        if self.writer is not None and self.settings.only_new_documents:
            hashes |= self.writer.recent_content_hashes()
        hashes |= self._read_local_state()
        self.logger.info("dedupe index ready: %d known document hashes", len(hashes))
        return hashes

    # ------------------------------------------------------------------ #
    # Harvest
    # ------------------------------------------------------------------ #
    def _harvest(self, specs: Sequence[SourceSpec], known_hashes: set[str]) -> None:
        total_documents = 0
        for spec in specs:
            if self._interrupted or time.monotonic() >= self.deadline:
                self.logger.warning("stopping before %s: budget exhausted", spec.id)
                break
            if total_documents >= self.settings.max_documents_total:
                self.logger.warning("global document cap (%d) reached", self.settings.max_documents_total)
                break

            context = AdapterContext(
                settings=self.settings,
                client=self.fetch,
                stats=self.stats,
                spec=spec,
                run_id=self.run_id,
                known_hashes=set(known_hashes),
                nlp=self.nlp,
                deadline=self.deadline,
            )
            remaining = max(1, self.settings.max_documents_total - total_documents)
            limit = min(remaining, int(self.options.limit_per_source or spec.max_documents))
            source_started = time.perf_counter()
            documents: list[Document] = []
            entities: list[Entity] = []
            relations: list[Relation] = []
            error_message = ""

            try:
                adapter = create_adapter(spec, context)
                for document in adapter.run(limit=limit):
                    documents.append(document)
                    if document.entities:
                        entities.extend(document.entities)
                    if document.relations:
                        relations.extend(document.relations)
                    if spec.kind is SourceType.UNSTRUCTURED and self.nlp is not None:
                        parsed = self._parse_document(document, spec)
                        entities.extend(parsed.entities)
                        relations.extend(parsed.relations)
                        # The adapter already reported the (empty) structured
                        # payload of this document; NLP output is attributed to
                        # the same source so the run report stays truthful.
                        self.stats.bump_source(spec.id, "entities", len(parsed.entities))
                        self.stats.bump_source(spec.id, "relations", len(parsed.relations))
                    if len(documents) >= limit:
                        break
            except AdapterError as exc:
                error_message = str(exc)
            except Exception as exc:  # noqa: BLE001
                error_message = f"{exc.__class__.__name__}: {exc}"
                self.stats.bump_source(spec.id, "errors")
                self.stats.record_error(spec.id, error_message)
                self.logger.exception("source %s raised: %s", spec.id, exc)

            # Merge entities/relations produced by this source before writing.
            entities = self._merge_entities(entities)
            relations = self._merge_relations(relations)
            self.stats.entities_extracted += len(entities)
            self.stats.relations_extracted += len(relations)
            if len(self._relation_pool) < self._relation_pool_cap:
                self._relation_pool.extend(relations[: self._relation_pool_cap - len(self._relation_pool)])

            summary = self._flush_source(spec, documents, entities, relations)
            known_hashes.update(context.known_hashes)
            total_documents += len(documents)

            self._source_reports.append(
                {
                    "source_id": spec.id,
                    "adapter": spec.adapter,
                    "kind": spec.kind.value,
                    "confidence": spec.confidence,
                    "documents": len(documents),
                    "entities": len(entities),
                    "relations": len(relations),
                    "relations_written": summary.relations,
                    "entities_written": summary.entities,
                    "seconds": round(time.perf_counter() - source_started, 3),
                    "error": error_message or None,
                }
            )
            self.logger.info(
                "source %-18s docs=%-4d entities=%-5d relations=%-5d (%.1fs)%s",
                spec.id, len(documents), len(entities), len(relations),
                time.perf_counter() - source_started,
                f" ERROR: {error_message}" if error_message else "",
            )

    def _parse_document(self, document: Document, spec: SourceSpec) -> ParseResult:
        """Run the NLP engine over one unstructured document."""
        if self.nlp is None:  # an assert would vanish under `python -O`
            raise RuntimeError("the NLP engine is not initialised — cannot parse a document")
        if not document.text:
            self.logger.debug("document %s has no text — skipping NLP", document.doc_id)
            return ParseResult(doc_id=document.doc_id, backend=self.nlp.backend)
        try:
            result = self.nlp.parse_document(document)
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            self.stats.bump_source(spec.id, "errors")
            self.stats.record_error(spec.id, f"NLP failure on {document.doc_id}: {exc.__class__.__name__}: {exc}")
            self.logger.exception("NLP failed on %s", document.doc_id)
            return ParseResult(doc_id=document.doc_id, backend=self.nlp.backend, warnings=[f"nlp-error: {exc}"])

        self.stats.sentences_processed += result.sentences
        self.stats.characters_processed += result.characters
        self.stats.dependency_triples += result.dependency_triples
        # Pattern-extracted triples are priced at the co-occurrence rate (their
        # ExtractionMethod is COOCCURRENCE), so they belong in the same counter
        # — otherwise a model-less run reports "0 co-occurrence triples" next to
        # a non-zero relation count.
        self.stats.cooccurrence_triples += result.cooccurrence_triples + result.pattern_triples
        self.stats.craft_entities += result.craft_mentions
        self.stats.bump_source(spec.id, "sentences", result.sentences)
        if result.warnings:
            for warning in result.warnings[:3]:
                self.logger.debug("document %s: %s", document.doc_id, warning)
        return result

    # ------------------------------------------------------------------ #
    # Merge / flush
    # ------------------------------------------------------------------ #
    @staticmethod
    def _merge_entities(entities: Iterable[Entity]) -> list[Entity]:
        merged: dict[str, Entity] = {}
        for entity in entities:
            existing = merged.get(entity.canonical_key)
            if existing is None:
                merged[entity.canonical_key] = entity
            else:
                existing.merge(entity)
        return list(merged.values())

    @staticmethod
    def _merge_relations(relations: Iterable[Relation]) -> list[Relation]:
        from .models import noisy_or

        merged: dict[str, Relation] = {}
        for relation in relations:
            if relation.subject.canonical_key == relation.obj.canonical_key:
                continue
            key = relation.signature()
            existing = merged.get(key)
            if existing is None:
                merged[key] = relation
                continue
            existing.confidence = noisy_or(existing.confidence, relation.confidence)
            if relation.method is ExtractionMethod.DEPENDENCY and existing.method is ExtractionMethod.COOCCURRENCE:
                # A parsed triple supersedes a co-occurrence guess.
                existing.method = relation.method
                existing.verb = relation.verb or existing.verb
                existing.evidence = relation.evidence or existing.evidence
            existing.extra["merged_observations"] = int(existing.extra.get("merged_observations", 1)) + 1
        return list(merged.values())

    def _flush_source(self, spec: SourceSpec, documents: Sequence[Document], entities: Sequence[Entity], relations: Sequence[Relation]) -> WriteSummary:
        if self.writer is None:
            return WriteSummary()
        if not self.options.flush_after_each_source and not documents:
            return WriteSummary()
        with timed(self.logger, f"graph flush for {spec.id}"):
            summary = self.writer.persist(documents, entities, relations)
        self._accumulate_summary(summary)
        self._flush_local_state(documents)
        return summary

    def _accumulate_summary(self, summary: WriteSummary) -> None:
        """Roll a per-source write summary into the run total."""
        self._cumulative_summary.sources += summary.sources
        self._cumulative_summary.documents += summary.documents
        self._cumulative_summary.entities += summary.entities
        self._cumulative_summary.mentions += summary.mentions
        self._cumulative_summary.relations += summary.relations
        self._cumulative_summary.relations_dropped += summary.relations_dropped
        self._cumulative_summary.entities_capped += summary.entities_capped
        self._cumulative_summary.relations_capped += summary.relations_capped
        self._cumulative_summary.seconds += summary.seconds
        for key, value in summary.relations_by_type.items():
            self._cumulative_summary.relations_by_type[key] = self._cumulative_summary.relations_by_type.get(key, 0) + value
        for key, value in summary.entities_by_type.items():
            self._cumulative_summary.entities_by_type[key] = self._cumulative_summary.entities_by_type.get(key, 0) + value

    def _analyse_graph(self) -> None:
        """Score the graph and write the calculated ``PUPPET_MASTER_OF`` layer.

        This runs *after* the harvest is written, because influence scoring is
        only as good as the graph it reads: the engine folds this run's edges
        together with what Neo4j already holds (earlier runs, other pipelines),
        so a person whose shells were harvested last week still scores today.

        It is also strictly non-fatal. A scoring bug or a database that went
        away mid-run must not discard a completed harvest, so every failure is
        logged, recorded against the run and reported as ``status: failed`` in
        the analytics section of the report — the exit code stays whatever the
        harvest decided.
        """
        if not self.settings.analytics_enabled:
            self.logger.info("analytics disabled (ANALYTICS_ENABLED=false) — no calculated layer this run")
            self._analytics = {"status": "disabled"}
            return
        if self.neo4j is None:
            self._analytics = {"status": "skipped", "reason": "graph not initialised"}
            return

        self.logger.info(
            "analytics: scoring %d harvested edge(s) against the graph (min_score=%.2f, top_n=%d)",
            len(self._relation_pool),
            self.settings.puppet_master_min_score,
            self.settings.puppet_master_top_n,
        )
        try:
            engine = AnalyticsEngine(
                self.neo4j,
                self.settings,
                stats=self.stats,
                writer=self.writer,
                run_id=self.run_id,
            )
            result = engine.analyse(relations=self._relation_pool)
            edges, risk = engine.write(result)
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.logger.exception("analytics pass failed: %s", exc)
            self.stats.record_error("analytics", f"{exc.__class__.__name__}: {exc}")
            self._analytics = {"status": "failed", "error": f"{exc.__class__.__name__}: {exc}"[:400]}
            return

        summary = result.to_dict()
        summary["status"] = "completed"
        summary["edges_written"] = edges
        summary["risk_scores_written"] = risk
        self._analytics = summary
        self.stats.bump_source("analytics", "persons_scored", len(result.persons))
        self.stats.bump_source("analytics", "nodes_considered", result.nodes_considered)
        self.logger.info(
            "analytics: %d person(s) scored over %d node(s)/%d edge(s) → %d PUPPET_MASTER_OF, "
            "%d risk score(s), %d stale edge(s) pruned in %.1fs",
            len(result.persons),
            result.nodes_considered,
            result.edges_considered + result.graph_edges_read,
            edges,
            risk,
            result.pruned,
            result.seconds,
        )
        if result.persons:
            top = max(result.persons, key=lambda person: person.risk_score)
            self.logger.info(
                "analytics: highest score %.3f — %s (%d controlled entit%s)",
                top.risk_score,
                top.name,
                len(top.targets),
                "y" if len(top.targets) == 1 else "ies",
            )

    def _flush_pending(self, *, final: bool = False) -> None:
        """Nothing is buffered across sources today, but keep the hook explicit."""
        if final and self.writer is not None:
            self.logger.info(
                "final flush complete: %d entities, %d relations written",
                self.stats.entities_written, self.stats.relations_written,
            )

    # ------------------------------------------------------------------ #
    # Local state (secondary dedupe cache, survives a graph outage)
    # ------------------------------------------------------------------ #
    def _state_file(self) -> Path:
        return self.settings.state_path / "content_hashes.json"

    def _read_local_state(self) -> set[str]:
        path = self._state_file()
        if not path.exists():
            return set()
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            entries = payload.get("hashes") if isinstance(payload, dict) else payload
            return {str(item) for item in (entries or []) if item}
        except (OSError, ValueError) as exc:
            self.logger.warning("could not read local dedupe state %s: %s", path, exc)
            return set()

    def _flush_local_state(self, documents: Sequence[Document]) -> None:
        if not documents:
            return
        path = self._state_file()
        existing = self._read_local_state()
        existing.update(doc.content_hash for doc in documents if doc.content_hash)
        # Keep the file bounded: most recent 100k hashes.
        trimmed = sorted(existing)[-100_000:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump({"updated_at": iso(utcnow()), "count": len(trimmed), "hashes": trimmed}, handle)
        except OSError as exc:
            self.logger.warning("could not persist local dedupe state: %s", exc)

    # ------------------------------------------------------------------ #
    # Close-out
    # ------------------------------------------------------------------ #
    def _close_run(self, status: str) -> None:
        if self.writer is not None:
            try:
                self.writer.finish_run(status=status)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("could not close the run node: %s", exc)

    def _build_report(self, status: str, started: float) -> RunReport:
        nlp_info = self.nlp.describe() if self.nlp is not None else {"backend": "disabled", "has_parser": False}
        network_info = self.fetch.describe() if self.fetch is not None else {}
        graph_info = self.neo4j.describe() if self.neo4j is not None else {}
        graph_info["status"] = status
        graph_info["summary"] = self._cumulative_summary.to_dict()
        return RunReport(
            run_id=self.run_id,
            stats=self.stats,
            sources=self._source_reports,
            nlp=nlp_info,
            graph=graph_info,
            network=network_info,
            relations_by_type=summarise_relations(self._relation_pool),
            analytics=self._analytics,
        )

    def _write_reports(self, report: RunReport) -> None:
        directory = self.settings.report_path
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{report.run_id}.json"
        markdown_path = directory / f"{report.run_id}.md"
        try:
            with json_path.open("w", encoding="utf-8") as handle:
                json.dump(report.to_dict(), handle, indent=2, ensure_ascii=False, default=str)
            with markdown_path.open("w", encoding="utf-8") as handle:
                handle.write(report.stats.markdown_summary() + "\n")
            report.report_path = str(json_path)
            report.markdown_path = str(markdown_path)
            self.logger.info("run report written → %s", json_path)
        except OSError as exc:
            self.logger.warning("could not write the run report: %s", exc)
            return

        self._emit_github_outputs(report)

    @staticmethod
    def _emit_github_outputs(report: RunReport) -> None:
        """Publish the report to the GitHub Actions step summary + outputs."""
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a", encoding="utf-8") as handle:
                    handle.write(report.stats.markdown_summary() + "\n")
            except OSError as exc:
                logger.debug("could not append to GITHUB_STEP_SUMMARY: %s", exc)
        output_path = os.environ.get("GITHUB_OUTPUT")
        if output_path:
            try:
                with open(output_path, "a", encoding="utf-8") as handle:
                    handle.write(f"run_id={report.run_id}\n")
                    handle.write(f"report_path={report.report_path}\n")
                    handle.write(f"documents={report.stats.documents_fetched}\n")
                    handle.write(f"entities={report.stats.entities_written}\n")
                    handle.write(f"relations={report.stats.relations_written}\n")
                    handle.write(f"errors={len(report.stats.errors)}\n")
            except OSError as exc:
                logger.debug("could not write GITHUB_OUTPUT: %s", exc)

    def _cleanup(self) -> None:
        if self.fetch is not None:
            self.fetch.close()
        if self.neo4j is not None:
            self.neo4j.close()
        if self.nlp is not None:
            self.nlp.close()


def run_pipeline(settings: Settings | None = None, options: PipelineOptions | None = None) -> IngestStats:
    """Module-level convenience wrapper used by ``ingest.py``."""
    resolved = settings or load_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)
    pipeline = IngestPipeline(settings=resolved, options=options)
    return pipeline.run()
