/**
 * worker_smoke.mjs — behavioural smoke test for the edge relay and graph API.
 *
 *   node tests/worker_smoke.mjs
 *
 * Runs the Worker's exported `fetch` handler in plain Node (no wrangler, no
 * network): global `fetch` is stubbed so we can see the exact request the Worker
 * sends upstream, and `env` is stubbed with the secrets the host profiles use.
 *
 * What it proves, in the terms the task set out:
 *   * Wikidata SPARQL is relayed as a form POST with `format=json` + `maxlag`,
 *     under a self-identifying User-Agent (their published policy), and the
 *     caller cannot raise the politeness ceiling from the client side.
 *   * OpenCorporates and ADS-B Exchange (RapidAPI) get their credentials
 *     injected at the edge — and those secrets never appear in the response the
 *     harvester receives, which is what keeps a token out of a GitHub Actions log.
 *   * Ordinary websites still get the rotating browser fingerprint.
 *   * The read-only /graph/* API — what web/ talks to — sends parameterised,
 *     mutation-free Cypher, clamps every caller-supplied limit, holds the Neo4j
 *     credentials itself and never returns them.
 *
 * The graph half runs against a fake Neo4j (below) that dispatches on the shape
 * of the statement the Worker emits, so the Cypher contract is asserted rather
 * than assumed.
 *
 * Exits non-zero on the first failed assertion so CI can gate on it.
 */

import assert from "node:assert/strict";

import {
  FX,
  FX_BY_KEY,
  GRAPH_INDEX,
  P_KASTELION,
  O_MERIDIAN,
  O_SARNEN,
  F_TALLOW,
  L_LIMASSOL,
  A_TAIL,
  P_UNKNOWN,
  neo4jCalls,
  neo4jBehaviour,
  runStatement,
  fakeNeo4jTxResponse,
} from "./helpers/fake_neo4j.mjs";

const worker = (await import("../worker.js")).default;

const SECRETS = {
  PROXY_AUTH_TOKEN: "relay-token-abc123",
  OPENCORPORATES_API_TOKEN: "oc-token-SECRET-xyz",
  ADSBEXCHANGE_API_KEY: "adsbx-key-SECRET-xyz",
  COMPANIES_HOUSE_API_KEY: "ch-key-SECRET-xyz",
  WIKIDATA_USER_AGENT: "PuppetNET-smoke/1.0 (contact: ops@example.invalid)",
};

/** Every upstream request the Worker made, in order. */
const upstreamCalls = [];

async function stubFetch(target, init = {}) {
  const url = typeof target === "string" ? target : target.url;
  upstreamCalls.push({
    url,
    method: String(init.method || "GET").toUpperCase(),
    headers: init.headers,
    body: typeof init.body === "string" ? init.body : null,
  });

  if (url.includes("robots.txt")) {
    return new Response("User-agent: *\nAllow: /\n", { status: 200 });
  }

  // Neo4j's transactional HTTP endpoint — the only upstream the graph API uses.
  const parsed = new URL(url);
  if (/\/db\/[^/]+\/tx\/commit$/.test(parsed.pathname)) {
    let payload = {};
    try {
      payload = JSON.parse(typeof init.body === "string" && init.body ? init.body : "{}");
    } catch (_) {
      payload = {};
    }
    neo4jCalls.push({
      url: parsed.toString(),
      method: String(init.method || "GET").toUpperCase(),
      headers: init.headers,
      statements: payload.statements || [],
      aborted: Boolean(init.signal && init.signal.aborted),
    });
    const envelope = (body, status) => new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { "content-type": "application/json" },
    });
    // Failure injection lives in the shared helper, so the Worker and the console
    // are tested against exactly the same misbehaving upstream.
    const out = fakeNeo4jTxResponse(payload.statements || []);
    return out.text !== undefined
      ? new Response(out.text, { status: out.status, headers: { "content-type": out.contentType } })
      : envelope(out.json, out.status);
  }
  // Echo the path only: a realistic upstream would never repeat our credentials,
  // and this keeps the "no secret in the response" assertions about the Worker.
  const echoed = new URL(url);
  echoed.search = "";
  return new Response(JSON.stringify({ ok: true, echo: echoed.toString() }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

// Install the stub before any request: worker.js calls the global fetch, so
// this is what turns the smoke test offline.
globalThis.fetch = stubFetch;

function makeEnv() {
  // No KV / Queue bindings: the Worker must still work stateless.
  return { ...SECRETS };
}

function makeCtx() {
  return { waitUntil: () => {}, passThroughOnException: () => {} };
}

async function relay(path, payload, token = SECRETS.PROXY_AUTH_TOKEN, env = makeEnv()) {
  const request = new Request(`https://relay.example.invalid${path}`, {
    method: payload === undefined ? "GET" : "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: payload === undefined ? null : JSON.stringify(payload),
  });
  const response = await worker.fetch(request, env, makeCtx());
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch (_) {
    body = null;
  }
  return { status: response.status, body, text };
}

function lastUpstream() {
  return upstreamCalls[upstreamCalls.length - 1];
}

function headerOf(call, name) {
  return call.headers ? call.headers.get(name) : null;
}

/**
 * Credential values that must never appear in what the harvester receives.
 *
 * The relay's own token is excluded (the caller already knows it) and so is
 * WIKIDATA_USER_AGENT: a descriptive User-Agent is *deliberately* public — that
 * is what Wikidata's policy asks for — and it is reported back as
 * `fingerprint_used` so the harvester can log which identity it presented.
 */
const CREDENTIAL_SECRETS = Object.freeze([
  "OPENCORPORATES_API_TOKEN",
  "ADSBEXCHANGE_API_KEY",
  "COMPANIES_HOUSE_API_KEY",
]);

function assertNoSecrets(text, label) {
  for (const key of CREDENTIAL_SECRETS) {
    assert.ok(!text.includes(SECRETS[key]), `${label}: ${key} leaked into the response body`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let checks = 0;
function check(name, fn) {
  return fn()
    .then(() => {
      checks += 1;
      console.log(`  ok  ${name}`);
    })
    .catch((error) => {
      console.error(`FAIL  ${name}`);
      console.error(error && error.message ? error.message : error);
      process.exit(1);
    });
}

console.log("worker.js host profiles — behavioural smoke test");

// --- 1. Wikidata SPARQL: form POST, API identity, politeness ceiling --------
await check("wikidata SPARQL is relayed as a polite form POST", async () => {
  upstreamCalls.length = 0;
  const sparql = "SELECT ?company WHERE { ?company wdt:P31 wd:Q1664720 }";
  const res = await relay("/fetch", {
    url: "https://query.wikidata.org/sparql",
    method: "POST",
    content_type: "application/x-www-form-urlencoded",
    body_text: `query=${encodeURIComponent(sparql)}&format=json&maxlag=5`,
    // A caller trying to hammer WDQS from the client side…
    rate_limit: { rate_per_sec: 50, burst: 25 },
    source_id: "wikidata.foundations",
  });

  assert.equal(res.status, 200, `expected 200, got ${res.status}: ${res.text.slice(0, 300)}`);
  assert.equal(res.body.ok, true);
  assert.equal(res.body.host_profile, "wikidata-sparql");

  const call = lastUpstream();
  assert.equal(call.method, "POST", "SPARQL must go upstream as POST");
  const contentType = headerOf(call, "content-type");
  assert.equal(contentType, "application/x-www-form-urlencoded", "SPARQL body must be a form, not JSON");
  const params = new URLSearchParams(call.body);
  assert.equal(params.get("query"), sparql, "the query must survive the relay verbatim");
  assert.equal(params.get("format"), "json");
  assert.equal(params.get("maxlag"), "5");

  // …cannot: the profile caps it at Wikidata's published politeness level.
  assert.equal(res.body.rate_limit.policy.ratePerSec, 0.25, "profile must cap the rate ceiling");
  assert.equal(res.body.rate_limit.policy.burst, 1, "profile must cap the burst");

  // API mode: honest UA, no rotating browser fingerprint.
  assert.equal(headerOf(call, "user-agent"), SECRETS.WIKIDATA_USER_AGENT);
  assert.equal(headerOf(call, "sec-ch-ua"), null, "an API host must not get browser fingerprint headers");
  assert.equal(headerOf(call, "sec-fetch-dest"), null);
  assert.equal(res.body.fingerprint_used, SECRETS.WIKIDATA_USER_AGENT);
  assertNoSecrets(res.text, "wikidata");
});

// --- 2. Burst is enforced: an immediate follow-up call is not fired ---------
//
// Check 1 already consumed the single token in query.wikidata.org's bucket
// (burst=1 at 0.25 req/s), so the very next SPARQL call must be refused rather
// than sent. This is the assertion that matters for the harvest: it is what
// stops a daily run from earning a project-wide 429 ban.
await check("an immediate follow-up SPARQL call is throttled, not fired", async () => {
  upstreamCalls.length = 0;
  const second = await relay("/fetch", {
    url: "https://query.wikidata.org/sparql",
    method: "POST",
    body_text: "query=SELECT%20%2A%20WHERE%20%7B%7D&format=json",
    cache_buster: false,
  });
  const allowed = Boolean(second.body && second.body.rate_limit && second.body.rate_limit.allowed === true);
  assert.ok(!allowed, `burst=1 must throttle the immediate second call (got ${second.status})`);
  assert.ok(second.status === 429 || second.status === 202, `expected 429/202, got ${second.status}`);
  const sparqlCalls = upstreamCalls.filter((call) => !call.url.includes("robots.txt"));
  assert.equal(sparqlCalls.length, 0, "a throttled request must never reach the upstream host");
});

// The cap in check 1 is real, so the bucket needs its 4-second window back
// before the next SPARQL assertion can run. That wait *is* the point: a relay
// that did not throttle here is a relay that gets the project IP-blocked.
await sleep(4500);

// --- 3. A JSON SPARQL payload is converted to a form ------------------------
await check("a JSON SPARQL payload is converted to a WDQS form body", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", {
    url: "https://query.wikidata.org/sparql",
    method: "POST",
    json: { query: "SELECT ?p WHERE { ?p wdt:P3320 wd:Q157031 }" },
  });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  const call = lastUpstream();
  const params = new URLSearchParams(call.body);
  assert.equal(params.get("format"), "json", "format=json must be added");
  assert.equal(params.get("maxlag"), "5", "maxlag must be added so WDQS can shed load");
  assert.equal(headerOf(call, "content-type"), "application/x-www-form-urlencoded");
});

// --- 4. OpenCorporates: token injected at the edge, never echoed ------------
await check("OpenCorporates gets its api_token at the edge and never echoes it", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", {
    url: "https://api.opencorporates.com/v0.4/companies/search?q=usmanov&jurisdiction_code=im",
  });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.host_profile, "opencorporates");
  assert.equal(res.body.credential, "credential:opencorporates-applied");

  const call = lastUpstream();
  const upstreamUrl = new URL(call.url);
  assert.equal(upstreamUrl.searchParams.get("api_token"), SECRETS.OPENCORPORATES_API_TOKEN);
  assert.equal(upstreamUrl.searchParams.get("q"), "usmanov", "the caller's query params must be preserved");
  assert.equal(headerOf(call, "sec-ch-ua"), null, "OpenCorporates is an API: no browser fingerprint");

  assertNoSecrets(res.text, "opencorporates");
});

