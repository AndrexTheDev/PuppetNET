# Edge fetch relay (`worker.js`)

A Cloudflare Worker that performs every outbound HTTP request on behalf of the Python
harvester. Two jobs: make the traffic look like ordinary browser traffic from many
places, and keep it polite enough that a daily cron never gets a source blocked.

* Source: [`worker.js`](../worker.js) · config: [`wrangler.toml`](../wrangler.toml)
* Runtime: Workers (V8 isolate), `nodejs_compat`, CPU limit 30 ms/request budget
* All bindings optional — without KV/Queues the relay degrades to a stateless forwarder
  with per-isolate rate limiting.

---

## Routes

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health`, `/healthz`, `/` | public | Liveness, version, binding presence, effective limits, colo |
| `GET` | `/stats` | bearer | Rate-limit / queue / cache counters |
| `POST` | `/fetch` | bearer | Proxy a request (control envelope in JSON) |
| `GET` | `/fetch?url=…` | bearer | Convenience form (query-string control) |
| `GET` | `/tasks/{id}` | bearer | Poll a deferred (queued) request |
| `DELETE` | `/tasks/{id}` | bearer | Drop a queued task result |
| `DELETE` | `/cache?url=…` | bearer | Purge a cached response |
| `OPTIONS` | `*` | public | CORS preflight (`ALLOWED_ORIGINS`) |

**Auth** — `Authorization: Bearer <PROXY_AUTH_TOKEN>` (or `X-Proxy-Token`). Multiple
comma-separated tokens are accepted in `PROXY_AUTH_TOKENS` for zero-downtime rotation.
Comparison is constant-time, and the Worker **fails closed**: with no token configured
every authenticated route is rejected, because an unauthenticated relay is an open proxy.

### `GET /health`

```json
{
  "ok": true,
  "worker": "puppetnet-edge-relay",
  "version": "1.5.0",
  "time": "2026-09-27T04:00:00.000Z",
  "bindings": {"rate_limit_kv": true, "result_kv": true, "queue": true, "auth_configured": true},
  "limits": {"global_rpm": 900, "host_rate_per_sec": 0.5, "host_burst": 4,
             "max_response_bytes": 8388608, "max_timeout_ms": 90000, "max_queue_delay_seconds": 300},
  "host_profiles": [
    {"label": "wikidata-sparql", "api_mode": true, "rate_per_sec": 0.25, "burst": 1,
     "timeout_ms": 120000, "cache_ttl_seconds": 3600, "credential": null},
    {"label": "opencorporates", "api_mode": true, "rate_per_sec": 0.5, "burst": 2,
     "timeout_ms": 30000, "cache_ttl_seconds": 21600, "credential": true}
  ],
  "colo": "CDG"
}
```

`host_profiles` is the effective per-API policy (see below). `credential` is a
**boolean** — `/health` is unauthenticated, so it reports whether a token exists,
never the token.

`ingest.py --doctor` probes this endpoint and reports the bindings, so a Worker deployed
without its KV namespaces is caught before a scheduled run.

### `GET /stats`

```json
{
  "ok": true,
  "version": "1.5.0",
  "global": {"minute": 1790000000, "count": 42, "rpm_limit": 900},
  "local_buckets": [{"host": "example.test", "tokens": 2.413, "age_ms": 1820}],
  "bindings": {"rate_limit_kv": true, "result_kv": true, "queue": true}
}
```

`local_buckets` is the per-isolate view (host-sorted, capped at 500 entries); the
authoritative cross-colo counters live in `RATE_LIMIT_KV` and are reconciled every
`KV_SYNC_INTERVAL_MS`.

### `POST /fetch` — control envelope

```json
{
  "url": "https://example.test/article",
  "method": "GET",
  "headers": {},
  "accept": "text/html,application/xhtml+xml",
  "referer": "https://example.test/",
  "user_agent": null,
  "fingerprint_salt": "run-20260927T040000Z-ab12cd:0",
  "follow_redirects": true,
  "cache_buster": "off",
  "respect_robots": true,
  "timeout_ms": 25000,
  "max_attempts": 3,
  "cache_ttl_seconds": 0,
  "priority": 5,
  "source_id": "news_world",
  "request_id": "…",
  "rate_limit": {"rate_per_sec": 0.3, "burst": 2},
  "queue_on_limit": true,
  "queue_delay_seconds": 60,
  "resolve_override": null,
  "body_text": null, "body_b64": null, "json": null, "content_type": null
}
```

`api_mode` and `host_profile` are also accepted, but callers never need to send
them: the Worker sets both from its host profiles.

Every field is optional except `url`. Values are clamped to the Worker's own limits
(`MIN_HOST_RATE_PER_SEC`/`MAX_HOST_BURST`, `MAX_TIMEOUT_MS`, `MAX_MAX_ATTEMPTS`,
`MAX_CACHE_TTL_SECONDS`, `MAX_QUEUE_DELAY_SECONDS`), so a misconfigured client cannot
make the relay impolite. A `robots.txt` `Crawl-delay` **lowers** the requested rate.

### `POST /fetch` — success

```json
{
  "ok": true,
  "request_id": "…",
  "cached": false,
  "status": 200, "status_text": "OK",
  "url": "https://example.test/article", "request_url": "…", "method": "GET",
  "content_type": "text/html; charset=utf-8",
  "headers": {"…": "…"},
  "text": "<html>…",            // text-like content types
  "body_b64": null,             // binary payloads (PDF, images) instead of `text`
  "byte_length": 48213, "truncated": false,
  "attempts": 2,
  "attempts_log": [{"attempt": 1, "status": 503, "retried_in_ms": 1400, "ua": "…"}],
  "elapsed_ms": 1830,
  "fingerprint_used": {"ua": "…", "secChUa": "…", "platform": "…"},
  "host_profile": "opencorporates",
  "credential": "credential:opencorporates-applied",
  "rate_limit": {"allowed": true, "remaining": 1.5,
                 "policy": {"host": "api.opencorporates.com", "ratePerSec": 0.5, "burst": 2, "cost": 1}},
  "colo": "CDG", "client_country": "FR", "served_from": "origin"
}
```

`host_profile` names the profile that was applied, `credential` says what was
injected (never the value), and `rate_limit.policy` is the bucket that actually
governed the call — the way to confirm a profile capped a client's request.

### Errors

Two distinct shapes. **Relay-level refusals** carry a structured `error` object:

```json
{"ok": false, "error": {"code": "robots_disallowed",
                        "message": "robots.txt disallows /private",
                        "robots": {"allowed": false, "crawl_delay": 0},
                        "request_id": "…"}}
