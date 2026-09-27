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
                    │      │            │        │             └─► Entity / Relation (structured, w ≤ 1.0)  │
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
python ingest.py --list-sources    # the 14 registered sources as JSON
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
| Official registers — Companies House, sanctions/registry dumps | `companies_house`, `register_files` | **1.0** |
| FAA Releasable Aircraft registry (owner ↔ tail number) | `faa_registry` | **1.0** |
| Wikidata SPARQL (ownerships, boards, foundations, political posts) | `wikidata` | **0.9** |
| OpenCorporates (directorships, groupings, shared addresses) | `opencorporates` | **0.9** |
| ADS-B Exchange / adsbdb.com telemetry, flight-log manifests | `adsb_exchange`, `flight_logs` | **0.8** |
| News / blogs / RSS | `rss` | **0.4** |

`source_weight` is the *authority of the source*; the number on each edge is the
*authority of the claim type*.

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

### Edge weights

Every relationship type also carries a domain weight
([`DOMAIN_EDGE_WEIGHTS`](puppetnet/models.py), 46 predicates), exposed as
`Relation.weight` and written as `r.weight`:

| Relationship | `weight` | Comes from |
| --- | --- | --- |
| `CONTROLS` | **1.0** | ICIJ/OpenCorporates officer & PSC rows |
| `TRUSTEE_OF` | **0.9** | Wikidata foundation boards (P3320/P488/P112/P169) |
| `SHARES_ADDRESS` | **0.8** | FAA registrant addresses, OpenCorporates registered offices |
| `PASSENGER_ON` | **0.8** | flight-log manifests |
| `DONATED_TO` | **0.7** | funding disclosures, news |
| `TRAVELED_WITH` | **0.6** | co-passenger pairs on the same manifest |
| `MENTIONED_WITH` | **0.4** | news co-occurrence (Person → Person) |
| `ASSOCIATED_WITH` | **0.3** | two people on the *same* board or foundation |
| `PUPPET_MASTER_OF` | *calculated* | the analytics pass — see below |

Adapters never set a weight by hand: `Relation.weight` falls back to the table, so
the vocabulary cannot drift per source. The one exception is the analytics pass,
which overrides it with a computed score.

### Influence scoring (`PUPPET_MASTER_OF`)

After the graph is written, [`puppetnet/graph/analytics.py`](puppetnet/graph/analytics.py)
walks each person's neighbourhood and derives
`(:Person)-[:PUPPET_MASTER_OF {score}]->(:Entity)` edges — additive components,
decayed by `0.7` per hop, capped at four hops:

| Component | Weight | Signal |
| --- | --- | --- |
| `control_breadth` | 0.30 | how many entities the person controls, chain-weighted |
| `opacity` | 0.25 | shell-company markers (no website/staff, mailbox address, secrecy jurisdiction) |
| `layering` | 0.20 | depth of the ownership chain beneath them |
| `convergence` | 0.10 | unrelated associates converging on the same assets |
| `adversarial` | 0.10 | sanctions, leaks and litigation signals |
| `movement` | 0.05 | aircraft/vessel movement around the same person |

Candidates scoring under `PUPPET_MASTER_MIN_SCORE` (0.40) are dropped. The pass is
idempotent, writes only its own `:PUPPET_MASTER_OF` edges, and can never fail a run:
an exception is recorded as an error and the run still exits 0.

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

Four storage labels plus domain labels, one closed relationship vocabulary
(46 predicates).

