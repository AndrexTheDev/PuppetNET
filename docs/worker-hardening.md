# Edge Worker hardening, cron maintenance and free-tier health

The Worker in [`apps/worker/worker.js`](../apps/worker/worker.js) is a public,
token-authenticated HTTP surface in front of two scarce resources: a single
AuraDB Free instance (200 000 nodes, 400 000 relationships, auto-pause after
72 hours without a write) and a Workers Free account (100 000 requests/day,
KV: 100 000 reads and 1 000 writes/day). Every measure below exists to keep those
two budgets intact when a caller is careless, a loop is tight, a redirect is
hostile or a cron run is duplicated.

| Control | Where | Failure it prevents |
| --- | --- | --- |
| 25 database requests/minute per caller and globally | `checkClientGraphRate`, token bucket | A refresh loop or a stolen token hammering AuraDB |
| Redirect-by-redirect SSRF validation | `guardedFetch` | `302 → http://169.254.169.254/` bypassing the URL check |
| Credential stripping across origins | `withoutCredentials` | A bearer/API key following a redirect to somebody else |
| KV lease before cron work | `acquireLock` | Two isolates running the same probe after a retry |
| Keep-alive **write** heartbeat | `runKeepAlive` | Aura pausing because `RETURN 1` is not a write |
| Snapshot + `/status` | `runHealthCheck`, `handleStatus` | Discovering the free-tier ceiling from an outage |
| Auth lockout per address | `recordAuthFailure` | Unlimited token guessing |
| Security headers on every response | `corsHeaders` | Framing, referrer leaks, MIME sniffing |

---

## 1. The 25 requests/minute database budget

`GRAPH_RPM` and `GRAPH_CLIENT_RPM` default to **25** and are clamped **downward
only**:

```js
ratePerSec: num(env.GRAPH_RATE_PER_SEC, DEFAULTS.GRAPH_RPM / 60, 0.01, DEFAULTS.GRAPH_RPM / 60),
burst:      num(env.GRAPH_BURST,      DEFAULTS.GRAPH_RPM,        1,    DEFAULTS.GRAPH_RPM),
```

An operator who sets `GRAPH_RATE_PER_SEC=200` (the old default) or `GRAPH_RPM=999`
gets 25, not a faster way to exhaust the database. The ceiling is a product
decision, not a tuning knob.

**Two layers, on purpose.**

1. **Token bucket** (`acquireHostToken`, host key `graph:neo4j`) shapes bursts
   across the fleet and is the only part that syncs through KV. It is the
   sustained-rate control: 25 tokens, refilled at 25/60 per second.
2. **Fixed window per caller** (`checkClientGraphRate`) is the literal "25 per
   clock minute" rule. It is in-isolate and has no KV round trip, so the hot path
   costs no quota. A caller is identified by `CF-Connecting-IP` (hashed with an
   optional `CLIENT_KEY_SALT`); `X-Forwarded-For` is deliberately ignored because
   a caller controls it.

The relay's `/fetch` is deliberately **not** part of this budget: it never touches
AuraDB. It keeps its own per-host token buckets and the 900/min `GLOBAL_RPM` safety
valve, which exist for a different reason (not getting the harvester blocked).
Capping the relay at 25/min would make the daily news run impossible without
protecting a database it does not use.

The KV sync for the graph bucket uses `GRAPH_KV_SYNC_INTERVAL_MS=300000`
(5 minutes), not the relay's 20-second cadence: a constantly-used console would
otherwise spend ~1 440 writes/day on publishing a bucket, i.e. more than the
entire KV Free write allowance. At five minutes the worst case is 288 writes/day,
and a rate-limited caller still publishes immediately.

