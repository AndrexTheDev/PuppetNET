#!/usr/bin/env node
/**
 * audit.mjs — whole-repository static audit.
 *
 *   npm run audit            # human-readable report + docs/audit/findings.json
 *   npm run audit -- --strict  # also fail on low/info findings
 *
 * The smoke suites answer "does this feature work?". This script answers a
 * different question: "is the repository internally consistent, safe and free to
 * run?". Those are the defects that survive a green test suite — a client-side
 * cap that drifted from the Worker's, a duplicate element id, an `aria-labelledby`
 * pointing at nothing, a paid API creeping into the ingest list, a secret in a log
 * line. Every check below encodes an invariant that two modules must agree on,
 * or a hygiene rule the project has chosen.
 *
 * Findings are graded high / medium / low / info. Accepted risks are not deleted
 * from the report — they are listed in ACCEPTED with a written justification, so
 * the ledger stays complete and the decision stays visible. CI fails on high and
 * medium (and on anything accepted whose justification has gone stale).
 *
 * Offline and dependency-free by design: it reads files, it runs nothing.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STRICT = process.argv.includes("--strict");

/* ========================================================================== */
/*  Ledger                                                                     */
/* ========================================================================== */

/** Accepted risks: check id → why this is deliberate. Keep the reason specific. */
const ACCEPTED = new Map([
  ["xss/innerHTML",
    "The surviving writes are: `host.innerHTML = \"\"` (clearing a container), the el() helper's "
    + "documented `html` key, the QR SVG (constant address through the vendored generator) and three "
    + "settings/boot strings (#cfg-probe, #cfg-mode-help, #boot-status) in which every interpolation "
    + "passes escapeHtml — audited by xss/elHtml and toast/sink above, and attacked at runtime by the "
    + "hostile-text checks in tests/web_smoke.mjs."],
  ["python/assert",
    "assert is used in tests and in config self-validation that runs at load time (never with "
    + "-O in the shipped workflows)."],
  ["cost/external-host",
    "Inventory check: the ledger lists every external host with its free-tier budget. Being on "
    + "the list is not a defect; being on it without a budget note is."],
]);

const findings = [];
const counters = { high: 0, medium: 0, low: 0, info: 0, accepted: 0 };

function finding(id, dimension, severity, module, where, message, fix) {
  const accepted = ACCEPTED.has(id);
  findings.push({
    id, dimension, severity: accepted ? "accepted" : severity,
    module, where, message, fix: fix || null, accepted,
    justification: accepted ? ACCEPTED.get(id) : null,
  });
  if (accepted) counters.accepted += 1;
  else counters[severity] += 1;
}

/* ========================================================================== */
/*  Helpers                                                                    */
/* ========================================================================== */

const cache = new Map();
function read(relative) {
  if (!cache.has(relative)) {
    const full = path.join(ROOT, relative);
    cache.set(relative, existsSync(full) ? readFileSync(full, "utf8") : null);
  }
  return cache.get(relative);
}
const exists = (relative) => existsSync(path.join(ROOT, relative));

/** Walk a file line by line, reporting every regex match with file:line. */
function scan(relative, regex, onMatch) {
  const text = read(relative);
  if (text === null) return 0;
  let hits = 0;
  text.split("\n").forEach((line, index) => {
    const re = new RegExp(regex.source, regex.flags.includes("g") ? regex.flags : regex.flags + "g");
    let match;
    while ((match = re.exec(line)) !== null) {
      hits += 1;
      onMatch(match, `${relative}:${index + 1}`, line.trim());
      if (match.index === re.lastIndex) re.lastIndex += 1;
    }
  });
  return hits;
}

function walk(dir, filter, out = []) {
  for (const entry of readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    const relative = path.posix.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (/^\.git$|node_modules|\.venv|__pycache__|\.pytest_cache/.test(entry.name)) continue;
      walk(relative, filter, out);
    } else if (!filter || filter(relative)) out.push(relative);
  }
  return out;
}

/** Read width/height out of a JPEG's SOF marker — no image library needed. */
function jpegSize(buffer) {
  if (buffer[0] !== 0xff || buffer[1] !== 0xd8) return null;
  let offset = 2;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) { offset += 1; continue; }
    const marker = buffer[offset + 1];
    const length = buffer.readUInt16BE(offset + 2);
    if (marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
      return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) };
    }
    offset += 2 + length;
  }
  return null;
}

/* ========================================================================== */
/*  3. SEO completeness                                                        */
/* ========================================================================== */

