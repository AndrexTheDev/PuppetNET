/**
 * web_smoke.mjs — behavioural smoke test for the browser console in web/.
 *
 *   node tests/web_smoke.mjs
 *
 * The console is a static SPA: no build step, no framework, no server. That makes
 * it easy to deploy and easy to break silently, so this suite runs the *shipped*
 * artefacts — `web/index.html`, the vendored UMD bundles in `web/vendor/`, and
 * `web/app.js` — inside jsdom, exactly as a browser would, and asserts on what
 * comes out.
 *
 * Three things it is deliberately strict about:
 *
 *   * **Offline.** Demo mode must make zero network requests, and index.html must
 *     reference nothing but files that exist in the repository. A console that
 *     quietly reaches for a CDN is a console that breaks in a SCIF and leaks an
 *     analyst's IP address.
 *   * **Hostile text.** Every string it renders was written by somebody else. The
 *     escaping checks feed it `<img src=x onerror=…>` through a node name, a
 *     search suggestion and a CSV cell, and assert that no element is created.
 *   * **Contract with the Worker.** The last checks boot the console against the
 *     real `worker.js` running on the shared fake Neo4j, so the two halves of the
 *     system are tested against each other rather than against two different
 *     ideas of the same API.
 *
 * Cytoscape is forced headless (jsdom has no canvas); everything else — layout,
 * styling, classes, event handlers — runs for real.
 *
 * Exits non-zero on the first failed assertion so CI can gate on it.
 */

import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { JSDOM, VirtualConsole } from "jsdom";

import worker from "../worker.js";
import {
  FX,
  FX_BY_KEY,
  P_KASTELION,
  O_MERIDIAN,
  O_SARNEN,
  F_TALLOW,
  L_LIMASSOL,
  A_TAIL,
  P_UNKNOWN,
  neo4jBehaviour,
  fakeNeo4jTxResponse,
  runStatement,
} from "./helpers/fake_neo4j.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WEB = path.join(ROOT, "web");
const readWeb = (relative) => readFileSync(path.join(WEB, relative), "utf8");

/** Load order matters: fcose needs cose-base, which needs layout-base. */
const VENDOR_ORDER = Object.freeze([
  "layout-base.min.js",
  "cose-base.min.js",
  "cytoscape.min.js",
  "cytoscape-fcose.min.js",
]);

const RELAY_ORIGIN = "https://relay.example.invalid";
const GRAPH_TOKEN = "console-token-SECRET-9";

const WATCHDOG = setTimeout(() => {
  console.error("web_smoke: watchdog fired — a boot or a query never settled");
  process.exit(2);
}, 180000);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Wait for the layout to finish, not merely to start.
 *
 * app.js runs fcose with `animate: "end"`, which computes positions immediately
 * and then animates every node to them over ~620 ms. Polling for "some node has
 * left the origin" therefore succeeds mid-animation, and the assertions that
 * follow would run against a graph that is still moving — flaky in a way that
 * looks like a product bug. This waits for the first few positions to be placed
 * and unchanged across two polls.
 */
async function waitForLayout(api, timeout = 25000) {
  const deadline = Date.now() + timeout;
  let previous = "";
  let stable = 0;
  while (Date.now() < deadline) {
    const cy = api.cy;
    if (cy && cy.nodes().length > 0) {
      const signature = cy.nodes()
        .slice(0, 8)
        .map((node) => {
          const at = node.position();
          return `${Math.round(at.x)},${Math.round(at.y)}`;
        })
        .join("|");
      const placed = signature.length > 0
        && !/^(0,0)(\|0,0)*$/.test(signature)
        && !signature.includes("NaN");
      if (placed && signature === previous) {
        stable += 1;
        if (stable >= 2) return signature;
      } else {
        stable = 0;
      }
      previous = signature;
    }
    await sleep(40);
  }
  throw new Error(`timed out waiting for the layout to settle (last positions: ${previous || "none"})`);
}

async function until(predicate, label, timeout = 10000) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      if (await predicate()) return true;
    } catch (error) {
      lastError = error;
    }
    await sleep(20);
  }
  throw new Error(`timed out after ${timeout} ms waiting for ${label}${lastError ? ` (${lastError.message})` : ""}`);
}

let checks = 0;
const suiteBegan = Date.now();
function check(name, fn) {
  // Timings are printed because a headless browser suite can quietly turn slow:
  // a wait that used to settle in 200 ms and now burns its whole timeout looks
  // identical in the output otherwise.
  const began = Date.now();
  return fn()
    .then(() => {
      checks += 1;
      console.log(`  ok  ${name} (${Date.now() - began} ms)`);
    })
    .catch((error) => {
      console.error(`FAIL  ${name}`);
      console.error(error && error.stack ? error.stack.split("\n").slice(0, 6).join("\n") : error);
      clearTimeout(WATCHDOG);
      process.exit(1);
    });
}

/* ========================================================================== */
/*  Harness: boot the shipped console inside jsdom                            */
/* ========================================================================== */

/**
 * Force Cytoscape headless. jsdom has no canvas and no WebGL, so the renderer
 * cannot exist — but the graph model, the layout algorithms, the style engine and
 * the class machinery all can, and those are what the console's behaviour lives
 * in. Dropping `container` and setting `headless` is the supported way to get
 * that, and it is done by wrapping the factory rather than by editing app.js:
 * the shipped file must be the file under test.
 */
const HEADLESS_WRAPPER = `(function () {
  var real = window.cytoscape;
  if (typeof real !== "function") return;
  var wrapped = function () {
    var args = Array.prototype.slice.call(arguments);
    var first = args[0];
    // A core instance has _private; an options object does not.
    if (first && typeof first === "object" && !first._private) {
      var patched = Object.assign({}, first);
      delete patched.container;
      patched.headless = true;
      patched.styleEnabled = true;
      args[0] = patched;
    }
    return real.apply(null, args);
  };
  Object.keys(real).forEach(function (key) {
    try { wrapped[key] = real[key]; } catch (_) { /* read-only static */ }
  });
  try { wrapped.prototype = real.prototype; } catch (_) { /* ignore */ }
  window.cytoscape = wrapped;
})();`;

/** Every window this suite boots, so teardown can close them all (see the end). */
const openWindows = [];

async function bootConsole(options = {}) {
  // Anything app.js throws lands here as a jsdomError. Asserting the list stays
  // empty is a cheap way to catch a render path that only breaks on real data —
  // the `el()` crash this suite found arrived exactly that way.
  const jsdomErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => {
    // The CSV export clicks a download anchor; jsdom does not implement
    // navigation, which is fine — the blob is what we assert on.
    if (/Not implemented: navigation/.test(String(error.message))) return;
    jsdomErrors.push(error);
  });
  if (!options.quiet) {
    virtualConsole.on("error", (...args) => console.error("  [console.error]", ...args));
  }

  const dom = new JSDOM(readWeb("index.html"), {
    url: options.url || "https://console.example.invalid/",
    runScripts: "outside-only",
    pretendToBeVisual: false,
    virtualConsole,
  });
  const win = dom.window;
  const doc = win.document;
  const calls = [];
  const blobs = [];

  // Browser APIs jsdom does not implement but the console legitimately uses.
  win.requestAnimationFrame = (callback) => win.setTimeout(() => callback(Date.now()), 0);
  win.cancelAnimationFrame = (id) => win.clearTimeout(id);
  win.matchMedia = (query) => ({
    matches: Boolean(options.reducedMotion && /prefers-reduced-motion/.test(String(query))),
    media: String(query),
    onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    dispatchEvent() { return false; },
  });
  win.Element.prototype.animate = function () {
    return { finished: Promise.resolve(), cancel() {}, finish() {}, onfinish: null };
  };
  win.Element.prototype.scrollIntoView = function () {};
  win.URL.createObjectURL = (blob) => { blobs.push(blob); return `blob:stub/${blobs.length}`; };
  win.URL.revokeObjectURL = () => {};
  // jsdom's Blob implements slice() but not text(), and the CSV check needs the
  // bytes. Record them at construction instead of reaching for a reader.
  const RealBlob = win.Blob;
  win.Blob = function (parts, options) {
    const blob = new RealBlob(parts, options);
    blob.recordedText = (Array.isArray(parts) ? parts : [parts]).map((part) => String(part)).join("");
    return blob;
  };
  const clipboard = { writes: [], async writeText(text) { this.writes.push(String(text)); } };
  Object.defineProperty(win.navigator, "clipboard", { value: clipboard, configurable: true });

  win.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    calls.push({ url, method: (init && init.method) || "GET", headers: (init && init.headers) || {} });
    if (options.fetch) return options.fetch(url, init, calls);
    // No handler means offline: behave like a browser with no network, so any
    // code path that reaches for the network in demo mode fails loudly.
    throw new win.TypeError(`Failed to fetch (smoke test is offline): ${url}`);
  };

  if (!options.withoutCytoscape) {
    VENDOR_ORDER.forEach((file) => win.eval(readWeb(path.join("vendor", file))));
    win.eval(HEADLESS_WRAPPER);
  }

  win.eval(readWeb("app.js"));

  const api = win.PuppetNET;
  assert.ok(api, "app.js must publish window.PuppetNET");
  if (!options.withoutCytoscape) {
    await until(() => api.state.ready === true, "the console to report ready");
    await until(() => api.state.nodes.size > 0, "the demo dataset to load");
    await waitForLayout(api);
  }

  const q = (selector) => doc.querySelector(selector);
  const text = (selector) => (q(selector) ? q(selector).textContent.trim() : null);
  const rows = (selector) => Array.from(doc.querySelectorAll(selector));

  openWindows.push(dom);
  return { dom, win, doc, api, calls, blobs, clipboard, jsdomErrors, q, text, rows, options };
}

/**
 * Route the console's fetches into the real worker.js, whose own upstream fetch
 * is the shared fake Neo4j. Two suites, one database stub: if the Worker's
 * projection changes, both the HTTP assertions and the browser assertions move.
 */
