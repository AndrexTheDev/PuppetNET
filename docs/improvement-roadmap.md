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

### Welle 6 — Reichweite (UI-i18n + PWA/Offline) — ✅ umgesetzt (2026-10-20)

- **UI-Internationalisierung DE/EN**: Der gesamte App-Chrome (Dashboard, Inspector,
  Tabelle, Boot-Zustände, Karte, Tastatur-Ansagen, Intro-/Anzeigen-Platzhalter) kommt
  aus einem byte-gepinnten Wörterbuch (`apps/web/lib/i18n-strings.ts`, ~200 Schlüssel);
  Sprachumschalter im Workspace-Header (localStorage `puppetnet-lang`), EN-Teilbaum mit
  `lang="en"`. Daten-Schicht-Meldungen (Standby-Gründe, Quellenlabels, Insights) bleiben
  bewusst Deutsch — sie sind Teil des Datenvertrags, keine UI-Chrome.
- **Englische Landing `/en/`**: vollständig statisch, hreflang-verlinkt (DE↔EN↔x-default),
  in allen drei Sitemap-Generatoren, mit Sprachlink im Footer; Entitätsdaten bleiben
  unübersetzt (Übersetzungen von Quelldaten werden niemals erfunden).
- **PWA/Offline**: Service-Worker (`sw.js`) mit stale-while-revalidate für `/data/*`
  (Workspace öffnet sofort, Datensatz wird im Hintergrund revalidiert — niemals werden
  Daten erfunden), cache-first für `/_next/static/*`, network-first für Dokumente mit
  App-Shell-Fallback (`/dashboard/`) und ehrlicher 503; Web-App-Manifest (installierbar,
  standalone), feature-geguardete Registrierung, `no-store` für den Worker selbst.

**Geliefert (Welle 6)**: Wörterbuch + Sprach-Provider (`app/dashboard/i18n.tsx`),
Umschalter im Workspace-Header, `/en/`-Landing, hreflang-Metadaten auf beiden Landings,
`sw.js`/`manifest.webmanifest`/`OfflineService`, `_headers`-Ergänzung, 7 neue i18n-Tests
(Schlüssel-/Platzhalter-Parität, gepinnte DE-Strings, EN-Render-Smoke, zweisprachiges
Tastaturmodell) + 2 neue Release-Pins (PWA-Sicherheit, EN-Publikation) — `ci:pages` grün,
0 zusätzliche Actions-Minuten (statischer Build + Browser-Code).

### Welle 7 — Untersuchungstiefe (Ego-Netze + Zeitachse) — ✅ umgesetzt (2026-10-20)

- **Ego-Netz-Expansion** (Maltegos Killer-Feature neben kürzesten Pfaden):
  deterministische BFS-Nachbarschaft (1–3 Hops, richtungsunabhängig) um eine Entität im
  gefilterten Ausschnitt — als eigenes Panel (Zentrum per Name/ID, fuzzy wie der
  Pfadfinder; LoD-Gruppen expandieren um ihr erstes Mitglied) und als Button
  „Umgebung aufspannen" im Inspector. Ergebnis als Status-Ansage; ein unbekanntes
  Zentrum wird ehrlich als „nicht gefunden" gemeldet, niemals erfunden.
  Zurücksetzen über „Gesamten Ausschnitt anzeigen".
- **Zeitliche Analytik**: Histogramm „Erwähnungen pro Monat" (erste Erwähnung pro
  Entität, UTC-Monatsbuckets, aufsteigend, höchstens die letzten 24 Monate, undatierte
  Entitäten fehlen ehrlich) als zugängliche Balkenliste im Analytik-Panel — bei jedem
  View-Build im Worker berechnet, Token-only-CSS.
- Beides läuft vollständig im Web Worker auf dem geladenen Ausschnitt — freie
  Browser-Rechenzeit, keine Cloud, keine neuen Daten.

