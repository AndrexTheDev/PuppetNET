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
 *   - RATE_LIMIT_KV : KV namespace  (token buckets, robots.txt cache)
 *   - RESULT_KV     : KV namespace  (response cache, queued task results)
 *   - FETCH_QUEUE   : Queue producer (deferred requests)
 *   - Secrets       : PROXY_AUTH_TOKEN / PROXY_AUTH_TOKENS
 *
 *  All bindings are optional: the Worker degrades to a stateless relay with
 *  per-isolate rate limiting when KV/Queues are not attached.
 * ============================================================================
 */

const WORKER_NAME = "puppetnet-edge-relay";
const WORKER_VERSION = "1.5.0";

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
  /** Cross-isolate KV synchronisation cadence for a bucket. */
  KV_SYNC_INTERVAL_MS: 20_000,
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

function corsHeaders(env) {
  const allowed = String(env.ALLOWED_ORIGINS || "*")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const origin = allowed.length === 1 ? allowed[0] : allowed.join(", ");
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-Id, X-Source-Id",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
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

const localBuckets = new Map(); // host -> { tokens, ts }
const localSyncState = new Map(); // host -> lastKvSyncMs
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
    const retryAfter = Math.max(1, Math.ceil((minute + 1) * 60000 - now) / 1000);
    return { allowed: false, retryAfter };
  }
  globalWindow.count += 1;
  return { allowed: true, retryAfter: 0 };
}

/**
 * Merge a KV-persisted bucket with the in-isolate bucket.
 *
 * KV is eventually consistent and each isolate keeps its own view, so we take
 * the *most conservative* of the two (fewest tokens, oldest timestamp) to keep
 * the fleet's aggregate request rate close to the configured budget. Sync is
 * throttled to one KV read + one write per host per `kvSyncIntervalMs`, which
 * keeps free-tier KV operation counts (100k reads / 1k writes per day) safe.
 */
async function syncBucketWithKv(env, cfg, host, local) {
  if (!env.RATE_LIMIT_KV) return local;
  const key = `rl:${host}`;
  const last = localSyncState.get(host) || 0;
  const now = nowMs();
  if (now - last < cfg.kvSyncIntervalMs) return local;
  localSyncState.set(host, now);

  let remote = null;
  try {
    remote = await env.RATE_LIMIT_KV.get(key, "json");
  } catch (err) {
    console.warn(`[ratelimit] KV read failed for ${host}: ${err && err.message}`);
    return local;
  }

  if (remote && Number.isFinite(remote.tokens) && Number.isFinite(remote.ts)) {
    const remoteRefilled = refill({ tokens: remote.tokens, ts: remote.ts }, remote.rate || cfg.hostRatePerSec, cfg.maxHostBurst, now);
    if (remoteRefilled.tokens < local.tokens) {
      local.tokens = remoteRefilled.tokens;
      local.ts = now;
    }
  }

  try {
    await env.RATE_LIMIT_KV.put(
      key,
      JSON.stringify({ tokens: local.tokens, ts: local.ts, rate: cfg.hostRatePerSec, burst: cfg.hostBurst, v: WORKER_VERSION }),
      { expirationTtl: Math.max(120, Math.ceil(3600 / Math.max(0.01, cfg.hostRatePerSec))) }
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
    bucket = { tokens: burst, ts: now };
    localBuckets.set(host, bucket);
  }
  refill(bucket, ratePerSec, burst, now);
  bucket = await syncBucketWithKv(env, cfg, host, bucket);
  refill(bucket, ratePerSec, burst, nowMs());

  if (bucket.tokens >= cost) {
    bucket.tokens -= cost;
    localBuckets.set(host, bucket);
    // Fire-and-forget persistence keeps the hot path fast.
    if (env.RATE_LIMIT_KV) {
      // `waitUntil` is supplied by the caller through cfg.ctx when available.
      const persist = env.RATE_LIMIT_KV
        .put(`rl:${host}`, JSON.stringify({ tokens: bucket.tokens, ts: bucket.ts, rate: ratePerSec, burst, v: WORKER_VERSION }), {
          expirationTtl: Math.max(120, Math.ceil(3600 / Math.max(0.01, ratePerSec))),
        })
        .catch((err) => console.warn(`[ratelimit] KV persist failed: ${err && err.message}`));
      if (cfg.ctx && typeof cfg.ctx.waitUntil === "function") cfg.ctx.waitUntil(persist);
    }
    return { allowed: true, remaining: bucket.tokens, retryAfter: 0, policy };
  }

  const deficit = cost - bucket.tokens;
  const retryAfter = Math.max(1, Math.ceil(deficit / ratePerSec));
  localBuckets.set(host, bucket);
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
    });
  }
  hosts.sort((a, b) => a.host.localeCompare(b.host));
  return hosts.slice(0, 500);
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
      if (current && current.rules.length === 0) {
        current.agents.push(value.toLowerCase());
      } else {
        current = { agents: [value.toLowerCase()], rules: [], crawlDelay: null };
        groups.push(current);
      }
    } else if (current) {
      if (field === "disallow" || field === "allow") {
        if (value) current.rules.push({ type: field, pattern: value });
        else if (field === "disallow") current.rules.push({ type: "allow", pattern: "/" });
      } else if (field === "crawl-delay") {
        const d = Number(value);
        if (Number.isFinite(d) && d > 0) current.crawlDelay = d;
      }
    }
  }
  return groups;
}