// --- 5. ADS-B Exchange via RapidAPI: key headers injected -------------------
await check("ADS-B Exchange on RapidAPI gets its key headers", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", {
    url: "https://adsbexchange-com1.p.rapidapi.com/v2/icao/A4B2C6/",
  });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.host_profile, "rapidapi-adsbexchange");
  assert.equal(res.body.credential, "credential:rapidapi-applied");

  const call = lastUpstream();
  assert.equal(headerOf(call, "x-rapidapi-key"), SECRETS.ADSBEXCHANGE_API_KEY);
  assert.equal(headerOf(call, "x-rapidapi-host"), "adsbexchange-com1.p.rapidapi.com");
  assertNoSecrets(res.text, "adsbexchange");
});

// --- 6. The free community mirror still works without a key -----------------
await check("adsbdb.com is profiled but needs no credential", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", { url: "https://adsbdb.com/api/v2/hex/484556" });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.host_profile, "adsbdb");
  assert.equal(res.body.credential, null);
  assert.equal(headerOf(lastUpstream(), "user-agent"), res.body.fingerprint_used);
});

// --- 7. An ordinary website keeps the rotating browser fingerprint ----------
await check("a non-profiled website still gets browser fingerprint headers", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", { url: "https://www.example-newsroom.invalid/article/1" });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.host_profile, null, "no profile should match");

  const call = lastUpstream();
  assert.ok(headerOf(call, "sec-ch-ua"), "websites get Client Hints");
  assert.ok(headerOf(call, "sec-fetch-dest"), "websites get Sec-Fetch-* hints");
});

// --- 8. /health reports profiles without leaking secret values --------------
await check("/health lists the host profiles and whether credentials exist", async () => {
  const res = await relay("/health");
  assert.equal(res.status, 200, res.text.slice(0, 300));
  const profiles = res.body && res.body.host_profiles;
  assert.ok(Array.isArray(profiles), "host_profiles must be an array");

  const labels = profiles.map((profile) => profile.label);
  for (const expected of [
    "wikidata-sparql",
    "opencorporates",
    "adsbdb",
    "rapidapi-adsbexchange",
    "faa-registry",
    "companies-house",
    "icij-offshore-leaks",
  ]) {
    assert.ok(labels.includes(expected), `/health is missing profile ${expected}`);
  }

  const wikidata = profiles.find((profile) => profile.label === "wikidata-sparql");
  assert.equal(wikidata.api_mode, true);
  assert.equal(wikidata.rate_per_sec, 0.25);

  const openCorporates = profiles.find((profile) => profile.label === "opencorporates");
  assert.equal(openCorporates.credential, true, "the OC token is configured in this env");

  const adsbdb = profiles.find((profile) => profile.label === "adsbdb");
  assert.equal(adsbdb.credential, null, "a profile with no credential reports null");

  assertNoSecrets(res.text, "/health");
});

// --- 9. Auth still gates the relay -----------------------------------------
await check("an unauthenticated caller is refused before any profile runs", async () => {
  upstreamCalls.length = 0;
  const res = await relay("/fetch", { url: "https://api.opencorporates.com/v0.4/companies/gb/01234567" }, "wrong-token");
  assert.equal(res.status, 401);
  assert.equal(upstreamCalls.length, 0, "no upstream request may be made for an unauthenticated call");
});

/* ========================================================================== */
/*  Graph read API (/graph/*) — the console's server side                      */
/* ==========================================================================
 *
 *  What these checks prove, in the terms the console depends on:
 *
 *   * The Worker turns an AuraDB bolt URI into the https transactional endpoint
 *     and attaches Basic auth itself — the browser never sees a credential, and
 *     no credential comes back in a response, a log line or a cache write.
 *   * Every endpoint is read-only, and every value a caller controls travels as a
 *     query parameter. The one thing that must be interpolated — an ORDER BY
 *     property, and a traversal bound Cypher cannot parameterise — is checked
 *     against an allowlist by the *fake server*, so an injection reaches the
 *     assertions as a failure instead of as a silent no-op.
 *   * Limits are clamped server-side: a browser asking for 100k nodes or a 99-hop
 *     walk gets the ceiling, not an error and not a stalled database.
 *   * Auth has three honest modes (graph token, relay token, opt-in public read)
 *     and fails closed when none is configured.
 * ========================================================================== */

const NEO4J = Object.freeze({
  uri: "neo4j+s://abcd1234.databases.neo4j.io:7687",
  user: "neo4j",
  password: "aura-SECRET-do-not-leak",
  database: "neo4j",
});
const GRAPH_TOKEN = "console-token-SECRET-9";

function lastNeo4j() {
  return neo4jCalls[neo4jCalls.length - 1];
}

function graphHeader(call, name) {
  if (!call || !call.headers) return null;
  if (typeof call.headers.get === "function") return call.headers.get(name);
  const key = Object.keys(call.headers).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
  return key ? call.headers[key] : null;
}

function makeGraphEnv(overrides = {}) {
  return {
    ...SECRETS,
    NEO4J_URI: NEO4J.uri,
    NEO4J_USERNAME: NEO4J.user,
    NEO4J_PASSWORD: NEO4J.password,
    NEO4J_DATABASE: NEO4J.database,
    GRAPH_API_TOKEN: GRAPH_TOKEN,
    // Off by default so each check sees a real query; the cache check opts in.
    GRAPH_CACHE_TTL_SECONDS: "0",
    // Generous by default so rate limiting is only asserted where it is the point.
    GRAPH_RATE_PER_SEC: "200",
    GRAPH_BURST: "200",
    ...overrides,
  };
}

