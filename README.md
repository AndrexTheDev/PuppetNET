# PuppetNET — serverless OSINT harvest & NLP graph engine

PuppetNET is a **serverless OSINT network-analysis application**: a daily pipeline that
harvests corporate-registry, offshore-leaks and news sources, extracts **people,
organisations, locations and craft (aircraft / vessels / vehicles)** with spaCy, resolves
them into **weighted relationships**, writes an idempotent property graph into **Neo4j
AuraDB**, keeps that graph true — and then lets an analyst read it in a browser.

There is no server to run. Six moving parts:

| Part | File | Runs on |
| --- | --- | --- |
| Edge fetch relay **and graph read API** (IP rotation, rate limiting, deferral queue, `/graph/*`) | [`worker.js`](worker.js) | Cloudflare Workers |
| Harvest + NLP + graph writer | [`ingest.py`](ingest.py) → [`puppetnet/`](puppetnet) | GitHub Actions runner |
| Graph maintenance (entity resolution, pruning, centrality, anomaly scoring) | [`graph_analytics.py`](graph_analytics.py) | GitHub Actions, after the ingest |
| Alerting (cluster-bridge push, daily digest) | [`telegram_bot.py`](telegram_bot.py) | GitHub Actions, after maintenance |
| **Analyst console** (search, graph canvas, handshake, evidence table) | [`web/`](web) | Cloudflare Pages — static, no build step |
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

### 6. Open the console

```bash
npm ci && npm run serve       # http://localhost:8080
```

It boots offline on a bundled demo dataset — 52 invented entities, 88 invented ties — so
the canvas, the filters, the handshake view and the evidence table are all usable before
anything is deployed. To point it at a real graph, press `S`, choose **Cloudflare Worker**,
and give it the Worker URL plus a token:

```bash
wrangler secret put GRAPH_API_TOKEN       # openssl rand -hex 16
wrangler deploy
npx wrangler pages deploy web --project-name=puppetnet-console
```

The console is a static upload: `web/` with **no build command**, because the Tailwind CSS
is compiled in CI and committed and the graph engine is vendored with its licences. Guide:
[docs/web-console.md](docs/web-console.md).

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

## Graph maintenance & alerting

Harvesting grows the graph; two tools keep it true and keep it inside the AuraDB
Free budget (200 000 nodes / 400 000 relationships). They run after the ingest,
in [`.github/workflows/graph_maintenance.yml`](.github/workflows/graph_maintenance.yml).

### Entity resolution (`graph_analytics.py --dedupe`)

Two paths, and a guard that sits in front of the second one:

| Path | Evidence | Context required |
| --- | --- | --- |
| **Strict** | Exact match on a globally unique identifier — `reg_number`, `company_number`, `lei`, `imo`, `mmsi`, `transponder`, `icao24`, `tail_number`, `wikidata_id`, `wikipedia_id`, `opencorporates_url` | No |
| **Fuzzy** | Jaro-Winkler / Levenshtein ≥ `DEDUPE_FUZZY_THRESHOLD` (0.88) | Only for generic, common or reordered names |

The **homonym guard** is the part that matters for OSINT. A fuzzy pair merges on
spelling alone only when the name is distinctive. It must additionally share one
contextual attribute when the name is generic (`Global Holdings Ltd`), held by
more than six nodes, a single ambiguous token (`Smith`), or a token-only match
(`Ivan Petrov` / `Petrov Ivan` — same words, possibly different people). Accepted
context is a shared normalised address, a shared strict identifier, or both names
appearing within 50 words of the same document in the last 90 days. Blocked pairs
are not dropped silently: they are reported as `homonyms_protected` with the
reason, so a threshold change can be reviewed against real cases.

Merging is transactional in shape: mentions are repointed first, then every
relationship type the loser held, then properties are folded into the winner
(aliases, source ids and doc ids unioned; confidence combined by noisy-OR;
`merged_from` records the absorbed keys), and only then is the loser deleted.
Candidates are generated by blocking — first characters, sorted token initials,
sorted token set, and an inverted token index — never by comparing all pairs.

### Capacity pruning (`graph_analytics.py --prune`)

An orphan is a node with ≤ `PRUNE_ORPHAN_MAX_DEGREE` (1) semantic ties whose
weakest tie scores below `PRUNE_ORPHAN_MAX_WEIGHT` (0.3) and which has not been
touched for `PRUNE_ORPHAN_MIN_AGE_DAYS` (180) days. Two protections override the
rule: a `risk_score` ≥ `PUPPET_MASTER_MIN_SCORE`, and an offshore/shell label with
at least one tie — those nodes *are* the findings. When utilisation passes
`PRUNE_CAPACITY_TARGET` (0.85) the rule escalates to degree ≤ 2, weight < 0.4 and
90 days, and `escalated: true` in the report says so. Documents left with no
`MENTIONS` edge go too. `PRUNE_MAX_DELETIONS` caps every run.

### Centrality & anomaly scoring (`graph_analytics.py --centrality`)

