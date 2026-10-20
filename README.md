# PuppetNET 2.0 — decoupled hybrid CI/CD

> **Aktueller Architekturstand (Zero-KV-Umstellung):** Der aktive Worker startet
> jetzt über `apps/worker/src/index.ts`. Er verarbeitet nur authentifizierte
> Rechercheaufträge und optionale öffentliche JSON-Weiterleitungen mit Cache API.
> Er hat keine KV-Bindings, Neo4j-Graph-API, Cron-Heartbeats oder Status-Snapshots.
> Dashboard, Legacy und Profile verwenden primär `graph_latest.json`. Ältere
> Graph-/KV-Beschreibungen unten gelten nur noch für die aufbewahrte historische
> Implementierung. Migration: [Zero-KV-Runbook](docs/zero-kv-worker.md).

Open-source OSINT ingestion, entity recognition and graph analysis.
Aktuelle Entwicklung: `arena/5fb0fe0b-puppetnet`; Codefreigabe über Pull Request nach
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
  (Regex → spaCy), Cloud-Modell standardmäßig aus; höchstens zwei reservierte
  Pipeline-Läufe pro UTC-Tag, fünf Minuten Job-Limit. Daten werden nach der automatischen
  Freigabepolicy exportiert und durch einen Pages-Neubuild veröffentlicht.
- **Kostenkontrolle:** Free-Tier-orientiert, keine Nullkosten-Garantie. Provider-
  Kontingente und Abrechnung beobachten; Cloud-Fallback und optionale Dienste
  nur bewusst aktivieren. Operator-Token nie als `NEXT_PUBLIC_*` veröffentlichen.
- **Release-Checkliste:** [docs/release-checklist.md](docs/release-checklist.md).
  Architektur und Einrichtung: [Zero-KV](docs/zero-kv-worker.md),
  [Ingestion](docs/daily-ingestion.md).

### Aktuelle Secrets und harte Grenzen

- GitHub Actions: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`; `GEMINI_API_KEY`
  nur bei bewusst aktiviertem Cloud-Fallback. Keine alten Secret-Aliasse.
- Worker: `GH_PAT` **separat als Worker-Secret hinterlegen** (GitHub-Actions-Secrets
  werden nicht automatisch nach Cloudflare übertragen). Dazu ein unabhängiges
  `SEARCH_AUTH_TOKEN` (>=32 zufällige Zeichen) für berechtigte Betreiber.
  Keine alten Secret-Aliasse; niemals `GH_PAT` im Browser nutzen.
- 5 Minuten Job-Limit, 90 Sekunden Ingestion intern / 105 Sekunden Prozesslimit,
  45 Sekunden Export. Kalter Modell-Download kann trotzdem zum Timeout führen.
- Keine NLP-/Modell-Inferenz im Browser. Parsing, Filter und Rendering bleiben
  Browserarbeit; **0 % CPU** für eine interaktive Website ist nicht möglich.
  Tabellenmodus und ausgeschaltete Physik reduzieren die Standardlast. Legacy
  nutzt für neue Einstellungen ein nicht animiertes Grid statt Force-Layout.
- **95 % lokale NER** ist kein gemessener Qualitätswert. NER läuft vollständig
  lokal; nur ausgewählte unklare Relationen dürfen optional in die Cloud.
- **~100 Actions-Minuten/Monat** ist ein Planungsziel, kein hartes Monatsbudget.
  31 tägliche Jobs à maximal 5 Minuten können bereits 155 Minuten benötigen;
  manuelle Jobs und CI kommen hinzu. Es gelten maximal 2 Reservierungen/UTC-Tag.
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

| Component | Source | Build / execution owner |
| --- | --- | --- |
| Next.js 15 static frontend | `apps/web` | Cloudflare Pages Git Integration |
| Authenticated search relay (Zero-KV) | `apps/worker` | Cloudflare Workers Builds |
| Python tests, local spaCy NLP, optional Neo4j ingestion | `packages/pipeline` | GitHub Actions standard Linux runner |

The new `/dashboard/` frontend uses Sigma.js 4, Graphology, an off-thread ForceAtlas2
layout and opt-in Leaflet/OpenStreetMap. The dashboard opens in the table view;
WebGL is loaded only when selected and physics is never started automatically. The previous analyst console is retained at
`/legacy/index.html`. See [dashboard guide](docs/webgl-dashboard.md). `output: 'export'` generates
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
the dashboard, legacy console, profiles and sitemap. See
[setup, review rules and deployment limitations](docs/daily-ingestion.md) before
enabling `ENABLE_DAILY_STATIC`. The new workflow has a five-minute timeout and
publishes changed data directly to production `main` using `GH_PAT`.