async function graph(path, options = {}) {
  const env = options.env || makeGraphEnv(options.envOverrides || {});
  const token = options.token === undefined ? GRAPH_TOKEN : options.token;
  const request = new Request(`https://relay.example.invalid${path}`, {
    method: options.method || "GET",
    headers: {
      ...(token === null ? {} : { authorization: `Bearer ${token}` }),
      ...(options.headers || {}),
    },
  });
  const response = await worker.fetch(request, env, makeCtx());
  const text = await response.text();
  let body = null;
  try {
    body = JSON.parse(text);
  } catch (_) {
    body = null;
  }
  return { status: response.status, body, text, headers: response.headers, env };
}

/** Values a caller controls that must never be interpolated into a statement. */
const POISON = Object.freeze([
  "' OR 1=1 //",
  "}) DETACH DELETE n //",
  "anomaly_score DESC, e.name",
  "99; DROP INDEX x",
  "<script>alert(1)</script>",
]);

function assertReadOnlyAndParameterised(label) {
  neo4jCalls.forEach((call) => {
    call.statements.forEach((item) => {
      const statement = String(item.statement || "");
      assert.ok(statement.length > 0, `${label}: empty statement sent`);
      for (const verb of ["CREATE ", "MERGE ", "DELETE", "DETACH", "REMOVE ", "DROP ", "LOAD CSV", "CALL {", "FOREACH"]) {
        assert.ok(!statement.toUpperCase().includes(verb.toUpperCase()),
          `${label}: a read-only API must never emit '${verb.trim()}' — got: ${statement.slice(0, 200)}`);
      }
      for (const poison of POISON) {
        assert.ok(!statement.includes(poison), `${label}: caller text was interpolated into Cypher: ${poison}`);
      }
      // Portability is part of correctness: ORDER BY, SKIP/OFFSET and LIMIT only
      // became *standalone* clauses in Neo4j 5.24, so `WITH … LIMIT $n ORDER BY …`
      // is a syntax error on every earlier 5.x — and an Aura Free instance is
      // provisioned with whichever 5.x Cloudflare's neighbour decides. This suite
      // stubs Neo4j and never parses Cypher, so without this assertion a statement
      // that fails against a real database passes every test we have.
      const standalone = /LIMIT\s+(?:\$\w+|\d+)\s+(?:ORDER\s+BY|SKIP|OFFSET)\b/i.exec(statement);
      assert.ok(!standalone,
        `${label}: statement needs Neo4j 5.24+ (standalone "${standalone ? standalone[0].replace(/\s+/g, " ") : ""}") — `
        + `keep ORDER BY/SKIP/LIMIT as subclauses of a WITH: ${statement.slice(0, 200)}`);
      // Parameters are the only channel for caller data: every `$name` the
      // statement references must be bound in the parameters map. (A statement
      // that references none — the count queries behind /graph/health — needs no
      // map at all, which is why this is not a blanket "must be an object".)
      const referenced = new Set();
      for (const match of statement.matchAll(/\$([A-Za-z_]\w*)/g)) referenced.add(match[1]);
      if (referenced.size) {
        assert.equal(typeof item.parameters, "object",
          `${label}: statement binds $${[...referenced].join(", $")} but sent no parameters map`);
        for (const name of referenced) {
          assert.ok(Object.prototype.hasOwnProperty.call(item.parameters, name),
            `${label}: statement references $${name} but it is not bound`);
        }
      }
    });
  });
}

function assertNoGraphSecrets(text, label) {
  assert.ok(!text.includes(NEO4J.password), `${label}: NEO4J_PASSWORD leaked into the response`);
  assert.ok(!text.includes(GRAPH_TOKEN), `${label}: GRAPH_API_TOKEN leaked into the response`);
  assert.ok(!text.includes("7687"), `${label}: the bolt port leaked — the URI should be reduced to a host`);
  const basic = `Basic ${Buffer.from(`${NEO4J.user}:${NEO4J.password}`).toString("base64")}`;
  assert.ok(!text.includes(basic), `${label}: the Basic auth header leaked into the response`);
  assert.ok(!text.includes(`${NEO4J.user}:${NEO4J.password}`), `${label}: credentials leaked into the response`);
}

// --- G1. /graph/health: public, informative, credential-free ----------------
await check("/graph/health rewrites the bolt URI, authenticates, and leaks nothing", async () => {
  neo4jCalls.length = 0;
  const res = await graph("/graph/health", { token: null });

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.ok, true, "a configured database must report ok");
  assert.equal(res.body.graph.configured, true);
  // The AuraDB bolt URI becomes the https transactional endpoint at the same host.
  assert.equal(res.body.graph.host, "abcd1234.databases.neo4j.io", "only the host may be reported");
  assert.equal(res.body.graph.database, "neo4j");
  assert.equal(res.body.graph.public_read, false);
  assert.equal(res.body.graph.counts.nodes, FX.nodes.length);
  assert.equal(res.body.graph.counts.edges, FX.edges.length);
  assert.equal(res.body.graph.counts.fulltext, true, "the full-text index must be detected");
  assert.ok(res.body.endpoints.includes("/graph/neighbors"));

  const call = lastNeo4j();
  assert.equal(call.url, `https://abcd1234.databases.neo4j.io/db/${NEO4J.database}/tx/commit`,
    "bolt+7687 must become https+443 at the transactional endpoint");
  assert.equal(call.method, "POST");
  const auth = graphHeader(call, "authorization");
  assert.ok(auth && auth.startsWith("Basic "), "the Worker must attach Basic auth itself");
  assert.equal(Buffer.from(auth.slice(6), "base64").toString("utf8"), `${NEO4J.user}:${NEO4J.password}`);
  assert.equal(graphHeader(call, "content-type"), "application/json");
  assert.ok(call.statements.some((item) => item.statement.includes("db.indexes()")));

  assertNoGraphSecrets(res.text, "/graph/health");
  assertReadOnlyAndParameterised("/graph/health");
});

// --- G2. /graph/overview: shaping, ceilings and the ORDER BY allowlist ------
await check("/graph/overview returns the console's node/edge shapes under a ceiling", async () => {
  neo4jCalls.length = 0;
  const res = await graph("/graph/overview?limit=4&metric=betweenness");

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.ok, true);
  assert.equal(res.body.subject, "overview");
  assert.equal(res.body.metric, "betweenness");
  assert.equal(res.body.nodes.length, 4, "the limit must be honoured");
  assert.equal(res.body.truncated, true, "6 entities with a limit of 4 is a truncated view");
  assert.equal(res.body.caps.nodes, FX.nodes.length);
  assert.equal(res.body.caps.edges, FX.edges.length);
  assert.equal(res.body.metrics_at, "2026-09-27T04:30:00Z");

  const scores = res.body.nodes.map((node) => node.betweenness);
  assert.deepEqual(scores, scores.slice().sort((a, b) => b - a), "nodes must arrive ranked by the requested metric");

  // The node shape is what app.js normalises; a rename here breaks the console.
  const top = res.body.nodes[0];
  for (const field of ["key", "name", "entity_type", "labels", "confidence", "mention_count", "degree",
    "betweenness", "anomaly_score", "cluster_id", "first_seen", "last_seen", "metrics_at", "aliases",
    "source_ids", "doc_ids", "props"]) {
    assert.ok(field in top, `overview node is missing '${field}'`);
  }
  assert.equal(top.key, L_LIMASSOL, "Limassol is the highest-betweenness fixture node");
  assert.deepEqual(top.labels, ["Entity", "Location"]);
  assert.equal(top.props.jurisdiction, "CY", "identifier properties must be carried in props");

  // Edges are the ones induced by the returned page — no dangling endpoints.
  const keys = new Set(res.body.nodes.map((node) => node.key));
  assert.ok(res.body.edges.length > 0, "the induced edge set must not be empty");
  res.body.edges.forEach((edge) => {
    assert.ok(keys.has(edge.source) && keys.has(edge.target),
      `edge ${edge.id} references a node the payload does not contain`);
    for (const field of ["id", "source", "target", "type", "weight", "confidence", "method", "evidence"]) {
      assert.ok(field in edge, `overview edge is missing '${field}'`);
    }
  });

  assert.equal(res.headers.get("X-PuppetNET-Cache"), "MISS");
  assertNoGraphSecrets(res.text, "/graph/overview");
  assertReadOnlyAndParameterised("/graph/overview");
});

