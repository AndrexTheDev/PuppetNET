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
| `mention_count` | int | How many mentions this entity has, counted **once per document** — a re-read of a known document never increments it. |
| `confidence` | float | Noisy-OR of the best extraction confidences, merged only for documents the node has not counted yet. |
| `source_ids` | string[] | Which sources asserted this entity (≤ 32). |
| `doc_ids` | string[] | The documents that carry this entity (≤ 64), kept as a union so a re-read cannot evict real provenance. |
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

### Domain labels

On top of the type label, [`puppetnet/domain.py`](../puppetnet/domain.py) stamps
**evidence-derived** labels onto the same node:

```
:Company · :ShellCompany · :Foundation · :Offshore
:Aircraft · :Vessel · :Vehicle
```

They are `SET`/`REMOVE`d after the `MERGE`, never part of it: the merge key is
`:Entity:{entity_type}` only, because these labels change as evidence accumulates and a
merge that included them would miss yesterday's node and violate the uniqueness
constraint instead. Two families are **mutually exclusive** — `(:Company, :Foundation)`
and `(:Aircraft, :Vessel, :Vehicle)` — so joining one removes its siblings and the
classification converges instead of accumulating.

| Label | Carries | Derived from |
| --- | --- | --- |
| `:Person` | `alias`, `risk_score` | longest alias; the analytics pass |
| `:Company` / `:Foundation` | `jurisdiction`, `reg_number`, `country` | registry fields, Wikidata `countryLabel` |
| `:ShellCompany` | `shell_risk`, `shell_risk_reasons[]`, `is_shell` | `shell_risk() ≥ 0.5` |
| `:Offshore` | `jurisdiction_class` | `jurisdiction_class()` secrecy tiers |
| `:Aircraft` | `tail_number`, `owner`, `craft_kind` | FAA registry, ADS-B, CRAFT detection |
| `:Location` | `address`, `address_key`, `gps` | normalised address string |

`shell_risk_reasons` records *why* a company was flagged (no website, mailbox address,
secrecy jurisdiction, nominee officers, …) so a label can be audited rather than trusted.

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

One closed vocabulary of 46 predicates — `RelationType`. Relationship types are
interpolated into Cypher, so `is_safe_relationship_type()` gates every write and
unknown values are coerced to `ASSOCIATED_WITH` rather than emitted.

| Group | Predicates |
| --- | --- |
| Ownership & control | `OWNS`, `OWNED_BY`, `CONTROLS`, `SUBSIDIARY_OF`, `PARENT_OF`, `ACQUIRED`, `SHAREHOLDER_OF`, `INTERMEDIARY_FOR`, `NOMINEE_OF`, `BENEFICIARY_OF` |
| Roles & employment | `DIRECTOR_OF`, `OFFICER_OF`, `EMPLOYED_BY`, `EMPLOYS`, `MEMBER_OF`, `FOUNDED`, `APPOINTED_BY` |
| Foundations & boards | `TRUSTEE_OF` |
| Money | `DONATED_TO`, `FUNDED`, `FUNDED_BY`, `INVESTED_IN`, `PAID_TO`, `CONTRACTED_WITH`, `TRANSFERRED_TO` |
| Place | `LOCATED_IN`, `REGISTERED_IN`, `NATIONAL_OF`, `OPERATES_IN`, `SHARES_ADDRESS` |
| Movement & craft | `PASSENGER_ON`, `TRAVELED_WITH`, `TRAVELED_TO`, `OPERATES`, `REGISTERED_TO`, `ARRIVED_FROM` |
| Personal & social | `MET_WITH`, `FAMILY_OF`, `AFFILIATED_WITH`, `MENTIONED_WITH` |
| Adversarial | `SANCTIONED_BY`, `INVESTIGATED_BY`, `ACCUSED_OF`, `LINKED_OFFSHORE` |
| Calculated | `PUPPET_MASTER_OF` |
| Fallback | `ASSOCIATED_WITH` |