function auditSeo() {
  // The v1 console's per-document head/JSON-LD audit was retired with the
  // console itself: the modern export builds every document head through the
  // Next.js Metadata API, and tests/export_smoke.mjs asserts the rendered
  // result on the shipped files. What still needs a static gate is the
  // deployment contract: the files a crawler requests first.

  // A public, indexable deployment needs these files; their absence means
  // crawlers get a 404 on /robots.txt and no sitemap to follow. The v2 static
  // export serves its documents (`/`, `/dashboard/`, profiles) from
  // `apps/web/public`; the v1 console is archived and `/legacy` is a redirect.
  const seoEmitterWired = /generate-seo\.js/.test(read("scripts/pages-build.mjs") || "");
  for (const [relative, why] of [
    ["apps/web/public/robots.txt", "crawl directives at the root of the Next static export"],
    ["apps/web/public/_redirects", "the permanent /legacy → /dashboard/ rules for Cloudflare Pages"]]) {
    if (!exists(relative)) {
      finding("seo/missingFile", "seo", "medium", relative, relative,
        `${relative} is missing — it carries ${why}`, `Add ${relative}.`);
    }
  }
  // A sitemap needs absolute URLs, and the deployment origin is not known at
  // commit time, so it is emitted at deploy time — but only if that is wired up.
  // A committed sitemap with a guessed hostname would be worse than none.
  if (!exists("apps/web/public/sitemap.xml") && !seoEmitterWired) {
    finding("seo/missingFile", "seo", "medium", "apps/web/public/sitemap.xml", "deploy",
      "no sitemap, and no emitter wired into the Pages build to produce one",
      "Run scripts/generate-seo.js from scripts/pages-build.mjs "
      + "(the Pages Git Integration build gate) before `next build`.");
  }
  for (const robotsPath of ["apps/web/public/robots.txt"]) {
    if (!exists(robotsPath)) continue;
    const robots = read(robotsPath);
    if (!/^User-agent:\s*\*/m.test(robots)) {
      finding("seo/robotsDirectives", "seo", "medium", robotsPath, robotsPath,
        "robots.txt has no User-agent: * block", "Say something to every crawler.");
    }
    if (/^Sitemap:/m.test(robots) && !seoEmitterWired) {
      finding("seo/robotsSitemap", "seo", "low", robotsPath, robotsPath,
        "a Sitemap line is committed but nothing regenerates it per deployment",
        "Let scripts/generate-seo.js own that line.");
    }
  }
}

/* ========================================================================== */
/*  3c. Cypher portability                                                     */
/* ========================================================================== */

/**
 * ORDER BY, SKIP/OFFSET and LIMIT became standalone clauses in Neo4j 5.24. On
 * every earlier 5.x — and an Aura Free database is provisioned with whichever 5.x
 * is current, not with the one this repository was written against — a statement
 * like `WITH p, cost LIMIT $enumCap ORDER BY cost ASC` is a syntax error, so the
 * feature behind it fails against the real database while every offline test
 * stays green: both smoke suites stub Neo4j and never parse Cypher.
 *
 * Scanned as whole files rather than line by line, because Python keeps its
 * Cypher in triple-quoted strings where the two clauses sit on different lines —
 * and because both languages build a statement out of quoted fragments, the
 * delimiters and concatenation operators between two fragments are blanked first
 * (whitespace only, newlines preserved, so the reported line is still the real
 * one). Documentation is deliberately not scanned: it quotes the anti-pattern.
 */
