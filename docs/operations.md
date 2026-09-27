# Operations

How the pipeline runs unattended, what it leaves behind, and what to do when it
misbehaves.

Related: [configuration](configuration.md) · [graph schema](graph-schema.md) ·
[edge relay](edge-relay.md)

---

## 1. Automation

Seven workflows live in [`.github/workflows/`](../.github/workflows): `ci.yml`,
`hourly_ingest.yml`, `daily_ingest.yml`, `weekly_ingest.yml`, `graph_maintenance.yml`,
`pages_deploy.yml` (documented with the console) and `screenshots.yml` (below).

### The tiered cadence

One cron cannot serve every source. A news wire publishes continuously and its articles are
corroboration while they are news; a company register says the same thing on Monday and on
Friday. Reading the register every hour buys nothing and burns the free tier's request
budget, while reading the wire once a day loses the day. So the registry assigns every
source a **cadence** and each workflow harvests exactly one tier:

| Tier | Cron (UTC) | Sources | Documents / runtime |
| --- | --- | --- | --- |
| `hourly` | `5 * * * *` | ADS-B Exchange, OCCRP, ICIJ stories, Global Witness, Transparency International, world news feeds, aviation news | 40 per source, 400 total, 15 min |
| `daily` | `13 4 * * *` | ICIJ leaks, OpenCorporates, Wikidata, FAA registry, flight logs, Companies House, register files | 150 / 1500, 35 min |
| `weekly` | `13 2 * * 0` | every source — the deep run | 400 / 4000, 55 min |

Implementation notes that matter when changing any of it:

* `Cadence` (in [`puppetnet/models.py`](../puppetnet/models.py)) is the vocabulary and
  `SourceSpec.cadence` the assignment; `specs_for_tier(tier)` in
  [`puppetnet/sources/registry.py`](../puppetnet/sources/registry.py) resolves it. The tiers
  are additive: `weekly` is the full registry, not `daily + weekly`.
* An unknown tier is a `ValueError`, never "all". A typo in a workflow must fail the run,
  not silently harvest the whole registry 24 times a day.
* `--tier` is an **outer bound**: `--sources` narrows a tier, it cannot widen it. Naming a
  daily source in an hourly run harvests nothing and logs
  `not harvested by the hourly tier — use --tier all`.
* The daily workflow resolves its tier from a dispatch input whose scheduled default is
  `daily`, so `workflow_dispatch` can widen a manual run to `all`; the hourly and weekly
  workflows hard-code theirs.
* All three ingest workflows share `concurrency: puppetnet-ingest-${{ github.ref }}` with
  `cancel-in-progress: false`. The 04:13 run therefore waits for a :05 run that is still
  going instead of overlapping it, and a queued run is never cancelled mid-batch.
* The crons deliberately do not chain (`workflow_run`): chaining hourly → daily would run
  the slow tier 24 times a day, which is the cost the tiers exist to avoid. The weekly run
  needs no chain either — the daily run starts two hours later and Graph Maintenance
  follows the daily run, so a weekly batch is maintained the same morning.
* `tests/test_schedule.py` pins this contract: the crons, the tiers each workflow passes,
  the shared lock, and that Graph Maintenance still follows the workflow it names.

### `ci.yml` — every push and pull request

| Job | What it does |
| --- | --- |
| `python` (matrix 3.10 / 3.11 / 3.12) | install → `ruff check` → byte-compile all modules → CLI smoke tests (`--version`, `--doctor`, `--list-sources`, `--print-config`) → `pytest -q` |
| `worker` | Node 20 syntax check of `worker.js`, `wrangler.toml` validation, optional `wrangler deploy --dry-run` |
| `web` | syntax check of `app.js`/`modals.js`/`worker.js` → committed Tailwind build has no drift → vendored libraries and licences present → both smoke suites (`worker_smoke.mjs`, `web_smoke.mjs`) → `npm run audit`, the static whole-repo consistency gate ([`docs/audit.md`](audit.md)) |

CI needs no secrets and writes nothing: the smoke tests run with `DRY_RUN=true`, which is
why `load_settings()` accepts that flag as an alternative to Neo4j credentials.

