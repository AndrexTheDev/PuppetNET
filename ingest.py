#!/usr/bin/env python3
"""PuppetNET daily data harvesting + NLP parsing engine — entry point.

Executed by ``.github/workflows/daily_ingest.yml`` on a cron schedule (once per
day) and equally usable by hand::

    python ingest.py --dry-run --sources news_world,aviation_news --limit 10
    python ingest.py --sources icij_leaks,opencorporates,wikidata
    python ingest.py --doctor          # validate config + connectivity, exit
    python ingest.py --list-sources    # print the source registry as JSON

Exit codes
----------
``0`` success · ``1`` configuration/deploy error · ``2`` runtime failure ·
``3`` partial success (some sources failed but the graph was written).

The three stages
----------------
``fetch → parse/NLP → graph``. This file owns the first stage's entry points:
one named fetcher per target API, each returning :class:`~puppetnet.models.Document`
objects that the spaCy stage and the Neo4j writer consume downstream. Nothing
here touches a model or a database, so a fetcher can be run on its own::

    python ingest.py --fetch-only wikidata,opencorporates   # no spaCy, no Neo4j

=================================  ==========================================  ======
Fetcher                            Target API                                  weight
=================================  ==========================================  ======
:func:`fetch_icij`                 ``offshoreleaks.icij.org`` + the TSV/ZIP     1.0
                                   data dumps (entities, officers, addresses)
:func:`fetch_faa_registry`         ``registry.faa.gov`` Releasable Aircraft     1.0
                                   dump (N-number → registrant → address)
:func:`fetch_wikidata`             ``query.wikidata.org/sparql`` — ownership,   0.9
                                   parent/subsidiary, board members, foundation
                                   trustees, political positions, operators
:func:`fetch_opencorporates`       ``api.opencorporates.com`` — directorships,  0.9
                                   parent/subsidiary, shared registered offices
:func:`fetch_adsb_exchange`        ``adsbdb.com`` / ADS-B Exchange v2 on        0.8
                                   RapidAPI — tail → registered owner
:func:`fetch_flight_logs`          passenger manifests (CSV/text/PDF) →         0.8
                                   ``PASSENGER_ON``
:func:`fetch_news`                 OCCRP, ICIJ, Global Witness, Transparency    0.4
                                   International and world/aviation RSS →
                                   spaCy co-occurrence (``MENTIONED_WITH``)
=================================  ==========================================  ======

The weight column is the per-source confidence multiplier every edge from that
fetcher carries; it is declared on the registry spec, not hard-coded here.

Environment
-----------
All configuration comes from the environment (see ``.env.example``):
``NEO4J_URI``, ``NEO4J_USERNAME``, ``NEO4J_PASSWORD``, ``PROXY_WORKER_URL``,
``PROXY_AUTH_TOKEN``, ``OPENCORPORATES_API_TOKEN``, ``COMPANIES_HOUSE_API_KEY``,
``WIKIDATA_USER_AGENT``, ``ADSBEXCHANGE_API_KEY``, ``AIRCRAFT_TAIL_NUMBERS``,
``FLIGHT_LOG_URLS``, ``SPACY_MODELS``, ``ENABLED_SOURCES``, ``DRY_RUN`` …
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import traceback
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow running from a source checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puppetnet.config import ConfigError, Settings, load_settings, resolve_source_specs  # noqa: E402
from puppetnet.logging_utils import banner, configure_logging, get_logger  # noqa: E402
from puppetnet.models import Document, IngestStats  # noqa: E402
from puppetnet.net.proxy_client import FetchClient  # noqa: E402
from puppetnet.pipeline import IngestPipeline, PipelineOptions  # noqa: E402
from puppetnet.sources import ADAPTERS, AdapterContext, AdapterError, create_adapter  # noqa: E402
from puppetnet.sources.registry import SOURCE_REGISTRY, describe_registry  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2
EXIT_PARTIAL = 3

logger = get_logger("ingest")


# --------------------------------------------------------------------------- #
# Stage 1 — modular API fetchers
# --------------------------------------------------------------------------- #
# One named entry point per target API, each returning Documents that the spaCy
# stage and the Neo4j writer consume later. Nothing in this section loads a
# model or opens a database connection.
#
# The fetchers are deliberately thin. An adapter owns the parsing and the domain
# mapping (which predicate, which node label, which properties, which evidence
# string); a fetcher owns "run just this API and hand me the documents". Both go
# through :meth:`SourceAdapter.run`, so the time budget, the dedupe window, the
# per-host politeness policy and the error accounting behave exactly as they do
# in a full daily run — there is no second harvesting implementation to drift.
#
# Every fetcher in a session shares one :class:`FetchClient`, which is what makes
# the rate limiting real: the Cloudflare relay, the token bucket and the
# deferred-task queue all key on host, so asking for Wikidata and OpenCorporates
# in one command still respects both hosts' limits.


@dataclass
class FetcherSession:
    """Settings + one shared HTTP client + run bookkeeping for a fetch stage.

    Use it as a context manager so the client's session is closed::

        with FetcherSession.create(settings) as session:
            documents = fetch_wikidata(session, limit=25)
    """

    settings: Settings
    client: Any
    stats: IngestStats = field(default_factory=IngestStats)
    run_id: str = ""
    deadline: float = 0.0
    known_hashes: set[str] = field(default_factory=set)
    nlp: Any = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        client: Any = None,
        run_id: str = "",
        max_runtime_seconds: float | None = None,
        known_hashes: Iterable[str] | None = None,
        nlp: Any = None,
    ) -> FetcherSession:
        resolved_id = run_id or settings.run_id or f"fetch-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        budget = float(max_runtime_seconds if max_runtime_seconds is not None else settings.max_runtime_seconds)
        session = cls(
            settings=settings,
            client=client if client is not None else FetchClient(settings),
            stats=IngestStats(run_id=resolved_id),
            run_id=resolved_id,
            deadline=time.monotonic() + budget if budget > 0 else 0.0,
            known_hashes=set(known_hashes or ()),
            nlp=nlp,
        )
        session.settings.run_id = resolved_id
        return session

    # ------------------------------------------------------------------ #
    def specs(self) -> tuple[Any, ...]:
        """Enabled registry specs, with the YAML/env overlay applied."""
        return resolve_source_specs(self.settings, SOURCE_REGISTRY)

    def spec_for(self, source: str) -> Any | None:
        """Find an enabled spec by id or adapter name (``wikidata``, ``rss``, …)."""
        wanted = str(source or "").strip().lower()
        if not wanted:
            return None
        for spec in self.specs():
            if spec.id == wanted or spec.adapter == wanted:
                return spec
        return None

    def context(self, spec: Any) -> AdapterContext:
        """Bind a spec to this session's client, stats and budget."""
        return AdapterContext(
            settings=self.settings,
            client=self.client,
            stats=self.stats,
            spec=spec,
            run_id=self.run_id,
            known_hashes=self.known_hashes,
            nlp=self.nlp,
            deadline=self.deadline,
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    def __enter__(self) -> FetcherSession:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def fetch_source(
    session: FetcherSession,
    source: str,
    *,
    limit: int | None = None,
    options: Mapping[str, Any] | None = None,
) -> list[Document]:
    """Run one source's adapter and return its documents.

    ``source`` is a registry id (``wikidata``) or an adapter name (``rss``);
    ``options`` are merged over the spec's own options for this call only, which
    is how a fetcher narrows a query set or a tail-number watchlist without
    editing ``config/sources.yaml``.

    An unknown or disabled source is reported and yields nothing — a typo in a
    ``--fetch-only`` list must not silently harvest the whole registry.
    """
    spec = session.spec_for(source)
    if spec is None:
        known = ", ".join(sorted({s.id for s in session.specs()}))
        logger.warning("source %r is not enabled (available: %s)", source, known or "none")
        session.stats.record_error("fetch", f"unknown or disabled source: {source}")
        return []
    if options:
        spec = spec.with_options(**options)

    adapter = create_adapter(spec, session.context(spec))
    logger.info(
        "fetching %s (%s, weight=%.2f)%s",
        spec.id,
        adapter.__class__.__name__,
        adapter.source_weight,
        f" limit={limit}" if limit else "",
    )
    try:
        documents = list(adapter.run(limit=limit))
    except AdapterError as exc:
        logger.error("fetcher %s failed: %s", spec.id, exc)
        return []
    logger.info("fetched %d document(s) from %s", len(documents), spec.id)
    return documents


def fetch_icij(session: FetcherSession, *, limit: int | None = None, dataset_urls: Iterable[str] = (), node_ids: Iterable[str] = (), search_terms: Iterable[str] = ()) -> list[Document]:
    """ICIJ Offshore Leaks — entities, officers, intermediaries and addresses.

    ``https://offshoreleaks.icij.org`` for the watchlist/search path, plus the
    TSV/ZIP data dumps (``ICIJ_DATASET_URLS``) which are streamed member by
    member. Edge weight **1.0**: this is the primary record. Produces
    ``OWNED_BY``, ``OFFICER_OF``, ``INTERMEDIARY_FOR``, ``LOCATED_IN`` and
    ``SHARES_ADDRESS``.
    """
    options: dict[str, Any] = {}
    if dataset_urls:
        options["dataset_urls"] = list(dataset_urls)
    if node_ids:
        options["node_ids"] = list(node_ids)
    if search_terms:
        options["search_terms"] = list(search_terms)
    return fetch_source(session, "icij_leaks", limit=limit, options=options)


def fetch_wikidata(session: FetcherSession, *, limit: int | None = None, queries: Iterable[str] = (), limit_per_query: int | None = None) -> list[Document]:
    """Wikidata SPARQL — ``https://query.wikidata.org/sparql``.

    Ownership (P127), parent/subsidiary (P749/P355), board members (P3320),
    foundation trustees and officers (P169/P576/P488), operators of aircraft and
    vessels (P137) and political positions (P102/P39). Edge weight **0.9** —
    structured, but crowdsourced and only as current as its last edit.

    ``queries`` narrows the query set for this call (see
    ``puppetnet.sources.wikidata.WIKIDATA_QUERIES``).
    """
    options: dict[str, Any] = {}
    if queries:
        options["queries"] = list(queries)
    if limit_per_query:
        options["limit_per_query"] = int(limit_per_query)
    return fetch_source(session, "wikidata", limit=limit, options=options)


def fetch_opencorporates(session: FetcherSession, *, limit: int | None = None, queries: Iterable[str] = (), jurisdictions: Iterable[str] = (), include_officers: bool | None = None) -> list[Document]:
    """OpenCorporates — ``https://api.opencorporates.com``.

    Directorships, parent/subsidiary groupings and shared registered addresses
    across 140+ registries. Edge weight **0.9**: an aggregator over official
    registers, so structured but second-hand. Send ``OPENCORPORATES_API_TOKEN``
    to lift the anonymous rate limit.
    """
    options: dict[str, Any] = {}
    if queries:
        options["queries"] = list(queries)
    if jurisdictions:
        options["jurisdictions"] = list(jurisdictions)
    if include_officers is not None:
        options["include_officers"] = bool(include_officers)
    return fetch_source(session, "opencorporates", limit=limit, options=options)


def fetch_faa_registry(session: FetcherSession, *, limit: int | None = None, dump_url: str = "", tail_numbers: Iterable[str] = (), registrant_names: Iterable[str] = (), states: Iterable[str] = (), force_refresh: bool = False) -> list[Document]:
    """FAA aircraft registry — ``https://registry.faa.gov`` Releasable Aircraft.

    Every US-registered N-number with its registrant and address, streamed from
    the monthly ZIP. Edge weight **1.0** (an official register). Produces
    ``(:Person|:Company)-[:OWNS]->(:Aircraft {tail_number, owner})`` plus
    ``(:Person)-[:SHARES_ADDRESS]->(:Person)`` for co-registrants, and refreshes
    at most once per ``refresh_days`` unless ``force_refresh`` is set.
    """
    options: dict[str, Any] = {}
    if dump_url:
        options["dump_url"] = dump_url
    if tail_numbers:
        options["tail_numbers"] = list(tail_numbers)
    if registrant_names:
        options["registrant_names"] = list(registrant_names)
    if states:
        options["states"] = list(states)
    if force_refresh:
        options["force_refresh"] = True
    return fetch_source(session, "faa_registry", limit=limit, options=options)


def fetch_adsb_exchange(session: FetcherSession, *, limit: int | None = None, tail_numbers: Iterable[str] = (), callsigns: Iterable[str] = ()) -> list[Document]:
    """ADS-B Exchange / adsbdb — per-tail aircraft enrichment.

    ``adsbdb.com`` by default; ``adsbexchange-com1.p.rapidapi.com`` (ADS-B
    Exchange v2 on RapidAPI) when ``ADSBEXCHANGE_API_KEY`` is set. Edge weight
    **0.8**: telemetry and registration lookups are aggregated and self-reported.
    Produces ``OWNS``, ``REGISTERED_TO`` and ``REGISTERED_IN``.

    With no watchlist (``AIRCRAFT_TAIL_NUMBERS``) this makes no requests at all.
    """
    options: dict[str, Any] = {}
    if tail_numbers:
        options["tail_numbers"] = list(tail_numbers)
    if callsigns:
        options["callsigns"] = list(callsigns)
    return fetch_source(session, "adsb_exchange", limit=limit, options=options)


def fetch_flight_logs(session: FetcherSession, *, limit: int | None = None, urls: Iterable[str] = (), default_tail_numbers: Iterable[str] = ()) -> list[Document]:
    """Flight logs and passenger manifests → ``PASSENGER_ON``.

    Court exhibits, FOIA dumps and operator logs in CSV, text or PDF. Edge
    weight **0.8**. A tabular manifest is parsed row by row into
    ``(:Person)-[:PASSENGER_ON]->(:Aircraft)`` plus ``TRAVELED_WITH`` between
    co-passengers; anything unstructured is passed through as text for the NLP
    stage rather than guessed at with a regex.
    """
    options: dict[str, Any] = {}
    if urls:
        options["flight_log_urls"] = list(urls)
    if default_tail_numbers:
        options["default_tail_numbers"] = list(default_tail_numbers)
    return fetch_source(session, "flight_logs", limit=limit, options=options)


def fetch_news(session: FetcherSession, *, limit: int | None = None, feeds: Iterable[str] = ()) -> list[Document]:
    """Unstructured news/RSS → spaCy co-occurrence downstream.

    OCCRP, ICIJ stories, Global Witness, Transparency International and the
    world/aviation feeds. Edge weight **0.4**, and co-occurrence-only triples
    take a further 0.2 penalty — a mention in a headline is not a relationship.
    """
    options: dict[str, Any] = {"feeds": list(feeds)} if feeds else {}
    specs = [spec for spec in session.specs() if spec.adapter == "rss"]
    documents: list[Document] = []
    for spec in specs:
        documents.extend(fetch_source(session, spec.id, limit=limit, options=options))
    return documents


#: Fetcher name → callable, in the order a run should call them. Structured
#: sources first: they seed the resolver with canonical nodes that the news pass
#: then attaches to, which is what keeps a headline mention from inventing a
#: duplicate person.
FETCHERS: dict[str, Any] = {
    "icij_leaks": fetch_icij,
    "faa_registry": fetch_faa_registry,
    "wikidata": fetch_wikidata,
    "opencorporates": fetch_opencorporates,
    "adsb_exchange": fetch_adsb_exchange,
    "flight_logs": fetch_flight_logs,
    "news": fetch_news,
}


def harvest_documents(
    session: FetcherSession,
    sources: Iterable[str] = (),
    *,
    limit: int | None = None,
    structured_first: bool = True,
) -> Iterator[Document]:
    """Yield every document from the requested sources (default: all enabled).

    This is stage 1 of the pipeline as an independent, iterable function — the
    spaCy stage and the graph writer consume it later, and ``--fetch-only``
    stops here.
    """
    requested = [str(item).strip().lower() for item in (sources or ()) if str(item).strip()]
    available = list(session.specs())
    if requested:
        specs: list[Any] = []
        for name in requested:
            # A name selects either one source id or every spec sharing an
            # adapter ("rss" → all six news feeds), in the order given.
            matches = [spec for spec in available if spec.id == name or spec.adapter == name]
            if not matches:
                known = ", ".join(sorted({spec.id for spec in available}))
                logger.warning("source %r is not enabled (available: %s)", name, known or "none")
                session.stats.record_error("fetch", f"unknown or disabled source: {name}")
                continue
            for match in matches:
                if match not in specs:
                    specs.append(match)
    else:
        specs = available

    if structured_first:
        specs.sort(key=lambda item: 0 if item.kind.confidence >= 1.0 else 1)

    for spec in specs:
        if session.deadline and time.monotonic() >= session.deadline:
            logger.warning("time budget exhausted — stopping the fetch stage before %s", spec.id)
            break
        yield from fetch_source(session, spec.id, limit=limit)


def summarise_fetch(documents: Sequence[Document], stats: IngestStats) -> dict[str, Any]:
    """Per-source counts for ``--fetch-only``: what stage 1 produced, nothing more."""
    per_source: dict[str, dict[str, int]] = {}
    predicates: dict[str, int] = {}
    for document in documents:
        bucket = per_source.setdefault(document.source_id, {"documents": 0, "entities": 0, "relations": 0, "words": 0})
        bucket["documents"] += 1
        bucket["entities"] += len(document.entities)
        bucket["relations"] += len(document.relations)
        bucket["words"] += len(document.text.split())
        for relation in document.relations:
            predicates[relation.rel_type] = predicates.get(relation.rel_type, 0) + 1
    return {
        "documents": len(documents),
        "entities": sum(bucket["entities"] for bucket in per_source.values()),
        "relations": sum(bucket["relations"] for bucket in per_source.values()),
        "per_source": per_source,
        "relations_by_type": dict(sorted(predicates.items(), key=lambda item: (-item[1], item[0]))),
        "errors": list(stats.errors),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="PuppetNET serverless OSINT harvester: fetch → parse → NLP → weighted graph writes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ingest.py --dry-run --limit 5 --sources news_world\n"
            "  python ingest.py --sources icij_leaks,opencorporates,wikidata\n"
            "  python ingest.py --fetch-only wikidata,opencorporates --limit 5\n"
            "  python ingest.py --doctor\n"
            "  python ingest.py --list-sources\n"
        ),
    )
    parser.add_argument("--sources", default="", help="Comma-separated source ids or adapter names (default: all enabled).")
    parser.add_argument("--limit", type=int, default=None, help="Maximum documents per source for this run.")
    parser.add_argument("--dry-run", action="store_true", help="Harvest + parse but write nothing to Neo4j.")
    parser.add_argument("--skip-nlp", action="store_true", help="Skip spaCy entirely (structured sources only).")
    parser.add_argument(
        "--fetch-only",
        nargs="?",
        const="__all__",
        default="",
        metavar="SOURCES",
        help=(
            "Run stage 1 in isolation: fetch the given sources (comma-separated ids or "
            "adapter names, default all enabled), print what came back, and touch neither "
            "spaCy nor Neo4j."
        ),
    )
    parser.add_argument("--skip-graph", action="store_true", help="Force the Neo4j client into dry-run mode.")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit non-zero if any source recorded an error.")
    parser.add_argument("--log-level", default="", help="DEBUG | INFO | WARNING | ERROR (default: LOG_LEVEL env).")
    parser.add_argument("--log-json", action="store_true", help="Emit structured JSON logs (set automatically by LOG_JSON).")
    parser.add_argument("--report-dir", default="", help="Where to write the JSON/Markdown run report.")
    parser.add_argument("--run-id", default="", help="Explicit run id (default: generated).")
    parser.add_argument("--max-runtime", type=int, default=None, help="Hard wall-clock budget in seconds.")
    parser.add_argument("--no-report", action="store_true", help="Do not write report files.")
    parser.add_argument("--doctor", action="store_true", help="Validate configuration, connectivity and model availability, then exit.")
    parser.add_argument("--list-sources", action="store_true", help="Print the source registry as JSON and exit.")
    parser.add_argument("--print-config", action="store_true", help="Print the resolved (redacted) configuration and exit.")
    parser.add_argument("--version", action="store_true", help="Print version information and exit.")
    return parser