function auditCypher() {
  const pattern = /LIMIT\s+(?:\$\w+|\d+)\s+(?:ORDER\s+BY|SKIP|OFFSET)\b/gi;
  const flatten = (text) => text.replace(/(`|"|')\s*(?:\+\s*)?(`|"|')/g,
    (chunk) => chunk.replace(/[^\n]/g, " "));
  const files = ["worker.js", "graph_analytics.py", "ingest.py", "telegram_bot.py"]
    .concat(walk("puppetnet", (relative) => relative.endsWith(".py")));
  for (const file of files) {
    const text = read(file);
    if (!text) continue;
    for (const match of flatten(text).matchAll(pattern)) {
      const line = text.slice(0, match.index).split("\n").length;
      finding("cypher/standaloneOrderBy", "bugs", "high", file, `${file}:${line}`,
        `a Cypher statement needs Neo4j 5.24+ to parse (${match[0].replace(/\s+/g, " ")})`,
        "Keep ORDER BY/SKIP/LIMIT as subclauses of the WITH or RETURN they belong to: "
        + "split `WITH x LIMIT $n ORDER BY y` into `WITH x LIMIT $n` followed by "
        + "`WITH x ORDER BY y LIMIT $m`.");
    }
  }
}

/* ========================================================================== */
/*  4. Cross-module invariants                                                 */
/* ========================================================================== */

function auditInvariants() {
  const worker = read("worker.js") || "";

  // The Worker publishes its ceilings in a frozen literal; the relay is the
  // only transport left (the v1 console that had to clamp to the same values
  // is archived), but the literal must stay parseable — GRAPH_LIMITS is the
  // budget the Python pipeline plans against too.
  const limits = {};
  const block = /const GRAPH_LIMITS = Object\.freeze\(\{([\s\S]*?)\}\);/.exec(worker);
  if (!block) {
    finding("limits/workerMissing", "bugs", "high", "worker.js", "GRAPH_LIMITS",
      "GRAPH_LIMITS could not be parsed", "Keep the frozen object literal.");
  } else {
    for (const match of block[1].matchAll(/(\w+):\s*(\d+)/g)) limits[match[1]] = Number(match[2]);
    if (Object.keys(limits).length === 0) {
      finding("limits/workerMissing", "bugs", "high", "worker.js", "GRAPH_LIMITS",
        "GRAPH_LIMITS parsed to an empty budget", "Keep the name/value pairs on one literal.");
    }
  }
}

/*  4b. robots.txt parity (relay ↔ direct transport)                           */
/* ========================================================================== */

function auditRobotsParity() {
  // robots.txt is enforced in two places that must agree: the relay (TypeScript)
  // and the direct transport (Python). They were written separately, so they
  // drifted — the Worker's tie-break let the file's line order decide an
  // equal-length Allow/Disallow pair, and only the Worker consulted the file at
  // all, which made an open circuit breaker a licence to crawl.
  //
  // The three things this pins are the ones whose absence is invisible at
  // runtime: the policy must still be *called* on both direct paths (a commented
  // call reads exactly like a real one to a substring search), both evaluators
  // must read an unreadable rules file as a refusal, and both must implement the
  // tie-break. Verdict correctness itself is covered by the shared fixture table
  // (tests/test_robots.py::RFC_FIXTURES = worker smoke G15b).
  const robotsPy = read("puppetnet/net/robots.py");
  const client = read("puppetnet/net/proxy_client.py");
  const workerFile = read("worker.js");
  if (!robotsPy || !client || !workerFile) {
    finding("invariant/robotsParity", "bugs", "high", "puppetnet/net/** ↔ worker.js", "robots.txt",
      "could not read the robots evaluators or the client (" +
        [robotsPy, client, workerFile].map((v) => (v ? "ok" : "missing")).join("/") + ")",
      "Keep these three files parseable — this check compares them.");
  } else {
    // Comment-stripped, because `# self.robots.check(...)` is not a call.
    const codeOnly = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");
    const clientCode = codeOnly(client);
    for (const [label, header] of [["request", "def _direct("], ["stream", "def stream_lines("]]) {
      const start = clientCode.indexOf(header);
      const rest = start < 0 ? "" : clientCode.slice(start + header.length);
      const nextDef = rest.indexOf("\n    def ");
      const body = nextDef < 0 ? rest : rest.slice(0, nextDef);
      if (start < 0 || !body.includes("self.robots.check(")) {
        finding("invariant/robotsParity", "docs", "high", "puppetnet/net/proxy_client.py",
          `${label} path`,
          `${header.trim()} does not consult the robots policy`,
          "The relay enforces robots.txt for the traffic it serves; the direct path must enforce it for its own.");
      }
    }
    if (!/rule\.pattern\.length === best\.pattern\.length[^\n]*allow/.test(workerFile) ||
        !/len\(pattern\) == len\(best\[1\]\) and rule_type == "allow"/.test(robotsPy)) {
      finding("invariant/robotsParity", "bugs", "high", "worker.js ↔ puppetnet/net/robots.py", "tie-break",
        "an equal-length Allow/Disallow pair is not resolved in favour of Allow in both evaluators",
        "RFC 9309 §2.2.2: the longest match wins and Allow breaks a tie — otherwise the order of the lines decides.");
    }
    if (!workerFile.includes("robots_unavailable") || !robotsPy.includes('"unreachable"')) {
      finding("invariant/robotsParity", "bugs", "high", "worker.js ↔ puppetnet/net/robots.py", "unreachable rules file",
        "an unreadable robots.txt is not reported as a refusal by both transports",
        "RFC 9309 §2.3.1.4: unreachable means complete disallow — and the client must be able to tell it from a verdict.");
    }
  }
}

/* ========================================================================== */
/*  4c. Header filter parity (direct transport ⊆ relay)                        */
/* ========================================================================== */

function auditHeaderParity() {
  // Both transports filter caller-supplied headers before they reach an origin:
  // Python in `HeaderFactory.build`, the relay in `applyCallerHeaders`. The relay
  // additionally refuses the names Cloudflare sets (`x-forwarded-for`,
  // `cf-connecting-ip`, …) — that list is only meaningful at the edge. What must
  // hold is that the relay's set is a *superset* of the direct transport's: a name
  // the runner refuses to send but the relay happily forwards is a bypass with
  // extra steps, and the asymmetry is invisible in either file alone.
  const workerFile = read("worker.js");
  const headersPy = read("puppetnet/net/headers.py");
  if (!workerFile || !headersPy) {
    finding("invariant/headerFilterParity", "security", "high", "worker.js ↔ puppetnet/net/headers.py", "blocked headers",
      "could not read one of the two filters",
      "Keep `BLOCKED_REQUEST_HEADERS` and `_UNSAFE_HEADERS` parseable — this check compares them.");
    return;
  }
  const jsBlock = /const BLOCKED_REQUEST_HEADERS = Object\.freeze\(\s*new Set\(\[([\s\S]*?)\]\)\s*\)/.exec(workerFile);
  const pyBlock = /_UNSAFE_HEADERS = frozenset\(\s*\{([\s\S]*?)\}\s*\)/.exec(headersPy);
  if (!jsBlock || !pyBlock) {
    finding("invariant/headerFilterParity", "security", "high", "worker.js ↔ puppetnet/net/headers.py", "blocked headers",
      `could not locate a filter list (worker: ${jsBlock ? "ok" : "missing"}, python: ${pyBlock ? "ok" : "missing"})`,
      "Keep the two lists in their current shape, or update this check with them.");
    return;
  }
  const names = (block) => [...block[1].matchAll(/"([a-z0-9-]+)"/g)].map((m) => m[1]);
  const jsNames = new Set(names(jsBlock));
  const pyNames = names(pyBlock);
  const notCovered = pyNames.filter((name) => !jsNames.has(name));
  if (notCovered.length) {
    finding("invariant/headerFilterParity", "security", "high", "puppetnet/net/headers.py → worker.js", "blocked headers",
      `the direct transport refuses ${notCovered.join(", ")}, the relay does not`,
      "Add the name to BLOCKED_REQUEST_HEADERS too: the relay is the transport that serves most traffic.");
  }
}

/* ========================================================================== */
/*  5. Worker security                                                         */
/* ========================================================================== */

function auditWorker() {
  const file = "worker.js";
  const worker = read(file);
  if (!worker) return;

  scan(file, /console\.(log|error|warn|info)\s*\(([^;]*)/g, (match, where) => {
    if (/token|secret|password|authorization|api[_-]?key|credential/i.test(match[2])) {
      finding("worker/secretInLog", "security", "high", file, where,
        `log line mentions a credential: ${match[2].trim().slice(0, 80)}`,
        "Log a fingerprint or a boolean, never the value.");
    }
  });
  scan(file, /"Access-Control-Allow-Origin":\s*"([^"]*)"/g, (match, where) => {
    if (match[1] === "*") {
      finding("worker/corsWildcard", "security", "high", file, where,
        "CORS origin is a wildcard", "Echo a validated origin instead.");
    }
  });
  // Access-Control-Allow-Origin takes exactly one origin, or `*`. A deployment
  // that lists two of them used to have them joined into the header — a value
  // every browser rejects, so such a deployment served none of its origins and
  // the console failed CORS with no clue why. The answer must also depend on the
  // caller's Origin, which is why the resolution has to happen per request.
  const corsStart = worker.indexOf("function corsHeaders(env) {");
  if (corsStart < 0) {
    finding("worker/corsMissing", "security", "high", file, "corsHeaders",
      "worker.js no longer exposes corsHeaders",
      "Restore it, or move these rules to wherever CORS is answered now.");
  } else {
    let depth = 0;
    let end = worker.length - 1;
    for (let i = worker.indexOf("{", corsStart); i < worker.length; i += 1) {
      if (worker[i] === "{") depth += 1;
      else if (worker[i] === "}") {
        depth -= 1;
        if (depth === 0) { end = i; break; }
      }
    }
    const body = worker.slice(corsStart, end + 1);
    if (/Access-Control-Allow-Origin"\]\s*=[^;]*\.join\(/.test(body)) {
      finding("worker/corsJoinedList", "security", "high", file, "corsHeaders",
        "the allowed origins are joined into Access-Control-Allow-Origin",
        "Echo the caller's Origin when it is on the list, send no header at all when it "
        + "is not, and keep `*` for wildcard deployments — a comma-separated list is "
        + "rejected by every browser.");
    }
    if (body.indexOf("__requestOrigin") < 0) {
      finding("worker/corsNotPerRequest", "security", "medium", file, "corsHeaders",
        "the CORS answer does not depend on the request's Origin",
        "Carry the Origin with the env (withRequestOrigin) and echo it back only when the "
        + "operator allowed it; a static answer cannot serve a multi-origin deployment.");
    }
  }
  if (/Access-Control-Allow-Credentials":\s*"true"/.test(worker)
    && /"Access-Control-Allow-Origin":\s*"\*"/.test(worker)) {
    finding("worker/corsCredentials", "security", "high", file, "CORS",
      "credentials allowed with a wildcard origin", "Never combine the two.");
  }
  // Read-only API: anything but GET/HEAD/OPTIONS must be refused.
  if (!/(405|Method Not Allowed)/i.test(worker)) {
    finding("worker/methodAllowance", "security", "high", file, "router",
      "no 405 path found — the graph API may accept writes",
      "Reject every method except GET/HEAD/OPTIONS.");
  }
  if (!/timingSafeEqual/.test(worker)) {
    finding("worker/tokenCompare", "security", "medium", file, "token comparison",
      "token comparison may not be constant-time", "Use crypto.subtle.timingSafeEqual.");
  }
  // Fail-closed default: no token configured must not mean open access.
  if (!/GRAPH_PUBLIC_READ/.test(worker)) {
    finding("worker/failClosed", "security", "medium", file, "graph auth",
      "no explicit public-read switch found", "Require a token unless explicitly opened.");
  }
  // Workers KV Free allows 1k writes/day. A KV write on the request path spends
  // that budget per document, and the scheduled cadence (24 hourly runs with
  // hundreds of article hosts each) would drain it before noon — after which the
  // fleet silently loses its shared rate limits and its robots.txt cache. Bucket
  // writes belong to the throttled sync (one per hot host per sync window).
  {
    const start = worker.indexOf("async function acquireHostToken(");
    if (start >= 0) {
      const end = worker.indexOf("\n}", start);
      const body = worker.slice(start, end < 0 ? worker.length : end);
      if (/\.put\(/.test(body)) {
        finding("worker/kvWritePerRequest", "cost", "high", file, "acquireHostToken",
          "a KV write sits on the per-request path",
          "Publish the bucket from the throttled sync (one write per hot host per window), "
          + "not once per request: KV Free allows 1k writes/day and a single hourly run can "
          + "exceed that.");
      }
    }
  }
}