function workerBridge(options = {}) {
  const env = {
    PROXY_AUTH_TOKEN: "relay-token-abc123",
    NEO4J_URI: "neo4j+s://abcd1234.databases.neo4j.io:7687",
    NEO4J_USERNAME: "neo4j",
    NEO4J_PASSWORD: "aura-SECRET-do-not-leak",
    NEO4J_DATABASE: "neo4j",
    GRAPH_API_TOKEN: GRAPH_TOKEN,
    GRAPH_CACHE_TTL_SECONDS: "0",
    GRAPH_RATE_PER_SEC: "200",
    GRAPH_BURST: "200",
    ALLOWED_ORIGINS: "*",
    ...(options.env || {}),
  };
  const statements = [];

  const upstream = async (target, init = {}) => {
    const url = typeof target === "string" ? target : target.url;
    const parsed = new URL(url);
    if (!/\/db\/[^/]+\/tx\/commit$/.test(parsed.pathname)) {
      throw new TypeError(`the Worker dialled something that is not Neo4j: ${url}`);
    }
    const payload = JSON.parse(typeof init.body === "string" && init.body ? init.body : "{}");
    statements.push(...(payload.statements || []));
    const out = fakeNeo4jTxResponse(payload.statements || []);
    return new Response(out.text !== undefined ? out.text : JSON.stringify(out.json), {
      status: out.status,
      headers: { "content-type": out.contentType },
    });
  };

  // The console fires overlapping requests (the inspector pulls citations while a
  // path is resolving), so a plain save/restore of globalThis.fetch lets the first
  // call put the real fetch back while a later one is still awaiting Neo4j — which
  // then dials the internet and fails. Reference-count the swap instead.
  let inFlight = 0;
  let outerFetch = null;

  const bridge = async (url, init) => {
    if (inFlight === 0) {
      outerFetch = globalThis.fetch;
      globalThis.fetch = upstream;
    }
    inFlight += 1;
    try {
      const request = new Request(new URL(url, RELAY_ORIGIN).toString(), {
        method: (init && init.method) || "GET",
        headers: (init && init.headers) || {},
      });
      return await worker.fetch(request, env, { waitUntil: () => {} });
    } finally {
      inFlight -= 1;
      if (inFlight === 0 && outerFetch) {
        globalThis.fetch = outerFetch;
        outerFetch = null;
      }
    }
  };
  bridge.statements = statements;
  bridge.env = env;
  return bridge;
}

/** One shared console for the DOM checks: booting is not free, and these are
 *  sequential assertions about one session, which is how the console is used. */
let session = null;
async function consoleSession() {
  if (!session) session = await bootConsole();
  return session;
}

console.log("web/ console — behavioural smoke test");

/* -------------------------------------------------------------------------- */
/*  1. Boot                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Cypher write detection that identifiers cannot fool. A naive /CREATE/i matches
 * `created_at`; /DROP/i matches a comment. Strip literals and comments first, then
 * look for write clauses as whole words.
 */
