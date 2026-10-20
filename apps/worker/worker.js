/**
 * ============================================================================
 *  PuppetNET — Edge Fetch Relay (Cloudflare Worker)
 * ============================================================================
 *
 *  Serverless egress layer for the PuppetNET OSINT harvester.
 *
 *  Responsibilities
 *  ----------------
 *   1. Proxy outbound scraping requests from the Python ingestion worker so the
 *      origin sees Cloudflare's anycast edge (natural per-colo IP diversity)
 *      instead of a single GitHub Actions runner IP.
 *   2. Rotate a realistic browser fingerprint (User-Agent, Accept-*, sec-ch-ua,
 *      Referer, cache-buster) per attempt so repeated polling of a hostile
 *      origin does not present a static, blockable signature.
 *   3. Enforce politeness with a per-host token bucket (in-isolate memory +
 *      KV synchronisation) plus a global requests-per-minute safety valve.
 *   4. When a host is over budget, defer the request into a Cloudflare Queue
 *      with a delay ("rate-limiting queue") and hand the caller a task id it
 *      can poll, instead of hard-failing the daily run.
 *   5. Optionally honour robots.txt, cache responses in KV, and retry
 *      transient failures with jittered exponential backoff.
 *
 *  Routes
 *  ------
 *   GET    /health            liveness + build metadata            (public)
 *   GET    /status            free-tier health dashboard (HTML)    (auth, opt-in public)
 *   GET    /status.json       same data as JSON                    (auth, opt-in public)
 *   POST   /status/refresh    run a live AuraDB probe now           (auth)
 *   GET    /stats             rate-limit / queue counters          (auth)
 *   POST   /fetch             proxy a request                      (auth)
 *   GET    /fetch?url=...     same, convenience form                (auth)
 *   GET    /tasks/{id}        poll a deferred (queued) request      (auth)
 *   DELETE /tasks/{id}        drop a queued task result             (auth)
 *   DELETE /cache?url=...     purge a cached response               (auth)
 *   OPTIONS *                 CORS preflight                       (public)
 *
 *  Auth: `Authorization: Bearer <PROXY_AUTH_TOKEN>` (multiple comma-separated
 *  tokens are accepted in PROXY_AUTH_TOKENS to allow zero-downtime rotation).
 *  Comparison is constant time. Requests without a valid token are rejected
 *  before any egress is performed.
 *
 *  Bindings (wrangler.toml)
 *   - RATE_LIMIT_KV : KV namespace  (token buckets, robots.txt cache, locks)
 *   - RESULT_KV     : KV namespace  (response cache, queued task results)
 *   - FETCH_QUEUE   : Queue producer (deferred requests)
 *   - Secrets       : PROXY_AUTH_TOKEN / PROXY_AUTH_TOKENS,
 *                     NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD,
 *                     CF_API_TOKEN / CF_ACCOUNT_ID (optional analytics),
 *                     TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional alerts)
 *   - Cron Triggers : keep-alive (every 12 h) and health snapshot (every 3 h)
 *
 *  All bindings are optional: the Worker degrades to a stateless relay with
 *  per-isolate rate limiting when KV/Queues are not attached. Distributed
 *  locks are advisory — see `acquireLock` — because Workers KV is eventually
 *  consistent; they exist to stop duplicate cron work, not to serialise writes.
 * ============================================================================
 */

const WORKER_NAME = "puppetnet-edge-relay";
const WORKER_VERSION = "1.7.0";

/* -------------------------------------------------------------------------- */
/*  Tunables (env-overridable)                                                */
/* -------------------------------------------------------------------------- */

const DEFAULTS = Object.freeze({
  /** Per-host token bucket: 1 request / 2s, small burst. */
  HOST_RATE_PER_SEC: 0.5,
  HOST_BURST: 4,
  /** Hard floor enforced by the Worker regardless of what the client asks for. */
  MIN_HOST_RATE_PER_SEC: 0.02,
  MAX_HOST_BURST: 20,
  /** Global safety valve across all hosts (protects free-tier quotas). */
  GLOBAL_RPM: 900,
  /** Hard ceiling for database-backed requests. AuraDB Free is one 200k-node
   *  instance; 25/min leaves the console responsive without letting one client
   *  turn a page refresh loop into a database incident. Not raisable. */
  GRAPH_RPM: 25,
  GRAPH_CLIENT_RPM: 25,
  /** Distributed graph-bucket sync cadence. Every sync is one KV read and one
   *  write; 5 minutes keeps a constantly-used API under 300 writes/day. */
  GRAPH_KV_SYNC_INTERVAL_MS: 300_000,
  /** Cross-isolate KV synchronisation cadence for a bucket. */
  KV_SYNC_INTERVAL_MS: 20_000,
  /** Failed bearer attempts before an address is briefly locked out. */
  AUTH_FAILURE_LIMIT: 10,
  AUTH_FAILURE_WINDOW_MS: 15 * 60_000,
  /** Redirect chain limit for the SSRF guard. */
  MAX_REDIRECTS: 5,
  /** Response / body caps. */
  MAX_RESPONSE_BYTES: 8 * 1024 * 1024,
  MAX_CONTROL_BODY_BYTES: 256 * 1024,
  /** Networking. */
  DEFAULT_TIMEOUT_MS: 25_000,
  MAX_TIMEOUT_MS: 90_000,
  DEFAULT_MAX_ATTEMPTS: 3,
  MAX_MAX_ATTEMPTS: 6,
  BACKOFF_BASE_MS: 700,
  BACKOFF_CAP_MS: 12_000,
  /** Caching. */
  DEFAULT_CACHE_TTL_SECONDS: 0, // 0 = do not cache
  MAX_CACHE_TTL_SECONDS: 86_400,
  ROBOTS_TTL_SECONDS: 6 * 3600,
  TASK_RESULT_TTL_SECONDS: 3600,
  /** Queue. */
  MAX_QUEUE_DELAY_SECONDS: 300,
  MAX_QUEUE_ATTEMPTS: 3,
});

/* -------------------------------------------------------------------------- */
/*  Browser fingerprint pool                                                  */
/* -------------------------------------------------------------------------- */

/**
 * Real, current-ish desktop/Chrome-Firefox-Safari/Edge fingerprints. Kept in
 * lockstep (UA ↔ sec-ch-ua ↔ platform) so the presented identity is coherent —
 * mismatched UA/client-hints pairs are a trivial bot signal.
 */
const FINGERPRINTS = Object.freeze([
  {
    ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    secChUa: '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
    secChUaPlatform: '"Windows"',
    acceptLanguage: "en-US,en;q=0.9",
  },
  {
    ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    secChUa: '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    secChUaPlatform: '"macOS"',
    acceptLanguage: "en-US,en;q=0.9",
  },
  {
    ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    secChUa: null,
    secChUaPlatform: null,
    acceptLanguage: "en-US,en;q=0.5",
  },
  {
    ua: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    secChUa: '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
    secChUaPlatform: '"Linux"',
    acceptLanguage: "en-GB,en;q=0.9",
  },
  {
    ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    secChUa: null,
    secChUaPlatform: null,
    acceptLanguage: "en-GB,en;q=0.9",
  },
  {
    ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    secChUa: '"Chromium";v="130", "Microsoft Edge";v="130", "Not?A_Brand";v="99"',
    secChUaPlatform: '"Windows"',
    acceptLanguage: "en-US,en;q=0.9",
  },
  {
    ua: "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    secChUa: null,
    secChUaPlatform: null,
    acceptLanguage: "en-US,en;q=0.5",
  },
]);

/**
 * Headers a caller must never be able to smuggle through the relay.
 */
const BLOCKED_REQUEST_HEADERS = Object.freeze(
  new Set([
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    // Never used in practice (RFC 2965 is dead), but the direct transport drops
    // it and the relay must not be the easier door of the two — `[security]
    // invariant/headerFilterParity` fails the build on exactly this asymmetry.
    "cookie2",
    "cf-connecting-ip",
    "cf-ipcountry",
    "cf-ray",
    "cf-visitor",
    "cf-worker",
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "proxy-authorization",
    "upgrade",
    "expect",
    "te",
    "keep-alive",
  ])
);

/**
 * Wildcard-DNS services that answer with whatever address the *name* encodes
 * (`127.0.0.1.nip.io`, `10-0-0-1.sslip.io`, `anything.lvh.me`). The literal-IP
 * test in validateTargetUrl never sees an address for those, and an edge Worker
 * has no resolver to ask, so the service itself has to be refused by name.
 */
const IP_ECHO_HOSTS = Object.freeze([
  "nip.io", "sslip.io", "xip.io", "localtest.me", "lvh.me", "traefik.me", "vcap.me", "lacolhost.com",
]);

/** Private, reserved and loopback IPv6, decided by the leading hextet. */
function isPrivateIpv6Host(host) {
  if (host.indexOf(":") < 0) return false;
  // `::` covers the unspecified address, the loopback `::1` and every IPv4-mapped
  // form (`::ffff:7f00:1`).
  if (host.startsWith("::")) return true;
  const first = parseInt(host.split(":")[0], 16);
  if (!Number.isFinite(first)) return false;
  return (first >= 0xfc00 && first <= 0xfdff)   // fc00::/7  unique local
    || (first >= 0xfe80 && first <= 0xfebf)     // fe80::/10 link local
    || (first >= 0xfec0 && first <= 0xfeff)     // fec0::/10 site local (deprecated)
    || first >= 0xff00;                          // ff00::/8  multicast
}

const TEXT_CONTENT_TYPES = Object.freeze([
  "text/",
  "application/json",
  "application/xml",
  "application/rss",
  "application/atom",
  "application/ld+json",
  "application/x-ndjson",
  "application/csv",
  "application/javascript",
  "application/x-www-form-urlencoded",
  "image/svg+xml",
]);

/* -------------------------------------------------------------------------- */
/*  Small utilities                                                           */
/* -------------------------------------------------------------------------- */

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** Deterministic, dependency-free string hash (FNV-1a, 32-bit, hex). */
function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** Pick a fingerprint: stable per (host, runSalt) but varies per attempt. */
function pickFingerprint(host, attempt, salt = "") {
  const seedStr = `${host}|${attempt}|${salt}`;
  let h = 0x811c9dc5;
  for (let i = 0; i < seedStr.length; i += 1) {
    h ^= seedStr.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return FINGERPRINTS[h % FINGERPRINTS.length];
}

function constantTimeEqual(a, b) {
  const bufA = encoder.encode(String(a));
  const bufB = encoder.encode(String(b));
  if (bufA.length !== bufB.length) return false;
  // Workers expose a native helper; fall back to a manual XOR accumulator.
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (subtle && typeof subtle.timingSafeEqual === "function") {
    try {
      return subtle.timingSafeEqual(bufA, bufB);
    } catch (_) {
      /* fall through */
    }
  }
  let diff = 0;
  for (let i = 0; i < bufA.length; i += 1) diff |= bufA[i] ^ bufB[i];
  return diff === 0;
}

function num(value, fallback, min = -Infinity, max = Infinity) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function jitter(baseMs) {
  return Math.round(baseMs * (0.6 + Math.random() * 0.8));
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const CHUNK = 0x8000; // avoid blowing the call stack on multi-MB payloads
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function fromBase64(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function sha256Hex(input) {
  const data = typeof input === "string" ? encoder.encode(input) : input;
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function isTextContentType(contentType) {
  const ct = String(contentType || "").toLowerCase();
  if (!ct) return true; // unknown → treat as text, caller decides
  return TEXT_CONTENT_TYPES.some((prefix) => ct.startsWith(prefix));
}

/* -------------------------------------------------------------------------- */
/*  HTTP response helpers                                                     */
/* -------------------------------------------------------------------------- */

/**
 * env plus the caller's Origin, resolved once per request in the fetch
 * entrypoint. corsHeaders has to answer per request (see below) but is reached
 * through jsonResponse from deep inside handlers, and a module-level "current
 * origin" would leak between concurrent requests at every await — so it travels
 * with the env copy instead. Not operator configuration: the key is prefixed to
 * say so.
 */
function withRequestOrigin(env, request) {
  const origin = request && request.headers ? (request.headers.get("origin") || "") : "";
  return Object.assign({}, env, { __requestOrigin: origin.trim() });
}

function corsHeaders(env) {
  const allowed = String(env.ALLOWED_ORIGINS || "*")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const requestOrigin = String(env.__requestOrigin || "");
  const headers = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-Id, X-Source-Id",
    "Access-Control-Max-Age": "86400",
    // The answer depends on the caller's Origin, so no cache may serve one
    // origin's response to another.
    Vary: "Origin",
    // Every route answers JSON, including the ones that echo a path or an error
    // message back; nosniff keeps a browser from guessing otherwise.
    "X-Content-Type-Options": "nosniff",
    // The Worker is an API, never a browsable page: no framing, no referrer
    // leak of a token-bearing URL, no device permissions. HSTS is harmless on
    // the workers.dev host and correct on a custom domain.
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  };
  // Access-Control-Allow-Origin names exactly one origin, or `*`. Joining a list
  // into it — what this function used to do — produces a value every browser
  // rejects, so a deployment allowing two origins served neither: the console
  // would fail CORS on all but (at best) the first.
  if (allowed.length === 1 && allowed[0] === "*") {
    headers["Access-Control-Allow-Origin"] = "*";
  } else if (requestOrigin && allowed.indexOf(requestOrigin) >= 0) {
    headers["Access-Control-Allow-Origin"] = requestOrigin;
  } else if (allowed.length === 1 && !requestOrigin) {
    // No Origin header (curl, a health probe, the queue): naming the single
    // allowed origin costs nothing and keeps non-browser callers working.
    headers["Access-Control-Allow-Origin"] = allowed[0];
  }
  // Anything else gets no Access-Control-Allow-Origin at all, which is the
  // refusal: the browser blocks the response and nothing was leaked by it.
  return headers;
}

function jsonResponse(payload, status = 200, extraHeaders = {}, env = {}) {
  return new Response(JSON.stringify(payload, null, 0), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-PuppetNET-Worker": `${WORKER_NAME}@${WORKER_VERSION}`,
      ...corsHeaders(env),
      ...extraHeaders,
    },
  });
}

function errorResponse(code, message, status, extra = {}, env = {}) {
  return jsonResponse({ ok: false, error: { code, message, ...extra } }, status, extra.headers || {}, env);
}

/* -------------------------------------------------------------------------- */
/*  Configuration                                                             */
/* -------------------------------------------------------------------------- */

function readConfig(env) {
  return {
    hostRatePerSec: num(env.HOST_RATE_PER_SEC, DEFAULTS.HOST_RATE_PER_SEC, DEFAULTS.MIN_HOST_RATE_PER_SEC, 1000),
    // Lower it, never raise it: the ceiling belongs to the database, not to a
    // deployment variable.
    graphRpm: Math.min(DEFAULTS.GRAPH_RPM, num(env.GRAPH_RPM, DEFAULTS.GRAPH_RPM, 1, DEFAULTS.GRAPH_RPM)),
    graphClientRpm: Math.min(DEFAULTS.GRAPH_CLIENT_RPM, num(env.GRAPH_CLIENT_RPM, DEFAULTS.GRAPH_CLIENT_RPM, 1, DEFAULTS.GRAPH_CLIENT_RPM)),
    graphKvSyncIntervalMs: num(env.GRAPH_KV_SYNC_INTERVAL_MS, DEFAULTS.GRAPH_KV_SYNC_INTERVAL_MS, 60_000, 3_600_000),
    authFailureLimit: num(env.AUTH_FAILURE_LIMIT, DEFAULTS.AUTH_FAILURE_LIMIT, 3, 1000),
    authFailureWindowMs: num(env.AUTH_FAILURE_WINDOW_MS, DEFAULTS.AUTH_FAILURE_WINDOW_MS, 60_000, 86_400_000),
    maxRedirects: num(env.MAX_REDIRECTS, DEFAULTS.MAX_REDIRECTS, 0, 10),
    blockedPorts: String(env.BLOCKED_TARGET_PORTS || "22,23,25,445,1433,2375,2376,3306,5432,6379,9200,11211,27017")
      .split(",").map((value) => parseInt(value.trim(), 10)).filter((value) => Number.isInteger(value) && value > 0 && value < 65536),
    hostBurst: num(env.HOST_BURST, DEFAULTS.HOST_BURST, 1, DEFAULTS.MAX_HOST_BURST),
    minHostRatePerSec: num(env.MIN_HOST_RATE_PER_SEC, DEFAULTS.MIN_HOST_RATE_PER_SEC, 0.001, 100),
    maxHostBurst: num(env.MAX_HOST_BURST, DEFAULTS.MAX_HOST_BURST, 1, 1000),
    globalRpm: num(env.GLOBAL_RPM, DEFAULTS.GLOBAL_RPM, 1, 100000),
    kvSyncIntervalMs: num(env.KV_SYNC_INTERVAL_MS, DEFAULTS.KV_SYNC_INTERVAL_MS, 1000, 600000),
    maxResponseBytes: num(env.MAX_RESPONSE_BYTES, DEFAULTS.MAX_RESPONSE_BYTES, 1024, 25 * 1024 * 1024),
    maxControlBodyBytes: num(env.MAX_CONTROL_BODY_BYTES, DEFAULTS.MAX_CONTROL_BODY_BYTES, 1024, 4 * 1024 * 1024),
    defaultTimeoutMs: num(env.DEFAULT_TIMEOUT_MS, DEFAULTS.DEFAULT_TIMEOUT_MS, 1000, DEFAULTS.MAX_TIMEOUT_MS),
    maxTimeoutMs: num(env.MAX_TIMEOUT_MS, DEFAULTS.MAX_TIMEOUT_MS, 1000, 300000),
    defaultMaxAttempts: num(env.DEFAULT_MAX_ATTEMPTS, DEFAULTS.DEFAULT_MAX_ATTEMPTS, 1, DEFAULTS.MAX_MAX_ATTEMPTS),
    maxMaxAttempts: num(env.MAX_MAX_ATTEMPTS, DEFAULTS.MAX_MAX_ATTEMPTS, 1, 10),
    defaultCacheTtlSeconds: num(env.DEFAULT_CACHE_TTL_SECONDS, DEFAULTS.DEFAULT_CACHE_TTL_SECONDS, 0, DEFAULTS.MAX_CACHE_TTL_SECONDS),
    maxCacheTtlSeconds: num(env.MAX_CACHE_TTL_SECONDS, DEFAULTS.MAX_CACHE_TTL_SECONDS, 60, 7 * 86400),
    robotsTtlSeconds: num(env.ROBOTS_TTL_SECONDS, DEFAULTS.ROBOTS_TTL_SECONDS, 600, 7 * 86400),
    taskResultTtlSeconds: num(env.TASK_RESULT_TTL_SECONDS, DEFAULTS.TASK_RESULT_TTL_SECONDS, 600, 7 * 86400),
    maxQueueDelaySeconds: num(env.MAX_QUEUE_DELAY_SECONDS, DEFAULTS.MAX_QUEUE_DELAY_SECONDS, 5, 900),
    enforceHttps: String(env.ENFORCE_HTTPS ?? "true").toLowerCase() !== "false",
    allowPrivateNetworks: String(env.ALLOW_PRIVATE_NETWORKS ?? "false").toLowerCase() === "true",
    blockedHostSuffixes: String(env.BLOCKED_HOST_SUFFIXES || "")
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  };
}

/* -------------------------------------------------------------------------- */
/*  OSINT host profiles                                                       */
/* -------------------------------------------------------------------------- */

/**
 * What this relay knows about the APIs it exists to serve.
 *
 * A profile can set four kinds of thing:
 *
 *  1. **Presentation** (`api: true`) — send an honest, stable API client instead
 *     of a rotating browser fingerprint. Wikidata, OpenCorporates, Companies
 *     House and RapidAPI all publish "identify your tool" policies; spoofing a
 *     browser at an API is how a project gets its whole IP range banned, and a
 *     Cloudflare Worker's IP range is shared. Fingerprint rotation stays on for
 *     genuine websites (the ICIJ search UI, news).
 *  2. **Politeness ceiling** (`ratePerSec`/`burst`) — a *maximum* the caller
 *     cannot raise. The Python side already sends per-source rates, but a bug or
 *     a hand-run loop must not be able to hammer a host into a 429 ban from the
 *     edge. The caller can always ask for less.
 *  3. **Credentials** (`credential`) — injected from Worker secrets at the last
 *     possible moment (inside `performFetch`, never into a queued task or a KV
 *     cache key). An authenticated OpenCorporates call also carries a far higher
 *     daily quota, which is the difference between "rate limited" and "harvested".
 *  4. **Shape** (`timeoutMs`, `accept`, `cacheTtlSeconds`, `sparqlForm`) — the
 *     contract of endpoints that are not plain GETs: SPARQL is a form POST and
 *     WDQS wants `maxlag` so it can say "I am behind, come back" instead of
 *     queueing our query on a lagging cluster.
 *
 * Matching is by longest host suffix, so `www.adsbdb.com` inherits `adsbdb.com`
 * and `adsbexchange-com1.p.rapidapi.com` inherits `p.rapidapi.com`.
 */
const HOST_PROFILES = Object.freeze({
  "query.wikidata.org": Object.freeze({
    label: "wikidata-sparql",
    api: true,
    // Wikidata's policy: identify yourself, keep well under 1 req/s, use maxlag.
    ratePerSec: 0.25,
    burst: 1,
    timeoutMs: 120_000,
    accept: "application/sparql-results+json;q=0.9,application/json;q=0.8",
    userAgentEnv: "WIKIDATA_USER_AGENT",
    userAgentFallback: "PuppetNET/1.5 (OSINT research harvester; github.com/AndrexTheDev/PuppetNET) edge-relay",
    // Wikidata publishes a User-Agent + rate policy instead of a robots policy.
    respectRobots: false,
    sparqlForm: true,
    maxlag: 5,
    cacheTtlSeconds: 3600,
  }),
  "api.opencorporates.com": Object.freeze({
    label: "opencorporates",
    api: true,
    ratePerSec: 0.5,
    burst: 2,
    timeoutMs: 30_000,
    accept: "application/json",
    // Anonymous quota is a handful of calls a day; OPENCORPORATES_API_TOKEN lifts it.
    credential: "opencorporates",
    cacheTtlSeconds: 21600,
  }),
  "adsbdb.com": Object.freeze({
    label: "adsbdb",
    api: true,
    // A community read-through of ADS-B Exchange data: be a good guest.
    ratePerSec: 0.5,
    burst: 2,
    timeoutMs: 20_000,
    accept: "application/json",
    cacheTtlSeconds: 43200,
  }),
  "p.rapidapi.com": Object.freeze({
    label: "rapidapi-adsbexchange",
    api: true,
    ratePerSec: 1,
    burst: 2,
    timeoutMs: 20_000,
    accept: "application/json",
    // ADS-B Exchange v2 lives on RapidAPI and bills per call — cache hard.
    credential: "rapidapi",
    cacheTtlSeconds: 300,
  }),
  "registry.faa.gov": Object.freeze({
    label: "faa-registry",
    api: true,
    ratePerSec: 0.1,
    burst: 1,
    timeoutMs: 180_000,
    accept: "application/zip,application/octet-stream;q=0.9,*/*;q=0.5",
    cacheTtlSeconds: 86400,
    // The Releasable Aircraft ZIP is tens of megabytes, so the harvester streams
    // it directly (the relay buffers a whole response before returning it and the
    // free tier caps CPU time). This profile covers the small pages and any
    // caller that does try the dump through the edge.
  }),
  "api.company-information.service.gov.uk": Object.freeze({
    label: "companies-house",
    api: true,
    ratePerSec: 2,
    burst: 5,
    timeoutMs: 30_000,
    accept: "application/json",
    credential: "companiesHouse",
    cacheTtlSeconds: 21600,
  }),
  "offshoreleaks.icij.org": Object.freeze({
    label: "icij-offshore-leaks",
    // A public website in front of Cloudflare: read it like a reader, not an API.
    api: false,
    ratePerSec: 0.25,
    burst: 2,
    timeoutMs: 60_000,
    cacheTtlSeconds: 21600,
  }),
});

/** Hostname → profile, longest-suffix match, memoised per isolate. */
const profileCache = new Map();

function profileFor(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/:\d+$/, "").trim();
  if (!host) return null;
  if (profileCache.has(host)) return profileCache.get(host);

  let best = null;
  let bestLength = -1;
  for (const [suffix, profile] of Object.entries(HOST_PROFILES)) {
    if (host === suffix || host.endsWith(`.${suffix}`)) {
      if (suffix.length > bestLength) {
        best = profile;
        bestLength = suffix.length;
      }
    }
  }
  profileCache.set(host, best);
  return best;
}

/**
 * Apply a host profile's *non-secret* policy to a task, in place.
 *
 * Credentials are deliberately NOT applied here: `handleFetch` may go on to
 * enqueue the task or hash it into a cache key, and a secret must not be written
 * into a Queue message or a KV entry. `injectCredentials` runs inside
 * `performFetch`, on the copy that actually leaves the isolate.
 *
 * Returns the list of adjustments, which is echoed (without values) in the
 * response meta so an operator can see why a request was slowed down.
 */