Every graph response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset`; a refusal is `429 graph_rate_limited` with `retry_after`.
`/graph/health` is subject to the same budget **and** caches its three-query
probe for 60 seconds (`GRAPH_HEALTH_CACHE_SECONDS`), because it is public: before
this change, a loop over that URL ran three Cypher statements per request.

What 25/min means in daily terms: at most 36 000 database requests/day, well
inside the Workers request allowance, and 25 concurrent page loads cannot all
refresh at once. The console's own cache (60 s) means most navigation costs zero
queries.

## 2. SSRF: validate the redirect, not just the URL

The first hop was already checked (`validateTargetUrl`: protocol, HTTPS policy,
hostname, IPv4/IPv6 private ranges, wildcard-DNS services). Three holes remained:

1. **`redirect: "follow"`.** The runtime followed a `302` internally, so a public
   hostname could point the Worker at `169.254.169.254` without the guard ever
   seeing the second URL. `guardedFetch` now uses `redirect: "manual"`, resolves
   `Location` against the current URL and re-validates every hop. Five hops is
   the limit; a chain that needs more is refused as `too_many_redirects`.
2. **`resolve_override`.** The control envelope accepted a caller-supplied DNS
   override, which is a direct route to a private address even when the hostname
   is public. The field is gone; `cf.resolveOverride` is never set.
3. **Credentials across origins.** Caller headers may include an API key. A
   redirect to another origin now drops `authorization`, `cookie`,
   `x-api-key`, `x-rapidapi-*`, `x-proxy-token`, `x-graph-token` and
   `x-status-token`; non-credential headers still follow.

Additional rules:

* URLs with embedded credentials (`https://user:pass@host/`) are refused — they
  end up in logs and `Referer` chains.
* Ports `22, 23, 25, 445, 1433, 2375, 2376, 3306, 5432, 6379, 9200, 11211,
  27017` are blocked (`BLOCKED_TARGET_PORTS`). This is a blocklist, not an
  allowlist: the relay legitimately talks to APIs on 8080/8443 and to a local
  Neo4j on 7474.
* IPv6 zone identifiers are refused; WHATWG already rejects most of them.
* `robots.txt` is fetched through the same guarded path (two hops), so a rules
  file cannot redirect the crawler into a private network.
* The Neo4j endpoint is operator configuration, but it is validated too. A
  `https://10.0.0.5:7474` typo is refused; local plaintext development
  (`http://localhost:7474`) remains explicitly allowed. The transaction POST and
  the Cloudflare Analytics query both send `redirect: "error"`: those two calls
  carry a Basic password and a Bearer token to a fixed host, and neither has any
  legitimate reason to be redirected.

**What this cannot do.** A Worker has no resolver API and cannot see the address
behind a hostname. A public name that resolves to a private address is stopped by
Cloudflare's platform egress policy, not by this JavaScript. The guard's contract
is therefore: no literal private address, no encoded form, no blocked port, no
plaintext downgrade and no unchecked redirect ever reaches `fetch`. The
`resolve_override` removal closes the one application-level way to bypass DNS
policy.

## 3. Distributed locks on Workers KV

```js
const lock = await acquireLock(env, "keepalive", 600, { job: "keepalive" });
if (!lock.acquired) { /* another isolate holds a live lease */ }
try { /* idempotent work */ } finally { await releaseLock(env, "keepalive", lock.owner); }
```

KV is **eventually consistent and last-write-wins**. It cannot provide a
linearizable mutex, and the code says so. The lease is deliberately advisory:

* it stops the common duplicate — a retried or manually re-triggered cron
  invocation in the same or a nearby isolate;
* it carries `until` and a KV `expirationTtl`, so a crashed run cannot wedge the
  schedule forever;
* the work under the lock is idempotent: one `MERGE` heartbeat, one snapshot
  overwrite. If two colos race, the second write is harmless;
* without `RATE_LIMIT_KV` it degrades to isolate memory and reports
  `backend: "memory"` rather than pretending to be distributed.

Cost: one read + one write per acquire, one read + one delete per release. The
cron runs 10 times a day, so the locks cost tens of operations, not thousands.
`/status?live=1` and `/status/refresh` take the same kind of short lease, so an
operator holding down the button does not queue up full graph scans. A live probe
also **skips the KV write** when the stored snapshot is younger than five
minutes: at the 30-second lease cap, persisting every manual refresh would cost
up to 2 880 writes/day — three times the Free quota — while showing the operator
the same numbers.

## 4. Cron triggers

`wrangler.toml`:

```toml
[triggers]
crons = ["0 */12 * * *", "23 */3 * * *"]
```

| Expression | Job | Why |
| --- | --- | --- |
| `0 */12 * * *` | keep-alive | Aura pauses after 72 h without a write; 12 h leaves a six-fold margin |
| `23 */3 * * *` | snapshot | 8 probes/day is enough to see a quota trend; minute 23 avoids the top-of-hour pile-up |

**Why not `0 */36 * * *`?** A five-field cron has an hour field of `0-23`. A
36-hour step selects hour 0 only, so it silently means "daily" (and stricter
parsers reject the step). "Every 36 hours" is not expressible without a
stateful scheduler; 12 hours preserves the intent with more margin. Workers Free
allows five cron triggers per account; this uses two, leaving room for a future
maintenance job.

