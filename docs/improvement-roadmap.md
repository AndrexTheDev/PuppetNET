# Verbesserungs-Roadmap — Best-in-Class-OSINT ohne zusätzliches Actions-Budget

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

Ziel: PuppetNET soll die beste OSINT-Netzwerk-App ihrer Nische werden — **ohne eine
zusätzliche GitHub-Actions-Minute**. Budget bleibt fix: 2 Cron-Läufe/UTC-Tag
(03:00, 15:00) + ≤1 manueller Lauf, je ≤5 Minuten (Deckel ≈310 min/Monat).
Grundlage: Vollaudit der Anwendung + Nischen-Benchmark (Stand 2026-10-20).

## 1. Ausgangslage (Audit)

**Bereits auf Nischen-Standard:** Provenienz pro Kante (`evidenceIds`, `feed_urls`),
Entity-Resolution (`canonical_key`), Confidence-Scoring + Privacy-Vetos, stabile
Revision-Snapshots, WebGL-Visualisierung mit LoD-Gruppierung (Sigma + ForceAtlas2 im
Web Worker), Karten-/Tabellenansicht, Inspector mit Cypher-Export, Zero-KV-Relay
(Cache API), DSGVO-Widerrufskette (`public_released:false` → Manifest-Pruning).

**Benchmark.** Die Referenzklasse (Maltego, Linkurious, Social Links, ShadowDragon,
SpiderFoot; OSINT-Netzwerkanalyse-Methodik) definiert sich über:

| Fähigkeit | Standard in der Nische | PuppetNET vor dieser Roadmap |
| --- | --- | --- |
| Zentralität (Degree, Betweenness, PageRank) | Pflicht (Linkurious: „centrality metrics direkt im View") | nur Degree im Client |
| Community-Detection (Louvain/LPA) | Pflicht („welche Cluster arbeiten zusammen") | fehlte (Feld `cluster` meist `unassigned`) |
| Pfadanalyse (kürzester Pfad A→B, Multi-Hop) | **Maltegos Killer-Feature** | fehlte vollständig |
| Temporale Netzwerkanalyse | Pflicht („wann existierte die Beziehung") | Rohdaten (Evidence-Datum) vorhanden, unaggregiert |
| Shared-Attribute-Analyse | wichtig (synthetische Identitäten) | teilweise (Identifikatoren auf Profilen) |
| Korrelations-Regeln (SpiderFoot: 37 YAML-Regeln) | verbreitet | fehlte |
| Provenienz + Confidence pro Kante | Pflicht („ohne Quelle kollabiert die Investigation") | **vorhanden — Stärke** |
| Snapshot/Versionierung | Pflicht („was wussten wir im März") | vorhanden (Revision-Hash) |

**Lücken:** analytische Tiefe (Zentralität/Communities/Pfade/Zeit), deren UI, und
Automatisierung pro Dokument (Qualität statt Quantität — das 10-Dokumente/Lauf-Limit
bleibt).

## 2. Rechenorts-Prinzip (Kern der Strategie)

Jede Verbesserung wird dorthin gelegt, wo sie **keine Actions-Minuten** kostet:

| Schicht | Free-Budget (verifiziert 2026-10) | Was dort läuft |
| --- | --- | --- |
| Browser | unbegrenzt (Client des Nutzers) | interaktive Analyse auf gefilterten Views: Betweenness, Pathfinder-BFS, Louvain, Ego-Netze, Fuzzy-Suche, CSV-Export |
| Pages-Build | 500 Builds/Monat, statische Auslieferung **unbegrenzt**, 20 000 Dateien/Site, 25 MiB/Datei | Entitätsseiten, SEO, JSON-LD, angereicherte Profile (aktuell ~62 Daten-Pushes/Monat + PR-Builds → viel Luft) |
| Worker | 100 000 Requests/Tag (geteilt), **10 ms CPU/Aufruf** | Cache-Relay + Dispatch; keine schwere Logik |
| GitHub Actions | fix: 2 Cron + 1 manuell/Tag, ≤5 min | begrenzte Ingestion + Export. **Export-Zeit-Analytics laufen im bestehenden ≤45-s-Export-Schritt — kein neuer Job, keine Extra-Minute** |

Guardrails: keine neuen Workflow-Dateien (`test_hybrid_ci.py` pinnt das Fileset),
5-Minuten-Job-Limit, 20 000-Dateien-Site-Limit (5 000 Entitäts-Slices + Seiten passen),
10-ms-Worker-CPU, Determinismus-Pflicht für alles, was in den Revision-Hash eingeht.

## 3. Wellen (systematisch, schrittweise)

### Welle 1 — Export-Zeit-Analytics ✅ (umgesetzt am 2026-10-20)

`enrich_with_analytics()` in `export_static_slices.py` reichert jede Veröffentlichung an:

- pro Knoten: `degree`, `rank` (PageRank, 20 gedämpfte Iterationen), `community`
  (synchrone Label-Propagation, ≤8 Pässe, sortierte Iteration, Kleinstes-Label-Tie-Break
  → deterministisch), `firstSeen`/`lastSeen` (ISO-Daten aus veröffentlichter Evidenz);
- top-level `analytics` (Version 1): `communities`, `largestCommunity`, `density`,
  `hubs` (Top 10 nach Rank), `temporalRange`;
- nur Allowlist-Eingaben → kein Privatdaten-Leck; Kosten <1 s bei 5 000 Knoten/20 000 Kanten;
- Revision-Hash bleibt stabil (Determinismus), 3 neue Tests (Suite: 1293).

### Welle 2 — Analyse-UI im Dashboard (Browser = 0 Actions-Minuten) — ✅ umgesetzt (inkl. 2b)

**Umgesetzt am 20.10.2026:** Pathfinder, Rank/Degree-Anzeige (Inspector, Tabelle,
Top-Hub-Liste), Community-Filter + Netzwerk-Farbmodus, Zeitraum-Filter,
Fuzzy-Suche, teilbare Permalinks (URL-State), CSV/JSON-Export der Ansicht.
**Welle 2b (ebenfalls 20.10.2026):** Betweenness-Broker-Analyse (On-Demand im
Worker, ≤1 200 Knoten im Ausschnitt, Top-10-Liste mit Betweenness-Anteil) und
Louvain-Nachschärfung der Communities (`randomWalk:false` → deterministisch,
≤20 000 geladene Knoten; ersetzt die Export-Communities in Filter/Farbmodus,
bis zurückgesetzt wird). Dependencies: `graphology-metrics` (Subpath-Import
`centrality/betweenness`) und `graphology-communities-louvain`. Oberhalb der
Grenzen erklärt die UI das Limit ehrlich, statt den Worker einzufrieren.

1. **Pathfinder** (höchster Nutzwert, Maltego-Kern): zwei Entitäten wählen → BFS im
   `graph.worker` auf dem gefilterten View (Hop-Limit ≤6) → Pfad als Kanten-Highlight +
   Evidenz pro Hop im Inspector. Unverbunden → „kein Pfad im veröffentlichten Ausschnitt"
   (ehrliche Auskunft, kein Raten).
2. **Zentralitäts-Anzeige**: `rank`/`degree` in Inspector + Tabelle; Hub-Seitenleiste aus
   `analytics.hubs`; Betweenness (Broker-Knoten) via `graphology-metrics` auf dem
   gefilterten View im Worker — freie Rechenzeit, View-größen-begrenzt.
3. **Communities**: `community` → Farbmodus „Community", Filter pro Community,
   Louvain-Nachschärfung auf dem View (`graphology-communities-louvain`, ~50 ms/1 000
   Knoten, `randomWalk:false` für Determinismus).
4. **Timeline**: Zeitregler filtert Knoten/Kanten über `firstSeen`/`lastSeen` +
   Evidence-Daten; Entitäts-Timeline im Inspector („wann tauchte was auf").
5. **Such-UX**: Fuzzy-Scoring + Typeahead, Typ-Filter, Tastaturkürzel, URL-State
   (`mode`/`focus`/`filter`/`q`) → teilbare Permalinks für Investigationen.
6. **CSV/JSON-Export des gefilterten Views** (clientseitig, Blob) — Berichtsfähigkeit
   ohne Server.
   Teststrategie: `dashboard.test.ts` (Logik), `site_browser.mjs`/`dashboard_browser.mjs`
   (Abnahme), keine neuen Produkt-Dependencies außer graphology-Standardbibliothek.

### Welle 3 — Entitätsseiten (Build-Zeit = 0 Actions-Minuten) — ✅ umgesetzt

**Umgesetzt am 20.10.2026:** Profile sind jetzt eigenständige kleine Link-Analysen,
rein statisch (Build-Zeit, kein JS zur Laufzeit, 0 Actions-Minuten):
- **Struktur-Fakten** in der Infobox: Degree, PageRank, Community-Label und — sofern
  datiert — erste/letzte Erwähnung. Nur gültige Export-Analytics werden gezeigt;
  ungültige Werte fallen ganz weg (nie „schön gerechnet"), fehlende bleiben leer.
- **Community-Kontext**: wie viele direkte Verbindungen dieselbe Community teilen.
- **Verbindungsliste mit Metriken**: jeder Nachbar zeigt Degree/Rank/Community.
- **Inline-Ego-Graph als deterministisches SVG** (kein Canvas, kein Skript):
  Entität in der Mitte, Nachbarn gleichmäßig im Kreis (Position = keine Bedeutung),
  ausgehend durchgezogen / eingehend gestrichelt, jeder Nachbar verlinkt auf sein
  Profil; `role="img"` + `<title>`/`<desc>` für Screenreader; Namen XML-escaped.
- **JSON-LD-Erweiterung** innerhalb der bestehenden `@graph`-Form (Website + Subject
  + Breadcrumb): `additionalProperty` für die Strukturmaße, `subjectOf` für Quellen,
  `knows` für Personen-Nachbarn (absolute `@id`-Referenzen).
Deutlicher Hinweis überall: Strukturmaße sind Lagehinweise, keine Bewertungen.

### Welle 4 — Ingestion-Qualität im bestehenden Budget (0 Zusatz-Minuten) — ✅ umgesetzt (2026-10-20)

1. **Novel-Document-Priorisierung**: Abgleich gegen die veröffentlichte Revision, bevor
   ein Dokument einen der 10 Slots pro Lauf verbraucht → mehr *neue* Informationen pro
   Lauf bei identischem Budget (20 Docs/Tag Obergrenze bleibt).
2. **Mehr Relation-Mustern/Gazetteer** (deutsches Handelsregister-Vokabular,
   Officer-/Rollenbegriffe, Beteiligungsformulierungen) → höhere Ausbeute pro Dokument.
3. **Feed-Gesundheitsbericht**: tote Feeds in der Laufzusammenfassung (Sekunden, keine
   neue Minute) — Operator sieht Quellenqualität sofort.
4. **Korrelations-Regeln** (SpiderFoot-Prinzip, aber bounded): deterministischer
   Regelsatz auf dem Export-Graphen (Nominee-Ketten, Hub-and-Spoke, Community-Brücken,
   Personenverflechtungen) → optionaler `insights`-Block; Regeln als Code, versioniert,
   keine LLM-Kosten.

**Geliefert (Welle 4)**: URL-Dedupe *vor* dem Artikel-Fetch (bekannte Storys verbrauchen
weder HTTP-Request noch Slot; umformulierte Reposts fliegen über den URL-Abgleich raus,
State additiv `urls`/`url_count` neben den bestehenden `hashes`); ~35 deutsche Verben,
~60 Rollen-Nomen und Copula-Erkennung („ist Geschäftsführerin der …", „ist Tochter von
…" — per Entitätstyp als Familie oder Konzern disambiguiert) im Token-Saver, dazu ~25
deutsche Verb- und ~17 Substantiv-Regeln im Relation-Mapper inkl. deutscher Hedges
(„angeblich") und Negationen („nicht", „bestreitet"); Feed-Gesundheits-Ledger
(`ok/empty/dead` pro Feed, bounded auf 100) mit Tabelle in der Laufzusammenfassung;
vier versionierte Korrelationsregeln im Export (`nominee-chain`, `hub-spoke`,
`community-bridge`, `shared-officer`, max. 8 pro Regel / 24 gesamt, jede mit
Kanten-Evidenz), sichtbar als klickbare „Korrelations-Insights" im Dashboard —
insight-freie Graphen exportieren byte-identisch weiter. 1312 Python-Tests,
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 5 — Vertrauen & Compliance (Differenzierung in der Nische) — ✅ umgesetzt (2026-10-20)

- Öffentliche **Widerrufs-/Löschseite** mit dokumentiertem Ablauf: Die technische Kette
  existiert bereits (`public_released:false` → nächster Export entfernt Slices via
  Manifest-Pruning) — sie wird öffentlich erklärbar (DSGVO Art. 17, Art. 21).
- Quellen-Policy-Hinweis EuGH C-37/20 + C-601/20 (wirtschaftliches Eigentum ist nicht
  mehr frei öffentlich) → betrifft Wikidata-/Register-Quellen der Ingestion.
- **Kanten-Provenienz in der UI**: Evidenz pro Kante sichtbar machen (Daten vorhanden) —
  „jede Kante trägt ihre Quelle" ist das Audit-Kriterium der Nische.

**Geliefert (Welle 5)**: öffentliche Rechtsseite `/legal/` (voll zweisprachig DE+EN,
im Sitemap, aus beiden Footern verlinkt) mit dem dokumentierten Lösch-/Widerspruchs-
ablauf (DSGVO Art. 17 & 21: `public_released:false` → `approved()`-Veto →
Manifest-Pruning im nächsten der zwei täglichen Läufe → Löschung regelmäßig ≤ 24 h,
inkl. EDPB-5/2019-Delisting-Hinweis und ehrlicher Dritt-Cache-Grenze) und der
Quellen-Policy nach EuGH C-37/20 + C-601/20 (kein Scraping zugangsbeschränkter
Register, nur frei zugängliche Quellen mit dokumentierter Gewichtung);
Operator-Vertiefung in `docs/source-policy.md`. Kanten-Provenienz im Workspace:
ein Klick auf eine Kante öffnet den Inspector mit genau den Dokumenten dieser
Verbindung (`clickEdge` → Worker-`edgeDetail` → `evidenceForEdge`), LoD-Gruppenkanten
erklären ehrlich ihre Grenze statt Quellen zu erfinden; der Dashboard-Footer verlinkt
die Rechtsseite. 0 zusätzliche Actions-Minuten (statische Seite + Browser-Code).

### Welle 6 — Reichweite (optional, Operator-Entscheidung)

UI-i18n Deutsch/Englisch (Build-Zeit, verdoppelt die Zielgruppe), PWA/Offline
(Service-Worker-Cache für `graph_latest.json` → Analyse ohne Netz). Beides ohne
Actions-Kosten, aber größerer Testaufwand → bewusst ans Ende priorisiert.

## 4. Budget-Zusammenfassung

| Welle | Ort | Zusätzliche Actions-Minuten | Status |
| --- | --- | --- | --- |
| 1 Export-Analytics | bestehender Export-Schritt | **0** | ✅ umgesetzt |
| 2 Analyse-UI | Browser/Web Worker | **0** | ✅ umgesetzt (inkl. 2b) |
| 3 Entitätsseiten | Pages-Build | **0** | ✅ umgesetzt |
| 4 Ingestion-Qualität | bestehende ≤5-min-Jobs | **0** | ✅ umgesetzt |
| 5 Compliance/Trust | statische Seiten + Doku | **0** | ✅ umgesetzt |
| 6 i18n/PWA | Pages-Build/Browser | **0** | optional |

Kein Schritt erzeugt neue Workflows, neue Cron-Jobs, längere Jobs oder bezahlte
Dienste. Die einzigen verbrauchsrelevanten Grenzen sind die bestehenden: 3
Reservierungen/UTC-Tag, 5-Minuten-Job-Limit, 6 Gemini-Calls/Tag (geteilt),
500 Pages-Builds/Monat, 100 000 Worker-Requests/Tag.

---

<a id="english"></a>

# Improvement roadmap — best-in-class OSINT without additional Actions budget

Goal: PuppetNET should become the best OSINT network app in its niche — **without a
single additional GitHub Actions minute**. The budget stays fixed: 2 cron runs/UTC day
(03:00, 15:00) + ≤1 manual run, each ≤5 minutes (ceiling ≈310 min/month).
Basis: full application audit + niche benchmark (as of 2026-10-20).

## 1. Baseline (audit)

**Already at niche standard:** per-edge provenance (`evidenceIds`, `feed_urls`), entity
resolution (`canonical_key`), confidence scoring + privacy vetoes, stable revision
snapshots, WebGL visualization with LoD grouping (Sigma + ForceAtlas2 in a web worker),
map/table views, inspector with Cypher export, zero-KV relay (Cache API), GDPR withdrawal
chain (`public_released:false` → manifest pruning).

**Benchmark.** The reference class (Maltego, Linkurious, Social Links, ShadowDragon,
SpiderFoot; OSINT network-analysis methodology) is defined by:

| Capability | Niche standard | PuppetNET before this roadmap |
| --- | --- | --- |
| Centrality (degree, betweenness, PageRank) | mandatory (Linkurious: centrality metrics in-view) | degree only, client-side |
| Community detection (Louvain/LPA) | mandatory ("which clusters work together") | missing (`cluster` mostly `unassigned`) |
| Path analysis (shortest path A→B, multi-hop) | **Maltego's killer feature** | completely missing |
| Temporal network analysis | mandatory ("when did the relationship exist") | raw evidence dates, unaggregated |
| Shared-attribute analysis | important (synthetic identities) | partial (identifiers on profiles) |
| Correlation rules (SpiderFoot: 37 YAML rules) | common | missing |
| Per-edge provenance + confidence | mandatory ("without a source the investigation collapses") | **present — a strength** |
| Snapshots/versioning | mandatory ("what did we know in March") | present (revision hash) |

**Gaps:** analytical depth (centrality/communities/paths/time), its UI, and per-document
automation (quality over quantity — the 10-documents/run limit stays).

## 2. Compute-location principle (core of the strategy)

Every improvement is placed where it costs **zero Actions minutes**:

| Layer | Free budget (verified 2026-10) | What runs there |
| --- | --- | --- |
| Browser | unlimited (the user's client) | interactive analysis on filtered views: betweenness, pathfinder BFS, Louvain, ego networks, fuzzy search, CSV export |
| Pages build | 500 builds/month, static delivery **unlimited**, 20,000 files/site, 25 MiB/file | entity pages, SEO, JSON-LD, enriched profiles (currently ~62 data pushes/month + PR builds → ample headroom) |
| Worker | 100,000 requests/day (shared), **10 ms CPU/invocation** | cache relay + dispatch; no heavy logic |
| GitHub Actions | fixed: 2 cron + 1 manual/day, ≤5 min | bounded ingestion + export. **Export-time analytics run inside the existing ≤45 s export step — no new job, no extra minute** |

Guardrails: no new workflow files (`test_hybrid_ci.py` pins the fileset), 5-minute job
limit, 20,000-file site limit (5,000 entity slices + pages fit), 10 ms worker CPU, and a
determinism requirement for everything that enters the revision hash.

## 3. Waves (systematic, step by step)

### Wave 1 — export-time analytics ✅ (implemented 2026-10-20)

`enrich_with_analytics()` in `export_static_slices.py` enriches every publication:

- per node: `degree`, `rank` (PageRank, 20 damped iterations), `community` (synchronous
  label propagation, ≤8 passes, sorted iteration, smallest-label tie-break →
  deterministic), `firstSeen`/`lastSeen` (ISO dates from published evidence);
- top-level `analytics` (version 1): `communities`, `largestCommunity`, `density`,
  `hubs` (top 10 by rank), `temporalRange`;
- allowlist inputs only → no private-data leak; cost <1 s at 5,000 nodes/20,000 edges;
- revision hash stays stable (determinism), 3 new tests (suite: 1293).

### Wave 2 — analysis UI in the dashboard (browser = 0 Actions minutes) — ✅ implemented (incl. 2b)

**Implemented 2026-10-20:** pathfinder, rank/degree display (inspector, table,
top-hub list), community filter + network color mode, time-window filter, fuzzy
search, shareable permalinks (URL state), CSV/JSON export of the view.
**Wave 2b (also 2026-10-20):** betweenness broker analysis (on demand in the
worker, ≤1,200 nodes in the visible slice, top-10 list with betweenness share)
and Louvain community refinement (`randomWalk:false` → deterministic, ≤20,000
loaded nodes; replaces the export communities in filter/color mode until reset).
Dependencies: `graphology-metrics` (subpath import `centrality/betweenness`) and
`graphology-communities-louvain`. Above the caps the UI honestly explains the
limit instead of freezing the worker.

1. **Pathfinder** (highest user value, Maltego's core): pick two entities → BFS in the
   `graph.worker` over the filtered view (hop limit ≤6) → path as edge highlight +
   per-hop evidence in the inspector. Unconnected → "no path in the published excerpt"
   (honest answer, no guessing).
2. **Centrality display**: `rank`/`degree` in inspector + table; hub sidebar from
   `analytics.hubs`; betweenness (broker nodes) via `graphology-metrics` on the filtered
   view in the worker — free compute, view-size bounded.
3. **Communities**: `community` → "community" color mode, per-community filter, Louvain
   refinement on the view (`graphology-communities-louvain`, ~50 ms/1,000 nodes,
   `randomWalk:false` for determinism).
4. **Timeline**: time slider filters nodes/edges via `firstSeen`/`lastSeen` + evidence
   dates; per-entity timeline in the inspector ("when did what surface").
5. **Search UX**: fuzzy scoring + typeahead, type filter, keyboard shortcuts, URL state
   (`mode`/`focus`/`filters`/`q`) → shareable permalinks for investigations.
6. **CSV/JSON export of the filtered view** (client-side, Blob) — report capability
   without a server.
   Test strategy: `dashboard.test.ts` (logic), `site_browser.mjs`/`dashboard_browser.mjs`
   (acceptance); no new product dependencies beyond the graphology standard library.

### Wave 3 — entity pages (build time = 0 Actions minutes) — ✅ implemented

**Implemented 2026-10-20:** profiles are now self-contained mini link analyses,
fully static (build time, no runtime JS, 0 Actions minutes):
- **Structural facts** in the info box: degree, PageRank, community label and — when
  dated — first/last mention. Only valid export analytics are shown; invalid values
  are dropped entirely (never clamped into looking real), missing ones stay absent.
- **Community context**: how many direct connections share the same community.
- **Connections list with metrics**: each neighbor shows degree/rank/community.
- **Inline ego graph as deterministic SVG** (no canvas, no script): the entity in the
  center, neighbors evenly on a circle (position carries no meaning), outgoing solid /
  incoming dashed, each neighbor links to its profile; `role="img"` + `<title>`/`<desc>`
  for screen readers; names XML-escaped.
- **JSON-LD enrichment** inside the existing `@graph` shape (website + subject +
  breadcrumb): `additionalProperty` for the structural metrics, `subjectOf` for sources,
  `knows` for person neighbors (absolute `@id` references).
Clear note everywhere: structural metrics are position hints, not verdicts.

### Wave 4 — ingestion quality inside the existing budget (0 extra minutes) — ✅ implemented (2026-10-20)

1. **Novel-document prioritization**: check against the published revision before a
   document consumes one of the 10 slots per run → more *new* information per run at an
   identical budget (the 20 docs/day ceiling stays).
2. **More relation patterns/gazetteers** (German commercial-register vocabulary,
   officer/role terms, shareholding phrasings) → higher yield per document.
3. **Feed-health report**: dead feeds in the run summary (seconds, not a new minute) —
   the operator sees source quality immediately.
4. **Correlation rules** (SpiderFoot principle, but bounded): deterministic rule set over
   the export graph (nominee chains, hub-and-spoke, community bridges, shared officers)
   → optional `insights` block; rules as code, versioned, no LLM cost.

**Shipped (Wave 4)**: URL dedupe *before* the article fetch (known stories burn neither
an HTTP request nor a slot; re-worded reposts are caught by the URL check; the state
file gains `urls`/`url_count` additively next to the pinned `hashes`); ~35 German verbs,
~60 role nouns and copula parsing ("ist Geschäftsführerin der …", "ist Tochter von …" —
disambiguated by entity type into family vs. corporate) in the token saver, plus ~25
German verb and ~17 noun rules in the relation mapper including German hedges
("angeblich") and negations ("nicht", "bestreitet"); a feed-health ledger
(`ok/empty/dead` per feed, bounded at 100) with a table in the run summary; four
versioned correlation rules in the export (`nominee-chain`, `hub-spoke`,
`community-bridge`, `shared-officer`, max 8 per rule / 24 overall, each citing edge
evidence), rendered as clickable "correlation insights" in the dashboard —
insight-free graphs keep exporting byte-identically. 1312 Python tests,
`ci:pages` green, 0 extra Actions minutes.

### Wave 5 — trust & compliance (differentiation in the niche) — ✅ implemented (2026-10-20)

- Public **withdrawal/erasure page** with a documented flow: the technical chain already
  exists (`public_released:false` → next export removes slices via manifest pruning) —
  it becomes publicly explainable (GDPR Art. 17, Art. 21).
- Source-policy note on CJEU C-37/20 + C-601/20 (beneficial ownership is no longer
  freely public) → affects the Wikidata/register sources of the ingestion.
- **Edge provenance in the UI**: make per-edge evidence visible (the data exists) —
  "every edge carries its source" is the niche's audit criterion.

**Shipped (Wave 5)**: a public legal page at `/legal/` (fully bilingual DE+EN, in the
sitemap, linked from both footers) documenting the erasure/objection workflow
(GDPR Art. 17 & 21: `public_released:false` → `approved()` veto → manifest pruning in
the next of the two daily runs → erasure normally effective within ≤ 24 h, including
the EDPB 5/2019 delisting note and the honest third-party-cache limitation) and the
source policy following CJEU C-37/20 + C-601/20 (no scraping of access-restricted
registers, freely accessible sources only, documented weighting ladder); operator
deep-dive in `docs/source-policy.md`. Edge provenance in the workspace: clicking an
edge opens the inspector with exactly the documents behind that one connection
(`clickEdge` → worker `edgeDetail` → `evidenceForEdge`); LoD group edges honestly
explain their limitation instead of inventing sources; the dashboard footer links the
legal page. 0 extra Actions minutes (static page + browser code).

### Wave 6 — reach (optional, operator decision)

UI i18n German/English (build time, doubles the audience), PWA/offline (service-worker
cache for `graph_latest.json` → analysis without network). Both cost zero Actions
minutes but carry a larger testing burden → deliberately prioritized last.

## 4. Budget summary

| Wave | Location | Additional Actions minutes | Status |
| --- | --- | --- | --- |
| 1 Export analytics | existing export step | **0** | ✅ implemented |
| 2 Analysis UI | browser/web worker | **0** | ✅ implemented (incl. 2b) |
| 3 Entity pages | Pages build | **0** | ✅ implemented |
| 4 Ingestion quality | existing ≤5-min jobs | **0** | ✅ implemented |
| 5 Compliance/trust | static pages + docs | **0** | ✅ implemented |
| 6 i18n/PWA | Pages build/browser | **0** | optional |

No step creates new workflows, new cron jobs, longer jobs or paid services. The only
consumption-relevant limits remain the existing ones: 3 reservations/UTC day, 5-minute
job limit, 6 Gemini calls/day (shared), 500 Pages builds/month, 100,000 worker
requests/day.