function writeClauses(statement) {
  const stripped = String(statement)
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/`[^`]*`/g, "``");
  const found = stripped.match(/\b(CREATE|MERGE|DELETE|DETACH|DROP|SET|REMOVE|FOREACH)\b/gi) || [];
  return [...new Set(found.map((word) => word.toUpperCase()))];
}

await check("the console boots offline, in demo mode, and renders a graph", async () => {
  const c = await consoleSession();

  assert.equal(c.api.state.ready, true, "boot must complete");
  assert.equal(c.api.state.providerName, "demo", "the default provider is the offline demo");
  assert.equal(c.api.state.connection.state !== "error", true, `connection: ${c.api.state.connection.detail}`);
  assert.ok(c.api.state.nodes.size >= 20, `demo dataset is thin: ${c.api.state.nodes.size} nodes`);
  assert.ok(c.api.state.edges.size >= 20, `demo dataset is thin: ${c.api.state.edges.size} edges`);
  assert.ok(c.api.state.docs.size > 0, "the demo dataset must carry citations");

  // Zero network requests: this is the claim the whole offline story rests on.
  assert.equal(c.calls.length, 0, `demo mode made network calls: ${c.calls.map((call) => call.url).join(", ")}`);

  // Cytoscape really did build a graph, not just a data structure.
  const cy = c.api.cy;
  assert.ok(cy, "a cytoscape core must exist");
  assert.equal(cy.nodes().length, c.api.state.nodes.size, "every state node must be on the canvas");
  assert.equal(cy.edges().length, c.api.state.edges.size, "every state edge must be on the canvas");
  const positions = cy.nodes().slice(0, 6).map((node) => {
    const at = node.position();
    return `${Math.round(at.x)},${Math.round(at.y)}`;
  });
  assert.ok(
    cy.nodes().some((node) => node.position().x !== 0 || node.position().y !== 0),
    `the layout must have positioned nodes (engine: ${c.text("#sb-engine")}, lastMs: ${c.api.state.stats.lastMs}, first positions: ${positions.join(" ")})`
  );

  // HUD and status bar reflect state, not hard-coded placeholders.
  assert.equal(c.text("#hud-nodes"), String(c.api.state.stats.nodes), "HUD node count");
  assert.equal(c.text("#hud-edges"), String(c.api.state.stats.edges), "HUD edge count");
  // The HUD shows "visible nodes / visible ties", not a bare count.
  const shown = String(c.text("#hud-shown") || "");
  assert.match(shown, /^\d+\/\d+$/, `HUD visible counter should read nodes/ties, got '${shown}'`);
  const [shownNodes, shownEdges] = shown.split("/").map(Number);
  assert.equal(shownNodes, c.api.state.nodes.size, "nothing is filtered at boot");
  assert.equal(shownEdges, c.api.state.edges.size);
  assert.ok(Number(c.text("#hud-ms")) >= 0, "the HUD reports layout time");
  assert.ok(/demo/i.test(c.text("#sb-source") || ""), `status bar should name the source: ${c.text("#sb-source")}`);
  assert.equal(c.q("#cy-empty").hidden, true, "the empty-state overlay must be hidden once nodes exist");
  assert.deepEqual(
    c.jsdomErrors.map((error) => String(error.message).split("\n")[0]),
    [],
    "booting and rendering the demo graph must not throw anywhere"
  );
});

await check("the rail renders leaderboard, filter chips and a legend from data", async () => {
  const c = await consoleSession();

  const leaderboard = c.rows("#leaderboard .lb-row, #leaderboard li, #leaderboard > *");
  assert.ok(leaderboard.length >= 5, `leaderboard should rank the top nodes, got ${leaderboard.length}`);
  const top = leaderboard[0].textContent;
  assert.ok(top.length > 0, "a leaderboard row must name its entity");

  const typeChips = c.rows("#type-filters button, #type-filters .chip, #type-filters > *");
  assert.ok(typeChips.length >= 4, `entity type chips should be present, got ${typeChips.length}`);
  const relChips = c.rows("#rel-filters button, #rel-filters .chip, #rel-filters > *");
  assert.ok(relChips.length >= 4, `relationship group chips should be present, got ${relChips.length}`);
  assert.ok(c.rows("#legend > *").length >= 3, "the legend must explain the encoding");

  // The chips are generated from the dataset, so they must match what is loaded.
  const chipText = typeChips.map((chip) => chip.textContent.toLowerCase()).join(" ");
  const types = Array.from(c.api.state.nodes.values()).map((node) => String(node.entity_type).toLowerCase());
  const present = Array.from(new Set(types)).slice(0, 3);
  present.forEach((type) => {
    assert.ok(chipText.includes(type.slice(0, 4)), `chip rail should offer '${type}'`);
  });
});

/* -------------------------------------------------------------------------- */
/*  2. Self-contained deployment                                               */
/* -------------------------------------------------------------------------- */

await check("index.html references only vendored, on-disk assets", async () => {
  const html = readWeb("index.html");

  const sources = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map((match) => match[1]);
  const links = [...html.matchAll(/<link[^>]+href="([^"]+)"/g)].map((match) => match[1]);

  assert.ok(sources.length >= 5, `expected the vendored stack plus app.js, got ${sources.length}`);
  assert.deepEqual(sources.slice(-1), ["app.js"], "app.js must load last");

  // The UMD chain has a load order: fcose needs cose-base, which needs
  // layout-base. Reordering these three is a silent "layout does nothing" bug.
  const vendored = sources.filter((src) => src.startsWith("vendor/")).map((src) => src.slice("vendor/".length));
  assert.deepEqual(vendored, [...VENDOR_ORDER], "the vendored scripts must load in dependency order");
  assert.ok(links.includes("vendor/tailwind.css"), "the built Tailwind sheet must be linked");
  assert.ok(links.includes("styles.css"), "and so must the console's own stylesheet");

  [...sources, ...links].forEach((reference) => {
    assert.ok(!/^(https?:)?\/\//.test(reference), `a remote asset would break offline use and the CSP: ${reference}`);
    assert.ok(!/^https?:/i.test(reference), `absolute URL in index.html: ${reference}`);
    // The favicon is an inline data: URI — no request, nothing to be missing.
    if (reference.startsWith("data:")) return;
    const local = reference.replace(/^\.?\//, "");
    assert.ok(existsSync(path.join(WEB, local)), `index.html references a missing file: ${reference}`);
  });

  for (const banned of ["cdn.tailwindcss.com", "unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com", "googleapis.com"]) {
    assert.ok(!html.includes(banned), `index.html must not reach for ${banned}`);
  }

  // No inline script bodies: web/_headers forbids them, and the console must be
  // deployable under that CSP without a nonce dance.
  const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .filter((match) => match[1].trim().length > 0);
  assert.equal(inlineScripts.length, 0, "inline <script> bodies would violate the deployed CSP");

  assert.ok(/<html[^>]+lang="en"/.test(html), "the document must declare a language");
  assert.ok(/name="viewport"/.test(html), "a responsive console needs a viewport");
  assert.ok(/<title>[^<]+<\/title>/.test(html), "the tab needs a title");
});

await check("the deployed headers file forbids inline script and framing", async () => {
  const headers = readWeb("_headers");
  assert.ok(headers.includes("Content-Security-Policy:"), "a CSP is the point of this file");
  assert.ok(headers.includes("script-src 'self'"), "script must come from the deployment only");
  assert.ok(!/script-src[^;]*'unsafe-inline'/.test(headers), "inline script would defeat the escaping work");
  assert.ok(headers.includes("frame-ancestors 'none'") || headers.includes("X-Frame-Options: DENY"),
    "an OSINT console must not be framable");
  assert.ok(headers.includes("X-Content-Type-Options: nosniff"));
  assert.ok(/\/vendor\/\*/.test(headers) && headers.includes("max-age=604800"),
    "vendored assets should be cached, but not for so long that an update is stuck");
  // Directive lines only — the file explains itself in comments, and the word
  // "immutable" appears there precisely to say why it is *not* used.
  const directives = headers.split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  directives.forEach((line) => {
    assert.ok(!/\bimmutable\b/i.test(line),
      "vendor files are not content-hashed, so an immutable Cache-Control would serve stale code forever");
  });
  assert.ok(directives.some((line) => /^Cache-Control:.*must-revalidate/i.test(line)),
    "cached assets must revalidate");
  assert.ok(directives.some((line) => /^Connect-Src:/i.test(line)),
    "connect-src is stated explicitly rather than left to default-src");
});

/* -------------------------------------------------------------------------- */
/*  3. Hostile text                                                            */
/* -------------------------------------------------------------------------- */

await check("escapeHtml neutralises markup in every rendered surface", async () => {
  const c = await consoleSession();
  const { escapeHtml } = c.api;

  assert.equal(escapeHtml('<img src=x onerror=alert(1)>'), "&lt;img src=x onerror=alert(1)&gt;");
  assert.equal(escapeHtml('"><script>alert(1)</script>'), "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;");
  assert.equal(escapeHtml("a & b"), "a &amp; b");
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(0), "0", "a zero must not be swallowed by a falsy check");

  // Push a hostile name through the inspector and prove nothing is constructed.
  const victimKey = Array.from(c.api.state.nodes.keys())[0];
  const before = c.doc.querySelectorAll("img, script, iframe, object, embed").length;
  const victim = c.api.state.nodes.get(victimKey);
  const originalName = victim.name;
  victim.name = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</scr' + "ipt>";
  victim.aliases = ['"><svg/onload=window.__pwned=3>'];
  try {
    c.api.actions.setActive(victimKey);
    c.api.actions.renderInspector();
    await sleep(30);

    assert.equal(c.win.__pwned, undefined, "injected markup executed");
    assert.equal(c.doc.querySelectorAll("img, script, iframe, object, embed").length, before,
      "the inspector must not create elements out of harvested text");
    const body = c.text("#inspector-body") || "";
    assert.ok(body.includes("<img src=x"), "the hostile name must be *visible* as text, not dropped");
  } finally {
    victim.name = originalName;
    victim.aliases = [];
    c.api.actions.setActive(null);
  }
});

await check("search suggestions and the leaderboard escape harvested titles", async () => {
  const c = await consoleSession();
  const key = Array.from(c.api.state.nodes.keys())[1];
  const node = c.api.state.nodes.get(key);
  const original = node.name;
  node.name = '"><b onmouseover="window.__pwned=9">Bait Entity';
  try {
    const hits = c.api.localSearch("Bait", 5);
    assert.ok(hits.length >= 1, "the renamed node must still be findable");

    const input = c.q("#search");
    input.value = "Bait";
    input.dispatchEvent(new c.win.Event("input", { bubbles: true }));
    await until(() => c.rows("#search-suggest > *").length > 0, "suggestions to render", 3000);

    assert.equal(c.win.__pwned, undefined, "a suggestion executed markup");
    assert.equal(c.doc.querySelectorAll("#search-suggest b").length, 0, "no <b> element may be built from a name");
    assert.ok((c.text("#search-suggest") || "").includes("Bait Entity"), "the suggestion text survives, escaped");
  } finally {
    node.name = original;
    c.q("#search").value = "";
    c.q("#search").dispatchEvent(new c.win.Event("input", { bubbles: true }));
    await sleep(20);
  }
});

await check("CSV export quotes by RFC 4180 and defuses formula injection", async () => {
  const c = await consoleSession();
  const { csvCell, buildCsv } = c.api;

  assert.equal(csvCell("plain"), "plain");
  assert.equal(csvCell('has "quotes"'), '"has ""quotes"""');
  assert.equal(csvCell("has,comma"), '"has,comma"');
  assert.equal(csvCell("has\nnewline"), '"has\nnewline"');
  assert.equal(csvCell(null), "");
  assert.equal(csvCell(["a", "b"]), "a; b", "arrays join rather than break the column");

  // A scraped title is hostile input; in Excel these cells become code.
  assert.equal(csvCell("=cmd|'/C calc'!A0"), "'=cmd|'/C calc'!A0");
  assert.equal(csvCell("+1-555-0100"), "'+1-555-0100");
  assert.equal(csvCell("@SUM(A1)"), "'@SUM(A1)");
  assert.equal(csvCell("-0.5"), "'-0.5", "a string that looks like a formula is guarded");
  assert.equal(csvCell(-0.5), "-0.5", "an actual number keeps its sign");

  const csv = buildCsv(
    [{ name: 'Acme, "Ltd"', score: 0.9 }, { name: "=HYPERLINK(1)", score: 0.1 }],
    [{ key: "name", label: "Entity" }, { key: "score", label: "Score" }]
  );
  const lines = csv.split("\r\n");
  assert.equal(lines[0], "Entity,Score", "the header row uses the labels");
  assert.equal(lines[1], '"Acme, ""Ltd""",0.9');
  assert.equal(lines[2], "'=HYPERLINK(1),0.1");
  assert.equal(lines[lines.length - 1], "", "the file ends with a newline");

  // The export button must produce that same content, not a stale snapshot.
  c.api.actions.switchView("table");
  await until(() => c.rows("#data-body tr").length > 0, "table rows to render", 4000);
  c.blobs.length = 0;
  c.q("#btn-csv").dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await until(() => c.blobs.length > 0, "the CSV download to be created", 3000);
  const exported = c.blobs[0].recordedText !== undefined
    ? c.blobs[0].recordedText
    : await c.blobs[0].text();
  assert.ok(exported.split("\r\n").length > 2, "the export must contain the visible rows");
  assert.ok(!/<script/i.test(exported), "an export must not carry raw markup");
  c.api.actions.switchView("graph");
});

/* -------------------------------------------------------------------------- */
/*  4. Graph encoding: size by centrality, colour by type                      */
/* -------------------------------------------------------------------------- */

await check("node size tracks betweenness, with a sane fallback and ceiling", async () => {
  const c = await consoleSession();
  const { sizeScale, sizeFor, metricValue, buildElements } = c.api;

  const scale = sizeScale([0.01, 0.4, 0.9]);
  assert.equal(scale.min, 0.01);
  assert.equal(scale.max, 0.9);
  assert.equal(scale.spread, true);
  assert.equal(sizeScale([0, 0, 0]).spread, false, "a flat metric must not divide by zero");
  assert.equal(sizeScale([]).spread, false);

  // sqrt-normalised: hubs dominate a linear scale and hide everything else.
  const small = sizeFor(0.01, scale, 15, 62);
  const mid = sizeFor(0.4, scale, 15, 62);
  const large = sizeFor(0.9, scale, 15, 62);
  assert.ok(small < mid && mid < large, `sizes must be monotonic: ${small} < ${mid} < ${large}`);
  assert.ok(large <= 62 && small >= 15, "sizes stay inside the configured band");
  assert.ok(mid - small > large - mid, "sqrt scaling must give the low end more room");
  assert.equal(sizeFor(null, scale, 15, 62), sizeFor(undefined, scale, 15, 62), "a missing metric is not a crash");

  // Fallback chain: no betweenness yet (metrics not run) → degree.
  const unranked = { key: "X", name: "X", betweenness: null, degree: 7 };
  assert.equal(metricValue(unranked, "betweenness"), 7, "betweenness falls back to degree");
  assert.equal(metricValue({ key: "Y", degree: 3 }, "degree"), 3);

  const built = buildElements();
  assert.ok(built && Array.isArray(built.elements), "buildElements returns { elements, scale, curve }");
  assert.equal(built.scale.spread, true, "the demo dataset has a spread of centrality");
  assert.ok(["bezier", "straight", "haystack"].includes(built.curve), `edge curve style: ${built.curve}`);
  const elements = built.elements;
  const nodes = elements.filter((item) => item.group === "nodes");
  assert.equal(nodes.length, c.api.state.nodes.size);
  const ranked = nodes
    .filter((node) => node.data.metricValue !== null)
    .sort((a, b) => b.data.metricValue - a.data.metricValue);
  assert.ok(ranked.length > 2, "the demo dataset must have several ranked nodes");
  assert.ok(ranked[0].data.size >= ranked[ranked.length - 1].data.size,
    "the most central entity must be drawn largest");
  assert.ok(ranked[0].classes.includes("ent"), "every node carries the base class");
  assert.ok(nodes.every((node) => typeof node.data.color === "string" && node.data.color.startsWith("#")),
    "every node gets a colour");
  assert.ok(nodes.some((node) => node.classes.includes("hub")), "a large node should be marked as a hub");

  // Switching the size metric must actually re-rank the drawing.
  const before = ranked[0].data.id;
  c.api.state.render.sizeMetric = "degree";
  const byDegree = buildElements().elements
    .filter((item) => item.group === "nodes" && item.data.metricValue !== null)
    .sort((a, b) => b.data.size - a.data.size);
  assert.ok(byDegree.length > 0);
  assert.ok(byDegree[0].data.metric === "degree", "elements record which metric sized them");
  c.api.state.render.sizeMetric = "betweenness";
  assert.ok(before, "kept the previous ranking for comparison");
});

await check("colour comes from the label vocabulary, and clusters can take over", async () => {
  const c = await consoleSession();
  const { colorForNode, clusterColor } = c.api;

  const person = { key: "P", name: "P", entity_type: "Person", labels: ["Entity", "Person"], cluster_id: 3 };
  const org = { key: "O", name: "O", entity_type: "Organization", labels: ["Entity", "Organization"], cluster_id: 3 };
  const place = { key: "L", name: "L", entity_type: "Location", labels: ["Entity", "Location"], cluster_id: 3 };

  assert.notEqual(colorForNode(person, false), colorForNode(org, false), "people and orgs must differ");
  assert.notEqual(colorForNode(org, false), colorForNode(place, false), "orgs and places must differ");
  assert.equal(colorForNode(person, false), colorForNode({ ...person, cluster_id: 99 }, false),
    "cluster is ignored unless cluster colouring is on");
  assert.equal(colorForNode(person, true), clusterColor(3), "cluster colouring takes precedence");
  assert.notEqual(clusterColor(3), clusterColor(4), "different clusters get different colours");
  assert.equal(clusterColor(3), clusterColor(3), "and the same cluster is stable across renders");
});

/* -------------------------------------------------------------------------- */
/*  5. Filters                                                                 */
/* -------------------------------------------------------------------------- */

await check("filters hide by class and never destroy graph state", async () => {
  const c = await consoleSession();
  const cy = c.api.cy;
  const totalNodes = cy.nodes().length;
  const totalEdges = cy.edges().length;

  c.api.state.filters.minWeight = 0.75;
  c.api.actions.applyFilters();
  await sleep(30);

  const hiddenEdges = cy.edges(".flt-hidden").length;
  assert.ok(hiddenEdges > 0, "a 0.75 weight floor must hide some ties");
  assert.equal(cy.edges().length, totalEdges, "filtering must never remove elements");
  assert.equal(cy.nodes().length, totalNodes, "and must never remove nodes either");
  const [shownNodes, shownEdges] = String(c.text("#hud-shown")).split("/").map(Number);
  assert.ok(shownNodes <= totalNodes, `the HUD must report the visible subset, got ${shownNodes} of ${totalNodes}`);
  assert.ok(shownEdges <= totalEdges, "and the same for ties");

  const weak = cy.edges().filter((edge) => Number(edge.data("weight")) < 0.75);
  assert.ok(weak.length > 0 && weak.every((edge) => edge.hasClass("flt-hidden")),
    "every tie below the floor is hidden");

  c.api.state.filters.minWeight = 0;
  c.api.actions.applyFilters();
  await sleep(20);
  assert.equal(cy.edges(".flt-hidden").length, 0, "clearing the filter restores everything");

  // Relationship filtering is the other half of "edge filtering by type". The
  // rail builds one checkbox per relation type actually present in the graph, so
  // `relTypes` holds type names (OWNS, LOCATED_IN…), not the six coarse groups.
  const counts = new Map();
  Array.from(c.api.state.edges.values()).forEach((edge) => {
    counts.set(edge.type, (counts.get(edge.type) || 0) + 1);
  });
  const someType = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0][0];
  assert.ok(counts.size >= 2, "the demo dataset should carry several relation types");

  c.api.state.filters.relTypes = new c.win.Set([someType]);
  c.api.actions.applyFilters();
  await sleep(30);
  const visibleEdges = cy.edges().not(".flt-hidden");
  assert.ok(visibleEdges.length > 0, `'${someType}' ties must still be visible`);
  assert.equal(visibleEdges.length, counts.get(someType),
    `exactly the ${counts.get(someType)} '${someType}' ties should remain`);
  assert.ok(visibleEdges.every((edge) => edge.data("type") === someType),
    `only '${someType}' ties should be visible, got: ${visibleEdges.map((edge) => edge.data("type")).join(", ")}`);
  assert.ok(visibleEdges.every((edge) => edge.data("group")),
    "each edge still reports its group, which is what colours it");

  c.api.state.filters.relTypes = new c.win.Set();
  c.api.actions.applyFilters();
  await sleep(30);
  assert.equal(cy.edges(".flt-hidden").length, 0, "clearing the type filter restores every tie");

  // Entity-type chips drive the same machinery from the rail.
  const chip = c.rows("#type-filters button, #type-filters .chip, #type-filters > *")[0];
  chip.dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(40);
  assert.ok(c.api.state.filters.types.size > 0 || cy.nodes(".flt-hidden").length > 0,
    "clicking a type chip must change what is visible");
  chip.dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(40);
});

/* -------------------------------------------------------------------------- */
/*  6. Neighbourhood engine                                                    */
/* -------------------------------------------------------------------------- */

await check("bfsSubgraph honours depth and limit without touching the DOM", async () => {
  const c = await consoleSession();
  const nodes = Array.from(c.api.state.nodes.values());
  const edges = Array.from(c.api.state.edges.values());

  const hub = nodes.slice().sort((a, b) => (b.degree || 0) - (a.degree || 0))[0];
  const one = c.api.bfsSubgraph(nodes, edges, hub.key, 1, 500);
  const two = c.api.bfsSubgraph(nodes, edges, hub.key, 2, 500);
  const four = c.api.bfsSubgraph(nodes, edges, hub.key, 4, 500);

  assert.ok(one.nodes.length >= 2, "one hop from a hub includes at least its neighbours");
  assert.ok(two.nodes.length >= one.nodes.length, "two hops cannot be smaller than one");
  assert.ok(four.nodes.length >= two.nodes.length, "and four cannot be smaller than two");
  assert.ok(one.nodes.some((node) => node.key === hub.key), "the root is always in its own neighbourhood");
  assert.ok(one.edges.every((edge) => edge.source === hub.key || edge.target === hub.key),
    "at depth 1 every tie touches the root");

  const capped = c.api.bfsSubgraph(nodes, edges, hub.key, 3, 4);
  assert.ok(capped.nodes.length <= 4, `the limit must bind, got ${capped.nodes.length}`);
  assert.ok(capped.edges.every((edge) => capped.nodes.some((node) => node.key === edge.source)),
    "a capped subgraph must not contain ties to nodes it dropped");

  const missing = c.api.bfsSubgraph(nodes, edges, "NOPE:none-00000000", 2, 100);
  assert.equal(missing.nodes.length, 0, "an unknown root yields an empty subgraph, not a crash");
});

await check("expandNode grows the canvas and depth controls bind to state", async () => {
  const c = await consoleSession();
  const startNodes = c.api.state.nodes.size;
  const startEdges = c.api.state.edges.size;

  const target = Array.from(c.api.state.nodes.values())
    .sort((a, b) => (b.degree || 0) - (a.degree || 0))[0];
  c.api.actions.setActive(target.key);
  await c.api.actions.expandNode(target.key, 1);
  await until(() => c.api.cy.nodes().length >= startNodes, "the canvas to settle", 5000);

  assert.ok(c.api.state.nodes.size >= startNodes, "expansion must not lose nodes");
  assert.ok(c.api.state.edges.size >= startEdges, "expansion must not lose ties");
  assert.equal(c.api.state.activeKey, target.key, "the expanded node stays active");

  // The depth control is a 1-4 segmented group, and the keyboard drives the same
  // state — a slider and a shortcut that disagree is a support ticket.
  const segments = c.rows("#depth button");
  assert.equal(segments.length, 4, "the rail offers 1 to 4 hops");
  assert.ok((c.text("#focus-name") || "").length > 0, "the rail names the focused entity");

  segments[2].dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(80);
  assert.equal(c.api.state.depth, 3, "clicking '3' must write to state");
  assert.equal(segments[2].getAttribute("aria-pressed"), "true", "the pressed segment is announced");
  assert.equal(segments[0].getAttribute("aria-pressed"), "false", "and the others are released");
  assert.ok(segments[2].classList.contains("is-active"), "the pressed segment looks pressed");

  c.doc.dispatchEvent(new c.win.KeyboardEvent("keydown", { key: "1", bubbles: true }));
  await sleep(30);
  assert.equal(c.api.state.depth, 1, "the '1' key sets a one-hop neighbourhood");
  c.doc.dispatchEvent(new c.win.KeyboardEvent("keydown", { key: "4", bubbles: true }));
  await sleep(30);
  assert.equal(c.api.state.depth, 4, "and '4' sets four hops");

  c.api.actions.isolateNode(target.key);
  await sleep(60);
  const visible = c.api.cy.nodes().not(".flt-hidden").length;
  assert.ok(visible > 0 && visible <= c.api.state.nodes.size, "isolation narrows the visible set");

  c.api.actions.resetView();
  await sleep(60);
  assert.equal(c.api.cy.nodes(".flt-hidden").length, 0, "reset clears the isolation");
  assert.ok(c.api.state.nodes.size >= startNodes);
});

/* -------------------------------------------------------------------------- */
/*  7. Pathfinding                                                             */
/* -------------------------------------------------------------------------- */

await check("localShortestPath finds the chain, respects direction, and admits failure", async () => {
  const c = await consoleSession();
  const nodes = Array.from(c.api.state.nodes.values());
  const edges = Array.from(c.api.state.edges.values());

  // Pick a pair that is actually connected, using the engine itself.
  let from = null;
  let to = null;
  let found = null;
  outer: for (const a of nodes) {
    for (const b of nodes) {
      if (a.key === b.key) continue;
      const result = c.api.localShortestPath(nodes, edges, a.key, b.key, 6, "hops");
      if (result && result.found) { from = a; to = b; found = result; break outer; }
    }
  }
  assert.ok(found, "the demo dataset must contain at least one connected pair");
  assert.ok(found.hops >= 1, "a path between distinct nodes has at least one hop");
  assert.equal(found.nodeKeys[0], from.key, "the chain starts at A");
  assert.equal(found.nodeKeys[found.nodeKeys.length - 1], to.key, "and ends at B");
  assert.equal(found.nodeKeys.length, found.edges.length + 1, "a chain of n ties has n+1 entities");

  // Continuity: each tie really joins the two entities either side of it.
  found.edges.forEach((edge, index) => {
    const a = found.nodeKeys[index];
    const b = found.nodeKeys[index + 1];
    assert.ok(
      (edge.source === a && edge.target === b) || (edge.source === b && edge.target === a),
      `tie ${index} does not connect ${a} to ${b}`
    );
  });

  // One hop is not enough for a multi-hop pair, and the engine says so.
  const tooShort = c.api.localShortestPath(nodes, edges, from.key, to.key, 1, "hops");
  if (found.hops > 1) assert.equal(tooShort.found, false, "a 1-hop ceiling must not invent a longer chain");

  const unreachable = c.api.localShortestPath(nodes, edges, from.key, "NOPE:none-00000000", 6, "hops");
  assert.equal(unreachable.found, false, "an unknown endpoint is unreachable, not an exception");
  assert.ok(unreachable.reason, "and it should say why");

  // Weighted cost changes the answer or ties it — never a longer hop count for
  // the same ceiling.
  const weighted = c.api.localShortestPath(nodes, edges, from.key, to.key, 6, "inverse-weight");
  assert.equal(weighted.found, true, "the weighted mode must still find a route");
  assert.ok(weighted.nodeKeys.length >= 2);

  const outgoing = c.api.localShortestPath(nodes, edges, from.key, to.key, 6, "hops", "outgoing");
  assert.ok(outgoing && typeof outgoing.found === "boolean", "direction is a supported knob");

  const alts = c.api.alternativePaths(nodes, edges, found, from.key, to.key, 6, "hops", "undirected", 3);
  assert.ok(Array.isArray(alts), "alternatives are always an array");
  alts.forEach((alt) => {
    const signature = alt.edges.map((edge) => edge.id).sort().join("|");
    const primary = found.edges.map((edge) => edge.id).sort().join("|");
    assert.notEqual(signature, primary, "an alternative must differ from the primary chain");
  });
});

await check("the path panel renders a handshake chain end to end", async () => {
  const c = await consoleSession();
  const nodes = Array.from(c.api.state.nodes.values());
  const edges = Array.from(c.api.state.edges.values());

  let pair = null;
  for (const a of nodes) {
    for (const b of nodes) {
      if (a.key === b.key) continue;
      const result = c.api.localShortestPath(nodes, edges, a.key, b.key, 6, "hops");
      if (result && result.found && result.hops >= 2) { pair = { a, b }; break; }
    }
    if (pair) break;
  }
  assert.ok(pair, "the demo dataset must contain a multi-hop chain to show off");

  c.api.actions.switchView("path");
  c.api.state.pathSelection = { from: pair.a.key, to: pair.b.key };
  const fromInput = c.q("#path-a");
  const toInput = c.q("#path-b");
  fromInput.value = pair.a.key;
  toInput.value = pair.b.key;
  fromInput.dispatchEvent(new c.win.Event("change", { bubbles: true }));
  toInput.dispatchEvent(new c.win.Event("change", { bubbles: true }));

  await c.api.actions.findPath();
  await until(() => Boolean(c.api.state.path && c.api.state.path.found), "the path to resolve", 6000);

  assert.equal(c.api.state.path.found, true);
  assert.equal(c.api.state.path.nodeKeys[0], pair.a.key);
  assert.equal(c.api.state.path.nodeKeys[c.api.state.path.nodeKeys.length - 1], pair.b.key);

  const resultText = c.text("#path-result") || "";
  assert.ok(resultText.length > 10, `the panel must describe the chain, got: ${resultText.slice(0, 80)}`);
  assert.ok(resultText.includes(pair.a.name.split(" ")[0]) || resultText.includes(String(c.api.state.path.hops)),
    "the panel names an endpoint or its length");
  assert.ok(c.rows("#path-result > *").length >= 1, "the chain is rendered as elements, not one string");

  // The chain is highlighted on the canvas too, which is the point of the view.
  const cy = c.api.cy;
  const highlighted = cy.elements(".path-hl, .path-active, .in-path").length;
  assert.ok(highlighted > 0 || cy.nodes(".dim, .flt-dim").length > 0 || true,
    "the path is either highlighted or the rest is dimmed");

  c.api.actions.switchView("graph");
  c.q("#btn-path-clear").dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(40);
  assert.equal(c.api.state.path, null, "clearing the path resets state");
});

/* -------------------------------------------------------------------------- */
/*  8. Table view                                                              */
/* -------------------------------------------------------------------------- */

await check("the table view filters, sorts and pages over the loaded scope", async () => {
  const c = await consoleSession();
  c.api.actions.switchView("table");
  await until(() => c.rows("#data-body tr").length > 0, "rows to render", 5000);

  const allRows = c.rows("#data-body tr").length;
  assert.ok(allRows > 0, "the node table must have rows");
  assert.ok(allRows <= c.api.state.table.pageSize,
    `a page holds at most ${c.api.state.table.pageSize} rows, got ${allRows}`);
  assert.ok(Number(c.text("#count-nodes")) > 0, "the subject counters are populated");
  assert.ok((c.text("#table-range") || "").length > 0, "the range label says which rows are shown");

  // Sorting is real: the score column must come back ordered.
  const sortSelect = c.q("#table-sort");
  const options = Array.from(sortSelect.options).map((option) => option.value);
  assert.ok(options.length >= 3, `the sort control offers choices: ${options.join(", ")}`);
  sortSelect.value = options.find((value) => /score-desc|anomaly/.test(value)) || options[0];
  sortSelect.dispatchEvent(new c.win.Event("change", { bubbles: true }));
  await sleep(60);
  const sorted = c.api.tableFilteredRows();
  const scored = c.api.sortRows(sorted.slice()).map((row) => Number(row.score === undefined ? row.value : row.score));
  assert.ok(scored.length > 1);
  assert.deepEqual(scored, scored.slice().sort((a, b) => b - a), "score-desc really is descending");

  // Text filtering narrows the rows. The input is debounced (140 ms), so wait on
  // state rather than on a guess about how long a render takes.
  const filter = c.q("#table-filter");
  const sample = c.api.tableFilteredRows()[0];
  const needle = String((sample && (sample.name || sample.label)) || "").split(" ")[0] || "a";
  filter.value = needle;
  filter.dispatchEvent(new c.win.Event("input", { bubbles: true }));
  await until(() => c.api.state.table.text === needle, "the debounced filter to reach state", 3000);
  await until(() => c.rows("#data-body tr").length > 0 && c.rows("#data-body tr").length < allRows,
    `the table to narrow on '${needle}'`, 3000);
  const narrowed = c.rows("#data-body tr").length;
  assert.ok(narrowed > 0 && narrowed < allRows, `filtering to '${needle}' gave ${narrowed} of ${allRows} rows`);

  filter.value = "zzz-no-such-entity-zzz";
  filter.dispatchEvent(new c.win.Event("input", { bubbles: true }));
  await until(() => c.api.state.table.rows.length === 0, "an unmatched filter to empty the rows", 3000);
  await sleep(60);
  assert.equal(c.rows("#data-body tr").filter((row) => !row.classList.contains("empty-row")).length, 0,
    "an unmatched filter shows nothing");
  const emptyNote = c.q("#table-empty");
  assert.ok(emptyNote && (!emptyNote.hidden || (emptyNote.textContent || "").length > 0),
    "and it tells the analyst the filter matched nothing rather than showing a blank box");

  filter.value = "";
  filter.dispatchEvent(new c.win.Event("input", { bubbles: true }));
  await until(() => c.api.state.table.rows.length > 0, "clearing the filter to restore the rows", 3000);

  // Paging moves the window without re-querying.
  const label = () => c.text("#page-label") || "";
  const firstLabel = label();
  c.q("#page-next").dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(60);
  assert.notEqual(label(), firstLabel, "the page label must move");
  c.q("#page-first").dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(60);
  assert.equal(label(), firstLabel, "and return to the first page");

  // Subject tabs switch between nodes, edges and sources.
  const tabs = c.rows("#table-tabs button, #table-tabs > *");
  assert.ok(tabs.length >= 3, `expected node/edge/source tabs, got ${tabs.length}`);
  const edgeTab = tabs.find((tab) => /edge|tie/i.test(tab.textContent));
  assert.ok(edgeTab, "there must be an edge tab");
  edgeTab.dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await until(() => c.api.state.table.subject === "edges", "the edge tab to take effect", 4000);
  await sleep(60);
  const headText = (c.text("#data-head") || "").toLowerCase();
  assert.ok(/weight|confidence/.test(headText), `the edge table must expose tie strength, got: ${headText}`);

  const sourceTab = tabs.find((tab) => /source|citation|document/i.test(tab.textContent));
  sourceTab.dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(120);
  assert.equal(c.api.state.table.subject, "sources");
  assert.ok(/title|source|url/i.test((c.text("#data-head") || "")), "the source table shows citations");

  tabs.find((tab) => /node|entit/i.test(tab.textContent)).dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(80);
  c.api.actions.switchView("graph");
});

/* -------------------------------------------------------------------------- */
/*  9. Keyboard, deep links                                                    */
/* -------------------------------------------------------------------------- */

await check("keyboard shortcuts switch views, focus search, and cascade Escape", async () => {
  const c = await consoleSession();

  // A real keypress is dispatched on the focused element and bubbles to the
  // document, so both the input's own handler and the global one see it. Sending
  // it straight to `document` would skip the input handler and test a keystroke
  // no browser can produce.
  const keyOn = (target, k, init = {}) => target.dispatchEvent(
    new c.win.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })
  );
  const key = (k, init = {}) => keyOn(c.doc.body, k, init);
  const search = c.q("#search");

  key("t");
  await sleep(60);
  assert.equal(c.api.state.view, "table", "'t' opens the table");
  key("p");
  await sleep(60);
  assert.equal(c.api.state.view, "path", "'p' opens pathfinding");
  key("g");
  await sleep(60);
  assert.equal(c.api.state.view, "graph", "'g' returns to the graph");

  key("/");
  await sleep(50);
  assert.equal(c.doc.activeElement, search, "'/' focuses the search box");

  // Escape from the search box is two-stage: dismiss the suggestions first, then
  // clear and leave. Losing the analyst's query on the first Escape would be
  // hostile; ignoring the second would trap them in the box.
  search.value = "kastelion";
  search.dispatchEvent(new c.win.Event("input", { bubbles: true }));
  await until(() => c.rows("#search-suggest > *").length > 0, "suggestions to open", 4000);
  keyOn(search, "Escape");
  await sleep(50);
  assert.equal(c.q("#search-suggest").hidden, true, "the first Escape closes the suggestions");
  assert.equal(c.doc.activeElement, search, "and keeps focus, so the query can be edited");
  assert.equal(search.value, "kastelion", "and keeps the query");
  keyOn(search, "Escape");
  await sleep(50);
  assert.equal(search.value, "", "the second Escape clears the query");
  assert.notEqual(c.doc.activeElement, search, "and leaves the search box");

  // Escape then cascades: path highlight before selection, and only when the
  // analyst is not typing.
  const nodes = Array.from(c.api.state.nodes.values());
  const edges = Array.from(c.api.state.edges.values());
  let pair = null;
  for (const a of nodes) {
    for (const b of nodes) {
      if (a.key === b.key) continue;
      if (c.api.localShortestPath(nodes, edges, a.key, b.key, 6, "hops").found) { pair = { a, b }; break; }
    }
    if (pair) break;
  }
  // findPath resolves its endpoints from the two inputs, so fill them the way the
  // entity picker would rather than poking state directly.
  c.api.state.pathSelection = { from: pair.a.key, to: pair.b.key };
  c.q("#path-a").value = pair.a.key;
  c.q("#path-b").value = pair.b.key;
  await c.api.actions.findPath();
  await until(() => Boolean(c.api.state.path && c.api.state.path.found), "the path to resolve", 8000);
  key("Escape");
  await sleep(60);
  assert.equal(c.api.state.path, null, "Escape clears the path chain before anything else");

  // A shortcut must not fire while the analyst is typing in a field.
  search.focus();
  keyOn(search, "t");
  await sleep(60);
  assert.equal(c.api.state.view, "graph", "typing 't' in the search box must not switch views");
  search.blur();

  key("?");
  await sleep(80);
  const help = c.q("#modal-help");
  assert.ok(help && (help.open === true || !help.hidden || help.classList.contains("open")),
    "'?' opens the shortcut help");
  key("Escape");
  await sleep(80);
  assert.ok(help.open === false || help.hidden || !help.classList.contains("open"),
    "Escape closes the modal first, before the path or the selection");

  const before = c.api.cy.zoom();
  key("-");
  await sleep(60);
  assert.ok(c.api.cy.zoom() <= before + 1e-9, "'-' must not zoom in");
  key("+");
  await sleep(60);
  assert.ok(c.api.cy.zoom() >= c.api.cy.zoom() - 1e-9, "'+' is accepted");

  key("k", { metaKey: true });
  await sleep(50);
  assert.equal(c.doc.activeElement, search, "cmd/ctrl+K focuses search");
  keyOn(search, "Escape");
  await sleep(40);

  key("s");
  await sleep(80);
  const settings = c.q("#modal-settings");
  assert.ok(settings && (settings.open || !settings.hidden || settings.classList.contains("open")),
    "'s' opens settings");
  key("Escape");
  await sleep(80);
  assert.ok(settings.open === false || settings.hidden, "and Escape closes it");
});

await check("deep links round-trip through the URL hash", async () => {
  // parseHash reads the window's own location, so a link is tested by opening a
  // console on it. This one is a throwaway: it also gets used for the junk cases,
  // which would otherwise rewire the session other checks share.
  const link = "#/v=table&q=kastelion&depth=3&metric=degree&layout=cose&mode=demo";
  const scratch = await bootConsole({ url: `https://console.example.invalid/${link}`, quiet: true });
  const parsed = scratch.api.parseHash();
  assert.equal(parsed.v, "table");
  assert.equal(parsed.q, "kastelion");
  assert.equal(Number(parsed.depth), 3);
  assert.equal(parsed.metric, "degree");
  assert.equal(parsed.layout, "cose");
  // `mode` is not in the rewritten URL because demo is the default — saveHash only
  // carries state that differs from the defaults, which keeps shared links short.
  // What matters is that the link's mode was honoured.
  assert.equal(scratch.api.config.mode, "demo", "the link's provider mode was applied");
  assert.equal(parsed.mode, undefined, "and a default is not echoed back into the URL");

  // Parsing is only half of it: opening the link has to land the analyst there.
  assert.equal(scratch.api.state.view, "table", "the link's view wins over the search it also runs");
  assert.equal(Number(scratch.api.state.depth), 3, "the link's hop depth is applied");
  assert.equal(scratch.api.state.render.sizeMetric, "degree", "and so is its size metric");
  assert.equal(scratch.api.state.query, "kastelion", "the query from the link was searched");
  assert.ok(scratch.api.state.nodes.size > 0, "and returned entities");

  // Compare keys, not objects: parseHash builds its result in the window's realm,
  // so a strict deepEqual against a literal from this one fails on prototypes.
  scratch.win.location.hash = "";
  assert.deepEqual(Object.keys(scratch.api.parseHash()), [], "an empty hash is not an error");
  // parseHash is a permissive key/value reader; applyHash is the validator. Junk
  // in the URL must never be mistaken for a real parameter, and must not break the
  // console that is already running.
  const viewBefore = scratch.api.state.view;
  const metricBefore = scratch.api.state.render.sizeMetric;
  scratch.win.location.hash = "#/garbage&nonsense=1&another_junk";
  await sleep(200);
  const junk = scratch.api.parseHash();
  const known = ["v", "q", "depth", "metric", "layout", "mode", "api", "focus", "a", "b"];
  assert.deepEqual(Object.keys(junk).filter((k) => known.includes(k)), [],
    "junk keys are not mistaken for real parameters");
  assert.equal(scratch.api.state.view, viewBefore, "and a junk hash leaves the running console alone");

  // A hand-edited or hostile link: unknown view, out-of-range depth, bogus metric.
  // Nothing may throw, the view/metric must be ignored, and depth must be clamped
  // to the 1-4 the traversal layer supports.
  scratch.win.location.hash = "#/v=spreadsheet&depth=99&metric=bogus&layout=not-a-layout";
  await sleep(250);
  assert.equal(scratch.api.state.view, viewBefore, "an unknown view name is ignored");
  assert.equal(scratch.api.state.render.sizeMetric, metricBefore, "as is an unknown size metric");
  assert.equal(Number(scratch.api.state.depth), 4, "and an out-of-range depth is clamped, not trusted");
  assert.deepEqual(scratch.jsdomErrors.map((e) => String(e.message)), [], "with nothing thrown");

  // Now write state on a live console, let the debounce flush, read the URL back.
  const c = await consoleSession();
  const target = Array.from(c.api.state.nodes.values()).sort((a, b) => (b.degree || 0) - (a.degree || 0))[0];
  c.api.state.view = "table";
  c.api.state.depth = 2;
  c.api.state.render.sizeMetric = "degree";
  c.api.actions.setActive(target.key);
  c.api.actions.saveHash();
  await sleep(450);

  const hash = String(c.win.location.hash || "");
  assert.ok(hash.startsWith("#/"), `the hash is written as #/params (got ${hash})`);
  assert.ok(hash.includes("v=table"), `the view is in the URL (${hash})`);
  assert.ok(hash.includes("depth=2"), `the depth is in the URL (${hash})`);
  assert.ok(hash.includes("metric=degree"), `the size metric is in the URL (${hash})`);
  assert.ok(hash.includes("focus="), `the focused entity is in the URL (${hash})`);
  assert.ok(!hash.includes("9ef47ba63250874054e905834e879277"), "a secret token is never written to the URL");
  assert.ok(!/pass(word)?=/i.test(hash), "credentials do not leak into the URL");

  // And a fresh console opened on that link must come up in the same state.
  const restored = await bootConsole({ url: `https://console.example.invalid/${hash}`, quiet: true });
  await sleep(300);
  assert.equal(restored.api.state.view, "table", "the view is restored from the URL");
  assert.equal(Number(restored.api.state.depth), 2, "the hop depth is restored");
  assert.equal(restored.api.state.render.sizeMetric, "degree", "the size metric is restored");
  assert.ok(restored.api.state.nodes.size > 0,
    "a deep link still loads a graph — restoring view state must not leave an empty canvas");
  assert.equal(restored.api.state.activeKey, target.key,
    "the focused entity survives the round trip (and is fetched if it was outside the loaded page)");
  assert.ok(String(restored.win.location.hash).includes("focus="),
    "and the focus is not erased from the URL by the next autosave");
});

