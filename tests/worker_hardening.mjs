/**
 * worker_hardening.mjs — MP6 contract: database budget, SSRF, KV locks, cron
 * and the free-tier status dashboard.
 *
 *   node tests/worker_hardening.mjs
 *
 * Everything runs offline: `fetch` is stubbed per scenario, KV is a Map with
 * operation counters, and Neo4j answers with the smallest envelope the Worker
 * actually parses. The suite asserts the security properties that are easy to
 * lose in a refactor:
 *
 *   * the 25-request/minute database ceiling (and that an environment cannot
 *     raise it),
 *   * SSRF protection on the *redirect chain*, not just the first URL,
 *   * credential stripping when a redirect changes origin,
 *   * KV locks that expire, respect an existing holder and degrade loudly,
 *   * a cron that writes a real AuraDB heartbeat and deduplicates itself,
 *   * a status page with no script, escaped data and a token by default.
 */

import assert from "node:assert/strict";

const workerModule = await import("../worker.js");
const worker = workerModule.default;
const internals = workerModule.__internals;

const NEO4J = {
  uri: "neo4j+s://abcd1234.databases.neo4j.io:7687",
  user: "neo4j",
  password: "aura-SECRET-do-not-leak",
};

const BASE_ENV = {
  PROXY_AUTH_TOKEN: "relay-token-abc123",
  GRAPH_API_TOKEN: "graph-token-xyz789",
  STATUS_TOKEN: "status-token-555",
  NEO4J_URI: NEO4J.uri,
  NEO4J_USERNAME: NEO4J.user,
  NEO4J_PASSWORD: NEO4J.password,
  NEO4J_DATABASE: "neo4j",
  GRAPH_CACHE_TTL_SECONDS: "0",
  GRAPH_RATE_PER_SEC: "0.4",
  GRAPH_BURST: "25",
};

const ctx = () => ({ waitUntil: () => {}, passThroughOnException: () => {} });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let checks = 0;
async function check(name, fn) {
  try {
    internals.resetRateLimits();
    await fn();
    checks += 1;
    console.log(`  ok  ${name}`);
  } catch (error) {
    console.error(`FAIL  ${name}`);
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
  }
}

function makeKv(initial = new Map()) {
  const ops = { gets: 0, puts: 0, deletes: 0 };
  return {
    store: initial,
    ops,
    async get(key, type) {
      ops.gets += 1;
      const raw = initial.get(key);
      if (raw === undefined) return null;
      if (type === "json") return JSON.parse(raw);
      return raw;
    },
    async put(key, value, options = {}) {
      ops.puts += 1;
      initial.set(key, String(value));
      return { key, value, options };
    },
    async delete(key) {
      ops.deletes += 1;
      initial.delete(key);
    },
  };
}