```
score = 0.40·betweenness + 0.35·degree_spike(24h) + 0.25·offshore_cluster_ratio
```

AuraDB Free has no GDS, so the engine probes `gds.version()` and otherwise uses
the bundled pure-Python implementations — Brandes betweenness (O(V·E), with a
wall-clock budget that rescales a partial result instead of abandoning it),
deterministic label propagation for clusters, and iterative Tarjan for
articulation points. No extra dependency, and `CENTRALITY_ENGINE=gds|python`
forces either path. Every score is written back to the node (`betweenness`,
`degree_spike`, `offshore_cluster_ratio`, `anomaly_score`, `cluster_id`,
`metrics_at`) as flat properties — Neo4j rejects map-valued ones — and stale
metrics are cleared after `ANALYTICS_PRUNE_DAYS`.

### Telegram alerting (`telegram_bot.py`)

* **Bridge alert (near real time).** A node first seen inside the alert window
  that is an articulation point joining two clusters which had no path between
  them before. Each alert carries a suppression key
  (`bridge:<canonical_key>:<cluster pair>`), so the same bridge is announced once
  per `TELEGRAM_SUPPRESSION_HOURS` — but the *same node* bridging a *new* pair of
  clusters is new information and gets its own alert.
* **Daily digest.** The `TELEGRAM_DIGEST_TOP_N` highest anomaly scores from the
  last 24 h, at most once per chat per UTC day.

State lives in `.state/telegram_alerts.json`; delete it to re-send everything.
The bot token is a credential: it is scrubbed from every log record by a filter
(`FetchClient` logs the URL it gave up on, and the token is in the URL), it is
never routed through the edge relay, and it appears in no report or state file.

---

## Web console

[`web/`](web) is the reading half: a dark, keyboard-driven single-page console for the
graph, in plain HTML + JavaScript + Tailwind + Cytoscape.js. No framework, no build step
at deploy time, no CDN and no paid dependency — every byte the browser loads is committed,
so it deploys to Cloudflare Pages as a static directory and works from a USB stick.

| Module | What it does |
| --- | --- |
| Search-first header | Instant querying of names, aliases, company numbers, canonical keys, jurisdictions and aircraft tails. Locally scored suggestions first, then the API. `Enter` loads the best match, `Shift+Enter` all of them, `Alt+Enter` opens them in the table |
| Graph canvas | Force-directed (fcose) 2D graph: zoom, pan, pin, click-to-inspect, double-click to expand, right-click quick actions. **Node size is betweenness centrality** (or anomaly, degree, mentions, confidence, risk, flat), colour is the entity label or the `cluster_id`, edges are grouped and filterable by type, weight and confidence |
| Neighbourhood engine | N-degree expansion, **1–4 hops**, around the active node — plus *isolate*, which drops everything outside that neighbourhood. Capped by a node ceiling, and truncation is reported rather than hidden |
| Handshake (pathfinding) | Entity A → Entity B renders the shortest connection chain with each tie's predicate, weight, confidence, method and citation, the weakest tie, up to three alternatives, and the Cypher that reproduces it. Hops ≤ 12, cost by hops / inverse weight / inverse confidence, direction undirected / outgoing / incoming |
| Evidence table | Nodes, edges or sources as filterable, sortable, paged rows; sync the filtered rows back to the canvas; export CSV (RFC 4180, with formula-injection defence) |
| Deep links | `#/v=table&q=kastelion&focus=PERSON:…&depth=2&metric=degree` — a shared link opens exactly what the sender saw. Credentials are never written to a URL, and never read from one |

It runs in three modes: **demo** (the bundled synthetic network, offline), **worker**
(`/graph/*` on `worker.js` — production, because the Worker holds the database
credentials) and **neo4j** (direct HTTP, local development only).