await check("/graph/overview refuses to interpolate a caller-supplied metric", async () => {
  neo4jCalls.length = 0;
  const res = await graph(`/graph/overview?limit=3&metric=${encodeURIComponent("anomaly_score DESC, e.name")}`);

  assert.equal(res.status, 200, res.text.slice(0, 400));
  // Clamped to the default: the injected fragment is gone, and the fake server
  // would have raised a syntax error had it reached the statement.
  assert.equal(res.body.metric, "anomaly_score");
  const statements = lastNeo4j().statements.map((item) => item.statement).join("\n");
  assert.ok(statements.includes("ORDER BY coalesce(e.anomaly_score, 0) DESC"), "the allowlisted metric must be used");
  assert.ok(!statements.includes("anomaly_score DESC, e.name"), "the injected ORDER BY text must not survive");

  // A metric that exists but is not sortable is clamped too.
  neo4jCalls.length = 0;
  const second = await graph("/graph/overview?limit=2&metric=password");
  assert.equal(second.body.metric, "anomaly_score");
  assertReadOnlyAndParameterised("/graph/overview metric clamp");
});

await check("/graph/overview clamps an absurd limit to the configured ceiling", async () => {
  neo4jCalls.length = 0;
  const res = await graph("/graph/overview?limit=999999");
  assert.equal(res.status, 200, res.text.slice(0, 400));
  const limit = lastNeo4j().statements[0].parameters.limit;
  assert.ok(limit <= 1200, `limit must be clamped to the ceiling, got ${limit}`);
  assert.equal(res.body.nodes.length, FX.nodes.length, "the fixture only has 6 entities");

  neo4jCalls.length = 0;
  const negative = await graph("/graph/overview?limit=-5");
  assert.ok(negative.body.nodes.length >= 1, "a negative limit must clamp up to 1, not to nothing");
});

// --- G3. /graph/search: index first, scan as fallback -----------------------
await check("/graph/search uses the full-text index when it is online", async () => {
  neo4jCalls.length = 0;
  const res = await graph("/graph/search?q=kastelion&limit=5");

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.method, "fulltext");
  assert.ok(res.body.nodes.length >= 1, "the fixture contains 'Kastelion'");
  assert.equal(res.body.nodes[0].key, P_KASTELION);
  assert.ok(res.body.nodes[0].match_score > 0, "a full-text hit must carry a score");

  const statement = lastNeo4j().statements[0].statement;
  assert.ok(statement.includes("db.index.fulltext.queryNodes($index, $ftq)"), "the index name and query must be parameters");
  assert.equal(lastNeo4j().statements[0].parameters.index, GRAPH_INDEX);
  assert.equal(lastNeo4j().statements[0].parameters.ftq, "kastelion*", "the query is reduced to Lucene-safe prefix tokens");
  assertNoGraphSecrets(res.text, "/graph/search");
  assertReadOnlyAndParameterised("/graph/search");
});

await check("/graph/search falls back to a deterministic scan when the index is gone", async () => {
  neo4jBehaviour.noFulltextIndex = true;
  try {
    neo4jCalls.length = 0;
    const res = await graph("/graph/search?q=meridian&limit=5");
    assert.equal(res.status, 200, res.text.slice(0, 400));
    assert.equal(res.body.method, "scan", "a missing index must degrade, not fail");
    assert.equal(res.body.nodes[0].key, O_MERIDIAN);
    assert.equal(res.body.nodes[0].match_score, 70, "'meridian' is a name prefix, so STARTS WITH scores 70");

    // A mid-name hit is weaker than a prefix: the ranking an analyst expects.
    neo4jCalls.length = 0;
    const contains = await graph("/graph/search?q=holdings");
    assert.equal(contains.body.nodes[0].key, O_MERIDIAN);
    assert.equal(contains.body.nodes[0].match_score, 50, "a CONTAINS hit scores 50 in the scan");

    // Exact identifiers outrank a name substring — that ordering is what makes
    // a pasted IMO or tail number land on the right entity.
    neo4jCalls.length = 0;
    const byTail = await graph("/graph/search?q=9H-KAST");
    assert.equal(byTail.body.nodes[0].key, A_TAIL);
    assert.equal(byTail.body.nodes[0].match_score, 95);

    // An empty query is not an error and costs the database nothing.
    neo4jCalls.length = 0;
    const empty = await graph("/graph/search?q=");
    assert.equal(empty.body.method, "empty");
    assert.equal(empty.body.nodes.length, 0);
    assert.equal(neo4jCalls.length, 0, "an empty query must not reach Neo4j");
  } finally {
    neo4jBehaviour.noFulltextIndex = false;
  }
});

// --- G4. /graph/node: inspector payload -------------------------------------
await check("/graph/node returns the node, its ties, its neighbours and its citations", async () => {
  neo4jCalls.length = 0;
  const res = await graph(`/graph/node?key=${encodeURIComponent(O_MERIDIAN)}`);

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.node.key, O_MERIDIAN);
  assert.equal(res.body.node.name, "Meridian Holdings Ltd");
  assert.equal(res.body.node.props.company_number, "HE312345");
  assert.equal(res.body.node.degree, 3, "Meridian has three incident ties");

  assert.equal(res.body.edges.length, 3);
  assert.ok(res.body.nodes.some((node) => node.key === P_KASTELION), "neighbours must be named, not just keyed");
  assert.equal(res.body.citations.length, FX.docs.length);
  assert.equal(res.body.citations[0].source_name, "OpenCorporates", "a citation must name its source");
  assert.ok(res.body.citations[0].url.startsWith("https://"), "a citation must be followable");

  assert.equal(lastNeo4j().statements.length, 4, "one round trip for the whole inspector payload");
  assertNoGraphSecrets(res.text, "/graph/node");
  assertReadOnlyAndParameterised("/graph/node");
});

await check("/graph/node 404s on an unknown key and 400s without one", async () => {
  const missing = await graph(`/graph/node?key=${encodeURIComponent(P_UNKNOWN)}`);
  assert.equal(missing.status, 404);
  assert.equal(missing.body.ok, false);
  assert.equal(missing.body.error.code, "not_found");

  const noKey = await graph("/graph/node");
  assert.equal(noKey.status, 400);
  assert.equal(noKey.body.error.code, "bad_request");
});

// --- G5. /graph/neighbors: the N-degree engine ------------------------------
await check("/graph/neighbors clamps depth and generates a level-by-level walk", async () => {
  neo4jCalls.length = 0;
  const res = await graph(`/graph/neighbors?key=${encodeURIComponent(P_KASTELION)}&depth=99&limit=50`);

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.depth, 4, "depth is clamped to the 4-hop ceiling");
  assert.ok(res.body.nodes.some((node) => node.key === A_TAIL), "4 hops from Kastelion reaches the aircraft");

  // The generated statement expands level by level rather than enumerating paths,
  // which is the difference between O(sum of degrees) and O(paths).
  const statement = lastNeo4j().statements[0].statement;
  assert.ok(statement.includes("WITH [root] AS seen0"), "the walk must start from the root list");
  assert.ok(statement.includes("f3 IS NOT NULL"), "depth 4 must generate four levels");
  assert.ok(!statement.includes("f4 IS NOT NULL"), "and no more than four");
  assert.ok(!statement.includes("[*1.."), "a variable-length path pattern would enumerate paths exponentially");
  assert.ok(statement.includes("CASE WHEN size(frontier0) = 0 THEN [null]"),
    "an empty frontier must not collapse the query");
  assertReadOnlyAndParameterised("/graph/neighbors");
});

