# PuppetNET — serverless OSINT harvest & NLP graph engine

PuppetNET is the data-acquisition half of an OSINT network-analysis application: a
**daily, serverless pipeline** that harvests corporate-registry, offshore-leaks and
news sources, extracts **people, organisations, locations and craft (aircraft /
vessels / vehicles)** with spaCy, resolves them into **weighted relationships**, and
writes an idempotent property graph into **Neo4j AuraDB**.

There is no server to run. The whole system is three moving parts:

| Part | File | Runs on |
| --- | --- | --- |
| Edge fetch relay (IP rotation, rate limiting, deferral queue) | [`worker.js`](worker.js) | Cloudflare Workers |
| Harvest + NLP + graph writer | [`ingest.py`](ingest.py) → [`puppetnet/`](puppetnet) | GitHub Actions runner |
| Daily schedule | [`.github/workflows/daily_ingest.yml`](.github/workflows/daily_ingest.yml) | GitHub Actions cron (`0 4 * * *`) |

```
                    ┌─────────────────────────── GitHub Actions (cron 04:00 UTC) ───────────────────────────┐
                    │                                                                                       │
  sources.yaml ───► │  ingest.py ─► IngestPipeline                                                          │
  .env / secrets    │      │            │                                                                   │
                    │      │            ├─ 1. resolve sources (structured first)                            │
                    │      │            ├─ 2. Neo4j: verify → ensure schema → open :IngestRun               │
                    │      │            ├─ 3. dedupe index (graph hashes + .state cache)                    │
                    │      │            ├─ 4. per source: adapter ─► Document                               │
                    │      │            │        │             └─► Entity / Relation (structured, w = 1.0)  │
                    │      │            │        └─ unstructured ─► NLPEngine (spaCy) ─► entities, SVO       │
                    │      │            │                              triples, CRAFT ids  (w = 0.4)        │
                    │      │            ├─ 5. merge (noisy-OR) → GraphWriter → batched MERGE Cypher         │
                    │      │            └─ 6. close run, write reports/{run_id}.json + .md, GITHUB_OUTPUT   │
                    │      │                                                                                │
                    │      └─ HTTP ──► FetchClient ──► PROXY_WORKER_URL  (relay first)                      │
                    │                        │              │  429/deferred → task id → poll /tasks/{id}    │
                    │                        └──────────────┴─ fallback: local token-bucket delay queue     │
                    └───────────────────────────────────────────────────────────────────────────────────────┘
                                                        │
                                            Cloudflare Workers edge
                                     (rotating fingerprints, per-host buckets,
                                      robots.txt, KV cache, Queue deferral)
                                                        │
                                                 origin sites / APIs
```

---

## Quick start

### 1. Run it locally — no credentials required

Everything degrades: with no Neo4j password the run is a dry run, with no Worker the
client uses its own token bucket, and with no spaCy model the engine falls back to a
blank pipeline plus gazetteers.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python ingest.py --doctor          # 6 checks: config, relay, neo4j, nlp, credentials, filesystem
python ingest.py --list-sources    # the 11 registered sources as JSON
python ingest.py --print-config    # resolved config, secrets redacted

