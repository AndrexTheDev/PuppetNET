"""Environment-driven configuration for the PuppetNET ingest engine.

Everything is read from environment variables (GitHub Actions secrets / vars)
with sane defaults, plus an optional ``config/sources.yaml`` overlay for the
source registry. Nothing here touches the network, so the module is safe to
import in tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
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


#: Canonical order for the positional ``ANOMALY_WEIGHTS`` form, matching the
#: scoring formula ``w1·betweenness + w2·degree_spike + w3·offshore_ratio``.
ANOMALY_WEIGHT_KEYS = ("betweenness", "degree_spike", "offshore_ratio")
DEFAULT_ANOMALY_WEIGHTS = {"betweenness": 0.40, "degree_spike": 0.35, "offshore_ratio": 0.25}


def _env_weights(name: str, default: Mapping[str, float] | None = None) -> dict[str, float]:
    """Parse an anomaly-weight triple.

    Three accepted spellings, because operators reach for different ones::

        ANOMALY_WEIGHTS=0.40,0.35,0.25
        ANOMALY_WEIGHTS=betweenness=0.4,degree_spike=0.35,offshore_ratio=0.25
        ANOMALY_WEIGHTS={"betweenness": 0.4, "degree_spike": 0.35, "offshore_ratio": 0.25}

    An unknown key or a non-numeric value raises :class:`ConfigError` rather
    than falling back to the default: silently re-weighting the anomaly score
    would change what operators are alerted about, which is worse than a failed
    run. The weights are *not* normalised here — :meth:`Settings.validate`
    requires them to sum to 1.0 so a missing component is caught.
    """
    fallback = dict(default or DEFAULT_ANOMALY_WEIGHTS)
    raw = _env_str(name).strip()
    if not raw:
        return fallback

    parsed: dict[str, float] = {}
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"{name}={raw!r} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"{name} must be a JSON object")
        for key, value in payload.items():
            parsed[str(key).strip().lower()] = _coerce_weight(name, key, value)
    elif "=" in raw:
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise ConfigError(f"{name}={raw!r}: expected key=value pairs")
            key, _, value = chunk.partition("=")
            parsed[key.strip().lower()] = _coerce_weight(name, key, value)
    else:
        values = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        if len(values) != len(ANOMALY_WEIGHT_KEYS):
            raise ConfigError(
                f"{name}={raw!r} must supply {len(ANOMALY_WEIGHT_KEYS)} weights "
                f"({', '.join(ANOMALY_WEIGHT_KEYS)}) or key=value pairs"
            )
        for key, value in zip(ANOMALY_WEIGHT_KEYS, values, strict=True):
            parsed[key] = _coerce_weight(name, key, value)

    unknown = sorted(set(parsed) - set(ANOMALY_WEIGHT_KEYS))
    if unknown:
        raise ConfigError(
            f"{name}: unknown weight(s) {', '.join(unknown)} "
            f"(expected {', '.join(ANOMALY_WEIGHT_KEYS)})"
        )
    missing = [key for key in ANOMALY_WEIGHT_KEYS if key not in parsed]
    if missing:
        raise ConfigError(f"{name}: missing weight(s) {', '.join(missing)}")
    return parsed


def _coerce_weight(name: str, key: Any, value: Any) -> float:
    """One weight value → float, with the house-style hard failure."""
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: {key}={value!r} is not a number") from exc
    if result != result:  # NaN
        raise ConfigError(f"{name}: {key} is not a number")
    return result


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
    #: AuraDB Free also caps relationships at 400k. Same guard, same semantics.
    aura_edge_cap: int = 400_000
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

    # -- Graph maintenance (graph_analytics.py) -----------------------------
    #: Entity resolution: strict identifier matches plus fuzzy name matching.
    dedupe_enabled: bool = True
    #: Jaro-Winkler / Levenshtein gate from the resolution spec. A pair below
    #: this is never merged, however much context it shares.
    dedupe_fuzzy_threshold: float = 0.88
    #: Merges applied per run. A cap keeps one bad threshold from rewriting the
    #: whole graph in a single pass — merges are auditable but not free.
    dedupe_max_merges: int = 500
    #: Entities pulled into the resolver per run (ordered by recency).
    dedupe_entity_limit: int = 60_000
    #: Co-occurrence window for the homonym rule: two names count as sharing
    #: context when they appear within this many words in the same document.
    dedupe_cooccurrence_window_words: int = 50
    #: Only documents from the last N days are scanned for co-occurrence.
    dedupe_cooccurrence_days: int = 90

    #: Capacity pruning of weak orphan nodes (AuraDB Free is 200k/400k).
    prune_enabled: bool = True
    #: "Orphan" = at most this many semantic ties.
    prune_orphan_max_degree: int = 1
    #: ...whose weakest tie scores below this.
    prune_orphan_max_weight: float = 0.3
    #: ...and which has not been touched for this many days.
    prune_orphan_min_age_days: int = 180
    #: Hard ceiling on deletions per run, independent of how much qualifies.
    prune_max_deletions: int = 20_000
    #: Utilisation at which the orphan rule escalates (degree ≤ 2, weight < 0.4,
    #: age > 90 days) because the free tier is about to refuse writes.
    prune_capacity_target: float = 0.85

    #: Betweenness centrality + anomaly scoring.
    centrality_enabled: bool = True
    #: Nodes projected per run. Betweenness is O(V·E); on a free-tier database
    #: this is the difference between a 40-second pass and a killed job.
    centrality_max_nodes: int = 4_000
    #: ``auto`` probes GDS and falls back to the bundled Python implementation
    #: (AuraDB Free has no GDS). ``gds`` and ``python`` force one path.
    centrality_engine: str = "auto"
    #: Wall-clock budget for the Python scorer; a partial result is rescaled and
    #: flagged ``truncated`` rather than abandoned.
    centrality_max_seconds: float = 240.0
    #: ``score = w1·betweenness + w2·degree_spike + w3·offshore_ratio``.
    anomaly_weights: dict[str, float] = field(
        default_factory=lambda: {"betweenness": 0.40, "degree_spike": 0.35, "offshore_ratio": 0.25}
    )
    #: Rows kept in the report's ``top_anomalies`` (the digest sends these).
    anomaly_top_n: int = 5
    #: Window over which degree growth is measured. Slightly wider than 24h so a
    #: run that starts late still compares against a full previous day.
    anomaly_degree_spike_window_hours: float = 30.0
    #: Bridge alerts pushed per run before the rest wait for the next one.
    telegram_bridge_alert_limit: int = 10

    # -- Telegram alerting (telegram_bot.py) --------------------------------
    #: Master switch. Alerting is opt-in: a deployment without a channel
    #: configured must not fail a maintenance run.
    telegram_enabled: bool = False
    #: Bot API token. Equivalent to a password — never logged, never written to
    #: a report, redacted in :meth:`describe`.
    telegram_bot_token: str = ""
    #: Comma/space separated chat ids (``-1001234567890``) or ``@channelname``.
    telegram_chat_ids: str = ""
    #: Overridable so a self-hosted Bot API server can be used.
    telegram_api_base: str = "https://api.telegram.org"
    #: ``HTML`` (a small allowed subset) or ``MarkdownV2``.
    telegram_parse_mode: str = "HTML"
    #: Outbound politeness: ~1 msg/s with a small burst stays well inside the
    #: documented 30/s global and 20/min-per-group limits.
    telegram_rate_per_sec: float = 1.0
    telegram_burst: float = 5.0
    telegram_send_retries: int = 3
    #: A bridge alert is not repeated for this many hours.
    telegram_suppression_hours: float = 24.0
    telegram_digest_top_n: int = 5
    telegram_disable_link_preview: bool = True

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
                     "companies_house_api_key", "adsbexchange_api_key", "extra_headers_json",
                     "telegram_bot_token"}
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key in sensitive:
                out[key] = "***redacted***" if value else ""
            elif key == "anomaly_weights":
                out[key] = dict(value or {})
            else:
                out[key] = list(value) if isinstance(value, (list, tuple, set)) else value
        if out.get("telegram_chat_ids"):
            # Chat ids identify a private channel; enough is shown to confirm
            # that *something* is configured.
            ids = [str(item) for item in str(out["telegram_chat_ids"]).replace(",", " ").split() if item]
            out["telegram_chat_ids"] = ", ".join(_mask_chat_id(item) for item in ids)
        return out

    @property
    def telegram_configured(self) -> bool:
        """True when both a token and at least one chat id are present."""
        return bool(self.telegram_bot_token and self.telegram_chat_ids.strip())

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
        if self.aura_edge_cap < 0:
            problems.append("AURA_EDGE_CAP must be >= 0 (0 disables the guard)")
        if not 0.5 <= self.dedupe_fuzzy_threshold <= 1.0:
            problems.append("DEDUPE_FUZZY_THRESHOLD must be within [0.5, 1.0]")
        if self.dedupe_max_merges < 0:
            problems.append("DEDUPE_MAX_MERGES must be >= 0 (0 disables merging)")
        if self.dedupe_cooccurrence_window_words < 1:
            problems.append("DEDUPE_COOCCURRENCE_WINDOW_WORDS must be >= 1")
        if self.prune_orphan_min_age_days < 1:
            problems.append("PRUNE_ORPHAN_MIN_AGE_DAYS must be >= 1 (deleting fresh nodes is data loss)")
        if self.prune_max_deletions < 0:
            problems.append("PRUNE_MAX_DELETIONS must be >= 0")
        if not 0.0 < self.prune_capacity_target <= 1.0:
            problems.append("PRUNE_CAPACITY_TARGET must be within (0, 1]")
        if self.centrality_max_nodes < 0:
            problems.append("CENTRALITY_MAX_NODES must be >= 0")
        if self.centrality_engine not in {"auto", "gds", "python"}:
            problems.append("CENTRALITY_ENGINE must be one of auto, gds, python")
        weights = self.anomaly_weights or {}
        if abs(sum(float(value) for value in weights.values()) - 1.0) > 0.01:
            problems.append("ANOMALY_WEIGHTS must sum to 1.0 (they are a convex combination)")
        if any(float(value) < 0 for value in weights.values()):
            problems.append("ANOMALY_WEIGHTS entries must be >= 0")
        if self.telegram_parse_mode not in {"HTML", "MarkdownV2", "Markdown", ""}:
            problems.append("TELEGRAM_PARSE_MODE must be HTML, MarkdownV2, Markdown or empty")
        if self.telegram_rate_per_sec <= 0 or self.telegram_rate_per_sec > 30:
            problems.append("TELEGRAM_RATE_PER_SEC must be within (0, 30]")
        if self.telegram_suppression_hours < 0:
            problems.append("TELEGRAM_SUPPRESSION_HOURS must be >= 0")
        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


def _mask_chat_id(chat_id: str) -> str:
    """Show the head and tail of a chat id only (``-100123…890``, ``@pup…ts``)."""
    text = str(chat_id or "")
    if len(text) <= 6:
        return text[:2] + "…" if text else ""
    return f"{text[:5]}…{text[-3:]}"


def load_settings(env: dict[str, str] | None = None, *, validate: bool = True) -> Settings:
    """Build :class:`Settings` from the process environment.

    ``env`` may be supplied for tests; otherwise ``os.environ`` is used.

    ``validate=False`` returns the settings without the run-level requirements
    check. Tooling that does not touch Neo4j or the fetch layer — the Telegram
    alert engine, in particular — must be runnable without a database password
    configured, and a hard ``ConfigError`` there would be a false alarm.
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
            aura_edge_cap=_env_int("AURA_EDGE_CAP", 400_000),
            dedupe_enabled=_env_bool("DEDUPE_ENABLED", True),
            dedupe_fuzzy_threshold=_env_float("DEDUPE_FUZZY_THRESHOLD", 0.88),
            dedupe_max_merges=_env_int("DEDUPE_MAX_MERGES", 500),
            dedupe_entity_limit=_env_int("DEDUPE_ENTITY_LIMIT", 60_000),
            dedupe_cooccurrence_window_words=_env_int("DEDUPE_COOCCURRENCE_WINDOW_WORDS", 50),
            dedupe_cooccurrence_days=_env_int("DEDUPE_COOCCURRENCE_DAYS", 90),
            prune_enabled=_env_bool("PRUNE_ENABLED", True),
            prune_orphan_max_degree=_env_int("PRUNE_ORPHAN_MAX_DEGREE", 1),
            prune_orphan_max_weight=_env_float("PRUNE_ORPHAN_MAX_WEIGHT", 0.3),
            prune_orphan_min_age_days=_env_int("PRUNE_ORPHAN_MIN_AGE_DAYS", 180),
            prune_max_deletions=_env_int("PRUNE_MAX_DELETIONS", 20_000),
            prune_capacity_target=_env_float("PRUNE_CAPACITY_TARGET", 0.85),
            centrality_enabled=_env_bool("CENTRALITY_ENABLED", True),
            centrality_max_nodes=_env_int("CENTRALITY_MAX_NODES", 4_000),
            centrality_engine=_env_str("CENTRALITY_ENGINE", "auto").strip().lower(),
            centrality_max_seconds=_env_float("CENTRALITY_MAX_SECONDS", 240.0),
            anomaly_weights=_env_weights("ANOMALY_WEIGHTS"),
            anomaly_top_n=_env_int("ANOMALY_TOP_N", 5),
            anomaly_degree_spike_window_hours=_env_float("ANOMALY_DEGREE_SPIKE_WINDOW_HOURS", 30.0),
            telegram_bridge_alert_limit=_env_int("TELEGRAM_BRIDGE_ALERT_LIMIT", 10),
            telegram_enabled=_env_bool("TELEGRAM_ENABLED", False),
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_ids=_env_str("TELEGRAM_CHAT_IDS", ""),
            telegram_api_base=_env_str("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            telegram_parse_mode=_env_str("TELEGRAM_PARSE_MODE", "HTML"),
            telegram_rate_per_sec=_env_float("TELEGRAM_RATE_PER_SEC", 1.0),
            telegram_burst=_env_float("TELEGRAM_BURST", 5.0),
            telegram_send_retries=_env_int("TELEGRAM_SEND_RETRIES", 3),
            telegram_suppression_hours=_env_float("TELEGRAM_SUPPRESSION_HOURS", 24.0),
            telegram_digest_top_n=_env_int("TELEGRAM_DIGEST_TOP_N", 5),
            telegram_disable_link_preview=_env_bool("TELEGRAM_DISABLE_LINK_PREVIEW", True),
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
        if validate:
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