await check("/graph/neighbors applies weight and type filters to every hop", async () => {
  neo4jCalls.length = 0;
  const strong = await graph(`/graph/neighbors?key=${encodeURIComponent(P_KASTELION)}&depth=2&min_weight=0.8`);
  assert.equal(strong.status, 200, strong.text.slice(0, 400));
  // Kastelion →(0.92) Meridian at hop 1, then Meridian →(0.83) Limassol at hop 2:
  // both ties clear the filter, so Limassol is legitimately in a 2-hop scope.
  // Sarnen is not — every route to it (0.58 direct, 0.71 via Meridian) is weaker.
  assert.deepEqual(strong.body.nodes.map((node) => node.key).sort(),
    [L_LIMASSOL, O_MERIDIAN, P_KASTELION].sort(),
    "the walk may only cross ties at or above min_weight");
  assert.ok(!strong.body.nodes.some((node) => node.key === O_SARNEN), "Sarnen is only reachable over weak ties");
  assert.ok(!strong.body.nodes.some((node) => node.key === A_TAIL), "and the aircraft is further out still");

  // The filter governs *traversal*, not *display*: every tie between in-scope
  // nodes comes back, including the 0.6 LOCATED_IN that was not walked. Hiding
  // weak ties is the console's own filter panel, so returning them keeps the two
  // independent instead of double-filtering.
  assert.deepEqual(strong.body.edges.map((edge) => edge.id).sort(), ["9001", "9004", "9005"],
    "the induced edge set covers every tie between in-scope nodes");
  assert.deepEqual(strong.body.filters, { min_weight: 0.8, types: [] });

  neo4jCalls.length = 0;
  const typed = await graph(`/graph/neighbors?key=${encodeURIComponent(P_KASTELION)}&depth=2&types=OWNS,CONTROLS`);
  assert.deepEqual(typed.body.filters.types, ["OWNS", "CONTROLS"]);
  assert.ok(typed.body.nodes.some((node) => node.key === O_SARNEN), "the CONTROLS tie reaches Sarnen");
  assert.ok(!typed.body.nodes.some((node) => node.key === L_LIMASSOL), "LOCATED_IN was filtered out");
  assert.equal(lastNeo4j().statements[0].parameters.types.join(","), "OWNS,CONTROLS");

  // An unknown type is dropped rather than passed through to Cypher.
  neo4jCalls.length = 0;
  const bogus = await graph(`/graph/neighbors?key=${encodeURIComponent(P_KASTELION)}&types=OWNS,HACKS`);
  assert.deepEqual(bogus.body.filters.types, ["OWNS"], "types outside the relation vocabulary must be ignored");
});

await check("/graph/neighbors returns a filtered-out node itself rather than nothing", async () => {
  // Tallow Creek has one tie, weaker than the filter. A naive `UNWIND []` walk
  // would collapse the whole query and 404 on a node that plainly exists.
  const res = await graph(`/graph/neighbors?key=${encodeURIComponent(F_TALLOW)}&depth=2&min_weight=0.95`);
  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.nodes.length, 1, "the root is always in its own neighbourhood");
  assert.equal(res.body.nodes[0].key, F_TALLOW);
  assert.equal(res.body.edges.length, 0, "and no tie survives the filter");
  assert.equal(res.body.nodes[0].degree, 1, "degree counts every tie, filtered or not");
});

// --- G6. /graph/path: shortest chain and handshake --------------------------
await check("/graph/path finds the shortest chain between two entities", async () => {
  neo4jCalls.length = 0;
  const res = await graph(`/graph/path?from=${encodeURIComponent(P_KASTELION)}&to=${encodeURIComponent(A_TAIL)}`);

  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.found, true);
  // The fixture gives Kastelion a direct CONTROLS tie to the shell, so the
  // shortest handshake is person → shell → aircraft (2 hops), not the 3-hop
  // route through the holding company. A path engine that returned the longer
  // chain would be quietly wrong in the one place an analyst looks hardest.
  assert.equal(res.body.hops, 2);
  assert.deepEqual(res.body.nodeKeys, [P_KASTELION, O_SARNEN, A_TAIL],
    "the handshake chain is person → shell → aircraft");
  assert.equal(res.body.edges.length, 2);
  assert.deepEqual(res.body.edges.map((edge) => edge.type), ["CONTROLS", "REGISTERED_TO"]);
  assert.equal(res.body.nodes[1].name, "Sarnen Offshore Services SA", "chain nodes arrive named");
  assert.ok(res.body.meanWeight > 0 && res.body.minWeight > 0, "the chain must carry its tie strengths");
  assert.ok(Array.isArray(res.body.alternatives), "alternatives are always an array");
  assert.equal(res.body.bounded, false, "hop-count shortest paths are exact, not bounded");
  assert.ok(lastNeo4j().statements[0].statement.includes("shortestPath"));
  assertNoGraphSecrets(res.text, "/graph/path");
  assertReadOnlyAndParameterised("/graph/path");
});

await check("/graph/path reports an unreachable pair instead of failing", async () => {
  const res = await graph(`/graph/path?from=${encodeURIComponent(P_KASTELION)}&to=${encodeURIComponent(P_UNKNOWN)}`);
  assert.equal(res.status, 200, res.text.slice(0, 400));
  assert.equal(res.body.found, false);
  assert.equal(res.body.reason, "unreachable");
  assert.equal(res.body.nodes.length, 0);

  const same = await graph(`/graph/path?from=${encodeURIComponent(P_KASTELION)}&to=${encodeURIComponent(P_KASTELION)}`);
  assert.equal(same.status, 400, "a path from a node to itself is a caller error");

  const missing = await graph(`/graph/path?from=${encodeURIComponent(P_KASTELION)}`);
  assert.equal(missing.status, 400);
});

await check("/graph/path bounds the weighted cost search and clamps its inputs", async () => {
  neo4jCalls.length = 0;
  const weighted = await graph(
    `/graph/path?from=${encodeURIComponent(P_KASTELION)}&to=${encodeURIComponent(A_TAIL)}&cost=inverse-weight&max_hops=12`
  );
  assert.equal(weighted.status, 200, weighted.text.slice(0, 400));
  assert.equal(weighted.body.cost_function, "inverse-weight");
  assert.equal(weighted.body.bounded, true, "weighted enumeration is an approximation and must say so");
  const enumeration = lastNeo4j().statements[1];
  assert.ok(enumeration, "a weighted request needs a second statement");
  assert.ok(enumeration.statement.includes("1.05 - coalesce(r.weight, 0)"), "cost is inverse tie weight");
  assert.ok(/\[\*1\.\.4\]/.test(enumeration.statement), "weighted enumeration is capped at 4 hops");
  assert.ok(enumeration.statement.includes("LIMIT $enumCap"), "and capped again by how many paths are produced");
  assert.equal(enumeration.parameters.enumCap, 20000);

  // Hostile inputs are clamped to the defaults, not echoed back.
  neo4jCalls.length = 0;
  const hostile = await graph(
    `/graph/path?from=${encodeURIComponent(P_KASTELION)}&to=${encodeURIComponent(A_TAIL)}` +
    `&cost=${encodeURIComponent("<script>alert(1)</script>")}&direction=${encodeURIComponent("sideways")}&max_hops=99`
  );
  assert.equal(hostile.body.cost_function, "hops");
  assert.equal(hostile.body.direction, "undirected");
  assert.equal(hostile.body.max_hops, 12, "hops are clamped to the ceiling");
  assert.ok(/\[\*1\.\.12\]/.test(lastNeo4j().statements[0].statement));
  assertReadOnlyAndParameterised("/graph/path clamping");
});

// --- G7. /graph/table: paged metadata --------------------------------------
await check("/graph/table pages nodes, edges and sources with server-side totals", async () => {
  const nodes = await graph("/graph/table?subject=nodes&limit=2&skip=1&sort=name&order=asc");
  assert.equal(nodes.status, 200, nodes.text.slice(0, 400));
  assert.equal(nodes.body.subject, "nodes");
  assert.equal(nodes.body.total, FX.nodes.length);
  assert.equal(nodes.body.rows.length, 2);
  assert.equal(nodes.body.limit, 2);
  assert.equal(nodes.body.skip, 1);

  const edges = await graph("/graph/table?subject=edges&min_weight=0.5&sort=weight&order=desc&limit=10");
  assert.equal(edges.body.subject, "edges");
  assert.ok(edges.body.rows.every((edge) => edge.weight >= 0.5), "min_weight must be applied server-side");
  assert.ok(edges.body.total >= edges.body.rows.length);
  const weights = edges.body.rows.map((edge) => edge.weight);
  assert.deepEqual(weights, weights.slice().sort((a, b) => b - a), "edges arrive in the requested order");

  const sources = await graph("/graph/table?subject=sources&limit=10");
  assert.equal(sources.body.subject, "sources");
  assert.equal(sources.body.rows.length, FX.docs.length);
  assert.ok(sources.body.rows[0].doc_id, "a source row must be identifiable");
  assert.ok(sources.body.rows[0].title, "and titled");

  const capped = await graph("/graph/table?subject=nodes&limit=999999");
  assert.ok(capped.body.limit <= 2000, `table rows must be capped, got ${capped.body.limit}`);

  const unknownSubject = await graph("/graph/table?subject=secrets");
  assert.equal(unknownSubject.body.subject, "nodes", "an unknown subject falls back to nodes");
  assertReadOnlyAndParameterised("/graph/table");
  assertNoGraphSecrets(sources.text, "/graph/table");
});

