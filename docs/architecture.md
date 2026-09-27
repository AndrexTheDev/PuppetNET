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
│            ├── graph.Analytics            PUPPET_MASTER_OF scoring (post-write hook)          │
│            ├── domain                     labels, shell risk, addresses, jurisdictions        │
│            ├── net.FetchClient            relay → queue → direct fallback, circuit breaker    │
│            │      ├── net.DelayQueue      per-host token buckets + global interval            │
│            │      └── net.HeaderFactory   fingerprint rotation per host/attempt               │
│            ├── sources.*                  9 adapters over 14 registered sources               │
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
│   • host profiles: per-API rate ceiling, API-mode UA, credentials at egress                    │
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

The one piece of logic it does own is the **fetcher layer** behind `--fetch-only`:
modular, per-source functions (`icij_leaks`, `faa_registry`, `wikidata`,
`opencorporates`, `adsb_exchange`, `flight_logs`, `news`) that run stage 1 alone —
harvest and extract, then report what each source yielded — without importing spaCy or
opening a Neo4j connection (`dry_run=True`, no client is ever constructed). It is the
cheapest way to check credentials, relay reachability and source health, and it is what
`ingest.py --doctor` reuses for its registry and credential probes.

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
7. **Analytics pass** — after the final flush and *before* the run is closed,
   `graph/analytics.py` scores every person it can see and writes the calculated
   `:PUPPET_MASTER_OF` layer plus `Person.risk_score`. It is a hook, not a stage: when
   `ANALYTICS_ENABLED=false` it reports `{"status": "disabled"}`, when it raises it
   reports `{"status": "failed", "error": …}`, records an `analytics` error and the run
   still exits `0`.
8. **Close-out** — close the run node with final statistics, write
   `reports/<run_id>.json` + `.md`, publish `GITHUB_STEP_SUMMARY` / `GITHUB_OUTPUT`,
   release every resource.