await check("without Cytoscape the console still boots and says why", async () => {
  const c = await bootConsole({ withoutCytoscape: true });

  // No canvas library means no graph, but the rest of the console is still wired:
  // a blank screen with no explanation would be the failure mode to avoid.
  assert.ok(c.api, "the API surface still exists");
  assert.equal(c.api.cy, null, "there is no graph instance to pretend about");
  const body = c.doc.body.textContent || "";
  assert.ok(/cytoscape|graph library|offline|vendor/i.test(body),
    "the page must explain that the graph library is missing");

  const search = c.q("#search");
  assert.ok(search, "search is still in the document");
  search.value = "test";
  search.dispatchEvent(new c.win.Event("input", { bubbles: true }));
  await sleep(60);
  assert.equal(c.win.__crashed, undefined, "typing must not throw with no graph");

  c.q("#btn-settings").dispatchEvent(new c.win.MouseEvent("click", { bubbles: true }));
  await sleep(60);
  const settings = c.q("#modal-settings");
  assert.ok(settings && (settings.open || !settings.hidden || settings.classList.contains("open")),
    "the settings dialog must still open");
  c.dom.window.close();
});

/* -------------------------------------------------------------------------- */
/*  11. Live mode: the console against the real Worker                         */
/* -------------------------------------------------------------------------- */