// --- G8. Auth, method and configuration ------------------------------------
await check("the graph API gates itself: token, relay fallback, opt-in public read", async () => {
  neo4jCalls.length = 0;
  const anonymous = await graph("/graph/overview?limit=2", { token: null });
  assert.equal(anonymous.status, 401, "a private graph must require a token");
  assert.equal(neo4jCalls.length, 0, "an unauthenticated call must never reach the database");

  const wrong = await graph("/graph/overview?limit=2", { token: "not-the-token" });
  assert.equal(wrong.status, 401);
  assert.equal(neo4jCalls.length, 0);

  const withGraphToken = await graph("/graph/overview?limit=2", { token: GRAPH_TOKEN });
  assert.equal(withGraphToken.status, 200);
  assert.equal(withGraphToken.body.auth_mode, "graph-token");

  const withRelayToken = await graph("/graph/overview?limit=2", { token: SECRETS.PROXY_AUTH_TOKEN });
  assert.equal(withRelayToken.status, 200, "a single-secret deployment must still work");
  assert.equal(withRelayToken.body.auth_mode, "relay-token");

  const publicRead = await graph("/graph/overview?limit=2", { envOverrides: { GRAPH_PUBLIC_READ: "true" }, token: null });
  assert.equal(publicRead.status, 200);
  assert.equal(publicRead.body.auth_mode, "public");

  const headerToken = await (async () => {
    const request = new Request("https://relay.example.invalid/graph/overview?limit=2", {
      headers: { "x-graph-token": GRAPH_TOKEN },
    });
    const response = await worker.fetch(request, makeGraphEnv(), makeCtx());
    return response.status;
  })();
  assert.equal(headerToken, 200, "x-graph-token is accepted for a browser that cannot set Authorization");
});

await check("the graph API is GET-only, rejects unknown routes, and can be switched off", async () => {
  const posted = await graph("/graph/overview?limit=2", { method: "POST" });
  assert.equal(posted.status, 405, "a read-only API must not accept writes");
  assert.equal(posted.body.error.code, "method_not_allowed");

  const unknown = await graph("/graph/drop-everything");
  assert.equal(unknown.status, 404);
  assert.ok(unknown.body.error.message.includes("/graph/health"), "the error should point at the route list");

  const disabled = await graph("/graph/overview?limit=2", { envOverrides: { GRAPH_API_ENABLED: "false" } });
  assert.equal(disabled.status, 403);
  assert.equal(disabled.body.error.code, "graph_disabled");
});

await check("an unconfigured database fails closed without touching the network", async () => {
  neo4jCalls.length = 0;
  const res = await graph("/graph/overview?limit=2", {
    envOverrides: { NEO4J_URI: "", NEO4J_PASSWORD: "" },
  });
  assert.equal(res.status, 503);
  assert.equal(res.body.error.code, "neo4j_unconfigured");
  assert.equal(neo4jCalls.length, 0);

  // A plaintext URI to a remote host is refused: it would put credentials on the wire.
  neo4jCalls.length = 0;
  const plaintext = await graph("/graph/overview?limit=2", {
    envOverrides: { NEO4J_URI: "http://neo4j.example.invalid:7474" },
  });
  assert.equal(plaintext.status, 503);
  assert.equal(neo4jCalls.length, 0, "plaintext to a remote database must not be dialled");

  // /graph/health stays public and says so.
  const health = await graph("/graph/health", { envOverrides: { NEO4J_URI: "" }, token: null });
  assert.equal(health.status, 200);
  assert.equal(health.body.ok, false);
  assert.equal(health.body.graph.configured, false);
});

// --- G9. Upstream failure mapping ------------------------------------------
await check("Neo4j failures map to honest HTTP statuses without leaking detail", async () => {
  neo4jBehaviour.unauthorized = true;
  try {
    const res = await graph("/graph/overview?limit=2");
    assert.equal(res.status, 503, "bad database credentials are a service problem, not a client one");
    assert.equal(res.body.error.code, "neo4j_error");
    assert.equal(res.body.error.neo4j_code, "Neo.ClientError.Security.Unauthorized");
    assertNoGraphSecrets(res.text, "neo4j unauthorized");
  } finally {
    neo4jBehaviour.unauthorized = false;
  }

  neo4jBehaviour.error = { message: "Invalid input 'DELETE': expected a read-only statement" };
  try {
    const res = await graph("/graph/overview?limit=2");
    assert.equal(res.status, 502);
    assert.equal(res.body.ok, false);
    assert.ok(res.body.error.message.includes("read-only"));
  } finally {
    neo4jBehaviour.error = null;
  }

  neo4jBehaviour.rawText = "<html>502 Bad Gateway</html>";
  try {
    const res = await graph("/graph/overview?limit=2");
    assert.equal(res.status, 502);
    assert.equal(res.body.error.code, "bad_gateway");
    assert.ok(res.body.error.message.includes("non-JSON"), "an HTML error page must be reported as such");
  } finally {
    neo4jBehaviour.rawText = null;
  }
});

// --- G10. Rate limiting and caching ----------------------------------------
await check("graph queries share a token bucket, so a burst cannot stall the database", async () => {
  const env = makeGraphEnv({ GRAPH_RATE_PER_SEC: "0.01", GRAPH_BURST: "2" });
  neo4jCalls.length = 0;

  const first = await graph("/graph/overview?limit=1", { env });
  const second = await graph("/graph/overview?limit=2", { env });
  const third = await graph("/graph/overview?limit=3", { env });

  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(third.status, 429, "burst=2 must refuse the third call");
  assert.equal(third.body.error.code, "graph_rate_limited");
  assert.ok(third.body.error.retry_after >= 1, "a 429 must tell the console when to retry");
  assert.equal(neo4jCalls.length, 2, "a refused call must not reach the database");
});

// The graph bucket lives in the isolate, keyed `graph:neo4j`, and is shared by
// every env the checks build — the previous check drained it at 0.01 req/s. At
// the default 200 req/s a 150 ms pause refills ~30 tokens, which is deterministic
// enough to assert on without waiting the 100 s that drain would need.
await sleep(150);

await check("a cached graph response costs one query and stores no credential", async () => {
  const store = new Map();
  const kv = {
    async get(key, type) {
      const raw = store.get(key);
      if (!raw) return null;
      return type === "json" ? JSON.parse(raw) : raw;
    },
    async put(key, value) {
      store.set(key, value);
    },
  };
  const env = makeGraphEnv({ RESULT_KV: kv, GRAPH_CACHE_TTL_SECONDS: "60" });
  neo4jCalls.length = 0;

  const first = await graph("/graph/overview?limit=3&metric=anomaly_score", { env });
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("X-PuppetNET-Cache"), "MISS");
  assert.equal(neo4jCalls.length, 1);

  const second = await graph("/graph/overview?limit=3&metric=anomaly_score", { env });
  assert.equal(second.status, 200);
  assert.equal(second.body.cached, true);
  assert.equal(second.headers.get("X-PuppetNET-Cache"), "HIT");
  assert.equal(neo4jCalls.length, 1, "a cache hit must not query the database again");
  assert.deepEqual(second.body.nodes.map((node) => node.key), first.body.nodes.map((node) => node.key));

  // A different query is a different cache entry.
  const third = await graph("/graph/overview?limit=4&metric=anomaly_score", { env });
  assert.equal(third.body.cached, undefined);
  assert.equal(neo4jCalls.length, 2);

  for (const value of store.values()) {
    assert.ok(!value.includes(NEO4J.password), "a cached payload must never contain the database password");
    assert.ok(!value.includes(GRAPH_TOKEN), "a cached payload must never contain a bearer token");
  }
});

// --- G11. Response ceiling --------------------------------------------------
await check("an oversized graph response is refused rather than shipped", async () => {
  const res = await graph("/graph/overview?limit=6", { envOverrides: { GRAPH_MAX_RESPONSE_BYTES: "512" } });
  assert.equal(res.status, 502);
  assert.equal(res.body.error.code, "response_too_large");
  assert.ok(res.body.error.bytes > 512, "the error should report how big the payload was");
});

