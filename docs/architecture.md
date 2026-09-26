# Architecture

PuppetNET's ingestion path is a single daily job with three deployable pieces. This
document explains what each one does, how data flows between them, and why the
boundaries are where they are.

```
┌──────────────────────────── GitHub Actions runner (ubuntu-latest) ────────────────────────────┐
│                                                                                               │
│  ingest.py (CLI)                                                                              │
│     └── puppetnet.pipeline.IngestPipeline                                                     │
│            ├── config.Settings            env + config/sources.yaml overlay, validated        │
│            ├── graph.Neo4jClient          retries, batching, dry-run recorder                 │
│            ├── graph.GraphWriter          ordered MERGE writes, node budget                   │
│            ├── graph.EntityResolver       cross-run alias index                               │
│            ├── net.FetchClient            relay → queue → direct fallback, circuit breaker    │
│            │      ├── net.DelayQueue      per-host token buckets + global interval            │
│            │      └── net.HeaderFactory   fingerprint rotation per host/attempt               │
│            ├── sources.*                  6 adapters over 11 registered sources               │
│            │      └── sources.base        budget, dedupe, politeness, error accounting        │
│            └── parsing.NLPEngine          spaCy orchestration + degraded fallback             │
│                   ├── parsing.text_extract  HTML/PDF/JSON/CSV → prose                         │
│                   ├── parsing.craft         aircraft / vessel / vehicle identifiers           │
│                   └── parsing.relations     SVO → RelationType rule table                     │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
                 │ HTTPS (Authorization: Bearer …)
                 ▼
┌──────────────────────────── Cloudflare Worker (worker.js) ────────────────────────────────────┐
│  /health  /stats  /fetch  /tasks/{id}  /cache                                                 │
│   • fingerprint pool (UA ↔ sec-ch-ua ↔ platform kept coherent), rotated per attempt            │
│   • per-host token bucket: in-isolate memory, reconciled with RATE_LIMIT_KV                    │
│   • global requests/minute safety valve                                                        │
│   • robots.txt fetch + cache, crawl-delay honoured                                             │
│   • over budget → FETCH_QUEUE with a delay → 202 + task id → result in RESULT_KV               │
│   • SSRF guards: HTTPS-only, private networks blocked, host deny-list                          │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
        origin sites, registries and APIs
```

---

## 1. Entry point — `ingest.py`

A thin CLI over `IngestPipeline`: argument parsing, logging bootstrap, diagnostics
(`--doctor`, `--list-sources`, `--print-config`) and exit-code translation. It contains
no harvesting logic, which keeps the pipeline importable and unit-testable.

Exit codes are a contract with the workflow: `0` success, `1` configuration,
`2` runtime failure, `3` partial success. The workflow escalates `1`/`2` and treats `3`
as a warning so one flaky feed cannot fail a daily run.

## 2. Orchestration — `puppetnet/pipeline.py`

`IngestPipeline.run()` executes a fixed sequence:

1. **Graph init** — open the client, `verify()` (which also wakes a sleeping Aura
   free-tier instance), apply idempotent DDL, open an `:IngestRun` node.
2. **Network init** — build the delay queue, header factory and `FetchClient`.
3. **Source resolution** — filter the registry by `ENABLED_SOURCES` /
   `DISABLED_SOURCES` / `--sources`, merge the `config/sources.yaml` overlay, apply
   env-level source configuration (`RSS_FEEDS`, `ICIJ_DATASET_URLS`,
   `WIKIDATA_QUERIES`, `REGISTER_FILES`), sort **structured first**, upsert `:Source`
   nodes.
4. **NLP init** — only if an unstructured source is enabled *and* `NLP_ENABLED`; a
   structured-only run never pays the spaCy load cost.
5. **Dedupe index** — recent content hashes from the graph (`DEDUPE_WINDOW_DAYS`)
   plus `.state/content_hashes.json`, so a dry run still dedupes.
6. **Harvest loop** — per source: build an `AdapterContext`, run the adapter, parse
   unstructured documents, merge entities/relations, flush that source's slice to
   Neo4j, update counters. Three hard stops: the wall-clock budget
   (`MAX_RUNTIME_SECONDS`), the global document cap (`MAX_DOCUMENTS_TOTAL`) and
   SIGTERM/SIGINT (finish the current source, then stop).
7. **Close-out** — close the run node with final statistics, write
   `reports/<run_id>.json` + `.md`, publish `GITHUB_STEP_SUMMARY` / `GITHUB_OUTPUT`,
   release every resource.

**A failing source never aborts the run.** Adapter exceptions are caught, counted,
logged and reported per source; only an unreachable database or `--fail-on-error`
changes the exit status. That is the behaviour an unattended cron job needs.

## 3. Harvesting — `puppetnet/net` + `puppetnet/sources`

`FetchClient.request()` is the only egress path. Order of preference:

1. **Edge relay** (`PROXY_WORKER_URL`) — the Worker rotates fingerprints and applies
   its own per-host bucket. On `429`/`202` with `queue_on_limit`, the client accepts a
   task id and polls `GET /tasks/{id}` until the queue consumer finishes.