DRY_RUN=true python ingest.py --dry-run --sources news_world --limit 5
```

Reports land in `reports/<run_id>.json` and `reports/<run_id>.md`; the harvest cache in
`.state/content_hashes.json`.

### 2. Install an NLP model (recommended)

```bash
python -m spacy download en_core_web_lg      # default: statistical NER + dependency parse
# or, for maximum accuracy (needs requirements-nlp-trf.txt):
pip install -r requirements-nlp-trf.txt && python -m spacy download en_core_web_trf
```

`SPACY_MODELS` is an ordered preference list — the first model that loads wins
(`en_core_web_trf,en_core_web_lg,en_core_web_md,en_core_web_sm` by default). If none
loads, extraction continues on the lexical fallback and every edge is priced at the
co-occurrence rate.

### 3. Deploy the edge relay

```bash
npm i -g wrangler && wrangler login
wrangler kv namespace create RATE_LIMIT_KV
wrangler kv namespace create RESULT_KV
wrangler queues create puppetnet-fetch-queue
# paste the ids into wrangler.toml, then:
wrangler secret put PROXY_AUTH_TOKEN          # openssl rand -hex 32
wrangler deploy
curl -s https://<your-worker>.workers.dev/health | jq
```

All bindings are optional: without KV/Queues the Worker degrades to a stateless
forwarder with per-isolate rate limiting. See [docs/edge-relay.md](docs/edge-relay.md).

### 4. Point it at Neo4j AuraDB

Create a free AuraDB instance, then set `NEO4J_URI` (keep the `+s` scheme),
`NEO4J_USERNAME`, `NEO4J_PASSWORD`. Constraints, indexes and fulltext indexes are
created on boot (`NEO4J_ENSURE_SCHEMA=true`), and the writer respects the free tier's
200k node ceiling (`AURA_NODE_CAP`).

### 5. Schedule it

Add these repository secrets and the cron workflow does the rest:

```
NEO4J_URI  NEO4J_USERNAME  NEO4J_PASSWORD          (required to persist)
PROXY_WORKER_URL  PROXY_AUTH_TOKEN                  (required for edge relay)
OPENCORPORATES_API_TOKEN  COMPANIES_HOUSE_API_KEY   (optional)
WIKIDATA_USER_AGENT  ICIJ_QUERY_TERMS  RSS_FEEDS    (optional)
```

`Daily Ingest` can also be triggered by hand (`workflow_dispatch`) with inputs for
sources, limit, dry run, log level, skip-NLP and fail-on-error. Each run uploads its
report as an artefact and appends a summary to the job log.

---

## Data ingestion & weighting

Edge confidence is a product of three factors, computed once in
[`puppetnet/models.py`](puppetnet/models.py) and used identically everywhere:

```
confidence = source_weight × method_factor × evidence_score        (clamped to [0, 1])
```

| Source family | Adapter(s) | `source_weight` |
| --- | --- | --- |
| ICIJ Offshore Leaks | `icij` | **1.0** |
| OpenCorporates | `opencorporates` | **1.0** |
| Wikidata (SPARQL) | `wikidata` | **1.0** |
| Official registers (Companies House, CSV/TSV/JSON register files) | `companies_house`, `register_files` | **1.0** |
| News / blogs / RSS | `rss` | **0.4** |

| Extraction method | `method_factor` |
| --- | --- |
| `structured` (direct field mapping from an API/dataset) | 1.0 |
| `dependency` (subject-verb-object from the parse tree) | 1.0 |
| `pattern` / `gazetteer` (EntityRuler, apposition, craft identifiers) | 1.0 |
| `cooccurrence` (sentence-level fallback when dependency parsing fails) | **0.8** — the mandated 0.2 penalty |

So a news-derived co-occurrence edge tops out at `0.4 × 0.8 = 0.32`, while a registry
row is `1.0`. Repeated independent observations of the same edge merge with a noisy-OR
(`1 − (1 − a)(1 − b)`), so corroboration accumulates without ever reaching certainty —
but two readings of the *same clause* never stack. Edges below `MIN_EDGE_CONFIDENCE`
(0.05) are dropped before they reach Neo4j.

### NLP

* **Entity types**: `PER`, `ORG`, `LOC`, `CRAFT`. CRAFT is a first-class type: aircraft
  registrations (`9H-VUC`, `N123AB`), IMO/MMSI numbers, IATA flight numbers
  (`BA286`), prefixed vessel names (`MT Renda`, `MV Amadea`) and type appositives
  ("the superyacht *Amadea*") are detected and stored as node properties
  (`registry_country`, `craft_type`, `imo`, `airline_code`, …).
* **Relation typing**: dependency parsing turns each clause into subject-verb-object
  triples, which a rule table (33 verb rules, 18 noun/apposition rules, 9 preposition
  rules → 35 mapped predicates) converts into typed edges such as `OWNS`,
  `DIRECTOR_OF`, `SUBSIDIARY_OF`, `TRAVELED_WITH`, `ARRIVED_FROM`, `SANCTIONED_BY`.
* **Fallback**: when a parse is unavailable (no model, unparsable sentence) the engine
  falls back to sentence-level co-occurrence and prices every resulting edge with the
  0.2 penalty. Negated clauses ("does **not** own") are dropped, hedged ones
  ("allegedly", "reportedly") are flagged and down-weighted.
* **Scale guards**: per-document character and sentence caps, entity caps, chunked
  processing, and a hard runtime budget for the whole run.

Full detail in [docs/nlp-pipeline.md](docs/nlp-pipeline.md).

---

## Graph model

Four node labels, one closed relationship vocabulary (38 predicates).

```
(:Source {source_id, kind, confidence})
(:Document {doc_id, url, content_hash, source_weight, word_count, published_at})
(:Entity:Person|Organization|Location|Craft {canonical_key, name, aliases[], confidence, …})
(:IngestRun {run_id, started_at, status, documents_fetched, entities_written, …})