### `hourly_ingest.yml` — cron `5 * * * *`

The fast tier: news wires, the investigative-news feeds and the live telemetry sources. It
starts five past the hour rather than on it, because the top of the hour is when GitHub's
scheduler and every other project's cron fire at once, and a delayed hourly run is a missed
news cycle.

It installs `en_core_web_sm` **first** — the opposite order from Daily Ingest — because a
15-minute window must not be spent waiting for a 500 MB transformer. `DEDUPE_WINDOW_DAYS`
is 7 here: anything older than a week that the hourly tier has not seen is the daily tier's
business. Artefacts are retained 7 days (24 runs a day would otherwise fill the storage).

### `daily_ingest.yml` — cron `13 4 * * *` (04:13 UTC)

The slow tier: registers, leak databases, Wikidata and the cross-referencing sources.

Off-peak for GitHub runners and for most origin sites. The Worker's own janitor cron runs
at 04:30 UTC ([`wrangler.toml`](../wrangler.toml) `[triggers] crons`); it prunes buckets
idle for more than 6 h and zeroes the global RPM window. It touches no graph state and no
live bucket of a run that started at 04:13, so the overlap is harmless — a harvest keeps
its own in-memory token bucket for the rest of the run.

Safeguards built into the job:

* `concurrency: puppetnet-ingest-${{ github.ref }}`, `cancel-in-progress: false` — two
  writers racing the same MERGE keys produce duplicates and burn minutes; the group is
  shared with the hourly and weekly runs, which is what keeps 04:13 and :05 apart;
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

### `weekly_ingest.yml` — cron `13 2 * * 0` (Sunday 02:13 UTC)

The deep run: `--tier weekly`, i.e. the whole registry, with the largest budgets and a
90-day dedupe window. It exists next to Daily Ingest because the daily run is bounded on
purpose — a day on which a register publishes a 4000-row delta must still finish — so the
deep run is where those deltas are read in full, and where the accurate (`_trf`
transformer) spaCy model is worth its install time.

Starting two hours before the daily run is deliberate: the 04:13 run hands the weekly graph
to Graph Maintenance at 06:35 the same morning. A partial failure (exit 3) is fatal here —
unlike the hourly runs, the next deep run is seven days away.

### `graph_maintenance.yml` — after the ingest, plus cron `35 6 * * *`

| Trigger | Why |
| --- | --- |
| `workflow_run` on **Daily Ingest** `completed` | Maintenance must see today's nodes. This is the primary trigger. |
| `schedule` 06:35 UTC | `workflow_run` only fires for workflows on the default branch, so a fork or a feature branch needs the safety net. The concurrency group absorbs the overlap. |
| `workflow_dispatch` | `passes` (all/dedupe/prune/centrality/bridges/capacity), `dry_run`, `send_alerts`, `force_alerts`, `threshold`, `max_merges`, `engine`, `log_level` |

`concurrency: graph-maintenance-${{ github.ref }}` with `cancel-in-progress: false` — two
overlapping runs would merge and delete the same nodes, and a Brandes pass would score a
graph that is being rewritten underneath it.

Steps, in order:

1. **Restore alert state** — `.state/` from `actions/cache` (rolling key
   `puppetnet-alerts-state-`). This is the suppression ledger; without it every bridge is
   re-announced on every run.
2. **Resolve arguments** — dispatch inputs are validated in `case` statements and passed
   through `env`, never interpolated into the script body.
3. **Run maintenance** — `graph_analytics.py $args --report-dir reports`, tee'd to
   `maintenance.log`, exit code captured into `steps.maintenance.outputs.exit_code` and
   swallowed (`exit 0`) so the reporting steps still run.
4. **Locate the report** — newest `reports/graph_maintenance_*.json`, or a warning.
5. **Push alerts** — `telegram_bot.py --all --report <path>`, skipped entirely when
   `TELEGRAM_BOT_TOKEN` is unset (maintenance-only deployments are valid) or when
   `send_alerts=false`.
6. **Artefacts, job summary, annotations, fail gate.**