```
(:Source {source_id, kind, confidence})
(:Document {doc_id, url, content_hash, source_weight, word_count, published_at})
(:Entity:Person|Organization|Location|Craft {canonical_key, name, aliases[], confidence, …})
     // domain labels are SET/REMOVEd on the same node, never MERGEd separately:
     //   :ShellCompany (opacity markers) · :Company {jurisdiction, reg_number}
     //   :Foundation {name, country} · :Aircraft {tail_number, owner}
     //   :Location {address, gps}
(:IngestRun {run_id, started_at, status, documents_fetched, entities_written, …})

(:Document)-[:FROM_SOURCE]->(:Source)
(:IngestRun)-[:PROCESSED]->(:Document)
(:Document)-[:MENTIONS {count, confidence, surface_forms[]}]->(:Entity)
(:Entity)-[:OWNS|CONTROLS|TRUSTEE_OF|SHARES_ADDRESS|… {confidence, weight, source_weight,
                                          method, evidence, observations, doc_id,
                                          source_id, run_id}]->(:Entity)
(:Person)-[:PUPPET_MASTER_OF {score, control_breadth, opacity, layering,
                              convergence, adversarial, movement, archetype}]->(:Entity)
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

// Two trustees of the same foundation, and which foundation it was
MATCH (a:Person)-[r:ASSOCIATED_WITH]->(b:Person)
RETURN a.name, b.name, r.weight, r.evidence ORDER BY r.confidence DESC

// Who moved with whom, and on whose aircraft
MATCH (p:Person)-[:PASSENGER_ON]->(ac:Aircraft)<-[:OWNS|CONTROLS]-(owner:Person)
RETURN p.name, ac.tail_number, owner.name, ac.owner ORDER BY ac.tail_number

// The calculated influence ranking
MATCH (p:Person)-[m:PUPPET_MASTER_OF]->(e)
WHERE m.score >= 0.4
RETURN p.name, m.score, m.archetype, labels(e), e.name ORDER BY m.score DESC LIMIT 25
```

Schema, properties and idempotency rules: [docs/graph-schema.md](docs/graph-schema.md).

---

## CLI

```
python ingest.py [--sources a,b] [--limit N] [--fetch-only] [--dry-run] [--skip-nlp] [--skip-graph]
                 [--fail-on-error] [--log-level DEBUG|INFO|WARNING|ERROR] [--log-json]
                 [--report-dir DIR] [--run-id ID] [--max-runtime SECONDS] [--no-report]
                 [--doctor] [--list-sources] [--print-config] [--version]
```

`--fetch-only [SOURCES]` runs stage 1 alone — fetch, extract and report what each
source yielded, without touching spaCy or Neo4j. It is the cheapest way to check
credentials, relay reachability and source health:

```bash
python ingest.py --fetch-only wikidata,opencorporates --limit 25
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
pytest tests/ -q          # 740 tests, ~11s
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
| `test_net.py` | token bucket, delay queue, header rotation, relay + fallback client, form bodies |
| `test_sources.py` | adapter framework, registry invariants, RSS/Wikidata/OpenCorporates |
| `test_aviation.py` | FAA registry parsing, ADS-B lookups, flight-log manifests, co-travel edges |
| `test_graph.py` | client retries/batching, writer ordering, resolver, node budget |
| `test_pipeline.py` | harvest orchestration, dedupe, budgets, failure isolation, reports |
| `test_ingest_cli.py` | argument handling, doctor, exit codes, GitHub annotations |
| `test_e2e_dry_run.py` | full rehearsal with scripted HTTP |

`worker.js` has its own behavioural test, in plain Node with the upstream stubbed —
no wrangler, no network, no bindings:

```bash
node --check worker.js          # syntax
node tests/worker_smoke.mjs     # host profiles: SPARQL form POST, rate ceiling,
                                # credential injection, secret redaction, /health
```

---

## Repository layout

```
worker.js                     Cloudflare Worker: edge relay, host profiles, queue
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
    registry.py               the 14 registered sources and their weights
    base.py                   adapter framework: budget, dedupe, politeness, errors
    icij.py opencorporates.py wikidata.py registers.py rss.py adsb.py
    archive.py                monthly-dump freshness cache + delimited-row iterator
  graph/
    schema.py                 constraints, indexes, MERGE Cypher templates
    neo4j_client.py           retries, batching, dry-run recorder
    resolver.py               cross-run alias resolution
    writer.py                 ordered, idempotent, batched writes + node budget
    analytics.py              PUPPET_MASTER_OF scoring pass over the written graph
  domain.py                   domain labels (ShellCompany/Foundation/Aircraft), jurisdictions
  pipeline.py                 the daily run
.github/workflows/
  daily_ingest.yml            cron 04:00 UTC + manual dispatch
  ci.yml                      lint + test on push/PR
tests/                        740 offline tests + tests/worker_smoke.mjs (Node)
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
