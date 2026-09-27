# Analyst console (`web/`)

A single-page OSINT console for the PuppetNET graph: search an entity, watch its
neighbourhood assemble on a 2D canvas, walk the ties that connect two of them, and
export the evidence. Dark, keyboard-driven, and built to be readable at 2 a.m. on a
laptop with the lights off.

* Source: [`web/index.html`](../web/index.html) · [`web/app.js`](../web/app.js) · [`web/styles.css`](../web/styles.css)
* Graph engine: Cytoscape.js 3.30.4 with the fcose force-directed layout (2.2.0) — vendored, MIT
* Styling: Tailwind 3.4.17, compiled to `web/vendor/tailwind.css` (13 KB) plus hand-written `styles.css`
* Hosting: any static host. Cloudflare Pages is the reference deployment; `_headers` ships with it
* **No build step at deploy time, no runtime dependency, no CDN, no paid service.** Every
  byte the browser loads is committed in `web/`.

The console is a *reader*. It never writes to the graph — maintenance belongs to
[`graph_analytics.py`](../graph_analytics.py) and ingestion to [`ingest.py`](../ingest.py).

---

## Quick start

```bash
npm ci            # dev tooling only: jsdom, the Tailwind CLI, terser
npm run serve     # http://localhost:8080 — no credentials, no network
```

It boots straight into **demo mode**: a synthetic offshore network of 52 entities and
88 ties, generated deterministically in the browser. Every name in it is invented. That
mode exists so the console is usable — and demonstrable, and testable — with nothing
deployed behind it.

To point it at a real graph, press <kbd>S</kbd> (or the gear) and choose a data source.

---

## Data sources

| Mode | What it talks to | Needs | Use it when |
| --- | --- | --- | --- |
| `demo` | the bundled synthetic dataset | nothing | first look, screenshots, offline, CI |
| `worker` | `worker.js` → `/graph/*` → Neo4j | API base URL + `GRAPH_API_TOKEN` | **production.** The Worker holds the database credentials, rate-limits, caches and caps payload size |
| `neo4j` | Neo4j's HTTP transactional endpoint directly | HTTP root, database, user, password | local development against your own database only |

`neo4j` mode puts database credentials in a browser. It is offered for a laptop and a
local AuraDB instance, and the settings dialog says so; do not deploy it. The Worker
mode exists precisely so that credentials never reach the client: the console sends a
bearer token, the Worker sends Basic auth, and the response contains neither.

### Settings

