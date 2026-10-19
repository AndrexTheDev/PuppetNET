# NLP pipeline

Code: [`puppetnet/parsing/nlp_engine.py`](../puppetnet/parsing/nlp_engine.py) ·
[`relations.py`](../puppetnet/parsing/relations.py) ·
[`craft.py`](../puppetnet/parsing/craft.py) ·
[`text_extract.py`](../puppetnet/parsing/text_extract.py) ·
[`nlp/extractor.py`](../nlp/extractor.py) ·
[`nlp/triplet_extractor.py`](../nlp/triplet_extractor.py)

The engine answers one question per document: *which actors are in this text, and what
happened between them?* It is built to be useful with a transformer model and still
honest without one.

---

## Backends

| `backend` | When | NER | Parse | Triple source | Edge method |
| --- | --- | --- | --- | --- | --- |
| `spacy:en_core_web_trf` / `…_lg` / `…_md` / `…_sm` | a statistical model loaded | statistical | yes | dependency parse (SVO) | `dependency` (factor 1.0) |
| `spacy:blank+gazetteer` | spaCy importable, no model available | EntityRuler + lexical/gazetteer | no | regex patterns, then sentence co-occurrence | `cooccurrence` (factor 0.8) |
| `pattern` | spaCy not importable at all | gazetteer + regex | no | regex patterns + co-occurrence | `cooccurrence` (factor 0.8) |

`SPACY_MODELS` is an ordered preference list; the first model that *loads cleanly* wins.
Every failure is recorded in `load_errors` and surfaces in the run report. A missing model
is a supported degraded mode — it is logged as a warning, never recorded as a run error,
so `--fail-on-error` and the exit codes do not punish an environment that is behaving as
designed.

`NLPEngine.describe()` reports `backend`, `has_parser`, `has_ner`,
`has_statistical_ner`, the loaded pipes, model candidates, load errors, the rule-table
shape and the co-occurrence penalty. It is embedded in every run report.

---

## Stages

```
Document.text
  │
  ├─ 1. chunk          _iter_chunks: paragraph/sentence boundaries, ≤ max(20k, chars/4)
  ├─ 2. sentence       spaCy sentencizer, or an abbreviation-aware regex splitter
  │                     (split_sentences, capped at NLP_MAX_SENTENCES_PER_DOC)
  ├─ 3. entity spans   model NER ∪ EntityRuler (craft ids) ∪ lexical/gazetteer spans
  │                     → _normalise_spans → _merge_span_entities → _keep_entity
  ├─ 4. craft          CraftDetector.find_all: registrations, IMO/MMSI, flight numbers,
  │                     vessel prefixes, type appositives → properties on the span
  ├─ 5. register       Entity per canonical key: aliases, mentions, provenance,
  │                     best confidence, flattened properties
  ├─ 6. triples        dependency SVO  |  regex patterns  |  sentence co-occurrence
  ├─ 7. decide         RelationMapper: verb / noun / preposition rules → predicate,
  │                     direction (passive flip), negation, hedging
  ├─ 8. score          evidence score → compose_confidence(source_weight, method, evidence)
  ├─ 9. reconcile      one surface form ⇒ one node (type conflicts resolved)
  └─ 10. dedupe        per-clause dedupe, noisy-OR across independent sightings
```

### 3. Entity spans

Spans arrive from several detectors and are merged by offset. On overlap the *richer*
span wins: a CRAFT candidate beats a non-CRAFT span, and properties are merged rather
than discarded (so the EntityRuler's registration match and the craft detector's
`registry_country` end up on one entity). Filters then drop noise:

* consumer-product surfaces ("MS Windows", "PS5") unless the span carries a hard craft
  identifier (registration, IMO, MMSI, flight number, vessel prefix, VIN);
* all-symbol or single-lowercase-token spans, sentence-initial function words
  ("The", "However"), and two-character single words;
* bare craft **type** words ("superyacht", "military transport aircraft") — a type is
  context for the appositive rule, never an identity, so it never becomes a node.

### 4. CRAFT detection

`CraftDetector` recognises, and stores as node properties:

