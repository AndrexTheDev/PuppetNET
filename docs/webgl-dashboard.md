# WebGL dashboard — Cloudflare Pages static export

## Entry point and architecture

Open `/dashboard/` (also linked on the root page). The app is built with Next.js 15,
Sigma **4.0.3** (the v4 options/styles API), Graphology, graphology-layout-forceatlas2,
Leaflet 1.9 and React Leaflet 5. Browser-only modules are dynamically imported with
SSR disabled. All routes are statically exported: `npm run build` produces
`apps/web/out/dashboard/index.html` plus local JS/CSS/worker chunks. No Next server,
server action, API route, CDN JavaScript, map API key or paid inference API is needed.
Cloudflare Pages uses the existing native Git build configuration and `npm run ci:pages`.

- `NetworkGraph.tsx`: WebGL2 Sigma renderer, resize/context-loss handling, batched
  Graphology construction, zoom/reset, click selection and transferred position updates.
- `graph.worker.ts`: parses JSON, generates fixtures, filters, groups, creates layout
  graphs and executes ForceAtlas2 with Barnes–Hut. No ForceAtlas2 computation runs on
  the UI thread. Typed position buffers are transferred, not copied as large JSON blobs.
- Layout is explicitly bounded (up to 15 seconds / 12 large-graph passes) and can be
  paused/restarted. Worker, renderer, observer, timers and requests are cleaned up.
- `graph-data.ts`: shared validation, evidence, filtering, grouping and geographic bins.
- `MapModule.tsx`: opt-in Leaflet map with Canvas CircleMarkers, no icon CDN requests.
- `SidePanel.tsx`: node/group properties, evidence, parameterized Cypher export.
- Data table: 50 rows per page, searchable, keyboard-selectable alternative to the canvas.

## Sources and imports

1. Default **fictional** demo: 900 nodes, 2,700 edges. It is never labeled live data.
2. **120,000-element fixture** button: 30,000 nodes + 90,000 edges, also explicitly fictional.
3. JSON file import: max **64 MiB**, **200,000 nodes**, **500,000 edges**. JSON parsing is
   in the worker. Importing an invalid/dangling/duplicate-ID graph reports an error
   and preserves the previous view/source label; no silent replacement with demo data.
4. Worker API: optional HTTPS base URL or empty for same-origin; fetches
   `/graph/overview?limit=5000`, and `/graph/node?key=…` on selection. Bearer token
   stays in memory, never in URLs/localStorage. Requests are bounded, abortable,
   redirect-refusing, and credential-free except for the explicit bearer header.

**Backend limits remain in place.** The existing API may clamp an overview to 250 or
another configured ceiling. This dashboard does not bypass server/free-tier safeguards
or claim a 120k live download from that endpoint. Large datasets use local JSON. The
current Worker does not expose all coordinate properties, so real map markers require
coordinates in the imported payload (or a separately reviewed API enhancement).
No automatic geocoding and no guessed locations are performed.

Supported JSON (`key`/`name`/`entity_type` from the existing Worker also work; Graphology
`attributes` objects are accepted):

```json
{
  "nodes": [
    {"id":"PERSON:alice","label":"Alice","type":"Person","confidence":0.9,
     "cluster_id":"c1","latitude":40.4168,"longitude":-3.7038,"doc_ids":["doc:1"]},
    {"id":"doc:1","type":"Document","title":"Original registry record",
     "url":"https://example.org/record","confidence":1}
  ],
  "edges": [
    {"source":"PERSON:alice","target":"doc:1","type":"EVIDENCED_BY",
     "confidence":0.9,"weight":1}
  ]
}
```

Citations may also be supplied as top-level `evidence`/`citations` arrays or per-node
`citations`. `EVIDENCED_BY` resolves entity → document; the existing schema's `MENTIONS`
resolves document → entity. Explicit doc IDs also work. Missing documents stay labeled
references, not fabricated source URLs. Invalid protocols are never rendered as links.
Imported text is rendered as React text, not `innerHTML`.

Cypher exports are two separate files: parameterized `.cypher` and parameter JSON.
The copy action exports the query/parameter envelope. Canonical IDs must correspond
to the intended Neo4j database. A graphical link or co-occurrence is not proof of
ownership, control or wrongdoing.

## Filtering and adaptive level of detail