function applyHostProfile(task, url, env, cfg) {
  const profile = profileFor(url.hostname);
  if (!profile) return { profile: null, notes: [] };

  const notes = [`profile:${profile.label}`];
  if (profile.api) {
    task.api_mode = true;
    notes.push("api-mode");
  }

  // 1) Politeness ceiling: min(what the caller asked for, what the host allows).
  const requested = task.rate_limit || {};
  const askedRate = num(requested.rate_per_sec, cfg.hostRatePerSec, cfg.minHostRatePerSec, cfg.hostRatePerSec * 10);
  const askedBurst = Math.round(num(requested.burst, cfg.hostBurst, 1, cfg.maxHostBurst));
  const ratePerSec = Math.min(askedRate, profile.ratePerSec);
  const burst = Math.max(1, Math.min(askedBurst, profile.burst));
  task.rate_limit = { ...requested, rate_per_sec: ratePerSec, burst };
  if (ratePerSec < askedRate) notes.push(`rate-capped:${ratePerSec}/s`);
  if (burst < askedBurst) notes.push(`burst-capped:${burst}`);

  // 2) Timeout: the longer of the two, never above the Worker's own ceiling.
  if (profile.timeoutMs) {
    const timeoutMs = Math.min(Math.max(task.timeout_ms, profile.timeoutMs), cfg.maxTimeoutMs);
    if (timeoutMs !== task.timeout_ms) notes.push(`timeout:${timeoutMs}ms`);
    task.timeout_ms = timeoutMs;
  }

  // 3) Presentation defaults — the caller's explicit values always win.
  if (!task.user_agent) {
    const fromEnv = String((profile.userAgentEnv && env[profile.userAgentEnv]) || "").trim();
    task.user_agent = fromEnv || profile.userAgentFallback || "";
    if (task.user_agent) notes.push(fromEnv ? "ua:env" : "ua:profile");
  }
  if (!task.accept && profile.accept) {
    task.accept = profile.accept;
    notes.push("accept:profile");
  }

  // 4) robots.txt can only be turned ON by a profile, never off.
  if (profile.respectRobots === true && !task.respect_robots) {
    task.respect_robots = true;
    notes.push("robots:on");
  }

  // 5) Cache TTL default for hosts whose data changes slowly.
  if (!task.cache_ttl_seconds && profile.cacheTtlSeconds) {
    task.cache_ttl_seconds = Math.min(profile.cacheTtlSeconds, cfg.maxCacheTtlSeconds);
    notes.push(`cache:${task.cache_ttl_seconds}s`);
  }

  // 6) SPARQL is a form POST; make sure it looks like one.
  if (profile.sparqlForm) {
    const note = ensureSparqlForm(task, profile);
    if (note) notes.push(note);
  }

  return { profile, notes };
}

/**
 * WDQS accepts `application/x-www-form-urlencoded` (and a JSON POST is silently
 * wrong), so a caller that sent the query as JSON is converted here, and
 * `format`/`maxlag` are filled in when missing.
 */
function ensureSparqlForm(task, profile) {
  if (String(task.method || "GET").toUpperCase() !== "POST") return "";

  if (task.json && typeof task.json === "object" && typeof task.json.query === "string") {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(task.json)) {
      if (value === null || value === undefined) continue;
      params.set(key, String(value));
    }
    if (!params.has("format")) params.set("format", "json");
    if (profile.maxlag && !params.has("maxlag")) params.set("maxlag", String(profile.maxlag));
    task.body_text = params.toString();
    task.json = null;
    task.content_type = "application/x-www-form-urlencoded";
    return "sparql:json-to-form";
  }

  if (typeof task.body_text === "string" && task.body_text.length > 0) {
    let params;
    try {
      params = new URLSearchParams(task.body_text);
    } catch (_) {
      return "";
    }
    if (!params.has("query")) return "";
    let changed = false;
    if (!params.has("format")) {
      params.set("format", "json");
      changed = true;
    }
    if (profile.maxlag && !params.has("maxlag")) {
      params.set("maxlag", String(profile.maxlag));
      changed = true;
    }
    if (changed) task.body_text = params.toString();
    if (!task.content_type) task.content_type = "application/x-www-form-urlencoded";
    return changed ? "sparql:defaults-added" : "sparql:form";
  }
  return "";
}

/**
 * Strip a URL-injected credential before the URL is reported anywhere.
 *
 * `injectCredentials` puts the token on the request that leaves the isolate, but
 * the response envelope, the attempts log, the stored task result and the KV
 * cache entry are all read back by the harvester — and by whoever reads its
 * logs. Header credentials never reach a URL, so there is nothing to strip.
 */
function redactUrl(rawUrl, profile) {
  if (!profile || !profile.credential) return String(rawUrl);
  try {
    const url = new URL(String(rawUrl));
    if (profile.credential === "opencorporates") url.searchParams.delete("api_token");
    return url.toString();
  } catch (_) {
    return String(rawUrl);
  }
}

/**
 * Attach this host's credentials at the moment of egress.
 *
 * Called from `performFetch` on the outgoing copy only, so a secret is never
 * written into a deferred Queue task, a KV cache key or a response body. The
 * returned note names what happened without naming the value.
 */
function injectCredentials(task, url, profile, env) {
  if (!profile || !profile.credential) return "";
  const headers = task.headers && typeof task.headers === "object" ? { ...task.headers } : {};
  const has = (name) => Object.keys(headers).some((key) => key.toLowerCase() === name);

  if (profile.credential === "opencorporates") {
    const token = String(env.OPENCORPORATES_API_TOKEN || "").trim();
    if (!token) return "credential:opencorporates-missing";
    if (url.searchParams.has("api_token")) return "";
    url.searchParams.set("api_token", token);
    task.url = url.toString();
    return "credential:opencorporates-applied";
  }

  if (profile.credential === "rapidapi") {
    const key = String(env.ADSBEXCHANGE_API_KEY || env.X_RAPIDAPI_KEY || env.RAPIDAPI_KEY || "").trim();
    if (!key) return "credential:rapidapi-missing";
    if (!has("x-rapidapi-key")) headers["x-rapidapi-key"] = key;
    if (!has("x-rapidapi-host")) headers["x-rapidapi-host"] = String(env.RAPIDAPI_HOST || url.hostname);
    task.headers = headers;
    return "credential:rapidapi-applied";
  }

  if (profile.credential === "companiesHouse") {
    const key = String(env.COMPANIES_HOUSE_API_KEY || "").trim();
    if (!key) return "credential:companies-house-missing";
    if (!has("authorization")) headers.Authorization = `Basic ${toBase64(encoder.encode(`${key}:`))}`;
    task.headers = headers;
    return "credential:companies-house-applied";
  }

  return "";
}

/* -------------------------------------------------------------------------- */
/*  Rate limiting — hybrid in-isolate + KV token bucket                       */
/* -------------------------------------------------------------------------- */

const localBuckets = new Map(); // host -> { tokens, ts, hits, windowStart, synced, denied }
const localSyncState = new Map(); // host -> { windowStart, lastSync } — legacy view, kept for /stats
const globalWindow = { minute: 0, count: 0 };

function nowMs() {
  return Date.now();
}

function refill(bucket, ratePerSec, burst, now) {
  const elapsed = Math.max(0, now - bucket.ts) / 1000;
  bucket.tokens = Math.min(burst, bucket.tokens + elapsed * ratePerSec);
  bucket.ts = now;
  return bucket;
}

/** Global requests-per-minute guard shared by every host. */
function checkGlobalRate(cfg) {
  const now = nowMs();
  const minute = Math.floor(now / 60000);
  if (globalWindow.minute !== minute) {
    globalWindow.minute = minute;
    globalWindow.count = 0;
  }
  if (globalWindow.count >= cfg.globalRpm) {
    // Whole seconds: this number reaches clients as retry_after and the queue
    // consumer as delaySeconds, and Cloudflare Queues takes an integer.
    const retryAfter = Math.max(1, Math.ceil(((minute + 1) * 60000 - now) / 1000));
    return { allowed: false, retryAfter };
  }
  globalWindow.count += 1;
  return { allowed: true, retryAfter: 0 };
}

/**
 * Merge a KV-persisted bucket with the in-isolate bucket — for *hot* hosts only.
 *
 * KV is eventually consistent and each isolate keeps its own view, so we take
 * the *most conservative* of the two (fewest tokens, oldest timestamp) to keep
 * the fleet's aggregate request rate close to the configured budget.
 *
 * The KV namespace is the scarce resource, not the requests: Workers KV Free
 * allows 100k reads and **1k writes per day**. The relay therefore only involves
 * KV when there is something to coordinate:
 *
 *   * a **cold** host (fewer than two requests in the current
 *     `kvSyncIntervalMs` window) is skipped entirely — one request cannot exceed
 *     a shared budget, so a read would teach nothing and a write would publish
 *     nothing. News harvesting is exactly this shape: hundreds of distinct
 *     article hosts, one request each, 24 times a day.
 *   * a **hot** host costs at most one read + one write per window, and a host
 *     that was rate-limited publishes that immediately (`denied`).
 *
 * Before this gate, every served request wrote its bucket
 * (`acquireHostToken`'s fire-and-forget `put`), plus one write per host per
 * window here: an hourly run over ~400 article hosts spent the day's write quota
 * in a single run and left the fleet without shared rate limits for the rest of
 * the day. See `docs/edge-relay.md` § Free-tier KV budget.
 */
async function syncBucketWithKv(env, cfg, host, local, policy) {
  if (!env.RATE_LIMIT_KV) return local;
  const key = `rl:${host}`;
  const now = nowMs();
  const hot = local.hits >= 2 || local.denied === true;
  if (!hot) return local;
  const last = localSyncState.get(host) || 0;
  // The graph API has its own (much slower) sync cadence: its budget is a
  // database-protection ceiling, and publishing it every 20 s would spend the
  // KV Free write quota on a namespace that also stores locks and cache entries.
  const interval = policy && policy.kv_sync_interval_ms
    ? Math.max(cfg.kvSyncIntervalMs, policy.kv_sync_interval_ms)
    : cfg.kvSyncIntervalMs;
  if (now - last < interval) return local;
  const effectiveRate = (policy && policy.rate) || cfg.hostRatePerSec;
  const effectiveBurst = (policy && policy.burst) || cfg.hostBurst;
  localSyncState.set(host, now);

  let remote = null;
  try {
    remote = await env.RATE_LIMIT_KV.get(key, "json");
  } catch (err) {
    console.warn(`[ratelimit] KV read failed for ${host}: ${err && err.message}`);
    return local;
  }

  if (remote && Number.isFinite(remote.tokens) && Number.isFinite(remote.ts)) {
    // Refill the remote bucket with *its own* rate and burst. Using
    // cfg.maxHostBurst here let a neighbour's bucket refill far above the burst
    // it was written with, so the conservative merge below compared against an
    // inflated number and dropped the constraint instead of applying it.
    const remoteBurst = num(remote.burst, effectiveBurst, 1, cfg.maxHostBurst);
    const remoteRefilled = refill({ tokens: remote.tokens, ts: remote.ts }, remote.rate || effectiveRate, remoteBurst, now);
    if (remoteRefilled.tokens < local.tokens) {
      local.tokens = remoteRefilled.tokens;
      local.ts = now;
    }
  }

  try {
    await env.RATE_LIMIT_KV.put(
      key,
      JSON.stringify({ tokens: local.tokens, ts: local.ts, rate: effectiveRate, burst: effectiveBurst, v: WORKER_VERSION }),
      { expirationTtl: Math.max(120, Math.ceil(3600 / Math.max(0.01, effectiveRate))) }
    );
  } catch (err) {
    console.warn(`[ratelimit] KV write failed for ${host}: ${err && err.message}`);
  }
  return local;
}

/**
 * Attempt to consume `cost` tokens for `host`.
 * @returns {{allowed:boolean, remaining:number, retryAfter:number, policy:object}}
 */
async function acquireHostToken(env, cfg, host, cost, requestedPolicy) {
  const ratePerSec = num(
    requestedPolicy && requestedPolicy.rate_per_sec,
    cfg.hostRatePerSec,
    Math.max(cfg.minHostRatePerSec, cfg.hostRatePerSec / 10),
    cfg.hostRatePerSec * 10
  );
  const burst = num(requestedPolicy && requestedPolicy.burst, cfg.hostBurst, 1, cfg.maxHostBurst);
  const policy = { host, ratePerSec, burst, cost };

  const now = nowMs();
  let bucket = localBuckets.get(host);
  if (!bucket) {
    bucket = { tokens: burst, ts: now, hits: 0, windowStart: now, denied: false };
    localBuckets.set(host, bucket);
  }
  // Requests are counted per sync window: the count is what tells the throttled
  // KV sync whether this host is worth coordinating at all.
  if (now - (bucket.windowStart || 0) >= cfg.kvSyncIntervalMs) {
    bucket.windowStart = now;
    bucket.hits = 0;
    bucket.denied = false;
  }
  bucket.hits += 1;

  refill(bucket, ratePerSec, burst, now);
  bucket = await syncBucketWithKv(env, cfg, host, bucket, { rate: ratePerSec, burst, kv_sync_interval_ms: requestedPolicy && requestedPolicy.kv_sync_interval_ms });
  refill(bucket, ratePerSec, burst, nowMs());

  if (bucket.tokens >= cost) {
    bucket.tokens -= cost;
    localBuckets.set(host, bucket);
    // No fire-and-forget write here on purpose: publishing the bucket is the
    // throttled sync's job, which keeps the namespace inside its daily write
    // quota. The cost is that the last requests of a window are published at the
    // start of the next one, which the conservative merge absorbs.
    return { allowed: true, remaining: bucket.tokens, retryAfter: 0, policy };
  }

  const deficit = cost - bucket.tokens;
  const retryAfter = Math.max(1, Math.ceil(deficit / ratePerSec));
  // A denial is the one case worth publishing out of turn: every other colo
  // should stop spending the same host's budget.
  bucket.denied = true;
  localBuckets.set(host, bucket);
  if (env.RATE_LIMIT_KV) {
    syncBucketWithKv(env, cfg, host, bucket, { rate: ratePerSec, burst, kv_sync_interval_ms: requestedPolicy && requestedPolicy.kv_sync_interval_ms }).catch(() => {});
  }
  return { allowed: false, remaining: bucket.tokens, retryAfter, policy };
}

function rateLimitSnapshot() {
  const now = nowMs();
  const hosts = [];
  for (const [host, bucket] of localBuckets.entries()) {
    hosts.push({
      host,
      tokens: Math.round(bucket.tokens * 1000) / 1000,
      age_ms: now - bucket.ts,
      window_hits: bucket.hits || 0,
    });
  }
  hosts.sort((a, b) => a.host.localeCompare(b.host));
  return hosts.slice(0, 500);
}

/* -------------------------------------------------------------------------- */
/*  Caller identity, per-client windows and auth throttling                   */
/* -------------------------------------------------------------------------- */

/**
 * Cloudflare sets `CF-Connecting-IP` at the edge and overwrites any caller
 * value, so it is the only address header worth trusting. `X-Forwarded-For`
 * is deliberately not consulted: the relay is reachable directly, and a
 * caller-controlled header would let one client mint unlimited identities
 * (and evade the 25/min database budget).
 */
function clientAddress(request) {
  if (!request || !request.headers) return "";
  return String(request.headers.get("cf-connecting-ip") || request.headers.get("x-real-ip") || "").trim();
}

/**
 * The address is hashed before it becomes a key. Distribution across millions
 * of callers does not need the plaintext, and a KV/rate-limit key is exactly
 * the kind of place an IP should not linger. The salt is optional; without it
 * the hash is only a privacy speed bump, not a cryptographic commitment.
 */
function clientFingerprint(request, env, credential = null) {
  const address = clientAddress(request) || "shared";
  const salt = (env && env.CLIENT_KEY_SALT) || "puppetnet-edge";
  // Authentication failures always key on the address alone: if a presented
  // (invalid) token were part of the key, rotating garbage tokens would mint a
  // fresh lockout counter and defeat the brute-force guard.
  if (!credential) return address === "shared" ? "shared" : fnv1a(`${salt}:${address}`);
  // Multi-analyst fairness: two people behind one corporate NAT each bring
  // their own valid token and therefore their own 25/min window. The token is
  // folded in as a second FNV-1a digest so the raw secret never becomes a key
  // (or a value in the map). Collisions can only merge two budgets, never widen
  // one beyond the shared database bucket.
  const tokenPrint = fnv1a(`${salt}:token:${credential}`);
  return fnv1a(`${salt}:${address}:token:${tokenPrint}`);
}

/** Tokens recognised by the graph API, in either the singular or plural variable. */
function graphTokenList(env) {
  return [env.GRAPH_API_TOKEN, env.GRAPH_API_TOKENS]
    .flatMap((raw) => String(raw || "").split(","))
    .map((token) => token.trim())
    .filter(Boolean);
}

function relayTokenList(env) {
  return [env.PROXY_AUTH_TOKEN, env.PROXY_AUTH_TOKENS]
    .flatMap((raw) => String(raw || "").split(","))
    .map((token) => token.trim())
    .filter(Boolean);
}

/** The credential as presented, accepting the same headers the API accepts. */
function presentedCredential(request) {
  const header = request.headers.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  return (match ? match[1] : request.headers.get("x-graph-token") || request.headers.get("x-proxy-token") || "").trim();
}

/**
 * A credential worth fingerprinting: only a token that actually authenticates.
 * An arbitrary header must not buy a fresh rate-limit identity, or the 25/min
 * budget would be one random string away from unlimited.
 */
function validCallerToken(request, env) {
  const presented = presentedCredential(request);
  if (!presented) return null;
  for (const token of [...graphTokenList(env), ...relayTokenList(env)]) {
    if (constantTimeEqual(presented, token)) return presented;
  }
  return null;
}

const graphClients = new Map(); // fingerprint -> { minute, count }
const authFailures = new Map(); // fingerprint -> { count, first, until }
const graphWindow = { minute: -1, count: 0 };

function pruneCallerState(now) {
  if (graphClients.size < 512 && authFailures.size < 512) return;
  const minute = Math.floor(now / 60000);
  for (const [key, entry] of graphClients) {
    if (entry.minute !== minute) graphClients.delete(key);
  }
  for (const [key, entry] of authFailures) {
    if (entry.until < now && now - entry.first > 3600_000) authFailures.delete(key);
  }
}

/**
 * Hard per-caller budget for the database-backed routes.
 *
 * A fixed window is used instead of the token bucket because the requirement is
 * a ceiling per clock minute ("25 requests/minute"), not a smooth rate: the
 * bucket still decides burst shaping, this decides the minute. In-isolate only —
 * KV is not linearizable and a read/write per caller would spend the Free-tier
 * write quota faster than the database saves it.
 */
function checkClientGraphRate(request, env, cfg, credential = null) {
  const now = nowMs();
  const minute = Math.floor(now / 60000);
  const key = clientFingerprint(request, env, credential);
  pruneCallerState(now);
  let entry = graphClients.get(key);
  if (!entry || entry.minute !== minute) {
    entry = { minute, count: 0 };
    graphClients.set(key, entry);
  }
  if (graphWindow.minute !== minute) {
    graphWindow.minute = minute;
    graphWindow.count = 0;
  }
  graphWindow.count += 1;
  const resetSeconds = Math.max(1, Math.ceil(((minute + 1) * 60000 - now) / 1000));
  if (entry.count >= cfg.graphClientRpm) {
    return { allowed: false, remaining: 0, limit: cfg.graphClientRpm, resetSeconds, retryAfter: resetSeconds };
  }
  entry.count += 1;
  return { allowed: true, remaining: cfg.graphClientRpm - entry.count, limit: cfg.graphClientRpm, resetSeconds, retryAfter: 0 };
}

function rateHeaders(budget) {
  return {
    "X-RateLimit-Limit": String(budget.limit),
    "X-RateLimit-Remaining": String(Math.max(0, budget.remaining)),
    "X-RateLimit-Reset": String(budget.resetSeconds),
  };
}

/** How many seconds a brute-forcing address is locked out for, if any. */
function authLockSeconds(request, env, cfg) {
  const entry = authFailures.get(clientFingerprint(request, env));
  if (!entry || !entry.until) return 0;
  const left = entry.until - nowMs();
  return left > 0 ? Math.ceil(left / 1000) : 0;
}

function recordAuthFailure(request, env, cfg) {
  const key = clientFingerprint(request, env);
  const now = nowMs();
  let entry = authFailures.get(key);
  if (!entry || now - entry.first > cfg.authFailureWindowMs) entry = { count: 0, first: now, until: 0 };
  entry.count += 1;
  // Ten bad tokens in a quarter of an hour is a credential-stuffing attempt,
  // not a typo. The lock is per address and in-isolate: a wrong token from one
  // caller must not decide anything for the harvest runner on another address.
  if (entry.count >= cfg.authFailureLimit) entry.until = now + cfg.authFailureWindowMs;
  authFailures.set(key, entry);
}

function clearAuthFailures(request, env) {
  authFailures.delete(clientFingerprint(request, env));
}

/** Test hook: reset only the database budget, leaving relay balances intact. */
function resetGraphRateLimits() {
  graphClients.clear();
  graphWindow.minute = -1;
  graphWindow.count = 0;
  localBuckets.delete(GRAPH_RATE_HOST);
  localSyncState.delete(GRAPH_RATE_HOST);
  graphHealthCache.at = 0;
  graphHealthCache.payload = null;
}

/** Test hook: reset every module-level counter so a suite can isolate checks. */
function resetRateLimits() {
  localBuckets.clear();
  localSyncState.clear();
  localAlertSentAt = 0;
  globalWindow.minute = 0;
  globalWindow.count = 0;
  graphClients.clear();
  authFailures.clear();
  graphWindow.minute = -1;
  graphWindow.count = 0;
  graphHealthCache.at = 0;
  graphHealthCache.payload = null;
}

/* -------------------------------------------------------------------------- */
/*  robots.txt                                                                */
/* -------------------------------------------------------------------------- */

/**
 * Minimal but correct-enough robots.txt evaluator: group selection by UA
 * substring, longest-match precedence for Disallow/Allow, `*` wildcard and
 * `$` end anchor support, plus Crawl-delay reporting.
 */
function parseRobots(text) {
  const groups = [];
  let current = null;
  for (const rawLine of String(text || "").split(/\r?\n/)) {
    const line = rawLine.replace(/#.*$/, "").trim();
    if (!line) continue;
    const idx = line.indexOf(":");
    if (idx < 0) continue;
    const field = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (field === "user-agent") {
      // The agent list stays open only until the group's first directive line —
      // *any* directive, including `Crawl-delay`. Keying this on `rules.length`
      // merged `User-agent: *` into the block above whenever that block's only
      // line was a Crawl-delay, spreading one crawler's pace over every other bot.
      if (current && current.agentListOpen) {
        current.agents.push(value.toLowerCase());
      } else {
        current = { agents: [value.toLowerCase()], rules: [], crawlDelay: null, agentListOpen: true };
        groups.push(current);
      }
    } else if (current) {
      current.agentListOpen = false;
      if (field === "disallow" || field === "allow") {
        // An empty `Disallow:` is not a rule: it matches nothing, so it loses to
        // every real pattern. Reading it as `Allow: /` let it outvote a
        // one-character `Disallow: /` on the tie-break above.
        if (value) current.rules.push({ type: field, pattern: value });
      } else if (field === "crawl-delay") {
        const d = Number(value);
        if (Number.isFinite(d) && d > 0) current.crawlDelay = d;
      }
    }
  }
  return groups;
}

function robotPathMatches(pattern, path) {
  // One character at a time, so the two wildcards survive: `*` matches any run of
  // characters, and a `$` anchors only when it ends the pattern — anywhere else it
  // is a literal (which is why it is escaped along with the rest).
  //
  // What this replaced appended `(?:$|[?#])|(?=.)` to every pattern that did not
  // end in `$`. The alternation binds at the top level, so the regex was
  // `^pattern(?:$|[?#])` OR `(?=.)` — and `(?=.)` succeeds at position zero of any
  // non-empty path. Every pattern therefore matched every path, and with
  // longest-match precedence the *longest line of the file* decided the fate of
  // every URL on that host: the relay either refused pages it was welcome to read,
  // or read pages whose rules it was breaking. The shipped fixture serves
  // `Allow: /`, which matched everything both before and after, so nothing caught
  // it — and respect_robots is on by default in the harvester's task model.
  //
  // Matching is a prefix match, exactly as the specification defines it: `/fish`
  // covers `/fish/chips` and also `/fishheads`.
  let regex = "";
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i];
    if (ch === "*") regex += ".*";
    else if (ch === "$" && i === pattern.length - 1) regex += "$";
    else regex += ch.replace(/[.+?^{}()|[\]\\$]/g, "\\$&");
  }
  try {
    return new RegExp(`^${regex}`).test(path);
  } catch (_) {
    return pattern === path || path.startsWith(pattern.replace(/[*$]/g, ""));
  }
}

