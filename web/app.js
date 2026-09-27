/* =============================================================================
   PuppetNET — Graph Intelligence Console
   app.js
   =============================================================================

   A dependency-light single-page console for the PuppetNET OSINT graph. No
   framework, no build step, no external request at runtime: Cytoscape.js and
   the fcose layout are vendored next to this file.

   Structure
   ---------
     1.  constants & domain vocabulary      (entity types, relationship classes)
     2.  micro-helpers                      (DOM, formatting, csv, prng)
     3.  store                              (single state object + subscribers)
     4.  config                             (localStorage + URL + defaults)
     5.  toasts
     6.  data providers                     (worker / neo4j-http / demo)
     7.  normalisation                      (provider payloads → one shape)
     8.  graph engine                       (cytoscape styles, layout, filters)
     9.  search                             (header, suggestions, keyboard)
    10.  inspector                           (node / edge / multi-select)
    11.  table view                          (nodes / edges / citations + csv)
    12.  pathfinding                         (shortest chain, alternatives)
    13.  neighbourhood engine                (N-degree expansion, 1..4)
    14.  leaderboard, legend, status bar
    15.  modals, context menu, shortcuts, deep links
    16.  boot

   Design rules this file holds itself to
   --------------------------------------
   * The browser never sees a database credential in `worker` mode. Direct
     `neo4j` mode exists for a scratch database on localhost and says so loudly.
   * Every user string is escaped before it reaches `innerHTML`; nothing from a
     harvested document is ever treated as markup.
   * Nothing is fetched twice: an in-flight request is aborted and replaced, and
     responses are cached per query for the session.
   * Every code path has an offline fallback, so the console is demonstrable
     without a Worker, a database or a network.
   ========================================================================== */