`DRY_RUN` is derived from the credential: `${{ secrets.NEO4J_PASSWORD == '' && 'true' ||
'false' }}`, so a fork produces a report instead of a red X.

**Secrets** — required: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`. Optional:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `NEO4J_DATABASE`.

**Artefacts** — `graph-maintenance-${{ github.run_id }}` containing `reports/`,
`maintenance.log` and `alerts.log`, retained **30** days (longer than the ingest report,
because a merge decision may need reviewing weeks later), uploaded with `if: always()`.

The job summary embeds a JSON block with the numbers an operator checks first:
utilisation, `merges_applied`, `homonyms_protected`, `purged`, `escalated`,
`nodes_scored`, the engine used, the bridge count and the top-5 anomalies.

---

### `screenshots.yml` — on changes to `web/`, plus manual dispatch

The visual half of the beta test. `scripts/screenshots.mjs` serves `web/` from a local
`node:http` server (path-traversal guarded) and drives it in a real Chromium: four
viewports × sixteen states = **60 screenshots**, each one gated by an in-page assertion, so
a state that does not verify fails the job instead of producing a misleading picture.

* Playwright is installed with `npm install --no-save playwright@1.63.0` **inside this job
  only** — it is not a `devDependency`, because its postinstall would download browser
  binaries on every `npm ci` in every other job;
* `.screenshots/` is uploaded as the `screenshot-matrix` artefact with
  `if: always()` and a 14-day retention: images are build output, never commits.
  The upload sets `include-hidden-files: true` — without it upload-artifact skips
  the dot-directory and reports "No files were found" after a *successful* render;
* the artefact also carries `run.log` (the step is `tee`d with `pipefail`) and
  `matrix.json`, which the harness writes in a `finally` block, so a run that died
  before its first screenshot still says which stage killed it. The job summary is
  that same `matrix.json` rendered by `scripts/screenshots.mjs --summary`;
* dispatch inputs (`only`, `viewport`) reach the script through `env` and a bash array,
  never by interpolation into the command line;
* `concurrency: screenshots-${{ github.ref }}` with `cancel-in-progress: true` — a superseded
  render is worthless, and Chromium minutes are the expensive part of this job.

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

### `graph_analytics.py`

| Code | Meaning | Scheduled run | Notes |
| --- | --- | --- | --- |
| `0` | Success | green | |
| `1` | Configuration error | **red** | Unparsable `--weights`, bad credentials, client could not be built |
| `2` | Runtime failure | **red** | Neo4j unreachable, or every requested stage failed |
| `3` | Partial | amber (`::warning`) | One stage failed; the others completed and their results are in the report |

A stage that fails records its own `status: "failed"` and `error` inside the report, so
`3` is actionable without reading the log. A stage nobody asked for is `"skipped"`, never
`"completed"` — a fresh report must not claim work that did not happen.

### `telegram_bot.py`

| Code | Meaning | Scheduled run | Notes |
| --- | --- | --- | --- |
| `0` | Delivered, or nothing to deliver | green | Suppressed alerts count as success |
| `1` | Bad arguments | **red** | argparse |
| `2` | Delivery failure | **red** | At least one message could not be sent |
| `3` | Configuration/report problem | amber | No token, no chat id, or no maintenance report found |

The maintenance workflow fails the job on `1`/`2` from `graph_analytics.py` and on `2` from
`telegram_bot.py`; `3` (partial) is a warning for both, and "Telegram not configured" is a
warning rather than an error because maintenance-only is a supported deployment.

#### Failure alerts — closing the 24-hour loop

The alert passes above need a maintenance report to say anything, and on a 24-hour schedule
that leaves the most dangerous failure invisible: a run that *dies before writing one*. The
next run overwrites the log line nobody reads, and the first symptom is a graph that quietly
stopped growing — days later, when somebody looks at it.

Every ingest workflow therefore has a **Warn on a hard failure** step:

```
python telegram_bot.py --failure "<reason> (exit N)" \
  --workflow "$WORKFLOW_NAME" --exit-code "$INGEST_EXIT" --run-url "$RUN_URL"