**Geliefert (Welle 7)**: `egoNetwork()` + `mentionTimeline()` (reine, getestete
Funktionen in `graph-data.ts`), Protokoll-Request/Response `ego`, Worker-Zweig +
`View.timeline`, Ego-Panel + Timeline-UI (DE/EN über das Wörterbuch), optionales
`onEgo`-Prop im Inspector, 2 neue Datenlagen-Tests + 1 neuer Release-Pin —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 8 — Entitätsauflösung + interaktive Zeitachse — ✅ umgesetzt (2026-10-20)

- **Dubletten-Hinweise** (Entity Resolution, ein Nicht-Verhandelbares der
  OSINT-Methodik): Knoten, deren normalisierte Bezeichnung kollidiert, während die
  IDs verschieden sind — der klassische Dublette-Fall über Quellengrenzen. Bewusst
  konservativ (nur Groß-/Kleinschreibung und Leerraum falten, keine Phonetik, kein
  Fuzzy-Raten), Typ-Abweichungen werden sichtbar gemacht statt versteckt, LoD-Gruppen
  sind ausgenommen (Anzeige-Artefakte, keine Entitäten). „Vergleichen" wechselt nur
  die Tabellenansicht auf die Bezeichnung — die App entscheidet **nie** selbst über
  eine Zusammenführung.
- **Interaktive Zeitachse**: Jeder Monatsbalken ist ein Knopf und setzt über
  `monthWindow()` das exakte Kalenderfenster (echte Schaltjahre, ungültige Monate →
  niemals geraten) in die bestehenden Zeitfilter — „Zeitfilter löschen" stellt den
  vollen Ausschnitt wieder her. Statusansage für Screenreader.

**Geliefert (Welle 8)**: `duplicateCandidates()` + `monthWindow()` (reine, getestete
Funktionen), `View.duplicates`, Worker-Berechnung auf dem gefilterten Ausschnitt,
Dubletten-Block + klickbare Monate im Analytik-Panel (DE/EN), token-only CSS
(pin-konform, keine Hex-Werte), 2 neue Datenlagen-Tests + 1 neuer Release-Pin —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 9 — Struktur-Klarheit (Kantenbündelung + Verwundbarkeit) — ✅ umgesetzt (2026-10-20)

- **Kantenbündelung** (optional zuschaltbar): Parallele Beziehungen desselben
  Knotenpaars werden zu einer Kante zusammengefasst — mit **Typ-Liste**, **Anzahl**,
  **vereinigter Provenienz** und der **stärksten belegten Konfidenz**. Genau das, was
  Link-Charts bei mehreren Beziehungstypen zwischen zwei Entitäten unlesbar macht;
  nichts wird verworfen, die Bündelung ist jederzeit abschaltbar, Reihenfolge und
  Ausgabe sind deterministisch, und die Eingabe wird nie verändert.
- **Strukturelle Verwundbarkeit**: Artikulationspunkte (Knoten, deren Entfernung den
  Ausschnitt zerlegt), Schnittkanten (Brücken zwischen Teilnetzen) und der k-Kern
  (dichtester zusammenhängender Kern). Iteratives Tarjan + k-Core per Binärsuche —
  stapelsicher, deterministisch, auf 50.000 Knoten begrenzt; darüber liefert die
  Analyse ehrlich `null` statt einer Schätzung. Doppelte Kanten zählen als eine
  Beziehung, LoD-Gruppen sind ausgenommen.