(:Document)-[:FROM_SOURCE]->(:Source)
(:IngestRun)-[:PROCESSED]->(:Document)
(:Document)-[:MENTIONS {count, confidence, surface_forms[]}]->(:Entity)
(:Entity)-[:OWNS|OWNED_BY|DIRECTOR_OF|… {confidence, source_weight, method, evidence,
                                          observations, doc_id, source_id, run_id}]->(:Entity)
```

Example queries:

```cypher
// Who is connected to a sanctioned vessel, and how sure are we?
MATCH (p:Person)-[r]-(c:Craft)
WHERE c.registry_country IS NOT NULL
RETURN p.name, type(r), c.name, r.confidence, r.evidence ORDER BY r.confidence DESC

// Ownership chains backed by at least one structured source
MATCH path = (a:Organization)-[:OWNS|SUBSIDIARY_OF|PARENT_OF*1..4]->(b:Organization)
WHERE all(r IN relationships(path) WHERE r.confidence >= 0.9)
RETURN path

// Corroboration: the same claim seen by several sources
MATCH (a:Entity)-[r:OWNS]->(b:Entity) WHERE r.observations > 1
RETURN a.name, b.name, r.confidence, r.observations ORDER BY r.observations DESC
```

Schema, properties and idempotency rules: [docs/graph-schema.md](docs/graph-schema.md).

---

## CLI

```
python ingest.py [--sources a,b] [--limit N] [--dry-run] [--skip-nlp] [--skip-graph]
                 [--fail-on-error] [--log-level DEBUG|INFO|WARNING|ERROR] [--log-json]
                 [--report-dir DIR] [--run-id ID] [--max-runtime SECONDS] [--no-report]
                 [--doctor] [--list-sources] [--print-config] [--version]
```

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Configuration / deployment error (`--doctor` failed, bad secrets) |
| `2` | Runtime failure (nothing harvested, or the pipeline aborted) |
| `3` | Partial success (some sources failed, the graph was still written) |

The workflow treats `3` as non-fatal — one flaky feed should not mark a daily run
failed — and escalates `1`/`2`.

---

## Configuration

Every knob is an environment variable (see [`.env.example`](.env.example)); per-source
overrides live in [`config/sources.yaml`](config/sources.yaml) and are merged over the
code registry at start-up. `python ingest.py --print-config` echoes the resolved
configuration with secrets redacted, and `--doctor` validates it end to end.

Field reference: [docs/configuration.md](docs/configuration.md).

---

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -q          # 621 tests, ~10s
```