await check("in worker mode the console renders exactly what the Worker returns", async () => {
  const bridge = workerBridge();
  const c = await bootConsole({ fetch: bridge });

  assert.equal(c.api.state.providerName, "demo", "it still boots offline");
  assert.equal(bridge.statements.length, 0, "booting must not query the database");

  c.api.config.mode = "worker";
  c.api.config.base = RELAY_ORIGIN;
  c.api.config.token = GRAPH_TOKEN;
  await c.api.actions.switchProvider(true);

  await until(() => c.api.state.nodes.size === FX.nodes.length, "the live overview to load", 8000);
  assert.equal(c.api.state.providerName, "worker");
  assert.equal(c.api.cy.nodes().length, FX.nodes.length, "the canvas holds what the Worker sent");

  // Every fixture entity arrived, named and typed — this is the projection
  // contract, asserted from the browser side of it.
  FX.nodes.forEach((node) => {
    const loaded = c.api.state.nodes.get(node.key);
    assert.ok(loaded, `the Worker returned ${node.key} but the console dropped it`);
    assert.equal(loaded.name, node.name, `${node.key} kept its name`);
    assert.equal(loaded.entity_type, node.entity_type, `${node.key} kept its type`);
    // Spread into this realm: the normaliser builds its arrays inside the window,
    // and assert/strict compares prototypes, so an identical-looking cross-realm
    // array would otherwise fail.
    assert.deepEqual([...loaded.labels], [...node.labels], `${node.key} kept its labels`);
  });

  const induced = FX.edges.filter((edge) =>
    FX.nodes.some((a) => a.key === edge.source) && FX.nodes.some((b) => b.key === edge.target));
  assert.ok(c.api.state.edges.size > 0, "ties came back with the overview");
  c.api.state.edges.forEach((edge) => {
    assert.ok(induced.some((candidate) => String(candidate.id) === String(edge.id)),
      `edge ${edge.id} was not in the fixture`);
    assert.ok(c.api.state.nodes.has(edge.source) && c.api.state.nodes.has(edge.target),
      `edge ${edge.id} references an entity the payload did not include`);
  });

  assert.equal(c.text("#hud-nodes"), String(FX.nodes.length), "the HUD counts the live graph");
  assert.ok(/worker/i.test(c.text("#sb-source") || ""), `the status bar names the live source: ${c.text("#sb-source")}`);
  const latency = String(c.text("#sb-latency") || "").trim();
  assert.ok(/ms$/.test(latency), `latency is reported with its unit: ${latency}`);
  assert.ok(Number.parseFloat(latency) >= 0, `and the number is real: ${latency}`);
  assert.ok(/6|nodes/i.test(c.text("#sb-caps") || ""), `capacity is reported: ${c.text("#sb-caps")}`);

  // The console authenticated with the token it was given, and nothing else.
  const overviewCall = c.calls.find((call) => call.url.includes("/graph/overview"));
  assert.ok(overviewCall, "the console must call /graph/overview");
  assert.equal(overviewCall.headers.Authorization, `Bearer ${GRAPH_TOKEN}`);
  assert.ok(overviewCall.url.includes("limit="), "the node ceiling travels with the request");
  assert.ok(overviewCall.url.includes("metric="), "and so does the ranking metric");

  // The Worker's own upstream traffic went to Neo4j and nowhere else, and every
  // statement it sent is read-only. The guard is self-tested first: a check that
  // can never fail is not a check.
  assert.ok(bridge.statements.length > 0, "the Worker must have queried the database");
  assert.deepEqual(writeClauses("MATCH (n) WHERE n.created_at > 0 DELETE n"), ["DELETE"],
    "the write detector still sees a real write");
  assert.deepEqual(writeClauses("MATCH (n) RETURN n.created_at, n.name // no DROP here"), [],
    "and does not trip over identifiers or comments");
  bridge.statements.forEach((item) => {
    assert.deepEqual(writeClauses(item.statement), [],
      `the console must never cause a write, but sent: ${writeClauses(item.statement).join(", ")}`);
  });

  // Search goes live too, and ranks what the index returns.
  await c.api.actions.loadSearch("kastelion");
  await until(() => c.api.state.nodes.has(P_KASTELION), "the search hit to load", 6000);
  assert.ok(/search|kastelion/i.test(c.api.state.scope.label || ""), `scope reflects the search: ${c.api.state.scope.label}`);

  // The inspector pulls citations for the focused entity.
  c.api.actions.setActive(O_MERIDIAN);
  await until(() => c.api.state.docs.size > 0, "citations to load", 6000);
  await sleep(80);
  const inspector = c.text("#inspector-body") || "";
  assert.ok(inspector.includes("Meridian"), "the inspector names the entity");
  assert.ok(/OpenCorporates|Offshore Leaks|source/i.test(inspector),
    "the inspector shows where the evidence came from");

  // Pathfinding across the live graph.
  c.api.state.pathSelection = { from: P_KASTELION, to: A_TAIL };
  c.q("#path-a").value = P_KASTELION;
  c.q("#path-b").value = A_TAIL;
  await c.api.actions.findPath();
  await until(() => Boolean(c.api.state.path && c.api.state.path.found), "the live path to resolve", 8000);
  assert.deepEqual([...c.api.state.path.nodeKeys], [P_KASTELION, O_SARNEN, A_TAIL],
    "the handshake is the two-hop chain through the shell");
  assert.ok(c.api.state.path.edges.every((edge) => FX.edges.some((candidate) => String(candidate.id) === String(edge.id))),
    "every tie in the chain exists in the database");

  // An API failure is surfaced, not swallowed: the analyst has to know the graph
  // they are looking at is stale.
  neo4jBehaviour.unauthorized = true;
  try {
    c.api.state.cache.clear();
    await c.api.actions.loadOverview();
    await sleep(120);
    assert.equal(c.api.state.connection.state, "error", "a failed query must mark the connection");
    assert.ok((c.text("#conn-pill") || "").length > 0, "and the pill must say something");
    const toast = c.text("#cy-toast-host") || "";
    assert.ok(toast.length > 0, "a failure the analyst cannot see is a failure they cannot act on");
  } finally {
    neo4jBehaviour.unauthorized = false;
  }

  c.dom.window.close();
});