| Field | Effect |
| --- | --- |
| Data source | `demo` / `worker` / `neo4j` (above) |
| API base URL | Worker root, or the Neo4j HTTP root. Empty means *same origin* — the right answer when the Worker is mounted on the Pages project as a Function or Worker Route |
| API token | sent as `Authorization: Bearer …` on every `/graph/*` call |
| Neo4j database | the database name in the transactional URL path |
| Neo4j user / password | direct mode only |
| Probe | tests the connection and reports version, entity count and whether the full-text index exists |
| Autosuggest | search-as-you-type dropdown |
| Auto-expand | clicking a node also expands its neighbourhood |
| Remember settings | persist config and filters in this browser. Off clears what was stored — see [Hardening](#hardening) |
| Reduce motion | no layout animation, no transitions; also honoured from `prefers-reduced-motion` |
| Table page size | 10–500 rows per page |
| Request timeout | 2–120 s, per call, enforced with an `AbortController` |

Settings and filters live in `localStorage` under `puppetnet.console.settings.v1` and
`puppetnet.console.v1.filters`. **Forget credentials** in the settings dialog clears both
and blanks the token, the Neo4j user and the password.

---

## The screen

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ⌘K search ────────────────────────────  graph │ data │ handshake   ⚙   │  header
├────────────┬─────────────────────────────────────────────┬───────────────┤
│ leaderboard│                                             │  inspector    │
│ filters    │            cytoscape canvas                 │  entity /     │
│ legend     │            + HUD (nodes, ties, scope)       │  tie detail,  │
│            │                                             │  citations    │
├────────────┴─────────────────────────────────────────────┴───────────────┤
│  source · latency · capacity · metrics timestamp                          │  status bar
└──────────────────────────────────────────────────────────────────────────┘
```

* **Header** — search first. The input queries names, aliases, company numbers,
  canonical keys, jurisdictions and aircraft tails; suggestions appear as you type
  (locally scored first, then the API), <kbd>Enter</kbd> loads the best match,
  <kbd>⇧ Enter</kbd> loads every match, <kbd>Alt Enter</kbd> opens them in the table.
* **Canvas** — the graph, with a HUD for visible node/tie counts, the current scope
  and the layout in use.
* **Left rail** — leaderboard by the active metric, filter chips per relationship
  type, and a colour legend. On a narrow screen it becomes a drawer.
* **Inspector** — everything known about the focused entity or tie: properties,
  metrics, aliases, and the citations (`Document` nodes reached through `MENTIONS`,
  or the `doc_id`/`source_id` recorded on the edge), each with its source and date.
* **Status bar** — which provider answered, how long it took, how many entities the
  API said it could give, and when the calculated metrics were last written.

---

## Canvas

**Size.** Node radius is a metric, mapped to 15–62 px. `betweenness` by default —
the brokers, not the loud ones — and switchable to anomaly score, degree, mention
count, confidence, risk score, or flat. A metric that has not been computed yet
falls back to degree rather than collapsing every node to the same dot, and the
status bar says when metrics were last refreshed. Nodes at or above 42 px get a
`hub` class, which is what the glow and the label priority key off.

**Colour.** By entity label from a fixed vocabulary — Person cyan, Organization
emerald, ShellCompany and Foundation purple, Location amber, Craft rose — or, with
*colour by cluster* on, by `cluster_id` from label propagation, which is how you see
a whole offshore cluster light up at once. Calculated edges (`PUPPET_MASTER_OF`) are
always violet: they are inference, and they should not look like evidence.

**Edges.** Grouped into control & ownership, roles & employment, money flows, place
& registration, movement & craft, social & adversarial, weak co-occurrence and
calculated — each with its own colour and a filter chip. Width tracks `weight`,
and the inspector shows `weight`, `confidence`, the extraction `method` and the
citation behind the tie.

**Layout.** `fcose` (force-directed, the default), `cose` (no extension), concentric
by centrality, breadth-first hierarchy, circle and grid. Repulsion is adjustable;
animation can be turned off. **Fit**, **centre**, **re-run layout** and **export PNG**
are one keystroke each.

**Filters.** Relationship-type chips, a minimum `weight`, a minimum `confidence`,
*hide calculated* and *hide weak*. Filtering **hides by class** — it never deletes
graph state — so clearing a chip brings the same nodes back in the same places,
without another round trip.

**Expansion.** Click a node to focus and inspect it; double-click (or <kbd>E</kbd>) to
expand its neighbourhood by the current depth; <kbd>I</kbd> isolates that neighbourhood
and drops everything else; <kbd>R</kbd> resets to the overview. Depth is 1–4 hops, on a
segmented control, from the keyboard with <kbd>1</kbd>–<kbd>4</kbd>, and it applies to
the API traversal *and* to the local fallback. Expansion is capped by the node ceiling
(250 by default, 1200 maximum) and the console reports when a response was truncated
rather than silently dropping entities.

---

## Handshake (pathfinding)

Two entities, and the chain that connects them — the question an investigator actually
asks.

* **Endpoints** are typed or picked from the canvas. A name is resolved locally first
  (exact, then scored) and then against the API. A **canonical key resolves exactly or
  not at all**: keys are identities, and fuzzy-matching one can silently pick a
  different entity when two of them share a token (the aircraft tail `9h-kast` and the
  name `Kastelion` both contain `kast`). Proving a handshake to the wrong company is
  worse than reporting that the endpoint could not be resolved.
* **Hops** 1–12, **direction** undirected / outgoing / incoming, **cost** hop count,
  inverse weight or inverse confidence. Weighted searches are capped at 4 hops and
  20 000 enumerated paths server-side so a free-tier database cannot be melted by one
  click.
* **Result** — the chain with each tie's predicate, weight, confidence, method and
  source, the hop count, the mean confidence and the *weakest tie* (the one a defence
  lawyer would attack first), up to three alternative routes, the chain drawn on the
  canvas, a CSV export and the Cypher that would reproduce it.
* When the API cannot answer — unreachable, or the graph is smaller than the question —
  the console falls back to a local shortest path over what is already loaded, and
  labels the result as local. It says "no connection found within N hops" when that is
  the truth, and never invents a chain.

---

## Table

The same scope as the canvas, as rows: **nodes** (name, type, jurisdiction, degree,
betweenness, anomaly, cluster, last seen), **edges** (subject, predicate, object,
weight, confidence, method, source) or **sources** (the documents behind the ties,
with their dates and URLs).

Text filter (debounced 140 ms), type filter, minimum weight and confidence, sorting on
an allowlisted metric in either direction, and paging. Clicking a row opens the
inspector; **sync to canvas** pulls the filtered rows into the graph, which makes the
table a query builder: filter to `weight ≥ 0.7` and `type = OWNS`, then look at the
shape of what is left.

**CSV export** quotes by RFC 4180 and defuses formula injection — a cell starting with
`=`, `+`, `-`, `@`, tab or carriage return gets a leading apostrophe, because entity
names in this graph are harvested from hostile web pages and Excel would otherwise
execute them.

---

## Keyboard & mouse

| Key | Action | | Key | Action |
| --- | --- | --- | --- | --- |
| <kbd>⌘</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd> or <kbd>/</kbd> | focus search | | <kbd>G</kbd> <kbd>T</kbd> <kbd>P</kbd> | graph / table / handshake view |
| <kbd>Enter</kbd> | load top match | | <kbd>E</kbd> | expand active node |
| <kbd>⇧ Enter</kbd> | load all matches | | <kbd>I</kbd> | isolate neighbourhood |
| <kbd>Alt Enter</kbd> | open matches in the table | | <kbd>R</kbd> | reset to overview |
| <kbd>1</kbd>–<kbd>4</kbd> | expansion depth | | <kbd>F</kbd> | fit graph |
| <kbd>+</kbd> <kbd>−</kbd> | zoom | | <kbd>C</kbd> | centre on selection |
| <kbd>S</kbd> | settings | | <kbd>L</kbd> | re-run layout |
| <kbd>?</kbd> | shortcut help | | <kbd>Esc</kbd> | close / clear / deselect |

<kbd>Esc</kbd> cascades, one layer per press: modal → context menu → rail → inspector →
path highlight → selection. From the search box the first <kbd>Esc</kbd> dismisses the
suggestions and keeps the query; the second clears it and leaves the field. Shortcuts
never fire while you are typing in a field.

Mouse: click to focus and inspect · double-click to expand · shift-click to add to the
selection · alt-click to hide a node and its ties · click an edge for its evidence ·
right-click for quick actions (expand, isolate, path A/B, hide) · drag the background to
pan, scroll to zoom, drag a node to pin it (double-click the background to unpin all).

---

## Deep links

Everything worth sharing is in the URL fragment, so a link opens exactly what the sender
was looking at — and nothing sensitive:

```
#/v=table&q=kastelion&focus=PERSON:vladimir-kastelion-9f2c1a7b&depth=2&metric=degree&layout=cose&mode=worker&api=https://relay.example.dev&a=…&b=…
```

| Parameter | Meaning |
| --- | --- |
| `v` | view: `graph`, `table`, `path` |
| `q` | search query (runs on open) |
| `focus` | canonical key to select and centre |
| `depth` | hop depth, 1–4 |
| `metric` | node size metric |
| `layout` | layout name |
| `mode`, `api` | provider and API base |
| `a`, `b` | handshake endpoints; with `v=path` the chain is resolved on open |

Rules the console holds itself to, all of them tested:

* Values equal to a default are **omitted**, so links stay short. Credentials are never
  written to the URL at all — not the token, not the Neo4j password.
* The graph is **loaded before** link state is applied, and a `focus` entity outside the
  loaded page is fetched rather than dropped. Restoring a view must not leave an empty
  canvas, and the next autosave must not erase the parameter the link was built around.
* `v` is applied **last**, because focusing an entity or running a search switches to the
  canvas — the right answer to a click and the wrong answer to a link that says "table
  view, focused on X".
* Junk keys are ignored; an unknown view or metric is ignored; an out-of-range depth is
  clamped. A hand-edited link cannot break the console.
* Non-secret query parameters are also accepted for convenience (`?api=`, `?source=`,
  `?db=`). **Credentials in the query string are ignored** — see [Hardening](#hardening).

---

## Copy-Cypher

Every view can hand you the query that would reproduce it in Neo4j Browser or Bloom:
the overview ranking, the neighbourhood of the focused entity at the current depth, or
the handshake's `shortestPath`. Values are inlined as escaped literals rather than
`$parameters`, because a pasted query cannot carry a parameter map — so the escaping is
the whole defence and is tested as such: a hostile entity name stays inside its literal,
exactly one statement is ever emitted, no write clause can appear outside a literal, and
the ordering metric comes from a fixed allowlist (Cypher cannot parameterise a property
name, so a poisoned metric falls back to `anomaly_score` instead of being interpolated).

---

## Hardening

The console renders text harvested from hostile sources — document titles, entity names,
evidence snippets — and it holds a credential that can read an entire investigative
graph. Both facts are treated as design constraints.

**Content Security Policy.** `web/_headers` ships `default-src 'self'; script-src 'self'`
with no `unsafe-inline` and no `unsafe-eval` for script, plus `object-src 'none'`,
`base-uri 'self'`, `frame-ancestors 'none'` and `frame-src 'none'`. If an escaping bug
ever slipped through, an injected `onerror=` handler would have nothing to execute.
`style-src` keeps `'unsafe-inline'` because per-entity colours come from data and are
written as inline `style`; every inline style the console writes is a colour it computed
itself, never harvested text, and an inline *style* cannot execute script in a CSP3
browser. `connect-src 'self' https:` is deliberately loose so `neo4j` mode can reach a
local HTTP endpoint — **tighten it to your Worker's exact origin in production** and drop
`https:`:

```
/*
  Connect-Src: 'self' https://relay.example.dev
```

**Escaping.** One `escapeHtml` helper is used on every rendered surface — suggestions,
leaderboard, inspector, table cells, path chains, toasts, scope labels — and the smoke
suite feeds it harvested strings containing `<script>`, `onerror=`, quotes and markup in
each of those places. Inline styles are built from computed colour values only.

**Framing and referrers.** `X-Frame-Options: DENY` and `frame-ancestors 'none'`, because
clickjacking a *hide node* or *forget credentials* button is a real attack on an
investigative tool. `Referrer-Policy: no-referrer` keeps API paths and query text out of
third-party logs; `Permissions-Policy` disables camera, microphone, geolocation,
browsing topics and FLoC; `X-Content-Type-Options: nosniff`.

**Credentials.** The token is sent as an `Authorization` header and never appears in a
URL, a hash, a log line or an exported file. `loadConfig` refuses `?token=`, `?neo4jUser=`
and `?neo4jPass=` from the query string even though it accepts `?api=` and `?source=`:
query strings end up in history, bookmarks, proxy and CDN logs and `Referer` headers, so
a link may point the console at an API but may not authenticate it to that API. In Worker
mode the database password never reaches the browser at all. *Remember settings* off
clears stored config rather than merely declining to write more, and *Forget credentials*
removes the key outright.

**Read-only by construction.** The console only ever issues `GET`s; the Worker's graph
API rejects any other method with `405`, and both smoke suites assert that no statement
reaching Neo4j contains a write clause — with the detector self-tested against
`created_at` and comments so it cannot pass vacuously.

**Injection.** Cypher: parameterised values everywhere, allowlisted property names and
relationship types, `escapeCypher` for the literals in a copy-paste query. CSV: formula
guard described above. HTML: escaping described above. URL: junk and hostile parameters
ignored or clamped.

**Availability.** Per-isolate rate limiting on the graph API, a payload ceiling (an
oversized result is refused, not shipped), a request timeout with `AbortController`,
bounded traversal (depth ≤ 4, hops ≤ 12, weighted hops ≤ 4, 20 000 enumerated paths) and
a demo fallback that keeps the console usable when the API is down — while telling the
analyst it is showing synthetic data.

**Supply chain.** Four MIT libraries, committed with their licence texts and reproduced
by `npm run vendor:libs`, which fails if an installed version does not match
`package.json`. No CDN, so no third party sees an investigator's IP address or query, and
nothing breaks when one changes hands. The compiled Tailwind CSS is committed too, and CI
fails if it drifts from its sources.

---

## Deployment

### Cloudflare Pages

Output directory `web`, **no build command** — the committed files are the artefact.
`web/_headers` deploys with them, so the CSP and the cache policy apply without extra
configuration (vendored libraries get 7 days plus `must-revalidate`, not `immutable`,
because they are not content-hashed; `index.html` always revalidates).

```bash
npx wrangler pages project create puppetnet-console --production-branch=main
npx wrangler pages deploy web --project-name=puppetnet-console
```

Or let [`.github/workflows/pages_deploy.yml`](../.github/workflows/pages_deploy.yml) do it
on every push to `main` that touches `web/`: it re-verifies the console (syntax, Tailwind
drift, the full headless smoke suite) and then uploads. It needs `CLOUDFLARE_API_TOKEN`
and `CLOUDFLARE_ACCOUNT_ID` as repository secrets, takes an optional `PAGES_PROJECT_NAME`
variable (default `puppetnet-console`), and without those secrets it verifies and skips
the upload with a notice rather than failing.

Mount the Worker on the same hostname (Pages Functions or a Worker Route) and leave *API
base URL* empty — same-origin means no CORS and no second certificate.

### Anywhere else

`web/` is static. Serve it from nginx, Caddy, S3 or a USB stick, and reproduce the
headers from `web/_headers` — the CSP and `X-Frame-Options` are the ones that matter.
Nothing in the console assumes Cloudflare.

### The API behind it

The graph endpoints are documented in [docs/edge-relay.md](edge-relay.md#graph-read-api).
Set `GRAPH_API_TOKEN` on the Worker, keep `GRAPH_PUBLIC_READ=false`, and give the console
that token. `GRAPH_CACHE_TTL_SECONDS`, `GRAPH_RATE_PER_SEC`, `GRAPH_MAX_NODES` and
`GRAPH_TIMEOUT_MS` tune it; `.env.example` documents every variable.

---

## Development

```
web/index.html        603 lines — layout, panels, dialogs, control markup
web/app.js          5 579 lines — one IIFE, twenty numbered sections, no framework
web/styles.css      1 251 lines — dark theme, glow, animations, responsive rules
web/_headers                  — CSP and cache policy for Cloudflare Pages
web/tailwind.config.cjs       — content globs (resolved from __dirname, not the CWD)
web/tailwind.input.css        — the directives Tailwind compiles
web/vendor/                   — cytoscape, fcose, layout-base, cose-base, tailwind.css,
                                four MIT licence texts, and a README inventory
scripts/vendor-libs.mjs       — reproduces web/vendor/ from node_modules, strictly
```

`app.js` is deliberately framework-free and organised in numbered sections: constants and
vocabularies, helpers, toasts, config and persistence, providers (demo / worker / neo4j)
and their normalisers, graph building, styles and layouts, rendering, the rail, the
inspector, search, filters, table, pathfinding, export, modals, keyboard, and boot.

It exports `window.PuppetNET` — version, `state`, `config`, the live `cy()` instance, the
pure helpers (`escapeHtml`, `bfsSubgraph`, `localShortestPath`, `buildCsv`, `demoDataset`,
`parseHash`, `cypherForCurrentView`, …) and the actions (`loadSearch`, `expandNode`,
`isolateNode`, `findPath`, `applyFilters`, `runLayout`, `exportPng`, `switchProvider`, …).
That surface exists for the tests, and it is the honest way to drive the UI headlessly:
the suite clicks the same buttons an analyst clicks.

Reproducing the assets after a dependency bump:

```bash
npm install && npm run vendor:libs && npm run build:css
```

`build:css` scans `index.html` and `app.js` for class names, so a class built by string
concatenation will not appear in the compiled CSS — write class names literally.

---

## Testing

```bash
npm run check         # node --check worker.js && node --check web/app.js
npm run test:web      # 24 checks, ~19 s
npm run test:worker   # 32 checks
npm test              # all three
```

`tests/web_smoke.mjs` boots the **shipped** `index.html`, `app.js` and vendored engine
inside jsdom and drives the real UI — no mocks of the console itself. It covers: offline
demo boot and render; the rail, leaderboard, filter chips and legend; asset references
(everything on disk, nothing remote); the deployed `_headers`; HTML escaping across every
rendered surface; CSV quoting and formula defence; node sizing by betweenness with
fallback and ceiling; colour by label and by cluster; filters hiding without destroying
state; `bfsSubgraph` depth and limit; node expansion and the depth control; local and
live shortest paths; the handshake panel end to end; table filtering, sorting and paging;
keyboard shortcuts and the <kbd>Esc</kbd> cascade; deep-link round-trips including junk
and hostile parameters; booting without Cytoscape; **worker mode against the real
`worker.js`** with a fake Neo4j behind it; the offline fallback; canonical-key format
agreement with the Python resolver; the formatting helpers; copy-Cypher injection
resistance; and credential handling.

Both suites stub every upstream, so they cannot pass because a third-party API happened
to be reachable — and they assert that nothing they ran threw an uncaught error.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Graph engine failed to load" | `web/vendor/cytoscape.min.js` missing (partial deploy) | redeploy the whole `web/` directory; the table and handshake views still work from the API |
| Empty canvas, "Demo dataset" toast | no API configured, or the API is unreachable and the console fell back | <kbd>S</kbd> → set mode, base URL and token → **Probe** |
| Probe says 401 | wrong or missing `GRAPH_API_TOKEN`, or `GRAPH_API_ENABLED=false` | match the Worker secret; check `/graph/health` |
| Probe says the full-text index is missing | `puppetnet_entity_search` not created | search still works through the `CASE` fallback; run the schema step in [`puppetnet/graph/schema.py`](../puppetnet/graph/schema.py) |
| CORS error in worker mode | `ALLOWED_ORIGINS` on the Worker does not include the console's origin | add it, or mount the Worker same-origin and leave the base URL empty |
| Every node the same size | metrics have not been computed | run `graph_analytics.py --centrality`; the console falls back to degree and says when metrics were last written |
| Layout looks frozen | animation off, or `reduce motion` on | <kbd>L</kbd> re-runs it; check the Reduce-motion setting |
| Rows missing from the table | a filter, a minimum weight/confidence, or a page | <kbd>R</kbd> resets filters and reloads the overview |
