# Local-first daily pipeline and unified static publication

## Implemented files

- `packages/pipeline/src/token_saver_nlp.py`: HTML/regex cleanup, local German
  spaCy NER/dependency extraction, optional bounded Gemini predicate classification.
- `packages/pipeline/src/export_static_slices.py`: reviewed, public Neo4j/local
  export, stable content revisions, safe per-entity slices, daily run reservation CLI.
- `.github/workflows/daily_ingestion.yml`: 03:00 UTC, repository dispatch and manual
  dispatch, ten-minute job timeout, at most two reserved runs per UTC day.

These are real pipeline integrations, not isolated examples: `NLP_BACKEND=token_saver`
selects the new engine in the existing harvesting/dedupe/Neo4j writer. Old spaCy and
GLiNER modes remain available. Enabling `ENABLE_DAILY_STATIC=true` disables the old
scheduled GLiNER ingestion job so there is no duplicate ingestion schedule.
The existing separate tests and keep-alive jobs are not removed.

## Important departures / prerequisites

1. **No automatic main push.** This session is fixed to
   `arena/5fb0fe0b-puppetnet`; the workflow checks out and pushes only that branch.
   It is not a claim that a Production deployment will be triggered. The repository
   currently documents `main` as Pages production branch. Review/merge the data PR
   for production, or intentionally configure a Pages preview for the publication
   branch. Workflow triggers themselves must first exist on the default branch.
   Nothing has been pushed, merged, enabled or provisioned by this change.
2. **Gemini 1.5 Flash is a historical model name.** The requested name is the
   configurable default, not an assurance that Google still serves it. A 404/429
   or API failure disables cloud processing for that run, without dropping local
   extraction. Set `GEMINI_MODEL` to a currently available supported model and
   verify that account's free quota, privacy terms and billing. No universal free
   API or always-free operation is guaranteed. Default cloud processing is OFF.
3. **150 means input + output.** `countTokens` receives the exact generation
   contents; prompts above 100 tokens are not generated. Output cap is <=50 and
   input+output <=150. Non-thinking 1.5/2.0 models are allowed; 2.5 Flash uses
   thinkingBudget=0; unknown thinking models are refused. Output must be a single
   allowed numeric predicate code. No entity creation or arbitrary Cypher from AI.
4. **Public export requires review.** Nodes AND edges must explicitly have boolean
   `public=true`, no private/noindex flag, and edges must not require review.
   Raw NLP results are NOT auto-approved. Empty publication fails closed unless
   deliberately passed `--allow-empty`; existing public files are not silently
   replaced by demo data or wiped after an unsuccessful database read.
5. **Ten minutes is a hard upper timeout, not a successful-completion promise.**
   Cold dependency/model installs, sleeping AuraDB or slow feeds may exhaust it.
   No heavy Torch/GLiNER model is installed by this workflow. Ingestion subprocess
   is bounded at 315 seconds (300-second internal processing budget) and export at 60 seconds, within the job deadline.

## Configuration

Repository variables:

| Variable | Value |
|---|---|
| `ENABLE_DAILY_STATIC` | `true` only when ready to activate |
| `DAILY_RSS_FEEDS` | Comma-separated, operator-approved German RSS URLs |
| `ENABLE_GEMINI_FALLBACK` | `false` initially; opt in only after quota/privacy review |
| `GEMINI_MODEL` | Requested default `gemini-1.5-flash`; verify availability or override |

Secrets:

- `NEO4J_URI` (`neo4j+s://...`, verified TLS)
- `NEO4J_USERNAME`
- `NEO4J_PASSWORD`
- `GEMINI_API_KEY` only for optional cloud stage
- `PROXY_WORKER_URL` and `PROXY_AUTH_TOKEN` ONLY if operating a separate historical HTTP relay; leave unset for the new search-only Worker

GitHub Actions needs contents-write permission for the publication job and an
existing remote publication branch. Branch protections may reject its push; the
workflow must fail rather than silently bypass protections. Pages needs its own
configured Git integration and Production-only SITE_ORIGIN.

## Local use

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e './packages/pipeline[dev]'
pip install 'https://github.com/explosion/spacy-models/releases/download/de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl'

# Existing ingestion CLI, new local-first backend:
NLP_BACKEND=token_saver RSS_FEEDS='https://YOUR-APPROVED-SOURCE/feed' \
  ENABLED_SOURCES=news_world MAX_RUNTIME_SECONDS=300 \
  MAX_DOCUMENTS_TOTAL=10 ANALYTICS_ENABLED=false TRIPLET_BACKEND=off \
  python packages/pipeline/ingest.py --sources news_world --limit 10