Confidence applies to nodes AND edges. Weight applies to edges. Both sliders operate
on [0,1]. Missing/non-finite scores become 0; filters never produce dangling edges.
Unchecking all entity types produces an empty view, not an implicit “all” fallback.

Above **5,000 filtered nodes**, LoD is enabled by default:

- camera ratio >= 0.55: overview groups;
- ratio >= 0.18: more detailed groups;
- closer zoom: fine groups;
- clicking a group exposes count, examples and internal edge count; “Gruppe untersuchen”
  drills into its actual members. Returning restores the filtered overall dataset.

Groups are deterministic display buckets based on type, supplied cluster ID and hashed
ID subdivisions. These are **not a new community-detection result**. Aggregated nodes
show counts and average confidence, edges show average scores; internal edges are
counted separately. Grouping conserves all filtered nodes/edges, not a sampled graph.
Turning LoD off renders individual nodes/edges; above 20k nodes the UI asks confirmation.
Labels are suppressed for large raw views and edges are hidden while moving the camera.
This reduces clutter and GPU work without claiming infinite device capacity.

## Geographic view / OpenStreetMap

Only valid explicit lat/lon in [-90,90]/[-180,180] are used. Null/empty/missing values do
not become (0,0). Geographic grid bins preserve the number of located nodes and keep
at most 1,000 Canvas markers. A multi-node marker is labeled as a spatial group and its
click explicitly selects an example, not an invented representative identity.

No external tile request occurs until the user presses **OpenStreetMap-Karte laden**.
The UI discloses IP transfer and shows OpenStreetMap attribution. The standard HTTPS
TileLayer uses the browser cache and visible map viewport; no offline/bulk download,
pre-fetch crawler, custom cache bypass or tile proxy is added. The deployment's
`strict-origin-when-cross-origin` referrer policy is compatible with providing a web
origin to the tile service. Service failure is shown rather than silently substituted.

Review https://operations.osmfoundation.org/policies/tiles/ before public rollout.
The public tile service is donation-funded shared infrastructure, **not an unlimited,
SLA-backed free commercial map backend**. High-traffic installations need an approved
provider or self-hosted tiles with their own terms/costs. No paid plan is enabled here.

## Keyboard navigation (WCAG 2.1 AA)

Sigma renders into WebGL, so the graph itself has no focusable DOM nodes. The canvas
container therefore behaves as one composite widget: a single tab stop with
`role="application"`, `aria-roledescription="Interaktiver Netzwerkgraph"` and
`aria-describedby` pointing at a visually hidden key reference. Everything the mouse can
do is reachable from the keyboard, and there is no keyboard trap — `Tab` always leaves.

| Key | Action |
| --- | --- |
| `Tab` / `Shift+Tab` | Skip link → filters → tabs → graph (one stop) → table → inspector |
| `←` / `↑` | previous node in the current view |
| `→` / `↓` | next node |
| `Home` / `End` | first / last node (wraps, never dead-ends) |
| `Enter` / `Space` | open the inspector for the focused node |
| `Escape` | clear the selection |
| `+` / `−` | zoom in / out |
| `0` | reset the viewport |

The mapping lives in `apps/web/lib/graph-keyboard.ts` as a pure function
(`graphKeyAction`), so `tests/a11y.test.tsx` asserts the whole key table, the wrap-around
stepping and the announcement text without a browser. The cursor is *drawn*: the focused
node is enlarged and set to white with `graph.mergeNodeAttributes`, because sigma v4
does not expose `nodeReducer` through `setSetting`. Every move writes a polite live
region (`role="status"`, `aria-atomic`): „Knoten 3 von 8: Anna Berg, Person,
Confidence 81 %". A visible badge repeats the focus target for sighted keyboard users.

The surrounding UI is operable for the same reason: view tabs are `aria-pressed` buttons
with `aria-controls`, the graph tools have text alternatives (`aria-label`), the inspector
is a labelled `region` with a keyboard-reachable close button, errors use `role="alert"`,
progress uses `role="status"`, `aria-busy` marks a loading workspace, focus is always
visible (2–3 px outline, `:focus-visible`), and `prefers-reduced-motion` disables
transitions and smooth scrolling.

## Table view: the non-visual alternative to the canvas

The **Tabelle** tab is not a fallback list of `<div>`s; it is a real `<table>` with a
`<caption>`, `scope="col"` headers and an `aria-describedby` explanation that it is the
text alternative to the WebGL graph. It renders every row of the current view (filtered
by the same query, paginated 25 at a time) with type, confidence, degree and cluster.

