# Operations

How the pipeline runs unattended, what it leaves behind, and what to do when it
misbehaves.

Related: [configuration](configuration.md) · [graph schema](graph-schema.md) ·
[edge relay](edge-relay.md)

---

## 1. Automation

Two workflows live in [`.github/workflows/`](../.github/workflows).

### `ci.yml` — every push and pull request

| Job | What it does |
| --- | --- |
| `python` (matrix 3.10 / 3.11 / 3.12) | install → `ruff check` → byte-compile all modules → CLI smoke tests (`--version`, `--doctor`, `--list-sources`, `--print-config`) → `pytest -q` |
| `worker` | Node 20 syntax check of `worker.js`, `wrangler.toml` validation, optional `wrangler deploy --dry-run` |

CI needs no secrets and writes nothing: the smoke tests run with `DRY_RUN=true`, which is
why `load_settings()` accepts that flag as an alternative to Neo4j credentials.

### `daily_ingest.yml` — cron `0 4 * * *` (04:00 UTC)

Off-peak for GitHub runners and for most origin sites. The Worker's own janitor cron runs
at 04:30 UTC ([`wrangler.toml`](../wrangler.toml) `[triggers] crons`); it prunes buckets
idle for more than 6 h and zeroes the global RPM window, so it does not disturb a run that
started at 04:00 and is still harvesting.

Safeguards built into the job:

* `concurrency: daily-ingest-${{ github.ref }}`, `cancel-in-progress: false` — two writers
  racing the same MERGE keys produce duplicates and burn minutes;
* `permissions: contents: read` — the job needs no write scope;
* `timeout-minutes: 90` against a 35-minute in-process harvest budget
  (`MAX_RUNTIME_SECONDS=2100`) plus install and model-download overhead;
* dispatch inputs reach the shell through `env`, never by interpolating into the script
  body, and `--sources` is rejected if it contains shell metacharacters.

**Secrets**

| Secret | Required | Used for |
| --- | --- | --- |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` | yes (for graph writes) | AuraDB connection; `NEO4J_USER` is accepted as an alias for the username |
| `NEO4J_DATABASE` | no | non-default database name |
| `PROXY_WORKER_URL`, `PROXY_AUTH_TOKEN` | yes (for edge routing) | relay base URL + bearer token |
| `OPENCORPORATES_API_TOKEN`, `COMPANIES_HOUSE_API_KEY` | no | higher rate limits on those sources |
| `WIKIDATA_USER_AGENT` | recommended | Wikidata requires a contactable UA |
| `ICIJ_QUERY_TERMS`, `ICIJ_DATASET_URLS`, `REGISTER_FILES`, `RSS_FEEDS` | no | dataset/feed overrides |

Without Neo4j credentials the run still completes: `--doctor` reports the gap, the pipeline
falls back to a dry run, and the job exits 0. A fork never fails its first scheduled
execution.

**Step order:** checkout → setup-python (pip cache) → install → spaCy model cascade
(`trf → lg → md → sm`, `continue-on-error: true`) → `--doctor` preflight → restore `.state`
cache → resolve args → run ingest (tee'd to `ingest.log`) → upload artefacts → annotate →
fail on hard errors.

The model cascade verifies each download by actually loading it
(`python -c "import spacy; spacy.load(model)"`); a model that downloads but fails to load
is skipped rather than trusted. If nothing loads, the job emits
`::warning title=NLP degraded::` and continues on the gazetteer pipeline.

**Artefacts** — `ingest-report-${{ github.run_id }}` containing `reports/`, `ingest.log`
and `.state/`, retained 14 days, uploaded with `if: always()`.

---

## 2. Exit codes

| Code | Meaning | Scheduled run | Notes |
| --- | --- | --- | --- |
| `0` | Success | green | |
| `1` | Configuration error | **red** (`::error`) | Missing/invalid secrets, unparsable `sources.yaml`, `ConfigError` |
| `2` | Runtime failure | **red** (`::error`) | Pipeline aborted; see `ingest.log` |
| `3` | Partial | amber (`::warning`) | At least one source failed but the graph was written |
| other | Unexpected | **red** | Should not happen |

Exit 3 is deliberately **non-fatal** on a schedule: one flaky feed must not mark the whole
run failed. To escalate, dispatch manually with `fail_on_error: true` (which makes any
source error produce a non-zero exit) or add a branch on `steps.ingest.outputs.exit_code`
in a notification step.

The ingest step captures `PIPESTATUS[0]` and always `exit 0` itself, so the reporting and
artefact steps run regardless; the *last* step re-raises the failure.

---

## 3. Run reports

Each run writes two files into `REPORT_DIR` (default `reports/`, override with
`--report-dir`):

```
reports/<run_id>.json     # full machine-readable report
reports/<run_id>.md       # stats.markdown_summary(), the same text as the GH step summary
```

Suppress with `--no-report`. Inside GitHub Actions the markdown summary is appended to
`$GITHUB_STEP_SUMMARY`, and these outputs are published: `run_id`, `report_path`,
`documents`, `entities`, `relations`, `errors`.

`<run_id>.json` top level:

| Key | Contents |
| --- | --- |
| `run_id`, `generated_at` | Identity |
| `stats` | Every counter (see below), `errors[]`, `per_source{}`, `duration_seconds` |
| `sources` | Per-source report: documents, entities, relations, warnings, errors, elapsed |
| `nlp` | `NLPEngine.describe()` — backend, pipes, `has_parser`, `has_statistical_ner`, model candidates, `load_errors`, rule counts, co-occurrence penalty |
| `graph` | Client `describe()` + `status` + cumulative `WriteSummary` (`entities_written`, `relations_written`, `entities_capped`, `relations_capped`, flush counts) |
| `network` | `EdgeFetchClient.describe()` — relay URL, transport mix, rate-limit and robots counters, deferred-task stats |
| `relations_by_type` | Predicate histogram with confidence min/mean/max per type |

The markdown summary's per-source table is the fastest way to spot a source that silently
returned nothing:

```
| Source | Docs | Entities | Relations | Errors |
| --- | --- | --- | --- | --- |
| `rss_world` | 42 | 318 | 96 | 0 |
| `icij_offshore` | 0 | 0 | 0 | 1 |
```

---

## 4. State and dedupe

* **Neo4j is authoritative.** `Document.doc_id` and `Entity.canonical_key` are unique
  constraints; a document already in the graph is skipped by MERGE semantics, and
  `ONLY_NEW_DOCUMENTS=true` filters by `seen_at`/`DEDUPE_WINDOW_DAYS` before that.
* **`.state/content_hashes.json`** is a warm cache of content hashes from recent runs,
  bounded to the most recent 100 000 entries. It survives a graph outage and makes an
  unchanged feed free. It is restored between Actions runs via `actions/cache`
  (`restore-keys: puppetnet-state-`). Deleting it is always safe — the next run simply
  re-checks the graph.

**Reprocessing a document deliberately:** delete its state entry (or the whole file) *and*
remove or re-stamp the `Document` node, because the graph is the authoritative gate.

---

## 5. Observability

**Logs.** Human-readable by default; `LOG_JSON=true` (set in the workflow) or `--log-json`
emits one JSON object per line:

```json
{"ts": "2026-09-27T04:03:11.482+00:00", "level": "INFO", "logger": "puppetnet.pipeline",
 "message": "source rss_world harvested 42 documents", "run_id": "…", "source_id": "rss_world",
 "doc_id": null, "url": null, "duration_s": 12.4, "event": null, "count": 42}