The Worker reads `event.cron`:

* the keep-alive schedule runs only `runKeepAlive`;
* the snapshot schedule runs only `runHealthCheck`;
* an unknown schedule (added in the dashboard, not in `wrangler.toml`) runs both
  idempotent jobs instead of doing nothing and looking healthy.

### The heartbeat is a write

Neo4j's Free-tier FAQ defines inactivity as **three days without a write query**;
read queries do not reset the timer. The Python `keep_alive.py` job's
`RETURN 1` proves reachability (and waking a paused instance still succeeds), but
it does not by itself prevent the pause. The cron therefore issues exactly one
bounded write:

```cypher
MERGE (h:_PuppetNetHeartbeat {id: $id})
SET h.last_seen = datetime(), h.worker = $worker, h.version = $version
RETURN toString(h.last_seen) AS last_seen
```

`MERGE` on a fixed id means the graph grows by at most one node regardless of
how many times it runs, and the id is configurable (`KEEPALIVE_NODE_ID`). That
node is never published and never enters the entity budget's meaningful count,
but it does appear in `count(n)` — the dashboard shows it as part of the total,
which is the honest number Aura's own limit uses.

### Snapshot job

`runHealthCheck` performs two counts (`MATCH (n)`, `MATCH ()-[r]->()`) and, when
`CF_API_TOKEN`/`CF_ACCOUNT_ID` are configured, one Cloudflare GraphQL query for
today's Worker requests. It writes:

* `health:snapshot` — the dashboard payload (30-day TTL, overwritten each run);
* `health:daily:YYYY-MM-DD` — runs, failures, last node/relationship counts
  (14-day TTL), read-modify-write, two KV operations per cron run;
* `health:keepalive` — the last keep-alive result (7-day TTL).

If the database is unreachable, the snapshot is still written with
`ok: false`, the error code and the degraded reasons. A monitoring page that
disappears during an incident is worse than no page.

## 5. `/status`: the free-tier dashboard

| Route | Method | Auth | Response |
| --- | --- | --- | --- |
| `/status` | GET | `STATUS_TOKEN`, graph or relay token | HTML, 60-second meta refresh |
| `/status.json` | GET | same | The same snapshot as JSON |
| `/status/refresh` | POST | token only, never public | Runs a live probe and returns JSON; persists a snapshot only if the stored one is older than 5 min |

`STATUS_PUBLIC=true` opens the **read-only** view; a live probe always needs a
token, because it costs two full graph scans. With neither a status nor an API
token configured, the route fails closed.

What it shows:

* **Workers requests today** from the Cloudflare GraphQL Analytics API
  (`workersInvocationsAdaptive`), with the 100 000/day ceiling. This needs a
  token with *Account Analytics: Read* in `CF_API_TOKEN` and `CF_ACCOUNT_ID`.
  Without them it says `n/a`, not `0` — the Worker cannot see the account-wide
  counter, and inventing one from a single isolate would be worse than nothing.
* **AuraDB nodes and relationships** from the last snapshot, against 200 000 and
  400 000, with the warning at 90 % that matches the pipeline's
  `AURA_NODE_STOP=180000`.
* **Graph budget**: the 25/min limit, requests used this minute, remaining.
* **KV limits and lock backend** and the two cron expressions.
* **Last keep-alive, last probe, snapshot age and every degraded reason.**

The page is server-rendered, has no script, no external asset and no inline
handler. A stored snapshot is served for up to 15 minutes; `?live=1` (token)
forces a probe and persists it only when the stored copy is older than five
minutes, which keeps repeated manual refreshes inside the KV write budget. Its CSP is `default-src 'none'; style-src 'unsafe-inline'`, it sends
`noindex, nofollow`, `X-Frame-Options: DENY` and `no-store`. Every value is
escaped (`escapeHtml`), including error messages that originate in Neo4j.

## 6. Authentication hardening

* Bearer comparison is constant-time (`constantTimeEqual`) and the Worker fails
  closed when no token is configured.
* Ten failed attempts from one address (`AUTH_FAILURE_LIMIT`) lock that address
  for 15 minutes (`AUTH_FAILURE_WINDOW_MS`). The lock covers the relay, graph and
  status token checks — `/status` and `/status/refresh` record failures exactly
  like `/graph/*` and `/fetch`; a correct token from a locked address is refused
  too, and a different address is unaffected.
* The lock is in-isolate, like the fixed rate window. A distributed brute-force
  defence would need a Durable Object or a paid WAF; for a single-token API this
  is the right cost/benefit point, and it is documented rather than implied.