- the search field has a visible `<label>`; the result count is a polite live region;
- the scroll container is focusable (`tabIndex=0`) so a keyboard-only user can scroll
  without a pointer;
- each row exposes **Details** (selects the node), **Im Graphen fokussieren** (moves the
  canvas cursor) and, for published entities, **Profil** as a plain `<a href>` to the
  prerendered `/entity/<slug>/` page;
- the empty state and the page announcement are text, not a colour or an icon;
- selecting a row through the table updates the same inspector as a canvas click, so no
  information is canvas-only.

`tests/a11y.test.tsx` renders the table with `react-dom/server` and asserts the caption,
header scopes, labels, accessible action names, the profile-link publication rule and the
empty state. This is the automated acceptance for the component; a manual screenreader
pass (NVDA/VoiceOver) and colour-contrast measurement are still operator acceptance
items, so the repository claims WCAG 2.1 AA *conformance work* rather than a certified
audit. The German UI strings are asserted in the same test file.

## Validation and measured scope

```sh
npm ci
npm run test:dashboard
npm run test:seo
npm run test:a11y
npm run test:scaffold
npm run ci:pages
npm run test:export       # after npm run build: validates apps/web/out itself
npm run ci:worker
```

The Pages native build gate runs the dashboard, SEO and accessibility tests in addition
to the existing console checks, TypeScript checking and the static export, then
`test:export`. That post-build gate verifies that every root-relative asset in the
exported HTML exists, that the ForceAtlas2 bundle is still a classic (non-module)
worker chunk referenced by the dashboard, that no `_next/server` output leaked into
`out/`, and that `_headers` caches content-hashed assets immutably while revalidating
the HTML shells. It also walks every `out/entity/<slug>/index.html`, checks canonical
URLs, Open Graph/Twitter tags, JSON-LD shape and internal links, verifies the six
1200 × 630 OG cards, and (when `SITE_ORIGIN` was set) that every sitemap URL resolves to
an exported file. Tests cover validation, filtering, zero/invalid coordinates, evidence
directions/URLs, Cypher parameter separation, the full 120k-element fixture under LoD,
publication rules, JSON-LD escaping and the keyboard/table contract. The Python-only
GitHub workflow remains unchanged.

`npm run test:export` also documents the Cloudflare Pages contract:

```
apps/web/out/
  index.html, dashboard/index.html, 404.html, robots.txt, sitemap.xml
  entity/<slug>/index.html   -> one prerendered page per published entity
  og/<kind>.png              -> six static 1200x630 social cards
  _headers        -> immutable /_next/static/*, no-cache HTML, 7-day /og/*
  _next/static/   -> hashed JS/CSS and the classic ForceAtlas2 worker
```

Serve it exactly as Pages would with `npm run preview`
(`python3 -m http.server 8080 --directory apps/web/out --bind 0.0.0.0`).

Optional browser acceptance (serve the static output on a separate terminal):

```sh
npm install --no-save playwright
npx playwright install chromium
python3 -m http.server 8080 --bind 0.0.0.0 --directory apps/web/out
# Other terminal:
node tests/dashboard_browser.mjs
```

Browser automation is intentionally not downloaded for every native build. The script
also accepts DASHBOARD_URL, PLAYWRIGHT_MODULE, CHROMIUM_EXECUTABLE and CHROMIUM_ARGS.
It exercises the exported site, real WebGL2/worker updates, LoD zoom/drill, raw 30k/90k
rendering, opt-in Leaflet (tile requests stubbed), evidence/XSS-safe import, download,
invalid-import preservation, mobile width, and no-WebGL fallback. Real API/OSM uptime
is not part of the offline acceptance suite.

A local Chromium/SwiftShader run loaded and rendered the complete **30,000 nodes and
90,000 edges**, with LoD disabled, without uncaught browser errors. An illustrative
one-second sample produced around 44 animation callbacks, not a portable FPS guarantee.
LoD showed 24 overview groups then 96 at closer zoom for the same fixture. These are
synthetic fixtures on one environment, not a dense-real-world graph benchmark. CPU,
GPU/driver, memory, graph density and browser change results. Rendering import data is
client-side and creates no per-element hosting fee, but free-tier provider policies
and device limits still apply. No Cloudflare production deployment or 100k-node
hardware-performance guarantee is claimed.
