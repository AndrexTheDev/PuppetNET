# PuppetNET 2.0 — decoupled hybrid CI/CD

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

> **Aktueller Architekturstand (Zero-KV-Umstellung):** Der aktive Worker startet
> jetzt über `apps/worker/src/index.ts`. Er verarbeitet nur authentifizierte
> Rechercheaufträge und optionale öffentliche JSON-Weiterleitungen mit Cache API.
> Er hat keine KV-Bindings, Neo4j-Graph-API, Cron-Heartbeats oder Status-Snapshots.
> Dashboard, Legacy und Profile verwenden primär `graph_latest.json`. Ältere
> Graph-/KV-Beschreibungen unten gelten nur noch für die aufbewahrte historische
> Implementierung. Migration: [Zero-KV-Runbook](docs/zero-kv-worker.md).

Open-source OSINT ingestion, entity recognition and graph analysis.
Aktuelle Entwicklung: jeweils auf der laufenden Session-/Feature-Branch; Codefreigabe über Pull Request nach
`main`. Der auf `main` aktivierte tägliche Ingestion-Workflow publiziert neue
Daten anschließend direkt auf `main` mit `GH_PAT`, ohne zusätzliche Daten-PRs.

## Release & Zero-Budget-Betrieb

Aktueller Drei-Phasen-Vertrag und Betriebsgrenzen: [Ressourcenmodus](docs/resource-saving-mode.md).

- **Frontend:** Cloudflare Pages Git Integration, Repository-Root als Build-Root,
  Build `npm run ci:pages`, Ausgabeverzeichnis `apps/web/out`, Node.js 22.
- **SEO:** `SITE_ORIGIN=https://puppetnet-web.pages.dev` (oder eigene Domain).
  Auch direktes `next build` erzeugt Sitemap, Robots, OG-Bilder und
  `data/seo-metadata.json`; Entitäts-HTML enthält Canonicals, OG und Schema.org.
  Ungültige Origins brechen den Build ab, statt falsche URLs auszuliefern.
- **Design:** gemeinsame Tokens in `apps/web/src/styles/globals.css`, gespeist aus
  `palette.json` (auch für WebGL/Leaflet); der Build
  kopiert sie für Legacy nach `/styles/globals.css`. Keine externen Fonts/Ad-Skripte.
- **Betrieb:** Pages liefert statische JSONs. Der optionale authentifizierte
  Zero-KV-Worker stößt nur Recherche an. GitHub Actions verarbeitet lokal
  (Regex → spaCy), Cloud-Modell standardmäßig aus; standardmäßig höchstens drei
  reservierte Pipeline-Läufe pro UTC-Tag (zwei Cron-Läufe 03:00/15:00 UTC + ein
  manueller Slot), fünf Minuten Job-Limit. Opt-in-Public-Profil (nur bei per API
  bestätigtem öffentlichem Repo): 8 + 3 Läufe, 30 Dokumente, 20 Minuten. Gemini
  optional über einen Free-Tier-Budget-Broker:
  [Gemini-Strategie](docs/gemini-strategy.md). Daten werden nach der automatischen
  Freigabepolicy exportiert und durch einen Pages-Neubuild veröffentlicht.
- **Kostenkontrolle:** Free-Tier-orientiert, keine Nullkosten-Garantie. Provider-
  Kontingente und Abrechnung beobachten; Cloud-Fallback und optionale Dienste
  nur bewusst aktivieren. Operator-Token nie als `NEXT_PUBLIC_*` veröffentlichen.
- **Release-Checkliste:** [docs/release-checklist.md](docs/release-checklist.md).
- **Go-live (Betreiberschritte in Reihenfolge):** [docs/go-live.md](docs/go-live.md).
  Architektur und Einrichtung: [Zero-KV](docs/zero-kv-worker.md),
  [Ingestion](docs/daily-ingestion.md),
  [Verbesserungs-Roadmap](docs/improvement-roadmap.md).

### Aktuelle Secrets und harte Grenzen