```

The fixed key set (`run_id`, `source_id`, `doc_id`, `url`, `duration_s`, `event`, `count`)
means logs are directly ingestible by Loki/Datadoc/CloudWatch without a custom parser.

**Counters worth alarming on** (all in `stats` and mirrored onto the `IngestRun` node):

| Signal | Healthy | Action if not |
| --- | --- | --- |
| `documents_fetched` | > 0 and stable day over day | 0 across all sources ⇒ relay or DNS problem |
| `http_via_worker` / `http_direct_fallback` | mostly via worker | rising direct fallback ⇒ relay down, `PROXY_AUTH_TOKEN` wrong, or 401s |
| `http_rate_limited`, `seconds_throttled` | small | growing steadily ⇒ lower `TOKEN_BUCKET_RATE_PER_SEC` or raise `HOST_RATE_PER_SEC` |
| `http_robots_blocked` | near 0 | a source's path became disallowed — fix the URL pattern, never bypass |
| `http_deferred_to_queue` | occasional | persistent ⇒ the host bucket is too tight for the feed volume |
| `entities_capped_node_budget` | 0 | non-zero ⇒ you hit `AURA_NODE_CAP`; see below |
| `relations_dropped_low_confidence` | informational | a sudden spike usually means the NLP model degraded |
| `nlp.backend` | `spacy:en_core_web_*` | `blank+gazetteer` ⇒ model install failed; check the cascade step |
| `error_count` | 0 | read `stats.errors[]` |

**Graph-side queries**

```cypher
// Last 14 runs at a glance
MATCH (r:IngestRun) RETURN r.run_id, r.status, r.finished_at, r.documents_fetched,
       r.entities_written, r.relations_written, r.error_count, r.duration_seconds
ORDER BY r.finished_at DESC LIMIT 14;

// Headroom against the free-tier node cap
MATCH (e:Entity) RETURN count(e) AS entity_nodes;

// Which runs were capped, and by how much
MATCH (r:IngestRun) WHERE r.entities_capped_node_budget > 0
RETURN r.run_id, r.entities_capped_node_budget, r.relations_capped_node_budget
ORDER BY r.finished_at DESC;

