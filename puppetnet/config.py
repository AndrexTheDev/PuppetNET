"""Environment-driven configuration for the PuppetNET ingest engine.

Everything is read from environment variables (GitHub Actions secrets / vars)
with sane defaults, plus an optional ``config/sources.yaml`` overlay for the
source registry. Nothing here touches the network, so the module is safe to
import in tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_utils import get_logger
from .models import SourceSpec, SourceType

__all__ = ["Settings", "load_settings", "ConfigError", "REPO_ROOT", "resolve_source_specs"]

logger = get_logger("config")

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or contradictory."""


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else str(value).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        # A typo in a cron job's environment must not silently change the
        # harvest budget or batch size: fail the run at startup instead.
        raise ConfigError(f"{name}={raw!r} is not a valid integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid number") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_list(name: str, default: Sequence[str] = ()) -> list[str]:
    raw = _env_str(name)
    if not raw:
        return list(default)
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


@dataclass
class Settings:
    """Fully-resolved runtime configuration."""

    # -- Neo4j AuraDB -------------------------------------------------------
    neo4j_uri: str = "neo4j+s://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_max_connection_pool_size: int = 8
    neo4j_connection_timeout_seconds: float = 30.0
    neo4j_transaction_timeout_seconds: float = 60.0
    neo4j_batch_size: int = 500
    neo4j_max_retries: int = 4
    neo4j_ensure_schema: bool = True
    #: AuraDB Free caps a database at 200k nodes. Once the entity population
    #: reaches this budget the writer stops *creating* nodes (existing ones keep
    #: updating) instead of letting every write fail. ``0`` disables the guard.
    aura_node_cap: int = 200_000
    #: Rows pulled into the cross-run alias index at the start of a run.
    entity_resolver_limit: int = 200_000

    # -- Edge relay (Cloudflare Worker) ------------------------------------
    worker_url: str = ""
    worker_token: str = ""
    worker_enabled: bool = True
    worker_timeout_seconds: float = 30.0
    worker_queue_on_limit: bool = True
    worker_task_poll_seconds: float = 180.0
    worker_task_poll_interval_seconds: float = 3.0
    worker_circuit_breaker_threshold: int = 4
    worker_circuit_breaker_cooldown_seconds: float = 300.0
    #: When the Worker is unreachable, fall back to a direct connection that is
    #: policed by the local token bucket below.
    direct_fallback_enabled: bool = True

    # -- Local token-bucket delay queue (fallback path) ---------------------
    token_bucket_rate_per_sec: float = 0.4
    token_bucket_burst: int = 3
    token_bucket_jitter_seconds: float = 0.35
    global_min_interval_seconds: float = 0.75
    http_timeout_seconds: float = 25.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 1.2
    http_backoff_cap_seconds: float = 45.0
    http_max_response_bytes: int = 12 * 1024 * 1024
    http_user_agent: str = (
        "PuppetNET-OSINT/1.5 (+https://github.com/AndrexTheDev/PuppetNET; research bot)"
    )

    # -- NLP ----------------------------------------------------------------
    #: Ordered preference list; the first importable model wins.
    spacy_models: list[str] = field(
        default_factory=lambda: ["en_core_web_trf", "en_core_web_lg", "en_core_web_md", "en_core_web_sm"]
    )
    nlp_enabled: bool = True
    nlp_max_chars_per_doc: int = 400_000
    nlp_max_sentences_per_doc: int = 1_500
    nlp_batch_size: int = 16
    nlp_max_entities_per_doc: int = 400
    nlp_language: str = "en"
    #: Triples below this confidence never reach Neo4j.
    min_edge_confidence: float = 0.05
    #: Extra penalty applied to co-occurrence triples is defined in models.py.
    enable_craft_detection: bool = True
    enable_cooccurrence_fallback: bool = True
    #: Maximum dependency distance (tokens) before evidence score decays.
    max_argument_distance: int = 24

    # -- Harvesting ---------------------------------------------------------
    enabled_sources: list[str] = field(default_factory=list)
    disabled_sources: list[str] = field(default_factory=list)
    sources_file: str = "config/sources.yaml"
    max_documents_per_source: int = 150
    max_documents_total: int = 1_500
    max_runtime_seconds: int = 2_100
    state_dir: str = ".state"
    report_dir: str = "reports"
    dedupe_window_days: int = 30
    only_new_documents: bool = True

    # -- Source credentials (all optional; adapters degrade gracefully) ------
    opencorporates_api_token: str = ""
    companies_house_api_key: str = ""
    wikidata_user_agent: str = ""
    icij_dataset_urls: list[str] = field(default_factory=list)
    icij_officer_query_terms: list[str] = field(default_factory=list)
    wikidata_queries: list[str] = field(default_factory=list)
    register_files: list[str] = field(default_factory=list)
    rss_feeds: list[str] = field(default_factory=list)
    extra_headers_json: str = ""

    # -- OSINT endpoints ----------------------------------------------------
    #: Real target endpoints. Every one is overridable so a mirror, a corporate
    #: egress proxy or a local dump can be substituted without a code change.
    wikidata_endpoint: str = "https://query.wikidata.org/sparql"
    opencorporates_endpoint: str = "https://api.opencorporates.com/v0.4"
    icij_base_url: str = "https://offshoreleaks.icij.org"
    #: Page listing the downloadable Offshore Leaks dumps; the adapter reads the
    #: ``.zip`` links from it when ``ICIJ_DATASET_URLS`` is empty.
    icij_data_index_url: str = "https://offshoreleaks.icij.org/pages/database"
    #: FAA monthly registry dump: N-number → registrant (owner) + address.
    faa_registry_url: str = "https://registry.faa.gov/database/ReleasableAircraft.zip"
    #: Free community read-through of ADS-B Exchange data.
    adsbdb_endpoint: str = "https://www.adsbdb.com/api/v1"
    #: ADS-B Exchange v2 via RapidAPI (needs ``ADSBEXCHANGE_API_KEY``).
    adsbexchange_endpoint: str = "https://adsbexchange-com1.p.rapidapi.com"
    adsbexchange_api_key: str = ""
    rapidapi_host: str = "adsbexchange-com1.p.rapidapi.com"
    #: Passenger manifests / flight logs (PDF, CSV or plain text).
    flight_log_urls: list[str] = field(default_factory=list)
    #: Tail numbers to enrich on every run.
    aircraft_tail_numbers: list[str] = field(default_factory=list)

    # -- Calculated layer (PUPPET_MASTER_OF + Person.risk_score) ------------
    analytics_enabled: bool = True
    #: People scored per run; keeps a pathological graph from stalling the job.
    analytics_max_persons: int = 5_000
    #: Previously written edges folded into today's score (0 = in-run data only).
    analytics_graph_edge_limit: int = 50_000
    #: Calculated edges not refreshed within this many days are deleted.
    analytics_prune_days: int = 14
    #: Minimum ``Person.risk_score`` before a PUPPET_MASTER_OF edge is written.
    puppet_master_min_score: float = 0.40
    #: Targets kept per person.
    puppet_master_top_n: int = 8
    #: Global cap on calculated edges per run.
    puppet_master_max_edges: int = 2_000

    # -- Runtime behaviour --------------------------------------------------
    dry_run: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    fail_on_error: bool = False
    run_id: str = ""
    github_run_id: str = ""
    github_sha: str = ""
    github_workflow: str = ""
    concurrency: int = 4
    seed: int = 1337

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_uri and self.neo4j_password)

    @property
    def worker_configured(self) -> bool:
        return bool(self.worker_enabled and self.worker_url and self.worker_token)

    @property
    def state_path(self) -> Path:
        path = Path(self.state_dir)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def report_path(self) -> Path:
        path = Path(self.report_dir)
        if not path.is_absolute():
            path = REPO_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sources_file_path(self) -> Path | None:
        candidate = Path(self.sources_file)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        return candidate if candidate.exists() else None

    def source_enabled(self, spec: SourceSpec) -> bool:
        if not spec.enabled:
            return False
        if self.disabled_sources and spec.id in self.disabled_sources:
            return False
        if self.enabled_sources and spec.id not in self.enabled_sources:
            return False
        return True

    def describe(self) -> dict[str, Any]:
        """Redacted snapshot for logs and the run report."""
        sensitive = {"neo4j_password", "worker_token", "opencorporates_api_token",
                     "companies_house_api_key", "adsbexchange_api_key", "extra_headers_json"}
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key in sensitive:
                out[key] = "***redacted***" if value else ""
            else:
                out[key] = list(value) if isinstance(value, (list, tuple, set)) else value
        return out

    def validate(self) -> None:
        problems: list[str] = []
        if not self.dry_run:
            if not self.neo4j_uri:
                problems.append("NEO4J_URI is required")
            if not self.neo4j_password:
                problems.append("NEO4J_PASSWORD is required (unless DRY_RUN=true)")
        if not self.worker_configured and not self.direct_fallback_enabled:
            problems.append(
                "Neither the Cloudflare relay (PROXY_WORKER_URL + PROXY_AUTH_TOKEN) nor the "
                "direct fallback (DIRECT_FALLBACK_ENABLED=true) is available — nothing could be fetched."
            )
        if self.max_runtime_seconds < 60:
            problems.append("MAX_RUNTIME_SECONDS must be >= 60")
        if self.neo4j_batch_size < 1 or self.neo4j_batch_size > 10_000:
            problems.append("NEO4J_BATCH_SIZE must be between 1 and 10000")
        if self.aura_node_cap < 0:
            problems.append("AURA_NODE_CAP must be >= 0 (0 disables the guard)")
        if self.entity_resolver_limit < 1_000:
            problems.append("ENTITY_RESOLVER_LIMIT must be >= 1000")
        if self.min_edge_confidence < 0 or self.min_edge_confidence > 1:
            problems.append("MIN_EDGE_CONFIDENCE must be within [0, 1]")
        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from the process environment.

    ``env`` may be supplied for tests; otherwise ``os.environ`` is used.
    """
    if env is not None:
        # Snapshot first: the caller may hand us ``os.environ`` itself, and
        # clearing it before reading would silently drop every value.
        incoming = {k: str(v) for k, v in env.items()}
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(incoming)
    try:
        models = _env_list(
            "SPACY_MODELS",
            default=["en_core_web_trf", "en_core_web_lg", "en_core_web_md", "en_core_web_sm"],
        )
        # A single legacy variable always wins the preference order, even when
        # it is also listed in SPACY_MODELS.
        single = _env_str("SPACY_MODEL")
        if single:
            models = [single, *(model for model in models if model != single)]

        settings = Settings(
            neo4j_uri=_env_str("NEO4J_URI", "neo4j+s://localhost:7687"),
            # NEO4J_USER is accepted as an alias: the Aura console and most
            # driver docs use the two names interchangeably, and guessing wrong
            # surfaces as an opaque authentication failure.
            neo4j_username=_env_str("NEO4J_USERNAME", _env_str("NEO4J_USER", "neo4j")),
            neo4j_password=_env_str("NEO4J_PASSWORD", ""),
            neo4j_database=_env_str("NEO4J_DATABASE", "neo4j"),
            neo4j_max_connection_pool_size=_env_int("NEO4J_MAX_CONNECTION_POOL_SIZE", 8),
            neo4j_connection_timeout_seconds=_env_float("NEO4J_CONNECTION_TIMEOUT_SECONDS", 30.0),
            neo4j_transaction_timeout_seconds=_env_float("NEO4J_TRANSACTION_TIMEOUT_SECONDS", 60.0),
            neo4j_batch_size=_env_int("NEO4J_BATCH_SIZE", 500),
            neo4j_max_retries=_env_int("NEO4J_MAX_RETRIES", 4),
            neo4j_ensure_schema=_env_bool("NEO4J_ENSURE_SCHEMA", True),
            aura_node_cap=_env_int("AURA_NODE_CAP", 200_000),
            entity_resolver_limit=_env_int("ENTITY_RESOLVER_LIMIT", 200_000),
            worker_url=_env_str("PROXY_WORKER_URL", "").rstrip("/"),
            worker_token=_env_str("PROXY_AUTH_TOKEN", ""),
            worker_enabled=_env_bool("PROXY_WORKER_ENABLED", True),
            worker_timeout_seconds=_env_float("PROXY_TIMEOUT_SECONDS", 30.0),
            worker_queue_on_limit=_env_bool("PROXY_QUEUE_ON_LIMIT", True),
            worker_task_poll_seconds=_env_float("PROXY_TASK_POLL_SECONDS", 180.0),
            worker_task_poll_interval_seconds=_env_float("PROXY_TASK_POLL_INTERVAL_SECONDS", 3.0),
            worker_circuit_breaker_threshold=_env_int("PROXY_CIRCUIT_BREAKER_THRESHOLD", 4),
            worker_circuit_breaker_cooldown_seconds=_env_float("PROXY_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 300.0),
            direct_fallback_enabled=_env_bool("DIRECT_FALLBACK_ENABLED", True),
            token_bucket_rate_per_sec=_env_float("TOKEN_BUCKET_RATE_PER_SEC", 0.4),
            token_bucket_burst=_env_int("TOKEN_BUCKET_BURST", 3),
            token_bucket_jitter_seconds=_env_float("TOKEN_BUCKET_JITTER_SECONDS", 0.35),
            global_min_interval_seconds=_env_float("GLOBAL_MIN_INTERVAL_SECONDS", 0.75),
            http_timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", 25.0),
            http_max_retries=_env_int("HTTP_MAX_RETRIES", 3),
            http_backoff_base_seconds=_env_float("HTTP_BACKOFF_BASE_SECONDS", 1.2),
            http_backoff_cap_seconds=_env_float("HTTP_BACKOFF_CAP_SECONDS", 45.0),
            http_max_response_bytes=_env_int("HTTP_MAX_RESPONSE_BYTES", 12 * 1024 * 1024),
            http_user_agent=_env_str("HTTP_USER_AGENT", Settings.http_user_agent),
            spacy_models=models,
            nlp_enabled=_env_bool("NLP_ENABLED", True),
            nlp_max_chars_per_doc=_env_int("NLP_MAX_CHARS_PER_DOC", 400_000),
            nlp_max_sentences_per_doc=_env_int("NLP_MAX_SENTENCES_PER_DOC", 1_500),
            nlp_batch_size=_env_int("NLP_BATCH_SIZE", 16),
            nlp_max_entities_per_doc=_env_int("NLP_MAX_ENTITIES_PER_DOC", 400),
            nlp_language=_env_str("NLP_LANGUAGE", "en"),
            min_edge_confidence=_env_float("MIN_EDGE_CONFIDENCE", 0.05),
            enable_craft_detection=_env_bool("ENABLE_CRAFT_DETECTION", True),
            enable_cooccurrence_fallback=_env_bool("ENABLE_COOCCURRENCE_FALLBACK", True),
            max_argument_distance=_env_int("MAX_ARGUMENT_DISTANCE", 24),
            enabled_sources=[s.lower() for s in _env_list("ENABLED_SOURCES")],
            disabled_sources=[s.lower() for s in _env_list("DISABLED_SOURCES")],
            sources_file=_env_str("SOURCES_FILE", "config/sources.yaml"),
            max_documents_per_source=_env_int("MAX_DOCUMENTS_PER_SOURCE", 150),
            max_documents_total=_env_int("MAX_DOCUMENTS_TOTAL", 1_500),
            max_runtime_seconds=_env_int("MAX_RUNTIME_SECONDS", 2_100),
            state_dir=_env_str("STATE_DIR", ".state"),
            report_dir=_env_str("REPORT_DIR", "reports"),
            dedupe_window_days=_env_int("DEDUPE_WINDOW_DAYS", 30),
            only_new_documents=_env_bool("ONLY_NEW_DOCUMENTS", True),
            opencorporates_api_token=_env_str("OPENCORPORATES_API_TOKEN", ""),
            companies_house_api_key=_env_str("COMPANIES_HOUSE_API_KEY", ""),
            wikidata_user_agent=_env_str(
                "WIKIDATA_USER_AGENT",
                "PuppetNET-OSINT/1.5 (https://github.com/AndrexTheDev/PuppetNET; contact: ops@example.org)",
            ),
            icij_dataset_urls=_env_list("ICIJ_DATASET_URLS"),
            icij_officer_query_terms=_env_list("ICIJ_QUERY_TERMS"),
            wikidata_queries=[q.lower() for q in _env_list("WIKIDATA_QUERIES")],
            register_files=_env_list("REGISTER_FILES"),
            rss_feeds=_env_list("RSS_FEEDS"),
            wikidata_endpoint=_env_str("WIKIDATA_ENDPOINT", Settings.wikidata_endpoint),
            opencorporates_endpoint=_env_str("OPENCORPORATES_ENDPOINT", Settings.opencorporates_endpoint),
            icij_base_url=_env_str("ICIJ_BASE_URL", Settings.icij_base_url),
            icij_data_index_url=_env_str("ICIJ_DATA_INDEX_URL", Settings.icij_data_index_url),
            faa_registry_url=_env_str("FAA_REGISTRY_URL", Settings.faa_registry_url),
            adsbdb_endpoint=_env_str("ADSDBD_ENDPOINT", Settings.adsbdb_endpoint),
            adsbexchange_endpoint=_env_str("ADSBEXCHANGE_ENDPOINT", Settings.adsbexchange_endpoint),
            # ADS-B Exchange is sold through RapidAPI; accept either naming.
            adsbexchange_api_key=_env_str(
                "ADSBEXCHANGE_API_KEY",
                _env_str("X_RAPIDAPI_KEY", _env_str("RAPIDAPI_KEY", "")),
            ),
            rapidapi_host=_env_str("RAPIDAPI_HOST", Settings.rapidapi_host),
            flight_log_urls=_env_list("FLIGHT_LOG_URLS"),
            aircraft_tail_numbers=[tail.strip().upper() for tail in _env_list("AIRCRAFT_TAIL_NUMBERS") if tail.strip()],
            analytics_enabled=_env_bool("ANALYTICS_ENABLED", True),
            analytics_max_persons=_env_int("ANALYTICS_MAX_PERSONS", 5_000),
            analytics_graph_edge_limit=_env_int("ANALYTICS_GRAPH_EDGE_LIMIT", 50_000),
            analytics_prune_days=_env_int("ANALYTICS_PRUNE_DAYS", 14),
            puppet_master_min_score=_env_float("PUPPET_MASTER_MIN_SCORE", 0.40),
            puppet_master_top_n=_env_int("PUPPET_MASTER_TOP_N", 8),
            puppet_master_max_edges=_env_int("PUPPET_MASTER_MAX_EDGES", 2_000),
            extra_headers_json=_env_str("EXTRA_HEADERS_JSON", ""),
            dry_run=_env_bool("DRY_RUN", False),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            log_json=_env_bool("LOG_JSON", False),
            fail_on_error=_env_bool("FAIL_ON_ERROR", False),
            run_id=_env_str("RUN_ID", ""),
            github_run_id=_env_str("GITHUB_RUN_ID", ""),
            github_sha=_env_str("GITHUB_SHA", ""),
            github_workflow=_env_str("GITHUB_WORKFLOW", ""),
            concurrency=_env_int("CONCURRENCY", 4),
            seed=_env_int("RANDOM_SEED", 1337),
        )
        settings.validate()
        return settings
    finally:
        if env is not None:
            os.environ.clear()
            os.environ.update(previous)


def resolve_source_specs(settings: Settings, registry: Iterable[SourceSpec]) -> list[SourceSpec]:
    """Apply enable/disable filters and the YAML overlay to the registry."""
    overlay = _load_sources_overlay(settings)
    resolved: list[SourceSpec] = []
    for spec in registry:
        overrides = dict(overlay.get(spec.id, {}))
        if overrides:
            kind = overrides.pop("kind", None)
            if kind:
                overrides["kind"] = SourceType(str(kind).lower())
            spec = spec.with_options(**overrides)
        spec = spec.with_options(max_documents=min(spec.max_documents, settings.max_documents_per_source))
        if settings.source_enabled(spec):
            resolved.append(spec)
    return resolved


def _load_sources_overlay(settings: Settings) -> dict[str, dict[str, Any]]:
    path = settings.sources_file_path()
    if path is None:
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001 - yaml.YAMLError is not a ValueError
        # A malformed overlay must degrade to the code registry, never abort a
        # scheduled run: the harvest itself is still perfectly well defined.
        logger.warning("ignoring unreadable %s (%s)", path, exc)
        return {}
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        return {}
    overlay: dict[str, dict[str, Any]] = {}
    for entry in sources:
        if isinstance(entry, dict) and entry.get("id"):
            overlay[str(entry["id"]).lower()] = {k: v for k, v in entry.items() if k != "id"}
    return overlay
