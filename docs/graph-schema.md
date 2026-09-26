# Graph schema

Everything the writer touches lives in [`puppetnet/graph/schema.py`](../puppetnet/graph/schema.py).
All writes are `MERGE`-based and keyed on deterministic identifiers, so re-running a day
— or replaying a failed one — converges instead of duplicating.

## Nodes

### `:Entity` + one secondary label

`:Person`, `:Organization`, `:Location`, `:Craft` (or `:Unknown`). The secondary label
comes from the closed `EntityType` enum, never from data, and is re-validated before it
is interpolated into Cypher.

| Property | Type | Notes |
| --- | --- | --- |
| `canonical_key` | string | **Unique.** `TYPE:slug-sha1[:8]` over the accent-folded, suffix-normalised name — stable across runs and readable in the browser. |
| `name` | string | Longest surface form seen. |
| `entity_type` | string | `Person` / `Organization` / `Location` / `Craft` / `Unknown`. |
| `aliases` | string[] | Every surface form observed, capped at 64. |
| `mention_count` | int | Accumulates across runs. |
| `confidence` | float | Noisy-OR of the best extraction confidences. |
| `source_ids` | string[] | Which sources asserted this entity (≤ 32). |
| `doc_ids` | string[] | Most recent documents (≤ 64). |
| `first_seen` / `last_seen` | ISO-8601 | |
| *flattened extras* | scalar / list | Nested maps are flattened (`registry.country` → `registry_country`) because Neo4j rejects map-valued properties. |

Typical extras by type:

* **Craft** — `craft_kind` (`Aircraft`/`Vessel`/`Ground`), `craft_type` (`superyacht`,
  `military transport aircraft`), `registry_country`, `registration`, `imo`, `mmsi`,
  `airline_code`, `flight_number`, `vessel_prefix`, `vin`.
* **Organization** — `wikidata_id`, `opencorporates_url`, `company_number`,
  `jurisdiction_code`, `jurisdiction`, `incorporation_date`, `dissolution_date`,
  `current_status`, `registered_address`, `industry`, `previous_names`, `lei`.
* **Person** — `wikidata_id`, `position`, `nationality`, `psc_kind`.
* **Location** — `jurisdiction_code`, `role` (e.g. `registered_address`).

### `:Document`

| Property | Notes |
| --- | --- |
| `doc_id` | **Unique.** Hash of `source_id` + URL (+ external id). |
| `source_id`, `url`, `title`, `author`, `content_type`, `language` | Provenance. |
| `content_hash` | SHA-256 of whitespace-normalised text — the dedupe key. Indexed. |
| `word_count`, `source_weight` | `source_weight` is 1.0 (structured) or 0.4 (unstructured). |
| `published_at`, `fetched_at`, `external_id` | |
| `first_ingested_at`, `last_ingested_at`, `ingest_count` | Set by the upsert's `ON MATCH` branch. |

### `:Source`

`source_id` (**unique**), `name`, `kind` (`structured`/`unstructured`), `confidence`
(1.0 / 0.4), `base_url`, `description`, `enabled`, `updated_at`.

### `:IngestRun`

`run_id` (**unique**), `started_at`, `finished_at`, `status`
(`running`/`completed`/`interrupted`/`failed`/`no_sources`), `dry_run`, `backend`,
`github_run_id`, `github_sha`, `github_workflow`, plus the full run statistics:
`documents_fetched`, `documents_skipped_duplicate`, `documents_failed`,
`sentences_processed`, `characters_processed`, `entities_extracted`, `entities_written`,
`entities_capped_node_budget`, `relations_extracted`, `relations_written`,
`relations_dropped_low_confidence`, `relations_capped_node_budget`,
`dependency_triples`, `cooccurrence_triples`, `craft_entities`, `http_requests`,
`http_via_worker`, `http_direct_fallback`, `http_rate_limited`,
`http_deferred_to_queue`, `http_robots_blocked`, `http_errors`, `seconds_throttled`,
`duration_seconds`, `error_count`.

## Relationships

### Structural

| Relationship | Direction | Properties |
| --- | --- | --- |
| `FROM_SOURCE` | `(:Document)-[]->(:Source)` | `first_seen`, `last_seen` |
| `PROCESSED` | `(:IngestRun)-[]->(:Document)` | — |
| `MENTIONS` | `(:Document)-[]->(:Entity)` | `count`, `confidence` (noisy-OR), `surface_forms[]` (≤ 8), `first_offset`, `first_seen`, `last_seen` |
| `HANDLED` | `(:IngestRun)-[]->(:Source)` | `documents`, `entities`, `relations`, `errors` |

### Semantic (entity → entity)

One closed vocabulary of 38 predicates — `RelationType`. Relationship types are
interpolated into Cypher, so `is_safe_relationship_type()` gates every write and
unknown values are coerced to `ASSOCIATED_WITH` rather than emitted.