```

| HTTP | `error.code` | Meaning / client behaviour |
| --- | --- | --- |
| 400 | `bad_request` | Malformed envelope, missing `url`, unparsable JSON body, or SSRF guard tripped (non-HTTPS, private network, deny-listed host) |
| 400 | `invalid_target` | Target failed `validateTargetUrl` (bad scheme/host) |
| 401 | `unauthorised` | Missing/invalid bearer token |
| 403 | `robots_disallowed` | **Terminal** for the client: recorded as `http_robots_blocked`, never retried, never falls back to direct |
| 404 | `task_not_found`, `not_found` | Unknown task id / unknown route |
| 413 | `payload_too_large` | Control body over `MAX_CONTROL_BODY_BYTES` |
| 429 | `host_rate_limited` | Per-host bucket exhausted and `queue_on_limit` off (or Queue/KV unbound); `retry_after`, `remaining_tokens`, `policy` |
| 429 | `global_rate_limited` | Worker-wide `GLOBAL_RPM` budget exhausted; `retry_after` |
| 202 | — (`deferred: true`) | Queued: `{ok:false, deferred:true, task_id, status:"queued", retry_after, request_id}` |
| 502 | *(fetch envelope)* | Origin failure or timeout after all attempts — see below |
| 503 | `queue_unavailable`, `kv_unbound` | Queue/`RESULT_KV` not bound or enqueue failed |
| 500 | `internal_error` | Relay fault |

**Upstream outcomes** are returned in the *fetch* envelope, where `error` is a plain
message string and `status` is the real upstream code:

```json
{"ok": false, "status": 503, "url": "…", "request_url": "…", "method": "GET",
 "error": "upstream returned 503", "attempts": 3,
 "attempts_log": [{"attempt": 1, "status": 503, "retried_in_ms": 1400, "ua": "…"}],
 "elapsed_ms": 4120, "colo": "CDG", "served_from": "origin"}