function isPathAllowed(groups, path, userAgent) {
  if (!groups.length) return { allowed: true, crawlDelay: null };
  const ua = String(userAgent || "").toLowerCase();
  let group = null;
  for (const g of groups) {
    // An empty `User-agent:` line matches nothing; `ua.includes("")` is always
    // true, so without the length check a stray line would swallow every bot.
    if (g.agents.some((a) => a && a !== "*" && ua.includes(a))) {
      group = g;
      break;
    }
  }
  if (!group) group = groups.find((g) => g.agents.includes("*")) || null;
  if (!group) return { allowed: true, crawlDelay: null };

  let best = null;
  for (const rule of group.rules) {
    if (!robotPathMatches(rule.pattern, path)) continue;
    // RFC 9309 §2.2.2: the longest match wins and, on a tie, Allow beats
    // Disallow. Taking the first longest line made the *file order* decide,
    // which is the trap the stdlib parser falls into — and here it would mean
    // refusing a page the origin explicitly allowed.
    if (
      !best ||
      rule.pattern.length > best.pattern.length ||
      (rule.pattern.length === best.pattern.length && rule.type === "allow")
    ) {
      best = rule;
    }
  }
  const allowed = best ? best.type === "allow" : true;
  return { allowed, crawlDelay: group.crawlDelay };
}

async function checkRobots(env, cfg, url, userAgent, respectRobots) {
  if (!respectRobots) return { allowed: true, crawlDelay: null, source: "disabled" };
  const parsed = new URL(url);
  const robotsUrl = `${parsed.origin}/robots.txt`;
  const cacheKey = `robots:${await sha256Hex(robotsUrl)}`;

  let text = null;
  if (env.RATE_LIMIT_KV) {
    try {
      text = await env.RATE_LIMIT_KV.get(cacheKey);
    } catch (_) {
      text = null;
    }
  }
  if (text === null || text === undefined) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      const { response: resp } = await guardedFetch(robotsUrl, {
        headers: { "User-Agent": userAgent, Accept: "text/plain,*/*;q=0.5" },
        signal: controller.signal,
        cf: { cacheEverything: false },
      }, cfg, { maxRedirects: 2 });
      clearTimeout(timer);
      if (resp.ok) {
        text = (await resp.text()).slice(0, 512 * 1024);
      } else if (resp.status === 401 || resp.status === 403) {
        // RFC 9309: a 401/403 on robots.txt means "everything disallowed".
        text = "User-agent: *\nDisallow: /\n";
      } else if (resp.status >= 500) {
        return { allowed: false, crawlDelay: null, source: "unreachable", status: resp.status };
      } else {
        text = ""; // 404 etc → no restrictions (RFC 9309 §2.3.1.3)
      }
    } catch (err) {
      console.warn(`[robots] fetch failed for ${robotsUrl}: ${err && err.message}`);
      // RFC 9309 §2.3.1.4: an unreachable robots.txt is complete disallow.
      // Failing open handed anyone who could break their own rules file a free
      // crawl, and the KV cache only holds *fresh* rules, so reaching this point
      // means there is no cached copy to fall back on.
      return { allowed: false, crawlDelay: null, source: "unreachable" };
    }
    // Cache robots.txt across runs only for hosts we actually revisit (see
    // syncBucketWithKv): a write per cold article host would spend the KV write
    // quota on hosts that are fetched once, and the next run simply fetches their
    // robots.txt again — one polite request per run instead of a quota write.
    const hostBucket = localBuckets.get(parsed.hostname);
    // robots.txt is checked *before* the host token is taken, so "this host has
    // already been requested in this window" is `hits >= 1` here — the same
    // condition the token path spells `hits >= 2` after incrementing.
    const revisited = Boolean(hostBucket) && (hostBucket.hits >= 1 || hostBucket.denied === true);
    if (env.RATE_LIMIT_KV && revisited) {
      env.RATE_LIMIT_KV
        .put(cacheKey, text, { expirationTtl: cfg.robotsTtlSeconds })
        .catch((err) => console.warn(`[robots] KV write failed: ${err && err.message}`));
    }
  }

  const groups = parseRobots(text);
  const verdict = isPathAllowed(groups, parsed.pathname + parsed.search, userAgent);
  return { allowed: verdict.allowed, crawlDelay: verdict.crawlDelay, source: text ? "robots.txt" : "empty" };
}

/* -------------------------------------------------------------------------- */
/*  URL validation (SSRF guard)                                               */
/* -------------------------------------------------------------------------- */

function validateTargetUrl(rawUrl, cfg) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch (_) {
    return { error: "Malformed URL" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { error: `Unsupported protocol: ${parsed.protocol}` };
  }
  if (cfg.enforceHttps && parsed.protocol !== "https:") {
    return { error: "Plaintext HTTP is disabled by policy (set ENFORCE_HTTPS=false to override)" };
  }
  // `https://user:pass@host/` is not a target, it is a credential on its way
  // into a log line and a redirect chain. The relay injects the credentials it
  // owns at egress; it never forwards a caller's.
  if (parsed.username || parsed.password) {
    return { error: "URLs may not embed credentials" };
  }
  // Two normalisations before any comparison, because the URL parser hands out
  // names that look different and resolve identically:
  //   - a trailing dot is the DNS root and changes nothing about where the name
  //     points (`localhost.` reaches loopback exactly like `localhost`), yet it
  //     made both the `host === "localhost"` test and the dotted-quad test fail,
  //     so `http://localhost./` walked straight past the guard — and past the
  //     operator's BLOCKED_HOST_SUFFIXES too;
  //   - IPv6 arrives in brackets.
  // IPv4 in other notations is already canonical by the time it gets here —
  // `new URL("http://2130706433/").hostname` is "127.0.0.1" — so that part of the
  // guard belongs to WHATWG, and the test asserts it stays that way.
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.+$/, "");
  if (!host) return { error: "Empty host" };
  // An IPv6 zone index (`fe80::1%25eth0`) addresses a local interface, and the
  // WHATWG parser hands it through. There is no legitimate public target that
  // needs one.
  if (host.includes("%")) return { error: "IPv6 zone identifiers are not routable targets" };

  // Port policy is a blocklist, not an allowlist: the relay legitimately talks
  // to public APIs on 8080/8443 and to a local Neo4j on 7474, so an allowlist
  // would break documented deployments. These are the ports that turn an SSRF
  // into direct access to a database, cache, admin agent or mail relay.
  const port = parsed.port ? Number(parsed.port) : (parsed.protocol === "https:" ? 443 : 80);
  if (cfg.blockedPorts && cfg.blockedPorts.includes(port)) {
    return { error: `Port ${port} is blocked by policy` };
  }

  for (const suffix of cfg.blockedHostSuffixes) {
    if (host === suffix || host.endsWith(`.${suffix}`)) return { error: `Host blocked by policy: ${host}` };
  }

  if (!cfg.allowPrivateNetworks) {
    if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) {
      return { error: "Private/reserved hostname blocked" };
    }
    for (const service of IP_ECHO_HOSTS) {
      if (host === service || host.endsWith(`.${service}`)) {
        return { error: `Host name encodes an address through ${service}` };
      }
    }
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a, b, c] = host.split(".").map(Number);
      if (
        a === 0 || a === 10 || a === 127                              // this-network, RFC1918, loopback
        || (a === 100 && b >= 64 && b <= 127)                        // RFC6598 carrier-grade NAT (cloud internals)
        || (a === 169 && b === 254)                                  // link local — the metadata service
        || (a === 172 && b >= 16 && b <= 31)                         // RFC1918
        || (a === 192 && b === 168)                                  // RFC1918
        || (a === 192 && b === 0 && c === 0)                         // IETF protocol assignments
        || (a === 198 && (b === 18 || b === 19))                     // benchmarking
        || a >= 224                                                   // multicast and reserved
      ) {
        return { error: "Private/reserved IPv4 range blocked" };
      }
    }
    // Was `startsWith("fc") || startsWith("fe80")`, which refused every hostname
    // beginning with those letters (`fcbank.example`) and still missed fd00::/8,
    // the other half of RFC 4193's unique-local range.
    if (isPrivateIpv6Host(host)) {
      return { error: "Private/reserved IPv6 range blocked" };
    }
  }
  return { url: parsed };
}

/* -------------------------------------------------------------------------- */
/*  Redirect-safe fetch (the other half of the SSRF guard)                    */
/* -------------------------------------------------------------------------- */

const REDIRECT_STATUS = Object.freeze(new Set([301, 302, 303, 307, 308]));

/** Headers that authenticate us to one origin and must not follow a redirect. */
const CREDENTIAL_HEADERS = Object.freeze([
  "authorization", "cookie", "proxy-authorization", "x-api-key", "x-rapidapi-key",
  "x-rapidapi-host", "x-proxy-token", "x-graph-token", "x-status-token",
]);

function withoutCredentials(headers) {
  const next = new Headers();
  headers.forEach((value, key) => {
    if (!CREDENTIAL_HEADERS.includes(key.toLowerCase())) next.set(key, value);
  });
  return next;
}

/**
 * `fetch` with manual redirects, re-validating every hop.
 *
 * `redirect: "follow"` is the SSRF hole that survives every hostname check:
 * `https://allowed.example/r` may answer `302 Location: http://169.254.169.254/`
 * and the runtime follows it internally, so the guard never sees the second URL.
 * This helper asks for the 3xx itself, resolves `Location` against the current
 * URL, refuses a downgrade to plaintext (ENFORCE_HTTPS still applies), drops
 * credentials when the origin changes, and returns the chain for the response
 * report — with every URL already safe.
 *
 * It cannot resolve DNS. An edge Worker has no resolver API, so a public
 * hostname that resolves to a private address is the platform's egress policy
 * territory, not something a JS check can decide. What this layer does guarantee
 * is that no literal private address, encoded address, blocked port or
 * plaintext hop gets a request, including through a redirect.
 */
async function guardedFetch(rawUrl, init, cfg, options = {}) {
  const maxRedirects = options.maxRedirects === undefined ? cfg.maxRedirects : options.maxRedirects;
  let current;
  try {
    current = new URL(rawUrl);
  } catch (_) {
    const err = new Error("Malformed URL");
    err.ssrf = true;
    err.code = "invalid_target";
    throw err;
  }
  let headers = new Headers(init.headers || {});
  let method = String(init.method || "GET").toUpperCase();
  let body = init.body;
  const baseInit = { ...init };
  const chain = [];

  for (let hop = 0; ; hop += 1) {
    const verdict = validateTargetUrl(current.toString(), cfg);
    if (verdict.error) {
      const err = new Error(verdict.error);
      err.ssrf = true;
      err.code = "invalid_target";
      err.blocked_url = current.toString();
      err.chain = chain;
      throw err;
    }
    current = verdict.url;
    const response = await fetch(current.toString(), {
      ...baseInit,
      method,
      headers,
      body: method === "GET" || method === "HEAD" ? undefined : body,
      redirect: "manual",
    });
    const location = response.headers.get("location");
    if (!REDIRECT_STATUS.has(response.status) || !location) {
      return { response, url: current, redirects: chain };
    }
    if (hop >= maxRedirects) {
      // maxRedirects = 0 is the caller asking for the 3xx itself
      // (`follow_redirects: false`); any other limit is a loop or a chain that
      // is too long to reason about, and returning the last hop would hide it.
      if (maxRedirects === 0) return { response, url: current, redirects: chain };
      const err = new Error(`More than ${maxRedirects} redirects`);
      err.ssrf = true;
      err.code = "too_many_redirects";
      err.chain = chain;
      throw err;
    }
    let next;
    try {
      next = new URL(location, current);
    } catch (_) {
      const err = new Error("Redirect target is not a valid URL");
      err.ssrf = true;
      err.code = "invalid_redirect";
      err.chain = chain;
      throw err;
    }
    if (next.origin !== current.origin) headers = withoutCredentials(headers);
    // Browser semantics: 303 always becomes GET; 301/302 turn a POST into GET.
    if (response.status === 303 || ((response.status === 301 || response.status === 302) && method !== "GET" && method !== "HEAD")) {
      method = "GET";
      body = undefined;
      headers.delete("content-type");
      headers.delete("content-length");
    }
    chain.push({ status: response.status, from: current.toString(), to: next.toString() });
    current = next;
  }
}

/**
 * The Neo4j endpoint is operator configuration, not caller input — but a typo
 * (`https://10.0.0.5:7474`) is exactly the private-network reach this Worker is
 * supposed to make impossible, so it goes through the same address policy.
 * Local plaintext development stays possible, and only that.
 */
function validateNeo4jEndpoint(env) {
  const base = neo4jHttpBase(env);
  if (!base) return "NEO4J_URI is not set on this Worker (or is a plaintext URI to a non-local host)";
  let parsed;
  try {
    parsed = new URL(base);
  } catch (_) {
    return "NEO4J_URI is not a valid URL";
  }
  const local = /^(localhost|127\.0\.0\.1|\[::1\])$/i.test(parsed.hostname);
  const cfg = readConfig(env);
  const verdict = validateTargetUrl(base, {
    ...cfg,
    enforceHttps: false, // neo4jHttpBase already refuses remote plaintext
    allowPrivateNetworks: cfg.allowPrivateNetworks || local,
  });
  return verdict.error || "";
}

/* -------------------------------------------------------------------------- */
/*  Header construction (dynamic fingerprinting)                              */
/* -------------------------------------------------------------------------- */

function buildOutboundHeaders(task, url, attempt, cfg) {
  const method = String(task.method || "GET").toUpperCase();
  const headers = new Headers();

  if (task.api_mode) {
    // An API host gets one stable, self-identifying client: no rotating
    // fingerprint, no Sec-Fetch-* navigation hints, no DNT. These endpoints ask
    // for a descriptive User-Agent and answer JSON; pretending to be Chrome is
    // both rude and detectable.
    headers.set("User-Agent", task.user_agent || `PuppetNET/${WORKER_VERSION} (edge-relay)`);
    headers.set("Accept", task.accept || "application/json");
    headers.set("Accept-Encoding", "gzip, deflate, br");
    if (method === "POST" || method === "PUT" || method === "PATCH") {
      headers.set("Content-Type", task.content_type || "application/json");
    }
    if (task.referer) headers.set("Referer", task.referer);
    return applyCallerHeaders(task, headers);
  }

  const fp = pickFingerprint(url.hostname, attempt, task.fingerprint_salt || "");
  headers.set("User-Agent", task.user_agent || fp.ua);
  headers.set("Accept", task.accept || "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7");
  headers.set("Accept-Language", fp.acceptLanguage);
  headers.set("Accept-Encoding", "gzip, deflate, br");
  headers.set("Upgrade-Insecure-Requests", "1");
  headers.set("Sec-Fetch-Dest", "document");
  headers.set("Sec-Fetch-Mode", "navigate");
  headers.set("Sec-Fetch-Site", task.referer ? "cross-site" : "none");
  headers.set("Sec-Fetch-User", "?1");
  headers.set("Cache-Control", "no-cache");
  headers.set("Pragma", "no-cache");
  headers.set("DNT", "1");
  if (fp.secChUa) {
    headers.set("sec-ch-ua", fp.secChUa);
    headers.set("sec-ch-ua-mobile", "?0");
    headers.set("sec-ch-ua-platform", fp.secChUaPlatform);
  }
  if (task.referer) headers.set("Referer", task.referer);
  if (task.origin) headers.set("Origin", task.origin);

  if (method === "POST" || method === "PUT" || method === "PATCH") {
    if (!headers.has("Content-Type")) {
      headers.set("Content-Type", task.content_type || "application/json");
    }
  }

  return applyCallerHeaders(task, headers);
}

/** Caller overrides (API keys, custom Accept, cookies for authenticated feeds). */
function applyCallerHeaders(task, headers) {
  const overrides = task.headers || {};
  for (const [key, value] of Object.entries(overrides)) {
    const lower = key.toLowerCase();
    if (BLOCKED_REQUEST_HEADERS.has(lower)) continue;
    if (value === null || value === undefined) {
      headers.delete(key);
      continue;
    }
    headers.set(key, String(value));
  }
  return { headers, fingerprint: headers.get("User-Agent") || "" };
}

/** Append/rotate a cache-buster so CDNs in front of the origin do not serve a
 *  stale (or poisoned-by-previous-crawler) copy on repeated polls. */
function maybeAddCacheBuster(url, task) {
  if (!task.cache_buster) return url.toString();
  const mode = String(task.cache_buster).toLowerCase();
  const out = new URL(url.toString());
  if (mode === "off" || mode === "false") return out.toString();
  if (mode === "timestamp") {
    out.searchParams.set("_pn", String(Date.now()));
  } else if (mode === "random") {
    out.searchParams.set("_pn", Math.random().toString(36).slice(2, 10));
  }
  return out.toString();
}

/* -------------------------------------------------------------------------- */
/*  Core fetch with retries                                                   */
/* -------------------------------------------------------------------------- */

function shouldRetryStatus(status) {
  return status === 408 || status === 425 || status === 429 || (status >= 500 && status <= 599);
}

async function readBodyCapped(response, maxBytes) {
  const declared = Number(response.headers.get("content-length") || 0);
  if (declared && declared > maxBytes) {
    // Still read the cap so callers get a usable truncated payload + a flag.
  }
  const reader = response.body && response.body.getReader ? response.body.getReader() : null;
  if (!reader) {
    const buf = await response.arrayBuffer();
    return { bytes: new Uint8Array(buf), truncated: buf.byteLength > maxBytes };
  }
  const chunks = [];
  let total = 0;
  let truncated = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      if (total + value.byteLength > maxBytes) {
        chunks.push(value.subarray(0, Math.max(0, maxBytes - total)));
        total = maxBytes;
        truncated = true;
        try {
          await reader.cancel();
        } catch (_) {
          /* ignore */
        }
        break;
      }
      chunks.push(value);
      total += value.byteLength;
    }
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { bytes: merged, truncated };
}

/**
 * Execute a single proxied request with retries, timeouts and size caps.
 * Shared by the synchronous `/fetch` path and the Queue consumer.
 */
async function performFetch(task, env, cfg, meta = {}) {
  const method = String(task.method || "GET").toUpperCase();
  // Credentials are attached to a per-call copy at the moment of egress: a
  // secret must never be written into a deferred Queue task, a KV cache key, a
  // stored task result or a log line.
  const outgoing = { ...task, headers: { ...(task.headers || {}) } };
  const targetUrl = new URL(task.url);
  const hostProfile = profileFor(targetUrl.hostname);
  const credentialNote = injectCredentials(outgoing, targetUrl, hostProfile, env);
  const profileLabel = outgoing.host_profile || (hostProfile && hostProfile.label) || null;
  const targetRaw = maybeAddCacheBuster(targetUrl, outgoing);
  // What we send upstream may carry a credential; what we report back never does.
  const reportUrl = redactUrl(targetRaw, hostProfile);
  const startedAt = nowMs();

  let lastError = null;
  let lastStatus = 0;
  const attemptsLog = [];

  for (let attempt = 1; attempt <= task.max_attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), task.timeout_ms);
    const { headers, fingerprint } = buildOutboundHeaders(outgoing, new URL(targetRaw), attempt, cfg);

    let body;
    if (outgoing.body_b64) body = fromBase64(outgoing.body_b64);
    else if (outgoing.body_text !== undefined && outgoing.body_text !== null) body = String(outgoing.body_text);
    else if (outgoing.json !== undefined && outgoing.json !== null) {
      body = JSON.stringify(outgoing.json);
      if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    }

    const cfOptions = {
      cacheEverything: false,
      cacheTtlByStatus: { "200-299": 0, "400-499": 0, "500-599": 0 },
      scrapeShield: false,
      minify: false,
    };

    try {
      const { response: upstream, url: finalUrl, redirects } = await guardedFetch(targetRaw, {
        method,
        headers,
        body: method === "GET" || method === "HEAD" ? undefined : body,
        signal: controller.signal,
        cf: cfOptions,
      }, cfg, { maxRedirects: outgoing.follow_redirects === false ? 0 : cfg.maxRedirects });
      clearTimeout(timer);
      lastStatus = upstream.status;

      if (shouldRetryStatus(upstream.status) && attempt < outgoing.max_attempts) {
        const retryAfterHeader = Number(upstream.headers.get("retry-after") || 0);
        const wait = retryAfterHeader > 0 ? Math.min(retryAfterHeader * 1000, 30000) : jitter(DEFAULTS.BACKOFF_BASE_MS * 2 ** (attempt - 1));
        attemptsLog.push({ attempt, status: upstream.status, retried_in_ms: wait, ua: fingerprint });
        await sleep(Math.min(wait, DEFAULTS.BACKOFF_CAP_MS));
        continue;
      }

      const contentType = upstream.headers.get("content-type") || "";
      const { bytes, truncated } = await readBodyCapped(upstream, cfg.maxResponseBytes);
      const textLike = isTextContentType(contentType);
      const elapsedMs = nowMs() - startedAt;

      const responseHeaders = {};
      upstream.headers.forEach((value, key) => {
        responseHeaders[key] = value;
      });

      return {
        ok: upstream.ok,
        status: upstream.status,
        status_text: upstream.statusText,
        url: redactUrl(finalUrl.toString() || targetRaw, hostProfile),
        request_url: reportUrl,
        redirects: redirects.map((hop) => ({ ...hop, from: redactUrl(hop.from, hostProfile), to: redactUrl(hop.to, hostProfile) })),
        method,
        content_type: contentType,
        headers: responseHeaders,
        text: textLike ? decoder.decode(bytes) : null,
        body_b64: textLike ? null : toBase64(bytes.buffer),
        byte_length: bytes.byteLength,
        truncated,
        attempts: attempt,
        attempts_log: attemptsLog,
        elapsed_ms: elapsedMs,
        fingerprint_used: fingerprint,
        host_profile: profileLabel,
        credential: credentialNote || null,
        colo: meta.colo || null,
        client_country: meta.country || null,
        served_from: "origin",
      };
    } catch (err) {
      clearTimeout(timer);
      if (err && err.ssrf) {
        // Deterministic refusal: retrying the same redirect chain would only
        // repeat the same blocked hop.
        lastError = err;
        attemptsLog.push({ attempt, error: err.message, blocked: err.blocked_url || null, ua: fingerprint });
        lastStatus = 0;
        break;
      }
      lastError = err && err.name === "AbortError" ? new Error(`timeout after ${task.timeout_ms}ms`) : err;
      attemptsLog.push({ attempt, error: String(lastError && lastError.message), ua: fingerprint });
      if (attempt < task.max_attempts) {
        await sleep(Math.min(jitter(DEFAULTS.BACKOFF_BASE_MS * 2 ** (attempt - 1)), DEFAULTS.BACKOFF_CAP_MS));
      }
    }
  }

  return {
    ok: false,
    status: lastStatus || 0,
    url: reportUrl,
    request_url: reportUrl,
    method,
    error: lastError ? String(lastError.message) : `upstream returned ${lastStatus}`,
    ssrf_blocked: Boolean(lastError && lastError.ssrf),
    attempts: task.max_attempts,
    attempts_log: attemptsLog,
    elapsed_ms: nowMs() - startedAt,
    host_profile: profileLabel,
    credential: credentialNote || null,
    colo: meta.colo || null,
    served_from: "origin",
  };
}

/* -------------------------------------------------------------------------- */
/*  Response cache                                                            */
/* -------------------------------------------------------------------------- */

async function cacheKeyFor(url, method, vary) {
  return `cache:${await sha256Hex(`${method}|${url}|${JSON.stringify(vary || {})}`)}`;
}

async function readCache(env, key) {
  if (!env.RESULT_KV) return null;
  try {
    const raw = await env.RESULT_KV.get(key, "json");
    if (!raw || !raw.result) return null;
    if (raw.expires_at && raw.expires_at < nowMs()) return null;
    return raw;
  } catch (_) {
    return null;
  }
}

async function writeCache(env, key, result, ttlSeconds, maxBytes) {
  if (!env.RESULT_KV || ttlSeconds <= 0) return;
  try {
    const payload = { result, cached_at: nowMs(), expires_at: nowMs() + ttlSeconds * 1000 };
    const serialised = JSON.stringify(payload);
    if (serialised.length > maxBytes) return; // too big to be worth caching
    await env.RESULT_KV.put(key, serialised, { expirationTtl: ttlSeconds });
  } catch (err) {
    console.warn(`[cache] write failed: ${err && err.message}`);
  }
}

/* -------------------------------------------------------------------------- */
/*  Request normalisation                                                     */
/* -------------------------------------------------------------------------- */