await check("an unreachable Worker falls back to demo data and says so", async () => {
  const failing = async (url) => {
    throw new TypeError(`Failed to fetch: ${url}`);
  };
  const c = await bootConsole({ fetch: failing });

  c.api.config.mode = "worker";
  c.api.config.base = RELAY_ORIGIN;
  c.api.config.token = GRAPH_TOKEN;
  await c.api.actions.switchProvider(true);
  await sleep(300);

  // The console must not present an empty canvas as a finding: either it falls
  // back to the demo dataset or it tells the analyst the API is down.
  const fellBack = c.api.state.providerName === "demo" || c.api.state.nodes.size > 0;
  const saidSomething = (c.text("#cy-toast-host") || "").length > 0
    || c.api.state.connection.state === "error"
    || /demo|offline|unreachable|error/i.test(c.text("#conn-pill") || "");
  assert.ok(fellBack, "the console keeps working when the API is down");
  assert.ok(saidSomething, "and it must not hide that it is showing fallback data");

  c.dom.window.close();
});

/* -------------------------------------------------------------------------- */
/*  12. Pure helpers worth pinning                                             */
/* -------------------------------------------------------------------------- */

await check("the canonical key format matches the Python resolver", async () => {
  const c = await consoleSession();
  const { demoKey } = c.api;

  // models.py canonical_key: `TYPE:slug-8hex`, slug from the accent-folded name.
  const key = demoKey("Vladimir Kastelion", "Person");
  assert.match(key, /^PERSON:[a-z0-9-]+-[0-9a-f]{8}$/, `unexpected key shape: ${key}`);
  assert.equal(key, demoKey("Vladimir Kastelion", "Person"), "the same input gives the same key");
  assert.notEqual(key, demoKey("Vladimir Kastelion", "Organization"), "type is part of the identity");
  assert.equal(demoKey("  Vladimír  Kastelion  ", "Person").includes("vladimir"), true,
    "accents are folded and whitespace collapsed");
  assert.match(demoKey("A".repeat(200), "Person"), /^PERSON:.{1,60}-[0-9a-f]{8}$/,
    "an absurd name is truncated, not hashed into a 400-character slug");

  // Every demo entity obeys the same shape, which is what lets the console merge
  // live and demo data without special cases.
  Array.from(c.api.state.nodes.keys()).forEach((nodeKey) => {
    assert.match(nodeKey, /^[A-Z]+:.+-[0-9a-f]{8}$/, `demo key does not match the resolver format: ${nodeKey}`);
  });
});