- GitHub Actions: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`; `GEMINI_API_KEY`
  nur bei bewusst aktiviertem Cloud-Fallback. Keine alten Secret-Aliasse.
- Worker: `GH_PAT` **separat als Worker-Secret hinterlegen** (GitHub-Actions-Secrets
  werden nicht automatisch nach Cloudflare übertragen). Dazu ein unabhängiges
  `SEARCH_AUTH_TOKEN` (>=32 zufällige Zeichen) für berechtigte Betreiber.
  Keine alten Secret-Aliasse; niemals `GH_PAT` im Browser nutzen.
- 5 Minuten Job-Limit, 90 Sekunden Ingestion intern / 105 Sekunden Prozesslimit,
  45 Sekunden Export (Standardprofil; Public-Profil: 20 min / 600 s / 660 s / 120 s).
  Kalter Modell-Download kann trotzdem zum Timeout führen.
- Keine NLP-/Modell-Inferenz im Browser. Parsing, Filter und Rendering bleiben
  Browserarbeit; **0 % CPU** für eine interaktive Website ist nicht möglich.
  Tabellenmodus und ausgeschaltete Physik reduzieren die Standardlast. Legacy
  nutzt für neue Einstellungen ein nicht animiertes Grid statt Force-Layout.
- **95 % lokale NER** ist kein gemessener Qualitätswert. NER läuft vollständig
  lokal; nur ausgewählte unklare Relationen dürfen optional in die Cloud.
- **Actions-Minuten/Monat:** Das Repository ist öffentlich (per API geprüft am
  21.10.2026). Standard-GitHub-Runner sind für öffentliche Repos kostenlos und
  minutenmäßig unbegrenzt; die frühere Rechnung „bis zu 310 Minuten/Monat“ gilt
  nur noch, falls das Repo privat wird. Es bleiben harte Grenzen (6 h/Job, Cron
  unzuverlässig, Deaktivierung nach 60 Tagen Inaktivität, Fair-Use-Klausel) und
  maximal 3 Reservierungen/UTC-Tag im Standard- bzw. 11 im Public-Profil
  (harte Obergrenze 12 + 4). Echte Engpässe: Gemini-RPD, Cloudflare-Pages-Builds
  (500/Monat), Neo4j-Aura-Kapazität.
  Bei aktivierter neuer Pipeline werden redundante tägliche Legacy-Tests und
  Keep-alive-Jobs übersprungen. Geplante Ausführungen sind nicht minutengenau.
- Statische Pages-Dateien bleiben ohne laufende Pipeline abrufbar, jedoch ohne
  24/7-SLA. Abrechnung/Free-Tier-Limits beim jeweiligen Anbieter überwachen.

Datencommits des produktiven Workflows erfolgen direkt nach `main` mit dem
Repository-Secret `GH_PAT`. Checkout und alle Quota-/Daten-Pushes verwenden dieses
Token. Kein Force-Push und keine Umgehung von Branch-Schutzregeln. Cloudflare Pages
muss `main` als Produktionsbranch beobachten und `apps/web/*` im Build-Watch haben.

**Auto-Approval:** Confidence >=0.75 ODER exakte, konfigurierte RSS-Provenienz in
`VERIFIED_RSS_FEEDS` genügt für `public_released: true` im Export. `private`,
`noindex`, `release_blocked`, Negation, Hedging und berechnete Verdachtsbeziehungen
bleiben ausgeschlossen; `public_released: false` ist eine explizite Rücknahme.
Ein hoher Extraktionsscore beweist nicht die Wahrheit einer Aussage. Wer diese
Policy aktiviert, akzeptiert automatische öffentliche Publikation, inklusive
möglicher Extraktionsfehler. Verifizierte Feeds nur bewusst einzeln zulassen.


---

<a id="english"></a>

# PuppetNET 2.0 — decoupled hybrid CI/CD (English)

*(Translation of the German sections above; the sections from the component
table onward are written in English already.)*

> **Current architecture state (Zero-KV migration):** The active worker now
> boots via `apps/worker/src/index.ts`. It only processes authenticated research
> orders and optional public JSON redirects with the Cache API. It has no KV
> bindings, no Neo4j graph API, no cron heartbeats and no status snapshots.
> Dashboard, legacy and profiles primarily use `graph_latest.json`. Older
> graph/KV descriptions below only apply to the retained historical
> implementation. Migration: [Zero-KV runbook](docs/zero-kv-worker.md).

Open-source OSINT ingestion, entity recognition and graph analysis.
Current development happens on the active session/feature branch; code is
released via pull request to `main`. The daily ingestion workflow enabled on
`main` subsequently publishes new data directly to `main` with `GH_PAT`, without
additional data PRs.

## Release & zero-budget operation

Current three-phase contract and operational limits:
[resource-saving mode](docs/resource-saving-mode.md).

- **Frontend:** Cloudflare Pages Git integration, repository root as build root,
  build `npm run ci:pages`, output directory `apps/web/out`, Node.js 22.
- **SEO:** `SITE_ORIGIN=https://puppetnet-web.pages.dev` (or your own domain).
  A direct `next build` also produces the sitemap, robots, OG images and
  `data/seo-metadata.json`; entity HTML contains canonicals, OG and Schema.org.
  Invalid origins abort the build instead of shipping wrong URLs.
- **Design:** shared tokens in `apps/web/src/styles/globals.css`, fed from
  `palette.json` (also for WebGL/Leaflet); the build copies them to
  `/styles/globals.css` for legacy. No external fonts/ad scripts.
- **Operation:** Pages serves static JSON. The optional authenticated Zero-KV
  worker only triggers research. GitHub Actions processes locally
  (regex → spaCy), cloud model off by default; at most three reserved pipeline
  runs per UTC day (two cron runs 03:00/15:00 UTC + one manual slot),
  five-minute job limit. Data is exported per the automatic release policy and
  published by a Pages rebuild.
- **Cost control:** free-tier oriented, no zero-cost guarantee. Monitor provider
  quotas and billing; activate cloud fallback and optional services only
  deliberately. Never publish operator tokens as `NEXT_PUBLIC_*`.
- **Release checklist:** [docs/release-checklist.md](docs/release-checklist.md).
- **Go-live (operator steps in order):** [docs/go-live.md](docs/go-live.md#english).
  Architecture and setup: [Zero-KV](docs/zero-kv-worker.md),
  [ingestion](docs/daily-ingestion.md),
  [improvement roadmap](docs/improvement-roadmap.md).

### Current secrets and hard limits

- GitHub Actions: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`; `GEMINI_API_KEY`
  only with the deliberately enabled cloud fallback. No legacy secret aliases.
- Worker: store `GH_PAT` **separately as a Worker secret** (GitHub Actions
  secrets are not automatically transferred to Cloudflare). Plus an independent
  `SEARCH_AUTH_TOKEN` (>=32 random characters) for authorized operators.
  No legacy secret aliases; never use `GH_PAT` in the browser.
- 5-minute job limit, 90 seconds ingestion internally / 105 seconds process
  limit, 45 seconds export (default profile; opt-in public profile for a verified
  public repository: 20 min / 600 s / 660 s / 120 s, 8 + 3 runs per UTC day; see
  [Gemini strategy](docs/gemini-strategy.md)). A cold model download can still cause a timeout.
- No NLP/model inference in the browser. Parsing, filters and rendering remain
  browser work; **0 % CPU** for an interactive website is not possible. Table
  mode and disabled physics reduce the default load. Legacy uses a
  non-animated grid instead of force layout for new settings.
- **95 % local NER** is not a measured quality value. NER runs fully locally;
  only selected ambiguous relations may optionally go to the cloud.
- **Actions minutes/month:** two cron runs/day at up to 5 minutes each mean up
  to 310 minutes/month for the daily pipeline alone (62 runs); manual runs
  (max 1/UTC day) and CI add to that. The old ~100 minutes/month planning target
  no longer holds with the second daily run. At most 3 reservations/UTC day
  apply. When the new pipeline is enabled, redundant daily legacy tests and
  keep-alive jobs are skipped. Scheduled runs are not minute-accurate.
- Static Pages files remain retrievable without a running pipeline, but without
  a 24/7 SLA. Monitor billing/free-tier limits at each provider.

Data commits of the production workflow go directly to `main` with the
repository secret `GH_PAT`. Checkout and all quota/data pushes use this token.
No force-push and no bypassing of branch protection rules. Cloudflare Pages must
watch `main` as the production branch and include `apps/web/*` in the build watch.

**Auto-approval:** confidence >=0.75 OR exact configured RSS provenance in
`VERIFIED_RSS_FEEDS` suffices for `public_released: true` in the export.
`private`, `noindex`, `release_blocked`, negation, hedging and calculated
suspicion relationships remain excluded; `public_released: false` is an explicit
withdrawal. A high extraction score does not prove the truth of a statement.
Whoever activates this policy accepts automatic public publication, including
possible extraction errors. Admit verified feeds deliberately, one by one.

| Component | Source | Build / execution owner |
| --- | --- | --- |
| Next.js 15 static frontend | `apps/web` | Cloudflare Pages Git Integration |
| Authenticated search relay (Zero-KV) | `apps/worker` | Cloudflare Workers Builds |
| Python tests, local spaCy NLP, optional Neo4j ingestion | `packages/pipeline` | GitHub Actions standard Linux runner |

The new `/dashboard/` frontend uses Sigma.js 4, Graphology, an off-thread ForceAtlas2
layout and opt-in Leaflet/OpenStreetMap. The dashboard opens in the table view;
WebGL is loaded only when selected and physics is never started automatically. The previous analyst
console is archived (git history; last shipped in the pre-2.1 tree): `/legacy` is a permanent
redirect to `/dashboard/`. See [dashboard guide](docs/webgl-dashboard.md). `output: 'export'` generates
`apps/web/out` during `next build` (Next 15 does not use the old `next export` command).
There are no Pages Functions, SSR routes or hosted model APIs.

## Local development / verification

```sh
npm ci
npm run dev
npm run ci:pages                  # console regression gate + static Next build
npm run ci:worker                 # Worker regression gate, no deployment
npm run provision:worker -- --dry-run   # Cloudflare Zero-KV/secrets/deploy plan (no account calls)
npm run audit                     # static, offline repository consistency ledger
npm run emit:entities -- --input graph-export.json   # publish profile data (or --input -)
SITE_ORIGIN=https://<your-pages-origin> npm run emit:seo   # sitemaps + Sitemap: lines
npm run test:scaffold             # monorepo/build configuration contracts
npm run test:dashboard            # filters, LoD, evidence, 120k-element fixture
npm run test:seo                  # publication rules, JSON-LD, metadata, sitemap
npm run test:a11y                 # keyboard model, live region, table semantics
npm run test:export               # after build: assets, profiles, OG cards, Pages headers
npm run deploy -w @puppetnet/worker -- --dry-run --outdir /tmp/puppetnet-worker
python -m venv .venv
. .venv/bin/activate
pip install -e './packages/pipeline[dev]'
python -m pytest -q -m 'not integration'
ruff check packages/pipeline tests
```

GLiNER CPU inference is an optional install (`packages/pipeline[gliner]`), selected
with `NLP_BACKEND=gliner`. `urchade/gliner_multi-v2.1` (Apache-2.0, 209M params) is the
multilingual checkpoint used by the ingestion workflow; `packages/pipeline[onnx]` is the
ONNX Runtime alternative for a checkpoint that ships `model.onnx`. Tests use offline model
doubles; production ingestion loads real model weights. spaCy remains the default for
backwards-compatible local runs.

The v1.6 extraction pipeline adds sentence-aligned 384-token GLiNER chunking and optional
Cloudflare Workers AI triple extraction within the free 10,000-neuron/day tier
(`TRIPLET_BACKEND=cloudflare`). See [the NLP pipeline guide](docs/nlp-pipeline.md#multilingual-gliner--workers-ai-v16)
for labels, cost arithmetic, graph confidence and the opt-in live tests.
The v1.7 graph layer adds APOC entity resolution (`entity_resolution.py`), an exact
ownership-cycle detector and a Cypher `PUPPET_MASTER_OF` blend of PageRank, betweenness and
in-degree (`graph_analytics.py --puppet-master --cycles`). New entity nodes stop at 90 % of
the free-tier ceiling (`AURA_NODE_STOP=180000`). AuraDB pauses after 72 hours without a
*write*, so the Worker's cron writes a single `:_PuppetNetHeartbeat` node every 12 hours;
the daily `RETURN 1` in `keep_alive.py` stays as the end-to-end credential check. See
[operations](docs/operations.md#the-72-hour-pause) and
[edge hardening](docs/worker-hardening.md).
The frontend adds one statically prerendered profile page per published entity
(`/entity/<slug>/`) with Schema.org JSON-LD, static Open Graph cards, a generated
`sitemap.xml`, a fully keyboard-operable graph and a real table alternative to the WebGL
canvas. Still a zero-cost `output: 'export'` deployment: no SSR, no Pages Functions. See
[entity pages](docs/entity-pages.md) and the
[dashboard guide](docs/webgl-dashboard.md#keyboard-navigation-wcag-21-aa).
The v1.7 edge layer is hardened for the free tier and maintained by cron: the graph
API is capped at **25 database requests/minute** (per caller and fleet-wide, clamped in
code), every outbound fetch is SSRF-checked on **each redirect hop** with credentials
stripped across origins, and `[triggers]` in `wrangler.toml` schedules the AuraDB
keep-alive (`0 */12 * * *`) and a free-tier snapshot (`23 */3 * * *`). The snapshot feeds a
server-rendered `/status` dashboard — no script, token-protected, `n/a` instead of invented
numbers — showing Workers requests today, AuraDB nodes/relationships against the 200 000 /
400 000 ceilings, the live graph budget and the last keep-alive. Distributed locks are KV
leases, so no paid Durable Object or Queue is involved. The 25/min window is keyed by
`hash(IP + token)` for authenticated analysts (a shared office NAT no longer shares one
budget) and by the hashed IP for anonymous traffic; a degraded snapshot sends a throttled,
free Telegram alert. See [worker-hardening.md](docs/worker-hardening.md).
Root compatibility symlinks retain the previous Python/console/tooling paths without
source duplication; use a symlink-capable checkout.

## Deployment and operation

[Hybrid setup guide](docs/2.0-scaffold.md) contains the exact Pages and Workers Builds
settings, watch paths, secrets, production gates and GLiNER limitations.
[Worker provisioning](docs/worker-provisioning.md) is the KV/secret/deploy runbook, with
`npm run provision:worker` automating the whole sequence.
[48-hour monitoring checklist](docs/operations/monitoring-checklist.md) covers the first two
production days: KV write budget, AuraDB growth, snapshot age and escalation.
[`cloudflare-builds.json`](cloudflare-builds.json) records dashboard settings; it is
**not** an automatically applied Cloudflare configuration schema.

GitHub Actions has **no build/deploy JavaScript jobs and no artificial minute gate**.
Standard hosted runner execution in this public repository is free under GitHub's
public-repository policy; normal service, concurrency and artifact limits still apply.
Daily ingestion is disabled until `ENABLE_INGESTION=true` and database secrets exist.
Workers AI triplets stay off until `vars.TRIPLET_BACKEND=cloudflare` and the
`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN` secrets are present; the neuron ledger is
cached between runs and the stage is capped per run.

Older docs and workflows under `docs/legacy-*` describe the v1 architecture only.
The historical static audit is not a v2 certification. Current runtime regressions,
Python CI and the Cloudflare build gates are the active checks.

## Bounded German daily publication (opt-in)

The new local-first path uses `NLP_BACKEND=token_saver`, `de_core_news_sm` and an
optional, explicitly budgeted Gemini fallback. `graph_latest.json` is shared by
the dashboard, profiles and sitemap. See
[setup, review rules and deployment limitations](docs/daily-ingestion.md) before
enabling `ENABLE_DAILY_STATIC`. The new workflow has a five-minute timeout and
publishes changed data directly to production `main` using `GH_PAT`.