```

* It runs on exit `1` (configuration) and `2` (runtime) — both mean nothing was written.
  Exit `3` is per-source and expected; the daily digest already reports it, and 24 warnings a
  day would train the operator to ignore the channel.
* No token configured is not an error: harvest-without-alerts is a valid deployment, and the
  step logs that it skipped.
* The warning is *best effort* — it always exits 0. A Telegram outage must not turn a failed
  harvest into a failed workflow for a different reason.
* Failures are suppressed like every other alert (24 h, `AlertLedger`), and the key is
  `workflow | source | exit code` — **not** the message. A broken source reports a different
  error on every attempt (timeout, then connection refused); the operator wants one alert per
  broken thing, not one per sentence.

A workflow that dies *before* the ingest step (a rejected `--doctor` preflight, a failed
checkout) sends no alert: GitHub's own scheduled-workflow failure notification covers it, and
the alternative — alerting on every step failure — is the noise this design avoids.

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

### Maintenance reports

`graph_analytics.py` writes `reports/graph_maintenance_<run_id>.json` (suppress with
`--no-report`). It is the contract between the two tools: `telegram_bot.py` reads
`bridge_alerts` and `top_anomalies` out of it and never opens the database.

| Key | Contents |
| --- | --- |
| `run_id`, `generated_at`, `status`, `dry_run` | Identity; `completed` / `partial` / `failed` |
| `capacity` | Counts plus utilisation against `AURA_NODE_CAP` / `AURA_EDGE_CAP`, `over_target`, `headroom_*`, and `error` when the probe itself failed (zeroes then mean "unknown") |
| `dedupe` | `entities_examined`, `strict_id_groups`, `strict_id_merges`, `fuzzy_candidates`, `context_pairs`, `decisions`, `merges_applied`, `mentions_repointed`, `relationships_repointed`, `nodes_deleted`, `homonyms_protected`, `type_conflicts`, `capped`, `merges[]`, `protected[]` |
| `prune` | `candidates`, `purged`, `protected_nodes`, `protected[]` (node + reason), `documents_purged`, `escalated`, `cutoff`, the thresholds actually used, `sample[]` |
| `centrality` | `engine`, `nodes_projected`, `nodes_scored`, `clusters`, `articulation_points`, `truncated`, `weights`, `spike_window_hours`, `top[]`, `bridges[]` |
| `top_anomalies` | The digest input: score, per-component values, cluster, degree, neighbours, reasons |
| `bridge_alerts` | One entry per new cluster bridge, with `dedupe_key` — the alert engine's suppression key |

`dedupe.protected[]` is the review queue for the homonym guard: each entry names the pair,
both spellings, the similarity, which signal admitted it (`string` or `token`) and why
context was demanded (`generic_name`, `common_name`, `ambiguous_single_token`). If a real
duplicate keeps landing there, the fix is more context in the graph (an address, an
identifier), not a lower threshold.

`telegram_bot.py` writes no report of its own; `--json` prints the run summary
(`bridge_sent`, `bridge_suppressed`, `bridge_capped`, `digest_sent`, `failures`, per-message
`outcomes[]`, ledger counters) to stdout with logging on stderr.

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
  `ONLY_NEW_DOCUMENTS=true` filters by content hash / `doc_id` against
  `DEDUPE_WINDOW_DAYS` before that.
* **The window is read from `last_ingested_at`**, which every write stamps (create *and*
  match) and which carries its own range index (`puppetnet_document_ingested`). The
  earlier form `WHERE d.fetched_at >= $since OR d.last_ingested_at >= $since` could use no
  index at all — an `OR` across two properties defeats both — so every run scanned every
  `Document` node, 24 times a day on the hourly tier. `tests/test_graph.py` pins both the
  query shape and the index.
* **`.state/content_hashes.json`** is a warm cache of content hashes from recent runs:
  `{hash: seen_at}` with the run's own window applied on read (7/30/90 days depending on
  the tier) and a retention of `LOCAL_STATE_RETENTION_DAYS` (120) on write, so an hourly
  run cannot prune what the weekly deep run depends on. It is capped at the newest
  `LOCAL_STATE_LIMIT` (100 000) *by timestamp* — the earlier version sorted the digests,
  which kept an arbitrary slice and could drop what had just been read. The file survives
  a graph outage and makes an unchanged feed free; it is restored between Actions runs via
  `actions/cache` (`restore-keys: puppetnet-state-`). Deleting it is always safe — the next
  run simply re-checks the graph.
* **Re-reading is safe, but it is not free.** A document inside the window is skipped
  entirely; one outside it is fetched again, parsed again and *written* again — which the
  evidence ledger in the writer absorbs without touching `confidence` or `observations`
  (see [graph schema](graph-schema.md#new-evidence-vs-a-re-read)). That is why the tiers
  set different windows: the hourly tier has no reason to re-read a week-old article, and
  the weekly deep run does have a reason to re-check three months of registers.

**Reprocessing a document deliberately:** set `DEDUPE_WINDOW_DAYS=0` for one manual run, or
delete its state entry *and* remove or re-stamp the `Document` node — the graph is the
authoritative gate.

* **`.state/telegram_alerts.json`** is the alert ledger: `sent{dedupe_key: timestamp}` for
  bridge suppression, `digests{chat_id: timestamp}` for the once-per-day rule, and
  cumulative `counters{}`. Entries older than seven times the suppression window are pruned
  on load, so the file does not grow for the lifetime of the repository. Deleting it is
  safe and re-arms every alert — that is the way to re-announce after a channel migration.
  A `--dry-run` never writes it: a preview must not consume the suppression window that the
  next real run depends on.

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

**Check sources without touching the graph** — stage 1 only: no spaCy, no Neo4j
connection is ever opened. This is the fastest way to confirm credentials, relay
reachability and what each API actually returned today.

```bash
python ingest.py --fetch-only wikidata,opencorporates --limit 25
python ingest.py --fetch-only                       # every enabled source
```

**Put an API token on the relay** — the Worker's host profiles inject credentials on the
outgoing request, so a runner needs none of them when it goes through the relay:

```bash
wrangler secret put OPENCORPORATES_API_TOKEN
wrangler secret put ADSBEXCHANGE_API_KEY        # ADS-B Exchange v2 via RapidAPI
wrangler secret put COMPANIES_HOUSE_API_KEY
curl -s "$PROXY_WORKER_URL/health" | jq '.host_profiles'   # booleans only, never values
```

`/health` reports *whether* each profile's credential is configured; the value never
leaves the Worker, and a URL-injected token is stripped from every URL the relay returns
or caches.

**Maintenance, locally and safely**

```bash
# What would change? Nothing is written; the report is the plan.
python graph_analytics.py --all --dry-run --report-dir /tmp/reports