// --- G12. The console's contract with the relay's /health -------------------
await check("/health advertises the graph API without exposing it", async () => {
  const res = await relay("/health");
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.bindings.graph_api, false, "the relay env has no NEO4J_* secrets, so the graph API is off");
  assert.equal(res.body.bindings.graph_token, false);
  assert.equal(res.body.graph_api, "/graph/health");
  assert.equal(res.body.version, "1.6.2");
  assertNoSecrets(res.text, "/health");

  const configured = await graph("/health", { token: null });
  assert.equal(configured.body.bindings.graph_api, true, "with NEO4J_* set, /health reports the graph API available");
  assert.equal(configured.body.bindings.graph_token, true);
  assertNoGraphSecrets(configured.text, "/health with graph secrets");
});

// --- G13. CORS: one origin per answer, and only an allowed one --------------
await check("CORS answers per request, and only for origins the operator allowed", async () => {
  const list = "https://console.example, https://mirror.example";

  // The wildcard deployment, which is what the shipped console runs against.
  const star = await graph("/graph/health", { envOverrides: { ALLOWED_ORIGINS: "*" } });
  assert.equal(star.headers.get("access-control-allow-origin"), "*");
  assert.equal(star.headers.get("vary"), "Origin", "an answer that varies must say so to caches");
  assert.equal(star.headers.get("x-content-type-options"), "nosniff",
    "routes echo paths and messages, so nothing may be MIME-sniffed");

  // Two allowed origins: the header has to echo the caller's own, because
  // Access-Control-Allow-Origin takes exactly one origin. The joined list this
  // used to send is rejected by every browser, so a deployment that allowed two
  // origins served neither.
  for (const origin of ["https://console.example", "https://mirror.example"]) {
    const res = await graph("/graph/overview?limit=5", {
      envOverrides: { ALLOWED_ORIGINS: list }, headers: { origin },
    });
    assert.equal(res.status, 200, res.text.slice(0, 200));
    assert.equal(res.headers.get("access-control-allow-origin"), origin, `${origin} must be echoed back`);
    assert.ok(!String(res.headers.get("access-control-allow-origin")).includes(","),
      "Access-Control-Allow-Origin may never contain a list");
  }

  // An origin nobody allowed gets no header at all — the browser blocks the
  // response, which is the refusal.
  const stranger = await graph("/graph/health", {
    envOverrides: { ALLOWED_ORIGINS: list }, headers: { origin: "https://evil.example" },
  });
  assert.equal(stranger.headers.get("access-control-allow-origin"), null,
    "an unlisted origin must not be granted access");
  assert.equal(stranger.headers.get("vary"), "Origin");

  // Preflight is answered without a token, under the same origin rules.
  const preflight = await graph("/graph/overview", {
    method: "OPTIONS", token: null,
    envOverrides: { ALLOWED_ORIGINS: list },
    headers: { origin: "https://mirror.example", "access-control-request-method": "GET" },
  });
  assert.equal(preflight.status, 204, "a preflight answers 204 and needs no bearer token");
  assert.equal(preflight.headers.get("access-control-allow-origin"), "https://mirror.example");
  assert.ok(String(preflight.headers.get("access-control-allow-methods")).includes("GET"));
  assert.ok(String(preflight.headers.get("access-control-allow-headers")).includes("Authorization"),
    "the console sends a bearer token, so the preflight must allow that header");

  // A single allowed origin still answers callers that send no Origin at all
  // (curl, an uptime probe, the queue consumer).
  const single = await graph("/graph/health", { envOverrides: { ALLOWED_ORIGINS: "https://console.example" } });
  assert.equal(single.headers.get("access-control-allow-origin"), "https://console.example");

  // Errors carry the same headers. Without them a browser reports a CORS failure
  // instead of the real 401, and the analyst debugs the wrong thing.
  const denied = await graph("/graph/overview", {
    token: "wrong-token", envOverrides: { ALLOWED_ORIGINS: list },
    headers: { origin: "https://console.example" },
  });
  assert.equal(denied.status, 401);
  assert.equal(denied.headers.get("access-control-allow-origin"), "https://console.example",
    "an error response needs the same CORS headers as a success");

  assertReadOnlyAndParameterised("CORS");
});

// --- G14. The SSRF guard: loopback in every notation ------------------------
await check("the SSRF guard refuses loopback in every notation it can be written", async () => {
  // Every case is https on purpose. Under the default ENFORCE_HTTPS a plaintext
  // URL is refused for being plaintext, which would make this suite pass whether
  // or not the address guard works at all — the mutation that deleted the
  // trailing-dot normalisation proved it, walking through an http:// list.
  const refused = [
    ["https://localhost/", "the plain loopback name", /Private\/reserved hostname/],
    ["https://localhost./", "trailing dot — the DNS root changes nothing about where it points", /Private\/reserved hostname/],
    ["https://something.localhost/", "a name under localhost", /Private\/reserved hostname/],
    ["https://127.0.0.1/", "dotted quad", /IPv4 range/],
    ["https://2130706433/", "integer IPv4 (WHATWG normalises it to 127.0.0.1)", /IPv4 range/],
    ["https://0x7f.1/", "hex IPv4", /IPv4 range/],
    ["https://0177.0.0.1/", "octal IPv4", /IPv4 range/],
    ["https://①②⑦.0.0.1/", "unicode digits", /IPv4 range/],
    ["https://0.0.0.0/", "the unspecified address", /IPv4 range/],
    ["https://10.0.0.1/", "RFC1918", /IPv4 range/],
    ["https://172.20.5.5/", "RFC1918, upper half", /IPv4 range/],
    ["https://192.168.1.1/", "RFC1918, the usual router", /IPv4 range/],
    ["https://169.254.169.254/latest/meta-data/", "the cloud metadata service", /IPv4 range/],
    ["https://100.64.0.1/", "carrier-grade NAT, which cloud internals use", /IPv4 range/],
    ["https://198.18.0.1/", "the benchmarking range", /IPv4 range/],
    ["https://224.0.0.1/", "multicast", /IPv4 range/],
    ["https://[::1]/", "IPv6 loopback", /IPv6 range/],
    ["https://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6", /IPv6 range/],
    ["https://[::ffff:10.0.0.1]/", "private IPv4 through the mapped form", /IPv6 range/],
    ["https://[fd00::1]/", "IPv6 unique local — the half a startsWith('fc') test misses", /IPv6 range/],
    ["https://[feb0::1]/", "IPv6 link local above fe80", /IPv6 range/],
    ["https://[ff02::1]/", "IPv6 multicast", /IPv6 range/],
    ["https://127.0.0.1.nip.io/", "a wildcard DNS service that encodes the address in the name", /nip\.io/],
    ["https://localhost.nip.io/", "the same service spelled with a name", /nip\.io/],
    ["https://10-0-0-1.sslip.io/", "…and its dash-flavoured sibling", /sslip\.io/],
    ["https://anything.lvh.me/", "a service that answers 127.0.0.1 for any subdomain", /lvh\.me/],
  ];
  for (const [url, why, reason] of refused) {
    const res = await relay("/fetch", { url });
    assert.equal(res.status, 400, `${url} (${why}) must be refused, got ${res.status}: ${res.text.slice(0, 160)}`);
    const body = res.body || {};
    assert.equal((body.error || {}).code, "invalid_target", `${url} (${why})`);
    assert.match(String((body.error || {}).message), reason,
      `${url} (${why}) must be refused for the right reason`);
  }

  // Plaintext is its own rule, and it stays on.
  const plaintext = await relay("/fetch", { url: "http://127.0.0.1/" });
  assert.match(String(plaintext.body.error.message), /Plaintext HTTP/,
    "ENFORCE_HTTPS refuses http:// before the address guard is reached");

  // A hostname that merely *looks* like an IPv6 prefix is not one: an earlier
  // `startsWith("fc")` test refused every name beginning with those two letters.
  const lookalike = await relay("/fetch", { url: "https://fcbank.example/page" });
  assert.equal(lookalike.status, 200,
    `a name beginning with fc is not a unique-local address — got ${lookalike.status}: ${lookalike.text.slice(0, 160)}`);
  const feHost = await relay("/fetch", { url: "https://fe80-reports.example/page" });
  assert.equal(feHost.status, 200, "nor is one beginning with fe80");

  // Operator policy works on the same normalised host, so a trailing dot cannot
  // smuggle a blocked name past it.
  const suffixEnv = { ...makeEnv(), BLOCKED_HOST_SUFFIXES: "example.org" };
  const post = (url) => worker.fetch(new Request("https://relay.example.invalid/fetch", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${SECRETS.PROXY_AUTH_TOKEN}` },
    body: JSON.stringify({ url }),
  }), suffixEnv, makeCtx());
  assert.equal((await (await post("https://www.example.org/page")).json()).error.code, "invalid_target",
    "the suffix list blocks the name");
  assert.equal((await (await post("https://www.example.org./page")).json()).error.code, "invalid_target",
    "and a trailing dot does not smuggle it through");

  // Private networks can be reached on purpose — that is what the escape hatch is
  // for, and it must not be silently ignored.
  const dev = await worker.fetch(new Request("https://relay.example.invalid/fetch", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${SECRETS.PROXY_AUTH_TOKEN}` },
    body: JSON.stringify({ url: "https://127.0.0.1:8080/status" }),
  }), { ...makeEnv(), ALLOW_PRIVATE_NETWORKS: "true" }, makeCtx());
  assert.equal(dev.status, 200, "ALLOW_PRIVATE_NETWORKS=true is an explicit opt-in for local work");
});

