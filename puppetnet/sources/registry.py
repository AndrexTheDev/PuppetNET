"""Source registry: every source PuppetNET knows how to harvest.

Confidence weights are fixed here per :class:`~puppetnet.models.SourceType`:

==============================  =========  ====================
Source family                   Type       Edge confidence
==============================  =========  ====================
ICIJ Offshore Leaks             structured 1.0
OpenCorporates                  structured 1.0
Wikidata                        structured 1.0
Official registers (Companies
House, generic register files)  structured 1.0
News / blogs / RSS              unstruct.  0.4
==============================  =========  ====================

Anything in ``config/sources.yaml`` or ``ENABLED_SOURCES`` / ``DISABLED_SOURCES``
can override the defaults without touching code.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..models import SourceSpec, SourceType

__all__ = ["SOURCE_REGISTRY", "get_spec", "adapter_names", "ALL_SOURCE_IDS", "specs_for_tier"]

from ..models import Cadence  # noqa: E402  (import placed with the table it annotates)

_STRUCTURED = SourceType.STRUCTURED
_UNSTRUCTURED = SourceType.UNSTRUCTURED
_HOURLY = Cadence.HOURLY
_DAILY = Cadence.DAILY

SOURCE_REGISTRY: tuple[SourceSpec, ...] = (
    # ------------------------------------------------------------------ #
    # Structured databases — confidence weight 1.0
    # ------------------------------------------------------------------ #
    SourceSpec(
        id="icij_leaks",
        cadence=_DAILY,
        name="ICIJ Offshore Leaks",
        kind=_STRUCTURED,
        adapter="icij",
        base_url="https://offshoreleaks.icij.org",
        description=(
            "ICIJ Offshore Leaks database: nodes (entities, officers, intermediaries) "
            "and edges from the Panama Papers, Pandora Papers, Paradise Papers, "
            "FinCEN Files and Bahamas Leaks releases."
        ),
        rate_per_sec=0.25,
        burst=2,
        respect_robots=True,
        cache_ttl_seconds=21600,
        timeout_ms=60_000,
        max_documents=200,
        options={
            # Watchlist of Offshore Leaks node pages to re-check daily.
            "node_ids": [],
            # Optional TSV/CSV dumps (streamed, gzip/zip aware).
            "dataset_urls": [],
            # Search terms used against the public search UI when no watchlist.
            "search_terms": [],
            "row_limit": 20_000,
            "jurisdictions": [],
        },
    ),
    SourceSpec(
        id="opencorporates",
        cadence=_DAILY,
        name="OpenCorporates",
        kind=_STRUCTURED,
        adapter="opencorporates",
        base_url="https://api.opencorporates.com/v0.4",
        description="Company, officer and corporate-grouping records from 140+ registries.",
        rate_per_sec=0.5,
        burst=2,
        respect_robots=True,
        cache_ttl_seconds=43200,
        timeout_ms=30_000,
        max_documents=150,
        # Commercial aggregator over 140+ registries: structured, but second-hand.
        confidence_override=0.9,
        options={
            "queries": [],
            "jurisdictions": ["gb", "us_de", "hk", "sg", "ch", "lu", "cy", "mt", "vg", "ky", "pa", "sc"],
            "per_page": 30,
            "include_officers": True,
            "include_groupings": True,
        },
    ),
    SourceSpec(
        id="wikidata",
        cadence=_DAILY,
        name="Wikidata",
        kind=_STRUCTURED,
        adapter="wikidata",
        base_url="https://query.wikidata.org/sparql",
        description=(
            "SPARQL queries over Wikidata's ownership (P127), parent/subsidiary (P749/P355), "
            "board member (P3320), operator (P137) and political affiliation (P102/P39) properties."
        ),
        rate_per_sec=0.1,
        burst=1,
        respect_robots=False,   # Wikidata publishes an explicit UA + rate policy instead
        cache_ttl_seconds=86400,
        timeout_ms=120_000,
        max_documents=60,
        # Crowdsourced and only as current as its last edit.
        confidence_override=0.9,
        options={
            "queries": [
                "ownership",
                "subsidiaries",
                "board_members",
                "foundation_trustees",
                "aircraft_operators",
                "vessel_operators",
                "political_positions",
            ],
            "limit_per_query": 150,
            "maxlag": 5,
            # People who sit on the same board/foundation are linked to each other
            # (ASSOCIATED_WITH, 0.2) — bounded so a 400-seat board is skipped.
            "link_shared_organizations": True,
            "max_shared_org_members": 24,
            "max_pairs_per_org": 12,
        },
    ),
    # ------------------------------------------------------------------ #
    # Aviation: registries, telemetry and passenger manifests — weight 1.0
    # for the official register, 0.8 for second-hand/self-reported data.
    # ------------------------------------------------------------------ #
    SourceSpec(
        id="faa_registry",
        cadence=_DAILY,
        name="FAA Aircraft Registry",
        kind=_STRUCTURED,
        adapter="faa_registry",
        base_url="https://registry.faa.gov",
        description=(
            "The FAA's monthly Releasable Aircraft dump: every US-registered N-number "
            "with its registrant, address and airframe data. Produces OWNS edges and "
            "SHARES_ADDRESS edges between registrants at one normalised address."
        ),
        rate_per_sec=0.1,
        burst=1,
        respect_robots=True,
        cache_ttl_seconds=86400,
        # Bulk ZIP over a slow government host; streaming is direct, not relayed.
        timeout_ms=180_000,
        max_documents=20,
        confidence_override=1.0,
        options={
            # Empty → settings.faa_registry_url (FAA_REGISTRY_URL). A local path works too.
            "dump_url": "",
            "row_limit": 20_000,
            # The dump is republished monthly; re-parsing it daily wastes the run.
            "refresh_days": 30,
            "tail_numbers": [],
            "registrant_names": [],
            "states": [],
            "min_shared_address_size": 2,
            "max_shared_address_size": 40,
            "max_pairs_per_address": 24,
            "stream_bytes_multiplier": 8,
        },
    ),
    SourceSpec(
        id="adsb_exchange",
        cadence=_HOURLY,
        name="ADS-B Exchange / adsbdb",
        kind=_STRUCTURED,
        adapter="adsb",
        base_url="https://www.adsbdb.com/api/v1",
        description=(
            "Per-tail aircraft enrichment from ADS-B Exchange data: registered owner, "
            "owner location, airframe type and registry country. Uses adsbdb.com when "
            "no key is configured and the ADS-B Exchange v2 API on RapidAPI when "
            "ADSBEXCHANGE_API_KEY is set."
        ),
        rate_per_sec=0.5,
        burst=2,
        respect_robots=True,
        cache_ttl_seconds=43200,
        timeout_ms=30_000,
        max_documents=200,
        # Telemetry and registration lookups are self-reported/aggregated.
        confidence_override=0.8,
        options={
            # Empty → settings.aircraft_tail_numbers (AIRCRAFT_TAIL_NUMBERS).
            "tail_numbers": [],
            "callsigns": [],
        },
    ),
    SourceSpec(
        id="flight_logs",
        cadence=_DAILY,
        name="Flight logs & passenger manifests",
        kind=_STRUCTURED,
        adapter="flight_logs",
        base_url="",
        description=(
            "Passenger manifests released as CSV, text or PDF (court exhibits, FOIA "
            "dumps, operator logs). Tabular manifests become PASSENGER_ON and "
            "TRAVELED_WITH edges directly; unstructured logs are handed to the NLP "
            "pipeline as text."
        ),
        rate_per_sec=0.5,
        burst=2,
        respect_robots=True,
        cache_ttl_seconds=21600,
        timeout_ms=60_000,
        max_documents=100,
        confidence_override=0.8,
        options={
            # Empty → settings.flight_log_urls (FLIGHT_LOG_URLS, comma-separated).
            "flight_log_urls": [],
            "link_copassengers": True,
            "max_copassenger_pairs": 60,
            "default_tail_numbers": [],
        },
    ),
    SourceSpec(
        id="companies_house",
        cadence=_DAILY,
        name="UK Companies House",
        kind=_STRUCTURED,
        adapter="companies_house",
        base_url="https://api.company-information.service.gov.uk",
        description="Official UK register: companies, officers and persons with significant control.",
        rate_per_sec=0.8,
        burst=3,
        respect_robots=False,
        cache_ttl_seconds=43200,
        timeout_ms=30_000,
        max_documents=150,
        options={"queries": [], "company_numbers": [], "include_psc": True, "include_officers": True},
    ),
    SourceSpec(
        id="register_files",
        cadence=_DAILY,
        name="Official Register Files",
        kind=_STRUCTURED,
        adapter="register_files",
        base_url="",
        description=(
            "Generic CSV/TSV/JSON register exports (national company registries, sanctions "
            "lists, aircraft/vessel registries) described declaratively in config/sources.yaml."
        ),
        rate_per_sec=0.3,
        burst=2,
        respect_robots=True,
        cache_ttl_seconds=21600,
        timeout_ms=60_000,
        max_documents=200,
        options={"files": [], "row_limit": 5_000},
    ),

    # ------------------------------------------------------------------ #
    # Unstructured news / blogs / RSS — confidence weight 0.4
    # ------------------------------------------------------------------ #
    SourceSpec(
        id="occrp_rss",
        cadence=_HOURLY,
        name="OCCRP investigations",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="https://www.occrp.org/en/rss",
        description="Organized Crime and Corruption Reporting Project — investigative stories.",
        rate_per_sec=0.2,
        burst=2,
        max_documents=60,
        cache_ttl_seconds=3600,
        options={"feeds": ["https://www.occrp.org/en/rss"]},
    ),
    SourceSpec(
        id="icij_stories",
        cadence=_HOURLY,
        name="ICIJ stories",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="https://www.icij.org/feed/",
        description="ICIJ newsroom feed (Panama/Pandora/FinCEN follow-ups).",
        rate_per_sec=0.2,
        burst=2,
        max_documents=60,
        cache_ttl_seconds=3600,
        options={"feeds": ["https://www.icij.org/feed/"]},
    ),
    SourceSpec(
        id="global_witness",
        cadence=_HOURLY,
        name="Global Witness",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="https://www.globalwitness.org/en/rss/",
        description="Corruption, extractives and enabler-network reporting.",
        rate_per_sec=0.2,
        burst=2,
        max_documents=40,
        cache_ttl_seconds=3600,
        options={"feeds": ["https://www.globalwitness.org/en/rss/"]},
    ),
    SourceSpec(
        id="transparency_intl",
        cadence=_HOURLY,
        name="Transparency International",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="https://www.transparency.org/en/rss",
        description="Corruption Perceptions Index newsroom and country analyses.",
        rate_per_sec=0.2,
        burst=2,
        max_documents=40,
        cache_ttl_seconds=3600,
        options={"feeds": ["https://www.transparency.org/en/rss"]},
    ),
    SourceSpec(
        id="news_world",
        cadence=_HOURLY,
        name="World news (wire/press RSS)",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="",
        description=(
            "General world/finance news feeds used for person↔organisation↔craft "
            "co-occurrence and travel reporting. Override with RSS_FEEDS."
        ),
        rate_per_sec=0.3,
        burst=2,
        max_documents=120,
        cache_ttl_seconds=1800,
        options={
            "feeds": [
                "https://www.theguardian.com/world/rss",
                "https://feeds.bbci.co.uk/news/world/rss.xml",
                "https://www.aljazeera.com/xml/rss/all.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                "https://www.france24.com/en/rss",
            ],
            "entry_content_max_chars": 20_000,
            "fetch_full_articles": True,
        },
    ),
    SourceSpec(
        id="aviation_news",
        cadence=_HOURLY,
        name="Aviation & maritime news",
        kind=_UNSTRUCTURED,
        adapter="rss",
        base_url="",
        description=(
            "Feeds with dense CRAFT signal: tail numbers, flight movements, "
            "vessel tracking and sanctions-evasion reporting."
        ),
        rate_per_sec=0.3,
        burst=2,
        max_documents=80,
        cache_ttl_seconds=1800,
        options={
            "feeds": [
                "https://www.flightglobal.com/rss",
                "https://avherald.com/rss",
                "https://www.aviation24.be/feed/",
                "https://gcaptain.com/feed/",
                "https://www.tradewindsnews.com/rss",
            ],
            "fetch_full_articles": True,
        },
    ),
)

ALL_SOURCE_IDS: tuple[str, ...] = tuple(spec.id for spec in SOURCE_REGISTRY)


def get_spec(source_id: str) -> SourceSpec | None:
    key = (source_id or "").strip().lower()
    for spec in SOURCE_REGISTRY:
        if spec.id == key or spec.adapter == key:
            return spec
    return None


def adapter_names() -> tuple[str, ...]:
    return tuple(sorted({spec.adapter for spec in SOURCE_REGISTRY}))


def specs_for_adapter(name: str) -> tuple[SourceSpec, ...]:
    return tuple(spec for spec in SOURCE_REGISTRY if spec.adapter == name)


def iter_specs(only: Iterable[str] | None = None) -> Iterable[SourceSpec]:
    if not only:
        yield from SOURCE_REGISTRY
        return
    wanted = {str(item).lower() for item in only}
    for spec in SOURCE_REGISTRY:
        if spec.id in wanted or spec.adapter in wanted:
            yield spec


def specs_for_tier(
    tier: Any,
    registry: Iterable[SourceSpec] | None = None,
) -> tuple[SourceSpec, ...]:
    """Select the sources a scheduled tier should harvest.

    The tiers are *additive*, which is what makes the whole schedule make sense:

    * ``hourly`` returns only the wire feeds and live telemetry — the sources
      whose content is superseded within the hour;
    * ``daily`` returns only the registers, leak databases and cross-referencing
      sources — the expensive, slow-moving ones. It does **not** repeat the hourly
      tier: a feed is not worth 24 requests a day *plus* another one at dawn;
    * ``weekly`` returns **every** source. That is the point of a deep run: the
      daily register harvest has 7 chances to miss a change, and this is the pass
      that catches it, together with the maintenance work that runs beside it;
    * ``all`` (and ``None``, for a plain manual run) returns everything, which is
      the behaviour every existing invocation keeps.

    An unrecognised tier raises instead of falling back to ``all``. The fallback
    would be silent *and* expensive: a workflow that passes ``--tier houry`` would
    quietly harvest every source once an hour, which is exactly the traffic the
    tiering exists to avoid — and on a system whose whole budget is free tiers,
    that is the failure nobody notices until an origin blocks the relay.
    """
    cadence = Cadence.coerce(tier, default=Cadence.ALL)
    if cadence is None:
        raise ValueError(
            f"unknown tier {tier!r} — expected one of "
            + ", ".join(item.value for item in Cadence)
        )
    specs = tuple(registry) if registry is not None else SOURCE_REGISTRY
    if cadence in (Cadence.ALL, Cadence.WEEKLY, None):
        return specs
    return tuple(spec for spec in specs if spec.cadence is cadence)


def describe_registry() -> list[dict[str, Any]]:
    """Serialisable view of the registry (used by ``ingest.py --list-sources``)."""
    return [
        {
            "id": spec.id,
            "name": spec.name,
            "adapter": spec.adapter,
            "kind": spec.kind.value,
            "confidence": spec.confidence,
            "base_url": spec.base_url,
            "max_documents": spec.max_documents,
            "cadence": spec.cadence.value,
            "rate_per_sec": spec.rate_per_sec,
            "options": {k: v for k, v in spec.options.items()},
        }
        for spec in SOURCE_REGISTRY
    ]