Edge properties:

| Property | Notes |
| --- | --- |
| `weight` | **How much this kind of relationship matters**, from `DOMAIN_EDGE_WEIGHTS` — `CONTROLS` 1.0, `TRUSTEE_OF` 0.9, `SHARES_ADDRESS`/`PASSENGER_ON` 0.8, `DONATED_TO`/`LOCATED_IN` 0.7, `TRAVELED_WITH` 0.6, `MENTIONED_WITH` 0.4, `ASSOCIATED_WITH` 0.3. Adapters never set it by hand (`Relation.weight` falls back to the table, so the vocabulary cannot drift per source); only the analytics pass overrides it with a computed score. |
| `confidence` | **How sure we are** — noisy-OR across *independent observations*: `1 − (1 − a)(1 − b)`. Merged only when the observation is new evidence — see below. |
| `source_weight` | Highest weight seen (a structured sighting upgrades a news edge). |
| `method` | `structured` / `dependency` / `pattern` / `gazetteer` / `cooccurrence`. `dependency` wins over `cooccurrence` on merge. |
| `evidence` | string[] — the last five supporting snippets. |
| `evidence_scores` | float[] — parallel to `evidence`. |
| `verb`, `rule` | The surface verb and the rule that fired (`verb:owns`, `noun:director`, `structured:wikidata`). |
| `observations` | How many independent sightings merged into this edge — one per new document, never one per re-read. |
| `doc_ids` | string[] | The documents that asserted this edge (≤ 20). This is the evidence ledger the merge consults. |
| `negated`, `hedged`, `passive` | Clause flags carried from the parse. |
| `source_id`, `doc_id`, `run_id` | Provenance. |
| `first_seen`, `last_seen` | ISO-8601. |

Two numbers, two questions: `weight` asks *how much does this kind of tie matter*, and
`confidence` asks *how sure are we about this particular tie*. Sorting by one and
filtering by the other is the normal way to query the graph.

#### New evidence vs. a re-read

`confidence` may only rise when a *new document* asserts the edge. The reasoning is the
whole point of the ledger:

* `noisy_or` is a sound merge for independent observations, and a document is the unit of
  independence. Two articles naming the same two people is corroboration.
* The same article read twice is not. The dedupe window (30 days daily, 7 hourly, 90
  weekly) is a *time* window, so it cannot stop this: the third read of a three-month-old
  article is outside it and would count as fresh corroboration. Left alone, a feed that
  republishes an archive crawls a single document's edge towards certainty.
* Cypher is order-sensitive, and Cypher does not document the evaluation order of the items
  in one `SET` — so a statement must never merge a number while reading a property a
  sibling item is writing. The verdict is therefore computed in Python
  (`models.relation_evidence_is_new`, fed by `graph/writer.py`, which reads the existing
  `doc_ids` per batch) and travels into the statement as the parameter `row.is_new`.

| `row.is_new` | What the upsert does |
| --- | --- |
| `true` | `confidence` merges by noisy-OR, `observations` increments, `doc_ids` gains the document (bounded at 20). |
| `false` | `doc_ids` is refreshed, everything else is left exactly as it was. |

