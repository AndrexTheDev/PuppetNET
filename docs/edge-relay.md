# Edge fetch relay (`worker.js`)

A Cloudflare Worker with two independent halves. The **fetch relay** performs every
outbound HTTP request on behalf of the Python harvester: make the traffic look like
ordinary browser traffic from many places, and keep it polite enough that a daily cron
never gets a source blocked. The **graph read API** answers the analyst console
([`web/`](../web), documented in [web-console.md](web-console.md)) straight from Neo4j,
so the database credentials never reach a browser.

* Source: [`worker.js`](../worker.js) · config: [`wrangler.toml`](../wrangler.toml)
* Runtime: Workers (V8 isolate), `nodejs_compat`, free-tier CPU budget (10 ms per
  request; the relay spends its CPU on string work and JSON shaping and waits on I/O —
  fetch, KV, the Neo4j round trip — which Cloudflare does not count as CPU)
* All bindings optional — without KV/Queues the relay degrades to a stateless forwarder
  with per-isolate rate limiting.
* Version `1.6.2`. The graph API is `graph/1`; every response says so.

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
| `GET` | `/graph/health` | public | Graph API liveness, limits, entity/index counts |
| `GET` | `/graph/*` | graph token | Read-only graph queries — see [Graph read API](#graph-read-api) |
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
  "version": "1.6.2",
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
  "version": "1.6.2",
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

## Graph read API

`/graph/*` is what the analyst console calls. It is **read-only** (any method but `GET`
is refused with `405`), it authenticates separately from the relay, and it holds the
Neo4j credentials server-side: the browser sends a graph token, the Worker sends Basic
auth upstream, and neither the URI nor the password ever appears in a response — even in
an error, which is why `/graph/health` reports the *host* rather than `NEO4J_URI`.

**Auth** — `Authorization: Bearer <GRAPH_API_TOKEN>` (or `X-Graph-Token`).
`GRAPH_API_TOKENS` accepts a comma-separated list for rotation, comparison is
constant-time, and the API fails closed: with no token configured every route is
rejected unless `GRAPH_PUBLIC_READ=true`, which exists for a console that genuinely has
no secret to hold and is off by default. `GRAPH_API_ENABLED=false` turns the whole
surface off with `403`.

### Endpoints

| Route | Parameters | Returns |
| --- | --- | --- |
| `/graph/health` | — | `ok`, version, `graph.limits`, entity/edge/index counts, whether `puppetnet_entity_search` is `ONLINE`, the endpoint list. Always `200`: a monitor needs the detail, `ok` carries the verdict |
| `/graph/overview` | `limit` (≤1200, default 250), `metric`, `type` | top entities ranked by an allowlisted metric, with the induced edges between them |
| `/graph/search` | `q` (≤400 chars), `limit`, `type` | full-text search when the index exists, otherwise a scored `CASE` fallback: exact key 100, id 95, name 90, prefix 70, contains 50, alias 40, jurisdiction 12 |
| `/graph/node` | `key` | the entity, its edges, its neighbours and up to 40 citations |
| `/graph/neighbors` | `key`, `depth` (1–4), `limit`, `min_weight`, `types` | level-by-level BFS. Induced edges include ties that were not traversed (a strong second-hop tie is evidence), plus a `truncated` flag |
| `/graph/path` | `from`, `to`, `max_hops` (≤12), `direction`, `cost` | shortest path by hops, `inverse-weight` or `inverse-confidence`, up to 3 alternatives. Weighted searches are capped at 4 hops and 20 000 enumerated paths |
| `/graph/table` | `subject=nodes\|edges\|sources`, `limit`, `skip`, `sort`, `order`, `q`, `type`/`types`, `min_weight`, `min_confidence` | paged rows for the console's table view |

`direction` is `undirected` (default), `outgoing` or `incoming`; `cost` is `hops`
(default), `inverse-weight` or `inverse-confidence`. Property names cannot be Cypher
parameters, so `metric`, `sort`, `type` and `types` are matched against fixed allowlists
(`GRAPH_SORTABLE`, `GRAPH_LABELS`, `GRAPH_REL_TYPES`) and silently fall back when a caller
invents one — the only defence that actually works for an `ORDER BY`.

### Envelope

Every success is one JSON object:

```json
{
  "ok": true,
  "subject": "neighbors",
  "root": "PERSON:vladimir-kastelion-9f2c1a7b",
  "depth": 2,
  "nodes": [{ "key": "…", "name": "…", "entity_type": "Person", "labels": ["Entity", "Person"],
              "betweenness": 0.412, "anomaly_score": 0.83, "degree": 4, "cluster_id": 7 }],
  "edges": [{ "id": 9001, "type": "OWNS", "source": "…", "target": "…",
              "weight": 0.92, "confidence": 0.92, "method": "nlp:leaks", "source_id": "icij:…" }],
  "truncated": false,
  "took_ms": 41,
  "auth_mode": "token",
  "source": "worker",
  "api": "graph/1",
  "cached": true
}
```

Errors are `{"ok": false, "error": {"code": "…", "message": "…"}}` with a status that
means something: `400` bad request, `401` unauthorised, `403` graph disabled, `404`
unknown route, `405` not `GET`, `429` rate-limited (graph budget or Worker-wide, with
`retry_after`), `502` upstream/refused (including `response_too_large`, which protects
both the free-tier database and the browser). Response headers carry
`X-PuppetNET-Cache: HIT|MISS`, `X-PuppetNET-Took-Ms` and
`X-PuppetNET-Ratelimit-Remaining`.

```bash
curl -s "$WORKER/graph/neighbors?key=PERSON:vladimir-kastelion-9f2c1a7b&depth=2" \
     -H "Authorization: Bearer $GRAPH_API_TOKEN"
```

### Limits, caching and politeness

Each handler issues **one** transaction POST to Neo4j, with an `AbortController` timeout
(`GRAPH_TIMEOUT_MS`, default 20 s). Graph queries draw from their own token bucket
(`GRAPH_RATE_PER_SEC` 4/s, `GRAPH_BURST` 8) keyed `graph:neo4j`, separate from the relay's
per-host buckets, so a console left open on a dashboard cannot starve the harvester.
Successful responses are cached (`GRAPH_CACHE_TTL_SECONDS`, default 60, `0` disables) when
a KV binding exists; a cache hit skips both Neo4j and the rate bucket. Serialised payloads
over `GRAPH_MAX_RESPONSE_BYTES` (default 6 MiB) are refused rather than shipped.

Traversal bounds are the reason this API can face the open internet: depth ≤ 4, hops ≤ 12
(weighted ≤ 4), 20 000 enumerated paths, 1200 nodes, 2000 table rows, 400-character
queries, 40 citations, 8 evidence items. Variable-length patterns are expanded
level-by-level instead of `[*1..4]`, which would enumerate every path in the graph.

### Testing the graph API

`tests/worker_smoke.mjs` (32 checks) runs the exported `fetch` handler against a fake
Neo4j built on the same fixture the console tests use — no wrangler, no network, no
bindings. It covers the projection contract for every route, the search fallback when the
full-text index is missing, auth and fail-closed behaviour, rate limiting, cache
headers, payload refusal, secret redaction (`assertNoGraphSecrets` on every response
body) and the read-only guarantee: every statement the Worker sent is checked for write
clauses, with the detector self-tested so the assertion cannot pass vacuously.

---

## CORS

`Access-Control-Allow-Origin` names exactly **one** origin, or `*`; a
comma-separated list is rejected by every browser. So the answer is resolved per
request rather than stored:

* an `Origin` that `ALLOWED_ORIGINS` lists is echoed back verbatim;
* an origin that is not on the list receives **no** `Access-Control-Allow-Origin`
  at all — which *is* the refusal, since the browser then hides the response;
* a deployment whose `ALLOWED_ORIGINS` is `*` answers `*`;
* a caller that sends no `Origin` (curl, an uptime probe, the queue consumer) is
  answered with the single allowed origin when there is exactly one.

`Vary: Origin` rides on every response, because the answer depends on the request,
and `X-Content-Type-Options: nosniff` rides with it, because route names and error
messages are echoed back as JSON. `OPTIONS` preflights are answered `204` under the
same rules and without a bearer token — a preflight never carries `Authorization`.
Errors carry the CORS headers too: without them a browser reports a CORS failure
instead of the real `401`, and the analyst debugs the wrong thing.

## The SSRF guard

The relay fetches URLs a caller supplies, so the address policy is enforced before any
egress, on a **normalised** host:

* a trailing dot is the DNS root and names the same host, so `localhost.` is `localhost`
  and `www.example.org.` is still `www.example.org` (the same normalisation applies to
  `BLOCKED_HOST_SUFFIXES`);
* IPv4 in any other notation — integer (`2130706433`), hex (`0x7f.1`), octal, unicode
  digits — never reaches the check, because the WHATWG URL parser has already canonicalised
  it to `127.0.0.1`. The smoke test pins that down, since the guard relies on it;
* IPv6 is decided by leading hextet: `::` (loopback, unspecified, every IPv4-mapped form),
  `fc00::/7` unique local, `fe80::/10` link local, `fec0::/10` site local, `ff00::/8`
  multicast — a hostname that merely *begins* with those letters is not an address;
* IPv4 ranges: this-network `0/8`, RFC1918 `10/8` `172.16/12` `192.168/16`, loopback
  `127/8`, link-local `169.254/16` (the cloud metadata service), carrier-grade NAT
  `100.64/10` (which cloud internals use), IETF assignments `192.0.0/24`, benchmarking
  `198.18/15`, and everything from `224/4` up;
* wildcard-DNS services are refused by **name** (`nip.io`, `sslip.io`, `xip.io`,
  `localtest.me`, `lvh.me`, `traefik.me`, `vcap.me`, `lacolhost.com`): they answer with the
  address encoded in the name, so no literal-IP test can ever see one, and an edge Worker
  has no resolver to ask.

`ENFORCE_HTTPS=false` and `ALLOW_PRIVATE_NETWORKS=true` are the two deliberate ways to
relax this, and nothing else relaxes it.

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

### Free-tier KV budget

Workers KV Free allows **100 000 reads and 1 000 writes per day**, and writes are the scarce
resource — the scheduled cadence is built around that number.

The relay therefore only touches KV when there is something to coordinate. A host that
receives fewer than two requests inside one `KV_SYNC_INTERVAL_MS` window is *cold*: no
bucket read, no bucket write, and no `robots.txt` cache entry. A host that is requested
again in the window is *hot* and costs at most one read plus one write per window; a host
that exhausted its budget publishes that immediately. The deferred-queue path writes one
`RESULT_KV` entry per *deferred task*, which only happens for hot hosts.

Why this matters here: an hourly news run fetches every article from its own host, so it
touches hundreds of hosts with one request each, 24 times a day.

| Shape | KV ops if every host is synced | KV ops with the hot-host rule |
| --- | --- | --- |
| One hourly run, ~400 article hosts, no host requested twice | ~400 writes (+ robots writes per host) | **0 writes**, 400 robots reads |
| The same host across a 15-minute run | up to ~45 writes (one per window) | ≤ 45 writes, same |
| A day of hourly runs (24) | ≫ 1 000 writes — the quota is gone before noon | bounded by the few hosts that are actually hot |

Losing the quota would not cost money (KV writes simply fail, and the relay catches the
error), but the fleet would lose its shared rate limits and its robots cache — i.e. it would
become less polite, which is the whole point of the relay. `[cost] worker/kvWritePerRequest`
in [`scripts/audit.mjs`](../scripts/audit.mjs) fails the build if a write ever returns to the
per-request path, and the smoke suite asserts the behaviour (a cold host costs one robots
read and nothing else; six requests inside one window cost at most one write).

The evaluator follows the specification's own vocabulary: group selection is by
user-agent substring (a group written for another crawler does not apply, and `*` is the
fallback), `*` matches any run of characters, a trailing `$` anchors the end of the path,
and matching is a **prefix** match — `/fish` covers `/fish/chips` and `/fishheads`. The
longest matching pattern wins, `Allow` breaks a tie against `Disallow` of the same length,
and a `401`/`403` on robots.txt itself is read as "everything disallowed".

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
# The graph read API (the analyst console). These stay on the Worker: the browser
# is given GRAPH_API_TOKEN and never sees the database credentials.
wrangler secret put NEO4J_URI             # neo4j+s://<instance>.databases.neo4j.io:7687
wrangler secret put NEO4J_USERNAME
wrangler secret put NEO4J_PASSWORD
wrangler secret put GRAPH_API_TOKEN       # openssl rand -hex 16
wrangler deploy                           # or: wrangler deploy --env production

# The shipped configuration sets workers_dev = false, so the Worker has no
# workers.dev address: it answers on the routes you publish (see `routes` in
# wrangler.toml) and, locally, wherever `wrangler dev` says. If the account has
# no domain to route yet, set workers_dev = true and use the <worker>.workers.dev
# hostname wrangler prints instead.
export WORKER_HOST=puppetnet-relay.example.com
curl -s https://$WORKER_HOST/health | jq
curl -s https://$WORKER_HOST/graph/health | jq '.graph.counts'
```

Local development needs no bindings at all:

```bash
wrangler dev --local      # stateless relay with per-isolate rate limiting
```

Then point the harvester at it:

```bash
export PROXY_WORKER_URL=https://$WORKER_HOST   # the host you routed above
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
| `GRAPH_API_ENABLED` / `GRAPH_PUBLIC_READ` | `true` / `false` | Graph surface on/off; tokenless reads (off by default — it fails closed) |
| `GRAPH_RATE_PER_SEC` / `GRAPH_BURST` | `4` / `8` | Graph token bucket, separate from the relay's per-host buckets |
| `GRAPH_CACHE_TTL_SECONDS` | `60` | `0` disables caching of graph responses |
| `GRAPH_TIMEOUT_MS` | `20000` | Per-transaction `AbortController` deadline |
| `GRAPH_MAX_NODES` / `GRAPH_MAX_DEPTH` / `GRAPH_MAX_HOPS` | `1200` / `4` / `12` | Ceilings the Worker enforces on caller overrides |
| `GRAPH_MAX_RESPONSE_BYTES` | `6291456` | Serialised payload ceiling; over it, `502 response_too_large` |
| `GRAPH_PATH_ALTERNATIVES` | `true` | Return up to 3 alternative routes with a handshake |

Every statement stays inside Cypher 5 as originally released: `ORDER BY`, `SKIP`
and `LIMIT` appear only as subclauses of the `WITH` or `RETURN` they belong to,
never as standalone clauses, which only parse from Neo4j **5.24** onward. A free
Aura instance is provisioned with whichever 5.x is current, so the floor here is
5.0 rather than "whatever the neighbour has". `scripts/audit.mjs` enforces the rule
statically (`cypher/standaloneOrderBy`) and the worker smoke test asserts it on
every statement the handlers emit.
| `NEO4J_DATABASE` | `neo4j` | Database in the transactional URL path |

The `[env.production]` overlay tightens `GLOBAL_RPM` to 600, `HOST_RATE_PER_SEC` to 0.34
and `HOST_BURST` to 3 for hostile origins.