// Weakest edges first — the natural cleanup target
MATCH ()-[rel]->() WHERE rel.confidence < 0.15 RETURN type(rel), count(*) AS n ORDER BY n DESC;
```

---

## 6. Common procedures

**Trigger a run by hand**

```bash
gh workflow run daily_ingest.yml --ref main                 # everything
gh workflow run daily_ingest.yml -f sources=rss_world -f limit=20 -f dry_run=true
gh workflow run daily_ingest.yml -f log_level=DEBUG -f skip_nlp=true
```

**Replay one source locally**

```bash
python ingest.py --sources icij_offshore --limit 10 --dry-run --log-level DEBUG --report-dir /tmp/reports
```

**Rotate the relay token** — set the new token in `PROXY_AUTH_TOKENS` (comma-separated
list on the Worker) *and* the GitHub secret, deploy, then drop the old value from the
Worker var. `PROXY_AUTH_TOKEN` (singular) still works for a hard cutover.

**Raise or lower the node budget** — `AURA_NODE_CAP` (default 200 000, `0` disables).
Before raising it past the AuraDB free-tier limit, check the instance's actual node quota:
exceeding it suspends writes at the database level, which is worse than the guard refusing
newcomers. When the cap binds, the run still completes; the refused entities are counted
and listed in the report, and existing nodes are always updated.

**Purge a cached relay response**

```bash
curl -X DELETE -H "Authorization: Bearer $PROXY_AUTH_TOKEN" \
     "$PROXY_WORKER_URL/cache?url=https://example.test/article"
```

**Add or change a feed** — edit [`config/sources.yaml`](../config/sources.yaml) (or set
`RSS_FEEDS` / `ICIJ_QUERY_TERMS`), commit, and dispatch a dry run for that source before
letting the cron pick it up. Every source spec carries its own confidence weight; news
sources stay at 0.4 unless the publisher is a primary register.

**Redeploy the Worker** — `wrangler deploy` (or `--env production`), then
`python ingest.py --doctor` to confirm the bindings and limits the harvester will see.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ConfigError` on startup, exit 1 | neither `DRY_RUN=true` nor Neo4j credentials present | set the three `NEO4J_*` secrets, or run with `--dry-run` |
| `--doctor` shows `relay: unreachable` | Worker not deployed, wrong URL, or token mismatch | `curl $PROXY_WORKER_URL/health`; compare `bindings.auth_configured` |
| `401 unauthorized` from the relay, harvest falls back to direct | `PROXY_AUTH_TOKEN` differs from the Worker secret | re-`wrangler secret put PROXY_AUTH_TOKEN` and update the repo secret |
| `403 robots_disallowed` | path genuinely disallowed | change the URL pattern; the refusal is recorded and never retried by design |
| `429` + growing `seconds_throttled` | host bucket too tight for the volume | lower `TOKEN_BUCKET_RATE_PER_SEC`, or raise `queue_on_limit` delay instead of hammering |
| `nlp.backend = spacy:blank+gazetteer` in CI | `spacy download` failed (network/CDN) | the cascade already retried; if it persists, pin a wheel in the workflow or vendor the model |
| Few entities, many `documents_skipped_duplicate` | feed unchanged | expected — `ONLY_NEW_DOCUMENTS` + content-hash cache |
| Entities extracted but `entities_written = 0` | dry run, or node cap already reached | check `graph.summary` and `entities_capped_node_budget` |
| Run aborts mid-harvest with exit 2 | `MAX_RUNTIME_SECONDS` exceeded, or an unhandled fault | raise the budget, reduce `MAX_DOCUMENTS_TOTAL`, read `ingest.log` |
| Duplicate-looking nodes | different canonical keys (name + type) | usually a type mismatch; see reconciliation in [nlp-pipeline.md](nlp-pipeline.md) |
| Actions cache miss on `.state` | first run, or cache evicted | harmless — Neo4j dedupe still applies |

---

## 8. Capacity guardrails

| Resource | Budget | Enforcement |
| --- | --- | --- |
| AuraDB Free nodes | `AURA_NODE_CAP` = 200 000 | probed once per run; strongest newcomers admitted, rest counted and refused |
| Harvest wall clock | `MAX_RUNTIME_SECONDS` = 2100 s in CI | checked between documents; the run closes cleanly and reports `partial` |
| Documents | `MAX_DOCUMENTS_PER_SOURCE` = 150, `MAX_DOCUMENTS_TOTAL` = 1500 | per-source and global caps |
| Worker CPU | 30 ms/request class | body cap, no HTML parsing in the Worker |
| Worker KV | reads/writes per request kept to the bucket + robots entries | `KV_SYNC_INTERVAL_MS` reconciliation instead of a KV hit per request |
| Global relay RPM | 900 (600 in `production`) | hard 429 once exceeded |
| Response size | 8 MiB per body | truncated with `"truncated": true` |
| Report retention | 14 days | `retention-days` on the artefact upload |