```

Any non-OK upstream — including a legitimate `404` from the origin — surfaces as **HTTP
502**; read `status` in the body for the upstream code. A timeout appears as
`error: "timeout after 25000ms"`. The Python client classifies these into
`http_errors` and only falls back to a direct request when `DIRECT_FALLBACK_ENABLED` and
the failure is not a policy refusal (`robots_disallowed`, `401`).

### Deferred requests — `GET /tasks/{id}`

When `queue_on_limit` is set and the host bucket is empty, the Worker enqueues the task
with a delay and returns `202` + `task_id`. The queue consumer executes it later and
stores the outcome in `RESULT_KV` (TTL `TASK_RESULT_TTL_SECONDS`):

```json
{"ok": true, "status": "done", "task_id": "…", "request_id": "…", "url": "…",
 "source_id": "news_world", "attempts_in_queue": 1, "completed_at": 1790000000000,
 "result": {"ok": true, "status": 200, "url": "…", "content_type": "text/html",
            "headers": {}, "text": "…", "byte_length": 48213, "truncated": false,
            "attempts": 1, "elapsed_ms": 640, "served_from": "origin"}}
```

`status` is `queued` (still pending — the record then holds `retry_after` and
`enqueued_at`), `done`, or `failed` (`error` + `completed_at`, no `result`). The envelope
sets `ok: true` for both `queued` and `done`, so callers must branch on `status`.
`DELETE /tasks/{id}` returns `{ok:true, deleted:"<id>"}`.

The Python client polls every `PROXY_TASK_POLL_INTERVAL_SECONDS` (3 s) for up to
`PROXY_TASK_POLL_SECONDS` (180 s) and records the outcome as `http_deferred_to_queue` with
transport `worker_queue`.

---

## Politeness mechanics

**Fingerprint rotation.** A pool of coherent desktop fingerprints (UA ↔ `sec-ch-ua` ↔
platform ↔ `Accept-Language` kept in lockstep, because mismatched pairs are a trivial bot
signal) is selected by hashing `fingerprint_salt` + attempt number. The Python side
supplies `"{run_id}:{attempt}"`, so the same host sees a different identity on each retry
while one attempt stays internally consistent. `cache_buster` can add a rotating query
parameter.

Rotation is switched **off** for profiled API hosts (`api_mode`): see below.

**Per-host token bucket.** Refill `HOST_RATE_PER_SEC` (default 0.5 ≈ 1 req/2 s), burst
`HOST_BURST` (4). Buckets live in isolate memory and reconcile with `RATE_LIMIT_KV` every
`KV_SYNC_INTERVAL_MS` (20 s), so the limit holds across colos without a KV round-trip per
request. A per-request `rate_limit` override is honoured but clamped.

**Global safety valve.** `GLOBAL_RPM` (900, 600 in the `production` environment) protects
free-tier CPU and KV quotas across all hosts.

**robots.txt.** Fetched and cached in `RATE_LIMIT_KV` for `ROBOTS_TTL_SECONDS` (6 h) per
host + user-agent. `Crawl-delay` tightens the host bucket. A disallowed path is refused
with `403 robots_disallowed` *before* any egress.

**Retries.** `max_attempts` (default 3) with jittered exponential backoff
(`BACKOFF_BASE_MS` 700 → `BACKOFF_CAP_MS` 12 s), honouring `Retry-After`. Every attempt is
logged in `attempts_log` with the fingerprint used, so a blocked source is diagnosable.

**Caching.** `cache_ttl_seconds > 0` stores the response in `RESULT_KV`; a hit returns
`cached: true` with `X-PuppetNET-Cache: HIT`. `DELETE /cache?url=…` purges.

**SSRF guards.** `ENFORCE_HTTPS=true` refuses plaintext targets,
`ALLOW_PRIVATE_NETWORKS=false` blocks localhost/link-local/RFC1918, and
`BLOCKED_HOST_SUFFIXES` is a deny-list (e.g. `facebook.com,linkedin.com`).

**Response caps.** Bodies are read up to `MAX_RESPONSE_BYTES` (8 MiB); an oversized body
is returned truncated with `"truncated": true` rather than dropped.

**Cron trigger.** `30 4 * * *` prunes in-isolate buckets untouched for more than 6 h and
zeroes the global RPM window, logging how many buckets remain active.

---

## Host profiles

`HOST_PROFILES` in `worker.js` pins what this relay knows about the APIs the harvester
exists to serve. Matching is by **longest host suffix**, so `www.adsbdb.com` inherits
`adsbdb.com` and `adsbexchange-com1.p.rapidapi.com` inherits `p.rapidapi.com`.

| Profile | Host | Mode | Rate ceiling | Timeout | Cache TTL | Credential |
| --- | --- | --- | --- | --- | --- | --- |
| `wikidata-sparql` | `query.wikidata.org` | API | 0.25/s, burst 1 | 120 s | 1 h | — |
| `opencorporates` | `api.opencorporates.com` | API | 0.5/s, burst 2 | 30 s | 6 h | `OPENCORPORATES_API_TOKEN` |
| `adsbdb` | `adsbdb.com` | API | 0.5/s, burst 2 | 20 s | 12 h | — |
| `rapidapi-adsbexchange` | `p.rapidapi.com` | API | 1/s, burst 2 | 20 s | 5 min | `ADSBEXCHANGE_API_KEY` |
| `faa-registry` | `registry.faa.gov` | API | 0.1/s, burst 1 | 180 s | 24 h | — |
| `companies-house` | `api.company-information.service.gov.uk` | API | 2/s, burst 5 | 30 s | 6 h | `COMPANIES_HOUSE_API_KEY` |
| `icij-offshore-leaks` | `offshoreleaks.icij.org` | browser | 0.25/s, burst 2 | 60 s | 6 h | — |

**API mode (`api: true`).** Sends one stable, self-identifying client: a descriptive
`User-Agent` (Wikidata's policy requires contact details), `Accept: application/json`,
`Accept-Encoding` — and *no* `sec-ch-ua`, `Sec-Fetch-*` or `DNT`. These endpoints ask to
be told who is calling; presenting a rotating fake browser to an API is both against
their terms and easy to detect, and a Cloudflare Worker's IP range is shared with
everyone else. Genuine websites (ICIJ's search UI, news) keep the rotating fingerprint.

**Rate ceiling.** `min(what the caller asked for, what the host allows)`. A client can
ask to be slower, never faster: a hand-run loop or a bad config cannot make the relay
hammer Wikidata into a project-wide 429 ban. The applied values come back in
`rate_limit.policy`. Profile timeouts are themselves capped by `MAX_TIMEOUT_MS`.

**Wikidata SPARQL shape.** WDQS is a form endpoint, so a `POST` whose body carries
`query=` gets `format=json` and `maxlag=5` filled in when missing, and a caller that sent
`{"json": {"query": …}}` is converted to a proper `application/x-www-form-urlencoded`
body. `maxlag` matters: it lets WDQS answer "my replica is behind, come back later"
instead of queueing our query on a lagging cluster.

**Credentials at the edge.** `OPENCORPORATES_API_TOKEN`, `ADSBEXCHANGE_API_KEY`
(+ `RAPIDAPI_HOST`) and `COMPANIES_HOUSE_API_KEY` are Worker secrets. They are injected
**inside `performFetch`, on a per-call copy**:

* a deferred Queue task never contains them (so a retried task cannot leak one);
* the KV cache key is computed from the token-free URL, so two callers share entries;
* a URL-injected token is stripped by `redactUrl` from `url`, `request_url` and the
  stored task result before any of it is returned or persisted.

This also means a GitHub Actions runner needs no API keys at all when it goes through the
relay — the token stays on Cloudflare and out of the run log. An authenticated
OpenCorporates call carries a far higher daily quota, which is usually the difference
between "rate limited" and "harvested".

**Adding a profile** is a single entry in `HOST_PROFILES`; nothing else needs to change,
because `handleFetch` applies profiles generically after URL validation and before the
cache, robots and rate-limit steps (all three read what a profile sets).

### Testing the relay

```bash
node --check worker.js          # syntax
node tests/worker_smoke.mjs     # behaviour, no network
```

The smoke test runs the exported `fetch` handler against a stubbed upstream and asserts
the SPARQL form POST, the rate ceiling, credential injection for OpenCorporates and
RapidAPI, that no secret value appears in any response, that an unprofiled website still
gets browser fingerprints, and that `/health` lists the profiles. CI runs both.

---

## Deploy

```bash
npm i -g wrangler          # or: npx wrangler@latest
wrangler login
wrangler kv namespace create RATE_LIMIT_KV
wrangler kv namespace create RESULT_KV
wrangler queues create puppetnet-fetch-queue
# paste the ids / preview_ids into wrangler.toml, then:
wrangler secret put PROXY_AUTH_TOKEN      # openssl rand -hex 32
# Optional — the host profiles inject these on the outgoing request:
wrangler secret put OPENCORPORATES_API_TOKEN
wrangler secret put ADSBEXCHANGE_API_KEY
wrangler secret put COMPANIES_HOUSE_API_KEY
wrangler deploy                           # or: wrangler deploy --env production
curl -s https://<worker>.workers.dev/health | jq
```

Local development needs no bindings at all:

```bash
wrangler dev --local      # stateless relay with per-isolate rate limiting
```

Then point the harvester at it:

```bash
export PROXY_WORKER_URL=https://<worker>.workers.dev
export PROXY_AUTH_TOKEN=<the secret you just set>
python ingest.py --doctor        # probes /health and reports the bindings
```

### Tunables (`[vars]` in `wrangler.toml`)

| Var | Default | Purpose |
| --- | --- | --- |
| `HOST_RATE_PER_SEC` / `HOST_BURST` | `0.5` / `4` | Per-host bucket |
| `MIN_HOST_RATE_PER_SEC` / `MAX_HOST_BURST` | `0.02` / `20` | Floors/ceilings the Worker enforces on client overrides |
| `GLOBAL_RPM` | `900` | Worker-wide requests/minute |
| `KV_SYNC_INTERVAL_MS` | `20000` | Bucket ↔ KV reconciliation cadence |
| `DEFAULT_TIMEOUT_MS` / `MAX_TIMEOUT_MS` | `25000` / `90000` | |
| `DEFAULT_MAX_ATTEMPTS` / `MAX_MAX_ATTEMPTS` | `3` / `6` | |
| `MAX_RESPONSE_BYTES` / `MAX_CONTROL_BODY_BYTES` | `8388608` / `262144` | |
| `DEFAULT_CACHE_TTL_SECONDS` / `MAX_CACHE_TTL_SECONDS` | `0` / `86400` | |
| `ROBOTS_TTL_SECONDS` / `TASK_RESULT_TTL_SECONDS` | `21600` / `3600` | |
| `MAX_QUEUE_DELAY_SECONDS` | `300` | |
| `ENFORCE_HTTPS` / `ALLOW_PRIVATE_NETWORKS` / `BLOCKED_HOST_SUFFIXES` | `true` / `false` / *(empty)* | SSRF & policy guards |
| `ALLOWED_ORIGINS` | `*` | CORS for the PuppetNET front-end |
| `WIKIDATA_USER_AGENT` | PuppetNET/1.5 … | Identity presented to WDQS (their policy requires contact details) |

The `[env.production]` overlay tightens `GLOBAL_RPM` to 600, `HOST_RATE_PER_SEC` to 0.34
and `HOST_BURST` to 3 for hostile origins.