| Signal | Example | Properties |
| --- | --- | --- |
| ICAO registration by country prefix | `9H-VUC`, `VP-BBF`, `P4-RMA` | `craft_kind=Aircraft`, `registry_country=Malta`, `registration` |
| US tail numbers | `N123AB`, `N707WA` | `registry_country=United States` |
| IMO number | `IMO 9876543` | `craft_kind=Vessel`, `imo` |
| MMSI | `MMSI 249843000` | `craft_kind=Vessel`, `mmsi` |
| IATA flight number (gated on assigned airline codes) | `BA286`, `EK17` | `craft_kind=Aircraft`, `airline_code`, `flight_number` |
| Prefixed vessel names | `MT Renda`, `MV Amadea`, `SS Northern Star` | `craft_kind=Vessel`, `vessel_prefix` |
| Type appositives | "the **superyacht** Amadea" | `craft_type=superyacht` |
| Ground craft | VINs, registry-listed vehicle types | `craft_kind=Ground` |

Flight numbers are gated on the assigned two-letter airline code list: an ungated
"two letters + digits" pattern would match `ZZ123`, `No 5` and half the product codes in
a news article. 33 identifier patterns are compiled into the spaCy `EntityRuler` so a real
model pipeline gets the same CRAFT coverage as the degraded one.

`craft_mentions` counts detections per document and feeds `stats.craft_entities`.

### 6. Triples

**Dependency path** (model available) — for each sentence, verbs are walked for
`nsubj`/`nsubjpass`/`dobj`/`obj`/`attr`/`pobj`/`conj` dependents, producing
subject–verb–object candidates with `verb_dep`, passivity, argument distance and the
clause text. Negated clauses (`not`, `never`, `denied`, `refused`, `rejected` attached to
the verb) are dropped and counted in `negated_dropped` — "X does **not** own Y" must never
reappear as a weak `ASSOCIATED_WITH` edge.

**Pattern path** (no parser) — regex SVO patterns over each *clause*. Two details matter:

* patterns run on sentences re-segmented by the abbreviation-aware splitter, because a
  blank pipeline's sentencizer keeps `Midea Holdings Ltd. Igor Sechin` in one sentence and
  a greedy argument group would otherwise swallow the next clause's subject (and hide it
  from `finditer`, which resumes after the previous match);
* argument surfaces are trimmed at a sentence-terminal `". "` before being resolved to an
  entity, and an already-detected span always wins over a raw regex group.

An optional preposition is captured with the verb, which is what turns "met **with**" into
`MET_WITH` and "sold **to**" into a transfer rather than a generic association.

**Co-occurrence fallback** — when a sentence yields no triple at all and
`ENABLE_COOCCURRENCE_FALLBACK` is on, entity pairs within the same sentence are linked at
the co-occurrence rate. A sentence that produced *any* parse-derived or pattern-derived
triple suppresses the fallback, so weak guesses never sit next to a real reading of the
same clause.

### 7. Deciding the predicate

`RelationMapper` holds the rule table — **33 verb rules, 18 noun/apposition rules,
9 preposition rules**, covering **35** of the 46 predicates:

* `from_verb(lemma, subject_type, object_type, preposition=…)` — `owns` → `OWNS`,
  `founded` → `FOUNDED`, `met`+`with` → `MET_WITH`, `sailed`+`from` → `ARRIVED_FROM`,
  `traveled`+`to` → `TRAVELED_TO`, `sanctioned` → `SANCTIONED_BY`, …
* `from_noun(role, …)` — apposition and role nouns: `director of` → `DIRECTOR_OF`,
  `chairman` → `OFFICER_OF`, `shareholder` → `SHAREHOLDER_OF`, `subsidiary` →
  `SUBSIDIARY_OF`, `beneficiary` → `LINKED_OFFSHORE`, …
* `from_preposition(prep, …)` — `based in` → `LOCATED_IN`, `registered in` →
  `REGISTERED_IN`, `owned by` → `OWNED_BY` (passive flip), …

Rules are type-aware: a person–organisation pair and an organisation–organisation pair
reading the same verb can map to different predicates. `canonicalise()` normalises
direction (an `OWNED_BY` pointing the wrong way is flipped to `OWNS`), and unmatched verbs
fall back to `ASSOCIATED_WITH` with a low base score (rule note
`no-lexical-rule:<verb>`), so an unmapped verb still connects the actors without claiming
a specific relationship.

