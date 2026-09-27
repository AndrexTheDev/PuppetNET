# Configuration

Three layers, highest precedence last:

```
registry / code defaults   <   config/sources.yaml   <   environment variables   <   CLI flags
```

* **Environment** is the only layer for runtime settings (credentials, limits, logging).
  Everything is optional — see [`.env.example`](../.env.example) for a fully commented
  template. `python ingest.py --print-config` echoes the resolved configuration with
  secrets redacted; `python ingest.py --doctor` validates it and probes each dependency.
* **`config/sources.yaml`** overlays *per-source* settings only (enable/disable,
  politeness, caps, adapter options). A malformed overlay is logged and ignored — the
  code registry still produces a working run.
* **CLI flags** override the environment for a single invocation.

Validation happens in `Settings.validate()` and raises `ConfigError` (exit code `1`):
a non-dry run needs `NEO4J_URI` + `NEO4J_PASSWORD`; at least one of the relay or the
direct fallback must be available; `MAX_RUNTIME_SECONDS ≥ 60`; `NEO4J_BATCH_SIZE` in
`[1, 10000]`; `MIN_EDGE_CONFIDENCE` in `[0, 1]`; `AURA_NODE_CAP ≥ 0`;
`ENTITY_RESOLVER_LIMIT ≥ 1000`.

---

## Neo4j AuraDB

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `NEO4J_URI` | `neo4j_uri` | `neo4j+s://localhost:7687` | AuraDB Free only speaks the encrypted `+s` scheme. |
| `NEO4J_USERNAME` / `NEO4J_USER` | `neo4j_username` | `neo4j` | Both spellings are accepted. |
| `NEO4J_PASSWORD` | `neo4j_password` | *(empty)* | Empty ⇒ dry run only. Redacted everywhere. |
| `NEO4J_DATABASE` | `neo4j_database` | `neo4j` | |
| `NEO4J_MAX_CONNECTION_POOL_SIZE` | `neo4j_max_connection_pool_size` | `8` | Free tier tolerates small pools. |
| `NEO4J_CONNECTION_TIMEOUT_SECONDS` | `neo4j_connection_timeout_seconds` | `30` | Also used for acquisition and max transaction retry time. |
| `NEO4J_TRANSACTION_TIMEOUT_SECONDS` | `neo4j_transaction_timeout_seconds` | `60` | |
| `NEO4J_BATCH_SIZE` | `neo4j_batch_size` | `500` | Rows per `UNWIND` statement. |
| `NEO4J_MAX_RETRIES` | `neo4j_max_retries` | `4` | Transient errors only; auth errors never retry. |
| `NEO4J_ENSURE_SCHEMA` | `neo4j_ensure_schema` | `true` | Idempotent DDL on boot. A rejected statement is logged, never fatal. |
| `AURA_NODE_CAP` | `aura_node_cap` | `200000` | Free-tier ceiling. Existing nodes always update; new nodes are admitted strongest-first until the budget is spent, then counted as `entities_capped` in the run summary. `0` disables the guard. |
| `AURA_EDGE_CAP` | `aura_edge_cap` | `400000` | The relationship ceiling that goes with it. Reported as `edge_utilisation` by `graph_analytics.py --capacity`; crossing `PRUNE_CAPACITY_TARGET` escalates pruning. `0` disables the guard. |
| `ENTITY_RESOLVER_LIMIT` | `entity_resolver_limit` | `200000` | Rows loaded into the cross-run alias index. Also how the node budget knows which nodes exist — keep it ≥ `AURA_NODE_CAP`. |