function normaliseTask(input, cfg, defaults = {}) {
  const url = String(input.url || defaults.url || "");
  const task = {
    url,
    method: String(input.method || defaults.method || "GET").toUpperCase(),
    headers: input.headers || defaults.headers || {},
    body_text: input.body_text ?? defaults.body_text,
    body_b64: input.body_b64 ?? defaults.body_b64,
    json: input.json ?? defaults.json,
    content_type: input.content_type || defaults.content_type,
    referer: input.referer || defaults.referer,
    origin: input.origin || defaults.origin,
    user_agent: input.user_agent || defaults.user_agent,
    accept: input.accept || defaults.accept,
    fingerprint_salt: input.fingerprint_salt || defaults.fingerprint_salt || "",
    follow_redirects: input.follow_redirects !== false,
    cache_buster: input.cache_buster ?? defaults.cache_buster ?? "off",
    respect_robots: Boolean(input.respect_robots ?? defaults.respect_robots ?? false),
    timeout_ms: num(input.timeout_ms ?? defaults.timeout_ms, cfg.defaultTimeoutMs, 500, cfg.maxTimeoutMs),
    max_attempts: Math.round(num(input.max_attempts ?? defaults.max_attempts, cfg.defaultMaxAttempts, 1, cfg.maxMaxAttempts)),
    cache_ttl_seconds: num(
      input.cache_ttl_seconds ?? defaults.cache_ttl_seconds,
      cfg.defaultCacheTtlSeconds,
      0,
      cfg.maxCacheTtlSeconds
    ),
    priority: num(input.priority ?? defaults.priority, 5, 1, 10),
    source_id: String(input.source_id || defaults.source_id || "unknown"),
    request_id: String(input.request_id || defaults.request_id || crypto.randomUUID()),
    rate_limit: input.rate_limit || defaults.rate_limit || {},
    // Set by applyHostProfile: an API host gets an honest client identity.
    api_mode: Boolean(input.api_mode ?? defaults.api_mode ?? false),
    host_profile: String(input.host_profile || defaults.host_profile || ""),
    // Queue behaviour
    queue_on_limit: Boolean(input.queue_on_limit ?? defaults.queue_on_limit ?? false),
    queue_delay_seconds: num(input.queue_delay_seconds ?? defaults.queue_delay_seconds, 0, 0, cfg.maxQueueDelaySeconds),
  };
  if (!["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].includes(task.method)) {
    throw new Error(`Unsupported method: ${task.method}`);
  }
  return task;
}

/* -------------------------------------------------------------------------- */
/*  Auth                                                                      */
/* -------------------------------------------------------------------------- */

function authorised(request, env) {
  const configured = [env.PROXY_AUTH_TOKEN, env.PROXY_AUTH_TOKENS]
    .flatMap((raw) => String(raw || "").split(","))
    .map((t) => t.trim())
    .filter(Boolean);
  if (configured.length === 0) {
    // Fail closed: an unauthenticated relay is an open proxy.
    return { ok: false, reason: "PROXY_AUTH_TOKEN is not configured on the Worker" };
  }
  const header = request.headers.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  const presented = match ? match[1].trim() : (request.headers.get("x-proxy-token") || "").trim();
  if (!presented) return { ok: false, reason: "Missing bearer token" };
  for (const token of configured) {
    if (constantTimeEqual(presented, token)) return { ok: true };
  }
  return { ok: false, reason: "Invalid token" };
}

/* -------------------------------------------------------------------------- */
/*  Route handlers                                                            */
/* -------------------------------------------------------------------------- */

async function handleFetch(request, env, cfg, ctx) {
  let input;
  if (request.method === "POST") {
    const declared = Number(request.headers.get("content-length") || 0);
    if (declared > cfg.maxControlBodyBytes) {
      return errorResponse("payload_too_large", `Control payload exceeds ${cfg.maxControlBodyBytes} bytes`, 413, {}, env);
    }
    try {
      input = await request.json();
    } catch (_) {
      return errorResponse("bad_request", "Request body must be valid JSON", 400, {}, env);
    }
  } else {
    const url = new URL(request.url);
    input = {
      url: url.searchParams.get("url"),
      method: url.searchParams.get("method") || "GET",
      timeout_ms: url.searchParams.get("timeout_ms"),
      respect_robots: url.searchParams.get("respect_robots") === "1",
      cache_ttl_seconds: url.searchParams.get("cache_ttl_seconds"),
    };
  }

  if (!input || !input.url) return errorResponse("bad_request", "`url` is required", 400, {}, env);

  const globalCheck = checkGlobalRate(cfg);
  if (!globalCheck.allowed) {
    return errorResponse("global_rate_limited", "Worker-wide request budget exhausted for this minute", 429, { retry_after: globalCheck.retryAfter }, env);
  }

  let task;
  try {
    task = normaliseTask(input, cfg);
  } catch (err) {
    return errorResponse("bad_request", err.message, 400, {}, env);
  }

  const validation = validateTargetUrl(task.url, cfg);
  if (validation.error) return errorResponse("invalid_target", validation.error, 400, {}, env);
  const targetUrl = validation.url;

  const meta = {
    colo: request.cf ? request.cf.colo : null,
    country: request.cf ? request.cf.country : null,
  };

  // 0) Host profile: politeness ceiling, API-mode presentation, timeout, cache
  //    TTL and SPARQL body defaults. Applied before the cache/robots/rate-limit
  //    steps because all three read what it sets. Credentials are deliberately
  //    not applied here — see injectCredentials.
  const profiled = applyHostProfile(task, targetUrl, env, cfg);
  if (profiled.profile) {
    task.host_profile = profiled.profile.label;
    meta.host_profile = profiled.profile.label;
    meta.profile_notes = profiled.notes;
  }

  // 1) Response cache
  const cKey = await cacheKeyFor(task.url, task.method, task.headers);
  if (task.cache_ttl_seconds > 0 && request.method === "POST") {
    const hit = await readCache(env, cKey);
    if (hit) {
      return jsonResponse(
        { ok: true, request_id: task.request_id, cached: true, ...hit.result },
        200,
        { "X-PuppetNET-Cache": "HIT" },
        env
      );
    }
  }

  // 2) robots.txt
  const probeUa = task.user_agent || pickFingerprint(targetUrl.hostname, 0, task.fingerprint_salt).ua;
  const robots = await checkRobots(env, cfg, task.url, probeUa, task.respect_robots);
  if (!robots.allowed) {
    // An unreachable rules file is not a verdict, and the client must be able to
    // tell the two apart: `robots_disallowed` is terminal (the client stops and
    // never falls back to its own transport), while this one is a retryable
    // origin-policy failure — the runner's own rules fetch may well succeed where
    // the edge could not reach the origin at all.
    if (robots.source === "unreachable") {
      return errorResponse(
        "robots_unavailable",
        `robots.txt for ${targetUrl.hostname} could not be read`,
        502,
        { robots, request_id: task.request_id },
        env
      );
    }
    return errorResponse(
      "robots_disallowed",
      `robots.txt disallows ${targetUrl.pathname}`,
      403,
      { robots, request_id: task.request_id },
      env
    );
  }
  if (robots.crawlDelay) {
    task.rate_limit = {
      rate_per_sec: Math.min(num(task.rate_limit.rate_per_sec, cfg.hostRatePerSec, 0.001, 1000), 1 / robots.crawlDelay),
      burst: task.rate_limit.burst,
    };
  }

  // 3) Per-host token bucket
  const budget = await acquireHostToken(env, { ...cfg, ctx }, targetUrl.hostname, 1, task.rate_limit);
  if (!budget.allowed) {
    const delaySeconds = Math.min(Math.max(budget.retryAfter, 1), cfg.maxQueueDelaySeconds);

    // 3a) Defer into the rate-limiting queue when the caller opted in and a
    //     Queue + result KV are bound.
    if (task.queue_on_limit && env.FETCH_QUEUE && env.RESULT_KV) {
      const taskId = crypto.randomUUID();
      const explicitDelay = task.queue_delay_seconds > 0 ? task.queue_delay_seconds : delaySeconds;
      try {
        await env.FETCH_QUEUE.send(
          { task_id: taskId, task, meta, enqueued_at: nowMs(), delay_seconds: explicitDelay },
          { delaySeconds: explicitDelay }
        );
        await env.RESULT_KV.put(
          `task:${taskId}`,
          JSON.stringify({
            status: "queued",
            request_id: task.request_id,
            url: task.url,
            source_id: task.source_id,
            retry_after: explicitDelay,
            enqueued_at: nowMs(),
          }),
          { expirationTtl: cfg.taskResultTtlSeconds }
        );
        return jsonResponse(
          { ok: false, deferred: true, task_id: taskId, status: "queued", retry_after: explicitDelay, request_id: task.request_id },
          202,
          { "Retry-After": String(explicitDelay), "X-PuppetNET-Task-Id": taskId },
          env
        );
      } catch (err) {
        console.error(`[queue] enqueue failed: ${err && err.message}`);
        return errorResponse("queue_unavailable", `Could not enqueue request: ${err.message}`, 503, { retry_after: delaySeconds }, env);
      }
    }

    // 3b) Otherwise tell the client to back off (it has its own token bucket).
    return errorResponse(
      "host_rate_limited",
      `Rate budget for ${targetUrl.hostname} exhausted`,
      429,
      {
        retry_after: delaySeconds,
        remaining_tokens: budget.remaining,
        policy: budget.policy,
        request_id: task.request_id,
      },
      env
    );
  }

  // 4) Execute
  const result = await performFetch(task, env, cfg, meta);
  result.request_id = task.request_id;
  result.source_id = task.source_id;
  result.rate_limit = { allowed: true, remaining: budget.remaining, policy: budget.policy };
  result.robots = robots.source === "disabled" ? undefined : robots;

  if (task.cache_ttl_seconds > 0 && result.ok) {
    ctx.waitUntil(writeCache(env, cKey, result, task.cache_ttl_seconds, cfg.maxResponseBytes));
  }

  const status = result.ok ? 200 : result.status && result.status >= 400 ? 502 : 502;
  return jsonResponse(
    { ok: result.ok === true && !result.error, ...result },
    result.error ? 502 : status,
    {
      "X-PuppetNET-Cache": "MISS",
      "X-PuppetNET-Attempts": String(result.attempts || 1),
      "X-PuppetNET-Ratelimit-Remaining": String(Math.floor(budget.remaining)),
    },
    env
  );
}

async function handleTaskLookup(request, env, cfg, taskId) {
  if (!env.RESULT_KV) return errorResponse("kv_unbound", "RESULT_KV is not bound to this Worker", 503, {}, env);
  if (request.method === "DELETE") {
    await env.RESULT_KV.delete(`task:${taskId}`);
    return jsonResponse({ ok: true, deleted: taskId }, 200, {}, env);
  }
  const raw = await env.RESULT_KV.get(`task:${taskId}`, "json");
  if (!raw) return errorResponse("task_not_found", `No task with id ${taskId}`, 404, {}, env);
  return jsonResponse({ ok: raw.status === "done" || raw.status === "queued", ...raw }, 200, {}, env);
}

async function handleCachePurge(request, env) {
  if (!env.RESULT_KV) return errorResponse("kv_unbound", "RESULT_KV is not bound to this Worker", 503, {}, env);
  const url = new URL(request.url).searchParams.get("url");
  if (!url) return errorResponse("bad_request", "`url` query parameter is required", 400, {}, env);
  const key = await cacheKeyFor(url, "GET", {});
  await env.RESULT_KV.delete(key);
  return jsonResponse({ ok: true, purged: url, key }, 200, {}, env);
}

/** Whether the Worker secret behind a profile's credential is present. */
function credentialConfigured(kind, env) {
  if (kind === "opencorporates") return Boolean(String(env.OPENCORPORATES_API_TOKEN || "").trim());
  if (kind === "rapidapi") {
    return Boolean(String(env.ADSBEXCHANGE_API_KEY || env.X_RAPIDAPI_KEY || env.RAPIDAPI_KEY || "").trim());
  }
  if (kind === "companiesHouse") return Boolean(String(env.COMPANIES_HOUSE_API_KEY || "").trim());
  return false;
}

function handleHealth(request, env, cfg) {
  return jsonResponse(
    {
      ok: true,
      worker: WORKER_NAME,
      version: WORKER_VERSION,
      time: new Date().toISOString(),
      bindings: {
        rate_limit_kv: Boolean(env.RATE_LIMIT_KV),
        result_kv: Boolean(env.RESULT_KV),
        queue: Boolean(env.FETCH_QUEUE),
        auth_configured: Boolean(env.PROXY_AUTH_TOKEN || env.PROXY_AUTH_TOKENS),
        // Booleans only: /health is unauthenticated. The URI host is reported by
        // /graph/health, never here.
        graph_api: readGraphConfig(env).enabled && graphConfigured(env),
        graph_token: Boolean(env.GRAPH_API_TOKEN || env.GRAPH_API_TOKENS),
        graph_public_read: String(env.GRAPH_PUBLIC_READ || "false").toLowerCase() === "true",
      },
      graph_api: "/graph/health",
      status: "/status",
      cron: { keepalive: CRON_KEEPALIVE, health: CRON_HEALTH },
      host_profiles: Object.values(HOST_PROFILES).map((profile) => ({
        label: profile.label,
        api_mode: Boolean(profile.api),
        rate_per_sec: profile.ratePerSec,
        burst: profile.burst,
        timeout_ms: profile.timeoutMs || cfg.defaultTimeoutMs,
        cache_ttl_seconds: profile.cacheTtlSeconds || 0,
        // Booleans only: /health is unauthenticated, so it reports whether a
        // credential exists, never what it is.
        credential: profile.credential ? credentialConfigured(profile.credential, env) : null,
      })),
      limits: {
        global_rpm: cfg.globalRpm,
        graph_rpm: cfg.graphRpm,
        graph_client_rpm: cfg.graphClientRpm,
        host_rate_per_sec: cfg.hostRatePerSec,
        host_burst: cfg.hostBurst,
        max_response_bytes: cfg.maxResponseBytes,
        max_timeout_ms: cfg.maxTimeoutMs,
        max_queue_delay_seconds: cfg.maxQueueDelaySeconds,
      },
      colo: request.cf ? request.cf.colo : null,
    },
    200,
    {},
    env
  );
}

function handleStats(env, cfg) {
  return jsonResponse(
    {
      ok: true,
      version: WORKER_VERSION,
      global: { minute: globalWindow.minute, count: globalWindow.count, rpm_limit: cfg.globalRpm },
      graph: {
        minute: graphWindow.minute,
        count: graphWindow.count,
        rpm_limit: cfg.graphRpm,
        clients: graphClients.size,
        health_cached: Boolean(graphHealthCache.payload),
        health_age_ms: graphHealthCache.at ? nowMs() - graphHealthCache.at : null,
      },
      local_buckets: rateLimitSnapshot(),
      bindings: { rate_limit_kv: Boolean(env.RATE_LIMIT_KV), result_kv: Boolean(env.RESULT_KV), queue: Boolean(env.FETCH_QUEUE) },
    },
    200,
    {},
    env
  );
}

/* -------------------------------------------------------------------------- */
/*  Graph read API (Neo4j → browser console)                                  */
/* -------------------------------------------------------------------------- */

/**
 * Why this exists
 * ---------------
 * `web/` is a static SPA on Cloudflare Pages. It cannot hold AuraDB credentials:
 * anything shipped to a browser is public. So the console talks to these
 * endpoints and the Worker holds the secrets — the same split the relay already
 * uses for API tokens.
 *
 * Rules this section holds itself to
 * ----------------------------------
 *  1. **Read-only.** Every statement starts with MATCH/CALL/UNWIND/RETURN. There
 *     is no endpoint that accepts Cypher, and no code path that interpolates a
 *     user string into a statement: values are always parameters. The only
 *     interpolated text is (a) a property name from `GRAPH_SORTABLE` and (b) an
 *     integer hop/depth count that has been clamped and re-parsed as a number.
 *  2. **Bounded.** Every query has LIMIT, every traversal has a hop ceiling, and
 *     the response is capped by size. A dashboard must not be able to ask a
 *     free-tier database for 200k nodes.
 *  3. **Polite.** Graph queries share the per-host token-bucket machinery with
 *     the relay (bucket key `graph:neo4j`) and the Worker-wide RPM guard, and
 *     responses are cached in RESULT_KV, so a page refresh costs one query.
 *  4. **Quiet.** Credentials never appear in a response, an error message or a
 *     log line; the Neo4j URI is reduced to its host before it is reported.
 */

const GRAPH_RATE_HOST = "graph:neo4j";

/** In-isolate cache for the three-query health probe. Public, so it must not
 *  become a free database load generator; 60 s is short enough to notice an
 *  outage and long enough that a refresh loop cannot multiply the probe. */
const graphHealthCache = { at: 0, payload: null };

const GRAPH_LIMITS = Object.freeze({
  DEFAULT_NODES: 250,
  MAX_NODES: 1200,
  DEFAULT_DEPTH: 1,
  MAX_DEPTH: 4,
  DEFAULT_HOPS: 6,
  MAX_HOPS: 12,
  MAX_HOPS_WEIGHTED: 4,
  DEFAULT_TABLE_ROWS: 500,
  MAX_TABLE_ROWS: 2000,
  MAX_QUERY_CHARS: 400,
  MAX_EVIDENCE: 8,
  MAX_CITATIONS: 40,
  MAX_ALIASES: 64,
  PATH_ENUMERATION_CAP: 20000,
});

/**
 * Property names that may appear in an ORDER BY. A property name cannot be a
 * query parameter in Cypher, so anything reaching a statement as text has to come
 * off a list a caller cannot extend.
 */
const GRAPH_SORTABLE = Object.freeze([
  "anomaly_score", "betweenness", "degree_spike", "offshore_cluster_ratio",
  "confidence", "mention_count", "risk_score", "name", "canonical_key",
  "last_seen", "first_seen", "metrics_at", "weight", "observations",
]);

/** Labels the console may filter on — mirrors `puppetnet.domain.DOMAIN_LABELS`. */
const GRAPH_LABELS = Object.freeze([
  "Entity", "Person", "Organization", "Location", "Craft", "Company", "ShellCompany",
  "Foundation", "Offshore", "Aircraft", "Vessel", "Vehicle",
]);

/** Relationship types the console may filter on — mirrors `RelationType`. */
const GRAPH_REL_TYPES = Object.freeze([
  "OWNS", "OWNED_BY", "CONTROLS", "SUBSIDIARY_OF", "PARENT_OF", "ACQUIRED",
  "SHAREHOLDER_OF", "INTERMEDIARY_FOR", "NOMINEE_OF", "BENEFICIARY_OF", "TRUSTEE_OF",
  "DIRECTOR_OF", "OFFICER_OF", "EMPLOYED_BY", "EMPLOYS", "MEMBER_OF", "FOUNDED",
  "APPOINTED_BY", "DONATED_TO", "FUNDED", "FUNDED_BY", "INVESTED_IN", "PAID_TO",
  "CONTRACTED_WITH", "TRANSFERRED_TO", "LOCATED_IN", "REGISTERED_IN", "NATIONAL_OF",
  "OPERATES_IN", "TRAVELED_WITH", "TRAVELED_TO", "OPERATES", "REGISTERED_TO",
  "ARRIVED_FROM", "PASSENGER_ON", "MET_WITH", "FAMILY_OF", "AFFILIATED_WITH",
  "SANCTIONED_BY", "INVESTIGATED_BY", "ACCUSED_OF", "LINKED_OFFSHORE",
  "SHARES_ADDRESS", "MENTIONED_WITH", "PUPPET_MASTER_OF",
]);

const GRAPH_DIRECTIONS = Object.freeze(["undirected", "outgoing", "incoming"]);
const GRAPH_COSTS = Object.freeze(["hops", "inverse-weight", "inverse-confidence"]);

/** Entity projection shared by every node-returning statement. */
const GRAPH_NODE_FIELDS = [
  "e.canonical_key AS key", "e.name AS name", "e.entity_type AS entity_type",
  "labels(e) AS labels", "e.jurisdiction AS jurisdiction", "e.confidence AS confidence",
  "e.mention_count AS mention_count", "e.betweenness AS betweenness",
  "e.anomaly_score AS anomaly_score", "e.anomaly_degree_spike AS anomaly_degree_spike",
  "e.anomaly_offshore_cluster_ratio AS anomaly_offshore_cluster_ratio",
  "e.degree_spike AS degree_spike", "e.offshore_cluster_ratio AS offshore_cluster_ratio",
  "e.cluster_id AS cluster_id", "e.risk_score AS risk_score",
  "e.first_seen AS first_seen", "e.last_seen AS last_seen", "e.metrics_at AS metrics_at",
  "e.aliases AS aliases", "e.source_ids AS source_ids", "e.doc_ids AS doc_ids",
  "e.reg_number AS reg_number", "e.company_number AS company_number", "e.lei AS lei",
  "e.imo AS imo", "e.mmsi AS mmsi", "e.tail_number AS tail_number",
  "e.transponder AS transponder", "e.icao24 AS icao24", "e.wikidata_id AS wikidata_id",
  "e.wikipedia_id AS wikipedia_id", "e.opencorporates_url AS opencorporates_url",
  "e.address_key AS address_key", "e.shell_risk AS shell_risk", "e.flag AS flag",
  "e.nationality AS nationality", "e.merged_from AS merged_from",
].join(", ");

/** Relationship projection: a map, so one statement can return edges inline. */
const GRAPH_EDGE_MAP = [
  "id: id(r)", "source: startNode(r).canonical_key", "target: endNode(r).canonical_key",
  "type: type(r)", "weight: r.weight", "confidence: r.confidence",
  "source_weight: r.source_weight", "method: r.method", "observations: r.observations",
  "evidence: r.evidence", "evidence_scores: r.evidence_scores", "verb: r.verb",
  "rule: r.rule", "source_id: r.source_id", "doc_id: r.doc_id", "run_id: r.run_id",
  "negated: r.negated", "hedged: r.hedged", "passive: r.passive",
  "first_seen: r.first_seen", "last_seen: r.last_seen",
].join(", ");

const GRAPH_DOC_FIELDS = [
  "d.doc_id AS doc_id", "d.title AS title", "d.url AS url", "d.source_id AS source_id",
  "d.published_at AS published_at", "d.fetched_at AS fetched_at",
  "d.content_hash AS content_hash", "d.entity_count AS entity_count",
  "s.name AS source_name", "s.kind AS source_kind", "s.confidence AS source_weight",
].join(", ");

/** Full-text index created by `puppetnet/graph/schema.py`. */
const GRAPH_FULLTEXT_INDEX = "puppetnet_entity_search";

function readGraphConfig(env) {
  return {
    enabled: String(env.GRAPH_API_ENABLED ?? "true").toLowerCase() !== "false",
    publicRead: String(env.GRAPH_PUBLIC_READ ?? "false").toLowerCase() === "true",
    database: String(env.NEO4J_DATABASE || "neo4j").trim() || "neo4j",
    // Both values are capped at the database budget, not at a "reasonable API
    // default". GRAPH_RATE_PER_SEC=200 in an operator's environment must not be
    // able to turn the console into a load generator against AuraDB Free.
    ratePerSec: num(env.GRAPH_RATE_PER_SEC, DEFAULTS.GRAPH_RPM / 60, 0.01, DEFAULTS.GRAPH_RPM / 60),
    burst: num(env.GRAPH_BURST, DEFAULTS.GRAPH_RPM, 1, DEFAULTS.GRAPH_RPM),
    clientRpm: DEFAULTS.GRAPH_CLIENT_RPM,
    healthCacheSeconds: num(env.GRAPH_HEALTH_CACHE_SECONDS, 60, 5, 3600),
    cacheTtlSeconds: num(env.GRAPH_CACHE_TTL_SECONDS, 60, 0, 86400),
    timeoutMs: num(env.GRAPH_TIMEOUT_MS, 20000, 1000, 60000),
    maxNodes: num(env.GRAPH_MAX_NODES, GRAPH_LIMITS.MAX_NODES, 1, 5000),
    maxDepth: num(env.GRAPH_MAX_DEPTH, GRAPH_LIMITS.MAX_DEPTH, 1, 4),
    maxHops: num(env.GRAPH_MAX_HOPS, GRAPH_LIMITS.MAX_HOPS, 1, 12),
    maxResponseBytes: num(env.GRAPH_MAX_RESPONSE_BYTES, 6 * 1024 * 1024, 1024, 25 * 1024 * 1024),
    alternatives: String(env.GRAPH_PATH_ALTERNATIVES ?? "true").toLowerCase() !== "false",
  };
}

/**
 * AuraDB publishes a bolt URI (`neo4j+s://<id>.databases.neo4j.io:7687`); the
 * transactional HTTP endpoint is the same host over TLS. Accept either shape, and
 * accept an explicit override for a self-hosted database behind a proxy.
 */
function neo4jHttpBase(env) {
  const explicit = String(env.NEO4J_HTTP_URI || env.NEO4J_HTTP_URL || "").trim();
  const raw = explicit || String(env.NEO4J_URI || "").trim();
  if (!raw) return "";
  const match = /^(neo4j(\+s|\+ssc)?|bolt(\+s|\+ssc)?|https?):\/\/([^/?#]+)/i.exec(raw);
  const scheme = match ? match[1].toLowerCase() : "";
  let authority = match ? match[4] : raw.replace(/^\/+/, "").split(/[/?#]/)[0];
  if (!authority) return "";

  const isLocal = /^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(authority);
  // Plaintext is refused for any remote host, whichever variable it came from:
  // Basic auth over http:// puts the database password on the wire. Local
  // development is the one exception, and it keeps its port — point
  // NEO4J_HTTP_URI at http://localhost:7474 and the bolt port is left alone.
  const wantsPlain = /^http:\/\//i.test(explicit || raw);
  if (wantsPlain && !isLocal) return "";
  if (!explicit && /^(neo4j|bolt)/.test(scheme)) {
    // A bolt port says nothing about where the HTTP endpoint listens: AuraDB
    // serves it on 443 at the same host. Drop the port unless the operator gave
    // an explicit NEO4J_HTTP_URI, in which case it is used verbatim above.
    authority = authority.replace(/:\d+$/, "");
  }
  return `${wantsPlain ? "http" : "https"}://${authority}`;
}

function neo4jPublicHost(env) {
  const base = neo4jHttpBase(env);
  if (!base) return "";
  try {
    return new URL(base).hostname;
  } catch (_) {
    return "";
  }
}

function graphConfigured(env) {
  return Boolean(neo4jHttpBase(env) && String(env.NEO4J_PASSWORD || "").trim());
}

function graphError(code, message, status, extra) {
  const error = new Error(message);
  error.graphError = true;
  error.code = code;
  error.status = status || 500;
  if (extra) Object.assign(error, extra);
  return error;
}

/**
 * Graph endpoints are gated separately from the relay.
 *
 * A public dashboard is a legitimate deployment (read-only graph, no secrets in
 * the browser) so `GRAPH_PUBLIC_READ=true` is supported — but it is opt-in, and
 * the default is to require a token. `GRAPH_API_TOKEN` lets the console use a
 * different secret from the harvester, so rotating the dashboard token does not
 * touch the pipeline.
 */
function graphAuthorised(request, env, gcfg) {
  const graphTokens = graphTokenList(env);
  const presented = presentedCredential(request);

  if (graphTokens.length && presented) {
    for (const token of graphTokens) {
      if (constantTimeEqual(presented, token)) return { ok: true, mode: "graph-token", token: presented };
    }
    // Fall through: a deployment that only set PROXY_AUTH_TOKEN still works.
  }
  const relay = authorised(request, env);
  if (relay.ok) return { ok: true, mode: "relay-token", token: presented || null };
  // A public dashboard stays usable with no token — and with a stale one. The
  // `token` field is what lets the rate limiter tell the two apart; an invalid
  // credential never becomes an identity.
  if (gcfg.publicRead) return { ok: true, mode: "public" };
  if (graphTokens.length && !presented) return { ok: false, reason: "Missing bearer token for the graph API" };
  return { ok: false, reason: graphTokens.length ? "Invalid graph token" : relay.reason };
}

/** Turn Neo4j's column/row envelope into plain objects. */
function rowsOf(result) {
  if (!result) return [];
  const columns = result.columns || [];
  return (result.data || []).map((item) => {
    const row = {};
    columns.forEach((name, index) => {
      row[name] = item.row ? item.row[index] : undefined;
    });
    return row;
  });
}

/**
 * Run statements against the Neo4j transactional HTTP endpoint.
 *
 * One `tx/commit` round trip for the whole batch: an auto-commit HTTP request per
 * statement would triple the latency of every panel the console renders.
 */
async function neo4jQuery(env, gcfg, statements, ctx) {
  const base = neo4jHttpBase(env);
  if (!base) {
    throw graphError("neo4j_unconfigured", "NEO4J_URI is not set on this Worker (or is a plaintext URI to a non-local host)", 503);
  }
  const password = String(env.NEO4J_PASSWORD || "");
  if (!password) throw graphError("neo4j_unconfigured", "NEO4J_PASSWORD is not set on this Worker", 503);
  const endpointProblem = validateNeo4jEndpoint(env);
  if (endpointProblem) {
    throw graphError("neo4j_unconfigured", `Neo4j endpoint rejected: ${endpointProblem}`, 503);
  }
  const user = String(env.NEO4J_USER || env.NEO4J_USER || "neo4j");

  const url = `${base}/db/${encodeURIComponent(gcfg.database)}/tx/commit`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), gcfg.timeoutMs);
  const started = nowMs();

  try {
    const response = await fetch(url, {
      method: "POST",
      // A database transaction endpoint never legitimately redirects, and
      // following one would carry the Basic credentials to the new origin.
      redirect: "error",
      signal: controller.signal,
      headers: {
        "content-type": "application/json",
        accept: "application/json",
        // Credentials are built here and go nowhere else: not into a cache key,
        // not into a log line, not into an error message.
        authorization: `Basic ${toBase64(encoder.encode(`${user}:${password}`))}`,
      },
      body: JSON.stringify({ statements }),
    });

    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch (_) {
      throw graphError("bad_gateway", `Neo4j returned a non-JSON response (HTTP ${response.status})`, 502);
    }

    if (body && Array.isArray(body.errors) && body.errors.length) {
      const first = body.errors[0];
      const status = first.code === "Neo.ClientError.Security.Unauthorized" ? 503 : 502;
      throw graphError(
        "neo4j_error",
        String(first.message || "Neo4j reported an error").slice(0, 400),
        status,
        { neo4j_code: first.code || null }
      );
    }
    if (!response.ok) throw graphError("bad_gateway", `Neo4j HTTP ${response.status}`, 502);
    return { results: (body && body.results) || [], elapsed_ms: nowMs() - started };
  } catch (err) {
    if (err && err.graphError) throw err;
    if (err && err.name === "AbortError") {
      throw graphError("gateway_timeout", `Neo4j did not answer within ${gcfg.timeoutMs} ms`, 504);
    }
    // The message can contain the URL; strip any userinfo just in case.
    const detail = String((err && err.message) || err).replace(/\/\/[^@/\s]+@/, "//***@").slice(0, 200);
    throw graphError("bad_gateway", `Could not reach Neo4j: ${detail}`, 502);
  } finally {
    clearTimeout(timer);
  }
}

/* ---- parameter shaping --------------------------------------------------- */

function intParam(value, fallback, min, max) {
  const parsed = parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function stringParam(value, max) {
  return String(value === null || value === undefined ? "" : value).trim().slice(0, max || GRAPH_LIMITS.MAX_QUERY_CHARS);
}

function listParam(value, allowlist) {
  if (!value) return [];
  return String(value)
    .split(",")
    .map((item) => item.trim())
    .filter((item) => allowlist.indexOf(item) >= 0)
    .slice(0, allowlist.length);
}

function sortable(value, fallback) {
  const name = stringParam(value, 40);
  return GRAPH_SORTABLE.indexOf(name) >= 0 ? name : fallback;
}

/**
 * Clamp an integer that is about to be interpolated into a traversal bound.
 * Cypher cannot parameterise `[*1..n]`, so the value is parsed, clamped, and
 * re-serialised — the string a caller sent is never used.
 */
function boundParam(value, fallback, max) {
  const parsed = intParam(value, fallback, 1, max);
  return String(parsed);
}

/* ---- response shaping ---------------------------------------------------- */

/** Coerce a Neo4j row into the node shape the console expects. */
function shapeNode(row) {
  if (!row || !row.key) return null;
  const aliases = Array.isArray(row.aliases) ? row.aliases.slice(0, GRAPH_LIMITS.MAX_ALIASES) : [];
  const props = {};
  ["reg_number", "company_number", "lei", "imo", "mmsi", "tail_number", "transponder", "icao24",
    "wikidata_id", "wikipedia_id", "opencorporates_url", "address_key", "shell_risk", "flag",
    "nationality", "jurisdiction", "anomaly_degree_spike", "anomaly_offshore_cluster_ratio",
    "anomaly_betweenness", "degree", "degree_prev"].forEach((name) => {
    if (row[name] !== null && row[name] !== undefined && row[name] !== "") props[name] = row[name];
  });
  return {
    key: String(row.key),
    name: String(row.name || row.key),
    entity_type: row.entity_type || inferType(row.labels) || "Unknown",
    labels: Array.isArray(row.labels) ? row.labels : [],
    jurisdiction: row.jurisdiction || props.jurisdiction || "",
    confidence: toFloat(row.confidence),
    mention_count: toFloat(row.mention_count, 0),
    degree: toFloat(row.degree, 0),
    betweenness: toFloat(row.betweenness),
    anomaly_score: toFloat(row.anomaly_score),
    degree_spike: toFloat(row.degree_spike),
    offshore_cluster_ratio: toFloat(row.offshore_cluster_ratio),
    cluster_id: row.cluster_id === null || row.cluster_id === undefined ? "" : String(row.cluster_id),
    risk_score: toFloat(row.risk_score),
    first_seen: row.first_seen || "",
    last_seen: row.last_seen || "",
    metrics_at: row.metrics_at || "",
    aliases: aliases,
    source_ids: Array.isArray(row.source_ids) ? row.source_ids : [],
    doc_ids: Array.isArray(row.doc_ids) ? row.doc_ids : [],
    merged_from: Array.isArray(row.merged_from) ? row.merged_from : [],
    match_score: toFloat(row.match_score, 0),
    props: props,
  };
}

function toFloat(value, fallback) {
  if (value === null || value === undefined || value === "") return fallback === undefined ? null : fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : (fallback === undefined ? null : fallback);
}

function inferType(labels) {
  const order = ["Person", "Organization", "Location", "Craft", "Company"];
  const set = Array.isArray(labels) ? labels : [];
  for (const label of order) if (set.indexOf(label) >= 0) return label;
  return "";
}

function shapeEdge(raw) {
  if (!raw) return null;
  const source = raw.source === null || raw.source === undefined ? "" : String(raw.source);
  const target = raw.target === null || raw.target === undefined ? "" : String(raw.target);
  if (!source || !target) return null;
  const evidence = Array.isArray(raw.evidence) ? raw.evidence.slice(0, GRAPH_LIMITS.MAX_EVIDENCE).map(String) : [];
  return {
    id: String(raw.id === null || raw.id === undefined ? `${source}:${raw.type}:${target}` : raw.id),
    source: source,
    target: target,
    type: String(raw.type || "RELATED_TO").toUpperCase(),
    weight: toFloat(raw.weight, 0),
    confidence: toFloat(raw.confidence, 0),
    source_weight: toFloat(raw.source_weight),
    method: raw.method || "",
    observations: toFloat(raw.observations, 1),
    evidence: evidence,
    evidence_scores: Array.isArray(raw.evidence_scores) ? raw.evidence_scores.map((v) => toFloat(v, 0)) : [],
    verb: raw.verb || "",
    rule: raw.rule || "",
    source_id: raw.source_id || "",
    doc_id: raw.doc_id || "",
    run_id: raw.run_id || "",
    negated: Boolean(raw.negated),
    hedged: Boolean(raw.hedged),
    passive: Boolean(raw.passive),
    first_seen: raw.first_seen || "",
    last_seen: raw.last_seen || "",
  };
}

function shapeDoc(row) {
  if (!row || !row.doc_id) return null;
  return {
    doc_id: String(row.doc_id),
    title: row.title || "(untitled document)",
    url: row.url || "",
    source_id: row.source_id || "",
    source_name: row.source_name || row.source_id || "",
    source_kind: row.source_kind || "",
    source_weight: toFloat(row.source_weight),
    published_at: row.published_at || "",
    fetched_at: row.fetched_at || "",
    content_hash: row.content_hash || "",
    entity_count: toFloat(row.entity_count),
    count: toFloat(row.count),
    confidence: toFloat(row.confidence),
    surface_forms: Array.isArray(row.surface_forms) ? row.surface_forms.slice(0, 8).map(String) : [],
  };
}

/** Strip anything that is not a plain value before it goes back to a browser. */
function trimList(list, max) {
  return Array.isArray(list) ? list.slice(0, max) : [];
}

/* ---- handlers ------------------------------------------------------------ */

async function handleGraphHealth(request, env, cfg, gcfg) {
  // Public and unauthenticated, so it must be the cheapest route in the file.
  // Before this gate one loop over `/graph/health` ran three Cypher statements
  // per request — a free database load generator with a public URL.
  // `/graph/health` is public, but a caller who presents a valid credential is
  // still a distinct analyst and gets their own minute window.
  const budget = checkClientGraphRate(request, env, cfg, validCallerToken(request, env));
  if (!budget.allowed) {
    return errorResponse(
      "graph_rate_limited",
      `Health budget exhausted: at most ${budget.limit} database-backed requests per minute`,
      429,
      { retry_after: budget.retryAfter, headers: rateHeaders(budget) },
      env
    );
  }

  const now = nowMs();
  if (graphHealthCache.payload && now - graphHealthCache.at < gcfg.healthCacheSeconds * 1000) {
    return jsonResponse(
      { ...graphHealthCache.payload, cached: true, age_ms: now - graphHealthCache.at },
      200,
      { "X-PuppetNET-Cache": "HIT", ...rateHeaders(budget) },
      env
    );
  }

  const configured = graphConfigured(env);
  let counts = null;
  let probeError = null;

  if (configured && request.method === "GET") {
    try {
      const res = await neo4jQuery(env, gcfg, [
        { statement: "MATCH (e:Entity) RETURN count(e) AS nodes" },
        { statement: "MATCH ()-[r]->() RETURN count(r) AS edges" },
        { statement: "CALL db.indexes() YIELD name, type, state RETURN name, type, state" },
      ]);
      const nodes = rowsOf(res.results[0])[0];
      const edges = rowsOf(res.results[1])[0];
      const indexes = rowsOf(res.results[2]);
      counts = {
        nodes: nodes ? toFloat(nodes.nodes, 0) : 0,
        edges: edges ? toFloat(edges.edges, 0) : 0,
        indexes: indexes.filter((index) => index.state === "ONLINE").length,
        fulltext: indexes.some((index) => index.name === GRAPH_FULLTEXT_INDEX && index.state === "ONLINE"),
        elapsed_ms: res.elapsed_ms,
      };
    } catch (err) {
      probeError = err.graphError ? { code: err.code, message: err.message } : { code: "probe_failed", message: String(err.message || err) };
    }
  }

  const payload = {
    ok: Boolean(configured && !probeError),
    worker: WORKER_NAME,
    version: WORKER_VERSION,
    time: new Date().toISOString(),
    graph: {
      api: "graph/1",
      enabled: gcfg.enabled,
      configured: configured,
      // The host only: a URI can carry credentials, and /graph/health is public.
      host: neo4jPublicHost(env),
      database: gcfg.database,
      public_read: gcfg.publicRead,
      cache_ttl_seconds: gcfg.cacheTtlSeconds,
      path_alternatives: gcfg.alternatives,
      limits: {
        max_nodes: Math.min(gcfg.maxNodes, GRAPH_LIMITS.MAX_NODES),
        default_nodes: GRAPH_LIMITS.DEFAULT_NODES,
        max_depth: Math.min(gcfg.maxDepth, GRAPH_LIMITS.MAX_DEPTH),
        max_hops: Math.min(gcfg.maxHops, GRAPH_LIMITS.MAX_HOPS),
        max_table_rows: GRAPH_LIMITS.MAX_TABLE_ROWS,
        rate_per_sec: gcfg.ratePerSec,
        burst: gcfg.burst,
        rpm: cfg.graphRpm,
        client_rpm: cfg.graphClientRpm,
      },
      counts: counts,
      error: probeError,
    },
    endpoints: ["/graph/health", "/graph/overview", "/graph/search", "/graph/node", "/graph/neighbors", "/graph/path", "/graph/table"],
  };

  graphHealthCache.at = now;
  graphHealthCache.payload = payload;
  return jsonResponse(payload, 200, { "X-PuppetNET-Cache": "MISS", ...rateHeaders(budget) }, env);
}

async function graphOverview(params, env, gcfg) {
  const limit = intParam(params.get("limit"), GRAPH_LIMITS.DEFAULT_NODES, 1, Math.min(gcfg.maxNodes, GRAPH_LIMITS.MAX_NODES));
  const metric = sortable(params.get("metric"), "anomaly_score");
  const labelFilter = listParam(params.get("type"), GRAPH_LABELS);
  const labelClause = labelFilter.length ? `WHERE any(l IN labels(e) WHERE l IN $labels) ` : "";

  const res = await neo4jQuery(env, gcfg, [
    {
      statement:
        `MATCH (e:Entity) ${labelClause}` +
        `OPTIONAL MATCH (e)-[r]-() ` +
        `WITH e, count(r) AS degree ` +
        `ORDER BY coalesce(e.${metric}, 0) DESC, e.canonical_key ASC LIMIT $limit ` +
        `RETURN ${GRAPH_NODE_FIELDS}, degree`,
      parameters: { limit: limit, labels: labelFilter },
    },
    {
      statement:
        `MATCH (e:Entity) ${labelClause}` +
        `WITH e ORDER BY coalesce(e.${metric}, 0) DESC, e.canonical_key ASC LIMIT $limit ` +
        `WITH collect(e) AS keep UNWIND keep AS a ` +
        `MATCH (a)-[r]->(b) WHERE b IN keep ` +
        `RETURN {${GRAPH_EDGE_MAP}} AS edge`,
      parameters: { limit: limit, labels: labelFilter },
    },
    { statement: "MATCH (e:Entity) RETURN count(e) AS nodes" },
    { statement: "MATCH ()-[r]->() RETURN count(r) AS edges" },
    {
      statement: "MATCH (e:Entity) WHERE e.metrics_at IS NOT NULL RETURN max(e.metrics_at) AS metrics_at",
    },
  ]);

  const nodes = rowsOf(res.results[0]).map(shapeNode).filter(Boolean);
  const edges = rowsOf(res.results[1]).map((row) => shapeEdge(row.edge)).filter(Boolean);
  const counts = rowsOf(res.results[2])[0] || {};
  const edgeCounts = rowsOf(res.results[3])[0] || {};
  const metrics = rowsOf(res.results[4])[0] || {};

  return {
    ok: true,
    subject: "overview",
    metric: metric,
    nodes: nodes,
    edges: edges,
    truncated: toFloat(counts.nodes, 0) > nodes.length,
    metrics_at: metrics.metrics_at || "",
    caps: {
      node_cap: num(env.AURA_NODE_CAP, 200000, 0, 100000000),
      edge_cap: num(env.AURA_EDGE_CAP, 400000, 0, 100000000),
      nodes: toFloat(counts.nodes, 0),
      edges: toFloat(edgeCounts.edges, 0),
    },
  };
}

/**
 * Build a Lucene query for the full-text index. Only word characters survive;
 * everything else is dropped rather than escaped, because a stray `~` or `"` in a
 * pasted entity name would otherwise be a syntax error in the index query.
 */
function fulltextQuery(raw) {
  const tokens = String(raw || "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((token) => token.length >= 2)
    .slice(0, 6);
  if (!tokens.length) return "";
  return tokens.map((token) => `${token}*`).join(" OR ");
}

async function graphSearch(params, env, gcfg) {
  const q = stringParam(params.get("q"), GRAPH_LIMITS.MAX_QUERY_CHARS);
  const limit = intParam(params.get("limit"), 12, 1, 200);
  const labelFilter = listParam(params.get("type"), GRAPH_LABELS);
  if (!q) return { ok: true, subject: "search", nodes: [], count: 0, query: q, method: "empty" };

  const labelClause = labelFilter.length ? " AND ($labels = [] OR any(l IN labels(e) WHERE l IN $labels))" : "";
  const lower = q.toLowerCase();

  // 1) Full-text index: ranked and fast, and it is why schema.py creates it.
  const ftq = fulltextQuery(q);
  if (ftq) {
    try {
      const res = await neo4jQuery(env, gcfg, [
        {
          statement:
            `CALL db.index.fulltext.queryNodes($index, $ftq) YIELD node AS e, score ` +
            `WHERE e:Entity${labelClause} ` +
            `OPTIONAL MATCH (e)-[r]-() ` +
            `WITH e, score, count(r) AS degree ` +
            `ORDER BY score DESC, coalesce(e.anomaly_score, 0) DESC LIMIT $limit ` +
            `RETURN ${GRAPH_NODE_FIELDS}, degree, score AS match_score`,
          parameters: { index: GRAPH_FULLTEXT_INDEX, ftq: ftq, limit: limit, labels: labelFilter },
        },
      ]);
      const nodes = rowsOf(res.results[0]).map(shapeNode).filter(Boolean);
      if (nodes.length) return { ok: true, subject: "search", nodes: nodes, count: nodes.length, query: q, method: "fulltext" };
    } catch (err) {
      // A missing or rebuilding index is not an error worth showing an analyst:
      // fall through to the deterministic scan.
      if (!err.graphError || err.code !== "neo4j_error") throw err;
    }
  }

  // 2) Deterministic scan: exact identifiers first, then name/alias matching.
  const res = await neo4jQuery(env, gcfg, [
    {
      statement:
        `MATCH (e:Entity) ` +
        `WITH e, CASE ` +
        `  WHEN e.canonical_key = $q THEN 100 ` +
        `  WHEN e.reg_number = $q OR e.company_number = $q OR e.lei = $q OR e.imo = $q ` +
        `    OR e.mmsi = $q OR e.tail_number = $q OR e.transponder = $q OR e.icao24 = $q ` +
        `    OR e.wikidata_id = $q OR e.wikipedia_id = $q OR e.opencorporates_url = $q THEN 95 ` +
        `  WHEN toLower(e.name) = $lower THEN 90 ` +
        `  WHEN toLower(e.name) STARTS WITH $lower THEN 70 ` +
        `  WHEN toLower(e.name) CONTAINS $lower THEN 50 ` +
        `  WHEN any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS $lower) THEN 40 ` +
        `  WHEN toLower(coalesce(e.jurisdiction, '')) = $lower THEN 12 ` +
        `  ELSE 0 END AS relevance ` +
        `WHERE relevance > 0${labelClause} ` +
        `OPTIONAL MATCH (e)-[r]-() ` +
        `WITH e, relevance, count(r) AS degree ` +
        `ORDER BY relevance DESC, coalesce(e.anomaly_score, 0) DESC, degree DESC LIMIT $limit ` +
        `RETURN ${GRAPH_NODE_FIELDS}, degree, relevance AS match_score`,
      parameters: { q: q, lower: lower, limit: limit, labels: labelFilter },
    },
  ]);
  const nodes = rowsOf(res.results[0]).map(shapeNode).filter(Boolean);
  return { ok: true, subject: "search", nodes: nodes, count: nodes.length, query: q, method: "scan" };
}

async function graphNode(params, env, gcfg) {
  const key = stringParam(params.get("key"), 200);
  if (!key) throw graphError("bad_request", "`key` is required", 400);

  const res = await neo4jQuery(env, gcfg, [
    {
      statement:
        `MATCH (e:Entity {canonical_key: $key}) ` +
        `OPTIONAL MATCH (e)-[r]-() ` +
        `WITH e, count(r) AS degree ` +
        `RETURN ${GRAPH_NODE_FIELDS}, degree`,
      parameters: { key: key },
    },
    {
      statement:
        `MATCH (e:Entity {canonical_key: $key})-[r]-(o:Entity) ` +
        `WITH e, r, o LIMIT $edgeLimit ` +
        `RETURN {${GRAPH_EDGE_MAP}} AS edge`,
      parameters: { key: key, edgeLimit: Math.min(gcfg.maxNodes, GRAPH_LIMITS.MAX_NODES) },
    },
    {
      statement:
        `MATCH (e:Entity {canonical_key: $key})-[r]-(o:Entity) ` +
        `WITH DISTINCT o LIMIT $neighbourLimit ` +
        `OPTIONAL MATCH (o)-[r2]-() ` +
        `WITH o AS e, count(r2) AS degree ` +
        `RETURN ${GRAPH_NODE_FIELDS}, degree`,
      parameters: { key: key, neighbourLimit: Math.min(gcfg.maxNodes, GRAPH_LIMITS.MAX_NODES) },
    },
    {
      statement:
        `MATCH (d:Document)-[m:MENTIONS]->(e:Entity {canonical_key: $key}) ` +
        `OPTIONAL MATCH (d)-[:FROM_SOURCE]->(s:Source) ` +
        `WITH d, s, m ORDER BY m.confidence DESC, d.published_at DESC LIMIT $citationLimit ` +
        `RETURN ${GRAPH_DOC_FIELDS}, m.count AS count, m.confidence AS confidence, ` +
        `m.surface_forms AS surface_forms, m.first_offset AS first_offset`,
      parameters: { key: key, citationLimit: GRAPH_LIMITS.MAX_CITATIONS },
    },
  ]);

  const nodeRows = rowsOf(res.results[0]).map(shapeNode).filter(Boolean);
  if (!nodeRows.length) throw graphError("not_found", `No entity with canonical_key ${key}`, 404);

  return {
    ok: true,
    subject: "node",
    node: nodeRows[0],
    edges: rowsOf(res.results[1]).map((row) => shapeEdge(row.edge)).filter(Boolean),
    nodes: rowsOf(res.results[2]).map(shapeNode).filter(Boolean),
    citations: rowsOf(res.results[3]).map(shapeDoc).filter(Boolean),
  };
}

/**
 * N-degree neighbourhood.
 *
 * The obvious Cypher — `MATCH (root)-[*1..n]-(other)` — enumerates *paths*, and
 * path counts grow exponentially with degree. On a graph with hubs (one registry
 * address shared by 400 shells) that is how a dashboard takes down a free-tier
 * database. So the walk is generated level by level: each level collects the
 * distinct nodes one hop further out, drops what was already seen, and caps the
 * frontier before expanding again. Cost is O(sum of degrees), not O(paths).
 *
 * Two details worth knowing:
 *   * An empty frontier must not kill the query — `UNWIND []` yields no rows, so
 *     each level unwraps a sentinel null and guards the match with `IS NOT NULL`.
 *     Without that a node with no neighbours would 404 instead of returning
 *     itself.
 *   * The final selection is ordered by canonical_key, so a neighbourhood that
 *     fits under the cap is identical across calls (and therefore cacheable).
 *     When the cap binds, *which* nodes survive is the planner's order, and the
 *     response admits it with `truncated: true` rather than implying completeness.
 */
function buildNeighborhoodCypher(depth, hasTypeFilter, hasWeightFilter) {
  const levels = [];
  for (let level = 0; level < depth; level += 1) {
    const conditions = [`f${level} IS NOT NULL`];
    if (hasTypeFilter) conditions.push(`type(r${level}) IN $types`);
    if (hasWeightFilter) conditions.push(`coalesce(r${level}.weight, 0) >= $minWeight`);
    levels.push(
      `UNWIND (CASE WHEN size(frontier${level}) = 0 THEN [null] ELSE frontier${level} END) AS f${level} ` +
      `OPTIONAL MATCH (f${level})-[r${level}]-(n${level}:Entity) ` +
      `WHERE ${conditions.join(" AND ")} ` +
      `WITH seen${level}, collect(DISTINCT n${level}) AS cand${level} ` +
      `WITH seen${level}, [x IN cand${level} WHERE x IS NOT NULL AND NOT x IN seen${level}] AS fresh${level} ` +
      `WITH seen${level} + fresh${level}[0..$cap] AS seen${level + 1}, ` +
      `fresh${level}[0..$cap] AS frontier${level + 1}`
    );
  }

  // `r` inside the edge map refers to the relationship being projected; the
  // induced-edge match binds it as r2, so rewrite the tokens for that scope.
  const inducedEdgeMap = GRAPH_EDGE_MAP.replace(/\br\b/g, "r2");

  return (
    `MATCH (root:Entity {canonical_key: $key}) ` +
    `WITH [root] AS seen0, [root] AS frontier0 ` +
    levels.join(" ") + " " +
    `WITH seen${depth} AS reached ` +
    `UNWIND reached AS s ` +
    `WITH s ORDER BY s.canonical_key ASC ` +
    `WITH collect(s)[0..$limit] AS keep ` +
    `UNWIND keep AS e ` +
    `OPTIONAL MATCH (e)-[rAny]-() ` +
    `WITH keep, e, count(rAny) AS degree ` +
    `OPTIONAL MATCH (e)-[r2]-(o) WHERE o IN keep ` +
    `WITH keep, e, degree, collect(DISTINCT {${inducedEdgeMap}}) AS rels ` +
    `RETURN ${GRAPH_NODE_FIELDS}, degree, rels`
  );
}

async function graphNeighbors(params, env, gcfg) {
  const key = stringParam(params.get("key"), 200);
  if (!key) throw graphError("bad_request", "`key` is required", 400);
  const maxDepth = Math.min(gcfg.maxDepth, GRAPH_LIMITS.MAX_DEPTH);
  const depth = intParam(params.get("depth"), GRAPH_LIMITS.DEFAULT_DEPTH, 1, maxDepth);
  const limit = intParam(params.get("limit"), GRAPH_LIMITS.DEFAULT_NODES, 1, Math.min(gcfg.maxNodes, GRAPH_LIMITS.MAX_NODES));
  const minWeight = Math.min(1, Math.max(0, Number(params.get("min_weight")) || 0));
  const relTypes = listParam(params.get("types"), GRAPH_REL_TYPES);

  // Relationship filters apply to every hop, so a walk never crosses a tie the
  // analyst filtered out to reach the far side of it. They govern *traversal*,
  // not *display*: the induced edge set returned below contains every tie between
  // in-scope nodes, including weaker ones that were not walked. Hiding those is
  // the console's own filter panel, and keeping the two independent avoids
  // double-filtering a graph the analyst then cannot explain.
  //
  // Cypher cannot parameterise a traversal bound, so `depth` is interpolated into
  // the statement — after being parsed as an integer and clamped to [1, maxDepth]
  // by intParam above. Nothing a caller sent reaches the string.
  const statement = buildNeighborhoodCypher(depth, relTypes.length > 0, minWeight > 0);

  const res = await neo4jQuery(env, gcfg, [
    {
      statement: statement,
      // `cap` bounds each frontier, `limit` bounds the returned neighbourhood;
      // they are the same number today but stay separate knobs on purpose.
      parameters: { key: key, limit: limit, cap: limit, minWeight: minWeight, types: relTypes },
    },
  ]);

  const rows = rowsOf(res.results[0]);
  if (!rows.length) throw graphError("not_found", `No entity with canonical_key ${key}`, 404);

  const nodes = rows.map(shapeNode).filter(Boolean);

  // One statement returns nodes *and* the edges induced by them, so the console
  // never receives a tie whose endpoint it cannot name. Each edge is collected
  // once per endpoint, hence the de-duplication.
  const seen = new Set();
  const edges = [];
  rows.forEach((row) => {
    trimList(row.rels, GRAPH_LIMITS.MAX_TABLE_ROWS).forEach((raw) => {
      const edge = shapeEdge(raw);
      if (!edge || seen.has(edge.id)) return;
      seen.add(edge.id);
      edges.push(edge);
    });
  });

  return {
    ok: true,
    subject: "neighbors",
    root: key,
    depth: depth,
    nodes: nodes,
    edges: edges,
    truncated: nodes.length >= limit,
    filters: { min_weight: minWeight, types: relTypes },
  };
}

async function graphPath(params, env, gcfg) {
  const from = stringParam(params.get("from"), 200);
  const to = stringParam(params.get("to"), 200);
  if (!from || !to) throw graphError("bad_request", "`from` and `to` are required", 400);
  if (from === to) throw graphError("bad_request", "`from` and `to` must differ", 400);

  const direction = GRAPH_DIRECTIONS.indexOf(stringParam(params.get("direction"), 20)) >= 0
    ? stringParam(params.get("direction"), 20) : "undirected";
  const cost = GRAPH_COSTS.indexOf(stringParam(params.get("cost"), 24)) >= 0
    ? stringParam(params.get("cost"), 24) : "hops";
  const maxHops = boundParam(params.get("max_hops"), GRAPH_LIMITS.DEFAULT_HOPS, Math.min(gcfg.maxHops, GRAPH_LIMITS.MAX_HOPS));
  const left = direction === "incoming" ? "<-" : "-";
  const right = direction === "outgoing" ? "->" : "-";

  const statements = [
    {
      statement:
        `MATCH (a:Entity {canonical_key: $from}), (b:Entity {canonical_key: $to}) ` +
        `MATCH p = shortestPath((a)${left}[*1..${maxHops}]${right}(b)) ` +
        `RETURN [n IN nodes(p) | {key: n.canonical_key, name: n.name, entity_type: n.entity_type, ` +
        `labels: labels(n), jurisdiction: n.jurisdiction, cluster_id: n.cluster_id, ` +
        `anomaly_score: n.anomaly_score, betweenness: n.betweenness, confidence: n.confidence, ` +
        `mention_count: n.mention_count, risk_score: n.risk_score}] AS chain, ` +
        `[r IN relationships(p) | {${GRAPH_EDGE_MAP}}] AS rels, ` +
        `length(p) AS hops`,
      parameters: { from: from, to: to },
    },
  ];

  // Weighted costs cannot use `shortestPath`, which counts hops. Enumerating all
  // paths is exponential, so the weighted mode is bounded twice: fewer hops, and
  // a hard cap on how many paths the planner may produce before ordering.
  if (cost !== "hops") {
    const weightedHops = String(Math.min(parseInt(maxHops, 10), GRAPH_LIMITS.MAX_HOPS_WEIGHTED));
    const costExpr = cost === "inverse-weight"
      ? "reduce(c = 0.0, r IN relationships(p) | c + (1.05 - coalesce(r.weight, 0)))"
      : "reduce(c = 0.0, r IN relationships(p) | c + (1.05 - coalesce(r.confidence, 0)))";
    statements.push({
      statement:
        `MATCH (a:Entity {canonical_key: $from}), (b:Entity {canonical_key: $to}) ` +
        // Cap first, then order — as two WITH clauses, each carrying its own
        // subclauses. The shape this replaced (`WITH p, cost LIMIT $enumCap`
        // followed by a *standalone* `ORDER BY cost ASC LIMIT $alts`) only parses
        // on Neo4j 5.24 or newer, where ORDER BY became a standalone clause; on
        // any earlier 5.x — which is what an Aura Free instance may well be
        // provisioned with — it is a syntax error, so every weighted handshake
        // would fail against a real database while the offline suite (which stubs
        // Neo4j and never parses Cypher) stayed green. A CALL subquery would also
        // work, but the read-only guard rejects `CALL {` outright and that guard
        // is worth more than the shorter statement.
        `MATCH p = (a)${left}[*1..${weightedHops}]${right}(b) ` +
        `WITH p LIMIT $enumCap ` +
        `WITH p, ${costExpr} AS cost ` +
        `ORDER BY cost ASC ` +
        `LIMIT $alts ` +
        `RETURN [n IN nodes(p) | {key: n.canonical_key, name: n.name, entity_type: n.entity_type, ` +
        `labels: labels(n), jurisdiction: n.jurisdiction, cluster_id: n.cluster_id, ` +
        `anomaly_score: n.anomaly_score}] AS chain, ` +
        `[r IN relationships(p) | {${GRAPH_EDGE_MAP}}] AS rels, ` +
        `length(p) AS hops, cost`,
      parameters: { from: from, to: to, enumCap: GRAPH_LIMITS.PATH_ENUMERATION_CAP, alts: 4 },
    });
  } else if (gcfg.alternatives) {
    const altHops = String(Math.min(parseInt(maxHops, 10), GRAPH_LIMITS.MAX_HOPS_WEIGHTED));
    statements.push({
      statement:
        `MATCH (a:Entity {canonical_key: $from}), (b:Entity {canonical_key: $to}) ` +
        // Same version-safety rule as the weighted branch above.
        `MATCH p = (a)${left}[*1..${altHops}]${right}(b) ` +
        `WITH p LIMIT $enumCap ` +
        `WITH p, length(p) AS hops ` +
        `ORDER BY hops ASC ` +
        `LIMIT $alts ` +
        `RETURN [n IN nodes(p) | {key: n.canonical_key, name: n.name, entity_type: n.entity_type, ` +
        `labels: labels(n), jurisdiction: n.jurisdiction, cluster_id: n.cluster_id, ` +
        `anomaly_score: n.anomaly_score}] AS chain, ` +
        `[r IN relationships(p) | {${GRAPH_EDGE_MAP}}] AS rels, hops, hops AS cost`,
      parameters: { from: from, to: to, enumCap: GRAPH_LIMITS.PATH_ENUMERATION_CAP, alts: 4 },
    });
  }

  const res = await neo4jQuery(env, gcfg, statements);
  const primary = rowsOf(res.results[0])[0];

  const shape = (row) => {
    if (!row || !Array.isArray(row.chain) || !row.chain.length) return null;
    return {
      hops: toFloat(row.hops, (row.chain.length || 1) - 1),
      cost: toFloat(row.cost, toFloat(row.hops, 0)),
      nodes: row.chain.map((entry) => shapeNode({
        key: entry.key, name: entry.name, entity_type: entry.entity_type, labels: entry.labels,
        jurisdiction: entry.jurisdiction, cluster_id: entry.cluster_id, anomaly_score: entry.anomaly_score,
        betweenness: entry.betweenness, confidence: entry.confidence, mention_count: entry.mention_count,
        risk_score: entry.risk_score,
      })).filter(Boolean),
      edges: (row.rels || []).map(shapeEdge).filter(Boolean),
    };
  };

  const found = shape(primary);
  if (!found) {
    return {
      ok: true, subject: "path", found: false, reason: "unreachable",
      from: from, to: to, max_hops: parseInt(maxHops, 10), direction: direction, cost: cost,
      hops: 0, nodes: [], edges: [], alternatives: [],
    };
  }

  const primarySignature = found.edges.map((edge) => edge.id).sort().join("|");
  const alternatives = statements.length > 1
    ? rowsOf(res.results[1]).map(shape).filter(Boolean).filter((candidate) => {
      const signature = candidate.edges.map((edge) => edge.id).sort().join("|");
      return signature && signature !== primarySignature;
    }).slice(0, 3)
    : [];

  return {
    ok: true,
    subject: "path",
    found: true,
    from: from,
    to: to,
    direction: direction,
    cost_function: cost,
    cost: found.cost,
    hops: found.hops,
    max_hops: parseInt(maxHops, 10),
    nodes: found.nodes,
    nodeKeys: found.nodes.map((node) => node.key),
    edges: found.edges,
    minWeight: found.edges.length ? Math.min(...found.edges.map((edge) => edge.weight)) : null,
    meanWeight: found.edges.length ? found.edges.reduce((sum, edge) => sum + edge.weight, 0) / found.edges.length : null,
    meanConfidence: found.edges.length ? found.edges.reduce((sum, edge) => sum + edge.confidence, 0) / found.edges.length : null,
    bounded: cost !== "hops",
    alternatives: alternatives,
  };
}

async function graphTable(params, env, gcfg) {
  const subject = ["nodes", "edges", "sources"].indexOf(stringParam(params.get("subject"), 12)) >= 0
    ? stringParam(params.get("subject"), 12) : "nodes";
  const limit = intParam(params.get("limit"), GRAPH_LIMITS.DEFAULT_TABLE_ROWS, 1, GRAPH_LIMITS.MAX_TABLE_ROWS);
  const skip = intParam(params.get("skip"), 0, 0, 1000000);
  const q = stringParam(params.get("q"), GRAPH_LIMITS.MAX_QUERY_CHARS).toLowerCase();
  const labelFilter = listParam(params.get("type"), GRAPH_LABELS);
  const relFilter = listParam(params.get("types"), GRAPH_REL_TYPES);
  const minWeight = Math.min(1, Math.max(0, Number(params.get("min_weight")) || 0));
  const minConfidence = Math.min(1, Math.max(0, Number(params.get("min_confidence")) || 0));
  const sort = sortable(params.get("sort"), subject === "edges" ? "weight" : "anomaly_score");
  const direction = String(params.get("order") || "desc").toLowerCase() === "asc" ? "ASC" : "DESC";

  if (subject === "edges") {
    const where = [
      "startNode(r):Entity", "endNode(r):Entity",
      "coalesce(r.weight, 0) >= $minWeight", "coalesce(r.confidence, 0) >= $minConfidence",
    ];
    if (relFilter.length) where.push("type(r) IN $types");
    if (q) where.push("(toLower(type(r)) CONTAINS $q OR toLower(coalesce(startNode(r).name,'')) CONTAINS $q OR toLower(coalesce(endNode(r).name,'')) CONTAINS $q OR toLower(coalesce(r.source_id,'')) CONTAINS $q)");
    const whereClause = `WHERE ${where.join(" AND ")}`;
    const res = await neo4jQuery(env, gcfg, [
      {
        statement: `MATCH ()-[r]->() ${whereClause} WITH r ORDER BY coalesce(r.${sort === "name" ? "confidence" : sort}, 0) ${direction}, id(r) ASC SKIP $skip LIMIT $limit RETURN {${GRAPH_EDGE_MAP}} AS edge`,
        parameters: { minWeight, minConfidence, types: relFilter, q, skip, limit },
      },
      {
        statement: `MATCH ()-[r]->() ${whereClause} RETURN count(r) AS total`,
        parameters: { minWeight, minConfidence, types: relFilter, q },
      },
    ]);
    const rows = rowsOf(res.results[0]).map((row) => shapeEdge(row.edge)).filter(Boolean);
    const total = toFloat((rowsOf(res.results[1])[0] || {}).total, rows.length);
    return { ok: true, subject: "edges", rows: rows, total: total, limit: limit, skip: skip, sort: sort, order: direction };
  }

  if (subject === "sources") {
    const where = [];
    if (q) where.push("(toLower(coalesce(d.title,'')) CONTAINS $q OR toLower(coalesce(d.source_id,'')) CONTAINS $q OR toLower(coalesce(d.doc_id,'')) CONTAINS $q)");
    const whereClause = where.length ? `WHERE ${where.join(" AND ")} ` : "";
    const sortKey = GRAPH_SORTABLE.indexOf(sort) >= 0 && ["published_at", "fetched_at", "title", "doc_id"].indexOf(sort) >= 0 ? sort : "published_at";
    const res = await neo4jQuery(env, gcfg, [
      {
        statement:
          `MATCH (d:Document) ${whereClause} ` +
          `OPTIONAL MATCH (d)-[:FROM_SOURCE]->(s:Source) ` +
          `OPTIONAL MATCH (d)-[m:MENTIONS]->(e:Entity) ` +
          `WITH d, s, count(m) AS entity_count ` +
          `ORDER BY coalesce(d.${sortKey}, '') ${direction} SKIP $skip LIMIT $limit ` +
          `RETURN ${GRAPH_DOC_FIELDS}, entity_count`,
        parameters: { q, skip, limit },
      },
      { statement: `MATCH (d:Document) ${whereClause} RETURN count(d) AS total`, parameters: { q } },
    ]);
    const rows = rowsOf(res.results[0]).map(shapeDoc).filter(Boolean);
    const total = toFloat((rowsOf(res.results[1])[0] || {}).total, rows.length);
    return { ok: true, subject: "sources", rows: rows, total: total, limit: limit, skip: skip, sort: sortKey, order: direction };
  }

  // `MATCH (e:Entity)` already constrains the label, so `where` starts empty and
  // a WHERE clause is only emitted when something actually filters.
  const where = [];
  if (labelFilter.length) where.push("any(l IN labels(e) WHERE l IN $labels)");
  if (minConfidence > 0) where.push("coalesce(e.confidence, 0) >= $minConfidence");
  if (q) {
    where.push(
      "(toLower(coalesce(e.name,'')) CONTAINS $q OR toLower(coalesce(e.canonical_key,'')) CONTAINS $q " +
      "OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS $q) " +
      "OR toLower(coalesce(e.reg_number,'')) = $q OR toLower(coalesce(e.imo,'')) = $q " +
      "OR toLower(coalesce(e.mmsi,'')) = $q OR toLower(coalesce(e.tail_number,'')) = $q " +
      "OR toLower(coalesce(e.transponder,'')) = $q OR toLower(coalesce(e.lei,'')) = $q)"
    );
  }
  const whereClause = where.length ? `WHERE ${where.join(" AND ")} ` : "";
  const sortKey = sort === "name" ? "name" : sort;
  const res = await neo4jQuery(env, gcfg, [
    {
      statement:
        `MATCH (e:Entity) ${whereClause} ` +
        `OPTIONAL MATCH (e)-[r]-() ` +
        `WITH e, count(r) AS degree ` +
        `ORDER BY coalesce(e.${sortKey}, ${sortKey === "name" ? "''" : "0"}) ${direction}, e.canonical_key ASC ` +
        `SKIP $skip LIMIT $limit ` +
        `RETURN ${GRAPH_NODE_FIELDS}, degree`,
      parameters: { labels: labelFilter, minConfidence, q, skip, limit },
    },
    {
      statement: `MATCH (e:Entity) ${whereClause} RETURN count(e) AS total`,
      parameters: { labels: labelFilter, minConfidence, q },
    },
  ]);
  const rows = rowsOf(res.results[0]).map(shapeNode).filter(Boolean);
  const total = toFloat((rowsOf(res.results[1])[0] || {}).total, rows.length);
  return { ok: true, subject: "nodes", rows: rows, total: total, limit: limit, skip: skip, sort: sortKey, order: direction };
}

/**
 * Dispatcher for `/graph/*`: auth → global RPM → per-host bucket → KV cache →
 * Neo4j. The order matters; an unauthenticated caller must cost the database
 * nothing, and a cache hit must not consume a token.
 */
async function handleGraph(request, env, cfg, gcfg, ctx, path, url) {
  if (!gcfg.enabled) {
    return errorResponse("graph_disabled", "The graph API is disabled on this Worker (GRAPH_API_ENABLED=false)", 403, {}, env);
  }
  if (request.method !== "GET") {
    return errorResponse("method_not_allowed", "The graph API is read-only and only accepts GET", 405, { allow: "GET" }, env);
  }

  const lockSeconds = authLockSeconds(request, env, cfg);
  if (lockSeconds > 0) {
    return errorResponse("auth_locked", "Too many failed authentication attempts", 429, { retry_after: lockSeconds }, env);
  }
  const auth = graphAuthorised(request, env, gcfg);
  if (!auth.ok) {
    recordAuthFailure(request, env, cfg);
    return errorResponse("unauthorised", auth.reason, 401, { headers: { "WWW-Authenticate": 'Bearer realm="puppetnet-graph"' } }, env);
  }
  clearAuthFailures(request, env);

  const route = path.replace(/^\/graph\/?/, "") || "";
  const handlers = {
    overview: graphOverview,
    search: graphSearch,
    node: graphNode,
    neighbors: graphNeighbors,
    path: graphPath,
    table: graphTable,
  };
  const handler = handlers[route];
  if (!handler) {
    return errorResponse("not_found", `Unknown graph route ${path}. Try /graph/health for the list.`, 404, {}, env);
  }

  const globalCheck = checkGlobalRate(cfg);
  if (!globalCheck.allowed) {
    return errorResponse("global_rate_limited", "Worker-wide request budget exhausted for this minute", 429, { retry_after: globalCheck.retryAfter }, env);
  }

  // The database budget: 25/client/minute. This is checked before the cache on
  // purpose — a refresh loop must not be able to serve itself free responses
  // and then burst the database the moment an entry expires.
  const clientBudget = checkClientGraphRate(request, env, cfg, auth.token || null);
  if (!clientBudget.allowed) {
    return errorResponse(
      "graph_rate_limited",
      `Graph budget exhausted: at most ${clientBudget.limit} database-backed requests per minute`,
      429,
      { retry_after: clientBudget.retryAfter, limit: clientBudget.limit, headers: rateHeaders(clientBudget) },
      env
    );
  }

  const params = url.searchParams;
  const cacheKey = await cacheKeyFor(`graph/${route}?${params.toString()}`, "GET", { db: gcfg.database, v: WORKER_VERSION });
  if (gcfg.cacheTtlSeconds > 0) {
    const hit = await readCache(env, cacheKey);
    if (hit) {
      // took_ms describes the work behind *this* response; re-serving the number
      // measured when the entry was written reports a query time nobody just paid.
      return jsonResponse({ ...hit.result, cached: true, took_ms: 0, auth_mode: auth.mode }, 200, { "X-PuppetNET-Cache": "HIT", ...rateHeaders(clientBudget) }, env);
    }
  }

  // Reuse the relay's bucket machinery, but not its ceilings: acquireHostToken
  // clamps a requested rate to 10x HOST_RATE_PER_SEC and a burst to
  // MAX_HOST_BURST, which is the right protection when the target is somebody
  // else's API. The graph bucket guards *our own* database and has its own knobs,
  // so widen the clamp to them — otherwise GRAPH_RATE_PER_SEC above 5 would be
  // silently ignored and the console would 429 under ordinary use.
  const budget = await acquireHostToken(env, {
    ...cfg,
    ctx,
    hostRatePerSec: gcfg.ratePerSec,
    hostBurst: gcfg.burst,
    minHostRatePerSec: Math.min(cfg.minHostRatePerSec, gcfg.ratePerSec),
    maxHostBurst: Math.max(cfg.maxHostBurst, gcfg.burst),
  }, GRAPH_RATE_HOST, 1, {
    rate_per_sec: gcfg.ratePerSec,
    burst: gcfg.burst,
    kv_sync_interval_ms: cfg.graphKvSyncIntervalMs,
  });
  if (!budget.allowed) {
    return errorResponse("graph_rate_limited", "Graph query budget exhausted — the free-tier database is protected from bursts", 429, {
      retry_after: Math.max(1, budget.retryAfter),
      policy: budget.policy,
    }, env);
  }

  const started = nowMs();
  try {
    const payload = await handler(params, env, gcfg);
    const elapsed = nowMs() - started;
    const body = { ...payload, took_ms: elapsed, auth_mode: auth.mode, source: "worker", api: "graph/1" };

    const serialised = JSON.stringify(body);
    if (serialised.length > gcfg.maxResponseBytes) {
      return errorResponse("response_too_large",
        `Result is ${(serialised.length / 1024).toFixed(0)} KiB, over the ${Math.round(gcfg.maxResponseBytes / 1024)} KiB graph budget — lower the limit`,
        502, { bytes: serialised.length }, env);
    }

    if (gcfg.cacheTtlSeconds > 0 && payload.ok && ctx && typeof ctx.waitUntil === "function") {
      ctx.waitUntil(writeCache(env, cacheKey, body, gcfg.cacheTtlSeconds, gcfg.maxResponseBytes));
    }
    return jsonResponse(body, 200, {
      "X-PuppetNET-Cache": "MISS",
      "X-PuppetNET-Took-Ms": String(elapsed),
      "X-PuppetNET-Ratelimit-Remaining": String(Math.floor(budget.remaining)),
      ...rateHeaders(clientBudget),
    }, env);
  } catch (err) {
    if (err && err.graphError) {
      const status = err.status || 502;
      console.warn(`[graph] ${route} failed: ${err.code} ${err.message}`);
      return errorResponse(err.code, err.message, status, { took_ms: nowMs() - started, neo4j_code: err.neo4j_code || undefined }, env);
    }
    throw err;
  }
}

/* -------------------------------------------------------------------------- */
/*  Free-tier budget, distributed locks and cron jobs                         */
/* -------------------------------------------------------------------------- */

/**
 * Published Free-plan limits, kept next to the code that spends them.
 * Sources: Cloudflare Workers limits (100k requests/day, 5 cron triggers per
 * account), Workers KV limits (100k reads, 1k writes/deletes per day), and
 * Neo4j AuraDB Free quotas (200k nodes, 400k relationships, auto-pause after
 * 72 hours without a *write*).
 */
const FREE_TIER = Object.freeze({
  WORKERS_REQUESTS_PER_DAY: 100_000,
  KV_READS_PER_DAY: 100_000,
  KV_WRITES_PER_DAY: 1_000,
  AURA_NODES: 200_000,
  AURA_RELATIONSHIPS: 400_000,
  AURA_PAUSE_HOURS: 72,
});

/**
 * Keep-alive is deliberately pinned to the every-12-hours expression
 * (`0 0,12 * * *`; wrangler.toml carries the equivalent step form).
 *
 * This is the state-free answer to the 72-hour AuraDB pause. The expression
 * *is* the whole schedule: there is no "next run" record that can be lost or
 * corrupt, and `runKeepAlive` has no branch that can skip the write. Given that
 * Cloudflare fires the trigger, the heartbeat is guaranteed — the job cannot
 * silently decide to do nothing. Twelve hours leaves a six-fold margin, so six
 * consecutive trigger failures would be needed before the pause window closes;
 * the daily Python `RETURN 1` adds an independent second heartbeat.
 *
 * The 100 % claim is scoped to the application logic on purpose: no in-Worker
 * design can survive Cloudflare not executing the cron or the account's
 * 100 000-request daily quota being exhausted. Those are platform
 * dependencies, not schedule state, and the 12-hour cadence absorbs them.
 *
 * The obvious "every 36 hours" schedule is not expressible in a five-field
 * cron: the hour field only runs 0-23, so a 36-hour step selects hour 0 and
 * silently means "daily" (some parsers reject it outright). Twelve hours is
 * the closest valid expression that preserves the intent with real margin, and
 * it costs two cron invocations per day out of the account's five.
 */
// wrangler.toml: crons = ["0 */12 * * *", "23 */3 * * *"] — this one is the heartbeat.
const CRON_KEEPALIVE = "0 */12 * * *";
const CRON_HEALTH = "23 */3 * * *";

const HEALTH_SNAPSHOT_KEY = "health:snapshot";
const HEALTH_KEEPALIVE_KEY = "health:keepalive";
const HEALTH_LEDGER_PREFIX = "health:daily:";

// Telegram alerting is free (the Bot API has no paid tier) and needs no extra
// infrastructure. One `alert:last_sent` key in KV is the whole throttle: it
// costs a read per health run and a write only when a message is actually sent.
const TELEGRAM_ALERT_KEY = "alert:last_sent";
const TELEGRAM_ALERT_TTL_SECONDS = 6 * 3600;
const TELEGRAM_TIMEOUT_MS = 5000;
let localAlertSentAt = 0;

const localLocks = new Map();

/** KV helpers that never turn a quota error into a request failure. */
async function readKvJson(env, key) {
  if (!env.RATE_LIMIT_KV) return null;
  try {
    return await env.RATE_LIMIT_KV.get(key, "json");
  } catch (err) {
    console.warn(`[kv] read ${key} failed: ${err && err.message}`);
    return null;
  }
}

async function writeKvJson(env, key, value, ttlSeconds) {
  if (!env.RATE_LIMIT_KV) return false;
  try {
    await env.RATE_LIMIT_KV.put(key, JSON.stringify(value), {
      expirationTtl: Math.max(60, Math.ceil(ttlSeconds || 86400)),
    });
    return true;
  } catch (err) {
    console.warn(`[kv] write ${key} failed: ${err && err.message}`);
    return false;
  }
}

/**
 * A best-effort distributed lock in Workers KV.
 *
 * KV is an eventually consistent, last-write-wins store: it cannot provide a
 * linearizable mutex, and this function does not pretend otherwise. Every read
 * and write is one KV operation, and another colo may still hold a stale view
 * for up to a minute. What it *does* provide is the property the cron jobs
 * need: a long-lived lease that stops a retried or duplicated invocation from
 * running a database probe twice, with an expiry so a crashed run cannot wedge
 * the schedule. Jobs under the lock are idempotent (`MERGE` heartbeat,
 * snapshot overwrite) precisely because the lock is advisory.
 *
 * Without a KV binding the lock degrades to isolate memory, which is exact
 * within that isolate and useless across them — reported as `backend: memory`.
 */
async function acquireLock(env, name, ttlSeconds, meta = {}) {
  const key = `lock:${name}`;
  const now = nowMs();
  const until = now + ttlSeconds * 1000;
  const owner = (crypto.randomUUID ? crypto.randomUUID() : `${now}-${Math.random().toString(36).slice(2)}`);

  if (!env.RATE_LIMIT_KV) {
    const held = localLocks.get(key);
    if (held && held.until > now) {
      return { acquired: false, backend: "memory", owner: held.owner, until: held.until, remaining_ms: held.until - now };
    }
    localLocks.set(key, { owner, until });
    return { acquired: true, backend: "memory", owner, until };
  }

  let existing = null;
  try {
    existing = await env.RATE_LIMIT_KV.get(key, "json");
  } catch (err) {
    console.warn(`[lock] read ${name} failed: ${err && err.message}`);
  }
  if (existing && Number(existing.until) > now) {
    return { acquired: false, backend: "kv", owner: existing.owner, until: existing.until, remaining_ms: existing.until - now };
  }
  const record = { owner, until, acquired_at: now, ttl_seconds: ttlSeconds, ...meta };
  try {
    await env.RATE_LIMIT_KV.put(key, JSON.stringify(record), { expirationTtl: Math.max(60, Math.ceil(ttlSeconds)) });
  } catch (err) {
    // A lock we cannot publish is not a distributed lock. The caller may still
    // run its idempotent job; the degradation is reported, not hidden.
    localLocks.set(key, { owner, until });
    return { acquired: true, backend: "memory", owner, until, degraded: String((err && err.message) || err) };
  }
  localLocks.set(key, { owner, until });
  return { acquired: true, backend: "kv", owner, until, advisory: true };
}

async function releaseLock(env, name, owner) {
  const key = `lock:${name}`;
  const held = localLocks.get(key);
  if (held && held.owner === owner) localLocks.delete(key);
  if (!env.RATE_LIMIT_KV) return false;
  try {
    const current = await env.RATE_LIMIT_KV.get(key, "json");
    if (current && current.owner === owner) {
      await env.RATE_LIMIT_KV.delete(key);
      return true;
    }
  } catch (err) {
    console.warn(`[lock] release ${name} failed: ${err && err.message}`);
  }
  return false;
}

/** UTC day key for the daily ledger; quota resets at 00:00 UTC. */
function dayKey(now = nowMs()) {
  return new Date(now).toISOString().slice(0, 10);
}

/**
 * Cloudflare's own request counter for this script, via the GraphQL Analytics
 * API. Optional: the token needs Account Analytics:Read. Without it the
 * dashboard reports "n/a" instead of inventing a number from one isolate.
 */
async function probeWorkersUsage(env) {
  const account = String(env.CF_ACCOUNT_ID || "").trim();
  const token = String(env.CF_API_TOKEN || "").trim();
  if (!account || !token) {
    return { available: false, source: "not_configured", requests_today: null };
  }
  const since = new Date();
  since.setUTCHours(0, 0, 0, 0);
  const query = `query Usage($account: String!, $script: String!, $since: Time!, $until: Time!) {
    viewer {
      accounts(filter: { accountTag: $account }) {
        workersInvocationsAdaptive(limit: 100, filter: {
          scriptName: $script, datetime_geq: $since, datetime_leq: $until
        }) {
          sum { requests errors subrequests }
        }
      }
    }
  }`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("https://api.cloudflare.com/client/v4/graphql", {
      method: "POST",
      // Fixed host, Bearer token: never let a redirect move it somewhere else.
      redirect: "error",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({
        query,
        variables: {
          account,
          script: String(env.CF_WORKER_SCRIPT_NAME || WORKER_NAME),
          since: since.toISOString(),
          until: new Date().toISOString(),
        },
      }),
      signal: controller.signal,
    });
    const body = await response.json().catch(() => null);
    if (!response.ok || !body || (body.errors && body.errors.length)) {
      return { available: false, source: "graphql_error", requests_today: null, status: response.status };
    }
    const rows = body?.data?.viewer?.accounts?.[0]?.workersInvocationsAdaptive || [];
    const requestsToday = rows.reduce((sum, row) => sum + Number((row.sum && row.sum.requests) || 0), 0);
    const errors = rows.reduce((sum, row) => sum + Number((row.sum && row.sum.errors) || 0), 0);
    return { available: true, source: "cloudflare_graphql", requests_today: requestsToday, errors_today: errors };
  } catch (err) {
    return {
      available: false,
      source: "unreachable",
      requests_today: null,
      error: String((err && err.message) || err).slice(0, 200),
    };
  } finally {
    clearTimeout(timer);
  }
}

/** Counts for the dashboard: one node scan and one relationship scan. */
async function probeAura(env, gcfg) {
  if (!graphConfigured(env)) {
    return { available: false, configured: false, error: "NEO4J_URI/NEO4J_PASSWORD not configured" };
  }
  const started = nowMs();
  try {
    const res = await neo4jQuery(env, gcfg, [
      { statement: "MATCH (n) RETURN count(n) AS nodes" },
      { statement: "MATCH ()-[r]->() RETURN count(r) AS relationships" },
    ]);
    const nodeRow = rowsOf(res.results[0])[0] || {};
    const relRow = rowsOf(res.results[1])[0] || {};
    return {
      available: true,
      configured: true,
      nodes: toFloat(nodeRow.nodes, 0),
      relationships: toFloat(relRow.relationships, 0),
      latency_ms: nowMs() - started,
      database: gcfg.database,
      host: neo4jPublicHost(env),
    };
  } catch (err) {
    return {
      available: false,
      configured: true,
      error: err && err.code ? err.code : "probe_failed",
      message: String((err && err.message) || err).slice(0, 240),
      latency_ms: nowMs() - started,
      database: gcfg.database,
      host: neo4jPublicHost(env),
    };
  }
}

/**
 * Degradation that deserves a human, as opposed to data the deployment never
 * configured. The Workers-usage card is optional (`CF_API_TOKEN`), and the
 * keep-alive note is expected between the first deploy and the next 00:00/12:00
 * cron; alerting on either would train the operator to ignore the bot.
 */
const IGNORED_ALERT_DEGRADED = [
  /^Workers-Nutzung nicht verfügbar \(not_configured\)\.$/,
  /^Noch kein Keep-Alive-Lauf aufgezeichnet\.$/,
];

function healthAlertReason(snapshot) {
  if (!snapshot) return null;
  if (snapshot.kv && snapshot.kv.write_failed) return "KV-Schreibfehler: health:snapshot konnte nicht gespeichert werden";
  if (snapshot.aura && snapshot.aura.available === false) {
    const code = snapshot.aura.error || (snapshot.aura.configured === false ? "neo4j_unconfigured" : "neo4j_error");
    return `AuraDB nicht erreichbar (${code})`;
  }
  if (!snapshot.ok) return "Snapshot nicht betriebsbereit";
  const relevant = (snapshot.degraded || []).filter((reason) => !IGNORED_ALERT_DEGRADED.some((pattern) => pattern.test(reason)));
  if (relevant.length) return relevant.slice(0, 3).join(" ");
  return null;
}

function telegramAlertText(snapshot, reason) {
  const aura = snapshot.aura || {};
  const workers = snapshot.workers || {};
  const line = (label, value) => `${label}: ${value}`;
  const text = [
    `⚠️ PuppetNET Edge — ${snapshot.ok === false ? "Störung" : "eingeschränkt"}`,
    line("Grund", reason),
    aura.available
      ? line("AuraDB", `${formatNumber(aura.nodes)} / ${formatNumber(aura.node_limit)} Knoten · ${formatNumber(aura.relationships)} / ${formatNumber(aura.relationship_limit)} Relationships`)
      : line("AuraDB", `nicht erreichbar (${aura.error || "unbekannt"})`),
    line("Workers heute", `${workers.requests_today === null || workers.requests_today === undefined ? "n/a" : formatNumber(workers.requests_today)} / ${formatNumber(workers.limit)}`),
    line("Snapshot", snapshot.generated_at || new Date().toISOString()),
    "Details: /status (Token erforderlich)",
  ].join("\n");
  // Telegram accepts 4 096 characters; stay well clear and strip control
  // characters that a Neo4j error message could theoretically carry.
  return text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "").slice(0, 3900);
}

function redactTelegramToken(value, token) {
  let text = String(value === undefined || value === null ? "" : value);
  if (token) text = text.split(token).join("<redacted>");
  return text.replace(/bot\d+:[A-Za-z0-9_-]+/g, "bot<redacted>");
}

/**
 * Send one alert, at most once per six hours.
 *
 * The throttle key is written *before* the network call, so a Telegram timeout
 * cannot turn the next cron run into a retry. The message is plain text (no
 * parse_mode) and carries only dashboard-level numbers — never a token, a
 * Neo4j host or a password. `guardedFetch` re-validates the endpoint and is
 * called with `maxRedirects: 0`: a redirect would move a URL that contains the
 * bot token to a different origin, which is exactly what must not happen.
 */
async function sendTelegramAlert(env, cfg, snapshot, reason) {
  const token = String(env.TELEGRAM_BOT_TOKEN || "").trim();
  const chatId = String(env.TELEGRAM_CHAT_ID || "").trim();
  if (!token || !chatId) return { sent: false, skipped: "not_configured" };

  const now = nowMs();
  const stored = await readKvJson(env, TELEGRAM_ALERT_KEY);
  const lastAt = Math.max(Number(stored && stored.at) || 0, localAlertSentAt || 0);
  if (lastAt && now - lastAt < TELEGRAM_ALERT_TTL_SECONDS * 1000) {
    return {
      sent: false,
      skipped: "throttled",
      last_sent_at: new Date(lastAt).toISOString(),
      retry_in_seconds: Math.ceil((TELEGRAM_ALERT_TTL_SECONDS * 1000 - (now - lastAt)) / 1000),
    };
  }

  const record = { at: now, reason: String(reason).slice(0, 300), worker: WORKER_NAME };
  localAlertSentAt = now;
  await writeKvJson(env, TELEGRAM_ALERT_KEY, record, TELEGRAM_ALERT_TTL_SECONDS);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TELEGRAM_TIMEOUT_MS);
  try {
    const { response } = await guardedFetch(
      `https://api.telegram.org/bot${token}/sendMessage`,
      {
        method: "POST",
        signal: controller.signal,
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ chat_id: chatId, text: telegramAlertText(snapshot, reason), disable_web_page_preview: true }),
      },
      cfg,
      { maxRedirects: 0 }
    );
    const body = await response.text().catch(() => "");
    if (!response.ok) {
      return {
        sent: false,
        skipped: "telegram_error",
        status: response.status,
        detail: redactTelegramToken(body, token).slice(0, 200),
        at: new Date(now).toISOString(),
      };
    }
    return { sent: true, at: new Date(now).toISOString() };
  } catch (err) {
    return {
      sent: false,
      skipped: "send_failed",
      error: redactTelegramToken(err && err.message ? err.message : err, token).slice(0, 200),
      at: new Date(now).toISOString(),
    };
  } finally {
    clearTimeout(timer);
  }
}

