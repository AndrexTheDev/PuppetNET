/**
 * worker_smoke.mjs — behavioural smoke test for the edge relay.
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
 *
 * Exits non-zero on the first failed assertion so CI can gate on it.
 */

import assert from "node:assert/strict";

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

async function relay(path, payload, token = SECRETS.PROXY_AUTH_TOKEN) {
  const request = new Request(`https://relay.example.invalid${path}`, {
    method: payload === undefined ? "GET" : "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: payload === undefined ? null : JSON.stringify(payload),
  });
  const response = await worker.fetch(request, makeEnv(), makeCtx());
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

console.log(`\nworker.js smoke test: ${checks} checks passed`);