**Geliefert (Welle 9)**: `bundleEdges()` + `structuralVulnerability()` (reine,
getestete Funktionen), `Filters.bundle`, `View.vulnerability`, Worker-Verdrahtung,
Bündelungs-Schalter + Verwundbarkeits-Panel im Analytik-Bereich (DE/EN, Sprungpunkte
direkt fokussierbar), token-only CSS, 2 neue Datenlagen-Tests (dashboard 27) +
1 neuer Release-Pin — `ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 10 — Zeitliche Plausibilität + Ausfall-Simulation — ✅ umgesetzt (2026-10-20)

- **„Pfad über Zeit"**: Jede Teilstrecke eines kürzesten Pfades erhält das
  **dokumentierte Zeitfenster** ihrer Beziehung (früheste bis späteste
  Beleg-Datumsangabe, auf den Tag normalisiert, mit vereinigter Provenienz). Liegt die
  späteste Beleglage einer Teilstrecke **vor** dem Beginn der vorherigen, wird sie als
  **Anachronismus** markiert — die Kette kann so nicht stattgefunden haben, ohne dass
  eine Erklärung fehlt. Undatierte Teilstrecken bleiben ehrlich leer und werden nie zum
  Urteil; eine Teilstrecke ohne belegte Beziehung liefert `null` statt einer Erfindung.
- **Ausfall-Simulation**: Jeder Artikulationspunkt aus Welle 9 trägt jetzt bei, in wie
  viele Teile der Ausschnitt bei seinem Wegfall zerfällt und wie groß der größte
  verbleibende Teil ist („zerfällt in 3 Teile · größter 42"). Das ist die operative
  Frage hinter der Strukturanalyse — nicht nur *ob* eine Entität kritisch ist, sondern
  *was* ihr Wegfall kostet.

**Geliefert (Welle 10)**: `pathTimeline()` (reine, getestete Funktion), `PathTimeline`
im Pfad-Protokoll + Worker-Berechnung auf dem sichtbaren Ausschnitt, `impact` je
Artikulationspunkt, Beleglagen-Block im Pfad-Panel (mit positiver Bestätigung bei
plausibler Kette) und Ausfall-Hinweis in der Verwundbarkeitsliste (DE/EN), token-only
CSS, 2 neue Datenlagen-Tests (dashboard 29) + 1 neuer Release-Pin (release 16) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 11 — Fallarbeit (Notizen + Fallbericht) — ✅ umgesetzt (2026-10-20)

- **Ermittlungsnotizen pro Entität**: Beobachtungen, Hypothesen und offene Fragen direkt
  im Inspector — gespeichert **nur im Browser** (localStorage, versionierter Schlüssel),
  nie an einen Server gesendet, hart auf 2.000 Zeichen begrenzt, beim Lesen aus dem
  Speicher erneut bereinigt (kaputte/leere/fremde Einträge werden verworfen statt
  vertraut). Ein Hinweis im Panel macht diese Zusage sichtbar; die Anzahl annotierter
  Entitäten steht im Filter-Panel.
- **Fallbericht (Markdown)**: ein deterministischer Bericht für die Fallakte —
  Ausschnitt, Notizen (alphabetisch, mehrzeilig, ohne leere Einträge), Analytik,
  Regelhinweise, strukturelle Befunde inklusive Ausfall-Simulation, Dubletten und
  monatliche Erwähnungen. Der Zeitstempel wird von außen übergeben (kein verstecktes
  `Date.now()`), der Bericht ist zweisprachig (DE/EN) und schließt mit der klaren
  Trennung: **„Hinweise, keine Beweise"**.

**Geliefert (Welle 11)**: `normalizeNote()` / `readNotes()` / `caseReport()` (reine,
getestete Funktionen), Notizblock im Inspector (DE/EN, Statusansagen, Zeichenzähler),
Fallbericht-Button im Export-Footer, token-only CSS, 2 neue Datenlagen-Tests
(dashboard 31) + 1 neuer Release-Pin (release 17) — `ci:pages` grün, 0 zusätzliche
Actions-Minuten.

### Welle 12 — Zeitliche Dynamik (Wiedergabe + Belegzeitraum) — ✅ umgesetzt (2026-10-20)

- **Kumulative Wiedergabe**: Das Netz wächst Monat für Monat bis zum Ende des jeweiligen
  Monats — Play/Pause plus Einzelschritt vor und zurück. Die Monatsliste wird beim Start
  **eingefroren**, weil ein Zeitfilter die sichtbare Zeitachse verkürzt; das Ende ist ein
  ehrlicher Stopp statt eines Rücksprungs. Jeder Schritt ist eine Statusansage für
  Screenreader; „Zeitfilter löschen" beendet die Ansicht.
- **Belegzeitraum pro Kante**: Der Kanten-Inspector zeigt jetzt das dokumentierte
  Zeitfenster der Beziehung (frühestes bis spätestes Beleg-Datum, auf den Tag
  normalisiert) inklusive Anzahl datierter Belege — und sagt offen „keine datierten
  Belege", wenn es keine gibt, statt ein Datum zu erfinden.

**Geliefert (Welle 12)**: `nextPlaybackMonth()` + `evidenceWindow()` (reine, getestete
Funktionen), Wiedergabe-Steuerung in der Zeitachse (DE/EN, `aria-pressed`,
Statusansagen), Belegzeitraum-Zeile im Kanten-Panel, token-only CSS, 2 neue
Datenlagen-Tests (dashboard 33) + 1 neuer Release-Pin (release 18) — `ci:pages` grün,
0 zusätzliche Actions-Minuten.

## 4. Budget-Zusammenfassung

| Welle | Ort | Zusätzliche Actions-Minuten | Status |
| --- | --- | --- | --- |
| 1 Export-Analytics | bestehender Export-Schritt | **0** | ✅ umgesetzt |
| 2 Analyse-UI | Browser/Web Worker | **0** | ✅ umgesetzt (inkl. 2b) |
| 3 Entitätsseiten | Pages-Build | **0** | ✅ umgesetzt |
| 4 Ingestion-Qualität | bestehende ≤5-min-Jobs | **0** | ✅ umgesetzt |
| 5 Compliance/Trust | statische Seiten + Doku | **0** | ✅ umgesetzt |
| 6 i18n/PWA | Pages-Build/Browser | **0** | ✅ umgesetzt |
| 7 Ego/Zeitachse | Browser/Web Worker | **0** | ✅ umgesetzt |
| 8 Entitätsauflösung/Zeitachse | Browser/Web Worker | **0** | ✅ umgesetzt |
| 9 Bündelung/Verwundbarkeit | Browser/Web Worker | **0** | ✅ umgesetzt |
| 10 Zeitplausibilität/Ausfall | Browser/Web Worker | **0** | ✅ umgesetzt |
| 11 Notizen/Fallbericht | Browser (localStorage) | **0** | ✅ umgesetzt |
| 12 Wiedergabe/Belegzeitraum | Browser | **0** | ✅ umgesetzt |

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

### Wave 6 — reach (UI i18n + PWA/offline) — ✅ implemented (2026-10-20)

- **UI internationalization DE/EN**: all app chrome (dashboard, inspector, table, boot
  states, map, keyboard announcements, intro/ad placeholders) comes from a byte-pinned
  dictionary (`apps/web/lib/i18n-strings.ts`, ~200 keys); language toggle in the
  workspace header (localStorage `puppetnet-lang`), EN subtree tagged `lang="en"`.
  Data-layer messages (standby reasons, source labels, insights) deliberately stay
  German — they are part of the data contract, not UI chrome.
- **English landing `/en/`**: fully static, hreflang-linked (DE↔EN↔x-default), listed in
  all three sitemap generators, with a language link in the footer; entity data stays
  untranslated (translations of source data are never fabricated).
- **PWA/offline**: service worker (`sw.js`) with stale-while-revalidate for `/data/*`
  (the workspace opens instantly while the dataset revalidates in the background — data
  is never fabricated), cache-first for `/_next/static/*`, network-first for documents
  with an app-shell fallback (`/dashboard/`) and an honest 503; web app manifest
  (installable, standalone), feature-guarded registration, `no-store` for the worker
  itself via `_headers`.

**Delivered (Wave 6)**: dictionary + language provider (`app/dashboard/i18n.tsx`),
toggle button in the workspace header, `/en/` landing, hreflang metadata on both
landings, `sw.js`/`manifest.webmanifest`/`OfflineService`, `_headers` addition, 7 new
i18n tests (key/placeholder parity, pinned German strings, EN render smoke, bilingual
keyboard model) + 2 new release pins (PWA safety, EN publication) — `ci:pages` green,
0 extra Actions minutes (static build + browser code).

### Wave 7 — investigation depth (ego networks + timeline) — ✅ implemented (2026-10-20)

- **Ego-network expansion** (Maltego's killer feature beside shortest paths):
  deterministic BFS neighborhood (1–3 hops, direction-agnostic) around an entity within
  the filtered slice — as its own panel (center by name/ID, fuzzy like the pathfinder;
  LoD groups expand around their first member) and as an "Expand neighborhood" button in
  the inspector. The result is announced as a status; an unknown center is honestly
  reported as "not found", never fabricated. Reset via "Show the full slice".
- **Temporal analytics**: "Mentions per month" histogram (first mention per entity, UTC
  month buckets, ascending, at most the last 24 months, undated entities honestly
  absent) as an accessible bar list inside the analytics panel — computed in the worker
  on every view build, token-only CSS.
- Both run entirely inside the web worker on the loaded slice — free browser compute,
  no cloud, no new data collection.

**Delivered (Wave 7)**: `egoNetwork()` + `mentionTimeline()` (pure, tested functions in
`graph-data.ts`), `ego` protocol request/response, worker branch + `View.timeline`,
ego panel + timeline UI (DE/EN via the dictionary), optional `onEgo` prop in the
inspector, 2 new data-layer tests + 1 new release pin — `ci:pages` green, 0 extra
Actions minutes.

### Wave 8 — entity resolution + interactive timeline — ✅ implemented (2026-10-20)

- **Duplicate hints** (entity resolution, a non-negotiable of OSINT methodology):
  nodes whose normalized label collides while their ids differ — the classic
  cross-source duplicate. Deliberately conservative (case and whitespace folding only,
  no phonetics, no fuzzy guessing); type mismatches are surfaced instead of hidden; LoD
  groups are excluded (display artifacts, not entities). "Compare" only switches the
  table view to the label — the app **never** merges anything on its own.
- **Interactive timeline**: every month bar is a button and applies the exact calendar
  window via `monthWindow()` (real leap years, invalid months → never guessed) to the
  existing time filters; "Clear time filter" restores the full slice. Status
  announcement for screen readers.

**Delivered (Wave 8)**: `duplicateCandidates()` + `monthWindow()` (pure, tested
functions), `View.duplicates`, worker computation on the filtered slice, duplicate
block + clickable months inside the analytics panel (DE/EN), token-only CSS
(pin-conform, no hex values), 2 new data-layer tests + 1 new release pin —
`ci:pages` green, 0 extra Actions minutes.

### Wave 9 — structural clarity (edge bundling + vulnerability) — ✅ implemented (2026-10-20)

- **Edge bundling** (opt-in): parallel relationships of the same node pair collapse into
  one edge carrying its **type list**, **count**, **united provenance** and the
  **strongest documented confidence**. This is exactly what makes link charts unreadable
  when two entities share several relationship types; nothing is discarded, the toggle
  can be switched off at any time, order and output are deterministic, and the input is
  never mutated.
- **Structural vulnerability**: articulation points (nodes whose removal splits the
  slice), cut edges (bridges between sub-networks) and the k-core (densest cohesive
  nucleus). Iterative Tarjan plus k-core by binary search — stack-safe, deterministic,
  capped at 50,000 nodes; above that the analysis honestly returns `null` instead of an
  estimate. Parallel edges count as one relationship, LoD groups are excluded.

**Delivered (Wave 9)**: `bundleEdges()` + `structuralVulnerability()` (pure, tested
functions), `Filters.bundle`, `View.vulnerability`, worker wiring, bundle toggle +
vulnerability panel in the analytics area (DE/EN, jump-off points focusable directly),
token-only CSS, 2 new data-layer tests (dashboard 27) + 1 new release pin —
`ci:pages` green, 0 extra Actions minutes.

### Wave 10 — temporal plausibility + failure simulation — ✅ implemented (2026-10-20)

- **"Path over time"**: every hop of a shortest path carries the **documented window**
  of its relationship (earliest to latest evidence date, normalized to the day, with
  united provenance). If a hop's latest documentation lies **before** the previous hop
  began, it is flagged as an **anachronism** — the chain cannot have happened that way
  without a missing explanation. Undated hops stay honestly empty and never become a
  verdict; a hop without a documented relation returns `null` instead of an invention.
- **Failure simulation**: every articulation point from Wave 9 now reports how many
  parts the slice falls into when it disappears and how large the largest survivor is
  ("splits into 3 parts · largest 42"). That is the operational question behind
  structural analysis — not only *whether* an entity is critical, but *what* losing it
  costs.

**Delivered (Wave 10)**: `pathTimeline()` (pure, tested function), `PathTimeline` in the
path protocol + worker computation on the visible slice, `impact` per articulation
point, per-hop evidence block in the path panel (with positive confirmation for a
plausible chain) and the failure hint in the vulnerability list (DE/EN), token-only CSS,
2 new data-layer tests (dashboard 29) + 1 new release pin (release 16) — `ci:pages`
green, 0 extra Actions minutes.

### Wave 11 — case work (notes + case report) — ✅ implemented (2026-10-20)

- **Research notes per entity**: observations, hypotheses and open questions straight in
  the inspector — stored **only in this browser** (localStorage, versioned key), never
  sent to a server, hard-capped at 2,000 characters, re-sanitized on every read
  (broken, empty or foreign entries are dropped instead of trusted). The panel states
  that promise explicitly; the number of annotated entities shows in the filter panel.
- **Case report (Markdown)**: a deterministic report for the case folder — scope, notes
  (alphabetical, multi-line, empty ones excluded), analytics, rule hits, structural
  findings including the failure simulation, duplicates and the monthly mention
  envelope. The timestamp is passed in (no hidden `Date.now()`), the report is
  bilingual (DE/EN) and closes with the clear split: **"hints, not proof"**.

**Delivered (Wave 11)**: `normalizeNote()` / `readNotes()` / `caseReport()` (pure, tested
functions), note block in the inspector (DE/EN, status announcements, character
counter), case-report button in the export footer, token-only CSS, 2 new data-layer
tests (dashboard 31) + 1 new release pin (release 17) — `ci:pages` green, 0 extra
Actions minutes.

### Wave 12 — temporal dynamics (playback + evidence window) — ✅ implemented (2026-10-20)

- **Cumulative playback**: the network grows month by month up to the end of each month
  — play/pause plus single steps forward and back. The month list is **frozen** when
  playback starts, because a time filter shortens the visible timeline; the end is an
  honest stop instead of a wrap-around. Every step is a screen-reader announcement;
  "Clear time filter" leaves the view.
- **Evidence window per edge**: the edge inspector now shows the documented time window
  of the relationship (earliest to latest evidence date, normalized to the day) including
  the number of dated documents — and says "no dated evidence" openly when there is
  none instead of inventing a date.

**Delivered (Wave 12)**: `nextPlaybackMonth()` + `evidenceWindow()` (pure, tested
functions), playback controls in the timeline (DE/EN, `aria-pressed`, status
announcements), evidence-window row in the edge panel, token-only CSS, 2 new data-layer
tests (dashboard 33) + 1 new release pin (release 18) — `ci:pages` green, 0 extra
Actions minutes.

## 4. Budget summary

| Wave | Location | Additional Actions minutes | Status |
| --- | --- | --- | --- |
| 1 Export analytics | existing export step | **0** | ✅ implemented |
| 2 Analysis UI | browser/web worker | **0** | ✅ implemented (incl. 2b) |
| 3 Entity pages | Pages build | **0** | ✅ implemented |
| 4 Ingestion quality | existing ≤5-min jobs | **0** | ✅ implemented |
| 5 Compliance/trust | static pages + docs | **0** | ✅ implemented |
| 6 i18n/PWA | Pages build/browser | **0** | ✅ implemented |
| 7 Ego/timeline | browser/web worker | **0** | ✅ implemented |
| 8 entity resolution/timeline | browser/web worker | **0** | ✅ implemented |
| 9 bundling/vulnerability | browser/web worker | **0** | ✅ implemented |
| 10 chronology/failure | browser/web worker | **0** | ✅ implemented |
| 11 notes/case report | browser (localStorage) | **0** | ✅ implemented |
| 12 playback/evidence window | browser | **0** | ✅ implemented |

No step creates new workflows, new cron jobs, longer jobs or paid services. The only
consumption-relevant limits remain the existing ones: 3 reservations/UTC day, 5-minute
job limit, 6 Gemini calls/day (shared), 500 Pages builds/month, 100,000 worker
requests/day.