function robotPathMatches(pattern, path) {
  let regex = "";
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i];
    if (ch === "*") regex += ".*";
    else if (ch === "$" && i === pattern.length - 1) regex += "$";
    else regex += ch.replace(/[.+?^{}()|[\]\\]/g, "\\$&");
  }
  if (!regex.endsWith("$")) regex += "(?:$|[?#])|(?=.)";
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
    if (g.agents.some((a) => a !== "*" && ua.includes(a))) {
      group = g;
      break;
    }
  }
  if (!group) group = groups.find((g) => g.agents.includes("*")) || null;
  if (!group) return { allowed: true, crawlDelay: null };

  let best = null;
  for (const rule of group.rules) {
    if (!robotPathMatches(rule.pattern, path)) continue;
    if (!best || rule.pattern.length > best.pattern.length) best = rule;
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
      const resp = await fetch(robotsUrl, {
        headers: { "User-Agent": userAgent, Accept: "text/plain,*/*;q=0.5" },
        signal: controller.signal,
        cf: { cacheEverything: false },
      });
      clearTimeout(timer);
      if (resp.ok) {
        text = (await resp.text()).slice(0, 512 * 1024);
      } else if (resp.status === 401 || resp.status === 403) {
        // RFC 9309: a 401/403 on robots.txt means "everything disallowed".
        text = "User-agent: *\nDisallow: /\n";
      } else {
        text = ""; // 404 etc → no restrictions
      }
    } catch (err) {
      console.warn(`[robots] fetch failed for ${robotsUrl}: ${err && err.message}`);
      text = "";
      // Fail-open on transient robots errors, but never cache the miss.
      return { allowed: true, crawlDelay: null, source: "fetch-error" };
    }
    if (env.RATE_LIMIT_KV) {
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
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (!host) return { error: "Empty host" };

  for (const suffix of cfg.blockedHostSuffixes) {
    if (host === suffix || host.endsWith(`.${suffix}`)) return { error: `Host blocked by policy: ${host}` };
  }

  if (!cfg.allowPrivateNetworks) {
    if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) {
      return { error: "Private/reserved hostname blocked" };
    }
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a, b] = host.split(".").map(Number);
      if (a === 10 || a === 127 || a === 0 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 169 && b === 254)) {
        return { error: "Private/reserved IPv4 range blocked" };
      }
    }
    if (host.startsWith("::") || host === "::1" || host.toLowerCase().startsWith("fc") || host.toLowerCase().startsWith("fe80")) {
      return { error: "Private/reserved IPv6 range blocked" };
    }
  }
  return { url: parsed };
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
    if (outgoing.resolve_override) cfOptions.resolveOverride = outgoing.resolve_override;

    try {
      const upstream = await fetch(targetRaw, {
        method,
        headers,
        body: method === "GET" || method === "HEAD" ? undefined : body,
        redirect: outgoing.follow_redirects === false ? "manual" : "follow",
        signal: controller.signal,
        cf: cfOptions,
      });
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
        url: redactUrl(upstream.url || targetRaw, hostProfile),
        request_url: reportUrl,
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
    resolve_override: input.resolve_override || defaults.resolve_override,
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
      },
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
      local_buckets: rateLimitSnapshot(),
      bindings: { rate_limit_kv: Boolean(env.RATE_LIMIT_KV), result_kv: Boolean(env.RESULT_KV), queue: Boolean(env.FETCH_QUEUE) },
    },
    200,
    {},
    env
  );
}

/* -------------------------------------------------------------------------- */
/*  Worker entrypoints                                                        */
/* -------------------------------------------------------------------------- */

export default {
  async fetch(request, env, ctx) {
    const cfg = readConfig(env);
    const started = nowMs();

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      if (path === "/" || path === "/health" || path === "/healthz") {
        return handleHealth(request, env, cfg);
      }

      const auth = authorised(request, env);
      if (!auth.ok) {
        return errorResponse("unauthorised", auth.reason, 401, {}, env);
      }

      if (path === "/stats" && request.method === "GET") return handleStats(env, cfg);
      if (path === "/cache" && request.method === "DELETE") return handleCachePurge(request, env);
      if ((path === "/fetch" && (request.method === "POST" || request.method === "GET"))) {
        return await handleFetch(request, env, cfg, ctx);
      }
      const taskMatch = /^\/tasks\/([\w.-]{1,128})$/.exec(path);
      if (taskMatch && (request.method === "GET" || request.method === "DELETE")) {
        return await handleTaskLookup(request, env, cfg, taskMatch[1]);
      }

      return errorResponse("not_found", `Unknown route ${request.method} ${path}`, 404, {}, env);
    } catch (err) {
      console.error(`[relay] unhandled error: ${err && err.stack ? err.stack : err}`);
      return errorResponse("internal_error", err && err.message ? err.message : "Unexpected relay failure", 500, { elapsed_ms: nowMs() - started }, env);
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
   * Cron (e.g. "30 4 * * *"): prune empty local buckets so long-lived isolates
   * do not leak memory, and warm the KV view of the busiest hosts.
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
    globalWindow.count = 0;
    console.log(`[scheduled] pruned ${pruned} idle buckets; ${localBuckets.size} active; global rpm limit ${cfg.globalRpm}`);
  },
};

/* Exported for unit tests running under `node --test` / vitest. */
export const __internals = {
  parseRobots,
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