## Cloudflare Worker edge relay

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `PROXY_WORKER_URL` | `worker_url` | *(empty)* | e.g. `https://puppetnet-relay.<subdomain>.workers.dev`. |
| `PROXY_AUTH_TOKEN` | `worker_token` | *(empty)* | `Authorization: Bearer …`. Redacted. |
| `PROXY_WORKER_ENABLED` | `worker_enabled` | `true` | Set `false` to force the direct path. |
| `PROXY_TIMEOUT_SECONDS` | `worker_timeout_seconds` | `30` | |
| `PROXY_QUEUE_ON_LIMIT` | `worker_queue_on_limit` | `true` | On `429`/`202`, accept the queued task and poll instead of failing. |
| `PROXY_TASK_POLL_SECONDS` | `worker_task_poll_seconds` | `180` | Polling budget per deferred task. |
| `PROXY_TASK_POLL_INTERVAL_SECONDS` | `worker_task_poll_interval_seconds` | `3` | |
| `PROXY_CIRCUIT_BREAKER_THRESHOLD` | `worker_circuit_breaker_threshold` | `4` | Consecutive relay failures before the breaker opens. |
| `PROXY_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `worker_circuit_breaker_cooldown_seconds` | `300` | Half-opens afterwards to probe recovery. |
| `DIRECT_FALLBACK_ENABLED` | `direct_fallback_enabled` | `true` | Turn off in production only if the relay is mandatory. |

## Token-bucket delay queue (Worker-independent fallback)

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `TOKEN_BUCKET_RATE_PER_SEC` | `token_bucket_rate_per_sec` | `0.4` | ≈ one request every 2.5 s per host. |
| `TOKEN_BUCKET_BURST` | `token_bucket_burst` | `3` | Costs above burst are clamped to burst. |
| `TOKEN_BUCKET_JITTER_SECONDS` | `token_bucket_jitter_seconds` | `0.35` | Avoids lock-step polling. |
| `GLOBAL_MIN_INTERVAL_SECONDS` | `global_min_interval_seconds` | `0.75` | Shared across hosts — a global rate cap by design. |

## HTTP client

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `HTTP_TIMEOUT_SECONDS` | `http_timeout_seconds` | `25` | |
| `HTTP_MAX_RETRIES` | `http_max_retries` | `3` | |
| `HTTP_BACKOFF_BASE_SECONDS` | `http_backoff_base_seconds` | `1.2` | |
| `HTTP_BACKOFF_CAP_SECONDS` | `http_backoff_cap_seconds` | `45` | |
| `HTTP_MAX_RESPONSE_BYTES` | `http_max_response_bytes` | `12582912` | 12 MiB; larger bodies are truncated and flagged. |
| `HTTP_USER_AGENT` | `http_user_agent` | `PuppetNET-OSINT/1.5 (…; research bot)` | Used on the direct path only; the Worker rotates real browser fingerprints. |
| `EXTRA_HEADERS_JSON` | `extra_headers_json` | *(empty)* | Static extra headers as JSON, e.g. `{"X-Api-Key":"…"}`. Redacted. |

## NLP

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `SPACY_MODELS` | `spacy_models` | `en_core_web_trf,en_core_web_lg,en_core_web_md,en_core_web_sm` | Ordered preference list; the first that loads wins. |
| `SPACY_MODEL` | — | *(empty)* | Legacy single-model override; wins the preference order when set. |
| `NLP_ENABLED` | `nlp_enabled` | `true` | `false` ⇒ documents only, no extraction. |
| `NLP_LANGUAGE` | `nlp_language` | `en` | |
| `NLP_MAX_CHARS_PER_DOC` | `nlp_max_chars_per_doc` | `400000` | Longer documents are truncated and flagged. |
| `NLP_MAX_SENTENCES_PER_DOC` | `nlp_max_sentences_per_doc` | `1500` | Sentence cap per document. |
| `NLP_BATCH_SIZE` | `nlp_batch_size` | `16` | |
| `NLP_MAX_ENTITIES_PER_DOC` | `nlp_max_entities_per_doc` | `400` | |
| `ENABLE_CRAFT_DETECTION` | `enable_craft_detection` | `true` | Aircraft/vessel/vehicle identifiers. |
| `ENABLE_COOCCURRENCE_FALLBACK` | `enable_cooccurrence_fallback` | `true` | Sentence-level fallback with the 0.2 penalty. |
| `MIN_EDGE_CONFIDENCE` | `min_edge_confidence` | `0.05` | Edges below this never reach Neo4j. |
| `MAX_ARGUMENT_DISTANCE` | `max_argument_distance` | `24` | Token distance beyond which the evidence score decays. |

## Harvest limits & state

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `ENABLED_SOURCES` | `enabled_sources` | *(all)* | Allowlist of source ids. |
| `DISABLED_SOURCES` | `disabled_sources` | *(none)* | Denylist of source ids. |
| `SOURCES_FILE` | `sources_file` | `config/sources.yaml` | Overlay path; missing file ⇒ no overlay. |
| `MAX_DOCUMENTS_PER_SOURCE` | `max_documents_per_source` | `150` | Clamps each spec's `max_documents`. |
| `MAX_DOCUMENTS_TOTAL` | `max_documents_total` | `1500` | Global cap for the run. |
| `MAX_RUNTIME_SECONDS` | `max_runtime_seconds` | `2100` | 35 min; must be ≥ 60. The workflow allows 90 min total. |
| `STATE_DIR` | `state_dir` | `.state` | Holds `content_hashes.json` (bounded to 100k hashes). |
| `REPORT_DIR` | `report_dir` | `reports` | `<run_id>.json` + `<run_id>.md`. |
| `ONLY_NEW_DOCUMENTS` | `only_new_documents` | `true` | Content-hash dedupe. |
| `DEDUPE_WINDOW_DAYS` | `dedupe_window_days` | `30` | Graph-side dedupe window; `0` ⇒ everything is new. |

## Source credentials & queries

Every credential below can instead live **on the Worker** (`wrangler secret put …`),
where the host profiles inject it on the outgoing request and strip it from every URL
they report or cache. Runner-side values are only needed for direct, non-relayed fetches
— and keeping them Worker-side keeps them out of the Actions log.

| Env var | Field | Notes |
| --- | --- | --- |
| `OPENCORPORATES_API_TOKEN` | `opencorporates_api_token` | Without it the adapter uses anonymous access unless `allow_anonymous: false`. Anonymous quota is a handful of calls a day. |
| `OPENCORPORATES_ENDPOINT` | `opencorporates_endpoint` | Default `https://api.opencorporates.com/v0.4`. |
| `COMPANIES_HOUSE_API_KEY` | `companies_house_api_key` | UK register (free key, 600 calls / 5 min). |
| `WIKIDATA_USER_AGENT` | `wikidata_user_agent` | Wikidata *requires* a descriptive UA with contact details; falls back to `HTTP_USER_AGENT`. Also read by the Worker's `wikidata-sparql` profile. |
| `WIKIDATA_ENDPOINT` | `wikidata_endpoint` | Default `https://query.wikidata.org/sparql`. Queried by **POST** with a form body. |
| `WIKIDATA_QUERIES` | `wikidata_queries` | Subset of `ownership, subsidiaries, board_members, foundation_trustees, aircraft_operators, vessel_operators, political_positions, employer, sanctioned_entities, state_owned, custom`. |
| `ICIJ_QUERY_TERMS` | `icij_officer_query_terms` | Free-text searches against Offshore Leaks. |
| `ICIJ_DATASET_URLS` | `icij_dataset_urls` | Bulk dumps (CSV/TSV, zip/gzip aware). |
| `FAA_REGISTRY_URL` | `faa_registry_url` | Default `https://registry.faa.gov/database/ReleasableAircraft.zip`; a local path works too. |
| `AIRCRAFT_TAIL_NUMBERS` | `aircraft_tail_numbers` | N-numbers that switch `faa_registry` and `adsb_exchange` on. |
| `ADSDBD_ENDPOINT` | `adsbdb_endpoint` | Default `https://www.adsbdb.com/api/v1` — free, no key. |
| `ADSBEXCHANGE_ENDPOINT` | `adsbexchange_endpoint` | Default `https://adsbexchange-com1.p.rapidapi.com`. |
| `ADSBEXCHANGE_API_KEY` | `adsbexchange_api_key` | Falls back to `X_RAPIDAPI_KEY` / `RAPIDAPI_KEY`. Required for ADS-B Exchange v2 (billed per call). |
| `RAPIDAPI_HOST` | `rapidapi_host` | Sent as `x-rapidapi-host`; defaults to the endpoint hostname. |
| `FLIGHT_LOG_URLS` | `flight_log_urls` | CSV/TSV/JSON passenger manifests → `PASSENGER_ON` + `TRAVELED_WITH`. |
| `REGISTER_FILES` | `register_files` | Declarative CSV/TSV/JSON register exports. |
| `RSS_FEEDS` | `rss_feeds` | Replaces the `news_world` feed list. |

