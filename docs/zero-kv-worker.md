# Zero-KV Worker and unified static data – migration/runbook

## Active architecture

- Wrangler entrypoint: `apps/worker/src/index.ts` (TypeScript, standalone).
- **No KV bindings, no placeholder IDs, no Durable Objects/queues/database bindings.**
- No scheduled handler, graph API, general-purpose fetch proxy, database heartbeat,
  account-analytics polling or legacy status dashboard in the active Worker.
- Historical `apps/worker/worker.js` remains solely for compatibility/testing of the
  old pipeline/advanced legacy integrations. It is NOT imported by the new entrypoint
  or bundled/deployed. `npm run test:worker:legacy` explicitly tests that old code.
- Root `wrangler.toml` remains a compatibility symlink; deploy from the Worker
  workspace, not repository root where the relative src path is different.

Normal read path:

```
Reviewed static export -> Pages CDN /data/graph_latest.json
  -> dashboard via apps/web/src/lib/dataFetcher.ts
  -> legacy static provider (same raw JSON and canonical IDs)
  -> Next profile generation via apps/web/lib/entity-store.ts
  -> sitemap via scripts/generate-sitemap.mjs
```

Actual route directory in this repo is `apps/web/app/dashboard`, not `src/app`.
No duplicate Next app tree was created. The requested transport helper lives at
`apps/web/src/lib/dataFetcher.ts` and is imported by the actual dashboard.

Per-entity JSON files/manifest remain available from the existing exporter; the
Next static profile generator uses the aggregate for a consistent build snapshot.
Views may display different filtered/bounded subsets of that same graph (legacy
overview is capped); this is not a claim that every view always has the same count.

## Endpoints

| Route | Behaviour |
|---|---|
| `POST /api/search` | Authenticated GitHub repository_dispatch, fixed event `puppetnet-refresh` |
| `GET /health` | Lightweight process/mode check; no database or GitHub probe |
| `GET/HEAD /data/graph_latest.json` | Optional public static forward/cache |
| `GET/HEAD /data/static-manifest.json` | Optional public static forward/cache |
| `GET/HEAD /data/entities/<64-hex-id>.json` | Optional public static forward/cache |
| `OPTIONS` on supported routes | Explicit CORS preflight |
| Old `/graph`, `/status`, generic proxy routes | 404 |

Static JSON normally loads DIRECTLY from Pages; the Worker is not needed for reads.
Optional forwarding allows only fixed paths against the configured HTTPS Pages
origin, without query parameters, user-selected target URLs, credentials, cookies
or redirects. Responses are bounded at 16 MiB and must be JSON with HTTP 200.

## Caching and cost boundaries

Public static forwards use `caches.default.match/put`, a 60-second Cache-Control
policy, ETag/Last-Modified when supplied by Pages, and origin-specific CORS on the
outgoing response. Mutable paths are deliberately not cached immutable: filenames
of entity slices hash IDs, not content. Error/auth/dispatch replies are `no-store`.
Cache failures degrade to origin fetch; no KV fallback is used. The Pages `/data/*`
headers use the same short policy. Client reload may see up to the cache TTL of
staleness, plus whatever time the build/deployment needs.

**Zero KV writes is not zero overall operating cost or unlimited service.** Worker
requests, CPU, GitHub Actions and optional model calls retain their own quotas.
Cache API is per-edge and evictable; it is neither a global rate limiter nor a lock.

## Search authentication and asynchronous semantics

Required server-side secrets:

- `GITHUB_DISPATCH_TOKEN`: a fine-grained GitHub credential permitted to send
  repository_dispatch to the configured repository (GitHub requires the applicable
  repository contents-write permission for this endpoint).
- `SEARCH_AUTH_TOKEN`: a DIFFERENT random operator credential, >=32 characters.
  It can be entered by an authorized operator in the dashboard. Never enter the
  GitHub credential in the browser; no secret is bundled as NEXT_PUBLIC_*.