### 8. Confidence

```
confidence = source_weight × method_factor × evidence_score
method_factor = 0.8 for cooccurrence, 1.0 otherwise          # the mandated 0.2 penalty
evidence_score = rule base score, then decayed:
    × max(0.6, 1 − 0.02 × (distance − 6))   for argument distance > 6 tokens
    × 0.9                                   for embedded verb deps (relcl, advcl, xcomp…)
    × 0.95                                  for passive constructions
    floored at 0.05, capped at 1.0
```

Worked examples: a Wikidata `OWNED_BY` row = `1.0 × 1.0 × 1.0 = 1.0`; a news sentence
parsed with a model = `0.4 × 1.0 × evidence`; a news co-occurrence edge = at most
`0.4 × 0.8 = 0.32`. Anything under `MIN_EDGE_CONFIDENCE` (0.05) is dropped before it
reaches Neo4j.

### 9–10. Reconciliation and dedupe

* **Type reconciliation** — degraded type assignment is heuristic, so one surface form can
  be registered twice ("sold Yugansk **to Gazprom**" reads as a destination). Because the
  canonical key embeds the type, both readings would become separate nodes. The winner is
  the reading with a hard craft identifier, then the most confident, then the most
  specific type; losers are folded into the winner *in place*, so relations that already
  reference them resolve to the surviving key.
* **Per-clause dedupe** — two patterns reading one clause are one claim, so the stronger
  reading wins (`_is_same_claim`: same document, same sentence, overlapping argument
  spans).
* **Corroboration** — the same claim from an *independent* sentence merges with noisy-OR
  (`1 − (1 − a)(1 − b)`), raising confidence without ever reaching certainty.