## Calculated layer (analytics)

Runs after the graph is written; see [docs/graph-schema.md](graph-schema.md#calculated--puppet_master_of).

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `ANALYTICS_ENABLED` | `analytics_enabled` | `true` | `false` ⇒ the hook reports `{"status": "disabled"}` and does nothing. |
| `ANALYTICS_MAX_PERSONS` | `analytics_max_persons` | `5000` | Persons scored per run. |
| `ANALYTICS_GRAPH_EDGE_LIMIT` | `analytics_graph_edge_limit` | `50000` | Edges pulled into the in-memory neighbourhood graph. |
| `ANALYTICS_PRUNE_DAYS` | `analytics_prune_days` | `14` | Deletes `PUPPET_MASTER_OF` edges not refreshed for this long. |
| `PUPPET_MASTER_MIN_SCORE` | `puppet_master_min_score` | `0.40` | Below this a person gets no calculated edges. |
| `PUPPET_MASTER_TOP_N` | `puppet_master_top_n` | `8` | Targets written per person. |
| `PUPPET_MASTER_MAX_EDGES` | `puppet_master_max_edges` | `2000` | Hard cap on calculated rows per run. |

## Graph maintenance (`graph_analytics.py`)

Entity resolution, capacity pruning and centrality. Runs after the ingest — see
[operations](operations.md#1-automation).

### Entity resolution

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `DEDUPE_ENABLED` | `dedupe_enabled` | `true` | `false` ⇒ the stage reports `"disabled"` and reads nothing. |
| `DEDUPE_FUZZY_THRESHOLD` | `dedupe_fuzzy_threshold` | `0.88` | Jaro-Winkler / Levenshtein gate. Must be within `[0.5, 1.0]`; below 0.8 the false-positive rate on person names is not defensible. |
| `DEDUPE_MAX_MERGES` | `dedupe_max_merges` | `500` | Merges per run. The rest wait for the next run and `capped: true` is set. `0` disables merging (report only). |
| `DEDUPE_ENTITY_LIMIT` | `dedupe_entity_limit` | `60000` | Entities pulled into the resolver, ordered by `canonical_key` so the set is stable between runs. |
| `DEDUPE_COOCCURRENCE_WINDOW_WORDS` | `dedupe_cooccurrence_window_words` | `50` | Context window for the homonym rule; converted to characters at ~6/word because the graph stores mention offsets. |
| `DEDUPE_COOCCURRENCE_DAYS` | `dedupe_cooccurrence_days` | `90` | Only documents fetched within this window are scanned for co-occurrence. |

Strict identifiers are a fixed list (`STRICT_ID_PROPERTIES` in `graph_analytics.py`):
`reg_number`, `company_number`, `wikidata_id`, `wikipedia_id`, `lei`, `imo`, `mmsi`,
`tail_number`, `transponder`, `icao24`, `opencorporates_url`. A pair that agrees on one of
them merges with no further evidence; a pair that *disagrees on `entity_type`* never merges
and is counted in `type_conflicts` instead — the same identifier on a person and an
organisation is a data error worth surfacing, not a duplicate to collapse.

### Capacity pruning

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `PRUNE_ENABLED` | `prune_enabled` | `true` | |
| `PRUNE_ORPHAN_MAX_DEGREE` | `prune_orphan_max_degree` | `1` | Semantic ties (Document/Source/IngestRun edges do not count). |
| `PRUNE_ORPHAN_MAX_WEIGHT` | `prune_orphan_max_weight` | `0.3` | The *weakest* tie must be below this. |
| `PRUNE_ORPHAN_MIN_AGE_DAYS` | `prune_orphan_min_age_days` | `180` | `last_seen` older than this. Must be ≥ 1 — deleting fresh nodes is data loss, not pruning. |
| `PRUNE_MAX_DELETIONS` | `prune_max_deletions` | `20000` | Hard ceiling per run. |
| `PRUNE_CAPACITY_TARGET` | `prune_capacity_target` | `0.85` | Utilisation at which the rule escalates to degree ≤ 2, weight < 0.4, age > 90 days. Within `(0, 1]`. |
| `PUPPET_MASTER_MIN_SCORE` | `puppet_master_min_score` | `0.40` | Doubles as the pruning protection threshold: a node at or above this risk score is never purged. |

Offshore- and shell-labelled nodes with at least one tie are protected as well — they are
findings, not litter. Every protection is listed in `prune.protected[]` with its reason.

### Centrality & anomaly scoring

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `CENTRALITY_ENABLED` | `centrality_enabled` | `true` | |
| `CENTRALITY_MAX_NODES` | `centrality_max_nodes` | `4000` | Projection cap. Betweenness is O(V·E); on a free-tier database this is the difference between 40 s and a killed job. Best-connected nodes are kept first. |
| `CENTRALITY_ENGINE` | `centrality_engine` | `auto` | `auto` probes `gds.version()` and falls back to the bundled Python implementation (AuraDB Free has no GDS). `gds` and `python` force one path. |
| `CENTRALITY_MAX_SECONDS` | `centrality_max_seconds` | `240` | Wall-clock budget for the Python scorer. A partial run is rescaled by the fraction of sources visited and flagged `truncated` rather than abandoned. |
| `ANOMALY_WEIGHTS` | `anomaly_weights` | `0.40,0.35,0.25` | `betweenness,degree_spike,offshore_ratio`. Also accepts `key=value` pairs or JSON. Must sum to 1.0 (a convex combination); a missing or unknown key raises `ConfigError` rather than silently re-weighting what operators are alerted about. |
| `ANOMALY_TOP_N` | `anomaly_top_n` | `5` | Rows in `top_anomalies` — the digest input. |
| `ANOMALY_DEGREE_SPIKE_WINDOW_HOURS` | `anomaly_degree_spike_window_hours` | `30` | Window for degree growth *and* for "first seen" in the bridge rule. Slightly wider than 24 h so a run that starts late still compares against a full previous day. |
| `MIN_EDGE_CONFIDENCE` | `min_edge_confidence` | `0.05` | Edges below this are not projected. |
| `TELEGRAM_BRIDGE_ALERT_LIMIT` | `telegram_bridge_alert_limit` | `10` | Bridge alerts kept per run, strongest first. `0` disables bridge detection output. |

A GDS projection is always dropped again, including when scoring fails — a leaked
projection sits in the instance heap and can OOM a free-tier database.

## Telegram alerting (`telegram_bot.py`)

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_ENABLED` | `telegram_enabled` | `false` | Alerting is opt-in; a deployment without a channel must not fail a maintenance run. |
| `TELEGRAM_BOT_TOKEN` | `telegram_bot_token` | *(empty)* | From `@BotFather`. A credential: redacted in `describe()`, scrubbed from every log record, never routed through the edge relay, never written to a report or to `.state`. |
| `TELEGRAM_CHAT_IDS` | `telegram_chat_ids` | *(empty)* | Comma/space separated; numeric ids (`-1001234567890`) or `@channelname`. Masked in `describe()`. |
| `TELEGRAM_API_BASE` | `telegram_api_base` | `https://api.telegram.org` | Overridable for a self-hosted Bot API server. |
| `TELEGRAM_PARSE_MODE` | `telegram_parse_mode` | `HTML` | `HTML`, `MarkdownV2`, `Markdown` or empty. Entity names are HTML-escaped regardless — an unescaped `<` makes Telegram reject the whole alert. |
| `TELEGRAM_RATE_PER_SEC` | `telegram_rate_per_sec` | `1.0` | Per-host token bucket. Within `(0, 30]`; Telegram allows ~30/s globally and ~20/min per group. |
| `TELEGRAM_BURST` | `telegram_burst` | `5.0` | |
| `TELEGRAM_SEND_RETRIES` | `telegram_send_retries` | `3` | Retries transport errors and 429s. `400`/`404` never retry; `401`/`403` abort the run (`TelegramAuthError`) instead of hammering a dead token. |
| `TELEGRAM_SUPPRESSION_HOURS` | `telegram_suppression_hours` | `24` | Per `(node, cluster pair)`. The same node bridging a *new* pair is a new alert. |
| `TELEGRAM_DIGEST_TOP_N` | `telegram_digest_top_n` | `5` | Digest rows; `--top` overrides. |
| `TELEGRAM_DISABLE_LINK_PREVIEW` | `telegram_disable_link_preview` | `true` | Preview unfurling on an alerts channel is noise. |
| `STATE_DIR` | `state_dir` | `.state` | The ledger lives at `<state_dir>/telegram_alerts.json`. |

`retry_after` from a 429 is honoured exactly, capped at 120 s so a hostile or broken
response cannot stall a CI job.

## Run behaviour & metadata

| Env var | Field | Default | Notes |
| --- | --- | --- | --- |
| `DRY_RUN` | `dry_run` | `false` | Harvest + parse + record every statement, write nothing. |
| `FAIL_ON_ERROR` | `fail_on_error` | `false` | Any recorded error ⇒ exit `3`. |
| `LOG_LEVEL` | `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`; `--log-level` wins. |
| `LOG_JSON` | `log_json` | `false` | Structured logs for machine processing. |
| `CONCURRENCY` | `concurrency` | `4` | Connection pool sizing. |
| `RANDOM_SEED` | `seed` | `1337` | Deterministic jitter/backoff for reproducible runs and tests. |
| `RUN_ID` | `run_id` | *(generated)* | `run-<UTC timestamp>-<6 hex>` when unset. |
| `GITHUB_RUN_ID` / `GITHUB_SHA` / `GITHUB_WORKFLOW` | — | *(empty)* | Injected by Actions; stored on the `:IngestRun` node. |

---

## `config/sources.yaml`

Keyed by source `id`; list only what you want to change. Recognised top-level keys map
onto `SourceSpec` fields; anything else is forwarded to the adapter's `options` map.

```yaml
version: 1
sources:
  - id: icij_leaks
    enabled: true
    rate_per_sec: 0.25        # per-host politeness for the relay's token bucket
    burst: 2
    cache_ttl_seconds: 86400
    max_documents: 400
    options:
      row_limit: 20000
      search_terms: [Kerimov, Amadea, Prigozhin]
      node_ids: []
      dataset_urls: []

  - id: news_world
    options:
      feeds: [https://www.theguardian.com/world/rss]
      fetch_full_articles: true          # resolve each link and parse the article
      entry_content_max_chars: 20000
      per_feed_limit: 20
      recent_days: 7

  - id: wikidata
    options:
      queries: [ownership, board_members, foundation_trustees, vessel_operators]
      limit_per_query: 150
      maxlag: 5
      link_shared_organizations: true   # Person-[:ASSOCIATED_WITH]->Person per shared board
      max_shared_org_members: 24        # above this the board is skipped (quadratic)
      max_pairs_per_org: 12

  - id: faa_registry
    options:
      tail_numbers: [N897RC]
      registrant_names: [Kerimov]
      refresh_days: 30                  # reuse the cached monthly dump while fresh
      min_shared_address_size: 2        # co-located registrants → :SHARES_ADDRESS
      max_pairs_per_address: 24

  - id: adsb_exchange
    options:
      tail_numbers: [N897RC]
      callsigns: []

  - id: opencorporates
    options:
      queries: ["Nord Stream", "Gazprom"]
      jurisdictions: [gb, ch, cy, mt]
      per_page: 30
      include_officers: true    # gates both per-company officers and the officer search
      include_groupings: true
```

`kind` may be overridden (`structured` / `unstructured`), and per-source authority comes
from `confidence_override` in the registry — **1.0** ICIJ, official registers and the FAA
registry; **0.9** Wikidata and OpenCorporates; **0.8** ADS-B and flight logs; **0.4**
news/RSS. It is derived from the source, never set by hand, and `kind` still decides
whether a document goes through spaCy at all.

### Adapter options reference

| Adapter | Options |
| --- | --- |
| `icij` | `node_ids`, `dataset_urls`, `search_terms`, `row_limit`, `jurisdictions` |
| `opencorporates` | `queries`, `jurisdictions`, `per_page`, `include_officers`, `include_groupings`, `allow_anonymous` |
| `wikidata` | `queries`, `limit_per_query`, `maxlag`, `custom_sparql`, `link_shared_organizations`, `max_shared_org_members`, `max_pairs_per_org` |
| `faa_registry` | `dump_url`, `row_limit`, `refresh_days`, `tail_numbers`, `registrant_names`, `states`, `min_shared_address_size`, `max_shared_address_size`, `max_pairs_per_address`, `stream_bytes_multiplier` |
| `adsb` | `tail_numbers`, `callsigns` |
| `flight_logs` | `flight_log_urls`, `link_copassengers`, `max_copassenger_pairs`, `default_tail_numbers` |
| `companies_house` | `queries`, `company_numbers`, `include_psc`, `include_officers` |
| `register_files` | `files` (declarative column→entity/relation mappings), `row_limit` |
| `rss` | `feeds`, `fetch_full_articles`, `entry_content_max_chars`, `per_feed_limit`, `recent_days`, `language` |