/* ========================================================================== */
/*  6. Python hazards                                                          */
/* ========================================================================== */

function auditPython() {
  const files = walk(".", (relative) => relative.endsWith(".py"));
  const hazards = [
    [/\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)/g, "high", "python/yamlLoad",
      "yaml.load without SafeLoader can execute arbitrary Python", "Use yaml.safe_load."],
    [/\bshell\s*=\s*True/g, "high", "python/shellTrue",
      "subprocess with shell=True interpolates into a shell", "Pass an argument list."],
    [/\bpickle\.load\b|\bmarshal\.load\b/g, "high", "python/pickle",
      "unpickling untrusted bytes is code execution", "Use JSON or a schema."],
    // A bare `eval(`/`exec(` is dynamic code execution. The lookbehind keeps
    // attribute calls out: `self.model.eval()` is PyTorch's inference-mode
    // switch, not the builtin, and the GLiNER engine legitimately needs it.
    [/(?<![\w.])eval\s*\(|(?<![\w.])exec\s*\(/g, "high", "python/eval",
      "dynamic code execution", "Replace with a dispatch table."],
    [/\bos\.system\s*\(/g, "medium", "python/osSystem",
      "os.system runs a shell string", "Use subprocess with a list."],
    [/^\s*except\s*:\s*$/g, "medium", "python/bareExcept",
      "bare except swallows KeyboardInterrupt and SystemExit too", "Catch Exception explicitly."],
    [/datetime\.utcnow\(\)/g, "low", "python/utcnow",
      "datetime.utcnow() is naive and deprecated in 3.12+",
      "Use datetime.now(timezone.utc)."],
    [/\bassert\b/g, "info", "python/assert",
      "assert is stripped under python -O", "Validate explicitly on production paths."],
    [/verify\s*=\s*False/g, "high", "python/tlsVerifyOff",
      "TLS verification disabled", "Remove verify=False."],
    // A renderer that passes a value through because it "looks like markup" is
    // an injection sink: the check that decides is a property of the *content*,
    // which the attacker controls, not of the caller. This existed in
    // `telegram_bot.bullet()` and let `<b>pwned</b>` from any source field reach
    // the channel as markup.
    [/startswith\("(<|\\u003c)"\)[^\n]*endswith\(">"\)/g, "high", "python/looksLikeMarkup",
      "a value is trusted because it looks like markup",
      "Escape everything and build tags with an explicit helper."],
  ];
  for (const file of files) {
    const isTest = /(^|\/)tests?\//.test(file) || /test_.*\.py$/.test(file);
    for (const [regex, severity, id, message, fix] of hazards) {
      scan(file, regex, (_match, where, line) => {
        if (/^\s*#/.test(line)) return;
        if (isTest && id === "python/assert") return;
        // A method *named* eval — the offline model double in tests/test_gliner.py
        // mirrors PyTorch's `Module.eval()` — defines an API, it does not execute
        // a string. Only a call of the builtin is the hazard.
        if (id === "python/eval" && /^\s*(async\s+)?def\s+(eval|exec)\s*\(/.test(line)) return;
        finding(id, "security", severity, file, where, `${message} — ${line.slice(0, 70)}`, fix);
      });
    }
  }
}

/* ========================================================================== */
/*  7. Cost & supply chain — the project must stay free to run                 */
/* ========================================================================== */

/**
 * Every external host the code touches, with the free-tier budget it lives in.
 * Adding a host here is a deliberate act: it forces the question "what does this
 * cost, and what happens at the limit?" before the code ships.
 */
const HOST_BUDGET = {
  "generativelanguage.googleapis.com": { cost: "optional free quota or PAID account", note: "Gemini fallback disabled by default; counted <=150 tokens/call, 3 reservations/run, at most 2 workflow runs/day; operator must verify current model availability, quota and billing" },
  "schema.org": { cost: "free", note: "JSON-LD vocabulary identifier, never fetched" },
  "www.w3.org": { cost: "free", note: "XML namespace identifier, never fetched" },
  "opensource.org": { cost: "free", note: "licence URL in metadata" },
  "github.com": { cost: "free", note: "repository links" },
  "api.github.com": { cost: "free", note: "5 000 req/h with a token, 60 without" },
  "raw.githubusercontent.com": { cost: "free", note: "soft rate limit, cacheable" },
  "objects.githubusercontent.com": { cost: "free", note: "release asset redirects" },
  "neo4j.com": { cost: "free", note: "docs links" },
  "console.neo4j.io": { cost: "free", note: "AuraDB Free: 200k nodes / 400k rels, 1 AU" },
  "developers.cloudflare.com": { cost: "free", note: "docs links" },
  "cloudflare.com": { cost: "free", note: "docs links" },
  "api.telegram.org": { cost: "free", note: "Bot API, ~30 msg/s global" },
  "api.cloudflare.com": { cost: "free tier, hard stop", note: "Workers AI: 10,000 neurons/day on both plans, requests fail after the cap instead of billing; the extractor keeps a UTC ledger and stops at the limit" },
  "registry.npmjs.org": { cost: "free", note: "dev-time vendoring only" },
  "pypi.org": { cost: "free", note: "dev-time installs only" },
  "files.pythonhosted.org": { cost: "free", note: "dev-time installs only" },
  "download.pytorch.org": { cost: "free", note: "CI-only CPU wheel index for the optional GLiNER extra; the shipped code never calls it" },
  "huggingface.co": { cost: "free", note: "GLiNER model weights on a cold cache (opted-in ingestion only); Apache-2.0 model, no API key, no paid endpoint" },
  "tile.openstreetmap.org": { cost: "free, donation-funded shared service", note: "opt-in map tiles after an explicit user click, viewport-only, browser-cached; honour operations.osmfoundation.org/policies/tiles/ and use an approved provider for high traffic" },
  "www.openstreetmap.org": { cost: "free", note: "attribution/copyright link rendered in the map control, never fetched as an API" },
  "offshoreleaks.icij.org": { cost: "free", note: "ICIJ Offshore Leaks, public-domain data" },
  "www.icij.org": { cost: "free", note: "ICIJ public pages" },
  "opencorporates.com": { cost: "free tier, then PAID", note: "anonymous use is throttled hard; an API key needs a paid plan for volume — keep it optional and off by default" },
  "www.wikidata.org": { cost: "free", note: "Wikidata SPARQL: be polite, one query at a time" },
  "query.wikidata.org": { cost: "free", note: "SPARQL endpoint, user-agent required" },
  "en.wikipedia.org": { cost: "free", note: "REST summary API, 200 req/s per IP" },
  "registry.faa.gov": { cost: "free", note: "US aircraft registry, public data" },
  "a4b2c6.local": { cost: "n/a", note: "test fixture host" },
  "relay.example.invalid": { cost: "n/a", note: "test fixture host" },
  "console.example.invalid": { cost: "n/a", note: "test fixture host" },
  "example.com": { cost: "n/a", note: "documentation placeholder" },
  "example.invalid": { cost: "n/a", note: "documentation placeholder" },
  "example.dev": { cost: "n/a", note: "documentation placeholder" },
  "www.sitemaps.org": { cost: "free", note: "sitemap XML namespace identifier, never fetched" },
  "puppetnet-web.pages.dev": { cost: "free", note: "own Cloudflare Pages deployment; static hosting within the documented Pages Free limits" },
  "puppetnet-console.pages.dev": { cost: "free", note: "the deployment's own Cloudflare Pages domain — the emitter's example origin, and the default the workflow derives" },
  "localhost": { cost: "free", note: "local development" },
  "127.0.0.1": { cost: "free", note: "local development" },
};

/** Hosts that would put the operator on somebody's invoice — never allowed. */
const PAID_BLOCKLIST = [
  "api.qrserver.com", "quickchart.io", "chart.googleapis.com", "maps.googleapis.com",
  "api.openai.com", "openai.com", "api.anthropic.com", "serper.dev", "serpapi.com",
  "api.hunter.io", "api.shodan.io", "haveibeenpwned.com", "api.ipify.org",
  "cdn.tailwindcss.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com",
  "fonts.googleapis.com", "fonts.gstatic.com", "google-analytics.com", "googletagmanager.com",
  "cdn.plot.ly", "d3js.org",
];

/**
 * Pattern rules, applied when a host is not listed exactly. A new source should
 * still land in a known cost class — and anything that does not is a finding,
 * which is the point: adding a data source is a decision, not a side effect.
 */
const HOST_RULES = [
  { test: /\.gov(\.|$)/, cost: "free", note: "public government data, no key" },
  { test: /\.gov\.uk$/, cost: "free", note: "UK public data; Companies House needs a free key (600 req/5 min)" },
  { test: /^(feeds|rss)\./, cost: "free", note: "RSS feed — respect robots.txt and the feed's own cadence" },
  { test: /rapidapi\.com$/, cost: "PAID subscription", note: "RapidAPI metering; must stay key-gated and off by default" },
  { test: /^api\.opencorporates\.com$/, cost: "PAID beyond a small free allowance", note: "token-gated, off by default; anonymous use is throttled and non-commercial only" },
  { test: /neo4j\.io$/, cost: "free tier", note: "AuraDB Free connection shape: 200k nodes / 400k rels, auto-pauses when idle" },
  { test: /npmjs\.(org|com)$/, cost: "free", note: "dev-time registry metadata for vendoring, never at runtime" },
  { test: /icij\.org$/, cost: "free", note: "ICIJ Offshore Leaks — public-domain data" },
  { test: /wikidata\.org$|wikipedia\.org$/, cost: "free", note: "send an identifying User-Agent, one query at a time" },
  { test: /adsbdb\.com$/, cost: "free", note: "community ADS-B read-through; no key, be polite" },
  { test: /faa\.gov$/, cost: "free", note: "US registry dump, public data" },
  // Editorial RSS and public news pages: free to read, but they are somebody's
  // property — the crawl budget in docs/operations.md is what keeps this legal.
  { test: /(theguardian|bbc\.co|nytimes|reuters|aljazeera|france24|flightglobal|tradewindsnews|gcaptain|avherald|aviation24|occrp|globalwitness|transparency)\./,
    cost: "free", note: "newsroom RSS/public page — fetch the feed, not the whole site, and honour crawl-delay" },
];

/** Test and documentation fixtures: reserved TLDs and example hosts. */
const isFixtureHost = (host) => !host.includes(".") || /\.(test|invalid|local|example)$/.test(host)
  || host.includes("example") || /\.example$/.test(host);

function auditCostAndSupplyChain() {
  // The generated ledger and this script are excluded: a report that quotes a
  // finding, and a blocklist that names what it forbids, are not calls.
  //
  // Generated build output (`apps/web/.next`, `apps/web/out`) and vendored
  // third-party bundles are excluded too. They are not this repository's source:
  // a minified library carries its homepage and licence URLs, so scanning them
  // invents "external hosts" that nothing here ever resolves — and `out/` only
  // exists after a local build, which would make the ledger depend on whether
  // somebody ran `npm run ci:pages` before `npm run audit`.
  const codeFiles = walk(".", (relative) => /\.(js|mjs|cjs|py|yml|yaml|toml|html|css|json)$/.test(relative)
    && !/package-lock\.json$/.test(relative)
    && !/(^|\/)vendor\//.test(relative)
    && !/^apps\/web\/(\.next|out)\//.test(relative)
    && !/^docs\/audit\//.test(relative)
    && relative !== "scripts/audit.mjs"
    && !/^node_modules\//.test(relative));

  // 1. Paid or third-party-CDN hosts must not appear anywhere in shipped code.
  //    The audit script itself is exempt: a blocklist has to name what it forbids.
  for (const file of codeFiles.filter((name) => name !== "scripts/audit.mjs")) {
    for (const host of PAID_BLOCKLIST) {
      scan(file, new RegExp(host.replace(/\./g, "\\."), "g"), (_m, where, line) => {
        if (/^\s*(#|\/\/|\*)/.test(line)) return;
        // A test or a blocklist that *names* a banned host is the opposite of a
        // call to it; those lines are the guard, not the offence.
        if (/banned|blocklist|denylist|must not|forbidden|never/i.test(line)) return;
        finding("cost/paidHost", "security", "high", file, where,
          `references ${host} — a paid service or a CDN that breaks offline use`,
          "Vendor it or remove it; this project must cost nothing to run.");
      });
    }
  }

  // 2. Inventory every external host so the free-tier budget stays explicit.
  const inventory = new Map();
  for (const file of codeFiles) {
    scan(file, /https?:\/\/([a-z0-9.:\[\]-]+)(?::\d+)?/gi, (match, where) => {
      // Two normalisations, for the same reason the worker does them: a trailing
      // dot is the DNS root and names the same host (`https://localhost./` is
      // `localhost`), and a bare IP literal is not a service with a free-tier
      // budget — it is an address, and the ones a test writes down are the ones
      // the SSRF guard refuses.
      const host = match[1].toLowerCase().replace(/\.+$/, "").replace(/^\[|\]$/g, "");
      if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.indexOf(":") >= 0) return;
      if (!inventory.has(host)) inventory.set(host, []);
      if (inventory.get(host).length < 3) inventory.get(host).push(where);
    });
  }
  for (const [host, where] of [...inventory.entries()].sort()) {
    if (isFixtureHost(host)) continue;
    const budget = HOST_BUDGET[host]
      || (() => { const rule = HOST_RULES.find((candidate) => candidate.test.test(host));
        return rule ? { cost: rule.cost, note: rule.note } : null; })();
    if (!budget) {
      // A host that appears only in tests is a fixture, not a dependency: nothing
      // shipped ever resolves it. It is recorded, but it does not need a budget.
      const testOnly = where.every((site) => /(^|\/)tests?\//.test(site));
      finding(testOnly ? "cost/testOnlyHost" : "cost/unknownHost", "security",
        testOnly ? "info" : "medium", host, where.join(", "),
        testOnly
          ? `external host ${host} appears only in tests (fixture)`
          : `external host ${host} has no recorded free-tier budget`,
        testOnly ? "No action — nothing shipped calls it."
          : "Add it to HOST_BUDGET with its cost and limit, or remove the call.");
    } else if (/PAID/i.test(budget.cost)) {
      finding("cost/external-host", "info", "low", host, where.join(", "),
        `${host}: ${budget.cost} — ${budget.note}`,
        "Keep it optional and disabled by default.");
    }
  }

  // 3. Any source that can cost money must be key-gated and off by default, so
  //    a fresh clone never sends a request somebody has to pay for.
  const configPy = read("puppetnet/config.py") || "";
  const PAIRED = [
    { host: "api.opencorporates.com", key: "opencorporates_api_token" },
    { host: "adsbexchange-com1.p.rapidapi.com", key: "adsbexchange_api_key" },
  ];
  for (const pair of PAIRED) {
    if (!inventory.has(pair.host)) continue;
    const declared = new RegExp(`${pair.key}:\\s*str\\s*=\\s*"([^"]*)"`).exec(configPy);
    if (!declared) {
      finding("cost/paidSourceUngated", "security", "high", "puppetnet/config.py", pair.host,
        `${pair.host} is reachable but no ${pair.key} setting was found`,
        "Gate the paid source behind an API key that defaults to empty.");
    } else if (declared[1] !== "") {
      finding("cost/paidSourceDefault", "security", "high", "puppetnet/config.py", pair.host,
        `${pair.key} ships with a non-empty default — the paid source is on for everybody`,
        'Default it to "" so the adapter degrades to the free source.');
    }
  }

  // 5. No secret may be committed; .env must stay ignored.
  const gitignore = read(".gitignore") || "";
  for (const pattern of [".env", ".venv", "node_modules", "data/"]) {
    if (!gitignore.includes(pattern)) {
      finding("hygiene/gitignore", "security", "medium", ".gitignore", ".gitignore",
        `.gitignore does not list ${pattern}`, `Add ${pattern}.`);
    }
  }
  if (exists(".env")) {
    finding("hygiene/dotEnv", "security", "high", ".env", ".env",
      "a .env file exists in the working tree", "Never commit it; it must stay ignored.");
  }
  for (const file of codeFiles) {
    scan(file, /(ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|neo4j(s)?:\/\/[^/\s]+:[^@\s]+@)/g,
      (_m, where, line) => {
        if (/example|placeholder|your[_-]?|REDACTED|\*\*\*/i.test(line)) return;
        finding("security/committedSecret", "security", "high", file, where,
          `something that looks like a credential: ${line.slice(0, 60)}`,
          "Rotate it and remove it from the repository.");
      });
  }
}

/* ========================================================================== */
/*  8. Operations — scheduling, workflows, docs                                */
/* ========================================================================== */

function auditOperations() {
  const workflows = walk(".github/workflows", (relative) => relative.endsWith(".yml"));
  if (workflows.length === 0) {
    finding("ops/noWorkflows", "bugs", "high", ".github/workflows", ".github/workflows",
      "no GitHub Actions workflows found", "The 24 h cycle depends on them.");
  }
  const crons = [];
  for (const file of workflows) {
    scan(file, /- cron:\s*['"]([^'"]+)['"]/g, (match, where) => {
      const expression = match[1];
      const parts = expression.trim().split(/\s+/);
      if (parts.length !== 5) {
        finding("ops/cronSyntax", "bugs", "high", file, where,
          `cron "${expression}" does not have five fields`, "Use minute hour dom month dow.");
        return;
      }
      crons.push({ file, where, expression });
      // GitHub shifts every cron that is not a round hour under load; the docs
      // recommend avoiding minute 0 for exactly that reason.
      if (parts[0] === "0") {
        finding("ops/cronMinuteZero", "bugs", "info", file, where,
          `cron "${expression}" runs at minute 0, when GitHub load-shifts most`,
          "Pick an off-peak minute (e.g. 17) for a more reliable start.");
      }
    });
    // A scheduled workflow must be able to fail loudly.
    if (!/timeout-minutes/.test(read(file))) {
      finding("ops/noTimeout", "bugs", "medium", file, file,
        "no timeout-minutes — a hung job burns the free-tier budget",
        "Add a timeout to every job.");
    }
    if (!/concurrency/.test(read(file))) {
      finding("ops/noConcurrency", "bugs", "medium", file, file,
        "no concurrency group — overlapping runs can double-harvest and hit rate limits",
        "Add concurrency with cancel-in-progress where safe.");
    }
  }
  if (crons.length === 0) {
    finding("ops/noSchedule", "bugs", "high", ".github/workflows", "schedules",
      "no cron schedule found — nothing runs unattended",
      "Add the harvest schedule.");
  }

  // The daily cycle must keep AuraDB Free awake: it auto-pauses after inactivity,
  // and a paused database is a dead console.
  if (crons.length > 0) {
    const perDay = crons.length;
    finding("ops/cadence", "info", "info", ".github/workflows", "schedules",
      `${perDay} scheduled trigger(s): ${crons.map((cron) => cron.expression).join(", ")}`,
      "See docs/operations.md for the free-tier budget this must fit in.");
  }
}

/* ========================================================================== */
/*  Report                                                                     */
/* ========================================================================== */

function report() {
  const order = { high: 0, medium: 1, low: 2, info: 3, accepted: 4 };
  const sorted = [...findings].sort((a, b) => (order[a.severity] - order[b.severity])
    || a.module.localeCompare(b.module));

  const byDimension = new Map();
  for (const item of findings) {
    byDimension.set(item.dimension, (byDimension.get(item.dimension) || 0) + 1);
  }

  console.log("\nPuppetNET audit — static, offline, dependency-free\n");
  console.log(`  modules scanned   ${walk(".", (r) => /\.(js|mjs|cjs|py|yml|html|css)$/.test(r) && !/^web\/vendor\//.test(r)).length}`);
  console.log(`  findings          ${findings.length}`);
  for (const severity of ["high", "medium", "low", "info", "accepted"]) {
    if (counters[severity]) console.log(`    ${severity.padEnd(9)} ${counters[severity]}`);
  }
  console.log(`  by dimension      ${[...byDimension.entries()].map(([key, value]) => `${key} ${value}`).join(" · ")}`);

  for (const severity of ["high", "medium", "low", "info"]) {
    const group = sorted.filter((item) => item.severity === severity);
    if (group.length === 0) continue;
    console.log(`\n── ${severity.toUpperCase()} ${"─".repeat(Math.max(0, 60 - severity.length))}`);
    for (const item of group) {
      console.log(`  [${item.dimension}] ${item.id}`);
      console.log(`      ${item.module} @ ${item.where}`);
      console.log(`      ${item.message}`);
      if (item.fix) console.log(`      → ${item.fix}`);
    }
  }
  const accepted = sorted.filter((item) => item.severity === "accepted");
  if (accepted.length) {
    console.log(`\n── ACCEPTED RISKS ${"─".repeat(46)}`);
    for (const item of accepted) {
      console.log(`  ${item.id} (${item.module}) — ${item.justification.split(". ")[0]}.`);
    }
  }

  const outDir = path.join(ROOT, "docs", "audit");
  const payload = {
    generated: new Date().toISOString(),
    strict: STRICT,
    counters,
    findings: sorted,
  };
  mkdirSync(outDir, { recursive: true });
  writeFileSync(path.join(outDir, "findings.json"), JSON.stringify(payload, null, 2) + "\n");
  console.log(`\nledger written to docs/audit/findings.json`);

  const blocking = findings.filter((item) => !item.accepted
    && (item.severity === "high" || item.severity === "medium" || (STRICT && item.severity === "low")));
  if (blocking.length) {
    console.log(`\nAUDIT FAILED: ${blocking.length} finding(s) must be resolved or accepted with a reason.`);
    process.exitCode = 1;
  } else {
    console.log("\nAUDIT PASSED: no unresolved high/medium findings.");
  }
}

auditSeo();
auditCypher();
auditInvariants();
auditRobotsParity();
auditHeaderParity();
auditWorker();
auditPython();
auditCostAndSupplyChain();
auditOperations();
await report();