function percentOf(used, limit) {
  if (used === null || used === undefined || !limit) return null;
  return Math.round((Number(used) / limit) * 1000) / 10;
}

/** The dashboard's data model. Everything unmeasured is null, never zero. */
function buildSnapshot({ env, cfg, aura, workers, keepalive, previous, cron, durationMs }) {
  const graphUsed = graphWindow.minute === Math.floor(nowMs() / 60000) ? graphWindow.count : 0;
  const previousStatus = previous && previous.last_run ? previous.last_run : {};
  const snapshot = {
    worker: { name: WORKER_NAME, version: WORKER_VERSION, colo: null },
    generated_at: new Date().toISOString(),
    last_run: {
      at: new Date().toISOString(),
      cron: cron || null,
      duration_ms: durationMs ?? null,
      keepalive_locked: previousStatus.keepalive_locked || null,
    },
    workers: {
      requests_today: workers.available ? workers.requests_today : null,
      limit: FREE_TIER.WORKERS_REQUESTS_PER_DAY,
      percent: workers.available ? percentOf(workers.requests_today, FREE_TIER.WORKERS_REQUESTS_PER_DAY) : null,
      source: workers.source,
      errors_today: workers.available ? workers.errors_today ?? null : null,
    },
    aura: aura && aura.available
      ? {
        available: true,
        nodes: aura.nodes,
        relationships: aura.relationships,
        node_limit: FREE_TIER.AURA_NODES,
        relationship_limit: FREE_TIER.AURA_RELATIONSHIPS,
        node_percent: percentOf(aura.nodes, FREE_TIER.AURA_NODES),
        relationship_percent: percentOf(aura.relationships, FREE_TIER.AURA_RELATIONSHIPS),
        latency_ms: aura.latency_ms,
        database: aura.database,
        host: aura.host,
      }
      : {
        available: false,
        configured: Boolean(aura && aura.configured),
        error: aura ? aura.error : "not_probed",
        message: aura ? aura.message : undefined,
        node_limit: FREE_TIER.AURA_NODES,
        relationship_limit: FREE_TIER.AURA_RELATIONSHIPS,
        host: aura ? aura.host : neo4jPublicHost(env),
      },
    rate: {
      graph_rpm: cfg.graphRpm,
      client_rpm: cfg.graphClientRpm,
      used_this_minute: graphUsed,
      remaining_this_minute: Math.max(0, cfg.graphRpm - graphUsed),
      host_buckets: localBuckets.size,
      global_rpm: cfg.globalRpm,
    },
    kv: {
      bound: Boolean(env.RATE_LIMIT_KV),
      reads_limit: FREE_TIER.KV_READS_PER_DAY,
      writes_limit: FREE_TIER.KV_WRITES_PER_DAY,
      lock_backend: env.RATE_LIMIT_KV ? "kv" : "memory",
    },
    keepalive: keepalive || null,
    ok: Boolean(aura && aura.available),
    degraded: [],
    warnings: [],
  };
  if (!env.RATE_LIMIT_KV) snapshot.degraded.push("KV nicht gebunden: Sperren und Verlaufsdaten sind nur pro Isolate gültig.");
  if (keepalive && keepalive.ok === false && !keepalive.skipped) {
    snapshot.degraded.push(`Letzter Keep-Alive fehlgeschlagen (${keepalive.error || "unbekannt"}).`);
  }
  if (!workers.available) snapshot.degraded.push(`Workers-Nutzung nicht verfügbar (${workers.source}).`);
  if (!aura || !aura.available) snapshot.degraded.push("AuraDB nicht erreichbar oder nicht konfiguriert.");
  if (snapshot.aura.node_percent !== null && snapshot.aura.node_percent >= 90) {
    snapshot.warnings.push(`AuraDB ist zu ${snapshot.aura.node_percent} % gefüllt — der 180 000-Knoten-Stopp der Pipeline greift bei 90 %.`);
  }
  return snapshot;
}