**A re-read document is not new evidence.** The writer keeps a per-edge `doc_ids` ledger
and a per-entity document list, and merges `confidence`/`observations` (edges) and
`confidence`/`mention_count` (entities) only for a document the node has not counted. The
verdict is computed in Python and travels into Cypher as `row.is_new`, because the
evaluation order of items inside one Cypher `SET` is undocumented — see
[graph schema → new evidence vs. a re-read](graph-schema.md#new-evidence-vs-a-re-read).

**A failing source never aborts the run.** Adapter exceptions are caught, counted,
logged and reported per source; only an unreachable database or `--fail-on-error`
changes the exit status. That is the behaviour an unattended cron job needs.

## 3. Harvesting — `puppetnet/net` + `puppetnet/sources`

`FetchClient.request()` is the only egress path. Order of preference:

1. **Edge relay** (`PROXY_WORKER_URL`) — the Worker applies its **host profiles**
   (per-API politeness ceiling, API-mode presentation, credential injection, SPARQL form
   defaults) and rotates fingerprints for everything else. On `429`/`202` with
   `queue_on_limit`, the client accepts a task id and polls `GET /tasks/{id}` until the
   queue consumer finishes. See [docs/edge-relay.md](edge-relay.md#host-profiles).
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

The registry entry also declares a **cadence** (`hourly`, `daily` or `weekly`), and
`specs_for_tier(tier)` turns that into the set a scheduled run may harvest. The tier is the
outer bound: `--sources` narrows it and cannot widen it, an unknown tier raises instead of
falling back to "all", and `tests/test_schedule.py` pins both the registry and the
workflows that call it. See
[operations → the tiered cadence](operations.md#the-tiered-cadence) for the rationale and the
per-tier budgets.

Structured adapters (ICIJ, OpenCorporates, Wikidata, registers, FAA registry, ADS-B,
flight logs) attach resolved `Entity`/`Relation` objects to their documents; unstructured
adapters (RSS) attach only text and let the NLP engine do the work.

Three details of the structured adapters are worth knowing:

* **Wikidata** sends SPARQL as a `POST` form body, not a GET query string — a real
  foundation-board query is longer than most proxies allow in a URL. Eleven named queries
  are implemented, including `foundation_trustees` (P3320 board members, P488 chair,
  P112 founder, P169 CEO over the Wikidata Foundation, Ford Foundation and Open Society
  Foundations), and boards can be *linked*: two people on the same organisation also get
  a `Person-[:ASSOCIATED_WITH]->Person` edge carrying the shared organisation as
  evidence. That is quadratic in board size, so a board larger than
  `max_shared_org_members` (24) is skipped rather than turned into 80k edges.
* **FAA registry** streams the monthly Releasable Aircraft ZIP directly — it is far too
  large to buffer through a Worker — and caches it per URL for `refresh_days`, because
  re-parsing a monthly dump every day wastes the whole run. Registrants sharing one
  address become `Person-[:SHARES_ADDRESS]->Person` clusters, which is how a mail drop
  fronting five owners shows up.
* **Flight logs** parse tabular manifests from the *raw* text before any readability
  pass, because `extract_text` destroys CSV structure; co-passengers on one flight get
  `TRAVELED_WITH`.

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
18 noun/apposition rules and 9 preposition rules covering 35 of the 46 predicates, with
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

`../domain.py` is the analysis vocabulary that sits between extraction and the graph:
`domain_labels()` decides which of `:Company`, `:ShellCompany`, `:Foundation`,
`:Offshore`, `:Aircraft`, `:Vessel`, `:Vehicle` a node earns from its evidence,
`shell_risk()` returns a score *and the reasons* behind it, `jurisdiction_class()` tiers
secrecy jurisdictions, `normalize_address()`/`address_key()` make two spellings of one
address comparable, and `entity_row_extras()` stamps `alias`, `risk_score`,
`tail_number`, `owner`, `address`, `gps` onto the rows the writer emits. Labels are
`SET`/`REMOVE`d after the merge — never part of the merge key — so classification can
change between runs without forking nodes.

`analytics.py` is the calculated layer. It derives a neighbourhood graph from the
relations the run produced (there is no separate entity pool to borrow), scores each
person with fixed component weights — `control_breadth` 0.30, `opacity` 0.25,
`layering` 0.20, `convergence` 0.10, `adversarial` 0.10, `movement` 0.05 — decaying a
chain by `0.7` per hop out to four hops, and writes the survivors as
`(:Person)-[:PUPPET_MASTER_OF {score}]->(:Entity)` plus `Person.risk_score`. Stale
calculated edges are pruned after `ANALYTICS_PRUNE_DAYS`.

---

## Design decisions worth knowing

| Decision | Why |
| --- | --- |
| Relay first, direct fallback second | IP rotation is the point of the Worker; but a Worker outage must not stop a daily harvest. |
| Host profiles pin politeness, not the client | A hand-run loop or a bad config must not be able to hammer Wikidata into a project-wide ban. The Worker takes `min(client, profile)`, so a caller can be slower but never faster. |
| API hosts get an honest UA, not a rotating fingerprint | Wikidata, OpenCorporates and RapidAPI publish "identify your tool" policies. A Cloudflare Worker's IP range is shared, so spoofing a browser there gets *everyone* blocked. Rotation stays on for real websites. |
| Credentials injected at the edge, per call | A token in a Queue message, a KV cache key or a response body is a token in a log. Injecting inside `performFetch` and redacting the URL keeps secrets off the runner and out of the report. |
| Domain labels are `SET`, never `MERGE`d | `:ShellCompany` is evidence-dependent and changes between runs; including it in the merge key would miss yesterday's node and violate the uniqueness constraint. |
| `weight` and `confidence` are separate numbers | "How much does this kind of tie matter" and "how sure are we about this tie" are different questions, and the useful queries filter on one while sorting by the other. |
| Analytics is a hook that cannot fail a run | A calculated layer is an opinion about the graph. Losing it is a bad day; losing the harvest because of it is a bad week. |
| Structured sources harvested first | They carry weight 1.0 and no NLP cost; if the budget runs out, the most valuable data is already in the graph. |
| Per-source flush | A crash in source *n* leaves sources *1..n−1* written; memory stays flat on big runs. |
| Closed predicate vocabulary | Relationship types are interpolated into Cypher; an allowlist prevents both injection and schema explosion. |
| Noisy-OR merging, per-clause dedupe | Independent corroboration should raise confidence; two patterns reading one clause should not. |
| Degraded NLP is a warning, not an error | A missing model is a supported mode. Treating it as an error would fail every run in a fresh environment. |
| Dry-run as a first-class mode | The same code path runs in CI, on a laptop and in production; only the recorder differs. |
| Content-hash dedupe in graph *and* `.state` | The graph is authoritative, but a local cache keeps dry runs honest and survives a graph outage. |