// --- G15. robots.txt: the rules decide, not the longest line ----------------
async function sha256Hex(input) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Ask the relay about one path, with the host's robots.txt served from a stubbed
 * KV cache so the test stays offline. `respect_robots` is what the harvester sets.
 */
async function robotsVerdict(host, robotsText, path) {
  const key = `robots:${await sha256Hex(`https://${host}/robots.txt`)}`;
  const env = {
    ...makeEnv(),
    // Generous, so this check measures the robots verdict and not the host bucket:
    // the relay refusing a burst with 429 is correct, and one case per host would
    // otherwise be decided by the limiter instead of by robots.txt.
    HOST_RATE_PER_SEC: "50",
    HOST_BURST: "50",
    RATE_LIMIT_KV: {
      get: async (requested) => (requested === key ? robotsText : null),
      put: async () => {},
    },
  };
  return relay("/fetch", { url: `https://${host}${path}`, respect_robots: true }, SECRETS.PROXY_AUTH_TOKEN, env);
}

await check("the robots evaluator honours the rules, not the longest line of the file", async () => {
  // This is the case that used to fail: `Disallow: /private` matched `/public`,
  // because the regex built for a pattern without a trailing `$` ended in an
  // alternative that matches any non-empty path. With longest-match precedence,
  // the longest rule in the file then decided every URL on the host.
  const rules = "User-agent: *\nDisallow: /private\nAllow: /private/public\n";

  const disallowed = await robotsVerdict("robots.example", rules, "/private/x");
  assert.equal(disallowed.status, 403, `a disallowed path must not be fetched: ${disallowed.text.slice(0, 160)}`);
  assert.equal((disallowed.body.error || {}).code, "robots_disallowed");
  assert.equal(disallowed.body.error.robots.allowed, false, "the verdict travels with the refusal");

  const allowed = await robotsVerdict("robots.example", rules, "/public/page");
  assert.equal(allowed.status, 200,
    "a path no rule mentions must be fetched — the old matcher refused every path as soon as any pattern existed");

  const longer = await robotsVerdict("robots.example", rules, "/private/public/x");
  assert.equal(longer.status, 200, "the longer Allow wins over the shorter Disallow, as the spec says");

  const sibling = await robotsVerdict("robots.example", rules, "/privateer/x");
  assert.equal(sibling.status, 403, "matching is a prefix match, so /private also covers /privateer");

  const anchored = await robotsVerdict("robots.example", "User-agent: *\nDisallow: /*.json$\n", "/data.json");
  assert.equal(anchored.status, 403, "`*` spans directories and `$` anchors the end");
  const withQuery = await robotsVerdict("robots.example", "User-agent: *\nDisallow: /*.json$\n", "/data.json?v=2");
  assert.equal(withQuery.status, 200, "and the anchor is the end of the path, which a query is not");
  const suffix = await robotsVerdict("robots.example", "User-agent: *\nDisallow: /*.json$\n", "/data.jsonp");
  assert.equal(suffix.status, 200, "an anchored pattern must not match by prefix");

  const everything = await robotsVerdict("robots.example", "User-agent: *\nDisallow: /\n", "/anything");
  assert.equal(everything.status, 403, "Disallow: / means exactly that");

  const otherCrawler = await robotsVerdict("robots.example",
    "User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "/page");
  assert.equal(otherCrawler.status, 200, "a group written for another crawler does not apply to ours");

  // The switch is respected in both directions: without respect_robots the Worker
  // must not spend a request on robots.txt at all.
  const before = upstreamCalls.length;
  const off = await relay("/fetch", { url: "https://robots.example/page" }, SECRETS.PROXY_AUTH_TOKEN,
    { ...makeEnv(), HOST_RATE_PER_SEC: "50", HOST_BURST: "50" });
  assert.equal(off.status, 200);
  const robotsFetches = upstreamCalls.slice(before).filter((call) => call.url.includes("robots.txt"));
  assert.equal(robotsFetches.length, 0, "with respect_robots off there is no robots request");
});

// --- G16. Workers KV Free allows 1k writes/day: only hot hosts may spend them ---
await check("KV writes go to hot hosts only, so an hourly news run cannot drain the free quota", async () => {
  // The relay is expected to be *polite* rather than to coordinate: a host that
  // receives one request in a sync window has no shared budget to defend, and a
  // KV read or write for it is pure quota spend. Before this rule, every served
  // request wrote its bucket (`acquireHostToken`'s fire-and-forget put) and the
  // robots cache wrote once per host per TTL — with ~400 article hosts per hourly
  // run that spent the day's 1k writes in one run.
  const ops = { reads: 0, writes: 0, keys: [] };
  const store = new Map();
  const env = {
    ...makeEnv(),
    HOST_RATE_PER_SEC: "50",
    HOST_BURST: "50",
    KV_SYNC_INTERVAL_MS: "20000",
    RATE_LIMIT_KV: {
      async get(key) {
        ops.reads += 1;
        ops.keys.push(`get:${key}`);
        return store.has(key) ? store.get(key) : null;
      },
      async put(key, value) {
        ops.writes += 1;
        ops.keys.push(`put:${key}`);
        store.set(key, value);
      },
    },
  };

  // One request to a cold article host: nothing about it is worth a KV op.
  const cold = await relay("/fetch", { url: "https://cold.example/story-1", respect_robots: true }, SECRETS.PROXY_AUTH_TOKEN, env);
  assert.equal(cold.status, 200, `the fetch itself must still work: ${cold.text.slice(0, 160)}`);
  assert.equal(ops.writes, 0, `a cold host must not write KV: ${ops.keys.join(",")}`);
  assert.equal(
    ops.keys.filter((entry) => entry.startsWith("get:rl:")).length,
    0,
    `a cold host must not read a rate-limit bucket: ${ops.keys.join(",")}`
  );
  // A robots.txt read is allowed and wanted: it is 100× cheaper than a write and a
  // hit saves the origin a request.
  assert.equal(ops.reads, 1, `one robots lookup, nothing else: ${ops.keys.join(",")}`);

  // A second request to the same host inside the same window makes it hot: now
  // the fleet-wide view matters and one read + one write are justified.
  const hot = await relay("/fetch", { url: "https://cold.example/story-2", respect_robots: true }, SECRETS.PROXY_AUTH_TOKEN, env);
  assert.equal(hot.status, 200);
  assert.ok(ops.writes >= 1, "a hot host publishes its bucket");
  assert.ok(ops.reads >= 1, "and inherits the fleet's view before spending");
  const bucketWrites = ops.keys.filter((entry) => entry.startsWith("put:rl:cold.example")).length;
  assert.equal(bucketWrites, 1, "at most one bucket write per host per sync window");

  // The robots.txt cache follows the same rule: a host fetched once does not get
  // a cache entry, a host fetched twice does.
  assert.ok(
    ops.keys.some((entry) => entry.startsWith("put:robots:")),
    "the second request is what makes the robots cache worth spending a write on"
  );

  // …and a hot host is throttled rather than published per request: the sync is
  // bounded by the window, which is what keeps a run's write count proportional to
  // its *duration* and not to its document count.
  const before = ops.writes;
  for (let index = 0; index < 6; index += 1) {
    const burst = await relay("/fetch", { url: `https://cold.example/story-${index + 3}`, respect_robots: true }, SECRETS.PROXY_AUTH_TOKEN, env);
    assert.equal(burst.status, 200, "the burst is served, not refused");
  }
  assert.ok(
    ops.writes - before <= 1,
    `six requests inside one window must cost at most one write, cost ${ops.writes - before}`
  );
});

console.log(`\nworker.js smoke test: ${checks} checks passed`);