2. **Circuit breaker** — after `PROXY_CIRCUIT_BREAKER_THRESHOLD` consecutive relay
   failures the client stops routing through the Worker for a cooldown, then
   half-opens to probe recovery.
3. **Direct fallback** (`DIRECT_FALLBACK_ENABLED`) — the runner talks to the origin
   itself, throttled by the local `DelayQueue`.

Two special cases are deliberately *not* retried or escalated: a relay `403` with
`error.code == "robots_disallowed"` is terminal (recorded as `http_robots_blocked`),
and a `401/403` with an unparseable body is treated as a credential rejection.

`DelayQueue` keeps one token bucket per host plus a global minimum interval, so a slow
host cannot starve the others; failures escalate a host's backoff
(`max(retry_after, 2^consecutive_failures)`), and `429`/`503` cost at least 5 s.

`sources/base.py` gives every adapter the same skeleton: budget checks, document
caps, content-hash dedupe, provenance stamping (`source_id`, `source_weight`,
`doc_id`, `published_at`), per-source statistics and error accounting. A new source is
a `harvest()` generator plus a registry entry — nothing else.

Structured adapters (ICIJ, OpenCorporates, Wikidata, registers) attach resolved
`Entity`/`Relation` objects to their documents; unstructured adapters (RSS) attach only
text and let the NLP engine do the work.

## 4. Parsing — `puppetnet/parsing`

`text_extract.py` normalises HTML (boilerplate-stripped), PDF (pypdf), JSON, CSV/TSV
and plain text into prose plus metadata (title, author, language, publication date) and
reports whether the result is *usable* (≥ 12 words).

`nlp_engine.py` orchestrates spaCy:

* **Preferred path** — a statistical model with NER + dependency parser: entities come
  from the model, triples from the parse tree (`nsubj`/`dobj`/`nsubjpass`/`pobj`),
  priced `method = dependency`.
* **Degraded path** — no model available: a blank pipeline plus an `EntityRuler`
  (craft identifiers) and lexical/gazetteer spans; triples come from regex patterns and
  sentence-level co-occurrence, all priced `method = cooccurrence` (the 0.2 penalty).
  The backend string (`spacy:<model>` vs `spacy:blank+gazetteer` vs `pattern`) is
  recorded on every parse result and in the run report, so a degraded run is visible
  rather than silently worse.

`craft.py` is the CRAFT detector: registration patterns by country prefix, US tail
numbers, IMO/MMSI, IATA flight numbers gated on assigned airline codes, prefixed vessel
names (`MT`, `MV`, `SS`, …) and type appositives ("the superyacht *Amadea*"). Types are
context, not identity — a bare type word never becomes a node.

`relations.py` is the rule table that turns a triple into a predicate: 33 verb rules,
18 noun/apposition rules and 9 preposition rules covering 35 of the 38 predicates, with
passive flipping, negation rejection and hedge detection.

## 5. Graph — `puppetnet/graph`

`schema.py` holds the DDL and every Cypher template. Labels and relationship types are
**never** taken from data: entity labels come from the closed `EntityType` enum and
predicates from the closed `RelationType` enum, both re-checked by
`is_safe_relationship_type()` before interpolation.

`neo4j_client.py` wraps the driver with bounded pooling, explicit timeouts (Aura drops
idle sockets), exponential backoff with jitter on transient errors, immediate failure on
authentication errors, and `UNWIND` batching (`NEO4J_BATCH_SIZE`) so a 50k-row run stays
inside free-tier transaction limits. `dry_run` records statement kind, parameter *keys*
and row counts — never values — which is what makes the whole pipeline testable.

`writer.py` writes in dependency order (sources → documents → entities → mentions →
relationships) because each step `MATCH`es what the previous one created. Everything is
`MERGE`-based and keyed on deterministic ids, so re-running a day converges instead of
duplicating. It also enforces the AuraDB Free node budget: existing nodes always update,
new nodes are admitted strongest-first until `AURA_NODE_CAP` is reached, and refused
nodes (plus the edges that would point at them) are counted in the run summary.

`resolver.py` loads an alias index at the start of each run so `OJSC Rosneft` lands on
the node the graph already knows as `ROSNEFT`, instead of forking the graph.

---

## Design decisions worth knowing

| Decision | Why |
| --- | --- |
| Relay first, direct fallback second | IP rotation is the point of the Worker; but a Worker outage must not stop a daily harvest. |
| Structured sources harvested first | They carry weight 1.0 and no NLP cost; if the budget runs out, the most valuable data is already in the graph. |
| Per-source flush | A crash in source *n* leaves sources *1..n−1* written; memory stays flat on big runs. |
| Closed predicate vocabulary | Relationship types are interpolated into Cypher; an allowlist prevents both injection and schema explosion. |
| Noisy-OR merging, per-clause dedupe | Independent corroboration should raise confidence; two patterns reading one clause should not. |
| Degraded NLP is a warning, not an error | A missing model is a supported mode. Treating it as an error would fail every run in a fresh environment. |
| Dry-run as a first-class mode | The same code path runs in CI, on a laptop and in production; only the recorder differs. |
| Content-hash dedupe in graph *and* `.state` | The graph is authoritative, but a local cache keeps dry runs honest and survives a graph outage. |