The suite is fully hermetic: **no network, no database, no spaCy model download**.
Transports are scripted doubles, Neo4j runs in dry-run mode (which records every
statement and parameter shape), and the dependency parser is exercised through an
API-compatible fake token tree. `tests/test_e2e_dry_run.py` rehearses a complete run —
real registry, real adapters, real NLP, real writer — against canned upstream payloads
and asserts the exact rows that would be written.

| File | Covers |
| --- | --- |
| `test_models.py` | weighting constants, confidence composition, canonical keys, noisy-OR |
| `test_config.py` | env/YAML overlays, validation, redaction |
| `test_text_extract.py` | HTML/PDF/JSON/CSV extraction, boilerplate stripping |
| `test_craft.py` | aircraft/vessel/ground identifier detection |
| `test_relations.py` | SVO → predicate rule table |
| `test_nlp_dependency.py` | parse-tree triples (fake token tree) |
| `test_nlp_degraded.py` | blank-pipeline fallback, 0.2 penalty, caps |
| `test_net.py` | token bucket, delay queue, header rotation, relay + fallback client |
| `test_sources.py` | adapter framework, registry invariants, RSS/Wikidata/OpenCorporates |
| `test_graph.py` | client retries/batching, writer ordering, resolver, node budget |
| `test_pipeline.py` | harvest orchestration, dedupe, budgets, failure isolation, reports |
| `test_ingest_cli.py` | argument handling, doctor, exit codes, GitHub annotations |
| `test_e2e_dry_run.py` | full rehearsal with scripted HTTP |

---

## Repository layout

```
worker.js                     Cloudflare Worker: edge relay, rate limiting, queue
wrangler.toml                 Worker bindings, politeness tunables, cron
ingest.py                     CLI entry point (also the workflow's command)
requirements.txt              runtime dependencies (Python 3.10+)
requirements-nlp-trf.txt      extra deps for the transformer model
config/sources.yaml           per-source overrides (feeds, queries, limits, enable)
.env.example                  every environment variable, documented
puppetnet/
  config.py                   Settings: env + YAML overlay, validation, redaction
  models.py                   Document/Entity/Relation/IngestStats, weighting, keys
  logging_utils.py            console + JSON logging, banners, timing
  net/
    token_bucket.py           token bucket, per-host delay queue, backoff
    headers.py                browser fingerprint rotation per host/attempt
    proxy_client.py           FetchClient: relay → queue → direct fallback, breaker
  parsing/
    text_extract.py           HTML/PDF/JSON/CSV → clean prose + metadata
    craft.py                  CRAFT detector (registrations, IMO/MMSI, flights, prefixes)
    relations.py              SVO → RelationType rule table
    nlp_engine.py             spaCy orchestration, degraded fallback, provenance
  sources/
    registry.py               the 11 registered sources and their weights
    base.py                   adapter framework: budget, dedupe, politeness, errors
    icij.py opencorporates.py wikidata.py registers.py rss.py
  graph/
    schema.py                 constraints, indexes, MERGE Cypher templates
    neo4j_client.py           retries, batching, dry-run recorder
    resolver.py               cross-run alias resolution
    writer.py                 ordered, idempotent, batched writes + node budget
  pipeline.py                 the daily run
.github/workflows/
  daily_ingest.yml            cron 04:00 UTC + manual dispatch
  ci.yml                      lint + test on push/PR
tests/                        621 offline tests
docs/                         architecture, configuration, schema, NLP, relay, operations
```

---

## Documentation

* [docs/architecture.md](docs/architecture.md) — components, data flow, design decisions
* [docs/configuration.md](docs/configuration.md) — every setting, env var and YAML key
* [docs/graph-schema.md](docs/graph-schema.md) — nodes, relationships, Cypher templates
* [docs/nlp-pipeline.md](docs/nlp-pipeline.md) — extraction stages, rules, degradation
* [docs/edge-relay.md](docs/edge-relay.md) — `worker.js` HTTP contract and deployment
* [docs/operations.md](docs/operations.md) — running, monitoring and troubleshooting

## Licence

MIT — see [LICENSE](LICENSE).