# Reviewed local graph; each approved node and edge needs public:true:
python packages/pipeline/src/export_static_slices.py --input reviewed-graph.json

# Or bounded, explicitly public data from AuraDB:
python packages/pipeline/src/export_static_slices.py --neo4j

SITE_ORIGIN=https://puppetnet-web.pages.dev npm run ci:pages
```

A missing de_core_news_sm model is an error, not a silent English/blank fallback.
`news_world` in this backend uses the configured German feeds instead of prepending
English default feeds. The local model supports conservative German named-entity
SVO patterns, not arbitrary multilingual relation understanding. Negated/hedged
sentences and uncertain passive direction are skipped rather than asserted.

## Manual refresh/search

Send an authenticated GitHub `repository_dispatch` with:

```json
{
  "event_type": "puppetnet-refresh",
  "client_payload": {"query": "Example Holdings"}
}
```

The optional query is a literal, case-insensitive filter on titles/text harvested
from the **configured RSS sources**, not a general Internet search engine. It is
at most 120 printable characters, passed through an environment variable, never
inserted into shell or Cypher. Without it, the bounded feed slice is refreshed.
Filtered-out documents are not marked deduped as though they had been processed.
No unauthenticated public dispatch endpoint is added to the website.

Scheduled and manual runs share a durable `.pipeline-state/daily-budget.json`:
TOTAL <=2 UTC-day reservations, scheduled <=1. A reservation is committed/pushed
BEFORE ingestion; failed runs consume their reservation. No cache is trusted as
quota authority. Actions concurrency serializes writers. A rejected reservation
performs no ingestion/export. Gemini uses a persisted hash/reservation ledger,
<=3 attempted classifications per process and <=6 ledger reservations per day;
failed/repeated requests do not trigger unbounded retries. Do not run parallel
local writers against these filesystem ledgers. A hard job cancellation may
prevent its final Gemini-ledger flush; the already-persisted daily run reservation
still limits how many workflow processes can start.

## Unified public data contract

```
Neo4j explicit public approval OR reviewed local graph
  -> graph_latest.json
      -> modern dashboard (same-origin bounded fetch)
      -> legacy static provider (same snapshot, no API token)
      -> Next static entity pages
      -> build-time sitemap
  -> entities/<sha256(canonical-id)>.json
  -> static-manifest.json (canonical ID -> safe filename, revision)
```

The aggregate is the canonical source. Per-entity slices are supplementary files
for selective consumers; the existing Next profile renderer deliberately still
normalizes the aggregate so pages/relationships match exactly. It does not make
one HTTP request per entity or use the raw canonical ID as a filesystem path.

Compatibility: `entities.json` and then `entities.sample.json` remain profile
fallbacks when no `graph_latest.json` exists. Modern/legacy runtime consumers also
fall back to explicitly labelled demos when the snapshot is absent/unreachable.
No live-API token is sent to the public file. Legacy static data is labelled a
published snapshot, not a live database connection or an invented demo.

Export bounds: 5,000 public nodes, 20,000 public relationships, up to 5,000 public
source references. Only allowlisted properties are published. Document bodies,
private notes, risk scores and PUPPET_MASTER_OF edges are not exported. Sources
must also be explicitly public; unsafe source URLs are omitted. The current
export does not reconstruct every private evidence trail for the public website.

Identical content produces identical revisions/files: no timestamp-only commits.
Per-file writes are atomic; a set of files is made publicly coherent by the Git
commit/deployment, not by claiming the local filesystem has a multi-file transaction.
Only files owned by the previous export manifest are pruned. Readers should compare
revisions if reading individual slices across a deployment boundary.

Existing published entities revoked/removed disappear on the next successful export
and deployment. Failed exports intentionally retain the last published snapshot;
urgent takedown therefore needs an explicit operational path, not reliance on retries.

## Verification performed

- Offline Python suite and lint; dedicated new tests for approval filters, cleanup,
  stable output, safe filenames, pruning, durable daily cap, Gemini exact input
  count/output cap/schema/deduplication and German PER/SVO/negation contracts.
- Legacy DOM test uses an actual streamed Response with a two-node publication.
- Full Pages build with a temporary two-node publication: two real entity profiles
  and matching sitemap URLs generated successfully. Temporary publication removed
  afterwards; no fictional test publication is committed as real data.
- spaCy inference contracts use constructed German parse fixtures; no claim of
  real-model precision, Gemini API availability, live AuraDB writes, workflow
  activation, production delivery or an actual ten-minute cloud run is made.