The console is a reader: it only ever issues `GET`s, the graph API refuses any other
method, and both smoke suites assert that no statement reaching Neo4j contains a write
clause. Harvested text is escaped everywhere it is rendered, a strict CSP forbids inline
script, `X-Frame-Options: DENY` stops a *hide node* button being clickjacked, and the
Cypher it offers for copy-paste takes its ordering metric from a fixed allowlist. See
[docs/web-console.md](docs/web-console.md#hardening).

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

```
python graph_analytics.py [--all] [--dedupe] [--prune] [--centrality] [--bridges] [--capacity]
                          [--dry-run] [--threshold 0.88] [--max-merges N] [--max-nodes N]
                          [--top N] [--engine auto|gds|python] [--weights w1,w2,w3]
                          [--report-dir DIR] [--no-report] [--run-id ID] [--json]
                          [--log-level L] [--log-json] [--version]
```

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Configuration error (bad credentials, unparsable `--weights`) |
| `2` | Runtime failure (Neo4j unreachable, every stage failed) |
| `3` | Partial — at least one stage failed, the others completed |

```
python telegram_bot.py [--all] [--bridge] [--digest] [--test]
                       [--report PATH] [--report-dir DIR] [--top N] [--bridge-limit N]
                       [--window-hours 24] [--chat-id IDS] [--token TOKEN] [--force]
                       [--dry-run] [--state PATH] [--json] [--version]
```

| Exit code | Meaning |
| --- | --- |
| `0` | Delivered, or nothing to deliver |
| `1` | Bad arguments |
| `2` | At least one delivery failed |
| `3` | Configuration or report problem (no token, no chat, no report) |

`--dry-run` renders every message to stdout and touches neither the API nor the
suppression ledger, so a preview never consumes an alert. `--test` verifies the
token with `getMe` and sends one "engine online" message to the first chat.
With `--json`, all logging moves to stderr and stdout is a single parseable
document.

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
pytest tests/ -q          # 928 tests, ~9s
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
| `test_graph_analytics.py` | similarity metrics, homonym guard, Brandes/label-propagation/Tarjan, pruning protections, anomaly formula, bridge rule, CLI |
| `test_telegram_bot.py` | HTML escaping, chunking, token redaction, suppression ledger, 429/`retry_after`, digest policy, CLI |
| `test_pipeline.py` | harvest orchestration, dedupe, budgets, failure isolation, reports |
| `test_ingest_cli.py` | argument handling, doctor, exit codes, GitHub annotations |
| `test_e2e_dry_run.py` | full rehearsal with scripted HTTP |

`worker.js` has its own behavioural test, in plain Node with the upstream stubbed —
no wrangler, no network, no bindings:

```bash
node --check worker.js          # syntax
node tests/worker_smoke.mjs     # 32 checks: host profiles (SPARQL form POST, rate
                                # ceiling, credential injection, secret redaction)
                                # and the whole /graph/* read API
```

The console is tested the same way — the **shipped** `index.html`, `app.js` and vendored
Cytoscape booted inside jsdom, with the real `worker.js` behind a fake Neo4j:

```bash
node tests/web_smoke.mjs        # 24 checks, ~19 s: offline boot and render, escaping on
                                # every surface, sizing/colour, filters, expansion,
                                # handshake, table + CSV, keyboard, deep links, worker
                                # mode, offline fallback, credentials
npm test                        # syntax check + both smoke suites
```

Both suites share `tests/helpers/fake_neo4j.mjs`, a stubbed Neo4j over a six-entity
fixture, so a browser-side assertion and a Worker-side assertion are made against the
same graph. Neither touches the network: they cannot pass because a third-party API
happened to be reachable.

---

## Repository layout

```
worker.js                     Cloudflare Worker: edge relay, host profiles, queue
wrangler.toml                 Worker bindings, politeness tunables, cron
ingest.py                     CLI entry point (also the workflow's command)
graph_analytics.py            maintenance CLI: dedupe, prune, centrality, bridges
telegram_bot.py               alert CLI: cluster-bridge push + daily digest
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
                              (maintenance reads/writes through neo4j_client too)
  domain.py                   domain labels (ShellCompany/Foundation/Aircraft), jurisdictions
  pipeline.py                 the daily run
web/
  index.html                  console shell: search, canvas, rail, inspector, dialogs
  app.js                      providers, rendering, filters, pathfinding, table, export
  styles.css                  dark theme, glow, animations, responsive rules
  _headers                    CSP, framing and cache policy (Cloudflare Pages)
  tailwind.config.cjs         content globs for the compiled, committed CSS
  vendor/                     cytoscape + fcose + layout-base + cose-base + tailwind.css,
                              four MIT licence texts and an inventory README
scripts/vendor-libs.mjs       reproduces web/vendor/ from node_modules, strictly
package.json                  dev tooling only: jsdom, tailwindcss, terser
.github/workflows/
  daily_ingest.yml            cron 04:00 UTC + manual dispatch
  graph_maintenance.yml       after the ingest: dedupe/prune/centrality + alerts
  ci.yml                      python, worker and web jobs on push/PR
  pages_deploy.yml            verify, then upload web/ to Cloudflare Pages on main
tests/                        928 offline tests, worker_smoke.mjs (32), web_smoke.mjs (24)
                              and helpers/fake_neo4j.mjs, the stub both suites share
docs/                         architecture, configuration, schema, NLP, relay, console,
                              operations
```

---

## Documentation

* [docs/architecture.md](docs/architecture.md) — components, data flow, design decisions
* [docs/configuration.md](docs/configuration.md) — every setting, env var and YAML key
* [docs/graph-schema.md](docs/graph-schema.md) — nodes, relationships, Cypher templates
* [docs/nlp-pipeline.md](docs/nlp-pipeline.md) — extraction stages, rules, degradation
* [docs/edge-relay.md](docs/edge-relay.md) — `worker.js` HTTP contract, graph read API, deployment
* [docs/web-console.md](docs/web-console.md) — the analyst console: UI, deep links, hardening, Pages
* [docs/operations.md](docs/operations.md) — running, monitoring and troubleshooting

## Licence

MIT — see [LICENSE](LICENSE).