* **Evidence novelty** — independence is counted per *document*, not per parse. The graph
  writer compares each row's document against the edge's `doc_ids` ledger
  (`models.relation_evidence_is_new`) and marks the row `is_new`; only a row that is new
  evidence may raise `confidence` or `observations`. Without that guard a feed that
  republishes an archive — or a dedupe window that has simply moved past a document —
  would let one article's edge climb towards 1.0 on its own. See
  [graph schema → New evidence vs. a re-read](graph-schema.md#new-evidence-vs-a-re-read).

---

## `ParseResult`

| Field | Meaning |
| --- | --- |
| `doc_id`, `characters`, `sentences` | Volume actually processed (after caps/truncation) |
| `entities` | Resolved `Entity` objects with aliases, mentions, provenance, properties |
| `relations` | Weighted `Relation` objects with evidence text, verb, rule note, spans |
| `dependency_triples` / `pattern_triples` / `cooccurrence_triples` | Where the triples came from |
| `craft_mentions` | CRAFT detections |
| `negated_dropped` | Clauses rejected because they were negated |
| `backend` | Which pipeline produced this |
| `warnings` | Truncation, sentence cap, per-document NLP errors |
| `elapsed_seconds` | Parse wall time |

Guards: `NLP_MAX_CHARS_PER_DOC` (400k, truncated with a warning),
`NLP_MAX_SENTENCES_PER_DOC` (1500), `NLP_MAX_ENTITIES_PER_DOC` (400),
`MAX_ARGUMENT_DISTANCE` (24 tokens).

---

## Text extraction

`text_extract.py` runs before the engine and normalises whatever the harvester fetched:

* **HTML** — BeautifulSoup with `lxml`: script/style/nav/footer/advert removal,
  main/article extraction, title/author/language/date metadata, entity decoding;
* **PDF** — `pypdf`, page-bounded, with metadata (title/author/creation date);
* **JSON / NDJSON / CSV / TSV** — flattened to readable prose so structured payloads
  fetched as files still parse;
* **plain text** — whitespace and mojibake cleanup.

Each result carries `word_count`, `warnings` and `is_usable` (≥ 12 words), which is how
paywalls, cookie walls and 404 pages are kept out of the graph.

---

# Multilingual GLiNER + Workers AI (v1.6)

The v1.5 engine above is spaCy-first and English-first. v1.6 adds a second,
independent path under [`nlp/`](../nlp/):

| Module | Responsibility |
| --- | --- |
| `nlp/labels.py` | The nine OSINT labels and their mapping to `EntityType` |
| `nlp/chunking.py` | Sentence-boundary windows of ≤384 tokens with overlap |
| `nlp/extractor.py` | `gliner_multi-v2.1` via PyTorch or ONNX Runtime |
| `nlp/triplet_extractor.py` | `(subject)-[PREDICATE]->(object)` via Workers AI |
| `nlp/graph_bridge.py` | The only conversion from model output to `Entity`/`Relation` |

Importing `nlp` never imports `torch`, `gliner` or `transformers`; the heavy
dependencies load when an extractor is constructed.

## Labels

`OSINT_LABELS` is exactly:

```
Person, Organization, Location, Date, Vehicle,
Aircraft_Registration, IMO_Number, Bank_Account, Crypto_Address
```

All nine are sent to GLiNER and echoed in the Workers AI prompt. Only six become
graph nodes without a schema migration:

| OSINT label | `EntityType` | Graph node? |
| --- | --- | --- |
| Person | Person | yes |
| Organization | Organization | yes |
| Location | Location | yes |
| Vehicle | Craft | yes |
| Aircraft_Registration | Craft | yes |
| IMO_Number | Craft | yes |
| Date | Unknown | no — span metadata (`date_mention`) |
| Bank_Account | Unknown | no — span metadata (`identifier`) |
| Crypto_Address | Unknown | no — span metadata (`identifier`) |

Keeping Date/account/crypto as spans is deliberate: promoting them would require
new Neo4j labels, constraints and domain properties. The extractor records the
exact OSINT label in `EntitySpan.properties["osint_label"]`, so a later schema
can adopt them without re-running the model.

## Chunking: 384 tokens, sentence-aligned

`nlp.chunking.split_sentences` handles German, English, Spanish, French,
Portuguese, Italian, Russian and CJK terminators. It suppresses boundaries after
titles (`Dr.`), German abbreviations (`z. B.`), initials (`J. Smith`), French
guillemet spacing (`Bonjour ! »`), decimals (`3.14`) and ordinal dates
(`3. Oktober 2020`).

`iter_chunks` then packs **whole sentences** into windows of at most
`GLINER_CONTEXT_TOKENS` (384) tokens with `GLINER_OVERLAP_TOKENS` (64) of trailing
sentences repeated. Consequences:

* an entity and its relation stay in one window;
* two windows can return the same span; `_dedupe` folds same-label spans with
  ≥80% overlap and keeps the highest score;
* every `TextChunk` carries absolute `start_char`/`end_char`, so a span is always
  `text[e.start_char:e.end_char] == e.text`;
* a sentence longer than the whole window is split at token/word boundaries and
  flagged (`split_sentence=True`, warnings `oversized-sentence-split:N`) rather
  than silently truncated;
* token counts use the checkpoint tokenizer when available and a conservative
  word-piece estimate otherwise.

Caps: `NLP_MAX_CHARS_PER_DOC` (400k), `NLP_MAX_SENTENCES_PER_DOC` (1,500),
`max_entities` (5,000). Truncation is reported, never silent.

## PyTorch and ONNX Runtime

```python
from nlp.extractor import GLiNERExtractor

with GLiNERExtractor("urchade/gliner_multi-v2.1", runtime="torch") as extractor:
    result = extractor.extract("Dr. Anna Müller trifft Acme in Berlin.", language="de")
    print(result.entity_counts)
```

* `runtime="torch"` is the default and the only path that works with the
  published checkpoint as-is.
* `runtime="onnx"` loads GLiNER's ONNX Runtime backend and requires a
  pre-exported `model.onnx` (the upstream checkpoint does not ship one). There is
  **no silent fallback**: a missing export raises `ExtractionError`.
* `model=` (and `tokenizer=`) inject a loaded model and keep the unit suite
  entirely offline.

`urchade/gliner_multi-v2.1` is 209M parameters and Apache-2.0. The older
`urchade/gliner_multi` is cc-by-nc-4.0 and must not be shipped.

## Workers AI triplets and the 10,000-neuron budget

Every Cloudflare account has **10,000 Neurons/day** free, reset at 00:00 UTC. On
the Workers Free plan requests hard-fail after the allocation; there is no
overflow invoice. `CloudflareTripletExtractor`:

1. sends one sentence-aligned chunk per call to
   `POST /client/v4/accounts/{account}/ai/run/{model}`;
2. asks for strict JSON `{"triplets":[{...}]}` and validates the answer:
   subject/object must be exact substrings of the chunk (or a supplied entity
   hint), the predicate must be in the graph's `RelationType` vocabulary,
   evidence must be a verbatim quote, self-edges and short arguments are dropped;
3. records actual `usage.prompt_tokens`/`completion_tokens` (falling back to a
   conservative `len/3` estimate) in a UTC-day ledger, and refuses a call that
   cannot fit the remaining budget with 2% headroom;
4. fails soft: HTTP 429/5xx retries honour `Retry-After`; an exhausted budget, an
   API error or prose instead of JSON becomes a warning on that document, never
   an exception that aborts the harvest.

Published rates (per million tokens, 2026-10) for the defaults:

| Model | Input | Output | 384-in/250-out chunk | ~chunks/day |
| --- | --- | --- | --- | --- |
| `@cf/meta/llama-3.1-8b-instruct` | 25,608 | 75,147 | ≈28.6 neurons | ≈350 |
| `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | 4,119 | 34,868 | ≈10.3 neurons | ≈970 |

The workflow selects `fp8-fast` by default (`CF_AI_MODEL`) and caps the stage at
`CF_AI_MAX_DOCUMENTS=10` documents per run, so a normal harvest spends a few
hundred of the 10,000 daily neurons and never touches a paid plan.

LLM predicates are coerced through `RelationType.coerce`; an unmodelled verb
becomes `ASSOCIATED_WITH`. `triplet_predicate_counts` exposes that fallback count
rather than pretending the predicate was understood.

## Graph bridge

```python
entities, relations = graph_from_triplets(
    document,
    triplet_result,
    known_entities=parse_result.entities,
    source_id="news_world",
)
```

Arguments that GLiNER resolved are typed from that span; otherwise the model's
own label is used; otherwise the node is `EntityType.UNKNOWN` (stored as
`:Entity:Unknown`). It is never guessed from capitalisation.

Confidence reuses the existing formula:

```
compose_confidence(0.4, ExtractionMethod.PATTERN, llm_confidence)
```

so an LLM edge is at most `0.4` and can never outrank a dependency-parsed edge.
The writer's noisy-OR and per-document evidence guard then apply unchanged.

`IngestPipeline` calls the bridge only when `TRIPLET_BACKEND=cloudflare`; the
stage is optional, capped and fail-soft, and `CF_AI_MAX_DOCUMENTS=0` disables it
without removing credentials.

## GitHub Actions

The repository contract allows exactly one workflow
(`.github/workflows/ingestion-pipeline.yml`); the NLP stages extend it instead of
adding a second file. Public-repository standard runners are free under GitHub's
public-repo policy; no matrix, GPU or paid inference API is used.

`tests` job: offline unit suite, ruff, plus an offline multilingual chunk/label/
budget contract. `ingestion` job (main + `ENABLE_INGESTION=true`):

* `GLINER_MODEL=urchade/gliner_multi-v2.1`, 384/64 window, CPU torch wheel;
* a real German/Russian/Arabic extraction preflight that asserts every returned
  span is a substring of its input;
* optional Workers AI preflight when `vars.TRIPLET_BACKEND=cloudflare`; the
  neuron ledger is restored/saved with `actions/cache`.

Configuration: `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (Workers AI
Read). The token is redacted from `Settings.describe()` and never written to the
ledger, reports or logs.

## Tests

```sh
pip install -e './packages/pipeline[dev]'          # no torch, no model download
python -m pytest -q -m 'not integration'           # offline suite

# live, opt-in
RUN_GLINER_INTEGRATION=1 pytest -m integration tests/test_nlp_integration.py -k gliner
CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... \
  pytest -m integration tests/test_nlp_integration.py -k workers_ai
```

The offline tests cover German, Russian, Arabic, French, Spanish and English
sentences, including exact offsets, overlap dedupe, script boundaries, budget
arithmetic, retry/`Retry-After`, malformed JSON and the graph bridge. Live tests
are deselected by default because they download a model or spend neurons.