async function updateDailyLedger(env, snapshot, kind) {
  const key = `${HEALTH_LEDGER_PREFIX}${dayKey()}`;
  const existing = (await readKvJson(env, key)) || {
    date: dayKey(),
    health_runs: 0,
    keepalive_runs: 0,
    keepalive_failures: 0,
    workers_requests: null,
    aura_nodes: null,
    aura_relationships: null,
  };
  existing.updated_at = snapshot.generated_at;
  if (kind === "health") existing.health_runs += 1;
  if (kind === "keepalive") {
    existing.keepalive_runs += 1;
    if (snapshot.keepalive_ok === false) existing.keepalive_failures += 1;
  }
  if (snapshot.workers && snapshot.workers.requests_today !== null) existing.workers_requests = snapshot.workers.requests_today;
  if (snapshot.aura && snapshot.aura.available) {
    existing.aura_nodes = snapshot.aura.nodes;
    existing.aura_relationships = snapshot.aura.relationships;
  }
  await writeKvJson(env, key, existing, 14 * 86400);
  return existing;
}

/**
 * Keep-alive against AuraDB Free.
 *
 * Neo4j's Free-tier FAQ defines inactivity as *three days without a write
 * query*; read queries do not reset the timer. A `RETURN 1` heartbeat (what the
 * Python job does) therefore proves reachability but does not itself prevent the
 * pause. The cron writes one bounded node instead — a single `MERGE` on a fixed
 * id, so the graph grows by at most one node no matter how often it runs.
 */