Plaintext configuration: `PAGES_ORIGIN`, comma-separated exact `ALLOWED_ORIGINS`,
`GITHUB_OWNER`, `GITHUB_REPO`. Add only the Preview origins you actually authorize;
there is no wildcard host/origin matching. CORS is not authentication for non-browser
clients: possession of the operator token is required independently.

Body: `{ "query": "Example Holdings" }`, 1–120 characters, no control characters,
JSON <=2 KiB. Repo/event/branch are NOT accepted from the request. The query is
passed as data, never interpolated into shell/Cypher.

HTTP 202 means ONLY that GitHub accepted the event. It is not evidence that a job
started, quota was available, relevant documents were found, NLP succeeded, public
approval exists or a deployment completed. Search scopes the configured RSS feeds,
not the entire Internet. The current graph remains visible while processing.
The dashboard offers a separate CDN reload button; there is no expensive polling loop.

The Worker has an advisory 2-reservation/day **per-isolate** guard, not a globally
strict two-request limit. The authoritative two-run/day UTC reservation remains in
`.github/workflows/daily_ingestion.yml` and its Git-persisted budget. The relay does
not read that budget or promise that each accepted event will be processed. Never
publish the operator token to all visitors: leaked credentials can still create
GitHub dispatch/queue traffic even when expensive ingestion is rejected by quota.
No automatic retries of non-idempotent GitHub dispatch POSTs are performed.

## Migration / activation (not performed by these edits)

1. Review and integrate code. Configure the GitHub daily workflow and its existing
   opt-in, source list, secrets and publication branch. It still publishes only to
   `arena/5fb0fe0b-puppetnet`, not production `main`; the previous production-promotion
   caveat remains. A new Worker does not fix that deployment boundary automatically.
2. Set Worker plaintext variables and the two new secrets. `npm run provision:worker
   -- --dry-run` shows the KV-free plan. With credentials supplied securely via the
   environment, `npm run provision:worker` uploads only those two secrets through
   stdin and deploys from the Worker workspace. It does not create any namespaces.
3. Remove old deployed cron triggers in Cloudflare and verify none remain. The
   new config/source does not schedule work. Simply editing local files is NOT
   proof that an existing remote cron was removed.
4. Remove obsolete bindings/secrets on the DEPLOYED Worker as needed. Existing
   unused namespaces need not be deleted, but old deployments/jobs must not keep
   writing to them. Do not blindly delete resources another Worker still needs.
5. Do NOT configure the new search relay as the Python pipeline's HTTP proxy or
   as a legacy `/graph` backend. Clear old PROXY_WORKER_URL/PROXY_AUTH_TOKEN settings
   or keep a separately operated historical relay if still needed. Normal new
   ingestion can use the pipeline's existing direct, rate-limited fetch path.
6. Test exact-Origin CORS, unauthorized POST ->401, authorized POST ->202, GitHub
   receipt/quota outcome, and an actual reviewed static publication/deployment.
7. Database keep-alive/status monitoring is no longer provided by this Worker.
   Evaluate whether daily pipeline writes and the separate Python check satisfy
   operations needs; a RETURN 1 read alone is not a write heartbeat.

Legacy UI loads the published snapshot first even if old connection settings are
saved. Advanced historical API options remain explicitly labelled for separately
operated old services; they are not advertised as features of the new search relay.
If no snapshot can be read, examples remain explicitly labelled as fictional.

## Verification

- New Worker TS typecheck and isolated HTTP/cache/auth/error tests.
- Data-fetcher tests: same-origin token-free read, offline/schema fallback, caller
  cancellation, explicit HTTPS search URL, accepted-only asynchronous response.
- Profile test verifies graph_latest wins over stale entities.json and matches the
  dashboard's normalized canonical IDs.
- Existing legacy streamed-snapshot test and Pages regression/build/export gates.
- Historical KV provisioner refuses to operate on the new active config; legacy
  provisioning E2E tests use an explicit isolated historical fixture, never this config.

No live Cloudflare/GitHub secrets, account changes, billable service calls or real
repository dispatches are used by these tests. A dry-run bundle is not a deployment.