(function () {
  "use strict";

  /* ===========================================================================
     1. Constants & domain vocabulary
     ======================================================================== */

  const VERSION = "1.0.0";
  const STORAGE_KEY = "puppetnet.console.v1";
  const SETTINGS_KEY = "puppetnet.console.settings.v1";

  /**
   * Entity types the pipeline can emit (`puppetnet.models.EntityType`) plus the
   * domain labels layered on top by `puppetnet.domain`. Colours follow the
   * console's three-signal rule: cyan for people, emerald for verified
   * corporate structure, purple for offshore/opacity, amber for places, rose for
   * anything adversarial.
   */
  const ENTITY_TYPES = Object.freeze({
    Person:       { label: "Person",       color: "#22d3ee", icon: "◉", order: 1 },
    Organization: { label: "Organization", color: "#10b981", icon: "◈", order: 2 },
    Company:      { label: "Company",      color: "#34d399", icon: "▣", order: 3 },
    ShellCompany: { label: "Shell",        color: "#a855f7", icon: "◇", order: 4 },
    Foundation:   { label: "Foundation",   color: "#c084fc", icon: "❖", order: 5 },
    Offshore:     { label: "Offshore",     color: "#8b5cf6", icon: "◊", order: 6 },
    Location:     { label: "Location",     color: "#f59e0b", icon: "◍", order: 7 },
    Craft:        { label: "Craft",        color: "#f43f5e", icon: "✈", order: 8 },
    Aircraft:     { label: "Aircraft",     color: "#fb7185", icon: "✈", order: 9 },
    Vessel:       { label: "Vessel",       color: "#f43f5e", icon: "⛴", order: 10 },
    Vehicle:      { label: "Vehicle",      color: "#e879a0", icon: "⛟", order: 11 },
    Unknown:      { label: "Unknown",      color: "#64748b", icon: "·", order: 99 },
  });

  const FALLBACK_COLOR = "#64748b";

  /**
   * Relationship classes, grouped so the filter rail stays readable when the
   * graph holds all 30-odd predicates from `puppetnet.models.RelationType`.
   * `calculated` edges are written by the analytics pass, not by an extractor,
   * and analysts routinely want them out of the way.
   */
  const REL_GROUPS = Object.freeze({
    control:   { label: "Control & ownership", color: "#10b981" },
    role:      { label: "Roles & employment",  color: "#22d3ee" },
    money:     { label: "Money flows",         color: "#f59e0b" },
    place:     { label: "Place & registration",color: "#a855f7" },
    movement:  { label: "Movement & craft",    color: "#f43f5e" },
    social:    { label: "Social & adversarial",color: "#38bdf8" },
    weak:      { label: "Weak / co-occurrence",color: "#64748b" },
    calculated:{ label: "Calculated",          color: "#c084fc" },
  });

  const REL_TYPES = Object.freeze({
    OWNS: "control", OWNED_BY: "control", CONTROLS: "control", SUBSIDIARY_OF: "control",
    PARENT_OF: "control", ACQUIRED: "control", SHAREHOLDER_OF: "control",
    INTERMEDIARY_FOR: "control", NOMINEE_OF: "control", BENEFICIARY_OF: "control",
    TRUSTEE_OF: "control",
    DIRECTOR_OF: "role", OFFICER_OF: "role", EMPLOYED_BY: "role", EMPLOYS: "role",
    MEMBER_OF: "role", FOUNDED: "role", APPOINTED_BY: "role",
    DONATED_TO: "money", FUNDED: "money", FUNDED_BY: "money", INVESTED_IN: "money",
    PAID_TO: "money", CONTRACTED_WITH: "money", TRANSFERRED_TO: "money",
    LOCATED_IN: "place", REGISTERED_IN: "place", NATIONAL_OF: "place",
    OPERATES_IN: "place", SHARES_ADDRESS: "place",
    TRAVELED_WITH: "movement", TRAVELED_TO: "movement", OPERATES: "movement",
    REGISTERED_TO: "movement", ARRIVED_FROM: "movement", PASSENGER_ON: "movement",
    MET_WITH: "social", FAMILY_OF: "social", AFFILIATED_WITH: "social",
    SANCTIONED_BY: "social", INVESTIGATED_BY: "social", ACCUSED_OF: "social",
    LINKED_OFFSHORE: "social",
    MENTIONED_WITH: "weak",
    PUPPET_MASTER_OF: "calculated",
  });

  /** Relationship types that never come from an extractor. */
  const CALCULATED_TYPES = Object.freeze(["PUPPET_MASTER_OF"]);

  /** Labels that mark an entity as an opacity finding rather than litter. */
  const OFFSHORE_LABELS = Object.freeze(["ShellCompany", "Offshore", "Foundation"]);

  /**
   * Property names that may be interpolated into a Cypher ORDER BY. A property
   * name cannot be a query parameter, so anything reaching a query string has to
   * come off a list the client cannot extend.
   */
  const SORTABLE_METRICS = Object.freeze([
    "betweenness", "anomaly_score", "degree_spike", "offshore_cluster_ratio",
    "confidence", "mention_count", "risk_score", "name", "last_seen", "first_seen",
  ]);

  /** Metrics that can drive node size; `betweenness` is the task's default. */
  const SIZE_METRICS = Object.freeze({
    betweenness:   { label: "Betweenness centrality", min: 0, max: 1, fallback: "degree" },
    anomaly_score: { label: "Anomaly score",          min: 0, max: 1, fallback: "degree" },
    degree:        { label: "Degree",                 min: 0, max: 1, fallback: null },
    mention_count: { label: "Mention count",          min: 0, max: 1, fallback: null },
    confidence:    { label: "Confidence",             min: 0, max: 1, fallback: null },
    risk_score:    { label: "Risk score",             min: 0, max: 1, fallback: null },
    flat:          { label: "Flat",                   min: 0, max: 1, fallback: null },
  });

  const DEFAULTS = Object.freeze({
    mode: "demo",            // demo | worker | neo4j
    base: "",                // worker root or neo4j http root
    token: "",               // GRAPH_API_TOKEN / PROXY_AUTH_TOKEN
    database: "neo4j",
    neo4jUser: "",
    neo4jPass: "",
    depth: 1,                // N-degree expansion
    nodeLimit: 250,
    minWeight: 0,
    minConfidence: 0,
    hideCalculated: false,
    hideWeak: false,
    sizeMetric: "betweenness",
    layout: "fcose",
    edgeStyle: "bezier",
    showLabels: true,
    colourClusters: false,
    glow: true,
    animateLayout: true,
    repulsion: 1,
    pageSize: 50,
    requestTimeout: 15000,
    autosuggest: true,
    autoexpand: false,
    persist: true,
    reduceMotion: false,
    pathHops: 6,
    pathDirection: "undirected",
    pathCost: "hops",
  });

  /* ===========================================================================
     2. Micro-helpers
     ======================================================================== */

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "html") node.innerHTML = value;   // callers must escape
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, String(value));
      }
    }
    // Children may be an array, a single node, a bare string, or nothing. Callers
    // build these inline and `[a, b].join("")` is a natural thing to write, so
    // normalise here rather than crash deep inside a render path — a TypeError in
    // a forEach takes the whole dropdown with it.
    const kids = children === null || children === undefined
      ? []
      : Array.isArray(children) ? children : [children];
    kids.forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /** HTML-escape anything that came out of a harvested document. */
  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/`/g, "&#96;");
  }

  /** Escape for a single-quoted Cypher string literal (used by "copy Cypher"). */
  function escapeCypher(value) {
    return String(value === null || value === undefined ? "" : value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  }

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const round = (value, digits) => {
    const f = Math.pow(10, digits === undefined ? 2 : digits);
    return Math.round(Number(value || 0) * f) / f;
  };

  function toNumber(value, fallback) {
    const n = typeof value === "number" ? value : parseFloat(value);
    return Number.isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  function formatNumber(value) {
    const n = toNumber(value, 0);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
    if (Number.isInteger(n)) return String(n);
    return n.toFixed(2);
  }

  function formatScore(value, digits) {
    if (value === null || value === undefined || value === "") return "—";
    const n = toNumber(value, NaN);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(digits === undefined ? 3 : digits);
  }

  function formatPercent(value) {
    if (value === null || value === undefined || value === "") return "—";
    const n = toNumber(value, NaN);
    if (!Number.isFinite(n)) return "—";
    return (n * 100).toFixed(1) + "%";
  }

  /** ISO timestamps arrive from Neo4j; show something an analyst can scan. */
  function formatDate(value) {
    if (!value) return "—";
    const text = String(value);
    const d = new Date(text.length === 10 ? text + "T00:00:00Z" : text);
    if (isNaN(d.getTime())) return text.slice(0, 10);
    return d.toISOString().slice(0, 10);
  }

  function formatDateTime(value) {
    if (!value) return "—";
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    return d.toISOString().slice(0, 16).replace("T", " ") + "Z";
  }

  function relativeTime(value) {
    if (!value) return "never";
    const then = new Date(value).getTime();
    if (!Number.isFinite(then)) return "—";
    const seconds = Math.round((Date.now() - then) / 1000);
    if (seconds < 60) return seconds + "s ago";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    const hours = Math.round(minutes / 60);
    if (hours < 48) return hours + "h ago";
    const days = Math.round(hours / 24);
    if (days < 60) return days + "d ago";
    return Math.round(days / 30) + "mo ago";
  }

  function debounce(fn, wait) {
    let timer = null;
    const wrapped = function () {
      const args = arguments, self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait);
    };
    wrapped.cancel = function () { if (timer) { clearTimeout(timer); timer = null; } };
    wrapped.flush = function () { if (timer) { clearTimeout(timer); timer = null; fn(); } };
    return wrapped;
  }

  function throttle(fn, wait) {
    let last = 0, timer = null;
    return function () {
      const args = arguments, self = this, now = Date.now();
      const remaining = wait - (now - last);
      if (remaining <= 0) {
        if (timer) { clearTimeout(timer); timer = null; }
        last = now; fn.apply(self, args);
      } else if (!timer) {
        timer = setTimeout(function () { timer = null; last = Date.now(); fn.apply(self, args); }, remaining);
      }
    };
  }

  /**
   * Deterministic PRNG (mulberry32). The demo dataset is generated from it so
   * the same numbers come back on every reload — a dashboard that reshuffles its
   * anomalies on refresh is impossible to reason about.
   */
  function prng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** Case/width-insensitive folding for search: also strips accents. */
  function normalizeText(value) {
    return String(value === null || value === undefined ? "" : value)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  /**
   * One CSV cell, quoted by RFC 4180 — and neutralised against formula injection.
   *
   * Every string in this console came from a document somebody else wrote: an
   * entity name, a headline, an evidence snippet. A cell beginning with `=`, `+`,
   * `-`, `@`, tab or CR is executed as a formula by Excel and LibreOffice, so
   * `=cmd|'/C calc'!A0` in a scraped title becomes code on an analyst's machine
   * the moment they open the export. Those cells get a leading apostrophe, which
   * forces text interpretation.
   *
   * Only strings are guarded: a numeric -0.5 is data, and mangling the sign of a
   * confidence column would be its own kind of wrong.
   */
  function csvCell(value) {
    if (value === null || value === undefined) return "";
    const text = Array.isArray(value) ? value.join("; ") : String(value);
    const guarded = typeof value === "string" && /^[=+\-@\t\r]/.test(text) ? "'" + text : text;
    return /[",\n\r]/.test(guarded) ? '"' + guarded.replace(/"/g, '""') + '"' : guarded;
  }

  function buildCsv(rows, columns) {
    const head = columns.map((c) => csvCell(c.label || c.key)).join(",");
    const body = rows.map((row) => columns.map((c) => csvCell(c.value ? c.value(row) : row[c.key])).join(","));
    return [head].concat(body).join("\r\n") + "\r\n";
  }

  function downloadText(filename, text, mime) {
    const blob = new Blob([text], { type: (mime || "text/plain") + ";charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (_) { /* fall through to the textarea path */ }
    try {
      const ta = el("textarea", { class: "sr-only" });
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_) { return false; }
  }

  function uid(prefix) {
    return (prefix || "id") + "-" + Math.random().toString(36).slice(2, 9);
  }

  /** FNV-1a, 32-bit. Stable across reloads and machines — `Math.random` is not. */
  function hash32(text) {
    let hash = 0x811c9dc5;
    const s = String(text);
    for (let i = 0; i < s.length; i++) {
      hash ^= s.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return hash >>> 0;
  }

  /**
   * Reproduce the canonical key shape from `puppetnet.models.canonical_key`
   * (`TYPE:slug-8hex`) for the demo dataset, so key handling, Cypher export and
   * identifier search all run against realistic values.
   */
  function demoKey(name, type) {
    const folded = normalizeText(name).replace(/ /g, "-").slice(0, 48).replace(/-+$/, "");
    const digest = hash32(type + "|" + normalizeText(name)).toString(16).padStart(8, "0");
    return String(type).toUpperCase() + ":" + (folded || "unknown") + "-" + digest;
  }

  /** A range input paints its filled track from a CSS variable. */
  function syncRange(input) {
    if (!input) return;
    const min = toNumber(input.min, 0), max = toNumber(input.max, 1), value = toNumber(input.value, min);
    const pct = max === min ? 0 : ((value - min) / (max - min)) * 100;
    input.style.setProperty("--fill", pct.toFixed(2) + "%");
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* ===========================================================================
     3. Store
     ======================================================================== */

  /**
   * One mutable state object plus a subscriber list. A framework would be
   * overkill for a single-screen console, but the indirection matters: every
   * panel reads from `state` rather than from the DOM, which is what makes the
   * providers swappable and the pure functions testable.
   */
  const state = {
    ready: false,
    view: "graph",                       // graph | table | path
    provider: null,                      // resolved provider instance
    providerName: "demo",
    connection: { state: "idle", label: "idle", detail: "", latencyMs: null, version: "" },

    query: "",
    suggestions: [],
    suggestCursor: -1,
    searching: false,

    scope: { kind: "overview", label: "whole graph", key: null },
    depth: DEFAULTS.depth,
    activeKey: null,                     // the node the neighbourhood engine works on
    selection: [],                       // cytoscape ids
    pathSelection: { from: null, to: null },

    nodes: new Map(),                    // key -> normalised node
    edges: new Map(),                    // id   -> normalised edge
    docs: new Map(),                     // doc_id -> citation
    hidden: new Set(),                   // keys hidden by alt-click

    filters: {
      types: new Set(),                  // empty = all types
      relTypes: new Set(),               // empty = all relationship types
      minWeight: 0,
      minConfidence: 0,
      hideCalculated: false,
      hideWeak: false,
      text: "",
    },

    render: {
      sizeMetric: "betweenness",
      layout: "fcose",
      edgeStyle: "bezier",
      showLabels: true,
      colourClusters: false,
      glow: true,
      animate: true,
      repulsion: 1,
    },

    stats: { nodes: 0, edges: 0, visible: 0, lastMs: 0, truncated: false, caps: null, metricsAt: null, engine: "" },
    leaderboard: [],
    path: null,                          // last path result
    table: { subject: "nodes", sort: "score-desc", page: 0, pageSize: DEFAULTS.pageSize, rows: [], total: 0 },
    busy: false,
    inflight: new Map(),                 // purpose -> AbortController
    cache: new Map(),                    // query key -> payload (session only)
    history: [],                         // scope trail, newest last
  };

  const listeners = new Set();
  function subscribe(fn) { listeners.add(fn); return function () { listeners.delete(fn); }; }
  function emit(topic, payload) {
    listeners.forEach(function (fn) {
      try { fn(topic, payload); } catch (err) { console.error("[console] subscriber failed", topic, err); }
    });
  }

  /* ===========================================================================
     4. Config (localStorage + URL + defaults)
     ======================================================================== */

  const config = Object.assign({}, DEFAULTS);

  function loadConfig() {
    let stored = {};
    try {
      const raw = localStorage.getItem(SETTINGS_KEY);
      if (raw) stored = JSON.parse(raw) || {};
    } catch (_) { stored = {}; }

    Object.keys(DEFAULTS).forEach(function (key) {
      if (stored[key] !== undefined && stored[key] !== null) config[key] = stored[key];
    });

    // URL parameters win over storage: a shared deep link must open exactly what
    // the sender saw, regardless of what the receiver last configured.
    const params = new URLSearchParams(location.search);
    const alias = { api: "base", worker: "base", url: "base", source: "mode", provider: "mode", db: "database" };
    // Never take a credential from a URL. Query strings land in browser history,
    // bookmarks, proxy and CDN logs, Referer headers and analytics — a `?token=`
    // link hands the key to everyone in between. A link may point the console at
    // an API; it may not authenticate it to that API. Keys are typed into
    // settings (and stay in this browser's localStorage) or configured by whoever
    // deploys the console.
    const secretKeys = ["token", "neo4jUser", "neo4jPass"];
    params.forEach(function (value, key) {
      const target = alias[key] || key;
      if (!(target in DEFAULTS)) return;
      if (secretKeys.indexOf(target) >= 0) return;
      const type = typeof DEFAULTS[target];
      if (type === "boolean") config[target] = value === "1" || value === "true" || value === "";
      else if (type === "number") config[target] = toNumber(value, DEFAULTS[target]);
      else config[target] = value;
    });

    // A same-origin Worker (Pages Functions / Worker Routes) needs no base URL.
    if (config.mode === "worker" && !config.base) config.base = "";
    return config;
  }

  function saveConfig() {
    // "Remember my settings" has to mean something in both directions: with
    // persist off, nothing is written and whatever was stored is dropped, so a
    // shared machine keeps no API key and no filter state after the toggle.
    if (!config.persist) {
      try { localStorage.removeItem(SETTINGS_KEY); } catch (_) { /* private mode */ }
      return config;
    }
    const payload = {};
    Object.keys(DEFAULTS).forEach(function (key) { payload[key] = config[key]; });
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(payload)); } catch (_) { /* private mode */ }
  }

  function forgetCredentials() {
    config.token = ""; config.neo4jUser = ""; config.neo4jPass = "";
    try { localStorage.removeItem(SETTINGS_KEY); } catch (_) { /* ignore */ }
    saveConfig();
  }

  /* ===========================================================================
     5. Toasts
     ======================================================================== */

  const TOAST_ICONS = { info: "◆", success: "✓", warn: "▲", error: "✕" };
  let toastHost = null;

  function toast(title, message, kind, ttl) {
    if (!toastHost) toastHost = $("#cy-toast-host");
    if (!toastHost) return null;
    const type = kind || "info";
    const node = el("div", { class: "toast", "data-kind": type, role: "status" }, [
      el("span", { class: "toast-icon", text: TOAST_ICONS[type] || "◆" }),
      el("div", { class: "toast-body" }, [
        el("div", { class: "toast-title", text: title }),
        message ? el("div", { class: "toast-msg", html: message }) : null,
      ]),
      el("button", { class: "toast-close", type: "button", "aria-label": "Dismiss", text: "✕" }),
    ]);
    const close = function () {
      if (!node.isConnected) return;
      node.classList.add("is-out");
      setTimeout(function () { node.remove(); }, 200);
    };
    node.querySelector(".toast-close").addEventListener("click", close);
    toastHost.appendChild(node);
    while (toastHost.children.length > 4) toastHost.firstElementChild.remove();
    if (ttl !== 0) setTimeout(close, ttl || (type === "error" ? 9000 : 4200));
    return close;
  }

  /* ===========================================================================
     6. Data providers
     ======================================================================== */

  /* ---------------------------------------------------------------- demo ---- */

  /**
   * A synthetic offshore network: 68 entities, ~118 relationships, 24 source
   * documents. Every name here is invented — the demo must never imply anything
   * about a real person or company, and it must be obvious on screen that the
   * data is synthetic (the status pill reads "demo dataset").
   *
   * Shape of a row: [key, name, entityType, labels, jurisdiction, cluster, tier]
   * `tier` 0 = hub, 1 = mid, 2 = leaf, and drives degree + centrality.
   */
  const DEMO_SEED = 20260927;

  const DEMO_NODE_ROWS = Object.freeze([
    // --- cluster 0: the Kastelion / Meridian control chain ------------------
    ["p-corvin",    "J. Aldric Corvin",            "Person",       [],                    "GB", 0, 0],
    ["p-vance",     "Mireille Vance",              "Person",       [],                    "FR", 0, 1],
    ["p-osei",      "Kwabena Osei",                "Person",       [],                    "GH", 0, 1],
    ["p-haldane",   "Ruaridh Haldane",             "Person",       [],                    "GB", 0, 2],
    ["o-kastelion", "Kastelion Overseas Ltd",      "Organization", ["Company","Offshore"], "VG", 0, 0],
    ["o-meridian",  "Meridian Holdings Group",     "Organization", ["Company"],            "CY", 0, 1],
    ["o-brightfen", "Brightfen Trust Services",    "Organization", ["Company"],            "JE", 0, 1],
    ["o-vela",      "Vela Maritime Holdings",      "Organization", ["Company","Offshore"], "PA", 0, 1],
    ["s-northquay", "Northquay Nominees SARL",     "Organization", ["Company","ShellCompany"], "LU", 0, 2],
    ["s-ashgrove",  "Ashgrove Settlements Ltd",    "Organization", ["Company","ShellCompany"], "VG", 0, 2],
    ["f-corvin",    "Corvin Family Foundation",    "Organization", ["Foundation","Offshore"], "LI", 0, 2],
    ["l-london",    "London, United Kingdom",      "Location",     [],                    "GB", 0, 2],
    ["l-limassol",  "Limassol, Cyprus",            "Location",     [],                    "CY", 0, 2],
    ["l-roadtown",  "Road Town, BVI",              "Location",     [],                    "VG", 0, 2],

    // --- cluster 1: the Sarnen aviation / logistics web --------------------
    ["p-ilves",     "Tomas Ilves",                 "Person",       [],                    "EE", 1, 1],
    ["p-marchetti", "Giulia Marchetti",            "Person",       [],                    "IT", 1, 1],
    ["p-dahlan",    "Nour Dahlan",                 "Person",       [],                    "AE", 1, 2],
    ["o-sarnen",    "Sarnen Air Logistics AG",     "Organization", ["Company"],            "CH", 1, 0],
    ["o-halcyon",   "Halcyon Freight Partners",    "Organization", ["Company","Offshore"], "MT", 1, 1],
    ["s-pelican",   "Pelican Cargo Ventures Ltd",  "Organization", ["Company","ShellCompany"], "MU", 1, 2],
    ["a-t7mer",     "T7-MER (Gulfstream G550)",    "Craft",        ["Aircraft"],           "SM", 1, 1],
    ["a-9hvel",     "9H-VEL (Bombardier G650)",    "Craft",        ["Aircraft"],           "MT", 1, 2],
    ["v-mv-aster",  "MV Aster Providence",         "Craft",        ["Vessel"],             "PA", 1, 1],
    ["l-geneva",    "Geneva, Switzerland",         "Location",     [],                    "CH", 1, 2],
    ["l-dubai",     "Dubai, UAE",                  "Location",     [],                    "AE", 1, 2],

    // --- cluster 2: the Ostreia sanctions-adjacent cluster -----------------
    ["p-kovalenko", "Olena Kovalenko",             "Person",       [],                    "UA", 2, 1],
    ["p-reznik",    "Aleksei Reznik",              "Person",       [],                    "RU", 2, 0],
    ["p-lindqvist", "Annika Lindqvist",            "Person",       [],                    "SE", 2, 2],
    ["o-ostreia",   "Ostreia Capital Partners",    "Organization", ["Company","Offshore"], "CY", 2, 0],
    ["o-ferrous",   "Ferrous Bridge Trading",      "Organization", ["Company"],            "AE", 2, 1],
    ["s-larkfield", "Larkfield Nominees Ltd",      "Organization", ["Company","ShellCompany"], "NZ", 2, 2],
    ["f-boreas",    "Boreas Charitable Trust",     "Organization", ["Foundation","Offshore"], "PA", 2, 2],
    ["l-nicosia",   "Nicosia, Cyprus",             "Location",     [],                    "CY", 2, 2],
    ["l-abudhabi",  "Abu Dhabi, UAE",              "Location",     [],                    "AE", 2, 2],

    // --- cluster 3: the Tallow Creek extractives chain ---------------------
    ["p-nakamura",  "Haruki Nakamura",             "Person",       [],                    "JP", 3, 1],
    ["p-boateng",   "Yaa Boateng",                 "Person",       [],                    "GH", 3, 1],
    ["o-tallow",    "Tallow Creek Resources",      "Organization", ["Company"],            "SG", 3, 0],
    ["o-greymouth", "Greymouth Minerals Ltd",      "Organization", ["Company","Offshore"], "HK", 3, 1],
    ["s-fernhill",  "Fernhill Assets Pty",         "Organization", ["Company","ShellCompany"], "AU", 3, 2],
    ["l-singapore", "Singapore",                   "Location",     [],                    "SG", 3, 2],
    ["l-accra",     "Accra, Ghana",                "Location",     [],                    "GH", 3, 2],

    // --- the bridges: nodes that join previously separate clusters ---------
    ["o-atlasgate", "Atlasyate Gateway Ltd",       "Organization", ["Company","Offshore"], "AE", 4, 0],
    ["p-sorensen",  "Lars Sørensen",               "Person",       [],                    "DK", 4, 1],
    ["s-quillback", "Quillback Services Ltd",      "Organization", ["Company","ShellCompany"], "VG", 4, 2],

    // --- leaves that the pruner would look at ------------------------------
    ["o-dustbin1",  "Pemberton Advisory Ltd",      "Organization", ["Company"],            "GB", 0, 2],
    ["p-dustbin2",  "H. Pemberton",                "Person",       [],                    "GB", 0, 2],
    ["o-dustbin3",  "Cobweb Trading Ltd",          "Organization", ["Company","Offshore"], "WS", 4, 2],
    ["p-dustbin4",  "Isolde Marchetti",            "Person",       [],                    "IT", 1, 2],
    ["o-dustbin5",  "Tallow Creek Freight",        "Organization", ["Company"],            "SG", 3, 2],
    ["l-dustbin6",  "Apia, Samoa",                 "Location",     [],                    "WS", 4, 2],
    ["s-dustbin7",  "Marlowe Nominees Ltd",        "Organization", ["Company","ShellCompany"], "JE", 2, 2],
    ["p-dustbin8",  "Dmitri Sokolov",              "Person",       [],                    "RU", 2, 2],
  ]);

  /** [subjectKey, type, objectKey, weightHint, confidenceHint, method] */
  const DEMO_EDGE_ROWS = Object.freeze([
    // cluster 0
    ["p-corvin", "OWNS", "o-kastelion", 0.92, 0.88, "structured"],
    ["p-corvin", "CONTROLS", "o-meridian", 0.74, 0.71, "dependency"],
    ["p-corvin", "DIRECTOR_OF", "o-brightfen", 0.66, 0.8, "structured"],
    ["p-corvin", "LOCATED_IN", "l-london", 0.4, 0.75, "pattern"],
    ["p-corvin", "BENEFICIARY_OF", "f-corvin", 0.81, 0.69, "structured"],
    ["p-vance", "NOMINEE_OF", "o-kastelion", 0.7, 0.66, "structured"],
    ["p-vance", "OFFICER_OF", "o-brightfen", 0.55, 0.62, "structured"],
    ["p-vance", "MET_WITH", "p-reznik", 0.48, 0.41, "cooccurrence"],
    ["p-osei", "EMPLOYED_BY", "o-vela", 0.52, 0.7, "structured"],
    ["p-osei", "SHAREHOLDER_OF", "s-northquay", 0.61, 0.58, "structured"],
    ["p-haldane", "INTERMEDIARY_FOR", "o-kastelion", 0.68, 0.6, "dependency"],
    ["p-haldane", "DIRECTOR_OF", "s-ashgrove", 0.44, 0.55, "structured"],
    ["o-kastelion", "PARENT_OF", "o-meridian", 0.86, 0.83, "structured"],
    ["o-kastelion", "REGISTERED_IN", "l-roadtown", 0.38, 0.9, "structured"],
    ["o-kastelion", "OWNS", "o-vela", 0.72, 0.64, "dependency"],
    ["o-kastelion", "LINKED_OFFSHORE", "s-ashgrove", 0.66, 0.59, "pattern"],
    ["o-meridian", "LOCATED_IN", "l-limassol", 0.36, 0.85, "structured"],
    ["o-meridian", "SUBSIDIARY_OF", "o-kastelion", 0.8, 0.78, "structured"],
    ["o-brightfen", "TRUSTEE_OF", "f-corvin", 0.77, 0.72, "structured"],
    ["o-vela", "OWNS", "v-mv-aster", 0.69, 0.67, "structured"],
    ["s-northquay", "NOMINEE_OF", "o-kastelion", 0.58, 0.5, "dependency"],
    ["s-northquay", "SHARES_ADDRESS", "s-ashgrove", 0.63, 0.61, "pattern"],
    ["s-ashgrove", "REGISTERED_IN", "l-roadtown", 0.34, 0.88, "structured"],
    ["f-corvin", "FUNDED_BY", "o-kastelion", 0.71, 0.55, "dependency"],
    ["o-dustbin1", "SUBSIDIARY_OF", "o-meridian", 0.18, 0.22, "cooccurrence"],
    ["p-dustbin2", "DIRECTOR_OF", "o-dustbin1", 0.2, 0.24, "cooccurrence"],

    // cluster 1
    ["p-ilves", "DIRECTOR_OF", "o-sarnen", 0.83, 0.79, "structured"],
    ["p-ilves", "OPERATES", "a-t7mer", 0.6, 0.58, "gazetteer"],
    ["p-ilves", "MET_WITH", "p-nakamura", 0.42, 0.37, "cooccurrence"],
    ["p-marchetti", "OFFICER_OF", "o-halcyon", 0.74, 0.7, "structured"],
    ["p-marchetti", "PASSENGER_ON", "a-t7mer", 0.66, 0.63, "gazetteer"],
    ["p-marchetti", "FAMILY_OF", "p-dustbin4", 0.3, 0.33, "pattern"],
    ["p-dahlan", "EMPLOYED_BY", "o-halcyon", 0.48, 0.52, "structured"],
    ["p-dahlan", "LOCATED_IN", "l-dubai", 0.37, 0.8, "structured"],
    ["o-sarnen", "LOCATED_IN", "l-geneva", 0.39, 0.86, "structured"],
    ["o-sarnen", "OPERATES", "a-t7mer", 0.78, 0.74, "structured"],
    ["o-sarnen", "OPERATES", "a-9hvel", 0.62, 0.6, "structured"],
    ["o-sarnen", "CONTRACTED_WITH", "o-halcyon", 0.57, 0.51, "dependency"],
    ["o-halcyon", "OWNS", "s-pelican", 0.64, 0.57, "structured"],
    ["o-halcyon", "REGISTERED_IN", "l-dubai", 0.33, 0.62, "structured"],
    ["s-pelican", "REGISTERED_TO", "v-mv-aster", 0.55, 0.44, "gazetteer"],
    ["a-t7mer", "TRAVELED_TO", "l-dubai", 0.5, 0.68, "gazetteer"],
    ["a-t7mer", "TRAVELED_WITH", "a-9hvel", 0.45, 0.4, "cooccurrence"],
    ["a-9hvel", "REGISTERED_TO", "o-halcyon", 0.58, 0.55, "structured"],
    ["p-ilves", "TRAVELED_WITH", "p-marchetti", 0.61, 0.58, "gazetteer"],

    // cluster 2
    ["p-reznik", "CONTROLS", "o-ostreia", 0.88, 0.81, "dependency"],
    ["p-reznik", "SANCTIONED_BY", "l-abudhabi", 0.2, 0.3, "cooccurrence"],
    ["p-reznik", "OWNED_BY", "s-dustbin7", 0.3, 0.28, "cooccurrence"],
    ["p-kovalenko", "DIRECTOR_OF", "o-ostreia", 0.63, 0.6, "structured"],
    ["p-kovalenko", "LOCATED_IN", "l-nicosia", 0.35, 0.77, "structured"],
    ["p-lindqvist", "NOMINEE_OF", "s-larkfield", 0.5, 0.47, "structured"],
    ["p-lindqvist", "TRUSTEE_OF", "f-boreas", 0.54, 0.49, "structured"],
    ["p-dustbin8", "EMPLOYED_BY", "o-ostreia", 0.22, 0.26, "cooccurrence"],
    ["o-ostreia", "LOCATED_IN", "l-nicosia", 0.38, 0.84, "structured"],
    ["o-ostreia", "OWNS", "o-ferrous", 0.7, 0.62, "dependency"],
    ["o-ostreia", "INVESTIGATED_BY", "l-abudhabi", 0.24, 0.29, "cooccurrence"],
    ["o-ferrous", "LOCATED_IN", "l-abudhabi", 0.36, 0.8, "structured"],
    ["o-ferrous", "TRANSFERRED_TO", "f-boreas", 0.67, 0.45, "dependency"],
    ["s-larkfield", "NOMINEE_OF", "o-ostreia", 0.59, 0.52, "structured"],
    ["s-dustbin7", "SHARES_ADDRESS", "s-larkfield", 0.41, 0.36, "pattern"],
    ["f-boreas", "FUNDED_BY", "o-ferrous", 0.6, 0.42, "dependency"],

    // cluster 3
    ["p-nakamura", "SHAREHOLDER_OF", "o-tallow", 0.79, 0.73, "structured"],
    ["p-nakamura", "LOCATED_IN", "l-singapore", 0.34, 0.81, "structured"],
    ["p-boateng", "OFFICER_OF", "o-greymouth", 0.68, 0.64, "structured"],
    ["p-boateng", "LOCATED_IN", "l-accra", 0.33, 0.79, "structured"],
    ["o-tallow", "LOCATED_IN", "l-singapore", 0.37, 0.87, "structured"],
    ["o-tallow", "OWNS", "o-greymouth", 0.75, 0.7, "structured"],
    ["o-tallow", "CONTRACTED_WITH", "o-halcyon", 0.44, 0.38, "cooccurrence"],
    ["o-greymouth", "TRANSFERRED_TO", "s-fernhill", 0.62, 0.5, "dependency"],
    ["s-fernhill", "REGISTERED_IN", "l-singapore", 0.3, 0.66, "structured"],
    ["o-dustbin5", "SUBSIDIARY_OF", "o-tallow", 0.19, 0.23, "cooccurrence"],

    // cluster 4 — the bridges. Each of these joins two clusters that had no
    // other path between them, which is exactly what telegram_bot.py alerts on.
    ["o-atlasgate", "OWNS", "o-ostreia", 0.72, 0.61, "dependency"],
    ["o-atlasgate", "SHAREHOLDER_OF", "o-kastelion", 0.69, 0.57, "dependency"],
    ["o-atlasgate", "CONTRACTED_WITH", "o-sarnen", 0.55, 0.48, "cooccurrence"],
    ["o-atlasgate", "LOCATED_IN", "l-abudhabi", 0.32, 0.83, "structured"],
    ["p-sorensen", "DIRECTOR_OF", "o-atlasgate", 0.84, 0.77, "structured"],
    ["p-sorensen", "INTERMEDIARY_FOR", "o-tallow", 0.58, 0.49, "dependency"],
    ["p-sorensen", "NOMINEE_OF", "s-quillback", 0.47, 0.44, "structured"],
    ["s-quillback", "NOMINEE_OF", "o-meridian", 0.52, 0.46, "dependency"],
    ["s-quillback", "REGISTERED_IN", "l-roadtown", 0.29, 0.72, "structured"],
    ["o-dustbin3", "SUBSIDIARY_OF", "s-quillback", 0.17, 0.21, "cooccurrence"],
    ["l-dustbin6", "LOCATED_IN", "l-dustbin6", 0, 0, "structured"],   // self-loop guard, dropped below
    ["o-dustbin3", "REGISTERED_IN", "l-dustbin6", 0.2, 0.3, "cooccurrence"],

    // calculated layer (written by graph_analytics.py, not an extractor)
    ["p-corvin", "PUPPET_MASTER_OF", "o-kastelion", 0.91, 0.86, "calculated"],
    ["p-corvin", "PUPPET_MASTER_OF", "o-meridian", 0.72, 0.68, "calculated"],
    ["p-reznik", "PUPPET_MASTER_OF", "o-ostreia", 0.85, 0.8, "calculated"],
    ["p-sorensen", "PUPPET_MASTER_OF", "o-atlasgate", 0.79, 0.74, "calculated"],
    ["p-ilves", "PUPPET_MASTER_OF", "o-sarnen", 0.66, 0.63, "calculated"],
    ["p-nakamura", "PUPPET_MASTER_OF", "o-tallow", 0.61, 0.59, "calculated"],
  ]);

  const DEMO_SOURCE_ROWS = Object.freeze([
    ["icij_offshore", 1.0, "ICIJ Offshore Leaks"],
    ["wikidata", 0.9, "Wikidata"],
    ["opencorporates", 0.9, "OpenCorporates"],
    ["adsb_exchange", 0.8, "ADS-B Exchange"],
    ["companies_house", 0.85, "Companies House"],
    ["faa_registry", 0.8, "FAA registry"],
    ["rss_world", 0.4, "World news RSS"],
    ["rss_business", 0.4, "Business news RSS"],
  ]);

  const DEMO_DOC_TITLES = Object.freeze([
    "Leaked registry shows nominee directors across three jurisdictions",
    "Court filing names foundation trustees in offshore transfer",
    "Flight logs place executives aboard corporate aircraft",
    "Corporate filings reveal common registered address",
    "Investigation opens into cargo contractor payments",
    "Shareholder register updated after restructuring",
    "Vessel ownership trail leads to holding company",
    "Sanctions database entry links trading house to trust",
    "Annual return lists dormant subsidiaries",
    "Procurement notice names intermediary firm",
    "Board minutes record appointment of nominee officer",
    "Lease agreement ties aircraft to logistics operator",
    "Registry extract shows shell company dissolution",
    "Wire transfer records describe multi-hop settlement",
    "Press release announces acquisition of freight partner",
    "Court order freezes assets of holding entity",
    "Charity commission filing lists related parties",
    "Customs manifest records passenger and cargo",
    "Company search result for nominee shareholder",
    "Newsletter reports on extractives joint venture",
    "Beneficial ownership declaration filed late",
    "Trade database entry links two exporters",
    "Regulatory notice flags address sharing",
    "Interview transcript mentions undisclosed adviser",
  ]);

  /**
   * Build the demo dataset once, lazily. Deterministic: the same seed produces
   * the same metrics every time, so screenshots and bug reports are reproducible.
   */
  function buildDemoDataset() {
    const random = prng(DEMO_SEED);
    const now = Date.now();
    const day = 86400000;
    const nodes = [];
    const byCluster = {};
    const slugToKey = new Map();          // authoring slug -> canonical key

    DEMO_NODE_ROWS.forEach(function (row, index) {
      const key = row[0], name = row[1], type = row[2], labels = row[3], juris = row[4], cluster = row[5], tier = row[6];
      const canonical = demoKey(name, type);
      slugToKey.set(key, canonical);
      const tierFactor = [1, 0.62, 0.3][tier] || 0.3;
      const offshore = labels.some(function (l) { return OFFSHORE_LABELS.indexOf(l) >= 0; });
      const jitter = 0.72 + random() * 0.5;

      // Betweenness: hubs sit on the shortest paths between clusters, so a hub
      // in the bridge cluster scores highest — which is the point of the metric.
      const bridge = cluster === 4;
      const betweenness = clamp((bridge ? 0.52 : 0.06) * tierFactor * jitter + random() * 0.05, 0, 1);
      const degreeSpike = clamp((bridge ? 1.8 : 0.4) * tierFactor * (0.5 + random()), 0, 3) / 3;
      const offshoreRatio = offshore ? clamp(0.55 + random() * 0.45, 0, 1) : clamp(random() * 0.3, 0, 1);
      const anomaly = round(0.4 * betweenness + 0.35 * degreeSpike + 0.25 * offshoreRatio, 4);
      const confidence = round(clamp(0.42 + tierFactor * 0.4 + random() * 0.18, 0, 0.99), 3);
      const mentions = Math.round((tier === 0 ? 34 : tier === 1 ? 12 : 3) * (0.6 + random()));
      const risk = round(clamp(anomaly * 0.7 + confidence * 0.3, 0, 1), 3);
      const lastSeen = new Date(now - Math.round(random() * (tier === 2 ? 400 : 40)) * day).toISOString();
      const firstSeen = new Date(now - Math.round((600 + random() * 900)) * day).toISOString();
      const sourceCount = 1 + Math.floor(random() * 3);
      const sourceIds = [];
      for (let i = 0; i < sourceCount; i++) sourceIds.push(DEMO_SOURCE_ROWS[Math.floor(random() * DEMO_SOURCE_ROWS.length)][0]);
      const docIds = [];
      for (let i = 0; i < Math.min(4, 1 + Math.floor(random() * 4)); i++) docIds.push("doc-" + ((index * 7 + i * 3) % DEMO_DOC_TITLES.length));

      const props = { jurisdiction: juris };
      if (type === "Organization" || type === "Company") {
        props.reg_number = juris + "-" + String(100000 + Math.floor(random() * 899999));
        props.address_key = normalizeText(juris + " " + name.split(" ")[0]).replace(/ /g, "-");
        if (offshore) props.shell_risk = round(clamp(0.4 + random() * 0.6, 0, 1), 2);
      }
      if (labels.indexOf("Aircraft") >= 0) {
        props.tail_number = name.split(" ")[0];
        props.transponder = String(1000000 + Math.floor(random() * 8999999));
        props.icao24 = Math.floor(random() * 0xffffff).toString(16).padStart(6, "0").toUpperCase();
      }
      if (labels.indexOf("Vessel") >= 0) {
        props.imo = String(9000000 + Math.floor(random() * 999999));
        props.mmsi = String(200000000 + Math.floor(random() * 99999999));
        props.flag = juris;
      }
      if (type === "Person") {
        props.nationality = juris;
        if (random() > 0.6) props.wikidata_id = "Q" + (1000000 + Math.floor(random() * 8999999));
      }
      if (random() > 0.75) props.wikipedia_id = normalizeText(name).replace(/ /g, "_");

      const node = {
        key: canonical,
        name: name,
        entity_type: type,
        labels: ["Entity", type].concat(labels).filter(function (v, i, arr) { return arr.indexOf(v) === i; }),
        jurisdiction: juris,
        confidence: confidence,
        mention_count: mentions,
        betweenness: round(betweenness, 5),
        anomaly_score: anomaly,
        anomaly_betweenness: round(betweenness, 4),
        anomaly_degree_spike: round(degreeSpike, 4),
        anomaly_offshore_cluster_ratio: round(offshoreRatio, 4),
        degree_spike: round(degreeSpike * 3, 3),
        offshore_cluster_ratio: round(offshoreRatio, 3),
        cluster_id: "c" + cluster,
        risk_score: risk,
        first_seen: firstSeen,
        last_seen: lastSeen,
        aliases: random() > 0.7 ? [name.split(" ")[0] + " " + (name.split(" ")[1] || ""), name.toUpperCase()] : [],
        source_ids: sourceIds.filter(function (v, i, arr) { return arr.indexOf(v) === i; }),
        doc_ids: docIds.filter(function (v, i, arr) { return arr.indexOf(v) === i; }),
        metrics_at: new Date(now - Math.round(random() * 6) * 3600000).toISOString(),
        props: props,
        degree: 0,
        _demo: true,
        _tier: tier,
      };
      nodes.push(node);
      byCluster[cluster] = (byCluster[cluster] || 0) + 1;
    });

    const nodeIndex = new Map(nodes.map(function (n) { return [n.key, n]; }));
    const edges = [];
    DEMO_EDGE_ROWS.forEach(function (row, index) {
      const type = row[1];
      const source = slugToKey.get(row[0]) || row[0];
      const target = slugToKey.get(row[2]) || row[2];
      if (source === target) return;                       // drop the self-loop guard row
      if (!nodeIndex.has(source) || !nodeIndex.has(target)) return;
      const method = row[5] || "structured";
      const weight = round(clamp(row[3] * (0.9 + random() * 0.2), 0.02, 1), 3);
      const confidence = round(clamp(row[4] * (0.92 + random() * 0.16), 0.02, 1), 3);
      const observations = method === "cooccurrence" ? 1 + Math.floor(random() * 3) : 1 + Math.floor(random() * 9);
      const sourceRow = DEMO_SOURCE_ROWS[Math.floor(random() * DEMO_SOURCE_ROWS.length)];
      const docId = "doc-" + ((index * 5) % DEMO_DOC_TITLES.length);
      edges.push({
        id: "e-" + source + "-" + type + "-" + target,
        source: source,
        target: target,
        type: type,
        weight: weight,
        confidence: confidence,
        source_weight: sourceRow[1],
        method: method,
        observations: observations,
        evidence: [
          method === "dependency" ? "... the filing records " + nodeIndex.get(source).name + " as controlling " + nodeIndex.get(target).name + " ..."
          : method === "cooccurrence" ? "... " + nodeIndex.get(source).name + " and " + nodeIndex.get(target).name + " were named in the same document ..."
          : method === "gazetteer" ? "... identifier match on " + (nodeIndex.get(target).props.tail_number || nodeIndex.get(target).props.imo || "registry key") + " ..."
          : "... registry field maps " + nodeIndex.get(source).name + " to " + nodeIndex.get(target).name + " ...",
        ],
        evidence_scores: [round(clamp(confidence * (0.8 + random() * 0.3), 0, 1), 3)],
        verb: type.toLowerCase().replace(/_/g, " "),
        source_id: sourceRow[0],
        doc_id: docId,
        run_id: "run-demo-" + (1 + (index % 5)),
        negated: false,
        hedged: method === "cooccurrence" && random() > 0.7,
        passive: random() > 0.8,
        rule: method === "pattern" ? "apposition" : method,
        first_seen: new Date(now - Math.round((300 + random() * 700)) * day).toISOString(),
        last_seen: new Date(now - Math.round(random() * 30) * day).toISOString(),
        calculated: CALCULATED_TYPES.indexOf(type) >= 0 || method === "calculated",
      });
    });

    // degrees (semantic edges only — exactly what the pruner counts)
    edges.forEach(function (edge) {
      const a = nodeIndex.get(edge.source), b = nodeIndex.get(edge.target);
      if (a) a.degree += 1;
      if (b) b.degree += 1;
    });

    const docs = [];
    DEMO_DOC_TITLES.forEach(function (title, index) {
      const sourceRow = DEMO_SOURCE_ROWS[index % DEMO_SOURCE_ROWS.length];
      docs.push({
        doc_id: "doc-" + index,
        title: title,
        url: "https://example.invalid/documents/doc-" + index,
        source_id: sourceRow[0],
        source_name: sourceRow[2],
        source_weight: sourceRow[1],
        published_at: new Date(now - Math.round((5 + index * 11) ) * day).toISOString(),
        fetched_at: new Date(now - Math.round(index * 2) * day).toISOString(),
        content_hash: (index * 2654435761 >>> 0).toString(16).padStart(8, "0"),
        entity_count: 2 + (index % 6),
      });
    });

    const sources = DEMO_SOURCE_ROWS.map(function (row) {
      return { source_id: row[0], name: row[2], weight: row[1] };
    });

    return { nodes: nodes, edges: edges, docs: docs, sources: sources, byCluster: byCluster };
  }

  let demoCache = null;
  function demoDataset() {
    if (!demoCache) demoCache = buildDemoDataset();
    return demoCache;
  }

  /**
   * Offline provider. It implements the same surface as the network providers
   * against the in-memory demo graph, so the whole console — search, expansion,
   * pathfinding, tables — is exercisable with no Worker and no database.
   */
  const DemoProvider = {
    name: "demo",
    label: "demo dataset",
    isLive: false,

    async health() {
      const data = demoDataset();
      return {
        ok: true, source: "demo", version: VERSION,
        graph: {
          configured: true, database: "in-memory", engine: "demo",
          nodes: data.nodes.length, edges: data.edges.length, documents: data.docs.length,
          limits: { max_nodes: data.nodes.length, max_depth: 4, max_hops: 12 },
        },
        note: "Synthetic data: every entity in this dataset is invented.",
      };
    },

    async overview(opts) {
      const data = demoDataset();
      const limit = clamp(toNumber(opts && opts.limit, 250), 1, 1200);
      const metric = (opts && opts.metric) || "anomaly_score";
      const ranked = data.nodes.slice().sort(function (a, b) {
        return toNumber(b[metric], 0) - toNumber(a[metric], 0) || toNumber(b.degree, 0) - toNumber(a.degree, 0);
      });
      const keep = new Set(ranked.slice(0, limit).map(function (n) { return n.key; }));
      const nodes = data.nodes.filter(function (n) { return keep.has(n.key); });
      const edges = data.edges.filter(function (e) { return keep.has(e.source) && keep.has(e.target); });
      return {
        ok: true, source: "demo", nodes: nodes, edges: edges, docs: data.docs,
        truncated: data.nodes.length > nodes.length,
        metrics_at: nodes.reduce(function (acc, n) { return n.metrics_at > acc ? n.metrics_at : acc; }, ""),
        caps: { node_cap: 200000, edge_cap: 400000, nodes: data.nodes.length, edges: data.edges.length },
      };
    },

    async search(q, opts) {
      const data = demoDataset();
      const needle = normalizeText(q);
      const limit = clamp(toNumber(opts && opts.limit, 12), 1, 50);
      if (!needle) return { ok: true, source: "demo", nodes: [], count: 0 };
      const scored = [];
      data.nodes.forEach(function (node) {
        const score = scoreMatch(node, needle);
        if (score > 0) scored.push({ node: node, score: score });
      });
      scored.sort(function (a, b) { return b.score - a.score || toNumber(b.node.betweenness, 0) - toNumber(a.node.betweenness, 0); });
      return { ok: true, source: "demo", nodes: scored.slice(0, limit).map(function (s) { return s.node; }), count: scored.length };
    },

    async node(key) {
      const data = demoDataset();
      const node = data.nodes.find(function (n) { return n.key === key; });
      if (!node) return { ok: false, source: "demo", error: { code: "not_found", message: "No entity " + key } };
      const edges = data.edges.filter(function (e) { return e.source === key || e.target === key; });
      return { ok: true, source: "demo", node: node, edges: edges, docs: data.docs, citations: citationsFor(node, data) };
    },

    async neighbors(key, opts) {
      const data = demoDataset();
      const depth = clamp(toNumber(opts && opts.depth, 1), 1, 4);
      const limit = clamp(toNumber(opts && opts.limit, 250), 1, 1200);
      const result = bfsSubgraph(data.nodes, data.edges, key, depth, limit);
      return {
        ok: true, source: "demo", root: key, depth: depth,
        nodes: result.nodes, edges: result.edges, docs: data.docs, truncated: result.truncated,
      };
    },

    async path(from, to, opts) {
      const data = demoDataset();
      const maxHops = clamp(toNumber(opts && opts.maxHops, 6), 1, 12);
      const result = localShortestPath(data.nodes, data.edges, from, to, maxHops, (opts && opts.cost) || "hops", opts && opts.direction);
      return Object.assign({ ok: true, source: "demo" }, result);
    },

    async table(opts) {
      const data = demoDataset();
      return localTable(data.nodes, data.edges, data.docs, opts || {});
    },
  };

  /* ------------------------------------------------------------- network ---- */

  function apiUrl(config2, path) {
    const base = String(config2.base || "").replace(/\/+$/, "");
    if (base) return base + path;
    // Same-origin deployment: Pages Functions or a Worker Route on this domain.
    return path;
  }

  function neo4jTxUrl(config2) {
    const base = String(config2.base || "").replace(/\/+$/, "");
    const db = encodeURIComponent(config2.database || "neo4j");
    return base + "/db/" + db + "/tx/commit";
  }

  /**
   * AuraDB hands out a bolt URI (`neo4j+s://abc.databases.neo4j.io:7687`); the
   * HTTP transaction endpoint is the same host over HTTPS. Accept either shape.
   */
  function boltToHttp(uri) {
    const text = String(uri || "").trim();
    if (!text) return "";
    const m = /^(neo4j(\+s|\+ssc)?|bolt(\+s|\+ssc)?):\/\/([^/]+)/i.exec(text);
    if (m) return "https://" + m[3].replace(/:\d+$/, "");
    return text.replace(/\/+$/, "");
  }

  /**
   * Shared HTTP plumbing for the two network providers: timeout, abort, JSON
   * parsing, and error normalisation so the UI can show one message shape.
   */
  async function httpJson(url, options, purpose) {
    const controller = new AbortController();
    const previous = state.inflight.get(purpose || url);
    if (previous) previous.abort();                 // a newer question supersedes it
    state.inflight.set(purpose || url, controller);

    const timeout = clamp(toNumber(config.requestTimeout, DEFAULTS.requestTimeout), 2000, 120000);
    const timer = setTimeout(function () { controller.abort(); }, timeout);
    const started = performance.now();

    try {
      const response = await fetch(url, Object.assign({ signal: controller.signal }, options || {}));
      const text = await response.text();
      const elapsed = Math.round(performance.now() - started);
      let body = null;
      try { body = text ? JSON.parse(text) : null; } catch (_) { body = null; }

      if (!response.ok) {
        const detail = body && body.error ? body.error : null;
        const err = new Error(detail && detail.message ? detail.message : "HTTP " + response.status);
        err.status = response.status;
        err.code = (detail && detail.code) || "http_error";
        err.retryAfter = toNumber(response.headers.get("retry-after"), 0);
        err.elapsed = elapsed;
        err.body = body;
        throw err;
      }
      return { body: body, elapsed: elapsed, headers: response.headers };
    } catch (err) {
      if (err && err.name === "AbortError") {
        const abort = new Error("Request aborted (timeout or superseded)");
        abort.code = "aborted"; abort.status = 0;
        throw abort;
      }
      throw err;
    } finally {
      clearTimeout(timer);
      if (state.inflight.get(purpose || url) === controller) state.inflight.delete(purpose || url);
    }
  }

  function authHeaders() {
    const headers = { Accept: "application/json" };
    if (config.token) headers.Authorization = "Bearer " + config.token;
    return headers;
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value === undefined || value === null || value === "") return;
      search.set(key, String(value));
    });
    const text = search.toString();
    return text ? "?" + text : "";
  }

  /**
   * Cloudflare Worker provider. This is the supported deployment: the Worker
   * holds the Neo4j credentials, so the browser only ever sees graph data.
   */
  function createWorkerProvider() {
    async function get(path, params, purpose) {
      const url = apiUrl(config, path) + query(params);
      const cacheKey = url;
      if (state.cache.has(cacheKey)) return state.cache.get(cacheKey);
      const res = await httpJson(url, { method: "GET", headers: authHeaders() }, purpose || path);
      const payload = res.body || { ok: false };
      payload._elapsed = res.elapsed;
      if (payload.ok) {
        if (state.cache.size > 60) state.cache.clear();
        state.cache.set(cacheKey, payload);
      }
      return payload;
    }

    return {
      name: "worker",
      label: "worker /graph",
      isLive: true,
      kind: "worker",

      async health() {
        const payload = await get("/graph/health", {}, "health");
        return payload;
      },
      async overview(opts) { return get("/graph/overview", { limit: opts.limit, metric: opts.metric }, "overview"); },
      async search(q, opts) { return get("/graph/search", { q: q, limit: opts.limit, type: opts.type }, "search"); },
      async node(key) { return get("/graph/node", { key: key }, "node:" + key); },
      async neighbors(key, opts) {
        return get("/graph/neighbors", {
          key: key, depth: opts.depth, limit: opts.limit,
          min_weight: opts.minWeight, types: (opts.types || []).join(","),
        }, "neighbors:" + key);
      },
      async path(from, to, opts) {
        return get("/graph/path", {
          from: from, to: to, max_hops: opts.maxHops, direction: opts.direction, cost: opts.cost,
        }, "path:" + from + ":" + to);
      },
      async table(opts) { return get("/graph/table", opts, "table"); },
    };
  }

  /**
   * Direct Neo4j HTTP provider — local development against a scratch database.
   * Credentials live in the browser, which is exactly why the settings dialog
   * refuses to pretend otherwise.
   */
  function createNeo4jProvider() {
    const base = boltToHttp(config.base);

    async function cypher(statements, purpose) {
      if (!base) throw Object.assign(new Error("Set the Neo4j HTTP(S) URL first"), { code: "config" });
      const user = config.neo4jUser || "neo4j";
      const pass = config.neo4jPass || "";
      const res = await httpJson(neo4jTxUrl(config), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: "Basic " + btoa(unescape(encodeURIComponent(user + ":" + pass))),
        },
        body: JSON.stringify({ statements: statements }),
      }, purpose || "neo4j");

      const body = res.body || {};
      if (Array.isArray(body.errors) && body.errors.length) {
        const first = body.errors[0];
        throw Object.assign(new Error(first.message || "Neo4j error"), { code: first.code || "neo4j_error", status: 200 });
      }
      return { results: body.results || [], elapsed: res.elapsed };
    }

    /** Turn Neo4j's column/row envelope into plain objects. */
    function rows(result) {
      const columns = result.columns || [];
      return (result.data || []).map(function (item) {
        const obj = {};
        columns.forEach(function (name, index) { obj[name] = item.row[index]; });
        return obj;
      });
    }

    // The Cypher here mirrors what the Worker runs; keeping both in one place
    // would be nicer, but a browser must not depend on server source.
    const NODE_RETURN = "e.canonical_key AS key, e.name AS name, e.entity_type AS entity_type, " +
      "labels(e) AS labels, e.jurisdiction AS jurisdiction, e.confidence AS confidence, " +
      "e.mention_count AS mention_count, e.betweenness AS betweenness, e.anomaly_score AS anomaly_score, " +
      "e.degree_spike AS degree_spike, e.offshore_cluster_ratio AS offshore_cluster_ratio, " +
      "e.cluster_id AS cluster_id, e.risk_score AS risk_score, e.first_seen AS first_seen, " +
      "e.last_seen AS last_seen, e.aliases AS aliases, e.source_ids AS source_ids, e.doc_ids AS doc_ids, " +
      "e.metrics_at AS metrics_at, e.reg_number AS reg_number, e.imo AS imo, e.mmsi AS mmsi, " +
      "e.tail_number AS tail_number, e.transponder AS transponder, e.icao24 AS icao24, " +
      "e.lei AS lei, e.wikidata_id AS wikidata_id, e.address_key AS address_key";

    const EDGE_RETURN = "id(r) AS id, startNode(r).canonical_key AS source, endNode(r).canonical_key AS target, " +
      "type(r) AS type, r.weight AS weight, r.confidence AS confidence, r.source_weight AS source_weight, " +
      "r.method AS method, r.observations AS observations, r.evidence AS evidence, " +
      "r.evidence_scores AS evidence_scores, r.verb AS verb, r.source_id AS source_id, r.doc_id AS doc_id, " +
      "r.run_id AS run_id, r.negated AS negated, r.hedged AS hedged, r.first_seen AS first_seen, r.last_seen AS last_seen";

    return {
      name: "neo4j",
      label: "neo4j http",
      isLive: true,
      kind: "neo4j",

      async health() {
        const res = await cypher([
          { statement: "MATCH (e:Entity) WITH count(e) AS nodes MATCH ()-[r]->() RETURN nodes, count(r) AS edges" },
        ], "health");
        const row = rows(res.results[0] || {})[0] || { nodes: 0, edges: 0 };
        return {
          ok: true, source: "neo4j", version: "direct", _elapsed: res.elapsed,
          graph: { configured: true, database: config.database || "neo4j", engine: "neo4j-http", nodes: row.nodes, edges: row.edges },
        };
      },

      async overview(opts) {
        const limit = clamp(toNumber(opts.limit, 250), 1, 1200);
        // Interpolated into Cypher, so it comes off an allowlist — a property
        // name cannot be a parameter and must never be attacker-controlled.
        const metric = SORTABLE_METRICS.indexOf(opts.metric) >= 0 ? opts.metric : "anomaly_score";
        const res = await cypher([
          {
            statement: "MATCH (e:Entity) WITH e ORDER BY coalesce(e." + metric + ", 0) DESC, e.mention_count DESC LIMIT $limit RETURN " + NODE_RETURN,
            parameters: { limit: limit },
          },
          {
            statement: "MATCH (e:Entity) WITH e ORDER BY coalesce(e." + metric + ", 0) DESC, e.mention_count DESC LIMIT $limit " +
                       "WITH collect(e) AS keep UNWIND keep AS a MATCH (a)-[r]->(b) WHERE b IN keep RETURN " + EDGE_RETURN,
            parameters: { limit: limit },
          },
        ], "overview");
        return {
          ok: true, source: "neo4j", _elapsed: res.elapsed,
          nodes: rows(res.results[0] || {}), edges: rows(res.results[1] || {}),
        };
      },

      async search(q, opts) {
        const limit = clamp(toNumber(opts.limit, 12), 1, 50);
        const needle = String(q || "").trim();
        if (!needle) return { ok: true, source: "neo4j", nodes: [], count: 0 };
        const res = await cypher([
          {
            statement: "MATCH (e:Entity) WHERE e.name CONTAINS $q OR e.canonical_key CONTAINS $q " +
                       "OR any(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS toLower($q)) " +
                       "OR e.reg_number = $q OR e.imo = $q OR e.mmsi = $q OR e.tail_number = $q " +
                       "OR e.transponder = $q OR e.lei = $q OR e.wikidata_id = $q " +
                       "RETURN " + NODE_RETURN + ", size([(e)--() | 1]) AS degree " +
                       "ORDER BY coalesce(e.anomaly_score,0) DESC, e.mention_count DESC LIMIT $limit",
            parameters: { q: needle, limit: limit },
          },
        ], "search");
        const nodes = rows(res.results[0] || {});
        return { ok: true, source: "neo4j", nodes: nodes, count: nodes.length, _elapsed: res.elapsed };
      },

      async node(key) {
        const res = await cypher([
          { statement: "MATCH (e:Entity {canonical_key: $key}) RETURN " + NODE_RETURN, parameters: { key: key } },
          { statement: "MATCH (e:Entity {canonical_key: $key})-[r]-(o:Entity) RETURN " + EDGE_RETURN, parameters: { key: key } },
          {
            statement: "MATCH (d:Document)-[m:MENTIONS]->(e:Entity {canonical_key: $key}) " +
                       "OPTIONAL MATCH (d)-[:FROM_SOURCE]->(s:Source) " +
                       "RETURN d.doc_id AS doc_id, d.title AS title, d.url AS url, d.published_at AS published_at, " +
                       "d.fetched_at AS fetched_at, s.source_id AS source_id, s.name AS source_name, " +
                       "m.count AS count, m.confidence AS confidence, m.surface_forms AS surface_forms " +
                       "ORDER BY m.confidence DESC LIMIT 40",
            parameters: { key: key },
          },
        ], "node:" + key);
        const node = rows(res.results[0] || {})[0];
        if (!node) return { ok: false, source: "neo4j", error: { code: "not_found", message: "No entity " + key } };
        return { ok: true, source: "neo4j", node: node, edges: rows(res.results[1] || {}), citations: rows(res.results[2] || {}), _elapsed: res.elapsed };
      },

      async neighbors(key, opts) {
        const depth = clamp(toNumber(opts.depth, 1), 1, 4);
        const limit = clamp(toNumber(opts.limit, 250), 1, 1200);
        const res = await cypher([
          {
            statement: "MATCH (root:Entity {canonical_key: $key}) " +
                       "MATCH (root)-[rels*1.." + depth + "]-(other:Entity) " +
                       "WITH root, other, rels LIMIT $limit " +
                       "UNWIND rels AS r WITH DISTINCT r RETURN " + EDGE_RETURN,
            parameters: { key: key, limit: limit * 2 },
          },
        ], "neighbors:" + key);
        const edges = rows(res.results[0] || {});
        const keys = new Set([key]);
        edges.forEach(function (e) { keys.add(e.source); keys.add(e.target); });
        const list = Array.from(keys);
        const nodeRes = await cypher([
          {
            statement: "UNWIND $keys AS k MATCH (e:Entity {canonical_key: k}) RETURN " + NODE_RETURN,
            parameters: { keys: list },
          },
        ], "neighbors-nodes:" + key);
        return {
          ok: true, source: "neo4j", root: key, depth: depth,
          nodes: rows(nodeRes.results[0] || {}), edges: edges, truncated: edges.length >= limit * 2,
        };
      },

      async path(from, to, opts) {
        const maxHops = clamp(toNumber(opts.maxHops, 6), 1, 12);
        const direction = ["outgoing", "incoming", "undirected"].indexOf(opts.direction) >= 0 ? opts.direction : "undirected";
        const left = direction === "incoming" ? "<-" : "-";
        const right = direction === "outgoing" ? "->" : "-";
        const res = await cypher([
          {
            statement: "MATCH (a:Entity {canonical_key: $from}), (b:Entity {canonical_key: $to}) " +
                       "MATCH p = shortestPath((a)" + left + "[*1.." + maxHops + "]" + right + "(b)) " +
                       "RETURN [n IN nodes(p) | n.canonical_key] AS keys, " +
                       "[r IN relationships(p) | {id: id(r), type: type(r), weight: r.weight, confidence: r.confidence, " +
                       "method: r.method, source_id: r.source_id, doc_id: r.doc_id}] AS rels, " +
                       "length(p) AS hops",
            parameters: { from: from, to: to },
          },
        ], "path:" + from + ":" + to);
        const row = rows(res.results[0] || {})[0];
        if (!row || !row.keys || !row.keys.length) {
          return { ok: true, source: "neo4j", found: false, hops: 0, nodes: [], edges: [], alternatives: [] };
        }
        return {
          ok: true, source: "neo4j", found: true, hops: row.hops,
          nodeKeys: row.keys, edgeDescriptors: row.rels, nodes: [], edges: [], alternatives: [],
        };
      },

      async table(opts) {
        // The direct provider keeps table paging client-side: it is a dev tool.
        const res = await cypher([
          { statement: "MATCH (e:Entity) RETURN " + NODE_RETURN + " LIMIT $limit", parameters: { limit: clamp(toNumber(opts.limit, 500), 1, 2000) } },
        ], "table");
        return { ok: true, source: "neo4j", nodes: rows(res.results[0] || {}), edges: [], docs: [] };
      },
    };
  }

  /** Resolve the provider the current configuration asks for. */
  function resolveProvider() {
    if (config.mode === "worker") return createWorkerProvider();
    if (config.mode === "neo4j") return createNeo4jProvider();
    return DemoProvider;
  }

  /* ===========================================================================
     7. Normalisation + the graph algorithms that run client-side
     ======================================================================== */

  /**
   * Providers return slightly different shapes (the Worker already normalises,
   * Neo4j returns columns, the demo returns full objects). Everything converges
   * here so the rest of the file only ever sees one shape.
   */
  function normalizeNode(raw) {
    if (!raw) return null;
    const key = String(raw.key || raw.canonical_key || raw.id || "").trim();
    if (!key) return null;
    const labels = Array.isArray(raw.labels) ? raw.labels.filter(Boolean).map(String)
      : typeof raw.labels === "string" ? raw.labels.split(",").map(function (s) { return s.trim(); }).filter(Boolean)
      : [];
    const type = raw.entity_type || pickTypeFromLabels(labels) || "Unknown";
    const props = raw.props && typeof raw.props === "object" ? raw.props : {};
    ["reg_number", "imo", "mmsi", "tail_number", "transponder", "icao24", "lei", "wikidata_id", "wikipedia_id",
     "address_key", "jurisdiction", "shell_risk", "flag", "nationality"].forEach(function (name) {
      if (raw[name] !== undefined && raw[name] !== null && props[name] === undefined) props[name] = raw[name];
    });
    return {
      key: key,
      name: String(raw.name || key),
      entity_type: type,
      labels: labels.length ? labels : ["Entity", type],
      jurisdiction: raw.jurisdiction || props.jurisdiction || "",
      confidence: toNumber(raw.confidence, null),
      mention_count: toNumber(raw.mention_count, 0),
      degree: toNumber(raw.degree, 0),
      betweenness: toNumber(raw.betweenness, null),
      anomaly_score: toNumber(raw.anomaly_score, null),
      degree_spike: toNumber(raw.degree_spike, null),
      offshore_cluster_ratio: toNumber(raw.offshore_cluster_ratio, null),
      cluster_id: raw.cluster_id === null || raw.cluster_id === undefined ? "" : String(raw.cluster_id),
      risk_score: toNumber(raw.risk_score, null),
      first_seen: raw.first_seen || "",
      last_seen: raw.last_seen || "",
      aliases: asList(raw.aliases),
      source_ids: asList(raw.source_ids),
      doc_ids: asList(raw.doc_ids),
      metrics_at: raw.metrics_at || "",
      props: props,
      matched_on: raw.matched_on || "",
      _raw: raw,
    };
  }

  function asList(value) {
    if (Array.isArray(value)) return value.filter(function (v) { return v !== null && v !== undefined && v !== ""; }).map(String);
    if (typeof value === "string" && value) return value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    return [];
  }

  function pickTypeFromLabels(labels) {
    const order = ["Person", "Organization", "Location", "Craft", "Company"];
    for (let i = 0; i < order.length; i++) if (labels.indexOf(order[i]) >= 0) return order[i];
    return "";
  }

  function normalizeEdge(raw, index) {
    if (!raw) return null;
    const source = String(raw.source || raw.from || raw.start || "").trim();
    const target = String(raw.target || raw.to || raw.end || "").trim();
    if (!source || !target) return null;
    const type = String(raw.type || raw.rel_type || "RELATED_TO").toUpperCase();
    const id = String(raw.id || raw.rel_id || ("e:" + source + ":" + type + ":" + target));
    return {
      id: id,
      source: source,
      target: target,
      type: type,
      group: REL_TYPES[type] || "weak",
      weight: toNumber(raw.weight, 0),
      confidence: toNumber(raw.confidence, 0),
      source_weight: toNumber(raw.source_weight, null),
      method: raw.method || "",
      observations: toNumber(raw.observations, 1),
      evidence: asList(raw.evidence),
      evidence_scores: Array.isArray(raw.evidence_scores) ? raw.evidence_scores.map(function (v) { return toNumber(v, 0); }) : [],
      verb: raw.verb || "",
      source_id: raw.source_id || "",
      doc_id: raw.doc_id || "",
      run_id: raw.run_id || "",
      negated: Boolean(raw.negated),
      hedged: Boolean(raw.hedged),
      passive: Boolean(raw.passive),
      first_seen: raw.first_seen || "",
      last_seen: raw.last_seen || "",
      calculated: CALCULATED_TYPES.indexOf(type) >= 0 || raw.method === "calculated" || raw.calculated === true,
      _index: index,
    };
  }

  function normalizeDoc(raw) {
    if (!raw) return null;
    const id = String(raw.doc_id || raw.id || "").trim();
    if (!id) return null;
    return {
      doc_id: id,
      title: raw.title || "(untitled document)",
      url: raw.url || "",
      source_id: raw.source_id || "",
      source_name: raw.source_name || raw.source_id || "",
      source_weight: toNumber(raw.source_weight, null),
      published_at: raw.published_at || "",
      fetched_at: raw.fetched_at || "",
      content_hash: raw.content_hash || "",
      count: toNumber(raw.count, null),
      confidence: toNumber(raw.confidence, null),
      surface_forms: asList(raw.surface_forms),
      entity_count: toNumber(raw.entity_count, null),
    };
  }

  function citationsFor(node, data) {
    const wanted = new Set((node.doc_ids || []).concat([]));
    return data.docs.filter(function (doc) { return wanted.has(doc.doc_id); }).map(function (doc) {
      return Object.assign({}, doc, { count: null, confidence: node.confidence, surface_forms: [node.name] });
    });
  }

  /**
   * How well a node matches a normalised search needle. Deliberately tiered:
   * an exact identifier beats a name prefix, which beats a substring, which
   * beats a token match. Centrality breaks ties so that between two
   * equally-named matches the better-connected entity comes first.
   */
  function scoreMatch(node, needle) {
    if (!needle) return 0;
    const key = String(node.key || "");
    const name = normalizeText(node.name);
    const props = node.props || {};
    const identifiers = [props.reg_number, props.imo, props.mmsi, props.tail_number, props.transponder,
      props.icao24, props.lei, props.wikidata_id, props.wikipedia_id, key]
      .filter(Boolean).map(function (v) { return normalizeText(v); });

    let score = 0;
    let matched = "";

    if (name === needle) { score = 100; matched = "name"; }
    else if (name.startsWith(needle)) { score = 78; matched = "prefix"; }
    else if (name.indexOf(needle) >= 0) { score = 58; matched = "substring"; }

    for (let i = 0; i < identifiers.length; i++) {
      const id = identifiers[i];
      if (id === needle) { score = Math.max(score, 120); matched = "identifier"; break; }
      if (id.indexOf(needle) >= 0 && needle.length >= 3) score = Math.max(score, 66), matched = matched || "identifier";
    }

    (node.aliases || []).forEach(function (alias) {
      const a = normalizeText(alias);
      if (!a) return;
      if (a === needle) score = Math.max(score, 92), matched = matched || "alias";
      else if (a.startsWith(needle)) score = Math.max(score, 70), matched = matched || "alias";
      else if (a.indexOf(needle) >= 0) score = Math.max(score, 52), matched = matched || "alias";
    });

    if (!score) {
      const tokens = name.split(" ").filter(Boolean);
      const needleTokens = needle.split(" ").filter(Boolean);
      const hits = needleTokens.filter(function (t) { return tokens.indexOf(t) >= 0; }).length;
      if (hits && hits === needleTokens.length) { score = 46; matched = "token"; }
      else if (hits) { score = 30 * (hits / needleTokens.length); matched = "partial"; }
    }

    if (!score) return 0;
    // Jurisdiction and type are legitimate query words ("kastelion cyprus").
    if (normalizeText(node.jurisdiction || "").indexOf(needle) >= 0) score += 4;
    const typeNeedle = needle.replace(/s$/, "");
    if (normalizeText(node.entity_type || "").indexOf(typeNeedle) >= 0) score += 6;

    const centrality = toNumber(node.anomaly_score, 0) * 6 + toNumber(node.betweenness, 0) * 5 + toNumber(node.degree, 0) * 0.35;
    return round(score + centrality, 3);
  }

  /**
   * Breadth-first N-degree expansion over an in-memory graph. Used by the demo
   * provider and as the offline fallback whenever a live query fails — the
   * console keeps working on whatever it already holds.
   */
  function bfsSubgraph(nodes, edges, rootKey, depth, limit) {
    const adjacency = new Map();
    const nodeByKey = new Map();
    nodes.forEach(function (n) { nodeByKey.set(n.key, n); });
    edges.forEach(function (e) {
      if (!adjacency.has(e.source)) adjacency.set(e.source, []);
      if (!adjacency.has(e.target)) adjacency.set(e.target, []);
      adjacency.get(e.source).push(e);
      adjacency.get(e.target).push(e);
    });

    const maxNodes = clamp(toNumber(limit, 250), 1, 5000);
    const visited = new Set([rootKey]);
    const collected = new Set();
    let frontier = [rootKey];
    let truncated = false;

    for (let hop = 0; hop < clamp(toNumber(depth, 1), 1, 4); hop++) {
      const next = [];
      for (let i = 0; i < frontier.length; i++) {
        const incident = adjacency.get(frontier[i]) || [];
        for (let j = 0; j < incident.length; j++) {
          const edge = incident[j];
          if (collected.size >= maxNodes * 3) { truncated = true; break; }
          collected.add(edge.id);
          const other = edge.source === frontier[i] ? edge.target : edge.source;
          if (!visited.has(other)) {
            if (visited.size >= maxNodes) { truncated = true; continue; }
            visited.add(other);
            next.push(other);
          }
        }
      }
      if (!next.length) break;
      frontier = next;
    }

    // Both endpoints must survive the cap. An edge is collected while walking,
    // before we know whether its far endpoint fits under `maxNodes`, so filtering
    // on `collected` alone can return a tie to a node the subgraph dropped — and a
    // renderer handed that either invents a stub entity or throws. A subgraph is
    // only a subgraph if it is closed over its own edges.
    const keepEdges = edges.filter(function (e) {
      return collected.has(e.id) && visited.has(e.source) && visited.has(e.target);
    });
    const keepNodes = nodes.filter(function (n) { return visited.has(n.key); });
    return { nodes: keepNodes, edges: keepEdges, visited: visited, truncated: truncated, hops: depth };
  }

  /** Edge cost functions for the handshake engine. */
  const PATH_COSTS = Object.freeze({
    hops: function () { return 1; },
    "inverse-weight": function (edge) { return 1.05 - clamp(toNumber(edge.weight, 0), 0, 1); },
    "inverse-confidence": function (edge) { return 1.05 - clamp(toNumber(edge.confidence, 0), 0, 1); },
  });

  /**
   * Dijkstra over an edge list. O((V+E) log V) with a simple binary heap; good
   * to a few thousand nodes, which is far beyond what a canvas should render
   * anyway. Returns the chain plus up to `alternatives` other routes so an
   * analyst can see that two entities are connected more than one way.
   */
  function localShortestPath(nodes, edges, fromKey, toKey, maxHops, costName, direction) {
    const cost = PATH_COSTS[costName] || PATH_COSTS.hops;
    const nodeKeys = new Set(nodes.map(function (n) { return n.key; }));
    if (!nodeKeys.has(fromKey) || !nodeKeys.has(toKey)) {
      return { found: false, reason: "unknown-endpoint", hops: 0, nodes: [], edges: [], alternatives: [] };
    }

    const adjacency = new Map();
    edges.forEach(function (edge) {
      const forward = direction !== "incoming";
      const backward = direction !== "outgoing";
      if (forward) {
        if (!adjacency.has(edge.source)) adjacency.set(edge.source, []);
        adjacency.get(edge.source).push({ edge: edge, next: edge.target });
      }
      if (backward && direction !== "outgoing") {
        if (!adjacency.has(edge.target)) adjacency.set(edge.target, []);
        adjacency.get(edge.target).push({ edge: edge, next: edge.source });
      }
    });

    const dist = new Map([[fromKey, 0]]);
    const prev = new Map();
    const done = new Set();
    const heap = [{ key: fromKey, d: 0 }];

    function push(item) {
      heap.push(item);
      let i = heap.length - 1;
      while (i > 0) {
        const parent = (i - 1) >> 1;
        if (heap[parent].d <= heap[i].d) break;
        const tmp = heap[parent]; heap[parent] = heap[i]; heap[i] = tmp;
        i = parent;
      }
    }
    function pop() {
      const top = heap[0];
      const last = heap.pop();
      if (heap.length) {
        heap[0] = last;
        let i = 0;
        for (;;) {
          const l = i * 2 + 1, r = l + 1;
          let smallest = i;
          if (l < heap.length && heap[l].d < heap[smallest].d) smallest = l;
          if (r < heap.length && heap[r].d < heap[smallest].d) smallest = r;
          if (smallest === i) break;
          const tmp = heap[smallest]; heap[smallest] = heap[i]; heap[i] = tmp;
          i = smallest;
        }
      }
      return top;
    }

    const hopsCap = clamp(toNumber(maxHops, 6), 1, 12);
    while (heap.length) {
      const current = pop();
      if (!current || done.has(current.key)) continue;
      done.add(current.key);
      if (current.key === toKey) break;
      const neighbours = adjacency.get(current.key) || [];
      for (let i = 0; i < neighbours.length; i++) {
        const step = neighbours[i];
        const hops = (prev.get(current.key) ? prev.get(current.key).hops : 0) + 1;
        if (hops > hopsCap) continue;
        const nextDist = current.d + cost(step.edge);
        if (!dist.has(step.next) || nextDist < dist.get(step.next)) {
          dist.set(step.next, nextDist);
          prev.set(step.next, { key: current.key, edge: step.edge, hops: hops });
          push({ key: step.next, d: nextDist });
        }
      }
    }

    if (!done.has(toKey)) {
      return { found: false, reason: "unreachable", hops: 0, nodes: [], edges: [], alternatives: [], searched: done.size };
    }

    const chainKeys = [toKey];
    const chainEdges = [];
    let cursor = toKey;
    while (cursor !== fromKey) {
      const step = prev.get(cursor);
      if (!step) break;
      chainEdges.unshift(step.edge);
      chainKeys.unshift(step.key);
      cursor = step.key;
    }

    const chainNodes = chainKeys.map(function (key) {
      return nodes.find(function (n) { return n.key === key; }) || { key: key, name: key };
    });

    const weights = chainEdges.map(function (e) { return toNumber(e.weight, 0); });
    const confidences = chainEdges.map(function (e) { return toNumber(e.confidence, 0); });

    return {
      found: true,
      hops: chainEdges.length,
      nodes: chainNodes,
      nodeKeys: chainKeys,
      edges: chainEdges,
      cost: round(dist.get(toKey), 4),
      costFunction: costName || "hops",
      minWeight: weights.length ? round(Math.min.apply(null, weights), 3) : null,
      meanWeight: weights.length ? round(weights.reduce(function (a, b) { return a + b; }, 0) / weights.length, 3) : null,
      meanConfidence: confidences.length ? round(confidences.reduce(function (a, b) { return a + b; }, 0) / confidences.length, 3) : null,
      alternatives: [],
      searched: done.size,
      local: true,
    };
  }

  /** Alternative chains: remove each edge of the found path in turn, re-run. */
  function alternativePaths(nodes, edges, found, fromKey, toKey, maxHops, costName, direction, count) {
    if (!found || !found.edges || !found.edges.length) return [];
    const out = [];
    const seen = new Set([found.edges.map(function (e) { return e.id; }).sort().join("|")]);
    for (let i = 0; i < found.edges.length && out.length < (count || 3); i++) {
      const blocked = found.edges[i].id;
      const subset = edges.filter(function (e) { return e.id !== blocked; });
      const alt = localShortestPath(nodes, subset, fromKey, toKey, maxHops, costName, direction);
      if (!alt.found) continue;
      const signature = alt.edges.map(function (e) { return e.id; }).sort().join("|");
      if (seen.has(signature)) continue;
      seen.add(signature);
      alt.removed = blocked;
      out.push(alt);
    }
    out.sort(function (a, b) { return a.hops - b.hops || a.cost - b.cost; });
    return out;
  }

  /** Client-side table: filtering, sorting and paging over loaded rows. */
  function localTable(nodes, edges, docs, opts) {
    const subject = opts.subject || "nodes";
    if (subject === "edges") return { ok: true, subject: "edges", rows: edges, total: edges.length };
    if (subject === "sources") return { ok: true, subject: "sources", rows: docs, total: docs.length };
    return { ok: true, subject: "nodes", rows: nodes, total: nodes.length };
  }

  /* --- size mapping ------------------------------------------------------ */

  /**
   * Map a metric value onto a node radius. The task asks for sizing by
   * betweenness centrality; every other metric uses the same curve so switching
   * is comparable. Values are normalised against the loaded set, then pushed
   * through a sqrt so a single dominant hub does not flatten everything else.
   */
  function sizeScale(values) {
    const finite = values.filter(function (v) { return Number.isFinite(v) && v > 0; });
    if (!finite.length) return { min: 0, max: 1, spread: false };
    const min = Math.min.apply(null, finite);
    const max = Math.max.apply(null, finite);
    return { min: min, max: max, spread: max > min * 1.0001 };
  }

  function metricValue(node, metric) {
    if (metric === "degree") return toNumber(node.degree, 0);
    if (metric === "mention_count") return toNumber(node.mention_count, 0);
    const value = node[metric];
    if (value === null || value === undefined || value === "" || !Number.isFinite(toNumber(value, NaN))) {
      const fallback = (SIZE_METRICS[metric] || {}).fallback;
      if (fallback) return metricValue(node, fallback);
      return null;
    }
    return toNumber(value, null);
  }

  function sizeFor(value, scale, minPx, maxPx) {
    if (value === null || value === undefined || !scale.spread) return Math.round((minPx + maxPx) / 2 * 0.72);
    const normalised = clamp((value - scale.min) / (scale.max - scale.min || 1), 0, 1);
    return Math.round(minPx + Math.sqrt(normalised) * (maxPx - minPx));
  }

  /* --- colour ------------------------------------------------------------- */

  function colorForNode(node, colourClusters) {
    if (colourClusters && node.cluster_id) return clusterColor(node.cluster_id);
    const labels = node.labels || [];
    // The most specific label wins: ShellCompany before Company before
    // Organization, so an opacity finding never hides inside a generic colour.
    const priority = ["ShellCompany", "Offshore", "Foundation", "Aircraft", "Vessel", "Vehicle", "Craft",
      "Company", "Person", "Organization", "Location"];
    for (let i = 0; i < priority.length; i++) {
      if (labels.indexOf(priority[i]) >= 0 && ENTITY_TYPES[priority[i]]) return ENTITY_TYPES[priority[i]].color;
    }
    const byType = ENTITY_TYPES[node.entity_type];
    return byType ? byType.color : FALLBACK_COLOR;
  }

  const CLUSTER_PALETTE = Object.freeze([
    "#22d3ee", "#a855f7", "#10b981", "#f59e0b", "#f43f5e", "#38bdf8", "#c084fc",
    "#34d399", "#fbbf24", "#fb7185", "#818cf8", "#2dd4bf",
  ]);

  function clusterColor(clusterId) {
    return CLUSTER_PALETTE[hash32(String(clusterId || "")) % CLUSTER_PALETTE.length];
  }

  /* ===========================================================================
     8. Graph engine (Cytoscape.js)
     ======================================================================== */

  let cy = null;
  let flowTimer = null;

  /**
   * Cytoscape styles.
   *
   * Two deliberate choices here:
   *
   *  1. Colour and size come from `data()` rather than style callbacks. A
   *     callback runs per element per frame during pan/zoom; a data field is
   *     resolved once at style-application time. With 1200 nodes on a laptop GPU
   *     that is the difference between smooth and stuttering.
   *  2. Glow is an `underlay`, not a shadow. Cytoscape draws to a <canvas>, so
   *     CSS box-shadow/filter does not exist here; `underlay-*` is the supported
   *     way to get a halo, and it can be switched off wholesale for reduce-motion
   *     or low-end devices by adding `.no-glow`.
   */
  function cyStyles() {
    return [
      /* ---- nodes ---- */
      {
        selector: "node",
        style: {
          label: "data(label)",
          width: "data(size)",
          height: "data(size)",
          "background-color": "data(color)",
          "background-opacity": 0.92,
          "border-width": 1.6,
          "border-color": "data(border)",
          "border-opacity": 0.95,
          color: "#cfe0f5",
          "font-size": 10.5,
          "font-family": "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "ellipsis",
          "text-max-width": "96px",
          "text-background-color": "#05070d",
          "text-background-opacity": 0.72,
          "text-background-padding": "2px",
          "text-background-shape": "roundrectangle",
          "min-zoomed-font-size": 7,
          "z-index": 10,
          "underlay-color": "data(color)",
          "underlay-padding": 5,
          "underlay-opacity": 0.3,
          shape: "ellipse",
          "overlay-opacity": 0,
        },
      },
      {
        selector: "node.hub",
        style: { shape: "ellipse", "border-width": 2.2 },
      },
      {
        selector: "node.offshore",
        style: { shape: "diamond" },
      },
      {
        selector: "node.craft",
        style: { shape: "vee" },
      },
      {
        selector: "node.place",
        style: { shape: "round-hexagon" },
      },
      { selector: "node.no-glow", style: { "underlay-opacity": 0 } },
      { selector: "node.no-label", style: { label: "" } },
      {
        selector: "node.pinned",
        style: { "border-style": "dashed", "border-width": 2.4 },
      },
      {
        selector: "node.neighbour",
        style: { "border-color": "#22d3ee", "border-width": 2.4, "underlay-padding": 7 },
      },
      {
        selector: "node.active",
        style: {
          "border-color": "#eafcff",
          "border-width": 3.2,
          "underlay-color": "#22d3ee",
          "underlay-padding": 11,
          "underlay-opacity": 0.5,
          "z-index": 999,
          color: "#ffffff",
          "font-size": 12,
        },
      },
      {
        selector: "node.selected",
        style: {
          "overlay-color": "#22d3ee",
          "overlay-opacity": 0.16,
          "overlay-padding": 7,
          "border-color": "#22d3ee",
          "z-index": 900,
        },
      },
      {
        selector: "node.path-node",
        style: {
          "border-color": "#10b981",
          "border-width": 3,
          "underlay-color": "#10b981",
          "underlay-padding": 10,
          "underlay-opacity": 0.48,
          "z-index": 980,
        },
      },
      {
        selector: "node.path-endpoint",
        style: {
          "border-color": "#a855f7",
          "underlay-color": "#a855f7",
          "underlay-opacity": 0.55,
          shape: "star",
          "z-index": 990,
        },
      },
      { selector: "node.dimmed", style: { opacity: 0.13, "underlay-opacity": 0, "text-background-opacity": 0.2 } },
      { selector: "node.flt-hidden", style: { display: "none" } },
      { selector: "node.search-hit", style: { "border-color": "#f59e0b", "border-width": 2.6, "underlay-color": "#f59e0b", "underlay-opacity": 0.4 } },

      /* ---- edges ---- */
      {
        selector: "edge",
        style: {
          curveStyle: "data(curve)",
          width: "data(width)",
          "line-color": "data(color)",
          "line-opacity": 0.62,
          "target-arrow-shape": "triangle",
          "target-arrow-color": "data(color)",
          "arrow-scale": 0.78,
          "curve-style": "data(curve)",
          "z-index": 1,
          "overlay-opacity": 0,
          label: "",
          "font-size": 9,
          color: "#9fb3cf",
          "text-rotation": "autorotate",
          "text-background-color": "#05070d",
          "text-background-opacity": 0.78,
          "text-background-padding": "2px",
          "font-family": "JetBrains Mono, ui-monospace, monospace",
          "min-zoomed-font-size": 9,
        },
      },
      { selector: "edge.weak", style: { "line-style": "dotted", "line-opacity": 0.4 } },
      { selector: "edge.calculated", style: { "line-style": "dashed", "line-dash-pattern": [5, 4] } },
      { selector: "edge.hedged", style: { "line-opacity": 0.34 } },
      {
        selector: "edge.selected",
        style: {
          "line-color": "#22d3ee", "target-arrow-color": "#22d3ee", "line-opacity": 1, "width": 3.4,
          "overlay-color": "#22d3ee", "overlay-opacity": 0.14, "overlay-padding": 4, "z-index": 900,
          label: "data(label)",
        },
      },
      {
        selector: "edge.incident",
        style: { "line-opacity": 0.95, "z-index": 800, width: "data(widthEmph)" },
      },
      {
        selector: "edge.path-edge",
        style: {
          "line-color": "#10b981", "target-arrow-color": "#10b981", "line-opacity": 1,
          width: 3.6, "line-style": "dashed", "line-dash-pattern": [8, 5],
          "z-index": 970, label: "data(label)", "font-size": 9.5, color: "#c9ffe9",
        },
      },
      { selector: "edge.dimmed", style: { "line-opacity": 0.05, label: "" } },
      { selector: "edge.flt-hidden", style: { display: "none" } },
    ];
  }

  function nodeShapeClass(node) {
    const labels = node.labels || [];
    if (labels.indexOf("ShellCompany") >= 0 || labels.indexOf("Offshore") >= 0 || labels.indexOf("Foundation") >= 0) return "offshore";
    if (labels.indexOf("Aircraft") >= 0 || labels.indexOf("Vessel") >= 0 || labels.indexOf("Vehicle") >= 0 || labels.indexOf("Craft") >= 0) return "craft";
    if (node.entity_type === "Location") return "place";
    return "";
  }

  /** Short label: keep the readable part of a name, drop corporate suffixes. */
  function shortLabel(name, max) {
    const text = String(name || "").replace(/\s+(Ltd|Limited|LLC|Inc|AG|SARL|S\.A\.|Pty|PLC|GmbH|Corp|Corporation|Holdings?|Group|Partners?)\.?$/i, "");
    const limit = max || 26;
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }

  /**
   * Build the cytoscape element array from the store. Sizes are normalised over
   * the *loaded* set, so the mapping stays meaningful whether 40 nodes or 1200
   * are on screen.
   */
  function buildElements() {
    const metric = state.render.sizeMetric;
    const nodes = Array.from(state.nodes.values());
    const rawValues = nodes.map(function (n) { return metricValue(n, metric); });
    const scale = sizeScale(rawValues.filter(function (v) { return v !== null; }));
    const colourClusters = state.render.colourClusters;

    const nodeElements = nodes.map(function (node, index) {
      const value = rawValues[index];
      const size = sizeFor(value, scale, 15, 62);
      const color = colorForNode(node, colourClusters);
      const classes = ["ent"];
      const shape = nodeShapeClass(node);
      if (shape) classes.push(shape);
      if (size >= 42) classes.push("hub");
      if (!state.render.glow) classes.push("no-glow");
      if (!state.render.showLabels) classes.push("no-label");
      if (state.hidden.has(node.key)) classes.push("flt-hidden");

      return {
        group: "nodes",
        data: {
          id: node.key,
          label: shortLabel(node.name),
          fullLabel: node.name,
          size: size,
          sizeNorm: value === null ? 0 : clamp((value - scale.min) / ((scale.max - scale.min) || 1), 0, 1),
          color: color,
          border: shade(color, -0.28),
          metric: metric,
          metricValue: value === null ? null : round(value, 4),
          entityKey: node.key,
          cluster: node.cluster_id || "",
          type: node.entity_type,
        },
        classes: classes,
      };
    });

    const curve = state.render.edgeStyle === "straight" ? "straight" : state.render.edgeStyle === "haystack" ? "haystack" : "bezier";
    const edgeElements = [];
    state.edges.forEach(function (edge) {
      const width = 0.9 + clamp(toNumber(edge.weight, 0), 0, 1) * 3.4;
      const group = edge.group || REL_TYPES[edge.type] || "weak";
      const color = edge.calculated ? "#c084fc" : (REL_GROUPS[group] ? REL_GROUPS[group].color : "#64748b");
      const classes = ["rel"];
      if (edge.calculated) classes.push("calculated");
      if (toNumber(edge.weight, 0) < 0.3) classes.push("weak");
      if (edge.hedged) classes.push("hedged");

      edgeElements.push({
        group: "edges",
        data: {
          id: "rel:" + edge.id,
          edgeId: edge.id,
          source: edge.source,
          target: edge.target,
          type: edge.type,
          group: group,
          label: edge.type.replace(/_/g, " ").toLowerCase(),
          width: round(width, 2),
          widthEmph: round(width + 1.4, 2),
          color: color,
          curve: curve,
          weight: toNumber(edge.weight, 0),
          confidence: toNumber(edge.confidence, 0),
        },
        classes: classes,
      });
    });

    return { elements: nodeElements.concat(edgeElements), scale: scale, curve: curve };
  }

  /** Lighten/darken a hex colour without pulling in a colour library. */
  function shade(hex, amount) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return hex || FALLBACK_COLOR;
    const int = parseInt(m[1], 16);
    const channel = function (shift) {
      const value = Math.round(clamp(((int >> shift) & 0xff) * (1 + amount), 0, 255));
      return value.toString(16).padStart(2, "0");
    };
    return "#" + channel(16) + channel(8) + channel(0);
  }

  function initCy() {
    const container = $("#cy");
    if (!container || typeof cytoscape !== "function") return null;

    if (typeof cytoscapeFcose === "function") {
      try { cytoscape.use(cytoscapeFcose); } catch (_) { /* already registered */ }
    }

    const instance = cytoscape({
      container: container,
      elements: [],
      style: cyStyles(),
      layout: { name: "preset" },
      minZoom: 0.05,
      maxZoom: 7,
      zoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: true,
      selectionType: "additive",
      touchTapThreshold: 12,
      desktopTapThreshold: 6,
      // Finer than Cytoscape's default on purpose: analysts zoom to a single
      // entity inside a dense cluster far more often than they sweep the whole
      // graph, and a default-sensitivity wheel overshoots that. Cytoscape logs a
      // one-line warning about any non-default value — expected, and worth it.
      wheelSensitivity: 0.22,
      pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
      // Viewport optimisations: while the user is moving the canvas, trade detail
      // for frames. On a 1000-node graph this is the single biggest win.
      textureOnViewport: true,
      hideEdgesOnViewport: true,
      motionBlur: false,
      autolock: false,
    });

    bindCyEvents(instance);
    return instance;
  }

  /** Layout options per algorithm, all derived from current render settings. */
  function layoutOptions(name) {
    const nodes = cy ? cy.nodes(":visible").length : 0;
    const animate = state.render.animate && !document.body.classList.contains("reduce-motion") ? "end" : false;
    const repulsion = clamp(toNumber(state.render.repulsion, 1), 0.5, 2.5);

    if (name === "fcose" && typeof cytoscapeFcose === "function") {
      return {
        name: "fcose",
        quality: nodes > 600 ? "eco" : "default",
        animate: animate,
        animationDuration: 620,
        animationEasing: "ease-out",
        randomize: !state.layoutSeeded,
        nodeRepulsion: function () { return 9000 * repulsion; },
        idealEdgeLength: function (edge) {
          // Strong ties pull closer: the layout shows the evidence structure
          // rather than a uniform hairball.
          const weight = clamp(toNumber(edge.data("weight"), 0.3), 0.05, 1);
          return (70 + (1 - weight) * 90) * (0.8 + repulsion * 0.35);
        },
        edgeElasticity: function () { return 0.42; },
        gravity: 0.28,
        gravityRangeCompound: 1.6,
        gravityCompound: 1.1,
        gravityRange: 3.2,
        numIter: nodes > 600 ? 1200 : 2500,
        tile: true,
        tilingPaddingVertical: 12,
        tilingPaddingHorizontal: 12,
        packComponents: true,
        nodeSeparation: 78,
      };
    }
    if (name === "cose") {
      return {
        name: "cose",
        animate: animate === false ? false : true,
        animationDuration: 620,
        randomize: !state.layoutSeeded,
        nodeRepulsion: function () { return 8200 * repulsion; },
        idealEdgeLength: function () { return 92 * repulsion; },
        edgeElasticity: function () { return 0.45; },
        gravity: 0.3,
        numIter: nodes > 600 ? 900 : 2000,
        nodeOverlap: 12,
        padding: 26,
        componentSpacing: 90,
      };
    }
    if (name === "concentric") {
      return {
        name: "concentric",
        animate: animate !== false,
        animationDuration: 520,
        padding: 28,
        minNodeSpacing: 26,
        concentric: function (node) { return toNumber(node.data("sizeNorm"), 0) + toNumber(node.data("size"), 0) / 200; },
        levelWidth: function () { return 0.16; },
      };
    }
    if (name === "breadthfirst") {
      return {
        name: "breadthfirst", animate: animate !== false, animationDuration: 480,
        padding: 24, spacingFactor: 1.16 * repulsion, circle: false, directed: true,
        roots: state.activeKey ? "#\\#" + state.activeKey : undefined,
      };
    }
    if (name === "circle") {
      return { name: "circle", animate: animate !== false, padding: 26, spacingFactor: 1.2, avoidOverlap: true, nodeDimensionsIncludeLabels: false };
    }
    if (name === "grid") {
      return { name: "grid", animate: animate !== false, padding: 24, avoidOverlap: true, condense: false, spacingFactor: 1.1 * repulsion };
    }
    return { name: "preset" };
  }

  let layoutHandle = null;
  function runLayout(name, opts) {
    if (!cy) return;
    const wanted = name || state.render.layout;
    const options = layoutOptions(wanted);
    if (opts && opts.fit === false) options.fit = false;
    else { options.fit = true; options.padding = 46; }

    const started = performance.now();
    try {
      if (layoutHandle && typeof layoutHandle.stop === "function") layoutHandle.stop();
      layoutHandle = cy.layout(options);
      layoutHandle.one("layoutstop", function () {
        state.layoutSeeded = true;
        state.stats.lastMs = Math.round(performance.now() - started);
        updateHud();
        emit("layoutstop", { name: wanted });
      });
      layoutHandle.run();
      $("#sb-engine").textContent = "layout: " + wanted + (typeof cytoscapeFcose === "function" || wanted !== "fcose" ? "" : " (fallback: cose)");
    } catch (err) {
      // A missing extension must never take the console down.
      console.warn("[console] layout failed, falling back to cose", err);
      if (wanted !== "cose") {
        state.render.layout = "cose";
        runLayout("cose", opts);
      } else {
        toast("Layout failed", escapeHtml(err && err.message ? err.message : String(err)), "error");
      }
    }
  }

  /**
   * Push the store into cytoscape.
   *
   * A diff, not a rebuild: removing and re-adding every element discards the
   * layout the user just arranged and costs a full style recomputation. Adding
   * only what is new keeps positions, keeps the animation cheap, and makes
   * "expand this node" feel like growth rather than a reload.
   */
  function applyGraph(opts) {
    if (!cy) return;
    const options = opts || {};
    const built = buildElements();
    const started = performance.now();
    let addedCount = 0;

    cy.startBatch();
    try {
      const existingNodes = new Set();
      const existingEdges = new Set();
      cy.nodes().forEach(function (n) { existingNodes.add(n.id()); });
      cy.edges().forEach(function (e) { existingEdges.add(e.id()); });

      const wantedNodes = new Set();
      const wantedEdges = new Set();
      const addList = [];

      built.elements.forEach(function (item) {
        const id = item.data.id;
        if (item.group === "nodes") {
          wantedNodes.add(id);
          if (existingNodes.has(id)) {
            const node = cy.getElementById(id);
            node.data(item.data);
            node.classes(item.classes.join(" "));
          } else {
            addList.push(item);
            addedCount += 1;
          }
        } else {
          wantedEdges.add(id);
          if (existingEdges.has(id)) {
            const edge = cy.getElementById(id);
            edge.data(item.data);
            edge.classes(item.classes.join(" "));
          } else {
            addList.push(item);
            addedCount += 1;
          }
        }
      });

      // Remove anything no longer in scope (isolate, reset, or a narrower query).
      const stale = [];
      cy.nodes().forEach(function (n) { if (!wantedNodes.has(n.id())) stale.push(n); });
      cy.edges().forEach(function (e) { if (!wantedEdges.has(e.id())) stale.push(e); });
      // `cy.remove()` takes a collection or a selector, not a plain array — an
      // array reaches into cytoscape's internals and throws. This path runs on
      // every scope change that shrinks the graph (isolate, a narrower query, a
      // reset), so it has to be right.
      if (stale.length) cy.remove(cy.collection(stale));

      if (addList.length) cy.add(addList);

      // Edges whose endpoints are not both present would be dropped by cytoscape
      // anyway; counting them keeps the HUD honest about what the API returned.
      state.stats.dangling = 0;
      cy.edges().forEach(function (e) {
        if (e.source().length === 0 || e.target().length === 0) state.stats.dangling += 1;
      });
    } finally {
      cy.endBatch();
    }

    state.stats.lastMs = Math.round(performance.now() - started);
    applyFilters();
    updateHud();
    renderLegend();
    renderTable();
    renderLeaderboard();
    updateInspectorExtras();

    if (options.layout !== false) {
      // Re-run the layout only when topology changed, so expanding one node
      // does not reshuffle a canvas the analyst has already arranged by hand.
      if (options.relayout !== false && addedCount > 0) {
        runLayout(state.render.layout, { fit: options.fit !== false });
      }
    }
    if (options.fit) fitView(60);
    emit("graph:applied", { nodes: cy.nodes().length, edges: cy.edges().length });
  }

  /**
   * Which elements pass the current filters. Filtering is a class toggle, never
   * a removal: the user can flip a checkbox and get the previous layout back
   * without re-querying or re-laying-out anything.
   */
  function applyFilters() {
    if (!cy) return;
    const filters = state.filters;
    const activeTypes = filters.types;                  // empty Set = all on
    const activeRels = filters.relTypes;
    const minWeight = toNumber(filters.minWeight, 0);
    const minConfidence = toNumber(filters.minConfidence, 0);

    cy.startBatch();
    let visibleNodes = 0;
    try {
      cy.nodes().forEach(function (node) {
        const key = node.id();
        const record = state.nodes.get(key);
        let hidden = state.hidden.has(key);
        if (!hidden && record && activeTypes.size) {
          const labels = record.labels || [];
          const type = record.entity_type;
          hidden = !(activeTypes.has(type) || labels.some(function (l) { return activeTypes.has(l); }));
        }
        if (!hidden && filters.text) {
          const needle = normalizeText(filters.text);
          const hay = normalizeText((record ? record.name : node.data("fullLabel")) + " " + key);
          hidden = hay.indexOf(needle) < 0;
        }
        node.toggleClass("flt-hidden", hidden);
        if (!hidden) visibleNodes += 1;
      });

      cy.edges().forEach(function (edge) {
        const record = state.edges.get(edge.data("edgeId"));
        const weight = record ? toNumber(record.weight, 0) : toNumber(edge.data("weight"), 0);
        const confidence = record ? toNumber(record.confidence, 0) : 0;
        const type = edge.data("type");
        let hidden = weight < minWeight - 1e-9 || confidence < minConfidence - 1e-9;
        if (!hidden && activeRels.size && !activeRels.has(type)) hidden = true;
        if (!hidden && record) {
          if (filters.hideCalculated && record.calculated) hidden = true;
          if (filters.hideWeak && (record.type === "MENTIONED_WITH" || record.method === "cooccurrence")) hidden = true;
        }
        // An edge whose endpoint is hidden must hide too, or it renders as a
        // stray line to nowhere.
        if (!hidden && (edge.source().hasClass("flt-hidden") || edge.target().hasClass("flt-hidden"))) hidden = true;
        edge.toggleClass("flt-hidden", hidden);
      });
    } finally {
      cy.endBatch();
    }

    state.stats.visible = visibleNodes;
    updateHud();
    return visibleNodes;
  }

  function fitView(padding) {
    if (!cy || !cy.elements().length) return;
    cy.animate({ fit: { eles: cy.elements(":visible"), padding: padding || 52 } }, { duration: document.body.classList.contains("reduce-motion") ? 0 : 320, easing: "ease-out" });
  }

  function zoomBy(factor) {
    if (!cy) return;
    const zoom = clamp(cy.zoom() * factor, cy.minZoom(), cy.maxZoom());
    cy.animate({ zoom: zoom, center: { eles: cy.elements(":visible").length ? undefined : undefined } }, { duration: 140 });
  }

  /* ---- selection & focus ------------------------------------------------- */

  function setActive(key, opts) {
    if (!cy) return;
    const options = opts || {};
    state.activeKey = key || null;

    cy.startBatch();
    cy.nodes().removeClass("active neighbour");
    if (key) {
      const node = cy.getElementById(key);
      if (node.length) {
        node.addClass("active");
        node.neighborhood("node:visible").addClass("neighbour");
        node.connectedEdges().addClass("incident");
      }
    }
    cy.endBatch();

    $("#focus-name").textContent = key ? shortLabel((state.nodes.get(key) || {}).name || key, 22) : "whole graph";
    $("#sb-scope").textContent = "scope: " + state.scope.label;
    renderPathActivePanel();
    renderInspector();
    if (options.center && key) centerOn(key);
    emit("active", { key: key });
  }

  function centerOn(key, zoom) {
    if (!cy) return;
    const node = cy.getElementById(key);
    if (!node.length) return;
    cy.animate({
      center: { eles: node },
      zoom: zoom || clamp(Math.max(cy.zoom(), 0.9), cy.minZoom(), cy.maxZoom()),
    }, { duration: document.body.classList.contains("reduce-motion") ? 0 : 300, easing: "ease-out" });
  }

  function selectElements(ids, additive) {
    if (!cy) return;
    if (!additive) cy.elements().unselect();
    ids.forEach(function (id) {
      const ele = cy.getElementById(id);
      if (ele.length) ele.select();
    });
    state.selection = cy.elements(":selected").map(function (e) { return e.id(); });
    renderInspector();
  }

  function clearSelection() {
    if (!cy) return;
    cy.elements().unselect();
    cy.nodes().removeClass("path-node path-endpoint search-hit");
    cy.edges().removeClass("path-edge");
    state.selection = [];
    state.pathSelection = { from: null, to: null };
    stopPathFlow();
    renderInspector();
  }

  /** Highlight a path result on the canvas and dim everything else. */
  function highlightPath(result) {
    if (!cy || !result || !result.found) return;
    const nodeKeys = result.nodeKeys || (result.nodes || []).map(function (n) { return n.key; });
    const edgeIds = (result.edges || []).map(function (e) { return "rel:" + e.id; });

    cy.startBatch();
    cy.nodes().removeClass("path-node path-endpoint").addClass("dimmed");
    cy.edges().removeClass("path-edge").addClass("dimmed");
    nodeKeys.forEach(function (key) {
      const node = cy.getElementById(key);
      if (node.length) node.removeClass("dimmed").addClass("path-node");
    });
    edgeIds.forEach(function (id) {
      const edge = cy.getElementById(id);
      if (edge.length) edge.removeClass("dimmed").addClass("path-edge");
    });
    if (nodeKeys.length) {
      const first = cy.getElementById(nodeKeys[0]);
      const last = cy.getElementById(nodeKeys[nodeKeys.length - 1]);
      if (first.length) first.addClass("path-endpoint");
      if (last.length) last.addClass("path-endpoint");
    }
    cy.endBatch();

    startPathFlow(edgeIds);
    const eles = cy.collection();
    nodeKeys.forEach(function (k) { const n = cy.getElementById(k); if (n.length) eles.merge(n); });
    edgeIds.forEach(function (id) { const e = cy.getElementById(id); if (e.length) eles.merge(e); });
    if (eles.length) cy.animate({ fit: { eles: eles, padding: 74 } }, { duration: document.body.classList.contains("reduce-motion") ? 0 : 420, easing: "ease-out" });
  }

  function clearPathHighlight() {
    if (!cy) return;
    cy.startBatch();
    cy.elements().removeClass("dimmed path-node path-endpoint path-edge");
    cy.endBatch();
    stopPathFlow();
  }

  /**
   * Animate the dashes along the found chain so the direction of the connection
   * is obvious at a glance. Bounded: only path edges, ~18 fps, auto-stops, and
   * skipped entirely under reduced motion.
   */
  function startPathFlow(edgeIds) {
    stopPathFlow();
    if (!cy || !edgeIds || !edgeIds.length) return;
    if (document.body.classList.contains("reduce-motion")) return;
    const edges = edgeIds.map(function (id) { return cy.getElementById(id); }).filter(function (e) { return e && e.length; });
    if (!edges.length) return;
    let offset = 0;
    flowTimer = setInterval(function () {
      if (!cy || !cy.elements().length) { stopPathFlow(); return; }
      offset = (offset - 1.4) % 1000;
      cy.startBatch();
      edges.forEach(function (edge) { edge.style("line-dash-offset", offset); });
      cy.endBatch();
    }, 55);
    // Stop after a while: the point is made, and an endless restyle loop burns
    // battery on a laptop for no analytic gain.
    setTimeout(function () { stopPathFlow(); }, 9000);
  }

  function stopPathFlow() {
    if (flowTimer) { clearInterval(flowTimer); flowTimer = null; }
    if (cy) cy.edges(".path-edge").forEach(function (e) { e.style("line-dash-offset", null); });
  }

  /* ---- cytoscape events -------------------------------------------------- */

  function bindCyEvents(instance) {
    let lastTap = 0;

    instance.on("tap", "node", function (event) {
      const node = event.target;
      const key = node.id();
      const additive = event.originalEvent && (event.originalEvent.shiftKey || event.originalEvent.metaKey);

      if (event.originalEvent && event.originalEvent.altKey) {
        hideNode(key);
        return;
      }

      const now = Date.now();
      const doubleTap = now - lastTap < 320 && state.activeKey === key;
      lastTap = now;

      if (!additive) {
        instance.elements().unselect();
        node.select();
      } else {
        node.select();
      }
      state.selection = instance.elements(":selected").map(function (e) { return e.id(); });
      setActive(key, { center: false });
      renderInspector();

      if (doubleTap) expandNode(key, state.depth);
      else if (config.autoexpand) expandNode(key, Math.min(state.depth, 1));
    });

    instance.on("tap", "edge", function (event) {
      const edge = event.target;
      if (!(event.originalEvent && event.originalEvent.shiftKey)) instance.elements().unselect();
      edge.select();
      state.selection = instance.elements(":selected").map(function (e) { return e.id(); });
      renderInspector();
    });

    instance.on("tap", function (event) {
      if (event.target === instance) {
        // Background tap: clear the path highlight but keep the active node so
        // the inspector stays usable while panning.
        clearPathHighlight();
        cy.elements().unselect();
        cy.nodes().removeClass("pinned");
        state.selection = [];
        renderInspector();
        hideContextMenu();
      }
    });

    instance.on("cxttap", "node", function (event) {
      showContextMenu(event, event.target.id());
    });

    instance.on("mouseover", "node", function (event) {
      const node = event.target;
      node.connectedEdges().addClass("incident");
      $("#cy").style.cursor = "pointer";
    });
    instance.on("mouseout", "node", function (event) {
      const node = event.target;
      if (!node.selected() && node.id() !== state.activeKey) node.connectedEdges().removeClass("incident");
      $("#cy").style.cursor = "";
    });
    instance.on("mouseover", "edge", function () { $("#cy").style.cursor = "pointer"; });
    instance.on("mouseout", "edge", function () { $("#cy").style.cursor = ""; });

    // A dragged node is pinned: analysts arrange subgraphs by hand and a layout
    // re-run must not throw that work away.
    instance.on("dragfree", "node", function (event) { event.target.addClass("pinned"); });

    instance.on("select", function () {
      state.selection = instance.elements(":selected").map(function (e) { return e.id(); });
    });
    instance.on("unselect", function () {
      state.selection = instance.elements(":selected").map(function (e) { return e.id(); });
    });

    const onViewport = throttle(function () {
      const zoom = instance.zoom();
      // Labels off when zoomed far out: they overlap into noise and cost frames.
      const hide = zoom < 0.42;
      instance.nodes().toggleClass("no-label", hide || !state.render.showLabels);
    }, 120);
    instance.on("viewport zoom pan", onViewport);

    instance.on("layoutstop", function () {
      instance.nodes(".pinned").forEach(function (n) { n.position(n.data("pinnedPosition") || n.position()); });
    });
  }

  function hideNode(key) {
    if (state.hidden.has(key)) state.hidden.delete(key);
    else state.hidden.add(key);
    applyFilters();
    emit("hidden", { key: key, hidden: state.hidden.has(key) });
  }

  /* ===========================================================================
     9. Ingesting payloads from any provider
     ======================================================================== */

  /**
   * Fold a provider payload into the store, then into the canvas.
   * `mode: "replace"` is used by overview/search/reset; `"merge"` by expansion,
   * so a node the analyst has already arranged keeps its place.
   */
  function mergePayload(payload, opts) {
    const options = opts || {};
    const mode = options.mode || "merge";
    if (mode === "replace") {
      state.nodes.clear();
      state.edges.clear();
      state.docs.clear();
      state.hidden.clear();
    }

    let addedNodes = 0, addedEdges = 0, updatedNodes = 0;
    const rawNodes = payload.nodes || [];
    const rawEdges = payload.edges || [];
    const rawDocs = payload.docs || payload.citations || [];

    rawNodes.forEach(function (raw) {
      const node = normalizeNode(raw);
      if (!node) return;
      if (state.nodes.has(node.key)) {
        const previous = state.nodes.get(node.key);
        // Later, richer data wins; a payload that omits a metric must not erase
        // one the maintenance pass already wrote.
        state.nodes.set(node.key, Object.assign({}, previous, node, {
          degree: Math.max(toNumber(previous.degree, 0), toNumber(node.degree, 0)),
          betweenness: node.betweenness === null ? previous.betweenness : node.betweenness,
          anomaly_score: node.anomaly_score === null ? previous.anomaly_score : node.anomaly_score,
          props: Object.assign({}, previous.props, node.props),
        }));
        updatedNodes += 1;
      } else {
        state.nodes.set(node.key, node);
        addedNodes += 1;
      }
    });

    rawEdges.forEach(function (raw, index) {
      const edge = normalizeEdge(raw, index);
      if (!edge) return;
      if (!state.nodes.has(edge.source) || !state.nodes.has(edge.target)) {
        // The API may return an edge to a node it did not include (budget cut).
        // Keep a stub so the tie is visible rather than silently dropped —
        // hiding evidence is worse than showing a thin node.
        [edge.source, edge.target].forEach(function (key) {
          if (!state.nodes.has(key)) {
            state.nodes.set(key, normalizeNode({ key: key, name: key, entity_type: "Unknown", labels: ["Entity"], _stub: true }));
            addedNodes += 1;
          }
        });
      }
      if (state.edges.has(edge.id)) {
        state.edges.set(edge.id, Object.assign({}, state.edges.get(edge.id), edge));
      } else {
        state.edges.set(edge.id, edge);
        addedEdges += 1;
      }
    });

    rawDocs.forEach(function (raw) {
      const doc = normalizeDoc(raw);
      if (doc && !state.docs.has(doc.doc_id)) state.docs.set(doc.doc_id, doc);
    });

    // Recompute degrees from the edges we actually hold: the server's number can
    // be stale or scoped differently, and a wrong degree mis-sizes every node.
    recomputeDegrees();

    state.stats.nodes = state.nodes.size;
    state.stats.edges = state.edges.size;
    if (payload.truncated !== undefined) state.stats.truncated = Boolean(payload.truncated);
    if (payload.metrics_at) state.stats.metricsAt = payload.metrics_at;
    if (payload.caps) state.stats.caps = payload.caps;
    if (payload.engine) state.stats.engine = payload.engine;

    applyGraph({ layout: options.layout !== false, relayout: options.relayout !== false, fit: options.fit !== false });
    syncTypeFilters();
    syncRelFilters();
    saveHash();

    return { addedNodes: addedNodes, addedEdges: addedEdges, updatedNodes: updatedNodes };
  }

  function recomputeDegrees() {
    state.nodes.forEach(function (node) { node.degree = 0; });
    state.edges.forEach(function (edge) {
      const a = state.nodes.get(edge.source);
      const b = state.nodes.get(edge.target);
      if (a) a.degree += 1;
      if (b) b.degree += 1;
    });
  }

  /* ===========================================================================
     10. Query orchestration
     ======================================================================== */

  function setBusy(on, label) {
    state.busy = Boolean(on);
    $("#cy-busy").hidden = !state.busy;
    $("#search-spinner").hidden = !state.searching;
    const pill = $("#conn-pill");
    if (on) {
      pill.dataset.state = "busy";
      pill.querySelector(".conn-label").textContent = label || "working";
      $("#sb-dot").className = "sb-dot is-busy";
    } else {
      setConnection(state.connection.state, state.connection.label, state.connection.detail);
    }
  }

  function setConnection(kind, label, detail) {
    state.connection.state = kind;
    state.connection.label = label;
    state.connection.detail = detail || "";
    const pill = $("#conn-pill");
    pill.dataset.state = kind;
    pill.querySelector(".conn-label").textContent = label;
    pill.title = detail || label;
    const dot = $("#sb-dot");
    dot.className = "sb-dot" + (kind === "live" ? " is-live" : kind === "error" ? " is-error" : "");
    $("#sb-source").textContent = label;
  }

  function describeError(err) {
    if (!err) return "Unknown error";
    if (err.code === "aborted") return "Request timed out or was superseded.";
    if (err.status === 401 || err.status === 403) {
      return "Rejected by the API (" + err.status + "). Check the token in settings — the Worker needs <code>GRAPH_API_TOKEN</code> or <code>PROXY_AUTH_TOKEN</code>.";
    }
    if (err.status === 404) return "Endpoint not found (404). Is the Worker deployed with the <code>/graph</code> routes?";
    if (err.status === 429) return "Rate limited (429)" + (err.retryAfter ? " — retry in " + err.retryAfter + "s" : "") + ". The Worker budgets graph queries to protect the free-tier database.";
    if (err.status === 503) return "Service unavailable (503). Neo4j may not be configured on the Worker.";
    if (err.status >= 500) return "Server error (" + err.status + "): " + escapeHtml(err.message || "");
    if (err.code === "neo4j_error") return "Neo4j: " + escapeHtml(err.message || "");
    return escapeHtml(err.message || String(err));
  }

  /** Every query goes through here: cache, busy state, error handling, fallback. */
  async function runQuery(purpose, fn, opts) {
    const options = opts || {};
    setBusy(true, options.label || "querying");
    const started = performance.now();
    try {
      const payload = await fn();
      if (!payload || payload.ok === false) {
        const detail = payload && payload.error ? payload.error.message : "Empty response";
        throw Object.assign(new Error(detail), { code: (payload && payload.error && payload.error.code) || "bad_payload" });
      }
      const elapsed = Math.round(performance.now() - started);
      state.connection.latencyMs = elapsed;
      $("#sb-latency").textContent = elapsed + " ms";
      if (payload._elapsed) $("#sb-latency").textContent = payload._elapsed + " ms";
      return payload;
    } catch (err) {
      if (err && err.code === "aborted" && !options.reportAbort) return null;
      console.warn("[console] " + purpose + " failed:", err);
      if (options.fallback) {
        toast("Falling back", describeError(err) + "<br>Showing what is already loaded.", "warn");
        return null;
      }
      toast("Query failed · " + purpose, describeError(err), "error");
      if (state.provider.isLive && options.fallbackToDemo !== false) {
        setConnection("error", "api error", err.message || "");
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function loadOverview(opts) {
    const options = opts || {};
    const metric = state.render.sizeMetric === "flat" ? "anomaly_score" : state.render.sizeMetric;
    const payload = await runQuery("overview", function () {
      return state.provider.overview({ limit: clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 1, 1200), metric: metric });
    }, { label: "loading overview" });
    if (!payload) return false;

    mergePayload(payload, { mode: "replace", relayout: true, fit: true });
    state.scope = { kind: "overview", label: "top " + state.nodes.size + " by " + metric, key: null };
    setActive(null);
    $("#cy-empty").hidden = state.nodes.size > 0;
    $("#sb-scope").textContent = "scope: " + state.scope.label;
    pushHistory("overview", { metric: metric, limit: config.nodeLimit });
    if (options.quiet !== true) {
      toast("Overview loaded", state.nodes.size + " entities · " + state.edges.size + " relationships" +
        (payload.truncated ? " (truncated by the node budget)" : ""), "success");
    }
    return true;
  }

  async function loadSearch(q, opts) {
    const options = opts || {};
    const needle = String(q || "").trim();
    if (!needle) return false;
    state.query = needle;

    const payload = await runQuery("search", function () {
      return state.provider.search(needle, { limit: options.limit || clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 5, 600), type: options.type || "" });
    }, { label: "searching" });
    if (!payload) return false;

    const nodes = payload.nodes || [];
    if (!nodes.length) {
      toast("No match", "Nothing in <b>" + escapeHtml(state.provider.label) + "</b> matches <code>" + escapeHtml(needle) + "</code>.", "warn");
      $("#cy-empty").hidden = state.nodes.size > 0;
      $("#cy-empty-msg").textContent = "No entity matched “" + needle + "”. Try a shorter fragment, a registration number, or an IMO/MMSI.";
      return false;
    }

    // Pull the neighbourhood of every hit so the result is a readable subgraph,
    // not a scatter of isolated points.
    const top = nodes.slice(0, options.expand === false ? 0 : 6);
    mergePayload({ nodes: nodes, edges: payload.edges || [], docs: payload.docs || [] }, {
      mode: options.mode || "replace", relayout: true, fit: true,
    });
    state.scope = { kind: "search", label: "search: " + needle, key: null };
    pushHistory("search", { q: needle });

    let expanded = 0;
    for (const node of top) {
      const key = normalizeNode(node).key;
      const sub = await runQuery("neighbors", function () {
        return state.provider.neighbors(key, { depth: clamp(toNumber(state.depth, 1), 1, 4), limit: clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 1, 1200) });
      }, { label: "expanding", fallback: true, reportAbort: false });
      if (sub) { mergePayload(sub, { mode: "merge", layout: false }); expanded += 1; }
    }
    if (expanded) { runLayout(state.render.layout, { fit: true }); }

    const first = normalizeNode(nodes[0]);
    markSearchHits(nodes.map(function (n) { return normalizeNode(n).key; }));
    setActive(first.key, { center: true });
    $("#cy-empty").hidden = true;
    $("#sb-scope").textContent = "scope: " + state.scope.label;
    if (options.view === "table") switchView("table");
    toast("Search results", "<b>" + nodes.length + "</b> match" + (nodes.length === 1 ? "" : "es") + " · " +
      state.nodes.size + " entities loaded" + (payload.count && payload.count > nodes.length ? " (" + payload.count + " in the graph)" : ""), "success");
    return true;
  }

  function markSearchHits(keys) {
    if (!cy) return;
    cy.startBatch();
    cy.nodes().removeClass("search-hit");
    keys.forEach(function (key) {
      const node = cy.getElementById(key);
      if (node.length) node.addClass("search-hit");
    });
    cy.endBatch();
  }

  /**
   * Expand a node by N hops. Server-side when the provider can; otherwise over
   * the subgraph already in memory, with the result labelled as local so nobody
   * mistakes a partial answer for a complete one.
   */
  async function expandNode(key, depth) {
    if (!key) { toast("No active node", "Select a node first (or search for one).", "warn"); return false; }
    const hops = clamp(toNumber(depth === undefined ? state.depth : depth, 1), 1, 4);
    const payload = await runQuery("neighbors", function () {
      return state.provider.neighbors(key, { depth: hops, limit: clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 1, 1200) });
    }, { label: "expanding " + hops + "-hop", fallback: true });

    if (payload && (payload.nodes || []).length) {
      const added = mergePayload(payload, { mode: "merge", relayout: true, fit: false });
      setActive(key);
      state.scope = { kind: "neighborhood", label: hops + "-hop around " + shortLabel((state.nodes.get(key) || {}).name || key, 18), key: key };
      $("#sb-scope").textContent = "scope: " + state.scope.label;
      pushHistory("expand", { key: key, depth: hops });
      renderNeighbourSummary(added, hops, payload.truncated);
      toast("Expanded " + hops + " hop" + (hops === 1 ? "" : "s"),
        "+" + added.addedNodes + " entities · +" + added.addedEdges + " relationships" +
        (payload.truncated ? " — <b>truncated</b> by the node budget" : ""), "success");
      return true;
    }

    // Offline fallback: expand over what is already loaded.
    const nodes = Array.from(state.nodes.values());
    const edges = Array.from(state.edges.values());
    const local = bfsSubgraph(nodes, edges, key, hops, 1200);
    if (local.nodes.length <= 1) {
      toast("Nothing to expand", "No further neighbours are available locally, and the API could not be reached.", "warn");
      return false;
    }
    local.nodes.forEach(function (n) { if (!state.nodes.has(n.key)) state.nodes.set(n.key, n); });
    local.edges.forEach(function (e) { if (!state.edges.has(e.id)) state.edges.set(e.id, e); });
    recomputeDegrees();
    applyGraph({ layout: true, relayout: true, fit: false });
    setActive(key);
    renderNeighbourSummary({ addedNodes: local.nodes.length, addedEdges: local.edges.length }, hops, local.truncated);
    toast("Expanded locally", hops + "-hop neighbourhood computed from the loaded subgraph — may be incomplete.", "info");
    return true;
  }

  /** Keep only the neighbourhood: the "isolate" action. */
  async function isolateNode(key, depth) {
    if (!key) return false;
    const hops = clamp(toNumber(depth === undefined ? state.depth : depth, 1), 1, 4);
    const payload = await runQuery("neighbors", function () {
      return state.provider.neighbors(key, { depth: hops, limit: clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 1, 1200) });
    }, { label: "isolating", fallback: true });

    if (payload && (payload.nodes || []).length) {
      mergePayload(payload, { mode: "replace", relayout: true, fit: true });
    } else {
      const local = bfsSubgraph(Array.from(state.nodes.values()), Array.from(state.edges.values()), key, hops, 1200);
      state.nodes.clear(); state.edges.clear();
      local.nodes.forEach(function (n) { state.nodes.set(n.key, n); });
      local.edges.forEach(function (e) { state.edges.set(e.id, e); });
      recomputeDegrees();
      applyGraph({ layout: true, relayout: true, fit: true });
    }
    setActive(key, { center: true });
    state.scope = { kind: "isolate", label: "isolated " + hops + "-hop around " + shortLabel((state.nodes.get(key) || {}).name || key, 16), key: key };
    $("#sb-scope").textContent = "scope: " + state.scope.label;
    pushHistory("isolate", { key: key, depth: hops });
    return true;
  }

  function renderNeighbourSummary(added, hops, truncated) {
    const host = $("#neighbour-summary");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(el("div", {}, ["+" + added.addedNodes + " entities, +" + added.addedEdges + " relationships at depth " + hops]));
    if (truncated) host.appendChild(el("div", { class: "warn" }, ["truncated — raise the node budget"]));
  }

  /* ===========================================================================
     11. Search (header)
     ======================================================================== */

  const searchInput = () => $("#search");

  function wireSearch() {
    const input = searchInput();
    const list = $("#search-suggest");
    const clear = $("#search-clear");

    const suggest = debounce(async function () {
      const q = input.value.trim();
      clear.hidden = !q;
      if (!q) { hideSuggestions(); return; }
      if (!config.autosuggest) { hideSuggestions(); return; }

      // Local first: instant, and usually enough once a subgraph is loaded.
      const local = localSearch(q, 8);
      if (local.length) {
        state.suggestions = local.map(function (hit) {
          return { node: hit.node, score: hit.score, origin: "local" };
        });
        renderSuggestions(q);
      }

      // Then the source of truth, which knows about entities not yet loaded.
      if (!state.provider) return;
      const payload = await runQuery("suggest", function () {
        return state.provider.search(q, { limit: 8 });
      }, { label: "suggest", fallback: true, reportAbort: false });
      if (!payload) return;
      const remote = (payload.nodes || []).map(function (raw) {
        return { node: normalizeNode(raw), score: 0, origin: "api" };
      }).filter(function (hit) { return hit.node; });

      const seen = new Set(state.suggestions.map(function (hit) { return hit.node.key; }));
      remote.forEach(function (hit) { if (!seen.has(hit.node.key)) state.suggestions.push(hit); });
      state.suggestions = state.suggestions.slice(0, 10);
      renderSuggestions(q);
    }, 190);

    input.addEventListener("input", function () {
      state.query = input.value;
      clear.hidden = !input.value;
      suggest();
    });

    input.addEventListener("focus", function () {
      if (input.value.trim() && state.suggestions.length) renderSuggestions(input.value.trim());
    });

    input.addEventListener("keydown", function (event) {
      const items = $$(".suggest-item", list);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!items.length) return;
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        state.suggestCursor = (state.suggestCursor + delta + items.length) % items.length;
        paintSuggestCursor(items);
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const cursorItem = items[state.suggestCursor];
        const q = input.value.trim();
        if (!q) return;
        hideSuggestions();
        if (cursorItem) {
          chooseSuggestion(cursorItem.dataset.key, event);
        } else if (event.altKey) {
          loadSearch(q, { view: "table" });
        } else {
          loadSearch(q, { expand: !event.shiftKey, limit: event.shiftKey ? 400 : 60 });
        }
        return;
      }
      if (event.key === "Escape") {
        if (!list.hidden) { hideSuggestions(); return; }
        input.value = "";
        clear.hidden = true;
        state.query = "";
        markSearchHits([]);
        input.blur();
      }
    });

    clear.addEventListener("click", function () {
      input.value = "";
      clear.hidden = true;
      state.query = "";
      hideSuggestions();
      markSearchHits([]);
      input.focus();
    });

    document.addEventListener("click", function (event) {
      if (!$("#search-box").contains(event.target)) hideSuggestions();
    });
  }

  /** Score the nodes already loaded — no round trip, no flicker. */
  function localSearch(q, limit) {
    const needle = normalizeText(q);
    if (!needle) return [];
    const hits = [];
    state.nodes.forEach(function (node) {
      const score = scoreMatch(node, needle);
      if (score > 0) hits.push({ node: node, score: score });
    });
    hits.sort(function (a, b) { return b.score - a.score; });
    return hits.slice(0, limit || 10);
  }

  function renderSuggestions(q) {
    const list = $("#search-suggest");
    const input = searchInput();
    list.innerHTML = "";
    if (!state.suggestions.length) {
      list.appendChild(el("li", { class: "suggest-empty" }, ["No entity matches “" + q + "”"]));
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      state.suggestCursor = -1;
      return;
    }

    const needle = normalizeText(q);
    state.suggestions.forEach(function (hit) {
      const node = hit.node;
      const color = colorForNode(node, false);
      const item = el("li", {
        class: "suggest-item", role: "option", "data-key": node.key, "aria-selected": "false", tabindex: "-1",
      }, [
        el("span", { class: "suggest-dot", style: "background:" + color + ";box-shadow:0 0 8px " + color }),
        el("span", { class: "suggest-main" }, [
          el("span", { class: "suggest-name", html: highlightMatch(escapeHtml(node.name), needle) }),
          el("span", { class: "suggest-sub" }, [
            node.entity_type.toLowerCase(),
            node.jurisdiction ? " · " + node.jurisdiction : "",
            hit.origin === "api" ? " · api" : "",
            node.cluster_id ? " · " + node.cluster_id : "",
          ].join("")),
        ]),
        el("span", { class: "suggest-score" }, [
          node.anomaly_score !== null ? "a " + formatScore(node.anomaly_score, 2) : "d " + formatNumber(node.degree),
        ]),
      ]);
      item.addEventListener("mousedown", function (event) { event.preventDefault(); });
      item.addEventListener("click", function () { chooseSuggestion(node.key); });
      list.appendChild(item);
    });

    list.appendChild(el("li", { class: "suggest-foot" }, [
      el("span", {}, ["↑↓ navigate · ⏎ load · ⇧⏎ all matches · ⎇⏎ table"]),
      el("span", {}, [state.provider.label]),
    ]));

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    state.suggestCursor = -1;
  }

  function highlightMatch(escapedText, needle) {
    if (!needle) return escapedText;
    const index = normalizeText(escapedText).indexOf(needle);
    if (index < 0) return escapedText;
    // Work on the escaped string with a needle-aware scan so the highlight cannot
    // split an HTML entity produced by escapeHtml.
    let i = 0, matched = 0, out = "";
    while (i < escapedText.length && matched < needle.length) {
      if (escapedText[i] === "&") {
        const semi = escapedText.indexOf(";", i);
        if (semi > 0 && semi - i < 8) { i = semi + 1; continue; }
      }
      const ch = escapedText[i];
      if (/[a-z0-9]/i.test(ch)) {
        if (normalizeText(ch) === needle[matched]) { out += "<mark>" + ch + "</mark>"; matched += 1; }
        else out += ch;
      } else {
        out += ch;
      }
      i += 1;
    }
    return out + escapedText.slice(i);
  }

  function paintSuggestCursor(items) {
    items.forEach(function (item, index) {
      const on = index === state.suggestCursor;
      item.classList.toggle("is-cursor", on);
      item.setAttribute("aria-selected", on ? "true" : "false");
      if (on) item.scrollIntoView({ block: "nearest" });
    });
  }

  function hideSuggestions() {
    const list = $("#search-suggest");
    list.hidden = true;
    list.innerHTML = "";
    state.suggestions = [];
    state.suggestCursor = -1;
    searchInput().setAttribute("aria-expanded", "false");
  }

  async function chooseSuggestion(key, event) {
    hideSuggestions();
    const known = state.nodes.get(key);
    if (known) {
      switchView("graph");
      setActive(key, { center: true });
      selectElements([key], false);
      markSearchHits([key]);
      if (config.autoexpand || (event && event.shiftKey)) expandNode(key, state.depth);
      pushHistory("focus", { key: key });
      saveHash();
      return;
    }
    // Not loaded yet: pull its neighbourhood, which is what "open this entity"
    // means in a graph console.
    const payload = await runQuery("node", function () { return state.provider.neighbors(key, { depth: 1, limit: 250 }); }, { label: "loading entity" });
    if (payload && (payload.nodes || []).length) {
      switchView("graph");
      mergePayload(payload, { mode: "merge", relayout: true, fit: true });
      setActive(key, { center: true });
      markSearchHits([key]);
    } else {
      const detail = await runQuery("node", function () { return state.provider.node(key); }, { label: "loading entity" });
      if (detail && detail.node) {
        mergePayload({ nodes: [detail.node], edges: detail.edges || [], docs: detail.docs || [] }, { mode: "merge", relayout: true, fit: true });
        setActive(key, { center: true });
      } else {
        toast("Could not load entity", "The API returned nothing for <code>" + escapeHtml(key) + "</code>.", "error");
      }
    }
    pushHistory("focus", { key: key });
    saveHash();
  }

  /* ===========================================================================
     12. Inspector
     ======================================================================== */

  /**
   * Fetch per-node detail (citations, full property set) the first time a node
   * is inspected. Overview payloads are deliberately thin — pulling documents for
   * 250 nodes nobody clicked would be wasteful.
   */
  async function ensureNodeDetails(key) {
    const node = state.nodes.get(key);
    if (!node || node._details || node._detailsLoading) return node;
    node._detailsLoading = true;
    const payload = await runQuery("node:" + key, function () { return state.provider.node(key); }, { label: "loading detail", fallback: true, reportAbort: false });
    node._detailsLoading = false;
    if (payload && payload.node) {
      const fresh = normalizeNode(payload.node);
      Object.assign(node, fresh, { _details: true, props: Object.assign({}, node.props, fresh.props) });
      (payload.citations || payload.docs || []).forEach(function (raw) {
        const doc = normalizeDoc(raw);
        if (doc) state.docs.set(doc.doc_id, doc);
      });
      (payload.edges || []).forEach(function (raw, index) {
        const edge = normalizeEdge(raw, index);
        if (edge && !state.edges.has(edge.id)) state.edges.set(edge.id, edge);
      });
      recomputeDegrees();
      if (state.activeKey === key) renderInspector();
    }
    return node;
  }

  function renderInspector() {
    const host = $("#inspector-body");
    if (!host) return;
    host.innerHTML = "";

    const selected = cy ? cy.elements(":selected") : null;
    const selectedNodes = selected ? selected.nodes() : null;
    const selectedEdges = selected ? selected.edges() : null;
    const nodeCount = selectedNodes ? selectedNodes.length : 0;
    const edgeCount = selectedEdges ? selectedEdges.length : 0;

    if (nodeCount === 1 && edgeCount === 0) {
      host.appendChild(nodeInspector(selectedNodes[0].id()));
      return;
    }
    if (nodeCount === 0 && edgeCount === 1) {
      host.appendChild(edgeInspector(selectedEdges[0].data("edgeId")));
      return;
    }
    if (nodeCount + edgeCount > 1) {
      host.appendChild(multiInspector(selectedNodes.map(function (n) { return n.id(); }), selectedEdges.map(function (e) { return e.data("edgeId"); })));
      return;
    }

    // Nothing selected: show the active node, or an empty state that says what to do.
    if (state.activeKey && state.nodes.has(state.activeKey)) {
      host.appendChild(nodeInspector(state.activeKey));
      return;
    }

    host.appendChild(el("div", { class: "inspector-empty" }, [
      el("span", { class: "inspector-glyph", text: "◈" }),
      el("p", {}, ["Select a node or an edge to inspect its metadata, metrics and source citations."]),
      el("p", { class: "dim mt-2.5 text-[11px]" }, [
        state.nodes.size ? state.nodes.size + " entities and " + state.edges.size + " relationships are loaded." : "Nothing loaded yet.",
      ]),
    ]));
  }

  function metricBar(label, value, max, cls, note) {
    const numeric = toNumber(value, null);
    const pct = numeric === null ? 0 : clamp(numeric / (max || 1), 0, 1) * 100;
    const wrap = el("div", { class: "metric" }, [
      el("div", { class: "metric-top" }, [
        el("span", { class: "metric-label", text: label }),
        el("span", { class: "metric-value", text: numeric === null ? "not scored" : formatScore(numeric, max && max > 3 ? 0 : 3) }),
      ]),
      el("div", { class: "metric-track" }, [el("div", { class: "metric-fill " + (cls || "f-cyan") })]),
      note ? el("div", { class: "metric-note", text: note }) : null,
    ]);
    // Animate from zero so a change of node is visible rather than instant.
    requestAnimationFrame(function () {
      const fill = wrap.querySelector(".metric-fill");
      if (fill) fill.style.width = pct.toFixed(1) + "%";
    });
    return wrap;
  }

  function tagFor(text, cls) {
    return el("span", { class: "tag " + (cls || "") }, [text]);
  }

  function nodeInspector(key) {
    const node = state.nodes.get(key);
    if (!node) return el("div", { class: "inspector-empty" }, ["Node not loaded."]);

    const color = colorForNode(node, state.render.colourClusters);
    const labels = node.labels || [];
    const offshore = labels.some(function (l) { return OFFSHORE_LABELS.indexOf(l) >= 0; });

    const head = el("div", { class: "ins-head" }, [
      el("div", { class: "ins-type" },
        [tagFor(node.entity_type, "tag-cyan")].concat(
          labels.filter(function (l) { return l !== "Entity" && l !== node.entity_type; })
            .slice(0, 4)
            .map(function (l) { return tagFor(l, OFFSHORE_LABELS.indexOf(l) >= 0 ? "tag-purple" : ""); })
        ).concat(offshore ? [tagFor("opacity risk", "tag-amber")] : [])
      ),
      el("div", { class: "ins-name", text: node.name }),
      el("div", {
        class: "ins-key", title: "Click to copy the canonical key",
        text: node.key + (node._stub ? "  (stub: not returned by the API)" : ""),
        onclick: async function () {
          const ok = await copyText(node.key);
          toast(ok ? "Copied" : "Copy failed", "<code>" + escapeHtml(node.key) + "</code>", ok ? "success" : "warn", 2000);
        },
      }),
      el("div", { class: "ins-actions" }, [
        el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: function () { expandNode(node.key, state.depth); } }, ["Expand " + state.depth + "-hop"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { isolateNode(node.key, state.depth); } }, ["Isolate"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { centerOn(node.key, 1.35); } }, ["Centre"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { setPathEndpoint("from", node); } }, ["Path A"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { setPathEndpoint("to", node); } }, ["Path B"]),
        el("button", { class: "btn btn-danger-ghost btn-sm", type: "button", onclick: function () { hideNode(node.key); } }, ["Hide"]),
      ]),
    ]);

    /* --- metrics ------------------------------------------------------- */
    const anomaly = toNumber(node.anomaly_score, null);
    const betweenness = toNumber(node.betweenness, null);
    const spike = toNumber(node.degree_spike, null);
    const offshoreRatio = toNumber(node.offshore_cluster_ratio, null);
    const maxMentions = Math.max(1, Array.from(state.nodes.values()).reduce(function (acc, n) { return Math.max(acc, toNumber(n.mention_count, 0)); }, 0));

    const recomputed = (betweenness !== null && spike !== null && offshoreRatio !== null)
      ? round(0.4 * clamp(betweenness, 0, 1) + 0.35 * clamp(spike / 3, 0, 1) + 0.25 * clamp(offshoreRatio, 0, 1), 3)
      : null;

    const metrics = el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [
        el("span", {}, ["Centrality & anomaly"]),
        el("span", { class: "dim", text: node.metrics_at ? "scored " + relativeTime(node.metrics_at) : "not scored" }),
      ]),
      metricBar("Betweenness centrality", betweenness, 1, "f-cyan", betweenness === null ? "run graph_analytics.py --centrality" : "share of shortest paths through this node"),
      metricBar("Anomaly score", anomaly, 1, "f-purple", recomputed === null ? null : "0.40·b + 0.35·spike + 0.25·offshore = " + recomputed),
      metricBar("24h degree spike", spike, 3, "f-amber", spike === null ? null : "saturates at 3× the previous day"),
      metricBar("Offshore cluster ratio", offshoreRatio, 1, "f-purple", null),
      metricBar("Risk score", toNumber(node.risk_score, null), 1, "f-rose", "pruning protection threshold: " + 0.4),
    ]);

    /* --- properties ---------------------------------------------------- */
    const props = el("dl", { class: "props" });
    const rows = [
      ["confidence", node.confidence === null ? null : formatScore(node.confidence, 3), true],
      ["mentions", node.mention_count ? formatNumber(node.mention_count) : null, false],
      ["degree", node.degree !== null ? node.degree : null, false],
      ["cluster", node.cluster_id || null, false],
      ["jurisdiction", node.jurisdiction || null, false],
    ];
    Object.keys(node.props || {}).forEach(function (name) {
      const value = node.props[name];
      if (value === null || value === undefined || value === "") return;
      rows.push([name, typeof value === "number" ? round(value, 4) : String(value), false]);
    });
    rows.push(["first seen", node.first_seen ? formatDate(node.first_seen) : null, false]);
    rows.push(["last seen", node.last_seen ? formatDate(node.last_seen) + " (" + relativeTime(node.last_seen) + ")" : null, false]);
    rows.forEach(function (row) {
      if (row[1] === null || row[1] === undefined || row[1] === "") return;
      props.appendChild(el("dt", { text: row[0] }));
      props.appendChild(el("dd", { class: row[2] ? "is-strong mono" : "mono", text: row[1] }));
    });

    const aliases = (node.aliases || []).length ? el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Aliases (" + node.aliases.length + ")"])]),
      el("div", { class: "alias-list" }, node.aliases.slice(0, 12).map(function (alias) { return tagFor(alias, ""); })),
    ]) : null;

    /* --- relationships -------------------------------------------------- */
    const incident = Array.from(state.edges.values()).filter(function (edge) {
      return edge.source === node.key || edge.target === node.key;
    }).sort(function (a, b) { return toNumber(b.weight, 0) - toNumber(a.weight, 0); });

    const edgesSection = el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [
        el("span", {}, ["Relationships (" + incident.length + ")"]),
        incident.length > 8 ? el("span", { class: "dim", text: "top 8 by weight" }) : null,
      ]),
    ]);
    incident.slice(0, 8).forEach(function (edge) {
      const otherKey = edge.source === node.key ? edge.target : edge.source;
      const other = state.nodes.get(otherKey);
      const outgoing = edge.source === node.key;
      const item = el("div", {
        class: "edge-item", role: "button", tabindex: "0",
        onclick: function () { selectEdgeInGraph(edge.id); },
        onkeydown: function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectEdgeInGraph(edge.id); } },
      }, [
        el("div", { class: "edge-title", html: (outgoing ? "→ <b>" : "← <b>") + escapeHtml(shortLabel(other ? other.name : otherKey, 34)) + "</b>" }),
        el("div", { class: "edge-type" + (edge.calculated ? " is-calc" : ""), text: edge.type }),
        el("div", { class: "edge-nums" }, [
          el("span", {}, ["w ", el("i", { text: formatScore(edge.weight, 2) })]),
          el("span", {}, ["conf ", el("i", { text: formatScore(edge.confidence, 2) })]),
          el("span", {}, ["×", el("i", { text: String(edge.observations || 1) })]),
          edge.method ? el("span", {}, [el("i", { text: edge.method })]) : null,
        ]),
      ]);
      edgesSection.appendChild(item);
    });
    if (!incident.length) edgesSection.appendChild(el("p", { class: "dim text-[11px]" }, ["No relationships loaded for this entity."]));

    /* --- citations ------------------------------------------------------ */
    const citations = citationsForNode(node);
    const citeSection = el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [
        el("span", {}, ["Source citations (" + citations.length + ")"]),
        node.source_ids && node.source_ids.length ? el("span", { class: "dim", text: node.source_ids.slice(0, 3).join(", ") }) : null,
      ]),
    ]);
    if (!citations.length) {
      citeSection.appendChild(el("p", { class: "dim text-[11px]" }, [
        node._detailsLoading ? "Loading citations…" : "No documents loaded for this entity yet.",
      ]));
    }
    citations.slice(0, 8).forEach(function (doc) {
      citeSection.appendChild(el("div", { class: "citation" }, [
        el("span", { class: "citation-icon", text: "❐" }),
        el("div", {}, [
          el("div", { class: "citation-title", html: doc.url ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(doc.title) + "</a>" : escapeHtml(doc.title) }),
          el("div", { class: "citation-meta" }, [
            el("span", { text: doc.source_name || doc.source_id || "unknown source" }),
            doc.published_at ? el("span", { text: formatDate(doc.published_at) }) : null,
            doc.confidence !== null ? el("span", { text: "conf " + formatScore(doc.confidence, 2) }) : null,
            doc.count ? el("span", { text: doc.count + " mentions" }) : null,
          ]),
          (doc.surface_forms || []).length ? el("div", { class: "citation-meta" }, [
            el("span", { text: "surface: " + doc.surface_forms.slice(0, 4).map(function (s) { return "“" + s + "”"; }).join(" ") }),
          ]) : null,
        ]),
        el("span", { class: "citation-strength", text: doc.source_weight !== null ? "×" + formatScore(doc.source_weight, 1) : "" }),
      ]));
    });

    const wrap = el("div", {}, [head, el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Properties"])]), props,
    ]), metrics, aliases, edgesSection, citeSection]);

    // Live mode: pull the citations and full property set on demand.
    if (state.provider.isLive && !node._details && !node._detailsLoading) ensureNodeDetails(node.key);
    return wrap;
  }

  function citationsForNode(node) {
    const wanted = new Set(node.doc_ids || []);
    const out = [];
    wanted.forEach(function (docId) {
      const doc = state.docs.get(docId);
      if (doc) out.push(doc);
      else out.push({ doc_id: docId, title: docId, url: "", source_name: "", published_at: "", confidence: null, count: null, surface_forms: [] });
    });
    out.sort(function (a, b) { return toNumber(b.confidence, 0) - toNumber(a.confidence, 0); });
    return out;
  }

  function edgeInspector(edgeId) {
    const edge = state.edges.get(edgeId);
    if (!edge) return el("div", { class: "inspector-empty" }, ["Relationship not loaded."]);
    const a = state.nodes.get(edge.source) || { name: edge.source, key: edge.source, entity_type: "Unknown", labels: [] };
    const b = state.nodes.get(edge.target) || { name: edge.target, key: edge.target, entity_type: "Unknown", labels: [] };
    const group = REL_GROUPS[edge.group] || { label: edge.group, color: "#64748b" };

    const head = el("div", { class: "ins-head" }, [
      el("div", { class: "ins-type" }, [
        tagFor(edge.type, edge.calculated ? "tag-purple" : "tag-emerald"),
        tagFor(group.label, ""),
        edge.method ? tagFor(edge.method, "") : null,
        edge.hedged ? tagFor("hedged", "tag-amber") : null,
        edge.negated ? tagFor("negated", "tag-rose") : null,
        edge.passive ? tagFor("passive", "") : null,
      ]),
      el("div", { class: "ins-name", html: '<span style="cursor:pointer" data-goto="' + escapeHtml(a.key) + '">' + escapeHtml(a.name) + "</span>" +
        ' <span class="dim text-xs">' + (edge.calculated ? "⇢" : "→") + "</span> " +
        '<span style="cursor:pointer" data-goto="' + escapeHtml(b.key) + '">' + escapeHtml(b.name) + "</span>" }),
      el("div", { class: "ins-actions" }, [
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { selectElements([edge.source], false); setActive(edge.source, { center: true }); } }, ["Open source"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { selectElements([edge.target], false); setActive(edge.target, { center: true }); } }, ["Open target"]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { setPathEndpoint("from", a); setPathEndpoint("to", b); switchView("path"); } }, ["Handshake"]),
      ]),
    ]);
    head.querySelectorAll("[data-goto]").forEach(function (span) {
      span.addEventListener("click", function () {
        const key = span.dataset.goto;
        selectElements([key], false);
        setActive(key, { center: true });
      });
    });

    const metrics = el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Evidence strength"])]),
      metricBar("Weight", toNumber(edge.weight, 0), 1, "f-cyan", "source trust × extraction method"),
      metricBar("Confidence", toNumber(edge.confidence, 0), 1, "f-emerald", "noisy-OR over " + (edge.observations || 1) + " observation" + (edge.observations === 1 ? "" : "s")),
      edge.source_weight !== null ? metricBar("Source weight", toNumber(edge.source_weight, 0), 1, "f-amber", edge.source_id || "") : null,
    ]);

    const props = el("dl", { class: "props" }, [
      el("dt", { text: "observations" }), el("dd", { class: "mono is-strong", text: String(edge.observations || 1) }),
      el("dt", { text: "method" }), el("dd", { class: "mono", text: edge.method || "—" }),
      edge.verb ? el("dt", { text: "verb" }) : null, edge.verb ? el("dd", { class: "mono", text: edge.verb }) : null,
      edge.rule ? el("dt", { text: "rule" }) : null, edge.rule ? el("dd", { class: "mono", text: String(edge.rule) }) : null,
      el("dt", { text: "first seen" }), el("dd", { class: "mono", text: formatDate(edge.first_seen) }),
      el("dt", { text: "last seen" }), el("dd", { class: "mono", text: formatDate(edge.last_seen) }),
      edge.source_id ? el("dt", { text: "source" }) : null, edge.source_id ? el("dd", { class: "mono", text: edge.source_id }) : null,
      edge.doc_id ? el("dt", { text: "document" }) : null, edge.doc_id ? el("dd", { class: "mono", text: edge.doc_id }) : null,
      edge.run_id ? el("dt", { text: "run" }) : null, edge.run_id ? el("dd", { class: "mono", text: edge.run_id }) : null,
    ].filter(Boolean));

    const evidence = el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Evidence (" + (edge.evidence || []).length + ")"])]),
    ]);
    if (!(edge.evidence || []).length) {
      evidence.appendChild(el("p", { class: "dim text-[11px]" }, ["No evidence text recorded on this relationship."]));
    }
    (edge.evidence || []).slice(0, 6).forEach(function (text, index) {
      const score = (edge.evidence_scores || [])[index];
      evidence.appendChild(el("div", {}, [
        el("div", { class: "evidence", text: String(text) }),
        el("div", { class: "evidence-meta", text: score === undefined ? "" : "evidence score " + formatScore(score, 3) }),
      ]));
    });

    const doc = edge.doc_id ? state.docs.get(edge.doc_id) : null;
    const citation = doc ? el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Citation"])]),
      el("div", { class: "citation" }, [
        el("span", { class: "citation-icon", text: "❐" }),
        el("div", {}, [
          el("div", { class: "citation-title", html: doc.url ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(doc.title) + "</a>" : escapeHtml(doc.title) }),
          el("div", { class: "citation-meta" }, [
            el("span", { text: doc.source_name || doc.source_id }),
            doc.published_at ? el("span", { text: formatDate(doc.published_at) }) : null,
          ]),
        ]),
      ]),
    ]) : null;

    return el("div", {}, [head, metrics, el("div", { class: "ins-section" }, [
      el("div", { class: "ins-section-title" }, [el("span", {}, ["Properties"])]), props,
    ]), evidence, citation]);
  }

  function multiInspector(nodeIds, edgeIds) {
    const nodes = nodeIds.map(function (id) { return state.nodes.get(id); }).filter(Boolean);
    const edges = edgeIds.map(function (id) { return state.edges.get(id); }).filter(Boolean);
    const byType = {};
    nodes.forEach(function (n) { byType[n.entity_type] = (byType[n.entity_type] || 0) + 1; });
    const meanAnomaly = nodes.length ? nodes.reduce(function (acc, n) { return acc + toNumber(n.anomaly_score, 0); }, 0) / nodes.length : 0;
    const meanWeight = edges.length ? edges.reduce(function (acc, e) { return acc + toNumber(e.weight, 0); }, 0) / edges.length : 0;

    return el("div", {}, [
      el("div", { class: "ins-head" }, [
        el("div", { class: "ins-type" }, [tagFor(nodes.length + " nodes", "tag-cyan"), edges.length ? tagFor(edges.length + " edges", "tag-emerald") : null]),
        el("div", { class: "ins-name", text: "Multi-selection" }),
        el("div", { class: "ins-actions" }, [
          el("button", { class: "btn btn-primary btn-sm", type: "button", onclick: function () { isolateSelection(nodes.map(function (n) { return n.key; })); } }, ["Isolate selection"]),
          el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { exportSelectionCsv(nodes, edges); } }, ["Export CSV"]),
          el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: clearSelection }, ["Clear"]),
        ]),
      ]),
      el("div", { class: "ins-section" }, [
        el("div", { class: "ins-section-title" }, [el("span", {}, ["Summary"])]),
        el("dl", { class: "props" }, [
          el("dt", { text: "types" }), el("dd", { class: "mono", text: Object.keys(byType).map(function (t) { return t + " " + byType[t]; }).join(", ") || "—" }),
          el("dt", { text: "mean anomaly" }), el("dd", { class: "mono is-strong", text: formatScore(meanAnomaly, 3) }),
          el("dt", { text: "mean weight" }), el("dd", { class: "mono", text: formatScore(meanWeight, 3) }),
          el("dt", { text: "clusters" }), el("dd", { class: "mono", text: Array.from(new Set(nodes.map(function (n) { return n.cluster_id; }).filter(Boolean))).join(", ") || "—" }),
        ]),
      ]),
      el("div", { class: "ins-section" }, [
        el("div", { class: "ins-section-title" }, [el("span", {}, ["Selected entities"])]),
      ].concat(nodes.slice(0, 40).map(function (node) {
        return el("div", {
          class: "edge-item", role: "button", tabindex: "0",
          onclick: function () { selectElements([node.key], false); setActive(node.key, { center: true }); },
        }, [
          el("div", { class: "edge-title", html: "<b>" + escapeHtml(shortLabel(node.name, 30)) + "</b>" }),
          el("div", { class: "edge-type", text: node.entity_type }),
          el("div", { class: "edge-nums" }, [
            el("span", {}, ["deg ", el("i", { text: String(node.degree) })]),
            node.anomaly_score !== null ? el("span", {}, ["anom ", el("i", { text: formatScore(node.anomaly_score, 2) })]) : null,
            node.jurisdiction ? el("span", {}, [el("i", { text: node.jurisdiction })]) : null,
          ]),
        ]);
      }))),
    ]);
  }

  function isolateSelection(keys) {
    if (!keys.length) return;
    const keep = new Set(keys);
    state.nodes.forEach(function (node, key) { if (!keep.has(key)) state.nodes.delete(key); });
    state.edges.forEach(function (edge, id) {
      if (!keep.has(edge.source) || !keep.has(edge.target)) state.edges.delete(id);
    });
    recomputeDegrees();
    applyGraph({ layout: true, relayout: true, fit: true });
    state.scope = { kind: "selection", label: keys.length + " selected entities", key: null };
    $("#sb-scope").textContent = "scope: " + state.scope.label;
    pushHistory("isolate-selection", { keys: keys.length });
  }

  function exportSelectionCsv(nodes, edges) {
    const rows = nodes.map(function (n) { return NODE_COLUMNS.map(function (c) { return c.value(n); }); });
    const csv = buildCsv(nodes, NODE_COLUMNS);
    downloadText("puppetnet-selection-" + Date.now() + ".csv", csv, "text/csv");
    toast("Exported", rows.length + " entities written to CSV.", "success");
  }

  function selectEdgeInGraph(edgeId) {
    if (!cy) return;
    const ele = cy.getElementById("rel:" + edgeId);
    if (!ele.length) return;
    cy.elements().unselect();
    ele.select();
    state.selection = [ele.id()];
    renderInspector();
  }

  /** Called after data changes to refresh the parts of the inspector that depend on it. */
  function updateInspectorExtras() {
    if (!state.activeKey) return;
    const node = state.nodes.get(state.activeKey);
    if (!node) return;
    $("#focus-name").textContent = shortLabel(node.name, 22);
  }

  /* ===========================================================================
     13. HUD, legend, status bar, leaderboard
     ======================================================================== */

  function updateHud() {
    const visibleNodes = cy ? cy.nodes(":visible").length : state.nodes.size;
    const visibleEdges = cy ? cy.edges(":visible").length : state.edges.size;
    $("#hud-nodes").textContent = formatNumber(state.nodes.size);
    $("#hud-edges").textContent = formatNumber(state.edges.size);
    $("#hud-shown").textContent = formatNumber(visibleNodes) + "/" + formatNumber(visibleEdges);
    $("#hud-ms").textContent = state.stats.lastMs;
    $("#count-nodes").textContent = formatNumber(state.nodes.size);
    $("#count-edges").textContent = formatNumber(state.edges.size);
    $("#count-sources").textContent = formatNumber(state.docs.size);
    $("#cy-empty").hidden = state.nodes.size > 0;

    const caps = state.stats.caps;
    if (caps && caps.node_cap) {
      const nodePct = ((caps.nodes || state.nodes.size) / caps.node_cap * 100).toFixed(2);
      const edgePct = caps.edge_cap ? ((caps.edges || state.edges.size) / caps.edge_cap * 100).toFixed(2) : "—";
      $("#sb-caps").textContent = "aura: " + nodePct + "% nodes · " + edgePct + "% edges";
    } else {
      $("#sb-caps").textContent = "nodes " + state.nodes.size + " · edges " + state.edges.size + (state.stats.dangling ? " · " + state.stats.dangling + " unresolved" : "");
    }
    $("#sb-metrics").textContent = "metrics: " + (state.stats.metricsAt ? relativeTime(state.stats.metricsAt) : "not scored");
  }

  function renderLegend() {
    const host = $("#legend");
    if (!host) return;
    host.innerHTML = "";

    const present = new Map();
    state.nodes.forEach(function (node) {
      const labels = node.labels || [];
      const specific = ["ShellCompany", "Offshore", "Foundation", "Aircraft", "Vessel", "Person", "Organization", "Location", "Company", "Craft"];
      let chosen = node.entity_type;
      for (let i = 0; i < specific.length; i++) {
        if (labels.indexOf(specific[i]) >= 0 && ENTITY_TYPES[specific[i]]) { chosen = specific[i]; break; }
      }
      present.set(chosen, (present.get(chosen) || 0) + 1);
    });

    const ordered = Array.from(present.entries()).sort(function (a, b) {
      const oa = (ENTITY_TYPES[a[0]] || {}).order || 50, ob = (ENTITY_TYPES[b[0]] || {}).order || 50;
      return oa - ob || b[1] - a[1];
    });

    ordered.slice(0, 8).forEach(function (entry) {
      const meta = ENTITY_TYPES[entry[0]] || { color: FALLBACK_COLOR, label: entry[0] };
      host.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-dot", style: "color:" + meta.color }),
        el("span", { text: meta.label + " " + entry[1] }),
      ]));
    });

    if (state.edges.size) {
      host.appendChild(el("span", { class: "legend-sep" }));
      const calculated = Array.from(state.edges.values()).filter(function (e) { return e.calculated; }).length;
      host.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-dot", style: "color:#c084fc" }),
        el("span", { text: "calculated " + calculated }),
      ]));
      host.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "legend-dot", style: "color:#64748b" }),
        el("span", { text: "weak w<0.3" }),
      ]));
    }
    if (state.render.colourClusters) {
      host.appendChild(el("span", { class: "legend-sep" }));
      host.appendChild(el("span", { class: "legend-item" }, [el("span", { text: "colour = cluster_id" })]));
    }
    host.appendChild(el("span", { class: "legend-sep" }));
    host.appendChild(el("span", { class: "legend-item" }, [el("span", { text: "size = " + ((SIZE_METRICS[state.render.sizeMetric] || {}).label || state.render.sizeMetric) })]));
  }

  function renderLeaderboard() {
    const host = $("#leaderboard");
    if (!host) return;
    host.innerHTML = "";

    const ranked = Array.from(state.nodes.values())
      .filter(function (n) { return toNumber(n.anomaly_score, null) !== null; })
      .sort(function (a, b) { return toNumber(b.anomaly_score, 0) - toNumber(a.anomaly_score, 0); })
      .slice(0, 5);

    if (!ranked.length) {
      host.appendChild(el("li", { class: "lb-empty" }, ["No anomaly scores in the current scope. Run graph_analytics.py --centrality, or load the overview."]));
      return;
    }

    const max = toNumber(ranked[0].anomaly_score, 0.0001) || 1;
    ranked.forEach(function (node, index) {
      const score = toNumber(node.anomaly_score, 0);
      const item = el("li", {
        class: "lb-item", role: "button", tabindex: "0", title: node.name + " — " + node.key,
        onclick: function () { chooseSuggestion(node.key); },
        onkeydown: function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); chooseSuggestion(node.key); } },
      }, [
        el("span", { class: "lb-rank", text: String(index + 1) }),
        el("span", { class: "lb-name", text: shortLabel(node.name, 26) }),
        el("span", { class: "lb-score", text: formatScore(score, 2) }),
        el("span", { class: "lb-bar" }, [el("span", { class: "lb-bar-fill" })]),
      ]);
      requestAnimationFrame(function () {
        const fill = item.querySelector(".lb-bar-fill");
        if (fill) fill.style.width = clamp((score / max) * 100, 2, 100).toFixed(1) + "%";
      });
      host.appendChild(item);
    });
  }

  function startClock() {
    const node = $("#sb-clock");
    const tick = function () {
      const now = new Date();
      node.textContent = now.toISOString().slice(11, 19) + " UTC";
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ===========================================================================
     14. Table view
     ======================================================================== */

  /**
   * Column definitions double as the CSV schema, so what an analyst sees on
   * screen is exactly what lands in the export.
   */
  const NODE_COLUMNS = Object.freeze([
    { key: "name", label: "Entity", cls: "cell-name", value: function (n) { return n.name; } },
    { key: "entity_type", label: "Type", value: function (n) { return n.entity_type; }, render: function (n) { return tagFor(n.entity_type, "tag-cyan").outerHTML; } },
    { key: "labels", label: "Labels", value: function (n) { return (n.labels || []).filter(function (l) { return l !== "Entity"; }).join("; "); },
      render: function (n) {
        return (n.labels || []).filter(function (l) { return l !== "Entity" && l !== n.entity_type; }).slice(0, 3)
          .map(function (l) { return tagFor(l, OFFSHORE_LABELS.indexOf(l) >= 0 ? "tag-purple" : "").outerHTML; }).join(" ") || '<span class="dim">—</span>';
      } },
    { key: "jurisdiction", label: "Juris", cls: "cell-mono", value: function (n) { return n.jurisdiction || ""; } },
    { key: "degree", label: "Degree", cls: "cell-num", value: function (n) { return n.degree; }, numeric: true },
    { key: "betweenness", label: "Betweenness", cls: "cell-num", numeric: true,
      value: function (n) { return n.betweenness === null ? "" : formatScore(n.betweenness, 4); },
      render: function (n) { return barCell(n.betweenness, 1, "is-emerald"); } },
    { key: "anomaly_score", label: "Anomaly", cls: "cell-num", numeric: true,
      value: function (n) { return n.anomaly_score === null ? "" : formatScore(n.anomaly_score, 3); },
      render: function (n) { return barCell(n.anomaly_score, 1, "is-purple"); } },
    { key: "confidence", label: "Confidence", cls: "cell-num", numeric: true,
      value: function (n) { return n.confidence === null ? "" : formatScore(n.confidence, 3); },
      render: function (n) { return barCell(n.confidence, 1, ""); } },
    { key: "risk_score", label: "Risk", cls: "cell-num", numeric: true,
      value: function (n) { return n.risk_score === null ? "" : formatScore(n.risk_score, 3); },
      render: function (n) { return barCell(n.risk_score, 1, "is-rose"); } },
    { key: "mention_count", label: "Mentions", cls: "cell-num", numeric: true, value: function (n) { return n.mention_count || 0; } },
    { key: "cluster_id", label: "Cluster", cls: "cell-mono", value: function (n) { return n.cluster_id || ""; } },
    { key: "reg_number", label: "Reg / ID", cls: "cell-mono",
      value: function (n) { return n.props.reg_number || n.props.imo || n.props.mmsi || n.props.tail_number || n.props.lei || n.props.wikidata_id || ""; } },
    { key: "sources", label: "Sources", cls: "cell-mono", value: function (n) { return (n.source_ids || []).join("; "); },
      render: function (n) { return (n.source_ids || []).slice(0, 3).map(function (s) { return tagFor(s, "").outerHTML; }).join(" ") || '<span class="dim">—</span>'; } },
    { key: "last_seen", label: "Last seen", cls: "cell-mono", value: function (n) { return n.last_seen ? formatDate(n.last_seen) : ""; },
      render: function (n) { return '<span title="' + escapeHtml(n.last_seen || "") + '">' + escapeHtml(n.last_seen ? formatDate(n.last_seen) : "—") + "</span>"; } },
    { key: "key", label: "Canonical key", cls: "cell-mono", value: function (n) { return n.key; } },
  ]);

  const EDGE_COLUMNS = Object.freeze([
    { key: "source", label: "Subject", cls: "cell-name", value: function (e) { return nameOf(e.source); } },
    { key: "type", label: "Predicate", cls: "cell-mono", value: function (e) { return e.type; },
      render: function (e) { return tagFor(e.type, e.calculated ? "tag-purple" : "tag-emerald").outerHTML; } },
    { key: "target", label: "Object", cls: "cell-name", value: function (e) { return nameOf(e.target); } },
    { key: "weight", label: "Weight", cls: "cell-num", numeric: true, value: function (e) { return formatScore(e.weight, 3); },
      render: function (e) { return barCell(e.weight, 1, e.weight < 0.3 ? "is-amber" : ""); } },
    { key: "confidence", label: "Confidence", cls: "cell-num", numeric: true, value: function (e) { return formatScore(e.confidence, 3); },
      render: function (e) { return barCell(e.confidence, 1, "is-emerald"); } },
    { key: "observations", label: "Obs", cls: "cell-num", numeric: true, value: function (e) { return e.observations || 1; } },
    { key: "method", label: "Method", cls: "cell-mono", value: function (e) { return e.method || ""; } },
    { key: "source_id", label: "Source", cls: "cell-mono", value: function (e) { return e.source_id || ""; } },
    { key: "doc_id", label: "Document", cls: "cell-mono", value: function (e) { return e.doc_id || ""; } },
    { key: "evidence", label: "Evidence", value: function (e) { return (e.evidence || []).join(" | "); },
      render: function (e) {
        const text = (e.evidence || [])[0] || "";
        return text ? '<span title="' + escapeHtml(text) + '">' + escapeHtml(text.length > 78 ? text.slice(0, 77) + "…" : text) + "</span>" : '<span class="dim">—</span>';
      } },
    { key: "last_seen", label: "Last seen", cls: "cell-mono", value: function (e) { return e.last_seen ? formatDate(e.last_seen) : ""; } },
  ]);

  const DOC_COLUMNS = Object.freeze([
    { key: "title", label: "Document", cls: "cell-name", value: function (d) { return d.title; },
      render: function (d) {
        return d.url ? '<a href="' + escapeHtml(d.url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(d.title) + "</a>" : escapeHtml(d.title);
      } },
    { key: "source_name", label: "Source", cls: "cell-mono", value: function (d) { return d.source_name || d.source_id || ""; } },
    { key: "source_weight", label: "Trust", cls: "cell-num", numeric: true, value: function (d) { return d.source_weight === null ? "" : formatScore(d.source_weight, 2); },
      render: function (d) { return barCell(d.source_weight, 1, "is-amber"); } },
    { key: "published_at", label: "Published", cls: "cell-mono", value: function (d) { return d.published_at ? formatDate(d.published_at) : ""; } },
    { key: "fetched_at", label: "Fetched", cls: "cell-mono", value: function (d) { return d.fetched_at ? formatDate(d.fetched_at) : ""; } },
    { key: "entities", label: "Entities", cls: "cell-num", numeric: true, value: function (d) { return entitiesForDoc(d.doc_id).length; } },
    { key: "doc_id", label: "Document id", cls: "cell-mono", value: function (d) { return d.doc_id; } },
    { key: "content_hash", label: "Content hash", cls: "cell-mono", value: function (d) { return d.content_hash || ""; } },
  ]);

  function nameOf(key) {
    const node = state.nodes.get(key);
    return node ? node.name : key;
  }

  function entitiesForDoc(docId) {
    const out = [];
    state.nodes.forEach(function (node) {
      if ((node.doc_ids || []).indexOf(docId) >= 0) out.push(node);
    });
    state.edges.forEach(function (edge) {
      if (edge.doc_id === docId) {
        [edge.source, edge.target].forEach(function (key) {
          const node = state.nodes.get(key);
          if (node && out.indexOf(node) < 0) out.push(node);
        });
      }
    });
    return out;
  }

  function barCell(value, max, cls) {
    if (value === null || value === undefined || value === "") return '<span class="dim">—</span>';
    const numeric = toNumber(value, 0);
    const pct = clamp((numeric / (max || 1)) * 100, 0, 100);
    return '<span class="cell-bar"><span class="cell-bar-track"><span class="cell-bar-fill ' + (cls || "") +
      '" style="width:' + pct.toFixed(1) + '%"></span></span><span class="cell-bar-value">' + formatScore(numeric, 3) + "</span></span>";
  }

  function tableColumns() {
    if (state.table.subject === "edges") return EDGE_COLUMNS;
    if (state.table.subject === "sources") return DOC_COLUMNS;
    return NODE_COLUMNS;
  }

  function tableSourceRows() {
    if (state.table.subject === "edges") return Array.from(state.edges.values());
    if (state.table.subject === "sources") return Array.from(state.docs.values());
    return Array.from(state.nodes.values());
  }

  /**
   * The table shows what the graph shows: the same type filters, weight and
   * confidence floors, hidden nodes and text filter apply. A table that quietly
   * includes rows the canvas is hiding is how an analyst exports the wrong thing.
   */
  function tableFilteredRows() {
    const filters = state.filters;
    const text = normalizeText(state.table.text || filters.text || "");
    const typeFilter = state.table.typeFilter || "";
    let rows = tableSourceRows();

    if (state.table.subject === "nodes") {
      rows = rows.filter(function (node) {
        if (state.hidden.has(node.key)) return false;
        if (filters.types.size) {
          const labels = node.labels || [];
          if (!(filters.types.has(node.entity_type) || labels.some(function (l) { return filters.types.has(l); }))) return false;
        }
        if (typeFilter && node.entity_type !== typeFilter && (node.labels || []).indexOf(typeFilter) < 0) return false;
        if (text) {
          const hay = normalizeText(node.name + " " + node.key + " " + node.jurisdiction + " " + (node.aliases || []).join(" ") + " " +
            Object.keys(node.props || {}).map(function (k) { return node.props[k]; }).join(" "));
          if (hay.indexOf(text) < 0) return false;
        }
        return true;
      });
    } else if (state.table.subject === "edges") {
      rows = rows.filter(function (edge) {
        if (state.hidden.has(edge.source) || state.hidden.has(edge.target)) return false;
        if (toNumber(edge.weight, 0) < toNumber(filters.minWeight, 0) - 1e-9) return false;
        if (toNumber(edge.confidence, 0) < toNumber(filters.minConfidence, 0) - 1e-9) return false;
        if (filters.relTypes.size && !filters.relTypes.has(edge.type)) return false;
        if (filters.hideCalculated && edge.calculated) return false;
        if (filters.hideWeak && (edge.type === "MENTIONED_WITH" || edge.method === "cooccurrence")) return false;
        if (typeFilter && edge.type !== typeFilter) return false;
        if (text) {
          const hay = normalizeText(edge.type + " " + nameOf(edge.source) + " " + nameOf(edge.target) + " " +
            edge.method + " " + edge.source_id + " " + edge.doc_id + " " + (edge.evidence || []).join(" "));
          if (hay.indexOf(text) < 0) return false;
        }
        return true;
      });
    } else {
      rows = rows.filter(function (doc) {
        if (typeFilter && doc.source_id !== typeFilter) return false;
        if (text) {
          const hay = normalizeText(doc.title + " " + doc.doc_id + " " + (doc.source_name || "") + " " + (doc.source_id || ""));
          if (hay.indexOf(text) < 0) return false;
        }
        return true;
      });
    }

    return sortRows(rows);
  }

  function sortRows(rows) {
    const sort = state.table.sort || "score-desc";
    const subject = state.table.subject;
    const numeric = function (row, key) {
      if (subject === "nodes") {
        if (key === "score") return toNumber(row.anomaly_score, toNumber(row.betweenness, 0));
        if (key === "degree") return toNumber(row.degree, 0);
        if (key === "confidence") return toNumber(row.confidence, 0);
        if (key === "recent") return row.last_seen ? new Date(row.last_seen).getTime() : 0;
        if (key === "name") return row.name || "";
      }
      if (subject === "edges") {
        if (key === "score" || key === "confidence") return toNumber(row.confidence, 0);
        if (key === "weight") return toNumber(row.weight, 0);
        if (key === "recent") return row.last_seen ? new Date(row.last_seen).getTime() : 0;
        if (key === "name") return row.type || "";
      }
      if (key === "score") return toNumber(row.source_weight, 0);
      if (key === "recent") return row.published_at ? new Date(row.published_at).getTime() : 0;
      if (key === "name") return row.title || "";
      return 0;
    };

    const parts = sort.split("-");
    const key = parts[0], direction = parts[1] === "asc" ? 1 : -1;
    return rows.slice().sort(function (a, b) {
      const av = numeric(a, key), bv = numeric(b, key);
      if (typeof av === "string" || typeof bv === "string") return String(av).localeCompare(String(bv)) * direction;
      if (av === bv) return String(a.name || a.type || a.title || "").localeCompare(String(b.name || b.type || b.title || ""));
      return (av - bv) * direction;
    });
  }

  function renderTable() {
    const head = $("#data-head"), body = $("#data-body");
    if (!head || !body) return;
    const columns = tableColumns();
    const rows = tableFilteredRows();
    state.table.rows = rows;
    state.table.total = rows.length;

    const pageSize = clamp(toNumber(state.table.pageSize, 50), 10, 500);
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    state.table.page = clamp(state.table.page, 0, pages - 1);
    const start = state.table.page * pageSize;
    const pageRows = rows.slice(start, start + pageSize);

    // head
    head.innerHTML = "";
    const tr = el("tr");
    columns.forEach(function (column) {
      const sorted = (state.table.sort || "").indexOf(column.key) === 0;
      tr.appendChild(el("th", {
        class: sorted ? "is-sorted" : "", "data-sort": column.key,
        title: "Sort by " + column.label,
        html: escapeHtml(column.label) + (sorted ? '<span class="sort-arrow">' + (state.table.sort.endsWith("asc") ? "▲" : "▼") + "</span>" : ""),
        onclick: function () {
          const ascending = sorted && state.table.sort.endsWith("desc");
          state.table.sort = column.key + (ascending ? "-asc" : "-desc");
          const select = $("#table-sort");
          if (select) select.value = state.table.sort;
          renderTable();
        },
      }));
    });
    head.appendChild(tr);

    // body
    body.innerHTML = "";
    pageRows.forEach(function (row) {
      const tr2 = el("tr", {
        "data-key": row.key || row.id || row.doc_id,
        class: state.selection.indexOf(row.key || ("rel:" + row.id)) >= 0 ? "is-selected" : "",
        onclick: function (event) { tableRowClick(row, event); },
        ondblclick: function () { tableRowActivate(row); },
      });
      columns.forEach(function (column) {
        const html = column.render ? column.render(row) : escapeHtml(column.value(row));
        tr2.appendChild(el("td", { class: column.cls || "", html: html, title: column.render ? escapeHtml(String(column.value(row) === undefined ? "" : column.value(row))) : null }));
      });
      body.appendChild(tr2);
    });

    $("#table-empty").hidden = rows.length > 0;
    $("#table-range").textContent = rows.length
      ? (start + 1) + "–" + Math.min(start + pageSize, rows.length) + " of " + rows.length + " rows" + (state.stats.truncated ? " (graph truncated by budget)" : "")
      : "0 rows";
    $("#page-label").textContent = (state.table.page + 1) + " / " + pages;
    $("#page-first").disabled = $("#page-prev").disabled = state.table.page === 0;
    $("#page-next").disabled = $("#page-last").disabled = state.table.page >= pages - 1;

    // the type dropdown reflects what is actually loaded
    const select = $("#table-type-filter");
    if (select) {
      const current = select.value;
      const options = state.table.subject === "edges"
        ? Array.from(new Set(Array.from(state.edges.values()).map(function (e) { return e.type; }))).sort()
        : state.table.subject === "sources"
          ? Array.from(new Set(Array.from(state.docs.values()).map(function (d) { return d.source_id; }).filter(Boolean))).sort()
          : Array.from(new Set(Array.from(state.nodes.values()).map(function (n) { return n.entity_type; }))).sort();
      select.innerHTML = "";
      select.appendChild(el("option", { value: "", text: "All " + (state.table.subject === "edges" ? "predicates" : state.table.subject === "sources" ? "sources" : "types") }));
      options.forEach(function (option) { select.appendChild(el("option", { value: option, text: option })); });
      select.value = options.indexOf(current) >= 0 ? current : "";
      state.table.typeFilter = select.value;
    }
  }

  function tableRowClick(row, event) {
    if (state.table.subject === "edges") {
      selectEdgeInGraph(row.id);
      return;
    }
    if (state.table.subject === "sources") {
      const related = entitiesForDoc(row.doc_id).map(function (n) { return n.key; });
      if (related.length) {
        switchView("graph");
        selectElements(related, false);
        markSearchHits(related);
        toast("Citation", "<b>" + escapeHtml(row.title) + "</b> · " + related.length + " loaded entities reference it.", "info");
      } else {
        toast("Citation not loaded", "No entity in the current scope references <code>" + escapeHtml(row.doc_id) + "</code>.", "warn");
      }
      return;
    }
    selectElements([row.key], Boolean(event && event.shiftKey));
    setActive(row.key);
  }

  function tableRowActivate(row) {
    if (state.table.subject === "nodes") {
      switchView("graph");
      setActive(row.key, { center: true });
      expandNode(row.key, state.depth);
    } else if (state.table.subject === "edges") {
      switchView("graph");
      selectEdgeInGraph(row.id);
      centerOn(row.source, 1.2);
    }
  }

  function exportTableCsv() {
    const columns = tableColumns();
    const rows = state.table.rows;
    if (!rows.length) { toast("Nothing to export", "No rows match the current filters.", "warn"); return; }
    const csv = buildCsv(rows, columns);
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    downloadText("puppetnet-" + state.table.subject + "-" + stamp + ".csv", csv, "text/csv");
    toast("CSV exported", rows.length + " " + state.table.subject + " rows · " + columns.length + " columns.", "success");
  }

  /* ===========================================================================
     15. Pathfinding / handshake engine
     ======================================================================== */

  function setPathEndpoint(which, node) {
    if (!node) return;
    const input = $(which === "from" ? "#path-a" : "#path-b");
    const swatch = $(which === "from" ? ".path-swatch-a" : ".path-swatch-b");
    input.value = node.name;
    input.dataset.key = node.key;
    if (swatch) swatch.classList.add("is-set");
    state.pathSelection[which] = node.key;
    renderPathActivePanel();
    saveHash();
  }

  /** Resolve whatever the analyst typed into a canonical key. */
  /**
   * Shape of a canonical key (`TYPE:slug-8hex`, as minted by the Python resolver
   * and by demoKey). Recognising it matters: a key is an identity, not a search
   * string.
   */
  const CANONICAL_KEY = /^[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9._-]+-[0-9a-fA-F]{8}$/;

  async function resolveEndpoint(input) {
    const typed = String(input.value || "").trim();
    if (!typed) return null;
    if (input.dataset.key && (nameOf(input.dataset.key) === typed || input.dataset.key === typed)) return input.dataset.key;

    // 1. exact match against what is loaded
    let hit = null;
    state.nodes.forEach(function (node) {
      if (hit) return;
      if (node.key === typed || node.name === typed) hit = node.key;
    });
    if (hit) { input.dataset.key = hit; return hit; }

    // 2. A canonical key must resolve exactly or not at all. Fuzzy-matching one
    //    picks up shared tokens — the aircraft tail "9h-kast" and the name
    //    "Kastelion" both contain "kast" — and an endpoint that silently resolves
    //    to a different entity turns a handshake into a false claim about two
    //    unrelated companies. "Could not resolve" is the honest answer.
    if (CANONICAL_KEY.test(typed)) {
      const detail = await runQuery("resolve", function () {
        return state.provider.node(typed);
      }, { label: "resolving entity", reportAbort: false });
      const node = detail && detail.ok !== false ? normalizeNode(detail.node) : null;
      if (node && node.key) {
        input.dataset.key = node.key;
        input.value = node.name || typed;
        return node.key;
      }
      return null;
    }

    // 3. best local match
    const local = localSearch(typed, 1);
    if (local.length && local[0].score >= 46) { input.dataset.key = local[0].node.key; return local[0].node.key; }

    // 4. ask the API
    const payload = await runQuery("resolve", function () { return state.provider.search(typed, { limit: 1 }); }, { label: "resolving entity" });
    if (payload && (payload.nodes || []).length) {
      const node = normalizeNode(payload.nodes[0]);
      input.dataset.key = node.key;
      input.value = node.name;
      return node.key;
    }
    return null;
  }

  async function findPath() {
    const inputA = $("#path-a"), inputB = $("#path-b");
    const host = $("#path-result");
    host.innerHTML = el("p", { class: "dim text-xs" }, ["Resolving endpoints…"]);

    const from = await resolveEndpoint(inputA);
    const to = await resolveEndpoint(inputB);
    if (!from || !to) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "path-none" }, [
        "Could not resolve " + (!from ? "<b>Entity A</b>" : "") + (!from && !to ? " or " : "") + (!to ? "<b>Entity B</b>" : "") +
        ". Type a name from the graph, or search for it first.",
      ]));
      return;
    }
    if (from === to) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "path-none" }, ["A and B are the same entity."]));
      return;
    }

    const maxHops = clamp(toNumber($("#path-hops").value, 6), 1, 12);
    const direction = $("#path-direction").value;
    const costName = $("#path-weight").value;

    let result = await runQuery("path", function () {
      return state.provider.path(from, to, { maxHops: maxHops, direction: direction, cost: costName });
    }, { label: "finding chain", fallback: true });

    let local = false;
    // A live provider may answer with node keys only; hydrate from what is loaded.
    if (result && result.found && (!result.nodes || !result.nodes.length) && result.nodeKeys) {
      const payload = await runQuery("path-hydrate", function () {
        return state.provider.neighbors(from, { depth: Math.min(maxHops, 4), limit: 400 });
      }, { label: "loading chain", fallback: true, reportAbort: false });
      if (payload) mergePayload(payload, { mode: "merge", layout: false });
      result.nodes = result.nodeKeys.map(function (key) { return state.nodes.get(key) || { key: key, name: key }; });
      result.edges = (result.edgeDescriptors || []).map(function (d, index) {
        const normalised = normalizeEdge(Object.assign({ id: d.id }, d));
        if (!normalised) return null;
        normalised.source = result.nodeKeys[index];
        normalised.target = result.nodeKeys[index + 1];
        return normalised;
      }).filter(Boolean);
    }

    if (!result || !result.found) {
      // Offline / failed: try the loaded subgraph so the panel is still useful.
      const nodes = Array.from(state.nodes.values());
      const edges = Array.from(state.edges.values());
      const localResult = localShortestPath(nodes, edges, from, to, maxHops, costName, direction);
      if (localResult.found) {
        localResult.alternatives = alternativePaths(nodes, edges, localResult, from, to, maxHops, costName, direction, 3);
        result = localResult;
        local = true;
      } else if (result && result.found === false) {
        renderPathNotFound(from, to, maxHops, result.reason);
        return;
      } else {
        renderPathNotFound(from, to, maxHops, localResult.reason);
        return;
      }
    }

    if (result.local) local = true;
    if (!result.alternatives || !result.alternatives.length) {
      result.alternatives = alternativePaths(Array.from(state.nodes.values()), Array.from(state.edges.values()), result, from, to, maxHops, costName, direction, 3);
    }

    state.path = Object.assign({}, result, { from: from, to: to, local: local });
    renderPathResult(state.path);
    renderPathAlternatives(state.path.alternatives || []);

    switchView("graph");
    highlightPath(state.path);
    pushHistory("path", { from: from, to: to, hops: result.hops });
    saveHash();
    toast("Chain found", result.hops + " hop" + (result.hops === 1 ? "" : "s") + " · cost " + formatScore(result.cost, 3) +
      (local ? " · <b>computed locally</b> from the loaded subgraph" : ""), local ? "info" : "success");
  }

  function renderPathNotFound(from, to, maxHops, reason) {
    const host = $("#path-result");
    host.innerHTML = "";
    host.appendChild(el("div", { class: "path-none" }, [
      el("div", { class: "mb-1.5" }, ["No connection within " + maxHops + " hops."]),
      el("div", { class: "dim text-[11px]" }, [
        reason === "unknown-endpoint"
          ? "One endpoint is not in the loaded subgraph — increase the node budget or expand a neighbour first."
          : "Searched " + (state.nodes.size) + " entities. Try more hops, undirected traversal, or expand both endpoints first.",
      ]),
      el("div", { class: "dim text-[11px] mt-1.5" }, [
        el("code", { text: nameOf(from) }), " ⇢ ", el("code", { text: nameOf(to) }),
      ]),
    ]));
    $("#path-alts").innerHTML = "";
    state.path = null;
    clearPathHighlight();
  }

  function renderPathResult(result) {
    const host = $("#path-result");
    host.innerHTML = "";

    host.appendChild(el("div", { class: "path-summary" }, [
      el("span", { class: "path-metric" }, [el("i", { text: String(result.hops) }), el("em", { text: "hops" })]),
      el("span", { class: "path-metric" }, [el("i", { text: formatScore(result.cost, 3) }), el("em", { text: "cost · " + (result.costFunction || "hops") })]),
      el("span", { class: "path-metric" }, [el("i", { text: formatScore(result.meanConfidence, 2) }), el("em", { text: "mean confidence" })]),
      el("span", { class: "path-metric" }, [el("i", { text: formatScore(result.minWeight, 2) }), el("em", { text: "weakest tie" })]),
      result.local ? tagFor("local computation", "tag-amber").outerHTML : "",
      el("span", { class: "ml-auto" }, [
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: function () { exportPathCsv(result); } }, ["⤓ CSV"]),
      ]),
    ]));

    const chain = el("div", { class: "chain" });
    (result.nodes || []).forEach(function (node, index) {
      const record = state.nodes.get(node.key) || node;
      const isEndpoint = index === 0 || index === (result.nodes.length - 1);
      const edge = (result.edges || [])[index - 1];

      if (edge) {
        chain.appendChild(el("div", { class: "chain-edge" + (edge.calculated ? " is-calculated" : "") }, [
          el("b", { text: edge.type }),
          el("span", { class: "edge-cost", text: "w " + formatScore(edge.weight, 2) + " · conf " + formatScore(edge.confidence, 2) + " · " + (edge.method || "—") }),
          edge.source_id ? el("span", { class: "edge-cost", text: edge.source_id }) : null,
          edge.hedged ? el("span", { class: "warn", text: "hedged" }) : null,
        ].filter(Boolean)));
      }

      chain.appendChild(el("div", { class: "chain-hop" + (isEndpoint ? " is-endpoint" : "") }, [
        el("div", { class: "chain-rail" }, [
          el("span", { class: "chain-node-dot" }),
          index < result.nodes.length - 1 ? el("span", { class: "chain-line" }) : null,
        ]),
        el("div", { class: "chain-body" }, [
          el("div", {
            class: "chain-name", title: record.key,
            onclick: function () { switchView("graph"); setActive(record.key, { center: true }); selectElements([record.key], false); },
            text: record.name,
          }),
          el("div", { class: "chain-meta" }, [
            tagFor(record.entity_type || "Unknown", "tag-cyan"),
            record.jurisdiction ? tagFor(record.jurisdiction, "") : null,
            record.cluster_id ? tagFor(record.cluster_id, "") : null,
            record.anomaly_score !== null && record.anomaly_score !== undefined ? tagFor("anomaly " + formatScore(record.anomaly_score, 2), "tag-purple") : null,
          ].filter(Boolean)),
        ]),
      ]));
    });
    host.appendChild(chain);
  }

  function renderPathAlternatives(alternatives) {
    const host = $("#path-alts");
    host.innerHTML = "";
    if (!alternatives || !alternatives.length) return;
    host.appendChild(el("div", { class: "path-alt-title" }, ["Alternative chains (" + alternatives.length + ")"]));
    alternatives.forEach(function (alt) {
      const names = (alt.nodes || []).map(function (n) { return shortLabel(n.name || n.key, 20); }).join(" → ");
      host.appendChild(el("div", {
        class: "path-alt", role: "button", tabindex: "0",
        title: names,
        onclick: function () {
          state.path = alt;
          renderPathResult(alt);
          switchView("graph");
          highlightPath(alt);
        },
        onkeydown: function (event) { if (event.key === "Enter") { state.path = alt; renderPathResult(alt); switchView("graph"); highlightPath(alt); } },
      }, [
        el("span", { class: "tag tag-cyan", text: alt.hops + " hops" }),
        el("span", { class: "path-alt-nodes", text: names }),
        el("span", { class: "path-alt-cost", text: formatScore(alt.cost, 3) }),
      ]));
    });
  }

  function exportPathCsv(result) {
    const rows = [];
    (result.nodes || []).forEach(function (node, index) {
      const edge = (result.edges || [])[index - 1];
      rows.push({
        hop: index,
        entity: node.name,
        key: node.key,
        type: node.entity_type || "",
        predicate: edge ? edge.type : "",
        weight: edge ? formatScore(edge.weight, 3) : "",
        confidence: edge ? formatScore(edge.confidence, 3) : "",
        method: edge ? edge.method || "" : "",
        source: edge ? edge.source_id || "" : "",
        document: edge ? edge.doc_id || "" : "",
      });
    });
    const columns = ["hop", "entity", "key", "type", "predicate", "weight", "confidence", "method", "source", "document"]
      .map(function (key) { return { key: key, label: key }; });
    downloadText("puppetnet-path-" + Date.now() + ".csv", buildCsv(rows, columns), "text/csv");
    toast("Path exported", rows.length + " hops written to CSV.", "success");
  }

  function renderPathActivePanel() {
    const host = $("#path-active");
    if (!host) return;
    const node = state.activeKey ? state.nodes.get(state.activeKey) : null;
    host.innerHTML = "";
    const rows = [
      ["Active node", node ? node.name : "none", ""],
      ["Canonical key", node ? node.key : "—", "mono"],
      ["Degree", node ? String(node.degree) : "—", ""],
      ["Cluster", node ? (node.cluster_id || "—") : "—", ""],
    ];
    rows.forEach(function (row) {
      host.appendChild(el("div", { class: "kv-row" }, [
        el("span", { text: row[0] }),
        el("b", { class: row[2] ? row[2] + (node ? "" : " muted") : (node ? "" : "muted"), text: row[1] }),
      ]));
    });

    // Keep the datalist of entity names useful for the A/B inputs.
    const datalist = $("#entity-options");
    if (datalist) {
      datalist.innerHTML = "";
      Array.from(state.nodes.values()).slice(0, 400).forEach(function (n) {
        datalist.appendChild(el("option", { value: n.name, label: n.entity_type }));
      });
    }
    ["#btn-path-from-active", "#btn-path-to-active", "#btn-neighbour-run"].forEach(function (selector) {
      const button = $(selector);
      if (button) button.disabled = !node;
    });
  }

  /** Cypher that reproduces the current view — for pasting into Bloom or a notebook. */
  function cypherForCurrentView() {
    if (state.path && state.path.found) {
      const from = state.path.from, to = state.path.to;
      const hops = clamp(toNumber($("#path-hops").value, 6), 1, 12);
      const direction = $("#path-direction").value;
      const left = direction === "incoming" ? "<-" : "-";
      const right = direction === "outgoing" ? "->" : "-";
      return "// Handshake: " + nameOf(from) + " → " + nameOf(to) + "\n" +
        "MATCH (a:Entity {canonical_key: '" + escapeCypher(from) + "'}), (b:Entity {canonical_key: '" + escapeCypher(to) + "'})\n" +
        "MATCH p = shortestPath((a)" + left + "[*1.." + hops + "]" + right + "(b))\n" +
        "UNWIND relationships(p) AS r\n" +
        "RETURN [n IN nodes(p) | n.name] AS chain,\n" +
        "       type(r) AS predicate, r.weight AS weight, r.confidence AS confidence,\n" +
        "       r.method AS method, r.source_id AS source, r.doc_id AS document\n" +
        "ORDER BY weight DESC;\n";
    }
    if (state.activeKey) {
      const depth = clamp(toNumber(state.depth, 1), 1, 4);
      return "// Neighbourhood: " + nameOf(state.activeKey) + " (" + depth + " hops)\n" +
        "MATCH (root:Entity {canonical_key: '" + escapeCypher(state.activeKey) + "'})\n" +
        "MATCH (root)-[rels*1.." + depth + "]-(other:Entity)\n" +
        "WITH root, other, rels LIMIT " + clamp(toNumber(config.nodeLimit, 250), 1, 1200) + "\n" +
        "UNWIND rels AS r\n" +
        "RETURN DISTINCT startNode(r).name AS subject, type(r) AS predicate, endNode(r).name AS object,\n" +
        "       r.weight AS weight, r.confidence AS confidence, r.method AS method, r.source_id AS source;\n";
    }
    const metric = SORTABLE_METRICS.indexOf(state.render.sizeMetric) >= 0 ? state.render.sizeMetric : "anomaly_score";
    return "// Overview: top " + clamp(toNumber(config.nodeLimit, 250), 1, 1200) + " entities by " + metric + "\n" +
      "MATCH (e:Entity)\n" +
      "OPTIONAL MATCH (e)-[r]-(o:Entity)\n" +
      "WITH e, count(r) AS degree\n" +
      "RETURN e.name AS name, labels(e) AS labels, e.canonical_key AS key, degree,\n" +
      "       e.betweenness AS betweenness, e.anomaly_score AS anomaly_score, e.cluster_id AS cluster_id\n" +
      "ORDER BY coalesce(e." + metric + ", 0) DESC\nLIMIT " + clamp(toNumber(config.nodeLimit, 250), 1, 1200) + ";\n";
  }

  /* ===========================================================================
     16. Filter rail
     ======================================================================== */

  function syncTypeFilters() {
    const host = $("#type-filters");
    if (!host) return;
    const counts = new Map();
    state.nodes.forEach(function (node) {
      const labels = node.labels || [];
      const specific = ["ShellCompany", "Offshore", "Foundation", "Aircraft", "Vessel", "Person", "Organization", "Location", "Company", "Craft"];
      let chosen = node.entity_type;
      for (let i = 0; i < specific.length; i++) {
        if (labels.indexOf(specific[i]) >= 0 && ENTITY_TYPES[specific[i]]) { chosen = specific[i]; break; }
      }
      counts.set(chosen, (counts.get(chosen) || 0) + 1);
    });

    const previous = new Set(state.filters.types);
    host.innerHTML = "";
    if (!counts.size) {
      host.appendChild(el("span", { class: "dim text-[11px]" }, ["No entities loaded."]));
      return;
    }
    Array.from(counts.entries())
      .sort(function (a, b) { return ((ENTITY_TYPES[a[0]] || {}).order || 50) - ((ENTITY_TYPES[b[0]] || {}).order || 50); })
      .forEach(function (entry) {
        const type = entry[0];
        const meta = ENTITY_TYPES[type] || { color: FALLBACK_COLOR, label: type };
        const on = !previous.size || previous.has(type);
        const chip = el("button", {
          class: "chip" + (on ? " is-on" : ""), type: "button", "aria-pressed": on ? "true" : "false",
          style: "--chip-color:" + meta.color + ";--chip-bg:" + hexToRgba(meta.color, 0.13) + ";color:" + (on ? meta.color : ""),
          title: "Toggle " + meta.label,
          onclick: function () {
            const active = state.filters.types;
            // First click switches from "everything" to "only this".
            if (!active.size) {
              Array.from(counts.keys()).forEach(function (t) { if (t !== type) active.add(t); });
              active.delete(type);
            } else if (active.has(type)) {
              active.delete(type);
              if (active.size === counts.size - 1) active.clear();   // back to "all"
            } else {
              active.add(type);
              if (active.size === counts.size) active.clear();
            }
            syncTypeFilters();
            applyFilters();
            renderTable();
            saveFilters();
          },
        }, [
          el("span", { class: "chip-dot", style: "color:" + meta.color }),
          el("span", { text: meta.label }),
          el("span", { class: "chip-count", text: String(entry[1]) }),
        ]);
        host.appendChild(chip);
        if (on && !previous.size) state.filters.types.delete(type);
      });
    if (previous.size) state.filters.types = new Set(Array.from(previous).filter(function (t) { return counts.has(t); }));
  }

  function hexToRgba(hex, alpha) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return "rgba(100,116,139," + alpha + ")";
    const int = parseInt(m[1], 16);
    return "rgba(" + ((int >> 16) & 255) + "," + ((int >> 8) & 255) + "," + (int & 255) + "," + alpha + ")";
  }

  function syncRelFilters() {
    const host = $("#rel-filters");
    if (!host) return;
    const counts = new Map();
    state.edges.forEach(function (edge) { counts.set(edge.type, (counts.get(edge.type) || 0) + 1); });
    const previous = new Set(state.filters.relTypes);
    host.innerHTML = "";

    if (!counts.size) {
      host.appendChild(el("span", { class: "dim text-[11px] p-1" }, ["No relationships loaded."]));
      return;
    }

    const entries = Array.from(counts.entries()).sort(function (a, b) { return b[1] - a[1]; });
    entries.forEach(function (entry) {
      const type = entry[0];
      const on = !previous.size || previous.has(type);
      const row = el("label", { class: "rel-item" + (on ? "" : " is-off") }, [
        el("input", {
          type: "checkbox", checked: on,
          onchange: function (event) {
            const active = state.filters.relTypes;
            if (!active.size) {
              entries.forEach(function (other) { if (other[0] !== type) active.add(other[0]); });
              active.delete(type);
            } else if (event.target.checked) {
              active.add(type);
              if (active.size === entries.length) active.clear();
            } else {
              active.delete(type);
            }
            syncRelFilters();
            applyFilters();
            renderTable();
            saveFilters();
          },
        }),
        el("span", { class: "rel-name", title: type + " · " + (REL_GROUPS[REL_TYPES[type] || "weak"] || {}).label, text: type }),
        el("span", { class: "rel-count", text: String(entry[1]) }),
      ]);
      host.appendChild(row);
    });
    if (previous.size) state.filters.relTypes = new Set(Array.from(previous).filter(function (t) { return counts.has(t); }));
  }

  function saveFilters() {
    if (!config.persist) return;
    try {
      localStorage.setItem(STORAGE_KEY + ".filters", JSON.stringify({
        types: Array.from(state.filters.types),
        relTypes: Array.from(state.filters.relTypes),
        minWeight: state.filters.minWeight,
        minConfidence: state.filters.minConfidence,
        hideCalculated: state.filters.hideCalculated,
        hideWeak: state.filters.hideWeak,
        render: state.render,
        depth: state.depth,
      }));
    } catch (_) { /* private mode */ }
  }

  function restoreFilters() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY + ".filters");
      if (!raw) return;
      const saved = JSON.parse(raw) || {};
      if (Array.isArray(saved.types)) state.filters.types = new Set(saved.types);
      if (Array.isArray(saved.relTypes)) state.filters.relTypes = new Set(saved.relTypes);
      if (saved.minWeight !== undefined) state.filters.minWeight = toNumber(saved.minWeight, 0);
      if (saved.minConfidence !== undefined) state.filters.minConfidence = toNumber(saved.minConfidence, 0);
      if (saved.hideCalculated !== undefined) state.filters.hideCalculated = Boolean(saved.hideCalculated);
      if (saved.hideWeak !== undefined) state.filters.hideWeak = Boolean(saved.hideWeak);
      if (saved.render) Object.assign(state.render, saved.render);
      if (saved.depth) state.depth = clamp(toNumber(saved.depth, 1), 1, 4);
    } catch (_) { /* ignore */ }
  }

  /* ===========================================================================
     17. View switching, drawers, modals
     ======================================================================== */

  function switchView(name) {
    const view = ["graph", "table", "path"].indexOf(name) >= 0 ? name : "graph";
    state.view = view;
    ["graph", "table", "path"].forEach(function (candidate) {
      const panel = $("#panel-" + candidate);
      const tab = $("#tab-" + candidate);
      const on = candidate === view;
      if (panel) { panel.classList.toggle("is-active", on); panel.hidden = !on; }
      if (tab) { tab.classList.toggle("is-active", on); tab.setAttribute("aria-selected", on ? "true" : "false"); }
    });
    moveTabInk();
    if (view === "table") renderTable();
    if (view === "graph" && cy) { cy.resize(); if (cy.nodes().length) fitView(56); }
    if (view === "path") { renderPathActivePanel(); setTimeout(function () { const a = $("#path-a"); if (a && !a.value) a.focus(); }, 60); }
    saveHash();
  }

  function moveTabInk() {
    const ink = $(".tab-ink");
    const active = $(".view-tab.is-active");
    if (!ink || !active) return;
    ink.style.width = active.offsetWidth + "px";
    ink.style.transform = "translateX(" + (active.offsetLeft - 3) + "px)";
  }

  function toggleDrawer(which, force) {
    const panel = $(which === "rail" ? "#rail" : "#inspector");
    const scrim = $(which === "rail" ? "#rail-scrim" : "#inspector-scrim");
    const button = $(which === "rail" ? "#btn-rail" : "#btn-inspector");
    const open = force === undefined ? !panel.classList.contains("is-open") : force;
    panel.classList.toggle("is-open", open);
    scrim.hidden = !open;
    if (button) button.classList.toggle("is-active", open);
    if (which === "rail" && open && cy) setTimeout(function () { cy.resize(); }, 280);
  }

  function openModal(id) {
    const modal = $(id);
    if (!modal) return;
    modal.hidden = false;
    state.modalOpen = id;
    const focusable = modal.querySelector("select, input, button");
    if (focusable) setTimeout(function () { focusable.focus(); }, 40);
    if (id === "#modal-settings") syncSettingsForm();
  }

  function closeModal() {
    if (!state.modalOpen) return;
    const modal = $(state.modalOpen);
    if (modal) modal.hidden = true;
    state.modalOpen = null;
  }

  function bindModals() {
    document.addEventListener("click", function (event) {
      const closer = event.target.closest("[data-close]");
      if (closer) { closeModal(); return; }
    });
    $("#btn-settings").addEventListener("click", function () { openModal("#modal-settings"); });
    $("#btn-help").addEventListener("click", function () { openModal("#modal-help"); });
    $("#btn-empty-settings").addEventListener("click", function () { openModal("#modal-settings"); });
  }

  /* ---- context menu ------------------------------------------------------ */

  function showContextMenu(event, key) {
    const menu = $("#ctx-menu");
    const node = state.nodes.get(key);
    if (!menu || !node) return;
    event.preventDefault && event.preventDefault();
    const original = event.originalEvent || event;
    original.preventDefault && original.preventDefault();

    menu.innerHTML = "";
    menu.appendChild(el("div", { class: "ctx-head", text: shortLabel(node.name, 28) }));
    const items = [
      ["Expand " + state.depth + "-hop", "E", function () { expandNode(key, state.depth); }, false],
      ["Isolate neighbourhood", "I", function () { isolateNode(key, state.depth); }, false],
      ["Centre on node", "C", function () { centerOn(key, 1.4); }, false],
      ["Inspect", "", function () { selectElements([key], false); setActive(key); openDrawer("inspector", true); }, false],
      ["Set as Entity A", "", function () { setPathEndpoint("from", node); }, false],
      ["Set as Entity B", "", function () { setPathEndpoint("to", node); }, false],
      ["Handshake A ⇢ B", "P", function () {
        if (state.pathSelection.from && state.pathSelection.from !== key) { setPathEndpoint("to", node); switchView("path"); findPath(); }
        else { setPathEndpoint("from", node); switchView("path"); toast("Entity A set", "Now pick Entity B.", "info"); }
      }, false],
      ["Copy canonical key", "", async function () {
        const ok = await copyText(key);
        toast(ok ? "Copied" : "Copy failed", "<code>" + escapeHtml(key) + "</code>", ok ? "success" : "warn", 2000);
      }, false],
      ["Hide node", "", function () { hideNode(key); }, true],
    ];
    items.forEach(function (item, index) {
      if (index === 4 || index === 7) menu.appendChild(el("div", { class: "ctx-sep" }));
      menu.appendChild(el("button", {
        class: "ctx-item" + (item[3] ? " is-danger" : ""), type: "button", role: "menuitem",
        onclick: function () { hideContextMenu(); item[2](); },
      }, [el("span", { text: item[0] }), item[1] ? el("kbd", { text: item[1] }) : null]));
    });

    menu.hidden = false;
    const x = clamp(original.clientX || 0, 8, window.innerWidth - menu.offsetWidth - 8);
    const y = clamp(original.clientY || 0, 8, window.innerHeight - menu.offsetHeight - 8);
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    setActive(key);
  }

  function hideContextMenu() {
    const menu = $("#ctx-menu");
    if (menu) menu.hidden = true;
  }

  function openDrawer(which, force) { toggleDrawer(which, force); }

  /* ===========================================================================
     18. Control wiring
     ======================================================================== */

  function applyConfigToUi() {
    // depth segmented control
    $$("#depth .seg").forEach(function (button) {
      const on = toNumber(button.dataset.depth, 1) === clamp(toNumber(state.depth, 1), 1, 4);
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });

    setInput("#node-limit", config.nodeLimit, "#node-limit-out", function (v) { return String(v); });
    setInput("#min-weight", state.filters.minWeight, "#min-weight-out", function (v) { return formatScore(v, 2); });
    setInput("#min-confidence", state.filters.minConfidence, "#min-confidence-out", function (v) { return formatScore(v, 2); });
    setInput("#link-distance", state.render.repulsion, "#link-distance-out", function (v) { return formatScore(v, 1) + "×"; });
    setInput("#path-hops", config.pathHops, "#path-hops-out", function (v) { return String(v); });
    setInput("#neighbour-depth", state.depth, "#neighbour-depth-out", function (v) { return v + " hop" + (v === 1 ? "" : "s"); });

    setChecked("#hide-calculated", state.filters.hideCalculated);
    setChecked("#hide-weak", state.filters.hideWeak);
    setChecked("#show-labels", state.render.showLabels);
    setChecked("#colour-clusters", state.render.colourClusters);
    setChecked("#glow", state.render.glow);
    setChecked("#animate-layout", state.render.animate);

    setValue("#size-metric", state.render.sizeMetric);
    setValue("#layout", state.render.layout);
    setValue("#edge-style", state.render.edgeStyle);
    setValue("#path-direction", config.pathDirection);
    setValue("#path-weight", config.pathCost);
    setValue("#table-sort", state.table.sort);
    setValue("#page-size", String(state.table.pageSize));

    document.body.classList.toggle("reduce-motion", Boolean(config.reduceMotion) || prefersReducedMotion());
    $("#cfg-version").textContent = VERSION;
  }

  function prefersReducedMotion() {
    return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function setInput(selector, value, outputSelector, format) {
    const input = $(selector);
    if (!input) return;
    input.value = value;
    syncRange(input);
    if (outputSelector) {
      const out = $(outputSelector);
      if (out) out.textContent = format ? format(toNumber(value, 0)) : String(value);
    }
  }

  function setChecked(selector, value) { const input = $(selector); if (input) input.checked = Boolean(value); }
  function setValue(selector, value) { const input = $(selector); if (input) input.value = value; }

  function bindControls() {
    /* ---- view tabs ---- */
    $$(".view-tab").forEach(function (tab) {
      tab.addEventListener("click", function () { switchView(tab.dataset.view); });
    });

    /* ---- depth ---- */
    $$("#depth .seg").forEach(function (button) {
      button.addEventListener("click", function () {
        state.depth = clamp(toNumber(button.dataset.depth, 1), 1, 4);
        applyConfigToUi();
        saveFilters();
        saveHash();
        if (state.activeKey) expandNode(state.activeKey, state.depth);
      });
    });

    /* ---- focus actions ---- */
    $("#btn-expand").addEventListener("click", function () { expandNode(state.activeKey, state.depth); });
    $("#btn-isolate").addEventListener("click", function () { isolateNode(state.activeKey, state.depth); });
    $("#btn-reset").addEventListener("click", function () { resetView(); });
    $("#btn-refresh").addEventListener("click", function () { refresh(); });

    /* ---- budgets & thresholds ---- */
    $("#node-limit").addEventListener("input", function (event) {
      config.nodeLimit = clamp(toNumber(event.target.value, 250), 25, 1200);
      syncRange(event.target);
      $("#node-limit-out").textContent = String(config.nodeLimit);
    });
    $("#node-limit").addEventListener("change", function () { saveConfig(); });

    $("#min-weight").addEventListener("input", function (event) {
      state.filters.minWeight = toNumber(event.target.value, 0);
      syncRange(event.target);
      $("#min-weight-out").textContent = formatScore(state.filters.minWeight, 2);
      applyFilters(); renderTable();
    });
    $("#min-weight").addEventListener("change", saveFilters);

    $("#min-confidence").addEventListener("input", function (event) {
      state.filters.minConfidence = toNumber(event.target.value, 0);
      syncRange(event.target);
      $("#min-confidence-out").textContent = formatScore(state.filters.minConfidence, 2);
      applyFilters(); renderTable();
    });
    $("#min-confidence").addEventListener("change", saveFilters);

    $("#hide-calculated").addEventListener("change", function (event) {
      state.filters.hideCalculated = event.target.checked; applyFilters(); renderTable(); saveFilters();
    });
    $("#hide-weak").addEventListener("change", function (event) {
      state.filters.hideWeak = event.target.checked; applyFilters(); renderTable(); saveFilters();
    });

    $("#types-all").addEventListener("click", function () { state.filters.types.clear(); syncTypeFilters(); applyFilters(); renderTable(); saveFilters(); });
    $("#rels-all").addEventListener("click", function () { state.filters.relTypes.clear(); syncRelFilters(); applyFilters(); renderTable(); saveFilters(); });

    /* ---- rendering ---- */
    $("#size-metric").addEventListener("change", function (event) {
      state.render.sizeMetric = event.target.value;
      applyGraph({ layout: false, relayout: false });
      saveFilters(); saveHash();
      renderLegend();
    });
    $("#layout").addEventListener("change", function (event) {
      state.render.layout = event.target.value;
      state.layoutSeeded = false;
      runLayout(state.render.layout, { fit: true });
      saveFilters();
    });
    $("#edge-style").addEventListener("change", function (event) {
      state.render.edgeStyle = event.target.value;
      applyGraph({ layout: false, relayout: false });
      saveFilters();
    });
    $("#show-labels").addEventListener("change", function (event) {
      state.render.showLabels = event.target.checked;
      if (cy) cy.nodes().toggleClass("no-label", !event.target.checked);
      saveFilters();
    });
    $("#colour-clusters").addEventListener("change", function (event) {
      state.render.colourClusters = event.target.checked;
      applyGraph({ layout: false, relayout: false });
      renderLegend(); saveFilters();
    });
    $("#glow").addEventListener("change", function (event) {
      state.render.glow = event.target.checked;
      applyGraph({ layout: false, relayout: false });
      saveFilters();
    });
    $("#animate-layout").addEventListener("change", function (event) {
      state.render.animate = event.target.checked; saveFilters();
    });
    $("#link-distance").addEventListener("input", function (event) {
      state.render.repulsion = toNumber(event.target.value, 1);
      syncRange(event.target);
      $("#link-distance-out").textContent = formatScore(state.render.repulsion, 1) + "×";
    });
    $("#link-distance").addEventListener("change", function () {
      state.layoutSeeded = false;
      runLayout(state.render.layout, { fit: true });
      saveFilters();
    });
    $("#btn-relayout").addEventListener("click", function () { state.layoutSeeded = false; runLayout(state.render.layout, { fit: true }); });
    $("#btn-export-png").addEventListener("click", exportPng);

    /* ---- canvas HUD ---- */
    $("#btn-zoom-in").addEventListener("click", function () { zoomBy(1.32); });
    $("#btn-zoom-out").addEventListener("click", function () { zoomBy(1 / 1.32); });
    $("#btn-fit").addEventListener("click", function () { fitView(56); });
    $("#btn-center").addEventListener("click", function () { if (state.activeKey) centerOn(state.activeKey, 1.3); else fitView(56); });
    $("#btn-fullscreen").addEventListener("click", toggleFullscreen);
    $("#btn-empty-overview").addEventListener("click", function () { loadOverview(); });
    $("#btn-empty-demo").addEventListener("click", async function () {
      config.mode = "demo"; saveConfig(); await switchProvider(true);
    });

    /* ---- drawers ---- */
    $("#btn-rail").addEventListener("click", function () { toggleDrawer("rail"); });
    $("#btn-inspector").addEventListener("click", function () { toggleDrawer("inspector"); });
    $("#btn-inspector-close").addEventListener("click", function () { toggleDrawer("inspector", false); });
    $("#rail-scrim").addEventListener("click", function () { toggleDrawer("rail", false); });
    $("#inspector-scrim").addEventListener("click", function () { toggleDrawer("inspector", false); });

    /* ---- table ---- */
    $$("#table-tabs .seg").forEach(function (button) {
      button.addEventListener("click", function () {
        $$("#table-tabs .seg").forEach(function (other) {
          other.classList.toggle("is-active", other === button);
          other.setAttribute("aria-pressed", other === button ? "true" : "false");
        });
        state.table.subject = button.dataset.table;
        state.table.page = 0;
        renderTable();
      });
    });
    const tableFilter = debounce(function (event) {
      state.table.text = event.target.value;
      state.table.page = 0;
      renderTable();
    }, 140);
    $("#table-filter").addEventListener("input", tableFilter);
    $("#table-type-filter").addEventListener("change", function (event) {
      state.table.typeFilter = event.target.value; state.table.page = 0; renderTable();
    });
    $("#table-sort").addEventListener("change", function (event) { state.table.sort = event.target.value; renderTable(); });
    $("#btn-csv").addEventListener("click", exportTableCsv);
    $("#btn-table-sync").addEventListener("click", function () {
      // Pull the filtered rows into the canvas — the table is a query builder.
      const keys = state.table.rows.map(function (row) { return row.key || row.source; }).filter(Boolean);
      if (state.table.subject === "edges") {
        const keep = new Set();
        state.table.rows.forEach(function (edge) { keep.add(edge.source); keep.add(edge.target); });
        isolateSelection(Array.from(keep));
      } else if (state.table.subject === "nodes") {
        isolateSelection(keys.slice(0, 400));
      } else {
        toast("Citations cannot be drawn", "Switch to Nodes or Edges to sync the canvas.", "warn");
        return;
      }
      switchView("graph");
      toast("Synced to graph", state.nodes.size + " entities · " + state.edges.size + " relationships.", "success");
    });
    $("#page-first").addEventListener("click", function () { state.table.page = 0; renderTable(); });
    $("#page-prev").addEventListener("click", function () { state.table.page = Math.max(0, state.table.page - 1); renderTable(); });
    $("#page-next").addEventListener("click", function () { state.table.page += 1; renderTable(); });
    $("#page-last").addEventListener("click", function () { state.table.page = 1e9; renderTable(); });
    $("#page-size").addEventListener("change", function (event) {
      state.table.pageSize = clamp(toNumber(event.target.value, 50), 10, 500);
      config.pageSize = state.table.pageSize;
      state.table.page = 0; renderTable(); saveConfig();
    });

    /* ---- pathfinding ---- */
    $("#btn-find-path").addEventListener("click", findPath);
    $("#btn-path-clear").addEventListener("click", function () {
      ["#path-a", "#path-b"].forEach(function (selector) { const input = $(selector); input.value = ""; delete input.dataset.key; });
      $$(".path-swatch").forEach(function (s) { s.classList.remove("is-set"); });
      state.pathSelection = { from: null, to: null };
      state.path = null;
      $("#path-result").innerHTML = "";
      $("#path-alts").innerHTML = "";
      clearPathHighlight();
      saveHash();
    });
    $("#path-swap").addEventListener("click", function () {
      const a = $("#path-a"), b = $("#path-b");
      const value = a.value, key = a.dataset.key;
      a.value = b.value; a.dataset.key = b.dataset.key || "";
      b.value = value; b.dataset.key = key || "";
      state.pathSelection = { from: state.pathSelection.to, to: state.pathSelection.from };
      $$(".path-swatch").forEach(function (s, index) { s.classList.toggle("is-set", Boolean(index === 0 ? a.dataset.key : b.dataset.key)); });
      saveHash();
    });
    $("#path-hops").addEventListener("input", function (event) {
      config.pathHops = clamp(toNumber(event.target.value, 6), 1, 12);
      syncRange(event.target);
      $("#path-hops-out").textContent = String(config.pathHops);
    });
    $("#path-hops").addEventListener("change", saveConfig);
    $("#path-direction").addEventListener("change", function (event) { config.pathDirection = event.target.value; saveConfig(); });
    $("#path-weight").addEventListener("change", function (event) { config.pathCost = event.target.value; saveConfig(); });
    ["#path-a", "#path-b"].forEach(function (selector) {
      $(selector).addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); findPath(); } });
      $(selector).addEventListener("input", function (event) { delete event.target.dataset.key; });
    });
    $("#neighbour-depth").addEventListener("input", function (event) {
      state.depth = clamp(toNumber(event.target.value, 1), 1, 4);
      syncRange(event.target);
      $("#neighbour-depth-out").textContent = state.depth + " hop" + (state.depth === 1 ? "" : "s");
      applyConfigToUi(); saveFilters();
    });
    $("#btn-neighbour-run").addEventListener("click", function () { expandNode(state.activeKey, state.depth); });
    $("#btn-path-from-active").addEventListener("click", function () {
      const node = state.nodes.get(state.activeKey);
      if (node) { setPathEndpoint("from", node); toast("Entity A set", escapeHtml(node.name), "info", 2200); }
    });
    $("#btn-path-to-active").addEventListener("click", function () {
      const node = state.nodes.get(state.activeKey);
      if (node) { setPathEndpoint("to", node); toast("Entity B set", escapeHtml(node.name), "info", 2200); }
    });

    /* ---- settings ---- */
    $("#cfg-mode").addEventListener("change", function (event) { syncSettingsForm(event.target.value); });
    $("#btn-save-cfg").addEventListener("click", async function () {
      readSettingsForm();
      saveConfig();
      await switchProvider(true);
      closeModal();
    });
    $("#btn-test-conn").addEventListener("click", async function () {
      readSettingsForm();
      await probeConnection(true);
    });
    $("#btn-clear-cfg").addEventListener("click", function () {
      forgetCredentials();
      $("#cfg-token").value = ""; $("#cfg-neo4j-user").value = ""; $("#cfg-neo4j-pass").value = "";
      $("#cfg-probe").textContent = "Credentials removed from this device.";
      toast("Credentials forgotten", "The token was deleted from <code>localStorage</code>. The mode is unchanged.", "success");
    });
    $("#cfg-autosuggest").addEventListener("change", function (e) { config.autosuggest = e.target.checked; saveConfig(); });
    $("#cfg-autoexpand").addEventListener("change", function (e) { config.autoexpand = e.target.checked; saveConfig(); });
    $("#cfg-persist").addEventListener("change", function (e) { config.persist = e.target.checked; saveConfig(); });
    $("#cfg-reduce-motion").addEventListener("change", function (e) {
      config.reduceMotion = e.target.checked;
      document.body.classList.toggle("reduce-motion", e.target.checked || prefersReducedMotion());
      saveConfig();
    });
    $("#cfg-page-size").addEventListener("change", function (e) {
      config.pageSize = clamp(toNumber(e.target.value, 50), 10, 500);
      state.table.pageSize = config.pageSize;
      setValue("#page-size", String(config.pageSize));
      renderTable(); saveConfig();
    });
    $("#cfg-request-timeout").addEventListener("change", function (e) {
      config.requestTimeout = clamp(toNumber(e.target.value, 15000), 2000, 120000); saveConfig();
    });
    $("#btn-copy-link").addEventListener("click", async function () {
      const ok = await copyText(location.href);
      toast(ok ? "Link copied" : "Copy failed", ok ? "The URL encodes the view, query, focus and depth." : "Clipboard access was blocked.", ok ? "success" : "warn");
    });
    $("#btn-copy-cypher").addEventListener("click", async function () {
      const text = cypherForCurrentView();
      const ok = await copyText(text);
      toast(ok ? "Cypher copied" : "Copy failed", ok ? "<code>" + escapeHtml(text.split("\n")[0].replace("// ", "")) + "</code>" : "", ok ? "success" : "warn");
    });

    /* ---- window ---- */
    window.addEventListener("resize", debounce(function () {
      if (cy) cy.resize();
      moveTabInk();
    }, 140));
    window.addEventListener("hashchange", function () { applyHash(); });
    document.addEventListener("click", function (event) {
      if (!$("#ctx-menu").contains(event.target)) hideContextMenu();
    });
    window.addEventListener("blur", hideContextMenu);
  }

  function exportPng() {
    if (!cy || !cy.nodes().length) { toast("Nothing to export", "Load a graph first.", "warn"); return; }
    try {
      const data = cy.png({ full: true, scale: clamp(window.devicePixelRatio || 1, 1, 2.5), bg: "#05070d" });
      const a = el("a", { href: data, download: "puppetnet-graph-" + Date.now() + ".png" });
      document.body.appendChild(a); a.click(); a.remove();
      toast("PNG exported", cy.nodes().length + " nodes · " + cy.edges().length + " edges at " + (window.devicePixelRatio || 1) + "×.", "success");
    } catch (err) {
      toast("Export failed", escapeHtml(err && err.message ? err.message : String(err)), "error");
    }
  }

  function toggleFullscreen() {
    const target = $("#panel-graph");
    if (!document.fullscreenElement) {
      if (target.requestFullscreen) target.requestFullscreen().catch(function () { toast("Fullscreen blocked", "The browser refused the request.", "warn"); });
    } else if (document.exitFullscreen) {
      document.exitFullscreen();
    }
    setTimeout(function () { if (cy) { cy.resize(); fitView(48); } }, 220);
  }

  async function resetView() {
    state.hidden.clear();
    state.filters.types.clear();
    state.filters.relTypes.clear();
    state.filters.minWeight = 0;
    state.filters.minConfidence = 0;
    state.filters.hideCalculated = false;
    state.filters.hideWeak = false;
    state.filters.text = "";
    state.table.text = "";
    $("#table-filter").value = "";
    state.cache.clear();
    applyConfigToUi();
    saveFilters();
    clearSelection();
    clearPathHighlight();
    switchView("graph");
    await loadOverview({ quiet: true });
    toast("View reset", "Filters cleared and the overview reloaded.", "info");
  }

  async function refresh() {
    state.cache.clear();
    if (state.scope.kind === "search" && state.query) await loadSearch(state.query, { expand: true });
    else if (state.scope.key) await expandNode(state.scope.key, state.depth);
    else await loadOverview();
  }

  /* ===========================================================================
     19. Provider switching & health probe
     ======================================================================== */

  async function switchProvider(reload) {
    state.provider = resolveProvider();
    state.providerName = state.provider.name;
    state.cache.clear();
    setConnection(state.provider.isLive ? "idle" : "demo", state.provider.label, "");
    const ok = await probeConnection(false);
    if (reload) {
      state.nodes.clear(); state.edges.clear(); state.docs.clear(); state.hidden.clear();
      state.scope = { kind: "overview", label: "whole graph", key: null };
      setActive(null);
      await loadOverview({ quiet: true });
      toast("Data source", "Now reading from <b>" + escapeHtml(state.provider.label) + "</b>" + (ok ? "" : " (probe failed)"), ok ? "success" : "warn");
    }
    return ok;
  }

  async function probeConnection(verbose) {
    if (!state.provider) state.provider = resolveProvider();
    setBusy(true, "probing");
    try {
      const health = await state.provider.health();
      const graph = (health && health.graph) || {};
      const live = state.provider.isLive && health && health.ok;
      state.connection.version = health && health.version ? String(health.version) : "";
      $("#cfg-worker-version").textContent = "worker: " + (state.connection.version || "unknown");
      if (graph.limits && graph.limits.max_depth) state.stats.maxDepth = graph.limits.max_depth;
      if (graph.nodes !== undefined) {
        state.stats.caps = { node_cap: 200000, edge_cap: 400000, nodes: graph.nodes, edges: graph.edges };
      }
      setConnection(live ? "live" : "demo", state.provider.label,
        (graph.database ? "db " + graph.database + " · " : "") +
        (graph.nodes !== undefined ? graph.nodes + " nodes / " + graph.edges + " edges · " : "") +
        (health && health.note ? health.note : ""));
      updateHud();
      if (verbose) {
        $("#cfg-probe").innerHTML = live
          ? '<span class="good">✓ reachable</span> — ' + escapeHtml(state.provider.label) +
            (graph.database ? " · database <code>" + escapeHtml(graph.database) + "</code>" : "") +
            (graph.nodes !== undefined ? " · " + escapeHtml(String(graph.nodes)) + " entities" : "")
          : '<span class="warn">▲ demo mode</span> — ' + escapeHtml(health && health.note || "synthetic dataset loaded");
        toast("Connection OK", escapeHtml(state.provider.label) + (graph.nodes !== undefined ? " · " + graph.nodes + " entities" : ""), "success");
      }
      return true;
    } catch (err) {
      setConnection("error", "api unreachable", err && err.message ? err.message : "");
      $("#cfg-probe").innerHTML = '<span class="bad">✕ unreachable</span> — ' + describeError(err);
      if (verbose) toast("Connection failed", describeError(err), "error", 9000);
      // A configured-but-dead API should not leave the analyst with a blank
      // canvas: fall back to the demo data and say so.
      if (state.provider.isLive && config.mode !== "demo") {
        toast("Falling back to demo data", "The configured API is unreachable, so the console loaded the bundled synthetic dataset.", "warn", 7000);
        config.mode = "demo";
        state.provider = resolveProvider();
        setConnection("demo", state.provider.label, "fallback: configured API unreachable");
        await state.provider.health().catch(function () { return null; });
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  function syncSettingsForm(mode) {
    const active = mode || config.mode;
    $("#cfg-mode").value = active;
    $("#row-base").hidden = active === "demo";
    $("#row-token").hidden = active !== "worker";
    $("#row-db").hidden = active !== "neo4j";
    $("#row-neo4j-creds").hidden = active !== "neo4j";
    $("#cfg-base").value = config.base || "";
    $("#cfg-base").placeholder = active === "neo4j" ? "neo4j+s://xxxx.databases.neo4j.io:7687" : "https://relay.example.workers.dev";
    $("#cfg-token").value = config.token || "";
    $("#cfg-db").value = config.database || "neo4j";
    $("#cfg-neo4j-user").value = config.neo4jUser || "";
    $("#cfg-neo4j-pass").value = config.neo4jPass || "";
    $("#cfg-autosuggest").checked = Boolean(config.autosuggest);
    $("#cfg-autoexpand").checked = Boolean(config.autoexpand);
    $("#cfg-persist").checked = Boolean(config.persist);
    $("#cfg-reduce-motion").checked = Boolean(config.reduceMotion);
    $("#cfg-page-size").value = String(config.pageSize);
    $("#cfg-request-timeout").value = String(config.requestTimeout);
    $("#cfg-mode-help").innerHTML = active === "demo"
      ? "The demo dataset is a synthetic " + demoDataset().nodes.length + "-node offshore network. Every entity in it is invented and nothing leaves the browser."
      : active === "worker"
        ? "Requests go to <code>" + escapeHtml(apiUrl(config, "/graph") || "/graph") + "</code>. The Worker holds the Neo4j credentials; the browser never sees them."
        : "Direct Neo4j HTTP transaction API. Credentials stay in this browser's <code>localStorage</code> — use a scratch database only.";
  }

  function readSettingsForm() {
    config.mode = $("#cfg-mode").value;
    config.base = $("#cfg-base").value.trim();
    config.token = $("#cfg-token").value.trim();
    config.database = $("#cfg-db").value.trim() || "neo4j";
    config.neo4jUser = $("#cfg-neo4j-user").value.trim();
    config.neo4jPass = $("#cfg-neo4j-pass").value;
    config.autosuggest = $("#cfg-autosuggest").checked;
    config.autoexpand = $("#cfg-autoexpand").checked;
    config.persist = $("#cfg-persist").checked;
    config.reduceMotion = $("#cfg-reduce-motion").checked;
    config.pageSize = clamp(toNumber($("#cfg-page-size").value, 50), 10, 500);
    config.requestTimeout = clamp(toNumber($("#cfg-request-timeout").value, 15000), 2000, 120000);
    state.table.pageSize = config.pageSize;
    syncSettingsForm(config.mode);
  }

  /* ===========================================================================
     20. Keyboard shortcuts
     ======================================================================== */

  function isTyping(target) {
    if (!target) return false;
    const tag = String(target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }

  function bindKeyboard() {
    document.addEventListener("keydown", function (event) {
      const mod = event.metaKey || event.ctrlKey;

      if (mod && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchInput().focus();
        searchInput().select();
        return;
      }
      if (event.key === "Escape") {
        if (state.modalOpen) { closeModal(); return; }
        if (!$("#ctx-menu").hidden) { hideContextMenu(); return; }
        if ($("#rail").classList.contains("is-open")) { toggleDrawer("rail", false); return; }
        if ($("#inspector").classList.contains("is-open")) { toggleDrawer("inspector", false); return; }
        if (document.activeElement === searchInput()) return;
        if (state.path) { clearPathHighlight(); state.path = null; return; }
        clearSelection();
        return;
      }
      if (isTyping(event.target)) return;

      if (event.key === "/" ) { event.preventDefault(); searchInput().focus(); return; }
      if (event.key === "?") { event.preventDefault(); openModal("#modal-help"); return; }

      const key = event.key.toLowerCase();
      if (key === "g") { event.preventDefault(); switchView("graph"); return; }
      if (key === "t") { event.preventDefault(); switchView("table"); return; }
      if (key === "p") { event.preventDefault(); switchView("path"); return; }
      if (key === "s") { event.preventDefault(); openModal("#modal-settings"); return; }
      if (key === "e") { event.preventDefault(); expandNode(state.activeKey, state.depth); return; }
      if (key === "i") { event.preventDefault(); isolateNode(state.activeKey, state.depth); return; }
      if (key === "r") { event.preventDefault(); resetView(); return; }
      if (key === "f") { event.preventDefault(); fitView(56); return; }
      if (key === "c") { event.preventDefault(); if (state.activeKey) centerOn(state.activeKey, 1.3); return; }
      if (key === "l") { event.preventDefault(); state.layoutSeeded = false; runLayout(state.render.layout, { fit: true }); return; }
      if (key === "h") { event.preventDefault(); if (state.activeKey) hideNode(state.activeKey); return; }
      if (event.key === "+" || event.key === "=") { event.preventDefault(); zoomBy(1.32); return; }
      if (event.key === "-" || event.key === "_") { event.preventDefault(); zoomBy(1 / 1.32); return; }
      if (["1", "2", "3", "4"].indexOf(event.key) >= 0) {
        event.preventDefault();
        state.depth = toNumber(event.key, 1);
        applyConfigToUi();
        saveFilters();
        if (state.activeKey) expandNode(state.activeKey, state.depth);
        return;
      }
      // Arrow keys pan when the canvas has focus.
      if (cy && ["arrowup", "arrowdown", "arrowleft", "arrowright"].indexOf(key) >= 0) {
        event.preventDefault();
        const pan = cy.pan();
        const step = 60 / cy.zoom();
        if (key === "arrowup") pan.y += step;
        if (key === "arrowdown") pan.y -= step;
        if (key === "arrowleft") pan.x += step;
        if (key === "arrowright") pan.x -= step;
        cy.pan(pan);
      }
    });
  }

  /* ===========================================================================
     21. Deep links
     ======================================================================== */

  function pushHistory(kind, detail) {
    state.history.push(Object.assign({ kind: kind, at: Date.now() }, detail || {}));
    if (state.history.length > 60) state.history.shift();
  }

  /**
   * Encode enough of the session to reproduce it from a URL: view, query, focus
   * node, depth and the current handshake. Analysts share findings by link, and
   * a link that opens a different graph than the sender saw is worse than no link.
   */
  const saveHash = debounce(function () {
    const params = new URLSearchParams();
    params.set("v", state.view);
    if (state.query) params.set("q", state.query);
    if (state.activeKey) params.set("focus", state.activeKey);
    if (state.depth !== DEFAULTS.depth) params.set("depth", String(state.depth));
    if (state.render.sizeMetric !== DEFAULTS.sizeMetric) params.set("metric", state.render.sizeMetric);
    if (state.render.layout !== DEFAULTS.layout) params.set("layout", state.render.layout);
    if (config.mode !== DEFAULTS.mode) params.set("mode", config.mode);
    if (config.base) params.set("api", config.base);
    if (state.pathSelection.from) params.set("a", state.pathSelection.from);
    if (state.pathSelection.to) params.set("b", state.pathSelection.to);
    const next = "#/" + params.toString();
    if (location.hash !== next) history.replaceState(null, "", next);
  }, 220);

  function parseHash() {
    const raw = String(location.hash || "").replace(/^#\/?/, "");
    const params = new URLSearchParams(raw);
    const out = {};
    params.forEach(function (value, key) { out[key] = value; });
    return out;
  }

  async function applyHash() {
    const hash = parseHash();
    if (hash.mode && hash.mode !== config.mode) {
      config.mode = hash.mode;
      if (hash.api) config.base = hash.api;
      await switchProvider(false);
      syncSettingsForm();
    }
    if (hash.depth) { state.depth = clamp(toNumber(hash.depth, 1), 1, 4); applyConfigToUi(); }
    if (hash.metric && SIZE_METRICS[hash.metric]) { state.render.sizeMetric = hash.metric; applyConfigToUi(); }
    if (hash.layout) { state.render.layout = hash.layout; applyConfigToUi(); }
    if (hash.q && hash.q !== state.query) {
      searchInput().value = hash.q;
      await loadSearch(hash.q, { expand: true });
    } else if (hash.focus) {
      if (!state.nodes.has(hash.focus)) {
        // The entity is real but outside the loaded page — a top-N overview does
        // not contain everything. Pull its neighbourhood so the link lands where
        // it was meant to, instead of quietly dropping the focus (which the next
        // debounced saveHash would then erase from the URL as well).
        const pulled = await runQuery("focus", function () {
          return state.provider.neighbors(hash.focus, {
            depth: clamp(toNumber(state.depth, 1), 1, 4),
            limit: clamp(toNumber(config.nodeLimit, DEFAULTS.nodeLimit), 1, 1200),
          });
        }, { label: "restoring focus", fallback: true, reportAbort: false });
        if (pulled) mergePayload(pulled, { mode: "merge", relayout: false, fit: false });
      }
      if (state.nodes.has(hash.focus)) {
        setActive(hash.focus, { center: true });
        selectElements([hash.focus], false);
      }
    }
    if (hash.a && hash.b) {
      const a = $("#path-a"), b = $("#path-b");
      a.dataset.key = hash.a; a.value = nameOf(hash.a);
      b.dataset.key = hash.b; b.value = nameOf(hash.b);
      state.pathSelection = { from: hash.a, to: hash.b };
      $$(".path-swatch").forEach(function (s) { s.classList.add("is-set"); });
      if (hash.v === "path") findPath();
    }

    // Last, so it wins: focusing an entity or running a search both switch to the
    // canvas, which is the right answer to a click and the wrong answer to a link
    // that says "table view, focused on X". The view the analyst shared is the
    // view they get.
    if (hash.v && ["graph", "table", "path"].indexOf(hash.v) >= 0) switchView(hash.v);
  }

  /* ===========================================================================
     22. Boot
     ======================================================================== */

  function fatal(message, detail) {
    const boot = $("#boot");
    boot.classList.remove("is-gone");
    $("#boot-status").innerHTML = '<span class="bad">' + escapeHtml(message) + "</span>" + (detail ? "<br>" + detail : "");
    boot.querySelector(".boot-core").style.display = "none";
  }

  async function boot() {
    const bootStatus = $("#boot-status");
    try {
      loadConfig();
      restoreFilters();
      state.depth = clamp(toNumber(state.depth, DEFAULTS.depth), 1, 4);
      state.table.pageSize = clamp(toNumber(config.pageSize, 50), 10, 500);
      state.table.sort = "score-desc";
      applyConfigToUi();

      if (typeof cytoscape !== "function") {
        fatal("Graph engine failed to load", "vendor/cytoscape.min.js is missing. The Data and Handshake views still work from the API.");
        $("#app").hidden = false;
        $("#app").classList.add("is-ready");
        wireSearch(); bindControls(); bindKeyboard(); bindModals(); startClock();
        state.provider = resolveProvider();
        await probeConnection(false);
        return;
      }

      bootStatus.textContent = "starting graph engine…";
      cy = initCy();
      if (!cy) { fatal("Cytoscape refused to start", "The canvas container is missing."); return; }
      state.cy = cy;

      bootStatus.textContent = "wiring controls…";
      wireSearch();
      bindControls();
      bindKeyboard();
      bindModals();
      startClock();
      moveTabInk();

      bootStatus.textContent = "connecting to data source…";
      state.provider = resolveProvider();
      await probeConnection(false);

      $("#app").hidden = false;
      requestAnimationFrame(function () {
        $("#app").classList.add("is-ready");
        if (cy) cy.resize();
        moveTabInk();
      });

      bootStatus.textContent = "loading graph…";
      const hash = parseHash();
      if (hash.q) {
        // A search link carries its own data: applyHash runs the query.
        await applyHash();
      } else {
        await loadOverview({ quiet: true });
        // Everything else a link can carry — view, depth, metric, layout, a
        // focused entity, a handshake pair — is state on top of a loaded graph,
        // so it is applied after the data exists rather than instead of it.
        if (Object.keys(hash).length) await applyHash();
      }

      state.ready = true;
      $("#boot").classList.add("is-gone");
      setTimeout(function () { const boot = $("#boot"); if (boot) boot.remove(); }, 520);

      if (state.provider.name === "demo") {
        toast("Demo dataset", "Synthetic offshore network — <b>every entity is invented</b>. Open settings (" +
          (navigator.platform.indexOf("Mac") >= 0 ? "⌘" : "Ctrl") + "+S or the gear) to connect a Worker.", "info", 8000);
      } else if (!state.nodes.size) {
        $("#cy-empty").hidden = false;
        $("#cy-empty-msg").textContent = "The API answered but returned no entities. Check that ingest.py and graph_analytics.py have run against this database.";
      }
    } catch (err) {
      console.error("[console] boot failed", err);
      fatal("Console failed to start", escapeHtml(err && err.message ? err.message : String(err)));
    }
  }

  /* ===========================================================================
     23. Public surface
     ======================================================================== */

  /**
   * Exposed for two reasons: an analyst can drive the console from devtools
   * (`PuppetNET.state`, `PuppetNET.cy`), and the offline test harness in
   * `tests/web_smoke.mjs` can exercise the pure logic without a browser.
   */
  const api = {
    version: VERSION,
    state: state,
    config: config,
    get cy() { return cy; },

    // pure / testable
    escapeHtml: escapeHtml,
    escapeCypher: escapeCypher,
    normalizeText: normalizeText,
    normalizeNode: normalizeNode,
    normalizeEdge: normalizeEdge,
    normalizeDoc: normalizeDoc,
    scoreMatch: scoreMatch,
    sizeScale: sizeScale,
    metricValue: metricValue,
    sizeFor: sizeFor,
    colorForNode: colorForNode,
    clusterColor: clusterColor,
    nodeShapeClass: nodeShapeClass,
    shortLabel: shortLabel,
    bfsSubgraph: bfsSubgraph,
    localShortestPath: localShortestPath,
    alternativePaths: alternativePaths,
    buildCsv: buildCsv,
    csvCell: csvCell,
    demoKey: demoKey,
    demoDataset: demoDataset,
    parseHash: parseHash,
    cypherForCurrentView: cypherForCurrentView,
    tableFilteredRows: tableFilteredRows,
    sortRows: sortRows,
    localSearch: localSearch,
    hash32: hash32,
    formatNumber: formatNumber,
    formatScore: formatScore,
    formatDate: formatDate,
    relativeTime: relativeTime,
    buildElements: buildElements,
    layoutOptions: layoutOptions,
    cyStyles: cyStyles,
    ENTITY_TYPES: ENTITY_TYPES,
    REL_TYPES: REL_TYPES,
    REL_GROUPS: REL_GROUPS,
    SIZE_METRICS: SIZE_METRICS,
    SORTABLE_METRICS: SORTABLE_METRICS,
    DEFAULTS: DEFAULTS,

    // actions
    actions: {
      switchView: switchView,
      loadOverview: loadOverview,
      loadSearch: loadSearch,
      expandNode: expandNode,
      isolateNode: isolateNode,
      resetView: resetView,
      refresh: refresh,
      findPath: findPath,
      setPathEndpoint: setPathEndpoint,
      setActive: setActive,
      applyFilters: applyFilters,
      runLayout: runLayout,
      fitView: fitView,
      exportPng: exportPng,
      switchProvider: switchProvider,
      probeConnection: probeConnection,
      renderTable: renderTable,
      renderInspector: renderInspector,
      toast: toast,
      saveHash: saveHash,
      // Modal plumbing and the clipboard helper are shared with modals.js so the
      // donation, legal and contact dialogs join the same Escape cascade and the
      // same secure-context clipboard path instead of reinventing either.
      openModal: openModal,
      closeModal: closeModal,
      copyText: copyText,
      applyHash: applyHash,
    },
  };

  window.PuppetNET = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