* `WWW-Authenticate: Bearer realm="…"` is returned on 401 so a client can tell
  "missing token" from "wrong route".

## 7. Free-tier arithmetic

| Resource | Free limit | What this deployment spends |
| --- | --- | --- |
| Workers requests | 100 000/day | Console + cron; 10 cron invocations/day is negligible |
| Cron triggers | 5/account | 2 |
| KV reads | 100 000/day | Locks (2/cron), robots cache, graph bucket sync, dashboard reads |
| KV writes | 1 000/day | ~30/day from cron + ≤288/day from a constantly-used graph bucket + ≤288/day if `/status?live=1` is refreshed every 5 min |
| KV deletes | 1 000/day | 2 per lock release, ~20/day |
| Aura nodes | 200 000 | `AURA_NODE_STOP=180000` in the pipeline, plus one heartbeat node |
| Aura relationships | 400 000 | `AURA_EDGE_CAP=400000` |
| Aura idle pause | 72 h without a write | Reset every 12 h by the cron |

No paid binding is enabled: no Queues, no Durable Objects, no Workers Paid.

## 8. Deploy checklist

```bash
cd apps/worker
npx wrangler kv namespace create PUPPETNET_RATE_LIMIT
npx wrangler kv namespace create PUPPETNET_RESULTS
# copy both ids and preview_ids into [[kv_namespaces]] in wrangler.toml
npx wrangler secret put PROXY_AUTH_TOKEN
npx wrangler secret put STATUS_TOKEN          # or reuse GRAPH_API_TOKEN
npx wrangler secret put NEO4J_URI
npx wrangler secret put NEO4J_USERNAME
npx wrangler secret put NEO4J_PASSWORD
npx wrangler secret put GRAPH_API_TOKEN
# optional, for the Workers-requests card:
npx wrangler secret put CF_API_TOKEN          # Account Analytics: Read
npx wrangler secret put CF_ACCOUNT_ID
npx wrangler deploy
```

Verify after deploying:

```bash
curl -s https://<worker>/health | jq '{version, bindings, limits}'
curl -s -H "Authorization: Bearer $STATUS_TOKEN" https://<worker>/status.json | jq
curl -s -o /dev/null -w '%{http_code}\n' https://<worker>/status   # 401 without a token
npx wrangler triggers list                                          # both crons present
```

The placeholder namespace ids in `wrangler.toml` are intentional: `wrangler
deploy` must fail until real ids are pasted in, rather than deploy a Worker whose
locks silently fall back to isolate memory.

## 9. Failure modes and what to do

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/status` says `AuraDB nicht erreichbar`, `neo4j_error` | paused instance, rotated password, wrong URI | Resume in the Aura console; check `NEO4J_*` secrets; the next cron retries |
| `n/a` for Workers requests | `CF_API_TOKEN`/`CF_ACCOUNT_ID` missing or lacking Account Analytics | Create a read-only token; expected without it |
| `429 graph_rate_limited` for the console | more than 25 calls/minute from one address | Wait for `retry_after`; the console cache absorbs ordinary navigation |
| `429 auth_locked` | ten bad tokens from that address | Wait 15 minutes or rotate the token; check for a client with a stale secret |
| KV writes fail | 1 000/day exhausted | Look at the graph bucket sync and lock cadence; lowering `GRAPH_KV_SYNC_INTERVAL_MS` raises cost, not lowers it |
| `skipped: locked` repeatedly | an earlier run died before release | The 10/15-minute lease expires on its own; check the cron logs |
| `511`/`1027` from Cloudflare | Workers daily request limit | The Worker is down until 00:00 UTC; this is the plan ceiling, not a bug |

## 10. Verification

```bash
npm run ci:worker          # syntax + relay/graph smoke + hardening suite
node tests/worker_hardening.mjs
node tests/worker_smoke.mjs
```

`tests/worker_hardening.mjs` covers exactly the claims above: 25/min with the
26th refused and a second caller unaffected, `GRAPH_RPM=999` clamped, the health
cache, credential/port/zone refusal, redirect-to-metadata refusal, cross-origin
credential stripping, HTTPS downgrade, redirect loops, `resolve_override`
removal, KV lease acquisition/contention/expiry/release, the memory fallback,
the keep-alive **write**, duplicate suppression, snapshot success/failure, cron
expression shape, dashboard rendering/escalation/auth, the per-address status
lockout, the no-rewrite rule for fresh snapshots, and the security headers.
