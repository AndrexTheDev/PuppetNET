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

__all__ = ["SOURCE_REGISTRY", "get_spec", "adapter_names", "ALL_SOURCE_IDS"]

_STRUCTURED = SourceType.STRUCTURED
_UNSTRUCTURED = SourceType.UNSTRUCTURED

SOURCE_REGISTRY: tuple[SourceSpec, ...] = (
    # ------------------------------------------------------------------ #
    # Structured databases — confidence weight 1.0
    # ------------------------------------------------------------------ #
    SourceSpec(
        id="icij_leaks",
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
        options={
            "queries": ["ownership", "subsidiaries", "board_members", "aircraft_operators", "vessel_operators", "political_positions"],
            "limit_per_query": 150,
            "maxlag": 5,
        },
    ),
    SourceSpec(
        id="companies_house",
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
            "rate_per_sec": spec.rate_per_sec,
            "options": {k: v for k, v in spec.options.items()},
        }
        for spec in SOURCE_REGISTRY
    ]