def options_from_args(args: argparse.Namespace) -> PipelineOptions:
    sources = [item.strip().lower() for item in args.sources.replace(";", ",").split(",") if item.strip()]
    return PipelineOptions(
        sources=sources,
        limit_per_source=args.limit,
        dry_run=True if args.dry_run else None,
        skip_nlp=args.skip_nlp,
        skip_graph=args.skip_graph,
        write_report=not args.no_report,
        report_dir=args.report_dir,
        fail_on_error=True if args.fail_on_error else None,
    )


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.log_level:
        settings.log_level = args.log_level.upper()
    if args.log_json:
        settings.log_json = True
    if args.dry_run:
        settings.dry_run = True
    if args.report_dir:
        settings.report_dir = args.report_dir
    if args.run_id:
        settings.run_id = args.run_id
    if args.max_runtime:
        settings.max_runtime_seconds = max(60, int(args.max_runtime))
    if args.fail_on_error:
        settings.fail_on_error = True
    return settings


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def command_list_sources() -> int:
    payload = describe_registry()
    print(json.dumps({"count": len(payload), "sources": payload}, indent=2, ensure_ascii=False))
    return EXIT_OK


def command_fetch_only(settings: Settings, args: argparse.Namespace) -> int:
    """Run the fetcher stage alone and report what each API produced.

    Useful when tuning a source (does the SPARQL query still return rows? did
    the FAA dump change shape?) without paying for NLP or touching the graph.
    Nothing is written to Neo4j: the client is never constructed.
    """
    raw = "" if args.fetch_only == "__all__" else str(args.fetch_only)
    sources = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    settings.dry_run = True

    print(banner(f"fetch stage — {', '.join(sources) if sources else 'all enabled sources'}"))
    session = FetcherSession.create(settings, max_runtime_seconds=float(args.max_runtime or settings.max_runtime_seconds))
    documents: list[Document] = []
    status = EXIT_OK
    try:
        for document in harvest_documents(session, sources, limit=args.limit):
            documents.append(document)
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("interrupted — reporting what was fetched")
        status = EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001
        logger.error("fetch stage failed: %s", exc)
        logger.debug("%s", traceback.format_exc())
        session.stats.record_error("fetch", f"{exc.__class__.__name__}: {exc}")
        status = EXIT_RUNTIME
    finally:
        session.close()

    summary = summarise_fetch(documents, session.stats)
    for source_id, bucket in sorted(summary["per_source"].items()):
        print(
            f"  {source_id:<22} documents={bucket['documents']:<5} entities={bucket['entities']:<6} "
            f"relations={bucket['relations']:<6} words={bucket['words']}"
        )
    if summary["relations_by_type"]:
        print("\n  relations by predicate:")
        for predicate, count in list(summary["relations_by_type"].items())[:20]:
            print(f"    {predicate:<24} {count}")
    print(
        f"\n  total: {summary['documents']} document(s), {summary['entities']} entity(ies), "
        f"{summary['relations']} relation(s), {len(summary['errors'])} error(s)"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if summary["errors"]:
        for error in summary["errors"][:5]:
            print(f"::warning title=PuppetNET fetch::{error}")
        return EXIT_PARTIAL if status == EXIT_OK else status
    return status


def command_print_config(settings: Settings) -> int:
    print(json.dumps(settings.describe(), indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


def command_doctor(settings: Settings) -> int:
    """Validate every external dependency without performing a harvest."""
    results: list[dict[str, Any]] = []

    def record(component: str, ok: bool, detail: str) -> None:
        results.append({"component": component, "ok": ok, "detail": detail})
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {component}: {detail}")

    # 1. Configuration
    try:
        settings.validate()
        record("config", True, "environment configuration is valid")
    except ConfigError as exc:
        record("config", False, str(exc).replace("\n", " | "))

    # 2. Edge relay
    if settings.worker_configured:
        ok, detail = _probe_worker(settings)
        record("edge-relay", ok, detail)
    else:
        record(
            "edge-relay",
            settings.direct_fallback_enabled,
            "not configured (PROXY_WORKER_URL/PROXY_AUTH_TOKEN missing)"
            + (" — direct token-bucket fallback enabled" if settings.direct_fallback_enabled else " — NOTHING CAN FETCH"),
        )

    # 3. Neo4j
    if settings.neo4j_configured or settings.dry_run:
        ok, detail = _probe_neo4j(settings)
        record("neo4j", ok, detail)
    else:
        record("neo4j", False, "NEO4J_URI/NEO4J_PASSWORD missing (use --dry-run to skip)")

    # 4. spaCy
    ok, detail = _probe_spacy(settings)
    record("nlp", ok, detail)

    # 5. Source registry → adapter → fetcher wiring
    ok, detail = _probe_registry(settings)
    record("registry", ok, detail)

    # 6. Source credentials
    record("credentials", True, _credential_summary(settings))

    # 7. Writable paths
    try:
        settings.state_path.mkdir(parents=True, exist_ok=True)
        settings.report_path.mkdir(parents=True, exist_ok=True)
        probe = settings.report_path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("filesystem", True, f"state={settings.state_path} reports={settings.report_path}")
    except OSError as exc:
        record("filesystem", False, f"cannot write: {exc}")

    failures = [item for item in results if not item["ok"]]
    print(banner(f"doctor: {len(results) - len(failures)}/{len(results)} checks passed"))
    return EXIT_OK if not failures else EXIT_CONFIG


def _probe_registry(settings: Settings) -> tuple[bool, str]:
    """Every enabled spec must resolve to an adapter class, and vice versa.

    A spec naming an adapter that does not exist fails only when that source is
    harvested — hours into a cron run — so it is checked here instead.
    """
    try:
        specs = resolve_source_specs(settings, SOURCE_REGISTRY)
    except Exception as exc:  # noqa: BLE001
        return False, f"registry could not be resolved: {exc.__class__.__name__}: {exc}"

    broken = [f"{spec.id}→{spec.adapter}" for spec in specs if spec.adapter not in ADAPTERS]
    enabled = len(specs)
    fetchers = ", ".join(sorted(FETCHERS))
    detail = f"{enabled} enabled source(s) of {len(SOURCE_REGISTRY)} registered; fetchers: {fetchers}"
    if broken:
        return False, f"no adapter registered for: {', '.join(broken)} ({detail})"
    if not enabled:
        return False, f"no sources enabled ({detail})"
    return True, detail


def _probe_worker(settings: Settings) -> tuple[bool, str]:
    """Query ``/health`` on the Worker and report reachability + bindings."""
    import requests

    endpoint = f"{settings.worker_url.rstrip('/')}/health"
    try:
        response = requests.get(endpoint, timeout=15)
        payload = response.json() if response.content else {}
        bindings = payload.get("bindings", {})
        detail = (
            f"{endpoint} → HTTP {response.status_code} "
            f"worker={payload.get('worker')}@{payload.get('version')} colo={payload.get('colo')} "
            f"kv={bindings.get('rate_limit_kv')}/{bindings.get('result_kv')} queue={bindings.get('queue')} "
            f"auth_configured={bindings.get('auth_configured')}"
        )
        return response.ok, detail
    except Exception as exc:  # noqa: BLE001
        logger.debug("worker probe failed: %s", traceback.format_exc())
        return False, f"{endpoint} unreachable: {exc.__class__.__name__}: {exc}"


def _probe_neo4j(settings: Settings) -> tuple[bool, str]:
    from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable

    client = Neo4jClient(settings, dry_run=settings.dry_run and not settings.neo4j_password)
    try:
        client.verify()
        if settings.dry_run and not settings.neo4j_password:
            return True, "dry-run mode: writes will be recorded, not executed"
        version = "unknown"
        try:
            rows = client.read("CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1")
            if rows:
                versions = rows[0].get("versions") or []
                version = f"{rows[0].get('name')} {versions[0] if versions else ''}".strip()
        except Exception as exc:  # noqa: BLE001
            version = f"connected (version probe failed: {exc.__class__.__name__})"
        return True, f"connected to {settings.neo4j_uri} ({version}, database={settings.neo4j_database})"
    except Neo4jUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"connection failed: {exc.__class__.__name__}: {exc}"
    finally:
        client.close()


def _probe_spacy(settings: Settings) -> tuple[bool, str]:
    try:
        from puppetnet.parsing.nlp_engine import NLPEngine

        engine = NLPEngine(settings, eager_load=True)
        detail = f"backend={engine.backend} parser={engine.has_parser} ner={engine.has_ner}"
        if engine.load_errors:
            detail += f" | fallbacks: {'; '.join(engine.load_errors)[:220]}"
        usable = engine.available
        engine.close()
        return usable, detail
    except Exception as exc:  # noqa: BLE001
        return False, f"NLP engine failed to initialise: {exc.__class__.__name__}: {exc}"


def _credential_summary(settings: Settings) -> str:
    present = []
    missing = []
    for label, value in (
        ("OPENCORPORATES_API_TOKEN", settings.opencorporates_api_token),
        ("COMPANIES_HOUSE_API_KEY", settings.companies_house_api_key),
        ("WIKIDATA_USER_AGENT", settings.wikidata_user_agent),
        ("PROXY_AUTH_TOKEN", settings.worker_token),
        ("ICIJ_DATASET_URLS", settings.icij_dataset_urls),
        ("RSS_FEEDS", settings.rss_feeds),
        ("ADSBEXCHANGE_API_KEY", settings.adsbexchange_api_key),
        ("AIRCRAFT_TAIL_NUMBERS", settings.aircraft_tail_numbers),
        ("FLIGHT_LOG_URLS", settings.flight_log_urls),
    ):
        (present if value else missing).append(label)
    return f"present: {', '.join(present) or 'none'} | missing: {', '.join(missing) or 'none'}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from puppetnet import __version__

        print(f"PuppetNET ingest engine {__version__} (python {sys.version.split()[0]})")
        return EXIT_OK

    # Bootstrap logging early so config errors are visible.
    configure_logging(level=(args.log_level or os.environ.get("LOG_LEVEL", "INFO")), json_output=bool(args.log_json or os.environ.get("LOG_JSON", "").lower() in {"1", "true", "yes"}))

    if args.list_sources:
        return command_list_sources()

    try:
        settings = apply_cli_overrides(load_settings(), args)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_CONFIG

    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if args.print_config:
        return command_print_config(settings)
    if args.doctor:
        return command_doctor(settings)
    if args.fetch_only:
        return command_fetch_only(settings, args)

    started = time.perf_counter()
    options = options_from_args(args)
    logger.info(banner(f"PuppetNET daily ingest — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"))

    try:
        pipeline = IngestPipeline(settings=settings, options=options)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not initialise the pipeline: %s", exc)
        logger.debug("%s", traceback.format_exc())
        return EXIT_CONFIG

    try:
        stats = pipeline.run()
    except SystemExit as exc:  # --fail-on-error path
        # SystemExit carries either an int status or a *message*; the pipeline
        # raises the latter, so it must not be fed to int().
        code = exc.code
        if code is None:
            return EXIT_PARTIAL
        if isinstance(code, int):
            return code
        logger.error("%s", code)
        return EXIT_PARTIAL
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("interrupted — exiting")
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001
        logger.error("run failed: %s", exc)
        logger.debug("%s", traceback.format_exc())
        return EXIT_RUNTIME

    elapsed = time.perf_counter() - started
    _print_final_summary(stats, elapsed)
    _maybe_emit_github_notice(stats)

    if stats.errors and settings.fail_on_error:
        return EXIT_PARTIAL
    if stats.documents_fetched == 0 and stats.errors:
        return EXIT_RUNTIME
    return EXIT_OK


def _print_final_summary(stats: IngestStats, elapsed: float) -> None:
    print()
    print(banner("RUN SUMMARY"))
    print(stats.markdown_summary())
    print(f"\nWall clock: {elapsed:.1f}s")
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s) recorded — see the report JSON for details.")


def _maybe_emit_github_notice(stats: IngestStats) -> None:
    """Surface hard failures as GitHub Actions annotations."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        try:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write(f"status={'degraded' if stats.errors else 'ok'}\n")
        except OSError:
            pass
    if stats.errors:
        for error in stats.errors[:5]:
            print(f"::warning title=PuppetNET ingest::{error}")


if __name__ == "__main__":
    raise SystemExit(main())