async function runKeepAlive(env, cfg, meta = {}) {
  const gcfg = readGraphConfig(env);
  const started = nowMs();
  if (!gcfg.enabled || !graphConfigured(env)) {
    return { ok: false, skipped: "graph_not_configured", duration_ms: nowMs() - started };
  }
  const lock = await acquireLock(env, "keepalive", 600, { job: "keepalive", cron: meta.cron || null });
  if (!lock.acquired) {
    const record = {
      ok: false,
      skipped: "locked",
      holder: lock.owner,
      remaining_ms: lock.remaining_ms,
      backend: lock.backend,
      at: new Date().toISOString(),
    };
    await writeKvJson(env, HEALTH_KEEPALIVE_KEY, record, 7 * 86400);
    return record;
  }
  let result;
  try {
    const res = await neo4jQuery(env, gcfg, [{
      statement: "MERGE (h:_PuppetNetHeartbeat {id: $id}) "
        + "SET h.last_seen = datetime(), h.worker = $worker, h.version = $version "
        + "RETURN toString(h.last_seen) AS last_seen",
      parameters: {
        id: String(env.KEEPALIVE_NODE_ID || "puppetnet-edge-cron"),
        worker: WORKER_NAME,
        version: WORKER_VERSION,
      },
    }]);
    const row = rowsOf(res.results[0])[0] || {};
    result = {
      ok: true,
      last_seen: row.last_seen || new Date().toISOString(),
      latency_ms: nowMs() - started,
      lock: lock.backend,
      cron: meta.cron || null,
      at: new Date().toISOString(),
    };
  } catch (err) {
    result = {
      ok: false,
      error: err && err.code ? err.code : "keepalive_failed",
      message: String((err && err.message) || err).slice(0, 240),
      latency_ms: nowMs() - started,
      lock: lock.backend,
      cron: meta.cron || null,
      at: new Date().toISOString(),
    };
    console.error(`[cron] keep-alive failed: ${result.error}`);
  } finally {
    await releaseLock(env, "keepalive", lock.owner);
  }
  await writeKvJson(env, HEALTH_KEEPALIVE_KEY, result, 7 * 86400);
  await updateDailyLedger(env, {
    generated_at: result.at,
    workers: null,
    aura: null,
    keepalive_ok: result.ok,
  }, "keepalive");
  return result;
}

async function runHealthCheck(env, cfg, meta = {}) {
  const started = nowMs();
  const lock = await acquireLock(env, "health", 900, { job: "health", cron: meta.cron || null });
  if (!lock.acquired) {
    return { ok: false, skipped: "locked", holder: lock.owner, remaining_ms: lock.remaining_ms, backend: lock.backend };
  }
  try {
    const gcfg = readGraphConfig(env);
    const previous = await readKvJson(env, HEALTH_SNAPSHOT_KEY);
    const keepalive = (await readKvJson(env, HEALTH_KEEPALIVE_KEY))
      || (previous && previous.keepalive) || null;
    const [aura, workers] = await Promise.all([probeAura(env, gcfg), probeWorkersUsage(env)]);
    const snapshot = buildSnapshot({
      env,
      cfg,
      aura,
      workers,
      keepalive,
      previous,
      cron: meta.cron || null,
      durationMs: nowMs() - started,
    });
    snapshot.lock = { backend: lock.backend, advisory: Boolean(lock.advisory) };
    if (!snapshot.keepalive) snapshot.degraded.push("Noch kein Keep-Alive-Lauf aufgezeichnet.");

    // Alert before persisting, so the message can include the snapshot's
    // degraded reasons. `sendTelegramAlert` is a no-op without both secrets and
    // is throttled by `alert:last_sent` (6 h), so a broken probe cannot page
    // anyone more often than four times a day.
    let alertReason = healthAlertReason(snapshot);
    if (alertReason) snapshot.alert = await sendTelegramAlert(env, cfg, snapshot, alertReason);

    if (meta.persist === false) {
      // A manual live probe can be repeated; the 1 000 KV writes/day budget
      // cannot. The caller still gets fresh data, but the stored snapshot is
      // only refreshed on the interval the cron enforces.
      snapshot.persisted = false;
      return snapshot;
    }
    const persisted = await writeKvJson(env, HEALTH_SNAPSHOT_KEY, snapshot, 30 * 86400);
    if (env.RATE_LIMIT_KV && !persisted) {
      // A bound KV that refuses the write is a hard degradation of the
      // monitoring path itself: say so, and alert once if nothing has yet.
      snapshot.kv = { ...snapshot.kv, write_failed: true, error: "kv_error" };
      snapshot.ok = false;
      snapshot.degraded.push("KV-Schreibfehler: Snapshot konnte nicht gespeichert werden.");
      if (!alertReason) {
        alertReason = healthAlertReason(snapshot);
        snapshot.alert = await sendTelegramAlert(env, cfg, snapshot, alertReason);
      }
    }
    if (persisted) await updateDailyLedger(env, snapshot, "health");
    return snapshot;
  } finally {
    await releaseLock(env, "health", lock.owner);
  }
}