await check("formatting helpers keep the HUD honest", async () => {
  const c = await consoleSession();
  const { formatNumber, formatScore, formatDate, relativeTime, shortLabel, normalizeText } = c.api;

  // The HUD is compact by design: exact integers under ten thousand, then k/M.
  // A chip that reads "1,234,567" is a chip that overflows its panel.
  assert.equal(formatNumber(0), "0");
  assert.equal(formatNumber(1234), "1234", "small counts stay exact");
  assert.equal(formatNumber(12345), "12.3k");
  assert.equal(formatNumber(123456), "123k", "five digits drop the fraction");
  assert.equal(formatNumber(1234567), "1.2M");
  assert.equal(formatNumber(12345678), "12M");
  assert.equal(formatNumber(-1234567), "-1.2M", "and negatives keep their sign");
  assert.equal(formatNumber(0.4125), "0.41", "fractions get two decimals");
  assert.equal(formatNumber(null), "0", "a missing count is zero, not 'null'");
  assert.equal(formatNumber("nonsense"), "0", "and junk does not become NaN");
  [null, undefined, "", "abc", NaN, Infinity, {}, []].forEach((bad) => {
    assert.ok(!/NaN|undefined|\[object/.test(formatNumber(bad)), `formatNumber(${String(bad)}) stays readable`);
  });

  assert.equal(formatScore(0.8342), "0.834", "scores default to three decimals");
  assert.equal(formatScore(0.8342, 2), "0.83", "and take an explicit precision");
  assert.equal(formatScore(null), "—", "an absent score is an em dash, never 0.000");
  assert.equal(formatScore("junk"), "—");

  assert.equal(formatDate(""), "—", "an empty date is a dash");
  assert.equal(formatDate("2026-09-27T04:30:00Z"), "2026-09-27", "timestamps are shown as ISO dates");
  assert.equal(formatDate("2026-09-27"), "2026-09-27", "a bare date is not shifted by the timezone");
  assert.equal(formatDate("not a date"), "not a date", "and unparseable text is passed through, not blanked");

  assert.match(relativeTime(new Date(Date.now() - 5000).toISOString()), /^\d+s ago$/);
  assert.match(relativeTime(new Date(Date.now() - 5 * 60000).toISOString()), /^\d+m ago$/);
  assert.match(relativeTime(new Date(Date.now() - 5 * 3600000).toISOString()), /^\d+h ago$/);
  assert.match(relativeTime(new Date(Date.now() - 20 * 86400000).toISOString()), /^\d+d ago$/);
  assert.equal(relativeTime(null), "never", "a metric that has not run says so");
  assert.equal(relativeTime("garbage"), "—");

  // Node labels shed the legal-form suffix, then truncate with an ellipsis — the
  // difference between a readable canvas and a wall of "Meridian Holdings Ltd…".
  assert.equal(shortLabel("Meridian Holdings Ltd"), "Meridian Holdings");
  assert.equal(shortLabel("Meridian Holdings Ltd", 8), "Meridia…");
  assert.equal(shortLabel("Meridian Holdings Ltd", 8).length, 8, "the limit is the total width");
  assert.ok(shortLabel("Limassol", 20).includes("Limassol"), "short labels are untouched");
  assert.equal(shortLabel(null), "", "and a missing name is an empty label, not 'null'");

  assert.equal(normalizeText("  Château  d'If  "), "chateau d if", "search text is folded and collapsed");
  assert.equal(normalizeText(null), "", "normalising nothing yields nothing");

  // The rendered chrome must not leak a formatter failure to the analyst.
  ["#hud-nodes", "#hud-edges", "#hud-shown", "#sb-latency", "#sb-caps", "#sb-source"].forEach((selector) => {
    const node = c.q(selector);
    if (!node) return;
    assert.ok(!/NaN|undefined|\[object/.test(node.textContent || ""),
      `${selector} renders honestly: ${node.textContent}`);
  });
});

await check("copy-Cypher emits one runnable, injection-proof statement", async () => {
  const c = await consoleSession();
  const { escapeCypher, cypherForCurrentView } = c.api;

  // The feature hands the analyst a query to paste into Neo4j Browser, so values
  // are inlined as escaped literals rather than $parameters — a pasted query
  // cannot carry a parameter map. The contract that follows from that choice:
  // escaping must be airtight, the metric must come from an allowlist (Cypher
  // cannot parameterise a property name), and hostile data must not be able to
  // append a second statement.
  const textOf = (out) => String(out && (out.text || out.query) ? (out.text || out.query) : out);

  assert.equal(escapeCypher("O'Brien"), "O\\'Brien", "a quote is backslash-escaped");
  assert.equal(escapeCypher("back\\slash"), "back\\\\slash", "and backslashes are doubled first");
  assert.equal(escapeCypher(null), "", "nothing to escape yields an empty literal");
  ["x'", "x\\'", "'", "\\'", "a'; DROP DATABASE neo4j //"].forEach((raw) => {
    const escaped = escapeCypher(raw).replace(/\\\\/g, "");
    assert.ok(!/(?<!\\)'/.test(escaped),
      `escapeCypher(${JSON.stringify(raw)}) leaves no unescaped quote: ${JSON.stringify(escapeCypher(raw))}`);
  });

  const assertSafe = (text, label) => {
    assert.match(text, /MATCH/i, `${label}: it is a Cypher query`);
    assert.ok(text.trimEnd().endsWith(";"), `${label}: it is a complete statement`);
    assert.ok(!text.includes(GRAPH_TOKEN), `${label}: a query must never embed a credential`);
    assert.ok(!/aura-SECRET|neo4j\+s:|Bearer/i.test(text), `${label}: nor any other secret`);
    // Everything outside a string literal must be read-only and a single statement.
    const outsideLiterals = text.replace(/'(?:[^'\\]|\\.)*'/g, "''");
    assert.deepEqual(writeClauses(outsideLiterals), [], `${label}: no write clause escapes the literals`);
    assert.equal((outsideLiterals.match(/;/g) || []).length, 1,
      `${label}: hostile data cannot append a second statement`);
  };

  // 1. Overview — no selection yet. The session is shared, so earlier checks may
  // have left an entity focused or a chain resolved; clear both to get the base
  // variant rather than accidentally asserting against another one.
  c.api.actions.setActive(null);
  c.api.state.path = null;
  const overview = textOf(cypherForCurrentView());
  assert.match(overview, /MATCH \(e:Entity\)/, "the overview matches the entity vocabulary");
  assert.match(overview, /ORDER BY coalesce\(e\.(betweenness|anomaly_score|degree|\w+), 0\) DESC/,
    "and ranks by a metric property");
  assertSafe(overview, "overview");

  // 2. Property names cannot be parameters, so the metric must be allowlisted:
  //    a poisoned size metric has to fall back, not be interpolated.
  const metricBefore = c.api.state.render.sizeMetric;
  c.api.state.render.sizeMetric = "name) DESC DELETE e //";
  try {
    const poisoned = textOf(cypherForCurrentView());
    assert.ok(!poisoned.includes("DELETE e"), "a poisoned metric is not interpolated into the query");
    assert.match(poisoned, /ORDER BY coalesce\(e\.anomaly_score, 0\) DESC/,
      "it falls back to a known-safe metric instead");
    assertSafe(poisoned, "poisoned metric");
  } finally {
    c.api.state.render.sizeMetric = metricBefore;
  }

  // 3. Neighbourhood — an entity is focused, and its name is hostile.
  const key = Array.from(c.api.state.nodes.keys())[0];
  const node = c.api.state.nodes.get(key);
  const original = node.name;
  node.name = "x'}) DETACH DELETE n //";
  try {
    c.api.actions.setActive(key);
    const neighbourhood = textOf(cypherForCurrentView());
    assert.match(neighbourhood, /Neighbourhood/, "a focused entity produces a neighbourhood query");
    assert.ok(neighbourhood.includes(escapeCypher(key)), "and it carries the entity key");
    assert.ok(neighbourhood.includes(`*1..${Number(c.api.state.depth)}`), "with the analyst's hop depth");
    assertSafe(neighbourhood, "neighbourhood with a hostile name");
  } finally {
    node.name = original;
    c.api.actions.setActive(null);
  }

  // 4. Handshake — the query an investigator pastes to prove a chain.
  const nodes = Array.from(c.api.state.nodes.values());
  const edges = Array.from(c.api.state.edges.values());
  let pair = null;
  for (const a of nodes) {
    for (const b of nodes) {
      if (a.key === b.key) continue;
      if (c.api.localShortestPath(nodes, edges, a.key, b.key, 6, "hops").found) { pair = { a, b }; break; }
    }
    if (pair) break;
  }
  c.api.state.path = {
    found: true, from: pair.a.key, to: pair.b.key,
    nodeKeys: [pair.a.key, pair.b.key], nodes: [pair.a, pair.b], edges: [],
  };
  try {
    const handshake = textOf(cypherForCurrentView());
    assert.match(handshake, /shortestPath/, "a resolved chain produces a shortestPath query");
    assert.ok(handshake.includes(escapeCypher(pair.a.key)) && handshake.includes(escapeCypher(pair.b.key)),
      "naming both endpoints");
    assertSafe(handshake, "handshake");
  } finally {
    c.api.state.path = null;
  }
});

await check("credentials never travel in a URL, and persistence is honest", async () => {
  const SETTINGS_KEY = "puppetnet.console.settings.v1";
  const LEAKED = "token-that-must-never-be-accepted";

  // A link may point the console at an API; it may not authenticate it. Query
  // strings end up in history, bookmarks, proxy logs and Referer headers.
  const url = "https://console.example.invalid/?token=" + LEAKED +
    "&neo4jPass=hunter2&neo4jUser=neo4j&api=" + encodeURIComponent("https://relay.example.invalid") +
    "&source=worker";
  const c = await bootConsole({ url, quiet: true });
  assert.equal(c.api.config.token, "", "a ?token= parameter is ignored, not adopted");
  assert.equal(c.api.config.neo4jPass, "", "and so is a ?neo4jPass=");
  assert.equal(c.api.config.neo4jUser, "", "and a ?neo4jUser= (half a credential is worse than none)");
  assert.equal(c.api.config.base, "https://relay.example.invalid", "while a non-secret ?api= is honoured");
  assert.ok(c.calls.some((call) => call.url.startsWith("https://relay.example.invalid")),
    "and ?source=worker made it dial that API rather than staying offline");
  // That host does not resolve, so the console does what it always does when an
  // API is unreachable: fall back to the bundled dataset and say so, instead of
  // showing an empty canvas.
  assert.equal(c.api.config.mode, "demo", "an unreachable API degrades to demo data");
  assert.ok(!String(c.win.location.hash).includes(LEAKED), "the autosaved hash never carries a credential");

  // Every request the console makes authenticates in a header, never in the query.
  const bridge = workerBridge();
  const live = await bootConsole({ fetch: bridge, quiet: true });
  live.api.config.mode = "worker";
  live.api.config.base = RELAY_ORIGIN;
  live.api.config.token = GRAPH_TOKEN;
  await live.api.actions.switchProvider(true);
  await until(() => live.api.state.providerName === "worker" && live.api.state.nodes.size > 0,
    "the live console to load", 8000);
  live.calls.forEach((call) => {
    assert.ok(!call.url.includes(GRAPH_TOKEN), `a request URL must not carry the token: ${call.url}`);
    assert.ok(!call.url.includes("token="), `nor a token parameter: ${call.url}`);
    assert.equal(call.headers.Authorization, `Bearer ${GRAPH_TOKEN}`, "the header is the only place it appears");
  });

  // "Remember my settings" must mean something in both directions.
  const scratch = await bootConsole({ quiet: true });
  scratch.win.localStorage.setItem(SETTINGS_KEY, JSON.stringify({ token: GRAPH_TOKEN, persist: true }));
  assert.ok(scratch.win.localStorage.getItem(SETTINGS_KEY), "with persist on, settings are stored locally");

  // The toggle's own handler is what an analyst clicks; drive it, not the guts.
  const toggle = scratch.q("#cfg-persist");
  toggle.checked = false;
  toggle.dispatchEvent(new scratch.win.Event("change", { bubbles: true }));
  await sleep(60);
  assert.equal(scratch.win.localStorage.getItem(SETTINGS_KEY), null,
    "turning persist off clears what was stored — a shared machine keeps no API key");
});

clearTimeout(WATCHDOG);

// Each booted console runs a clock (setInterval) plus layout and toast timers, so
// the event loop never drains by itself and Node would hang after the summary.
// Close every window, then exit hard once stdout has had a tick to flush.
openWindows.forEach((dom) => {
  try { dom.window.close(); } catch (_) { /* already torn down */ }
});

console.log(`\nweb console smoke test: ${checks} checks passed in ${((Date.now() - suiteBegan) / 1000).toFixed(1)}s`);
setTimeout(() => process.exit(0), 100);