function fakeNeo4j({ nodes = 1234, relationships = 5678, fail = false } = {}) {
  const calls = [];
  async function stub(target, init = {}) {
    const url = new URL(String(target));
    if (/\/db\/[^/]+\/tx\/commit$/.test(url.pathname)) {
      const body = JSON.parse(String(init.body || "{}"));
      calls.push({ url: url.toString(), statements: body.statements || [], headers: init.headers });
      if (fail) {
        return new Response(JSON.stringify({ results: [], errors: [{ code: "Neo.ClientError.Security.Unauthorized", message: "auth failed" }] }), { status: 401, headers: { "content-type": "application/json" } });
      }
      const results = (body.statements || []).map((item) => {
        const statement = String(item.statement || "");
        if (/count\(n\)/.test(statement)) return { columns: ["nodes"], data: [{ row: [nodes] }] };
        if (/count\(r\)/.test(statement)) return { columns: ["relationships"], data: [{ row: [relationships] }] };
        if (/MERGE \(h:_PuppetNetHeartbeat/.test(statement)) return { columns: ["last_seen"], data: [{ row: ["2026-10-19T15:00:00Z"] }] };
        if (/RETURN 1 AS ok/.test(statement)) return { columns: ["ok"], data: [{ row: [1] }] };
        return { columns: [], data: [] };
      });
      return new Response(JSON.stringify({ results, errors: [] }), { status: 200, headers: { "content-type": "application/json" } });
    }
    throw new Error(`unexpected upstream ${url.toString()}`);
  }
  return { stub, calls };
}

async function call(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.token !== null && options.token !== undefined) headers.authorization = `Bearer ${options.token}`;
  const request = new Request(`https://relay.example.invalid${path}`, {
    method: options.method || (options.body === undefined ? "GET" : "POST"),
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const response = await worker.fetch(request, options.env || BASE_ENV, ctx());
  const text = await response.text();
  let body = null;
  try { body = JSON.parse(text); } catch (_) { body = null; }
  return { status: response.status, body, text, headers: response.headers };
}

const originalFetch = globalThis.fetch;
async function withFetch(stub, fn) {
  globalThis.fetch = stub;
  try { return await fn(); } finally { globalThis.fetch = originalFetch; }
}

console.log("worker hardening — MP6 behavioural contract");

/* -------------------------------------------------------------------------- */
/*  1. The database budget                                                     */
/* -------------------------------------------------------------------------- */

await check("the graph API enforces 25 database-backed requests per minute", async () => {
  const neo = fakeNeo4j();
  await withFetch(neo.stub, async () => {
    const ip = { "cf-connecting-ip": "203.0.113.9" };
    for (let index = 0; index < 25; index += 1) {
      const res = await call("/graph/health", { headers: ip });
      assert.equal(res.status, 200, `request ${index + 1} must be served: ${res.text.slice(0, 160)}`);
    }
    const denied = await call("/graph/health", { headers: ip });
    assert.equal(denied.status, 429);
    assert.equal(denied.body.error.code, "graph_rate_limited");
    assert.match(denied.body.error.message, /25 database-backed requests per minute/);
    assert.equal(denied.headers.get("x-ratelimit-limit"), "25");
    assert.equal(denied.headers.get("x-ratelimit-remaining"), "0");
    assert.ok(Number(denied.body.error.retry_after) >= 1);

    // A different caller is not punished for the first one's burst.
    const other = await call("/graph/health", { headers: { "cf-connecting-ip": "198.51.100.7" } });
    assert.equal(other.status, 200);
  });
});

await check("GRAPH_RPM cannot raise the ceiling above 25", async () => {
  const res = await call("/health", { env: { ...BASE_ENV, GRAPH_RPM: "999" } });
  assert.equal(res.status, 200);
  assert.equal(res.body.limits.graph_rpm, 25);
  assert.equal(res.body.limits.graph_client_rpm, 25);
  assert.equal(res.body.status, "/status");
});

await check("/graph/health caches its three-query probe", async () => {
  const neo = fakeNeo4j();
  await withFetch(neo.stub, async () => {
    const first = await call("/graph/health", { token: null, headers: { "cf-connecting-ip": "203.0.113.10" } });
    const second = await call("/graph/health", { token: null, headers: { "cf-connecting-ip": "203.0.113.10" } });
    assert.equal(first.status, 200);
    assert.equal(second.status, 200);
    assert.equal(first.headers.get("x-puppetnet-cache"), "MISS");
    assert.equal(second.headers.get("x-puppetnet-cache"), "HIT");
    assert.equal(neo.calls.length, 1, "a refresh loop must not re-run the probe");
  });
});

/* -------------------------------------------------------------------------- */
/*  2. SSRF protection                                                         */
/* -------------------------------------------------------------------------- */

await check("URLs with credentials, zone ids and dangerous ports are refused", async () => {
  const cases = [
    ["https://user:pass@example.com/", /may not embed credentials/],
    ["https://[fe80::1%25eth0]/", /Malformed URL/], // WHATWG refuses the zone form outright
    ["https://example.com:6379/", /Port 6379 is blocked/],
    ["https://example.com:22/", /Port 22 is blocked/],
  ];
  for (const [url, reason] of cases) {
    const res = await call("/fetch", { token: BASE_ENV.PROXY_AUTH_TOKEN, body: { url } });
    assert.equal(res.status, 400, `${url} must be refused`);
    assert.equal(res.body.error.code, "invalid_target");
    assert.match(res.body.error.message, reason, url);
  }
});

await check("a redirect to the metadata service is refused before the second hop", async () => {
  const calls = [];
  const stub = async (target) => {
    calls.push(String(target));
    return new Response(null, { status: 302, headers: { location: "https://169.254.169.254/latest/meta-data/" } });
  };
  await withFetch(stub, async () => {
    const res = await call("/fetch", { token: BASE_ENV.PROXY_AUTH_TOKEN, body: { url: "https://redirect.example/start" } });
    assert.equal(res.status, 502);
    assert.equal(res.body.ssrf_blocked, true);
    assert.match(res.body.error, /Private\/reserved IPv4 range blocked/);
    assert.equal(calls.length, 1, "the metadata host must never be dialled");
  });
});

await check("a cross-origin redirect drops caller credentials, a same-origin one keeps them", async () => {
  const seen = [];
  const stub = async (target, init = {}) => {
    const url = new URL(String(target));
    seen.push({ url: url.toString(), authorization: init.headers ? init.headers.get("authorization") : null, custom: init.headers ? init.headers.get("x-custom") : null });
    if (url.pathname === "/same") return new Response(null, { status: 302, headers: { location: "https://one.example/same-final" } });
    if (url.pathname === "/same-final") return new Response("ok", { status: 200 });
    if (url.pathname === "/cross") return new Response(null, { status: 302, headers: { location: "https://two.example/landing" } });
    if (url.pathname === "/landing") return new Response("ok", { status: 200 });
    throw new Error(`unexpected ${url}`);
  };
  await withFetch(stub, async () => {
    const same = await call("/fetch", {
      token: BASE_ENV.PROXY_AUTH_TOKEN,
      body: { url: "https://one.example/same", headers: { authorization: "Bearer caller-secret", "x-custom": "kept" } },
    });
    assert.equal(same.status, 200);
    assert.equal(seen[1].authorization, "Bearer caller-secret", "same-origin redirects keep the caller header");

    seen.length = 0;
    const cross = await call("/fetch", {
      token: BASE_ENV.PROXY_AUTH_TOKEN,
      body: { url: "https://one.example/cross", headers: { authorization: "Bearer caller-secret", "x-custom": "kept" } },
    });
    assert.equal(cross.status, 200);
    assert.equal(seen[1].url, "https://two.example/landing");
    assert.equal(seen[1].authorization, null, "a credential must not follow a redirect to another origin");
    assert.equal(seen[1].custom, "kept", "non-credential headers may follow");
  });
});

await check("an https → http redirect is refused", async () => {
  const stub = async () => new Response(null, { status: 302, headers: { location: "http://one.example/plain" } });
  await withFetch(stub, async () => {
    const res = await call("/fetch", { token: BASE_ENV.PROXY_AUTH_TOKEN, body: { url: "https://one.example/down" } });
    assert.equal(res.status, 502);
    assert.equal(res.body.ssrf_blocked, true);
    assert.match(res.body.error, /Plaintext HTTP/);
  });
});

await check("a redirect loop fails closed instead of returning the last hop", async () => {
  let hops = 0;
  const stub = async (target) => {
    hops += 1;
    const url = new URL(String(target));
    const next = Number(url.pathname.slice(1)) + 1;
    return new Response(null, { status: 302, headers: { location: `https://loop.example/${next}` } });
  };
  await withFetch(stub, async () => {
    const res = await call("/fetch", { token: BASE_ENV.PROXY_AUTH_TOKEN, body: { url: "https://loop.example/0" } });
    assert.equal(res.status, 502);
    assert.equal(res.body.ssrf_blocked, true);
    assert.match(res.body.error, /More than 5 redirects/);
    assert.equal(hops, 6, "six requests: the first plus five followed hops");
  });
});

await check("database and analytics calls refuse to follow a redirect", async () => {
  const seen = [];
  const stub = async (target, init = {}) => {
    const url = String(target);
    seen.push({ url, redirect: init.redirect });
    if (url.includes("api.cloudflare.com")) {
      return new Response(JSON.stringify({
        data: { viewer: { accounts: [{ workersInvocationsAdaptive: [{ sum: { requests: 4321, errors: 2 } }] }] } },
        errors: [],
      }), { status: 200, headers: { "content-type": "application/json" } });
    }
    const body = JSON.parse(String(init.body || "{}"));
    const results = (body.statements || []).map((item) => {
      const statement = String(item.statement || "");
      if (/count\(n\)/.test(statement)) return { columns: ["nodes"], data: [{ row: [900] }] };
      if (/count\(r\)/.test(statement)) return { columns: ["relationships"], data: [{ row: [1000] }] };
      return { columns: [], data: [] };
    });
    return new Response(JSON.stringify({ results, errors: [] }), { status: 200, headers: { "content-type": "application/json" } });
  };
  const env = {
    ...BASE_ENV,
    RATE_LIMIT_KV: makeKv(),
    CF_API_TOKEN: "cf-analytics-token",
    CF_ACCOUNT_ID: "0123456789abcdef0123456789abcdef",
  };
  await withFetch(stub, async () => {
    const result = await worker.scheduled({ cron: internals.CRON_HEALTH }, env, ctx());
    assert.equal(result.health.workers.requests_today, 4321);
    assert.equal(result.health.workers.source, "cloudflare_graphql");
  });
  const analytics = seen.find((entry) => entry.url.includes("api.cloudflare.com"));
  const database = seen.find((entry) => entry.url.includes("neo4j.io"));
  assert.equal(analytics.redirect, "error", "a Bearer token must not follow a redirect");
  assert.equal(database.redirect, "error", "Basic credentials must not follow a redirect");
});

await check("a caller cannot override DNS resolution", async () => {
  const seen = [];
  const stub = async (target, init = {}) => {
    seen.push(init.cf || {});
    return new Response("ok", { status: 200, headers: { "content-type": "text/plain" } });
  };
  await withFetch(stub, async () => {
    const res = await call("/fetch", {
      token: BASE_ENV.PROXY_AUTH_TOKEN,
      body: { url: "https://public.example/page", resolve_override: "169.254.169.254" },
    });
    assert.equal(res.status, 200);
    assert.equal(seen[0].resolveOverride, undefined, "resolveOverride must not reach the runtime");
  });
});

/* -------------------------------------------------------------------------- */
/*  3. Distributed locks in KV                                                 */
/* -------------------------------------------------------------------------- */

await check("a KV lock excludes a second holder, expires, and only its owner can release it", async () => {
  const kv = makeKv();
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  const first = await internals.acquireLock(env, "unit", 1, { job: "unit" });
  assert.equal(first.acquired, true);
  assert.equal(first.backend, "kv");
  assert.equal(kv.ops.puts, 1);

  const second = await internals.acquireLock(env, "unit", 1, { job: "unit" });
  assert.equal(second.acquired, false);
  assert.ok(second.remaining_ms > 0);
  assert.equal(second.owner, first.owner);

  assert.equal(await internals.releaseLock(env, "unit", "somebody-else"), false);
  assert.equal(await internals.releaseLock(env, "unit", first.owner), true);
  const third = await internals.acquireLock(env, "unit", 1, { job: "unit" });
  assert.equal(third.acquired, true);

  // A lease that has run out is not a lock any more, even if KV still holds it.
  await sleep(1100);
  const fourth = await internals.acquireLock(env, "unit", 1, { job: "unit" });
  assert.equal(fourth.acquired, true);
});

await check("without KV the lock degrades to isolate memory and says so", async () => {
  const first = await internals.acquireLock(BASE_ENV, "memory", 30);
  const second = await internals.acquireLock(BASE_ENV, "memory", 30);
  assert.equal(first.acquired, true);
  assert.equal(first.backend, "memory");
  assert.equal(second.acquired, false);
  assert.equal(second.backend, "memory");
  await internals.releaseLock(BASE_ENV, "memory", first.owner);
});

/* -------------------------------------------------------------------------- */
/*  4. Cron: keep-alive and snapshot                                           */
/* -------------------------------------------------------------------------- */

await check("the keep-alive cron writes one bounded AuraDB heartbeat", async () => {
  const neo = fakeNeo4j();
  const kv = makeKv();
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  await withFetch(neo.stub, async () => {
    const result = await worker.scheduled({ cron: internals.CRON_KEEPALIVE, scheduledTime: Date.now() }, env, ctx());
    assert.equal(result.keepalive.ok, true);
    assert.equal(result.health, undefined, "the keep-alive schedule must not run the heavy probe");

    const statement = neo.calls[0].statements[0];
    assert.match(statement.statement, /MERGE \(h:_PuppetNetHeartbeat/);
    assert.equal(statement.parameters.id, "puppetnet-edge-cron");
    assert.equal(statement.parameters.worker, "puppetnet-edge-relay");

    const stored = JSON.parse(kv.store.get("health:keepalive"));
    assert.equal(stored.ok, true);
    assert.equal(kv.store.has("lock:keepalive"), false, "the lease is released after the run");

    const ledger = JSON.parse(kv.store.get(`health:daily:${new Date().toISOString().slice(0, 10)}`));
    assert.equal(ledger.keepalive_runs, 1);
    assert.equal(ledger.keepalive_failures, 0);
  });
});

await check("a held keep-alive lease suppresses a duplicate run", async () => {
  const neo = fakeNeo4j();
  const kv = makeKv(new Map([["lock:keepalive", JSON.stringify({ owner: "other-colo", until: Date.now() + 60_000 })]]));
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  await withFetch(neo.stub, async () => {
    const result = await worker.scheduled({ cron: internals.CRON_KEEPALIVE }, env, ctx());
    assert.equal(result.keepalive.skipped, "locked");
    assert.equal(neo.calls.length, 0, "a locked run must not touch the database");
    const stored = JSON.parse(kv.store.get("health:keepalive"));
    assert.equal(stored.skipped, "locked");
    assert.equal(stored.holder, "other-colo");
  });
});

await check("the hourly snapshot stores node counts and the free-tier budget", async () => {
  const neo = fakeNeo4j({ nodes: 12_345, relationships: 67_890 });
  const kv = makeKv();
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  await withFetch(neo.stub, async () => {
    const result = await worker.scheduled({ cron: internals.CRON_HEALTH }, env, ctx());
    assert.ok(result.health.generated_at);
    assert.equal(result.health.aura.nodes, 12_345);
    assert.equal(result.health.aura.relationships, 67_890);
    assert.equal(result.health.aura.node_limit, 200_000);
    assert.equal(result.health.workers.requests_today, null, "no Cloudflare token: report n/a, not zero");
    assert.equal(result.health.workers.source, "not_configured");
    assert.equal(result.health.rate.graph_rpm, 25);
    assert.equal(result.health.ok, true);

    const stored = JSON.parse(kv.store.get("health:snapshot"));
    assert.equal(stored.aura.nodes, 12_345);
    assert.equal(stored.kv.bound, true);
    assert.ok(Array.isArray(stored.degraded));
  });
});

await check("a database failure is recorded as a degraded snapshot", async () => {
  const neo = fakeNeo4j({ fail: true });
  const kv = makeKv();
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  await withFetch(neo.stub, async () => {
    const result = await worker.scheduled({ cron: internals.CRON_HEALTH }, env, ctx());
    assert.equal(result.health.ok, false);
    assert.equal(result.health.aura.available, false);
    assert.equal(result.health.aura.error, "neo4j_error");
    assert.ok(kv.store.has("health:snapshot"), "a failed probe is still a snapshot");
  });
});

await check("cron expressions are valid five-field schedules and not the 36-hour trap", () => {
  for (const expression of [internals.CRON_KEEPALIVE, internals.CRON_HEALTH]) {
    assert.equal(expression.trim().split(/\s+/).length, 5);
  }
  assert.notEqual(internals.CRON_KEEPALIVE, "0 */36 * * *");
  assert.match(internals.CRON_KEEPALIVE, /^0 \*\/12 /);
  assert.match(internals.CRON_HEALTH, /^\d+ \*\/3 /);
});

/* -------------------------------------------------------------------------- */
/*  5. Health dashboard                                                        */
/* -------------------------------------------------------------------------- */

function storedSnapshot(overrides = {}) {
  return {
    worker: { name: "puppetnet-edge-relay", version: "1.7.0" },
    generated_at: new Date().toISOString(),
    last_run: { at: new Date().toISOString(), cron: internals.CRON_HEALTH, duration_ms: 42 },
    workers: { requests_today: 1234, limit: 100_000, percent: 1.2, source: "cloudflare_graphql", errors_today: 0 },
    aura: {
      available: true, nodes: 12_345, relationships: 67_890,
      node_limit: 200_000, relationship_limit: 400_000,
      node_percent: 6.2, relationship_percent: 17, latency_ms: 12,
      database: "neo4j", host: "abcd1234.databases.neo4j.io",
    },
    rate: { graph_rpm: 25, client_rpm: 25, used_this_minute: 3, remaining_this_minute: 22, host_buckets: 1, global_rpm: 900 },
    kv: { bound: true, reads_limit: 100_000, writes_limit: 1_000, lock_backend: "kv" },
    keepalive: { ok: true, at: new Date().toISOString(), latency_ms: 9 },
    ok: true,
    degraded: [],
    warnings: [],
    ...overrides,
  };
}

await check("/status renders a script-free German dashboard from the stored snapshot", async () => {
  const kv = makeKv(new Map([["health:snapshot", JSON.stringify(storedSnapshot())]]));
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  const res = await call("/status", { env, token: BASE_ENV.STATUS_TOKEN });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /text\/html/);
  assert.match(res.headers.get("content-security-policy"), /default-src 'none'/);
  assert.equal(res.headers.get("x-robots-tag"), "noindex, nofollow");
  assert.match(res.text, /<html lang="de">/);
  assert.match(res.text, /Free-Tier-Status/);
  assert.match(res.text, /AuraDB Knoten/);
  assert.match(res.text, /12\.345/, "German number formatting");
  assert.match(res.text, /Limit 25\/Client\/Minute/);
  assert.doesNotMatch(res.text, /<script/i, "the dashboard must not execute anything");
  assert.doesNotMatch(res.text, /https?:\/\/[^"']+\.(?:js|css)/i, "no external assets");
});

await check("the dashboard is token-protected, opt-in public, and escapes errors", async () => {
  const kv = makeKv(new Map([["health:snapshot", JSON.stringify(storedSnapshot({
    ok: false,
    aura: { available: false, configured: true, error: "<script>alert(1)</script>", node_limit: 200_000, relationship_limit: 400_000, host: "h" },
    degraded: ["AuraDB nicht erreichbar"],
  }))]]));
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };

  const denied = await call("/status", { env, token: null });
  assert.equal(denied.status, 401);
  assert.match(denied.headers.get("www-authenticate"), /puppetnet-status/);

  const publicEnv = { ...env, STATUS_PUBLIC: "true" };
  const open = await call("/status", { env: publicEnv, token: null });
  assert.equal(open.status, 200);
  assert.match(open.text, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(open.text, /<script>alert/);

  // A live probe is never public, even when the read-only view is.
  const refresh = await call("/status/refresh", { env: publicEnv, method: "POST", token: null });
  assert.equal(refresh.status, 401);
  const live = await call("/status?live=1", { env: publicEnv, token: null });
  assert.equal(live.status, 401, "?live=1 must not be reachable through the public view");

  const json = await call("/status.json", { env, token: BASE_ENV.GRAPH_API_TOKEN });
  assert.equal(json.status, 200);
  assert.equal(json.body.aura.available, false);
  assert.equal(json.body.cached, true);
});

await check("/status degrades honestly when no snapshot exists yet", async () => {
  const env = { ...BASE_ENV, STATUS_PUBLIC: "true" }; // no KV at all
  const res = await call("/status", { env, token: null });
  assert.equal(res.status, 200);
  assert.match(res.text, /Noch kein Cron-Lauf/);
  const json = await call("/status.json", { env, token: null });
  assert.equal(json.status, 200);
  assert.equal(json.body.aura.error, "not_probed");
  assert.equal(json.body.workers.source, "not_probed");
  assert.equal(json.body.cached, false);
});

await check("a live probe honours the status lease and answers from the snapshot", async () => {
  const neo = fakeNeo4j();
  const kv = makeKv(new Map([
    ["health:snapshot", JSON.stringify(storedSnapshot())],
    ["lock:status-live", JSON.stringify({ owner: "other-colo", until: Date.now() + 30_000 })],
  ]));
  const env = { ...BASE_ENV, RATE_LIMIT_KV: kv };
  await withFetch(neo.stub, async () => {
    const res = await call("/status.json?live=1", { env, token: BASE_ENV.STATUS_TOKEN });
    assert.equal(res.status, 200);
    assert.equal(res.body.cached, true);
    assert.equal(res.body.refresh.skipped, "locked");
    assert.equal(res.body.refresh.holder, "other-colo");
    assert.equal(neo.calls.length, 0, "a locked probe must not scan the graph");
  });
});

/* -------------------------------------------------------------------------- */
/*  6. Authentication throttling and response hardening                        */
/* -------------------------------------------------------------------------- */

await check("repeated bad tokens lock the address without locking anyone else", async () => {
  const env = { ...BASE_ENV, AUTH_FAILURE_LIMIT: "3" };
  const attacker = { "cf-connecting-ip": "192.0.2.44" };
  for (let index = 0; index < 3; index += 1) {
    const res = await call("/fetch", { env, token: "wrong-token", headers: attacker, body: { url: "https://public.example/" } });
    assert.equal(res.status, 401, `attempt ${index + 1}`);
  }
  const locked = await call("/fetch", { env, token: BASE_ENV.PROXY_AUTH_TOKEN, headers: attacker, body: { url: "https://public.example/" } });
  assert.equal(locked.status, 429);
  assert.equal(locked.body.error.code, "auth_locked");

  await withFetch(async () => new Response("ok", { status: 200, headers: { "content-type": "text/plain" } }), async () => {
    const other = await call("/fetch", { env, token: BASE_ENV.PROXY_AUTH_TOKEN, headers: { "cf-connecting-ip": "192.0.2.45" }, body: { url: "https://public.example/" } });
    assert.equal(other.status, 200, "the lock is per address, not global");
  });
  internals.resetRateLimits();
});

await check("every response carries the API hardening headers", async () => {
  const res = await call("/health", { token: null });
  assert.equal(res.headers.get("x-frame-options"), "DENY");
  assert.equal(res.headers.get("referrer-policy"), "no-referrer");
  assert.equal(res.headers.get("x-content-type-options"), "nosniff");
  assert.match(res.headers.get("strict-transport-security"), /max-age=31536000/);
  assert.equal(res.headers.get("content-security-policy"), null, "JSON needs no CSP, but no framing either");
});

console.log(`\nworker hardening: ${checks} checks passed`);