function cronJobName(cron) {
  const value = String(cron || "").trim();
  if (value === CRON_KEEPALIVE) return "keepalive";
  if (value === CRON_HEALTH) return "health";
  if (!value) return "manual";
  // A schedule added in the dashboard that wrangler.toml does not know about:
  // run both idempotent jobs rather than doing nothing and looking healthy.
  return "all";
}

/* -------------------------------------------------------------------------- */
/*  Free-tier health dashboard                                                */
/* -------------------------------------------------------------------------- */

const STATUS_HTML_HEADERS = Object.freeze({
  "Content-Type": "text/html; charset=utf-8",
  "Cache-Control": "no-store",
  "X-Robots-Tag": "noindex, nofollow",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  // No script on this page at all: it renders server-side and refreshes with a
  // meta tag. `default-src 'none'` would also block the inline stylesheet, so
  // style-src is the single exception, and it cannot execute anything.
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
});

function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatNumber(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "n/a";
  try {
    return new Intl.NumberFormat("de-DE").format(Number(value));
  } catch (_) {
    return String(value);
  }
}

function statusAuthorised(request, env, options = {}) {
  const allowPublic = options.allowPublic !== false;
  if (allowPublic && String(env.STATUS_PUBLIC || "false").toLowerCase() === "true") {
    return { ok: true, mode: "public" };
  }
  const tokens = [env.STATUS_TOKEN, env.GRAPH_API_TOKEN, env.GRAPH_API_TOKENS, env.PROXY_AUTH_TOKEN, env.PROXY_AUTH_TOKENS]
    .flatMap((raw) => String(raw || "").split(","))
    .map((token) => token.trim())
    .filter(Boolean);
  if (!tokens.length) {
    return { ok: false, reason: "STATUS_TOKEN (or GRAPH_API_TOKEN / PROXY_AUTH_TOKEN) is not configured" };
  }
  const header = request.headers.get("authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  const presented = (match ? match[1] : request.headers.get("x-status-token") || "").trim();
  if (!presented) return { ok: false, reason: "Missing bearer token" };
  for (const token of tokens) {
    if (constantTimeEqual(presented, token)) return { ok: true, mode: "token" };
  }
  return { ok: false, reason: "Invalid token" };
}

/**
 * Stored snapshot first, live probe only when asked.
 *
 * A dashboard refresh every 60 seconds must not become 1 440 full graph counts
 * a day. The cron writes the snapshot; this route reads it and falls back to a
 * clearly-labelled runtime view when no cron has run yet.
 */
async function statusSnapshot(env, cfg, options = {}) {
  const stored = await readKvJson(env, HEALTH_SNAPSHOT_KEY);
  const storedAt = stored && stored.generated_at ? Date.parse(stored.generated_at) : NaN;
  const ageMs = Number.isFinite(storedAt) ? nowMs() - storedAt : null;

  if (!options.live && stored && ageMs !== null && ageMs < 15 * 60_000) {
    return { ...stored, cached: true, cache_age_ms: ageMs };
  }
  if (options.live) {
    // A live probe runs two full graph scans, so it takes the same kind of
    // advisory lease as the cron. Without it, an operator holding down
    // "refresh" (or a script polling /status?live=1) turns the dashboard into
    // the very load it exists to detect. A locked probe answers from the
    // stored snapshot and says why, never with an error page.
    const lease = await acquireLock(env, "status-live", 30, { job: "status-live" });
    if (!lease.acquired) {
      if (stored) {
        return {
          ...stored,
          cached: true,
          cache_age_ms: ageMs,
          refresh: { ok: false, skipped: "locked", holder: lease.owner || null },
        };
      }
      return {
        ...buildSnapshot({
          env,
          cfg,
          aura: { available: false, configured: graphConfigured(env), error: "probe_skipped" },
          workers: { available: false, source: "not_probed", requests_today: null },
          keepalive: null,
          previous: null,
          cron: null,
          durationMs: null,
        }),
        cached: false,
        refresh: { ok: false, skipped: "locked", holder: lease.owner || null },
      };
    }
    // Persisting every live view would spend 2 KV writes per refresh (~2 880/day
    // at the 30-second lease cap, i.e. three times the Free quota). A stored
    // snapshot younger than five minutes is current enough to keep, so the
    // manual probe shows live numbers without rewriting KV.
    const snapshotIsFresh = stored && ageMs !== null && ageMs < 5 * 60_000;
    let fresh;
    try {
      fresh = await runHealthCheck(env, cfg, { cron: null, persist: !snapshotIsFresh });
    } finally {
      await releaseLock(env, "status-live", lease.owner);
    }
    if (fresh && fresh.generated_at) return { ...fresh, cached: false };
    if (stored) return { ...stored, cached: true, cache_age_ms: ageMs, refresh: fresh };
    return {
      ...buildSnapshot({
        env,
        cfg,
        aura: { available: false, configured: graphConfigured(env), error: "probe_skipped" },
        workers: { available: false, source: "not_probed", requests_today: null },
        keepalive: null,
        previous: null,
        cron: null,
        durationMs: null,
      }),
      cached: false,
      refresh: fresh,
    };
  }
  if (stored) return { ...stored, cached: true, cache_age_ms: ageMs };
  return {
    ...buildSnapshot({
      env,
      cfg,
      aura: { available: false, configured: graphConfigured(env), error: "not_probed" },
      workers: { available: false, source: "not_probed", requests_today: null },
      keepalive: null,
      previous: null,
      cron: null,
      durationMs: null,
    }),
    cached: false,
    note: "Noch kein Cron-Lauf gespeichert. Eine Live-Prüfung gibt es unter /status?live=1 (Token erforderlich).",
  };
}

function renderStatusPage(snapshot) {
  const healthy = snapshot.ok === true;
  const degraded = !healthy || (snapshot.degraded && snapshot.degraded.length > 0);
  const state = healthy && !degraded ? "ok" : healthy ? "degraded" : "down";
  const stateLabel = state === "ok" ? "Betriebsbereit" : state === "degraded" ? "Eingeschränkt" : "Gestört";

  const card = (label, used, limit, extra) => {
    const percent = used !== null && used !== undefined && limit ? Math.min(100, (Number(used) / limit) * 100) : 0;
    const shown = used === null || used === undefined ? "n/a" : formatNumber(used);
    return `<article class="card">
      <p class="label">${escapeHtml(label)}</p>
      <p class="value">${escapeHtml(shown)}<span>${limit ? ` / ${formatNumber(limit)}` : ""}</span></p>
      <div class="bar" role="img" aria-label="${escapeHtml(`${label}: ${shown}${limit ? ` von ${formatNumber(limit)}` : ""}`)}"><span style="width:${percent.toFixed(1)}%"></span></div>
      <p class="hint">${escapeHtml(extra || "")}</p>
    </article>`;
  };

  const aura = snapshot.aura || {};
  const workers = snapshot.workers || {};
  const rate = snapshot.rate || {};
  const keepalive = snapshot.keepalive;
  const lastRun = snapshot.last_run || {};

  const degradedList = (snapshot.degraded || []).length
    ? `<ul class="warn">${snapshot.degraded.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : `<p class="hint">Alle konfigurierten Prüfungen antworten.</p>`;

  const keepaliveRow = keepalive
    ? `<tr><td>Keep-Alive</td><td class="${keepalive.ok ? "good" : "bad"}">${keepalive.ok ? "ok" : escapeHtml(keepalive.error || keepalive.skipped || "fehlgeschlagen")}</td><td>${escapeHtml(keepalive.at || "—")}</td><td>${keepalive.latency_ms !== undefined ? `${formatNumber(keepalive.latency_ms)} ms` : "—"}</td></tr>`
    : `<tr><td>Keep-Alive</td><td class="muted">noch kein Lauf</td><td>—</td><td>—</td></tr>`;

  const auraRow = aura.available
    ? `<tr><td>AuraDB</td><td class="good">erreichbar</td><td>${escapeHtml(aura.database || "neo4j")}</td><td>${formatNumber(aura.latency_ms)} ms</td></tr>`
    : `<tr><td>AuraDB</td><td class="bad">${escapeHtml(aura.configured === false ? "nicht konfiguriert" : (aura.error || "nicht erreichbar"))}</td><td>${escapeHtml(aura.host || "—")}</td><td>—</td></tr>`;

  return `<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="60">
<title>PuppetNET Edge — Free-Tier-Status</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b111c;color:#e6edf8;font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.shell{max-width:1080px;margin:0 auto;padding:40px 24px 64px}
.eyebrow{margin:0;font-size:10px;letter-spacing:.18em;color:#7f93ad;text-transform:uppercase}
h1{margin:8px 0 6px;font-size:30px;font-weight:600;letter-spacing:-.02em}
h2{font-size:15px;letter-spacing:.02em;margin:0 0 14px;color:#c8d6e8}
.lede{margin:0;color:#93a6bf;max-width:70ch}
.badge{display:inline-block;margin-top:18px;padding:5px 11px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:.04em}
.badge.ok{background:#123a31;color:#6fe3c3;border:1px solid #2c6b58}
.badge.degraded{background:#3a3315;color:#e8d07f;border:1px solid #6c5c25}
.badge.down{background:#3d1f1f;color:#ff9f92;border:1px solid #7a3a35}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0}
.card{background:linear-gradient(150deg,#121d2c,#0f1926);border:1px solid #223144;border-radius:10px;padding:18px 18px 16px}
.card .label{margin:0;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:#8498b2}
.card .value{margin:8px 0 12px;font-size:26px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.card .value span{font-size:12px;color:#7f93ad;letter-spacing:0}
.bar{height:6px;border-radius:3px;background:#1b2a3d;overflow:hidden}
.bar span{display:block;height:100%;background:linear-gradient(90deg,#3fbfa5,#7ceae0)}
.card .hint{margin:10px 0 0;font-size:11px;color:#7f93ad}
.panel{background:#101a28;border:1px solid #223144;border-radius:10px;padding:20px;margin-top:18px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:9px 6px;border-bottom:1px solid #1f2d40;vertical-align:top}
th{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#8398b3}
td.good{color:#7de3c8}
td.bad{color:#ffab9d}
td.muted{color:#8292ab}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#b9d6e6}
.warn{margin:10px 0 0;padding-left:18px;color:#e8c98a;font-size:12px}
.warn li{margin:4px 0}
.footer{margin-top:28px;display:flex;flex-wrap:wrap;gap:14px;justify-content:space-between;color:#6f829b;font-size:11px}
a{color:#7ceae0}
@media (max-width:620px){h1{font-size:24px}.shell{padding:26px 16px 48px}.card .value{font-size:22px}}
</style>
</head>
<body>
<main class="shell">
<header>
<p class="eyebrow">PuppetNET Edge · Free-Tier</p>
<h1>Status und Budget</h1>
<p class="lede">Diese Seite zählt nur, was der Worker selbst messen kann. Cloudflare-Requests stammen aus der Analytics-API, der AuraDB-Verbrauch aus einem Cron-Probe. Kein Skript, kein Tracking, kein externer Asset-Request.</p>
<span class="badge ${state}">${escapeHtml(stateLabel)}</span>
</header>

<section class="grid" aria-label="Free-Tier-Verbrauch">
${card("Workers Requests heute", workers.requests_today, workers.limit, workers.source === "cloudflare_graphql" ? "Quelle: Cloudflare Analytics API" : `Quelle: ${workers.source || "unbekannt"}`)}
${card("AuraDB Knoten", aura.available ? aura.nodes : null, aura.node_limit, aura.available ? `Datenbank ${aura.database || "neo4j"}` : (aura.message || aura.error || "nicht konfiguriert"))}
${card("AuraDB Beziehungen", aura.available ? aura.relationships : null, aura.relationship_limit, aura.available ? "Grenze: 400 000" : "ohne Live-Probe nicht verfügbar")}
${card("Graph-Anfragen diese Minute", rate.used_this_minute, rate.graph_rpm, `Isolate-Sicht · Limit ${rate.client_rpm}/Client/Minute · ${rate.remaining_this_minute} frei`)}
</section>

<section class="panel" aria-labelledby="checks-heading">
<h2 id="checks-heading">Letzte Prüfungen</h2>
<table>
<thead><tr><th scope="col">Prüfung</th><th scope="col">Ergebnis</th><th scope="col">Zeitpunkt (UTC)</th><th scope="col">Dauer</th></tr></thead>
<tbody>
${keepaliveRow}
${auraRow}
<tr><td>Snapshot</td><td>${snapshot.cached ? "gespeichert" : "frisch"}</td><td>${escapeHtml(lastRun.at || snapshot.generated_at || "—")}</td><td>${lastRun.duration_ms !== null && lastRun.duration_ms !== undefined ? `${formatNumber(lastRun.duration_ms)} ms` : "—"}</td></tr>
</tbody>
</table>
</section>

<section class="panel" aria-labelledby="budget-heading">
<h2 id="budget-heading">Hinweise</h2>
${snapshot.note ? `<p class="hint">${escapeHtml(snapshot.note)}</p>` : ""}
${snapshot.alert ? `<p class="hint">Telegram: ${snapshot.alert.sent
    ? `Alarm gesendet um ${escapeHtml(snapshot.alert.at || "")}`
    : snapshot.alert.skipped === "throttled"
      ? "Alarm unterdrückt (6-Stunden-Drosselung)"
      : snapshot.alert.skipped === "not_configured"
        ? "nicht konfiguriert"
        : `Versand fehlgeschlagen (${escapeHtml(snapshot.alert.skipped || "unbekannt")})`}</p>` : ""}
${degradedList}
<p class="hint">KV-Free-Limits: ${formatNumber(snapshot.kv ? snapshot.kv.reads_limit : null)} Reads · ${formatNumber(snapshot.kv ? snapshot.kv.writes_limit : null)} Writes pro Tag. Sperr-Backend: <code>${escapeHtml(snapshot.kv ? snapshot.kv.lock_backend : "unbekannt")}</code>. Cron: <code>${escapeHtml(CRON_KEEPALIVE)}</code> (Keep-Alive) und <code>${escapeHtml(CRON_HEALTH)}</code> (Snapshot).</p>
</section>

<footer class="footer">
<span>${escapeHtml(snapshot.worker ? snapshot.worker.name : "puppetnet-edge-relay")} @ ${escapeHtml(snapshot.worker ? snapshot.worker.version : "?")} · Stand ${escapeHtml(snapshot.generated_at || "")}</span>
<span><a href="/status.json">JSON</a> · <a href="/health">Liveness</a></span>
</footer>
</main>
</body>
</html>`;
}

async function handleStatus(request, url, env, cfg) {
  const live = url.searchParams.get("live") === "1";
  // A live probe costs two full graph scans. It is allowed for a public
  // dashboard only when the operator explicitly opted in *and* the request is
  // authenticated; `STATUS_PUBLIC` covers the read-only view, not the probe.
  const lockSeconds = authLockSeconds(request, env, cfg);
  if (lockSeconds > 0) {
    return errorResponse("auth_locked", "Too many failed attempts", 429, { retry_after: lockSeconds }, env);
  }
  const auth = statusAuthorised(request, env, { allowPublic: !live });
  if (!auth.ok) {
    // The status token is a bearer secret like any other: guessing it must be
    // as expensive as guessing the relay or graph token.
    recordAuthFailure(request, env, cfg);
    return errorResponse("unauthorised", auth.reason, 401, { headers: { "WWW-Authenticate": 'Bearer realm="puppetnet-status"' } }, env);
  }
  clearAuthFailures(request, env);
  const snapshot = await statusSnapshot(env, cfg, { live });
  if (url.pathname === "/status.json") {
    return jsonResponse({ ...snapshot, auth_mode: auth.mode }, 200, { "X-Robots-Tag": "noindex, nofollow" }, env);
  }
  return new Response(renderStatusPage(snapshot), { status: 200, headers: { ...STATUS_HTML_HEADERS, Vary: "Origin" } });
}

async function handleStatusRefresh(request, env, cfg) {
  const lockSeconds = authLockSeconds(request, env, cfg);
  if (lockSeconds > 0) {
    return errorResponse("auth_locked", "Too many failed attempts", 429, { retry_after: lockSeconds }, env);
  }
  const auth = statusAuthorised(request, env, { allowPublic: false });
  if (!auth.ok) {
    recordAuthFailure(request, env, cfg);
    return errorResponse("unauthorised", auth.reason, 401, { headers: { "WWW-Authenticate": 'Bearer realm="puppetnet-status"' } }, env);
  }
  clearAuthFailures(request, env);
  const snapshot = await statusSnapshot(env, cfg, { live: true });
  return jsonResponse({ ok: snapshot.ok !== false, ...snapshot, auth_mode: auth.mode }, 200, { "X-Robots-Tag": "noindex, nofollow" }, env);
}

/* -------------------------------------------------------------------------- */
/*  Worker entrypoints                                                        */
/* -------------------------------------------------------------------------- */

export default {
  async fetch(request, env, ctx) {
    const cfg = readConfig(env);
    const started = nowMs();
    const reqEnv = withRequestOrigin(env, request);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(reqEnv) });
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      if (path === "/" || path === "/health" || path === "/healthz") {
        return handleHealth(request, reqEnv, cfg);
      }

      // The graph read API gates itself (GRAPH_API_TOKEN / GRAPH_PUBLIC_READ),
      // so it is matched before the relay's bearer check. It is read-only and
      // holds no Neo4j credentials in the response.
      const gcfg = readGraphConfig(reqEnv);
      if (path === "/graph/health") {
        return await handleGraphHealth(request, reqEnv, cfg, gcfg);
      }
      if (path.startsWith("/graph/")) {
        return await handleGraph(request, reqEnv, cfg, gcfg, ctx, path, url);
      }

      // The status dashboard carries its own token set (STATUS_TOKEN, or the
      // graph/relay token) and is never public unless STATUS_PUBLIC=true.
      if ((path === "/status" || path === "/status.json") && request.method === "GET") {
        return await handleStatus(request, url, reqEnv, cfg);
      }
      if (path === "/status/refresh" && request.method === "POST") {
        return await handleStatusRefresh(request, reqEnv, cfg);
      }

      const lockSeconds = authLockSeconds(request, reqEnv, cfg);
      if (lockSeconds > 0) {
        return errorResponse("auth_locked", "Too many failed authentication attempts", 429, { retry_after: lockSeconds }, reqEnv);
      }
      const auth = authorised(request, reqEnv);
      if (!auth.ok) {
        recordAuthFailure(request, reqEnv, cfg);
        return errorResponse("unauthorised", auth.reason, 401, { headers: { "WWW-Authenticate": 'Bearer realm="puppetnet"' } }, reqEnv);
      }
      clearAuthFailures(request, reqEnv);

      if (path === "/stats" && request.method === "GET") return handleStats(reqEnv, cfg);
      if (path === "/cache" && request.method === "DELETE") return handleCachePurge(request, reqEnv);
      if ((path === "/fetch" && (request.method === "POST" || request.method === "GET"))) {
        return await handleFetch(request, reqEnv, cfg, ctx);
      }
      const taskMatch = /^\/tasks\/([\w.-]{1,128})$/.exec(path);
      if (taskMatch && (request.method === "GET" || request.method === "DELETE")) {
        return await handleTaskLookup(request, reqEnv, cfg, taskMatch[1]);
      }

      return errorResponse("not_found", `Unknown route ${request.method} ${path}`, 404, {}, reqEnv);
    } catch (err) {
      console.error(`[relay] unhandled error: ${err && err.stack ? err.stack : err}`);
      return errorResponse("internal_error", err && err.message ? err.message : "Unexpected relay failure", 500, { elapsed_ms: nowMs() - started }, reqEnv);
    }
  },

  /**
   * Queue consumer: executes deferred requests once their delay elapses and
   * parks the result in RESULT_KV for the client to poll.
   */
  async queue(batch, env, ctx) {
    const cfg = readConfig(env);
    for (const message of batch.messages) {
      const payload = message.body || {};
      const taskId = payload.task_id || `unknown-${message.id}`;
      try {
        const task = normaliseTask(payload.task || {}, cfg, { request_id: taskId });
        const validation = validateTargetUrl(task.url, cfg);
        if (validation.error) throw Object.assign(new Error(validation.error), { fatal: true });

        // Re-check the host budget: the queue may have released several tasks
        // for the same host at once. If still saturated, re-delay instead of
        // stampeding the origin.
        const budget = await acquireHostToken(env, { ...cfg, ctx }, validation.url.hostname, 1, task.rate_limit);
        if (!budget.allowed) {
          const delay = Math.min(Math.max(budget.retryAfter, 5), cfg.maxQueueDelaySeconds);
          if (message.attempts < DEFAULTS.MAX_QUEUE_ATTEMPTS) {
            message.retry({ delaySeconds: delay });
            continue;
          }
        }

        const result = await performFetch(task, env, cfg, payload.meta || {});
        if (env.RESULT_KV) {
          await env.RESULT_KV.put(
            `task:${taskId}`,
            JSON.stringify({
              status: result.error ? "failed" : "done",
              task_id: taskId,
              request_id: task.request_id,
              url: task.url,
              source_id: task.source_id,
              attempts_in_queue: message.attempts,
              completed_at: nowMs(),
              result,
            }),
            { expirationTtl: cfg.taskResultTtlSeconds }
          );
        }
        message.ack();
      } catch (err) {
        console.error(`[queue] task ${taskId} failed: ${err && err.message}`);
        if (err && err.fatal) {
          if (env.RESULT_KV) {
            await env.RESULT_KV.put(
              `task:${taskId}`,
              JSON.stringify({ status: "failed", task_id: taskId, error: err.message, completed_at: nowMs() }),
              { expirationTtl: cfg.taskResultTtlSeconds }
            ).catch(() => {});
          }
          message.ack(); // deterministic failure, retrying is pointless
          continue;
        }
        if (message.attempts < DEFAULTS.MAX_QUEUE_ATTEMPTS) {
          message.retry({ delaySeconds: 30 * message.attempts });
        } else {
          if (env.RESULT_KV) {
            await env.RESULT_KV.put(
              `task:${taskId}`,
              JSON.stringify({ status: "failed", task_id: taskId, error: String(err && err.message), attempts: message.attempts, completed_at: nowMs() }),
              { expirationTtl: cfg.taskResultTtlSeconds }
            ).catch(() => {});
          }
          message.ack();
          if (batch.queue) message.sendToDLQ?.();
        }
      }
    }
  },

  /**
   * Cron dispatcher. `wrangler.toml` declares two schedules:
   *
   *   `CRON_KEEPALIVE` — keep-alive (AuraDB Free pauses after 72 h without a
   *                      write; two runs a day leaves a six-fold margin)
   *   `CRON_HEALTH`    — free-tier snapshot (node/relationship counts)
   *
   * The expression is read from `event.cron`, so an operator adding a schedule
   * in the dashboard gets the idempotent pair rather than silence. Every run
   * first prunes isolate state: a long-lived isolate must not accumulate
   * per-host buckets or rate-limit windows forever.
   */
  async scheduled(event, env, ctx) {
    const cfg = readConfig(env);
    const now = nowMs();
    let pruned = 0;
    for (const [host, bucket] of localBuckets.entries()) {
      if (now - bucket.ts > 6 * 3600 * 1000) {
        localBuckets.delete(host);
        localSyncState.delete(host);
        pruned += 1;
      }
    }
    for (const [name, lock] of localLocks.entries()) {
      if (lock.until <= now) localLocks.delete(name);
    }
    globalWindow.count = 0;
    graphWindow.count = 0;
    graphWindow.minute = Math.floor(now / 60000);
    pruneCallerState(now);

    const cron = event && event.cron ? String(event.cron) : "";
    const job = cronJobName(cron);
    const started = nowMs();
    const results = {};
    if (job === "keepalive" || job === "all") {
      results.keepalive = await runKeepAlive(env, cfg, { cron });
    }
    if (job === "health" || job === "all") {
      results.health = await runHealthCheck(env, cfg, { cron });
    }
    console.log(
      `[scheduled] cron=${cron || "manual"} job=${job} pruned=${pruned} `
      + `keepalive=${results.keepalive ? (results.keepalive.ok ? "ok" : results.keepalive.skipped || results.keepalive.error || "fail") : "n/a"} `
      + `health=${results.health ? (results.health.generated_at ? "ok" : results.health.skipped || "fail") : "n/a"} `
      + `elapsed=${nowMs() - started}ms global_rpm=${cfg.globalRpm} graph_rpm=${cfg.graphRpm}`
    );
    return results;
  },
};

/* Exported for unit tests running under `node --test` / vitest. */
export const __internals = {
  parseRobots,
  guardedFetch,
  validateNeo4jEndpoint,
  withoutCredentials,
  acquireLock,
  releaseLock,
  runKeepAlive,
  runHealthCheck,
  statusSnapshot,
  statusAuthorised,
  renderStatusPage,
  buildSnapshot,
  clientFingerprint,
  checkClientGraphRate,
  resetRateLimits,
  resetGraphRateLimits,
  WORKER_VERSION,
  FREE_TIER,
  CRON_KEEPALIVE,
  CRON_HEALTH,
  isPathAllowed,
  robotPathMatches,
  pickFingerprint,
  buildOutboundHeaders,
  normaliseTask,
  validateTargetUrl,
  readConfig,
  toBase64,
  fromBase64,
  fnv1a,
  constantTimeEqual,
  isTextContentType,
  DEFAULTS,
  FINGERPRINTS,
};