| Group | Predicates |
| --- | --- |
| Ownership & control | `OWNS`, `OWNED_BY`, `CONTROLS`, `SUBSIDIARY_OF`, `PARENT_OF`, `ACQUIRED`, `SHAREHOLDER_OF`, `INTERMEDIARY_FOR` |
| Roles & employment | `DIRECTOR_OF`, `OFFICER_OF`, `EMPLOYED_BY`, `EMPLOYS`, `MEMBER_OF`, `FOUNDED`, `APPOINTED_BY` |
| Money | `FUNDED`, `FUNDED_BY`, `INVESTED_IN`, `PAID_TO`, `CONTRACTED_WITH`, `TRANSFERRED_TO` |
| Place | `LOCATED_IN`, `REGISTERED_IN`, `NATIONAL_OF`, `OPERATES_IN` |
| Movement & craft | `TRAVELED_WITH`, `TRAVELED_TO`, `OPERATES`, `REGISTERED_TO`, `ARRIVED_FROM` |
| Personal & social | `MET_WITH`, `FAMILY_OF`, `AFFILIATED_WITH` |
| Adversarial | `SANCTIONED_BY`, `INVESTIGATED_BY`, `ACCUSED_OF`, `LINKED_OFFSHORE` |
| Fallback | `ASSOCIATED_WITH` |

Edge properties:

| Property | Notes |
| --- | --- |
| `confidence` | Noisy-OR across observations: `1 − (1 − a)(1 − b)`. |
| `source_weight` | Highest weight seen (a structured sighting upgrades a news edge). |
| `method` | `structured` / `dependency` / `pattern` / `gazetteer` / `cooccurrence`. `dependency` wins over `cooccurrence` on merge. |
| `evidence` | string[] — the last five supporting snippets. |
| `evidence_scores` | float[] — parallel to `evidence`. |
| `verb`, `rule` | The surface verb and the rule that fired (`verb:owns`, `noun:director`, `structured:wikidata`). |
| `observations` | How many independent sightings merged into this edge. |
| `negated`, `hedged`, `passive` | Clause flags carried from the parse. |
| `source_id`, `doc_id`, `run_id` | Provenance. |
| `first_seen`, `last_seen` | ISO-8601. |

## Constraints & indexes

Created on boot by `ensure_schema_statements()` (each `IF NOT EXISTS`; a statement the
server rejects is logged and skipped so a 4.x instance cannot break a run):

```
CONSTRAINT puppetnet_entity_key    FOR (e:Entity)     REQUIRE e.canonical_key IS UNIQUE
CONSTRAINT puppetnet_document_id   FOR (d:Document)   REQUIRE d.doc_id IS UNIQUE
CONSTRAINT puppetnet_source_id     FOR (s:Source)     REQUIRE s.source_id IS UNIQUE
CONSTRAINT puppetnet_run_id        FOR (r:IngestRun)  REQUIRE r.run_id IS UNIQUE

INDEX puppetnet_entity_name/type/confidence/aliases
INDEX puppetnet_person_name, puppetnet_org_name, puppetnet_location_name, puppetnet_craft_name
INDEX puppetnet_document_hash/source/published/fetched
INDEX puppetnet_run_started

FULLTEXT puppetnet_entity_search   FOR (e:Entity)   ON EACH [e.name, e.aliases]
FULLTEXT puppetnet_document_search FOR (d:Document) ON EACH [d.title, d.url]
```

## Write order

`persist()` runs four steps in this order because each one `MATCH`es what the previous
created:

```
documents  →  entities  →  mentions  →  relationships
```

`upsert_sources()` runs before documents, and `begin_run()`/`finish_run()` bracket the
whole run. Rows are grouped so each statement carries exactly one entity label or one
relationship type, then batched with `UNWIND` at `NEO4J_BATCH_SIZE` rows.

## Node budget (AuraDB Free)

AuraDB Free stops accepting writes around 200k nodes. Before each entity flush the
writer probes `MATCH (e:Entity) RETURN count(e)` once per run and:

* always writes entities whose `canonical_key` the alias index already knows (updating a
  node costs nothing against the ceiling);
* admits newcomers strongest-first (confidence, then mention count) while headroom
  remains;
* refuses the rest, counts them in `summary.entities_capped`, and drops any edge that
  would point at a refused node (`relations_capped`) — a dangling `MATCH` would silently
  write nothing, so the loss is recorded instead.

`AURA_NODE_CAP=0` disables the guard; an unreadable count degrades to "no cap" rather
than "write nothing".

## Useful queries

```cypher
// Full-text entity search across names and aliases
CALL db.index.fulltext.queryNodes('puppetnet_entity_search', 'Gazprom~') YIELD node, score
RETURN node.name, labels(node), node.confidence, score LIMIT 20

// Everything known about one actor, with provenance
MATCH (e:Entity {name: 'Igor Sechin'})
OPTIONAL MATCH (e)-[r]-(other:Entity)
OPTIONAL MATCH (d:Document)-[m:MENTIONS]->(e)
RETURN e, collect(DISTINCT {rel: type(r), other: other.name, confidence: r.confidence,
                           evidence: r.evidence}) AS edges,
       collect(DISTINCT {doc: d.url, count: m.count}) AS documents

// Network around a craft identifier
MATCH (c:Craft) WHERE c.imo IS NOT NULL OR c.registration IS NOT NULL
MATCH (c)-[r]-(n:Entity)
RETURN c.name, c.registration, c.imo, type(r), n.name, r.confidence

// Run health over time
MATCH (r:IngestRun) RETURN r.run_id, r.started_at, r.status, r.documents_fetched,
       r.entities_written, r.relations_written, r.error_count
ORDER BY r.started_at DESC LIMIT 14

// Which sources actually corroborate each other?
MATCH (a:Entity)-[r]->(b:Entity) WHERE r.observations > 1
RETURN a.name, type(r), b.name, r.observations, r.confidence, r.method
ORDER BY r.observations DESC LIMIT 50
```