# Capacity only — the cheapest check, one read.
python graph_analytics.py --capacity

# Review the homonym guard before trusting a threshold change.
python graph_analytics.py --dedupe --dry-run --json \
  | jq '.dedupe.protected[] | {names, similarity, signal, generic_name, common_name}'

# See the merges that would happen, strongest evidence first.
python graph_analytics.py --dedupe --dry-run --json \
  | jq '.dedupe.merges[] | {winner_name, loser_name, method, similarity, identifier, context}'

# Re-score with different weights without touching the database.
python graph_analytics.py --centrality --dry-run --weights 0.5,0.3,0.2 --top 10
```

**Alerts, locally**

```bash
python telegram_bot.py --test                        # getMe + one "engine online" message
python telegram_bot.py --all --dry-run               # render every message, send nothing
python telegram_bot.py --digest --force              # ignore the once-per-day guard
python telegram_bot.py --all --report reports/graph_maintenance_<run>.json --json
```

`--dry-run` needs neither a chat id nor network access: with no `TELEGRAM_CHAT_IDS` it
renders to a `(dry-run)` destination, which is how the message format gets reviewed before
a channel exists.

**After a merge went wrong** — every winner records `merged_from` (the absorbed canonical
keys), `aliases` (the absorbed names) and `source_ids`/`doc_ids` unions, so the provenance
survives the delete. Restore by re-running the ingest for the affected sources: the
resolver rebuilds the absorbed node from its documents, and the `merged_from` list tells you
which key to look for.

**Read the calculated layer**

```cypher
MATCH (p:Person)-[m:PUPPET_MASTER_OF]->(t)
WHERE m.score >= 0.4
RETURN p.name, p.risk_score, m.score, m.depth, m.components, m.reasons, labels(t), t.name
ORDER BY m.score DESC LIMIT 25
```

If it is empty, check `analytics.status` in the run report first (`disabled`,
`failed`, or `ok` with `persons_scored`/`puppet_master_rows` counts), then
`PUPPET_MASTER_MIN_SCORE`.

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
| Relay answer shows a slower rate than you asked for | the host profile capped it (`rate_limit.policy.ratePerSec`) | by design — e.g. Wikidata is pinned at 0.25 req/s, burst 1. Raise `queue_on_limit` delay, not the rate |
| `credential:opencorporates-missing` in the response | the Worker has no `OPENCORPORATES_API_TOKEN` secret | `wrangler secret put OPENCORPORATES_API_TOKEN`, or set it runner-side for direct fetches |
| `credential:rapidapi-missing` | no `ADSBEXCHANGE_API_KEY` | set the secret; without it only the free `adsbdb.com` mirror answers |
| Wikidata `400`/`HTML` where JSON was expected | the query went as a GET, or the form body lost `format=json` | the relay fills in `format` and `maxlag`; send SPARQL as `data={...}` so it becomes a form POST |
| `analytics.status = failed` in the report | the calculated layer raised | the run is still valid and still exits 0; read `analytics.error`, then `errors` for the `analytics` entry |
| No `:PUPPET_MASTER_OF` edges though persons were scored | nobody cleared the threshold | lower `PUPPET_MASTER_MIN_SCORE` (0.40) or check `ANALYTICS_MAX_PERSONS` / `ANALYTICS_GRAPH_EDGE_LIMIT` |
| `adsb_exchange` yields nothing | no `tail_numbers` / `callsigns` configured | set `AIRCRAFT_TAIL_NUMBERS` — without a subject the adapter has nothing to ask for |
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
| Worker KV | 1 000 writes/day (Workers KV Free) | a cold host costs nothing; a hot host costs at most one read + one write per `KV_SYNC_INTERVAL_MS` window, and a denial publishes immediately. `[cost] worker/kvWritePerRequest` fails the build if a write returns to the request path |
| Global relay RPM | 900 (600 in `production`) | hard 429 once exceeded |
| Response size | 8 MiB per body | truncated with `"truncated": true` |
| Report retention | 14 days | `retention-days` on the artefact upload |
| Analytics persons / edges | `ANALYTICS_MAX_PERSONS` = 5 000, `ANALYTICS_GRAPH_EDGE_LIMIT` = 50 000 | scored set and neighbourhood graph are bounded before any walking starts |
| Calculated edges | `PUPPET_MASTER_TOP_N` = 8 per person, `PUPPET_MASTER_MAX_EDGES` = 2 000 per run | hard truncation, logged when it binds; stale edges pruned after `ANALYTICS_PRUNE_DAYS` |
| Shared-organisation ties | `max_shared_org_members` = 24, `max_pairs_per_org` = 12 | a bigger board is skipped rather than squared |
| Co-passenger ties | `max_copassenger_pairs` = 60 | per manifest |
| AuraDB Free relationships | `AURA_EDGE_CAP` = 400 000 | reported as `edge_utilisation`; crossing `PRUNE_CAPACITY_TARGET` escalates the orphan rule |
| Merges per maintenance run | `DEDUPE_MAX_MERGES` = 500 | equivalence classes beyond the cap wait for the next run; `capped: true` says so |
| Entities resolved per run | `DEDUPE_ENTITY_LIMIT` = 60 000 | ordered by `canonical_key`, so the set is stable between runs |
| Pairwise name comparisons | `MAX_PAIR_COMPARISONS` = 250 000 | blocking first; over-full buckets (a shared 3-char prefix, a surname like Kim) are skipped, not squared |
| Deletions per run | `PRUNE_MAX_DELETIONS` = 20 000 | hard cap, `capped: true` in the report |
| Centrality projection | `CENTRALITY_MAX_NODES` = 4 000 nodes, `ANALYTICS_GRAPH_EDGE_LIMIT` edges | best-connected nodes first; a truncated projection keeps the backbone, not a random sample |
| Betweenness wall clock | `CENTRALITY_MAX_SECONDS` = 240 s | a partial Brandes run is rescaled by the fraction of sources visited and flagged `truncated` |
| Bridge alerts per run | `TELEGRAM_BRIDGE_ALERT_LIMIT` = 10 | strongest by anomaly score; the rest wait for the next run |
| Alert repetition | `TELEGRAM_SUPPRESSION_HOURS` = 24 per `(node, cluster pair)` | ledger in `.state/telegram_alerts.json` |
| Digest frequency | once per chat per UTC day | `--force` overrides |
| Telegram outbound rate | `TELEGRAM_RATE_PER_SEC` = 1.0, burst 5 | per-host token bucket, plus `retry_after` honoured on 429 (capped at 120 s) |
| Message size | 4 096 characters | chunked on line boundaries; a chunk never ends inside an HTML tag |

---

## 9. What this costs

The design constraint is **zero**: nothing here needs a payment method, and every quota below
is a permanently free tier. The numbers are the *worst case* the configuration allows, not an
average — a run that finds nothing new costs the same as one that does.

| Service | Free tier | Worst case per day | Headroom |
| --- | --- | --- | --- |
| GitHub Actions (public repo) | unlimited minutes, 20 concurrent jobs | 24 hourly (≤ 45 min each) + 1 daily (≤ 90) + 1 weekly (≤ 120) + maintenance (≤ 60) + screenshots | the schedule is serial by design (one ingest at a time, shared lock), so concurrency is never a factor |
| Cloudflare Workers | 100 000 requests/day, 10 ms CPU/request | 24 runs × (400 documents + retries + robots) ≈ **10 000–15 000** | ~85 % |
| Workers KV reads | 100 000/day | ≈ 1 per hot host per window + robots lookups ≈ **15 000** | ~85 % |
| Workers KV writes | **1 000/day** | ≤ 1 per hot host per 20 s window. A run touches each article host once, so only the feeds (a dozen) are hot: **well under 100** | the binding constraint of the whole system — see [edge relay § Free-tier KV budget](edge-relay.md) |
| Workers KV storage | 1 GB | bucket + robots entries, expiring on their own TTLs | ~100 % |
| Cloudflare Queues | 10 000 operations/day | one per *deferred* request, i.e. only when the relay is over budget | large: queues are the escape hatch, not the path |
| Cloudflare Pages | 500 builds/month, unlimited requests | 1 build per push to `web/` or `worker.js` in a month of development, then 0 | one order of magnitude |
| Neo4j AuraDB Free | 1 instance, 200 000 nodes / 400 000 relationships | capped in code (`AURA_NODE_CAP`, `AURA_EDGE_CAP`) and reported as utilisation | the pipeline refuses newcomers rather than failing writes |
| Telegram Bot API | no quota for a bot of this size | ≤ 10 bridge alerts + 1 digest + at most a handful of failure warnings per day | rate-limited to 1 msg/s locally, `retry_after` honoured |
| Domain / TLS | none used | the console is served from `*.pages.dev`, the relay from `*.workers.dev` | a custom domain is the only thing that could ever cost money, and nothing requires one |

Two numbers are worth watching rather than assuming:

* **KV writes** are the only quota a burst can exhaust (1 000/day), and the relay's write count
  is proportional to a run's *duration*, not to its document count: a host is only published
  once it has been requested inside the current sync window. `worker/kvWritePerRequest` and the
  smoke suite pin that.
* **AuraDB nodes** are the only quota the data can exhaust (200 000). `AURA_NODE_CAP` is probed
  once per run, newcomers are admitted strongest-first, and a refusal is counted in the run
  report as `entities_capped` — a full graph degrades, it does not fail.

There is no analytics, no CDN, no third-party API key required, and no host on the paid
blocklist is reachable by default: `[cost] paidSourceDefault` fails the build if a paid source
ever becomes enabled without the operator setting a key. The audit's `cost/external-host`
inventory lists every host the code touches, with the budget it lives in.