A missing verdict defaults to `true` — a writer that cannot read the ledger must not be able
to silently stop counting evidence; it logs a warning instead, and `:IngestRun` records the
degradation. Entity nodes and `MENTIONS` carry the same contract: entity counters and
confidence merge only for an uncounted document (checked against the row's `doc_ids`), and a
mention row is idempotent (`count` is assigned, `confidence` takes the maximum, surface forms
union) so one document can be re-read indefinitely without inflating anything.

### Calculated — `PUPPET_MASTER_OF`

Written by [`puppetnet/graph/analytics.py`](../puppetnet/graph/analytics.py) *after* the
harvest, from the graph it just produced — never by an adapter.

```
(:Person)-[:PUPPET_MASTER_OF {score}]->(:Entity)
```

| Property | Notes |
| --- | --- |
| `score` | Chain-weighted influence over that specific target (0–1). |
| `weight` | `min(1.0, score)` — the calculated override of the vocabulary default. |
| `confidence` | `max(score, risk_score × chain_strength)`. |
| `components` | map — `control_breadth`, `opacity`, `layering`, `convergence`, `adversarial`, `movement`. |
| `reasons` | string[] (≤ 12) — human-readable justification. |
| `evidence` | string — the chain, as text. |
| `depth` | hops from the person to the target (decay `0.7`/hop, max 4). |
| `run_id`, `computed_at`, `first_seen`, `last_seen` | Provenance. |

The person node also gets `risk_score`, `components`, `reasons` and `updated_at` written
back onto it (`PERSON_RISK_UPDATE`), which is what the mandated `Person(name, alias,
risk_score)` shape refers to.

Scoring is additive with fixed component weights — `control_breadth` 0.30, `opacity`
0.25, `layering` 0.20, `convergence` 0.10, `adversarial` 0.10, `movement` 0.05 — and a
person must clear `PUPPET_MASTER_MIN_SCORE` (0.40) to appear at all. Only the top
`PUPPET_MASTER_TOP_N` (8) targets per person are written, with `PUPPET_MASTER_MAX_EDGES`
(2000) as a hard cap for a large graph. Edges not refreshed within
`ANALYTICS_PRUNE_DAYS` (14) are deleted, so the calculated layer reflects the current
graph rather than every graph ever written.

The pass cannot fail a run: any exception is recorded as an `analytics` error and the
pipeline still exits 0.

## Constraints & indexes

Created on boot by `ensure_schema_statements()` (each `IF NOT EXISTS`; a statement the
server rejects is logged and skipped so a 4.x instance cannot break a run):

```
CONSTRAINT puppetnet_entity_key    FOR (e:Entity)     REQUIRE e.canonical_key IS UNIQUE
CONSTRAINT puppetnet_document_id   FOR (d:Document)   REQUIRE d.doc_id IS UNIQUE
CONSTRAINT puppetnet_source_id     FOR (s:Source)     REQUIRE s.source_id IS UNIQUE
CONSTRAINT puppetnet_run_id        FOR (r:IngestRun)  REQUIRE r.run_id IS UNIQUE

INDEX puppetnet_entity_name/type/confidence/aliases
INDEX puppetnet_person_risk          FOR (p:Person) ON (p.risk_score)
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

// Shell companies and why they were flagged
MATCH (s:ShellCompany)
RETURN s.name, s.jurisdiction, s.shell_risk, s.shell_risk_reasons
ORDER BY s.shell_risk DESC LIMIT 25

// Foundation boards: who sits with whom, and on what
MATCH (p:Person)-[t:TRUSTEE_OF]->(f:Foundation)
RETURN f.name, f.country, collect(p.name) AS trustees, avg(t.confidence) AS confidence

// A tail number, its owner and everyone seen aboard
MATCH (ac:Aircraft) WHERE ac.tail_number STARTS WITH 'N'
OPTIONAL MATCH (owner:Person)-[:OWNS|CONTROLS]->(ac)
OPTIONAL MATCH (pax:Person)-[b:PASSENGER_ON]->(ac)
RETURN ac.tail_number, owner.name, collect(DISTINCT pax.name) AS passengers, b.weight

// Registrants sharing one address (mail-drop clusters)
MATCH (a:Person)-[r:SHARES_ADDRESS]->(b:Person)
RETURN a.name, b.name, r.weight, r.confidence, r.evidence ORDER BY r.confidence DESC

// The calculated influence layer, with its justification
MATCH (p:Person)-[m:PUPPET_MASTER_OF]->(t)
WHERE m.score >= 0.4
RETURN p.name, p.risk_score, t.name, labels(t), m.score, m.depth, m.components, m.reasons
ORDER BY m.score DESC LIMIT 25
```
