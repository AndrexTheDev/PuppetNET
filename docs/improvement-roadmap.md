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

### Welle 13 — Entitätsauflösung vertieft + Mehrfach-Ausfall — ✅ umgesetzt (2026-10-20)

- **Dubletten-Direktvergleich**: Fakten nebeneinander — Typ, Grad, Community, erste und
  letzte Erwähnung, Belegzahl sowie **gemeinsame** und **eigene Nachbarn** je Kandidat.
  Gemeinsame Nachbarn sind das eigentliche Auflösungssignal, abweichende Merkmale werden
  farblich markiert (Typ, Community, Zeitfenster). Die Kandidaten selbst zählen **nicht**
  als eigene Nachbarn, unbekannte IDs erzeugen keine Spalte, und die App führt weiterhin
  nichts zusammen — der Hinweis „die App führt nichts zusammen" steht direkt am Vergleich.
- **Mehrfach-Ausfall-Simulation**: Bis zu drei Artikulationspunkte per Checkbox
  auswählen; der Worker rechnet auf dem **sichtbaren** Ausschnitt und meldet „n Knoten
  entfernt → x Teile · größter y · isoliert z" inklusive Screenreader-Ansage. Auswahl
  ist begrenzt, unbekannte IDs werden ignoriert, LoD-Gruppen sind keine Entitäten.

**Geliefert (Welle 13)**: `compareDuplicates()` + `multiFailureImpact()` (reine,
getestete Funktionen), Protokoll/Worker-Anfrage `impact`, Direktvergleich-Tabelle +
Ausfall-Auswahl im Analytik-Panel (DE/EN), token-only CSS, 2 neue Datenlagen-Tests
(dashboard 35) + 1 neuer Release-Pin (release 19) — `ci:pages` grün, 0 zusätzliche
Actions-Minuten.

### Welle 14 — Fallsitzungen + Provenienz-Export — ✅ umgesetzt (2026-10-20)

- **Gespeicherte Ansichten**: Modus, Suche, Community, Zeitfenster, Farbmodus und Fokus
  lassen sich unter einem Namen als Fallsitzung sichern und mit einem Klick
  wiederherstellen. Gespeichert wird **nur im Browser** (localStorage, versionierter
  Schlüssel), Namen werden von Steuerzeichen und Mehrfach-Leerraum befreit und auf 60
  Zeichen begrenzt, beim Lesen wird jeder Eintrag erneut geprüft (leere, undatierte oder
  doppelte Einträge fallen raus), die Liste ist auf 50 Sitzungen begrenzt und bleibt
  alphabetisch sortiert — gleicher Name überschreibt statt zu duplizieren.
- **Provenienz-Export (CSV)**: Alle Belege, die den **sichtbaren** Ausschnitt stützen —
  eine Zeile je Dokument mit Quelle, Datum, URL und der Liste der Entitäten **und**
  Beziehungen, die es belegt. Damit ist die Kette „Aussage ← Beleg" exportierbar, nicht
  nur im Inspector sichtbar; nicht mehr auflösbare Dokument-IDs bleiben als ID erhalten
  statt zu verschwinden.

**Geliefert (Welle 14)**: `normalizeSavedName()` / `readSavedViews()` / `upsertSavedView()`
/ `removeSavedView()` / `evidenceToCSV()` (reine, getestete Funktionen),
Protokoll/Worker-Anfrage `evidenceCsv` (berechnet auf dem sichtbaren Ausschnitt),
Sitzungs-Block im Filter-Panel, Beleg-Export im Footer (DE/EN, Statusansagen), token-only
CSS, 2 neue Datenlagen-Tests (dashboard 37) + 1 neuer Release-Pin (release 20) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 15 — Fallbericht-Roundtrip (Notizen zurücklesen) — ✅ umgesetzt (2026-10-20)

- **Notizen importieren**: Ein exportierter Fallbericht (DE oder EN) lässt sich wieder
  einlesen — die Datei bleibt auf dem Gerät, es wird nichts hochgeladen. Der Parser liest
  **nur** den Abschnitt „Ermittlungsnotizen"/„Research notes", verlangt das strenge
  Eintragsformat (`- **Label** (`id`)` plus eingerückter Notizblock), übernimmt
  Mehrzeiligkeit und bereinigt jede Notiz mit denselben Regeln wie live erfasste Notizen
  (2.000 Zeichen, Steuerzeichen, Leerraum). Eine fremde oder abgeschnittene Datei liefert
  ehrlich das, was drinsteht, statt zu werfen.
- **Zusammenführen ohne stilles Überschreiben**: Importierte Notizen füllen **leere**
  Plätze und werden als „neu" gezählt; eine abweichende eigene Notiz **bleibt erhalten**
  und wird als „übersprungen" gemeldet; identischer Text zählt als „unverändert".
  Damit ist der Bericht ein sicherer Transportweg zwischen Rechnern, ohne dass
  Ermittlungsarbeit verloren geht.
- Die Statuszeile nennt die Zahlen („Import: 3 neu, 1 unverändert, 1 übersprungen"),
  Dateien über 5 MiB und unlesbare Dateien werden klar abgelehnt.

**Geliefert (Welle 15)**: `parseCaseNotes()` + `mergeNotes()` (reine, getestete
Funktionen), Import-Steuerung im Filter-Panel (DE/EN, Statusansage), token-only CSS,
1 neuer Datenlagen-Test mit vollständigem Roundtrip in beiden Sprachen (dashboard 38) +
1 neuer Release-Pin (release 21) — `ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 16 — Fallarbeit skalenfest (Notiz-Suche + Fallakte) — ✅ umgesetzt (2026-10-20)

- **Notiz-Suche (Fallindex)**: Sobald Notizen existieren, durchsucht ein Index im
  Filter-Panel **Bezeichnungen und Notiztexte** — ein leeres Suchfeld listet alle
  annotierten Entitäten als Fallübersicht, eine Suche zeigt je Treffer die **passende
  Zeile** als Snippet (nicht die erste) und springt per Klick in den Graphen.
  Treffer, die Bezeichnung *und* Notiz betreffen, stehen vor reinen Label-Treffern,
  davor reine Notiztreffer; Notizen zu Entitäten, die im Ausschnitt fehlen, bleiben mit
  ihrer rohen ID sichtbar statt zu verschwinden. Deterministisch, begrenzt, Snippets
  auf 140 Zeichen gekürzt.
- **Fallakte (JSON)**: Notizen, gespeicherte Ansichten und der Markdown-Bericht in einem
  versionierten Umschlag (`format: puppetnet-case`, `version: 1`) — sortiert,
  normalisiert, ohne verstecktes `Date.now()`, damit dieselbe Fallakte dieselben Bytes
  ergibt. Plain JSON, in jedem Editor lesbar, ohne Server.

**Geliefert (Welle 16)**: `searchNoteIndex()` + `caseBundle()` (reine, getestete
Funktionen), Notiz-Index + Fallakte-Export (DE/EN, Statusansage, Anzahl je Treffer),
token-only CSS, 2 neue Datenlagen-Tests (dashboard 40) + 1 neuer Release-Pin (release 22)
— `ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 17 — Provenienz-Lücken + Fallakte-Roundtrip — ✅ umgesetzt (2026-10-20)

- **Provenienz-Lücken**: Die Methodik verlangt für hochsichere Verknüpfungen **zwei
  unabhängige Quellen** — jetzt prüfbar. Das Panel zeigt Belegdeckung
  („belegt: 12/14 Knoten · 9/11 Kanten"), listet **hochsichere Beziehungen mit nur einer
  Quelle** (Konfidenz ≥ 50 %) und **Entitäten ohne jeden Beleg**, jeweils fokussierbar.
  Ehrlich bei sauberem Ausschnitt: dann steht positiv da, dass keine Lücken bestehen —
  keine leere Liste, kein `null`. LoD-Gruppen und aggregierte Gruppenkanten sind keine
  Aussagen und bleiben außen vor; Listen deterministisch und begrenzt.
- **Fallakte-Roundtrip**: Eine exportierte Fallakte (Welle 16) lässt sich wieder einlesen —
  Format und Version werden **ganz** geprüft (falsche Version → Ablehnung statt
  Teilimport), Notizen und Sitzungen laufen durch dieselben Sanitizer wie der Live-Speicher,
  der Bericht kommt als reiner Text zurück (nie ausgeführt). Notizen werden wie in Welle 15
  zusammengeführt: leere Plätze füllen, eigene Notiz nie überschreiben.
- Der Provenienz-Abschnitt wandert zusätzlich in den **Fallbericht** (DE/EN), damit die
  Beleglage im Dokument steht und nicht nur im UI.

**Geliefert (Welle 17)**: `provenanceGaps()` + `readCaseBundle()` (reine, getestete
Funktionen), `View.gaps` im Worker-View, Lücken-Panel + Fallakte-Import (DE/EN,
Statusansagen), Provenienz-Abschnitt im Bericht, token-only CSS, 2 neue Datenlagen-Tests
(dashboard 42) + 1 neuer Release-Pin (release 23) — `ci:pages` grün, 0 zusätzliche
Actions-Minuten.

### Welle 18 — Belegqualität (Unabhängigkeit + gemeinsame Belege) — ✅ umgesetzt (2026-10-20)

- **Quellenlage je Beziehung**: Der Kanten-Inspector bewertet jetzt *Unabhängigkeit* statt
  nur Dokumente zu zählen. Fünf ehrliche Stufen: `keine Belege`, `ein Beleg`
  („für eine belastbare Aussage fehlt die zweite Quelle"), `mehrere Belege, derselbe
  Quellenname` (**nicht** unabhängig — zwei Artikel desselben Hauses sind keine
  Korroboration), `mehrere Belege ohne Quellenname` (Unabhängigkeit **nicht belegbar**)
  und `n unabhängige Quellen`. Farbgebung über Tokens, Text bleibt die Aussage.
- **Belege mit mehreren Entitäten**: Ein Dokument, das mehrere sichtbare Entitäten stützt
  (Registerauszug mit drei Firmen, Artikel mit zwei Personen), wird als eigene Gruppe
  gezeigt — ein beleggetriebener Zusammenhang, den kantenbasierte Ansichten nicht
  sichtbar machen können. Explizit als **Hinweis auf gemeinsame Herkunft, keine Aussage
  über Identität** gekennzeichnet; jede Entität ist direkt fokussierbar.
- Beides wandert in den **Fallbericht** (DE/EN), damit die Belegqualität im Dokument steht.

**Geliefert (Welle 18)**: `edgeSources()` + `sharedSourceGroups()` (reine, getestete
Funktionen), `View.sharedSources` aus dem Worker, Quellenlage-Zeile im Inspector,
Gemeinsame-Belege-Panel + Berichtszeile (DE/EN), token-only CSS, 2 neue Datenlagen-Tests
(dashboard 44) + 1 neuer Release-Pin (release 24) — `ci:pages` grün, 0 zusätzliche
Actions-Minuten.

### Welle 19 — Quellen-Filter (Belegqualität als Arbeitsmittel) — ✅ umgesetzt (2026-10-20)

- **Netz auf belastbare Aussagen einschränken**: Der Filter „Belegqualität" bietet
  *Alle Beziehungen*, *Nur mehrfach belegte* (mindestens zwei Belege aus **verschiedenen
  benannten** Quellen) und *Nur dünn belegte* (Einzelbeleg, gleicher Quellenname oder
  ohne Quellenname). Damit wird aus der Anzeige aus Welle 18 ein Arbeitsmittel: erst
  der Korridor, der einer Prüfung standhält, dann die Lücken.
- **Grundsatz „Aussagen, keine Entitäten"**: Der Filter entfernt ausschließlich Kanten —
  Entitäten bleiben sichtbar, auch wenn ihre Belege dünn sind. Die Belegtabelle wird nie
  reduziert, die Statuszeile nennt „x von y Beziehungen entsprechen der Belegqualität",
  und bei leerem Ergebnis steht das ehrlich da statt eines leeren Netzes ohne Erklärung.
- **Teilbare Ansicht**: Der Filter liegt im Permalink (`sources=corroborated|thin`),
  unbekannte Werte werden beim Lesen verworfen — eine manipulierte URL kann den Filter
  nicht in einen undefinierten Zustand bringen.

**Geliefert (Welle 19)**: `SourceFilter` + `edgeSourceStatus()` (reine, getestete
Funktionen), Kantenfilter in `filterDataset()`, URL-Roundtrip, Filter-Block im
Filter-Panel (DE/EN, Statuszeile, Leer-Hinweis), token-only CSS, 1 neuer Datenlagen-Test
(dashboard 45) + 1 neuer Release-Pin (release 25) — `ci:pages` grün, 0 zusätzliche
Actions-Minuten.

### Welle 20 — Quellen-Register (wer sagt das?) — ✅ umgesetzt (2026-10-20)

- **Quellen-Register**: Ein Panel listet die **Absender** des Ausschnitts — pro Quelle die
  Zahl der Dokumente, gestützten Entitäten und Beziehungen sowie ihr dokumentiertes
  Zeitfenster. Das beantwortet die Frage, die per-Knoten-Beleglisten nie beantworten
  können: *wer trägt diesen Fall eigentlich*, und wie breit ist die Quellenbasis.
  Dokumente **ohne Quellenangabe** werden als eigene Zeile geführt, nicht verschwiegen;
  ein referenziertes, aber nicht auflösbares Dokument zählt dort ebenfalls (ehrliche
  Unschärfe statt stiller Lücke).
- **Quellenfilter („nur dieser Absender")**: Klick auf eine Quelle zeigt ausschließlich
  deren Berichterstattung — Kanten mit Belegen dieser Quelle plus die Entitäten, die
  darin vorkommen. Bewusst **anders** als der Belegqualitäts-Filter aus Welle 19: hier
  werden Entitäten mit eingeschränkt (die Frage ist die Abdeckung *einer* Quelle), das
  wird im UI benannt und ist mit einem Klick aufgehoben. Groß-/Kleinschreibung und
  Leerraum werden normalisiert, unbekannte Quellen ergeben ehrlich ein leeres Netz,
  und das Register bleibt während des Filters **vollständig** sichtbar (Umschalten ohne
  Umweg über „zurücksetzen").
- Der Filter liegt im Permalink (`outlet=…`) und ist längenbeschränkt; überlange Werte
  werden beim Lesen verworfen.

**Geliefert (Welle 20)**: `sourceRegistry()` (reine, getestete Funktion, reihenfolge-
unabhängig durch Sortierung der Dokument-IDs), `Filters.outlet`, `View.sourceRegistry`,
Register-Panel + Quellenfilter (DE/EN, Statuszeile, Aufheben), token-only CSS, 2 neue
Datenlagen-Tests (dashboard 47) + 1 neuer Release-Pin (release 26) — `ci:pages` grün,
0 zusätzliche Actions-Minuten.

### Welle 21 — Widersprüche & Aktualität — ✅ umgesetzt (2026-10-21)

- **Widersprüchliche Aussagen**: Trägt dasselbe Entitätenpaar mehrere Beziehungstypen,
  wird das nicht pauschal als Widerspruch verkauft. Jeder Typ bekommt sein belegtes
  Zeitfenster, und erst der Vergleich entscheidet:
  **„Widerspruch"** (Fenster überlappen — beide Aussagen gelten im selben Zeitraum,
  das braucht eine Entscheidung), **„Entwicklung"** (Fenster getrennt — z. B. erst
  Eigentum 2020, später Verkauf 2021: eine Geschichte, kein Streit) und
  **„nicht bewertbar"** (mindestens eine Seite ohne Datum). Gleicher Typ aus mehreren
  Quellen ist Korroboration und erscheint gar nicht erst.
- **Aktualität der Belege**: Wie alt ist das, worauf die Aussagen beruhen? Je Entität
  wird das jüngste Dokument gegen ein Fenster (Standard 12 Monate) geprüft; das
  Referenzdatum wird von außen übergeben, damit das Ergebnis reproduzierbar ist.
  Entitäten **ohne jede Datumsangabe** werden getrennt gezählt — „unbekannt" ist nicht
  „alt". Die Monatsarithmetik klemmt den Tag (31.01. minus 1 Monat = 31.12., 31.03. =
  28.02., Schaltjahre echt), LoD-Gruppen sind keine Entitäten.
- Beide Abschnitte wandern in den **Fallbericht** (DE/EN): eine Fallakte, die
  Widersprüche und veraltete Belege verschweigt, wäre unvollständig.

**Geliefert (Welle 21)**: `pairConflicts()` + `evidenceFreshness()` + `monthsBefore()`
(reine, getestete Funktionen), `View.conflicts` + `View.freshness` aus dem Worker,
Widerspruchs- und Aktualitäts-Panel (DE/EN, Farbcodierung nach Art, fokussierbare
Entitäten), Berichtsabschnitte, token-only CSS, 2 neue Datenlagen-Tests (dashboard 49) +
1 neuer Release-Pin (release 27) — `ci:pages` grün, 0 zusätzliche Actions-Minuten.
Der i18n-Paritätstest hat dabei einen echten Platzhalter-Fehler (`{entity}`/`{label}`)
vor dem Commit gefangen.

### Welle 22 — Widerspruchs-Ansicht & Zeitfenster — ✅ umgesetzt (2026-10-21)

- **Widerspruchs-Ansicht** (`Filters.dispute`): ein Klick reduziert das Netzwerk auf
  genau die Paare, deren Aussagen sich **wirklich** widersprechen — beide Aussagen des
  Paares bleiben sichtbar, alles andere verschwindet. Entwicklungen (getrennte
  Zeiträume) und Korroboration (gleicher Typ) werden **nicht** eingeschnitten, denn sie
  sind keine Streitfrage. Das Konfliktset wird aus dem Ausschnitt **vor** der
  Einschränkung berechnet, deshalb ist das Umschalten stabil und das Panel bleibt
  vollständig; ist nichts strittig, sagt die Ansicht das ehrlich statt leer zu bleiben.
- **Zeitfenster der Aktualitätsprüfung** (`Filters.fresh`, geschlossene Liste
  6 / 12 / 24 / 60 Monate, Standard 12): im Panel umschaltbar mit `aria-pressed`,
  im Permalink nur bei Abweichung vom Standard (`fresh=24`), unbekannte Werte werden
  verworfen statt geraten. So ist „veraltet" immer eine definierte Aussage.
- Beides zusammengenommen beantwortet die Frage, die vor jedem Telefonat steht:
  *Was ist strittig, und wie dünn ist die Aktenlage darunter?*

**Geliefert (Welle 22)**: `FRESH_WINDOWS`/`isFreshWindow`/`DEFAULT_FRESH_MONTHS`,
`Filters.dispute`, Einschränkung als letzter Schritt in `filterDataset()`, `fresh`+
`dispute` im Permalink (beide Writer), Worker rechnet Konflikte und Aktualität auf dem
Ausschnitt vor der Einschränkung, Umschalter im Widerspruchs-Panel und
Fenster-Buttons im Aktualitäts-Panel, 10 neue DE/EN-Schlüssel, token-only CSS,
2 neue Datenlagen-Tests (dashboard 51) + 1 neuer Release-Pin (release 28) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten. Zwei Wellen-Pins aus Welle 21 wurden
dabei auf stabile Muster umgestellt, weil Welle 22 dieselben Zeilen erweitert hat.

### Welle 23 — Belastbarkeit der Akte — ✅ umgesetzt (2026-10-21)

- **Zwei ehrliche Maße statt einer erfundenen Punktzahl**: je Entität werden die
  Belege, die Zahl der **unabhängigen** Quellen und der jüngste Beleg gegenübergestellt.
  Daraus folgen sechs benannte Zustände: **ohne Beleg**, **dünn und alt** (die
  Kombination, die einen Anruf braucht), **nur eine Quelle** (dünn, aber aktuell),
  **veraltet** (korroboriert, aber alt), **ohne Datum** (korroboriert, aber nicht
  datierbar) und **solide**.
- **Unbenannte Dokumente stützen, belegen aber nicht unabhängig** (Regel aus Welle 18):
  drei namenlose Dokumente bleiben eine Quelle. Belege, die über eine sichtbare
  Beziehung an eine Entität gelangen, zählen mit (Regel aus Welle 20) — sonst hieße
  eine Entität „unbelegt", nur weil der Export das Dokument an die Kante gehängt hat.
  Beide Regeln liegen in **einer** exportierten Funktion (`nodeBacking`), die Panel
  und Filter gemeinsam nutzen.
- **Dünn-Filter** (`Filters.weak`): schneidet auf Entitäten mit höchstens einer
  unabhängigen Quelle — bewusst **ohne Uhr**, damit er reproduzierbar ist; die
  zweidimensionale Bewertung (dünn *und* alt) bleibt Aufgabe des Panels.
- **CSV-Export** der Nachrecherche-Liste (RFC-4180, gleiche Quoting-Funktion wie die
  übrigen Exporte) und ein eigener Berichtsabschnitt DE/EN. Das Panel rechnet wie
  Konflikte und Register auf dem Ausschnitt **vor** der Einschränkung, damit das
  Einschränken die eigene Bewertung nicht verfälscht.

**Geliefert (Welle 23)**: `nodeBacking()`, `evidenceRisk()`, `riskToCSV()`,
`Filters.weak` als letzter Schritt in `filterDataset()`, `weak=1` im Permalink (beide
Writer), Risiko-Panel mit Zustandsliste, CSV-Knopf, Umschalter und ehrlichem
Alles-solide-Zustand, Berichtsabschnitt, 18 neue DE/EN-Schlüssel, token-only CSS,
3 neue Datenlagen-Tests (dashboard 54) + 1 neuer Release-Pin (release 29) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten. Der Welle-22-Pin wurde dabei erneut auf
ein Verhaltensmuster umgestellt, statt eine Zeile festzunageln.

### Welle 24 — Änderungs-Vergleich — ✅ umgesetzt (2026-10-21)

- **Was ist neu?** Ein älterer Export lässt sich als **Vergleichsstand** laden; die App
  zeigt gegen den aktuellen Stand: neue und verschwundene Entitäten, neue und
  verschwundene Beziehungen, neue Dokumente, **neu widersprüchliche Paare**,
  **aufgelöste Widersprüche** und Entitäten, die **neu durch zwei unabhängige Quellen**
  gedeckt sind.
- **Identität bewusst streng**: Entitäten über die ID, Beziehungen über das **gerichtete**
  Tripel Quelle/Typ/Ziel, Dokumente über die ID. Eine umbenannte Entität ist weder neu
  noch verschwunden, und ein zweiter Beleg zu einer bekannten Beziehung ist **keine**
  neue Beziehung — genau die Verwechslung, die ein Monitoring unbrauchbar macht.
- **Gleiche Regeln wie überall**: Widersprüche und Unabhängigkeit werden mit denselben
  Funktionen bewertet wie die Panels (`pairConflicts`, `nodeBacking`), damit „neu
  widersprüchlich" und „jetzt korroboriert" exakt dasselbe bedeuten wie anderswo.
- **Worker statt UI-Thread**: der Vergleichsstand wird im Worker geparst und verglichen
  (wie jeder Import), damit auch 64 MiB den Browser nicht blockieren. Der Vergleich ist
  eine reine Funktion, ohne Uhr, deterministisch und in jeder Liste begrenzt (Zählungen
  bleiben vollständig).
- **Änderungsliste als CSV** (RFC-4180, wiederverwendete Quoting-Funktion) für die
  Weitergabe; der Vergleich ist jederzeit mit einem Klick entfernbar.

**Geliefert (Welle 24)**: `diffDatasets()`, `diffToCSV()`, `DatasetDiff`,
`WorkerRequest`/`WorkerResponse` `diff`, Vergleichsdatei-Knopf in der Quellenleiste,
Änderungs-Panel (Zählungen, neue/verschwundene Entitäten, Korroboration, neue
Widersprüche, aufgelöste, CSV, Entfernen), 17 neue DE/EN-Schlüssel, token-only CSS,
1 neuer Datenlagen-Test (dashboard 55) + 1 neuer Release-Pin (release 30) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 25 — Akten-Suche — ✅ umgesetzt (2026-10-21)

- **Ein Suchfeld für die ganze Akte**: Entitäten, Widersprüche, Belastbarkeit,
  Quellen und Recherche-Notizen. Jeder Treffer nennt seine **Art** — damit ist klar,
  *warum* etwas passt — und ein Klick springt an die richtige Stelle: Entität
  fokussieren, Widerspruchs-Ansicht öffnen, Quellenfilter setzen.
- **Jedes Objekt erscheint genau einmal**, unter dem aussagekräftigsten Titel: eine
  Entität, die zugleich ein Belastbarkeits-Befund ist, wird als Entität gelistet und
  trägt den Befund als Detail — zwei Zeilen für dieselbe Sache wären Lärm.
- **Gewichtung ist erklärt statt zufällig**: Entität vor Widerspruch vor
  Belastbarkeit vor Quelle vor Notiz, bei gleichem Text-Score; danach Label und ID.
  Leere Eingabe liefert **nichts** statt einer Datenhalde; Ergebnis begrenzt,
  deterministisch, LoD-Gruppen ausgenommen. Jeder Indexteil ist optional.
- **Notiz-Auszüge zeigen den Treffer**, nicht den Anfang: das Fenster wird um den
  Treffer zentriert und enthält das gesuchte Wort vollständig, wenn es hineinpasst —
  ein Auszug, der genau das gesuchte Wort abschneidet, wäre schlechter als keiner.
- Die Suche läuft **im Browser** auf den bereits geladenen Panels — 0 zusätzliche
  Actions-Minuten, kein Server, keine neue Datenkopie.

**Geliefert (Welle 25)**: `searchCase()`, `noteSnippet()`, `CaseHit`/`CaseKind`,
Suchfeld im Analyse-Bereich mit Trefferart, Detail, Sprungzielen und ehrlichem
Leer-/Kein-Treffer-Text, 14 neue DE/EN-Schlüssel, token-only CSS,
2 neue Datenlagen-Tests (dashboard 57) + 1 neuer Release-Pin (release 31) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 26 — Build-Gate ortsunabhängig (Go-live-Blocker) — ✅ umgesetzt (2026-10-21)

- **Befund**: Der Cloudflare-Pages-Check schlägt bei **jedem** Branch-Commit fehl —
  seit mindestens Welle 16, also lange vor dieser Arbeit. Reproduktion in einer
  sauberen Kopie: der dokumentierte Befehl `cd ../.. && npm ci && npm run ci:pages`
  **funktioniert** (EXIT 0), aber `npm run ci:pages` **aus `apps/web` heraus** bricht mit
  `Missing script: "ci:pages"` ab. Genau das passiert, wenn Cloudflare den Build aus dem
  konfigurierten Wurzelverzeichnis (`apps/web`) startet — der wahrscheinlichste Grund
  für den jahrelangen „Build failed"-Status.
- **Fix an zwei Stellen, damit keine Konfiguration mehr falsch sein kann**:
  1. `apps/web` und `apps/worker` bekommen einen delegierenden Gleichnamigkeits-Skript
     (`ci:pages` bzw. `ci:worker`), das zur Repo-Wurzel wechselt — der Build läuft damit
     aus **beiden** Startpunkten identisch.
  2. `scripts/pages-build.mjs` und `scripts/worker-build.mjs` verankern sich selbst am
     Repository (`import.meta.url` → `process.chdir`), statt dem Arbeitsverzeichnis des
     Aufrufers zu vertrauen. Relative Pfade können so nie auf die falschen Dateien zeigen.
- **Verifiziert in beide Richtungen**: `npm run ci:pages` von der Wurzel **und** aus
  `apps/web` → EXIT 0 (87 Dateien, 6.58 MiB); `npm run ci:worker` von der Wurzel **und**
  aus `apps/worker` → EXIT 0.
- Dokumentiert in `cloudflare-builds.json` und `docs/operations.md` (inklusive der
  Fehlersignatur `Missing script: "ci:pages"`), damit der Befund nicht wieder verloren geht.
- **0 zusätzliche Actions-Minuten**, und der Go-live-Pfad ist nicht mehr durch eine
  Konfigurationsfalle blockiert.

**Geliefert (Welle 26)**: Workspace-Delegation in beiden Workspaces, selbstverankerte
Build-Gates, dokumentierte Startpunkte, 1 neuer Release-Pin (release 32), `ci:pages`
grün aus Root und aus `apps/web`.

### Welle 27 — Beobachtungsliste — ✅ umgesetzt (2026-10-21)

- **Entitäten markieren, die im Blick bleiben sollen** — im Inspektor mit einem Klick,
  `aria-pressed` inklusive. Die Liste überlebt einen Reload (nur `localStorage`), ist auf
  200 Einträge begrenzt, wird beim Lesen **bereinigt** (keine Duplikate, keine leeren IDs,
  Zahlen und Objekte sind Müll) und verlässt das Gerät nur beim bewussten CSV-Export.
- **Sortiert nach Dringlichkeit, nicht nach Eingabe**: Streitfälle zuerst, dann
  verschwundene Einträge, dann Befunde (dünn und alt, veraltet, nur eine Quelle …), dann
  der Rest — nichts wird ausgeblendet. Eine Beobachtungsliste, die ihre Probleme
  versteckt, wäre nur eine Lesezeichenliste.
- **Bewertet mit den Regeln der Panels**: Streitfälle aus `pairConflicts` (nur echte
  Widersprüche, keine zeitlichen Entwicklungen), Belege und Quellen aus `nodeBacking`,
  Zustände aus `evidenceRisk`. „Ohne Befund" heißt ehrlich *kein Eintrag in der
  Auffälligkeitsliste* — nicht „solide", denn das behauptet die Risiko-Liste nicht.
- **Verschwundene Einträge werden benannt, nicht gelöscht**: fällt eine beobachtete
  Entität aus dem aktuellen Export, sagt die Liste es und behält die ID. Beobachten ist
  Fallarbeit und darf nicht von der Datenlage abhängen.
- **CSV-Export** der kurzen Liste (inkl. strittiger Beziehungstypen), damit sie in ein
  Gespräch mitgenommen werden kann — kein Server, keine Telemetrie, 0 zusätzliche
  Actions-Minuten.

**Geliefert (Welle 27)**: `WATCH_LIMIT`/`WATCH_STORAGE_KEY`/`readWatchlist()`/`toggleWatch()`,
`watchReport()`, `watchToCSV()`, Inspektor-Umschalter, Beobachtungs-Panel mit Dringlichkeits-Reihenfolge,
Entfernen je Eintrag, Verschwunden-Hinweis und CSV, 16 neue DE/EN-Schlüssel, token-only CSS,
2 neue Datenlagen-Tests (dashboard 59) + 1 neuer Release-Pin (release 34) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

### Welle 28 — Beobachtungs-Abgleich — ✅ umgesetzt (2026-10-21)

- **Die eigentliche Monitoring-Frage**: Welle 24 zeigt *alles*, was sich geändert hat —
  und das ist in der Praxis fast immer Rauschen. Welle 28 schneidet den Vergleich auf die
  **eigene Beobachtungsliste** zu: *was hat sich bei genau dem geändert, was ich im Blick
  habe?*
- **Änderungsindex statt Baseline**: `diffDatasets()` liefert jetzt zusätzlich die
  **Entitäts-IDs** je Änderungsart (neu, verschwunden, korroboriert, neu widersprüchlich,
  Beziehung geändert, neu dokumentiert). Damit ist der Abgleich eine reine Funktion auf
  dem geladenen Vergleich — die 64-MiB-Baseline muss nie erneut gelesen werden, und ein
  Häkchen in der Beobachtungsliste aktualisiert den Abgleich **sofort**.
- **Jede Entität einmal, alle Änderungsarten erhalten**, sortiert nach Dringlichkeit
  (neuer Widerspruch → verschwunden → neu → zweite unabhängige Quelle → Beziehung →
  neues Dokument). Unveränderte Beobachtungen erscheinen **gar nicht** — ein Abgleich, der
  alles listet, ist keiner.
- **Gleiche Regel wie überall**: ein neues Dokument, das an einer Beziehung hängt,
  dokumentiert **beide Enden** (Welle-23-Regel) — sonst meldete der Abgleich eine Entität
  als unverändert, obwohl sie neue Belege hat.
- **Ehrliche Grenzen**: der Index ist je Art auf 2 000 IDs begrenzt (dedupliziert,
  sortiert) und sagt im Panel, wenn gekürzt wurde. Export als CSV für „was hat sich bei
  meinen Beobachtungen bewegt".

**Geliefert (Welle 28)**: `DatasetDiff.watch`, `watchChanges()`, `watchChangesToCSV()`,
`WatchChange`/`WatchChangeKind`, Abgleich-Abschnitt im Beobachtungs-Panel (Dringlichkeits-Reihenfolge,
Arten-Chips, CSV, gekürzt-Hinweis, ehrlicher „nichts geändert"-Zustand), 10 neue DE/EN-Schlüssel,
token-only CSS, 2 neue Datenlagen-Tests (dashboard 61) + 1 neuer Release-Pin (release 35) —
`ci:pages` grün, 0 zusätzliche Actions-Minuten.

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
| 13 Direktvergleich/Mehrfachausfall | Browser/Web Worker | **0** | ✅ umgesetzt |
| 14 Fallsitzungen/Provenienz-Export | Browser/Web Worker | **0** | ✅ umgesetzt |
| 15 Fallbericht-Roundtrip | Browser | **0** | ✅ umgesetzt |
| 16 Notiz-Index/Fallakte | Browser | **0** | ✅ umgesetzt |
| 17 Provenienz-Lücken/Fallakte-Import | Browser/Web Worker | **0** | ✅ umgesetzt |
| 18 Belegqualität/gemeinsame Belege | Browser/Web Worker | **0** | ✅ umgesetzt |
| 19 Quellen-Filter | Browser/Worker | **0** | ✅ umgesetzt |
| 20 Quellen-Register | Browser/Worker | **0** | ✅ umgesetzt |
| 21 Widersprüche/Aktualität | Browser/Worker | **0** | ✅ umgesetzt |
| 22 Widerspruchs-Ansicht/Zeitfenster | Browser/Worker | **0** | ✅ umgesetzt |
| 23 Belastbarkeit der Akte | Browser/Worker | **0** | ✅ umgesetzt |
| 24 Änderungs-Vergleich | Browser/Worker | **0** | ✅ umgesetzt |
| 25 Akten-Suche | Browser | **0** | ✅ umgesetzt |
| 26 Build-Gate ortsunabhängig | Build/CI | **0** | ✅ umgesetzt |
| 27 Beobachtungsliste | Browser (lokal) | **0** | ✅ umgesetzt |
| 28 Beobachtungs-Abgleich | Browser | **0** | ✅ umgesetzt |

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

### Wave 13 — deeper entity resolution + multi-node failure — ✅ implemented (2026-10-20)

- **Duplicate side-by-side comparison**: facts next to each other — type, degree,
  community, first and last mention, evidence count plus **shared** and **own**
  neighbours per candidate. Shared neighbours are the actual resolution signal;
  differing attributes (type, community, time window) are marked. The candidates never
  count as each other's neighbours, unknown ids never create a column, and the app
  still merges nothing — the note "the app merges nothing" sits right next to the table.
- **Multi-node failure simulation**: select up to three articulation points via
  checkboxes; the worker computes on the **visible** slice and reports "n nodes removed →
  x parts · largest y · isolated z" with a screen-reader announcement. The selection is
  bounded, unknown ids are ignored, LoD groups are not entities.

**Delivered (Wave 13)**: `compareDuplicates()` + `multiFailureImpact()` (pure, tested
functions), `impact` protocol/worker request, side-by-side table + failure selection in
the analytics panel (DE/EN), token-only CSS, 2 new data-layer tests (dashboard 35) +
1 new release pin (release 19) — `ci:pages` green, 0 extra Actions minutes.

### Wave 14 — case sessions + provenance export — ✅ implemented (2026-10-20)

- **Saved views**: mode, search, community, time window, colour mode and focus can be
  stored under a name as a case session and restored with one click. Stored **only in
  this browser** (localStorage, versioned key); names lose control characters and
  repeated whitespace and are capped at 60 characters, every entry is re-validated on
  read (empty, undated or duplicate entries drop out), the list is capped at 50 sessions
  and stays alphabetically sorted — the same name overwrites instead of duplicating.
- **Provenance export (CSV)**: every document that supports the **visible** slice — one
  row per document with source, date, URL and the entities **and** relationships it
  backs. That makes the "claim ← evidence" chain exportable instead of inspector-only;
  documents that no longer resolve keep their raw id instead of disappearing.

**Delivered (Wave 14)**: `normalizeSavedName()` / `readSavedViews()` / `upsertSavedView()`
/ `removeSavedView()` / `evidenceToCSV()` (pure, tested functions), `evidenceCsv`
protocol/worker request (computed on the visible slice), sessions block in the filter
panel, evidence export in the footer (DE/EN, status announcements), token-only CSS,
2 new data-layer tests (dashboard 37) + 1 new release pin (release 20) — `ci:pages`
green, 0 extra Actions minutes.

### Wave 15 — case-report roundtrip (notes back in) — ✅ implemented (2026-10-20)

- **Import notes**: an exported case report (DE or EN) can be read back in — the file
  stays on the device, nothing is uploaded. The parser reads **only** the "Research
  notes"/"Ermittlungsnotizen" section, requires the strict entry format
  (`- **Label** (`id`)` plus the indented note block), preserves multi-line notes and
  sanitizes every note with the same rules as live notes (2,000 characters, control
  characters, whitespace). A foreign or truncated file honestly yields what it contains
  instead of throwing.
- **Merging without silent overwrites**: imported notes fill **empty** slots and count as
  "new"; a differing local note is **kept** and reported as "skipped"; identical text
  counts as "unchanged". That makes the report a safe transport between machines without
  losing investigative work.
- The status line names the numbers ("Import: 3 new, 1 unchanged, 1 skipped"), and files
  above 5 MiB or unreadable files are rejected clearly.

**Delivered (Wave 15)**: `parseCaseNotes()` + `mergeNotes()` (pure, tested functions),
import control in the filter panel (DE/EN, status announcement), token-only CSS, 1 new
data-layer test with a full roundtrip in both languages (dashboard 38) + 1 new release
pin (release 21) — `ci:pages` green, 0 extra Actions minutes.

### Wave 16 — case work that scales (note index + case bundle) — ✅ implemented (2026-10-20)

- **Note index**: as soon as notes exist, a panel searches **labels and note text** — an
  empty field lists every annotated entity as a case overview, a query shows the
  **matching line** as the snippet (not the first one) and jumps into the graph on click.
  Hits matching label *and* note rank first, then label-only, then note-only; notes whose
  entity is missing from the slice stay visible under their raw id instead of vanishing.
  Deterministic, bounded, snippets capped at 140 characters.
- **Case bundle (JSON)**: notes, saved views and the Markdown report in a versioned
  envelope (`format: puppetnet-case`, `version: 1`) — sorted, normalized, without a
  hidden `Date.now()`, so the same case produces the same bytes. Plain JSON, readable in
  any editor, no server involved.

**Delivered (Wave 16)**: `searchNoteIndex()` + `caseBundle()` (pure, tested functions),
note index + case-bundle export (DE/EN, status announcement, hit counts), token-only CSS,
2 new data-layer tests (dashboard 40) + 1 new release pin (release 22) — `ci:pages`
green, 0 extra Actions minutes.

### Wave 17 — provenance gaps + case-bundle roundtrip — ✅ implemented (2026-10-20)

- **Provenance gaps**: the methodology asks for **two independent sources** on
  high-confidence links — now checkable. The panel shows coverage ("backed: 12/14 nodes ·
  9/11 edges"), lists **high-confidence relationships with a single source** (confidence
  ≥ 50 %) and **entities without any document**, each focusable. Honest on a clean
  slice: it positively states that there are no gaps — no empty list, no `null`. LoD
  groups and aggregated group edges are not claims and stay out; lists are deterministic
  and bounded.
- **Case-bundle roundtrip**: an exported bundle (Wave 16) can be read back in — format and
  version are validated **as a whole** (wrong version → rejection instead of a partial
  import), notes and sessions pass the same sanitizers as the live store, and the report
  returns as plain text (never executed). Notes merge like in Wave 15: fill empty slots,
  never overwrite the investigator's own note.
- The provenance section also travels into the **case report** (DE/EN), so the evidence
  situation is in the document, not just the UI.

**Delivered (Wave 17)**: `provenanceGaps()` + `readCaseBundle()` (pure, tested functions),
`View.gaps` in the worker view, gaps panel + bundle import (DE/EN, status announcements),
provenance section in the report, token-only CSS, 2 new data-layer tests (dashboard 42) +
1 new release pin (release 23) — `ci:pages` green, 0 extra Actions minutes.

### Wave 18 — evidence quality (independence + shared documents) — ✅ implemented (2026-10-20)

- **Source status per relationship**: the edge inspector now judges *independence*
  instead of counting documents. Five honest levels: `no evidence`, `one document`
  ("the second source is missing for a solid claim"), `several documents, same source
  name` (**not** independent — two articles from one outlet are not corroboration),
  `several documents without a source name` (independence **cannot be proven**) and
  `n independent sources`.
- **Documents backing several entities**: one document supporting several visible
  entities (a register extract naming three companies, an article naming two people) is
  shown as its own group — an evidence-driven connection that edge-based views cannot
  surface. Explicitly labelled as a **hint at shared provenance, not a statement about
  identity**; every entity is focusable.
- Both travel into the **case report** (DE/EN), so evidence quality is in the document.

**Delivered (Wave 18)**: `edgeSources()` + `sharedSourceGroups()` (pure, tested
functions), `View.sharedSources` from the worker, source-status row in the inspector,
shared-document panel + report line (DE/EN), token-only CSS, 2 new data-layer tests
(dashboard 44) + 1 new release pin (release 24) — `ci:pages` green, 0 extra Actions
minutes.

### Wave 19 — evidence-quality filter (quality as a working tool) — ✅ implemented (2026-10-20)

- **Narrow the network to claims that hold**: the "evidence quality" filter offers *all
  relationships*, *corroborated only* (at least two documents from **distinct named**
  sources) and *thinly sourced only* (single document, same source name, or no source
  name). That turns the Wave-18 display into a tool: first the corridor that survives
  review, then the gaps.
- **"Claims, not entities"**: the filter removes edges only — entities stay visible even
  when their evidence is thin. The evidence table is never reduced, the status line says
  "x of y relationships match the evidence quality", and an empty result is stated
  honestly instead of leaving an unexplained empty network.
- **Shareable view**: the filter lives in the permalink
  (`sources=corroborated|thin`); unknown values are dropped on read, so a tampered URL
  cannot put the filter into an undefined state.

**Delivered (Wave 19)**: `SourceFilter` + `edgeSourceStatus()` (pure, tested functions),
edge filtering in `filterDataset()`, URL round trip, filter block in the filter panel
(DE/EN, status line, empty hint), token-only CSS, 1 new data-layer test (dashboard 45) +
1 new release pin (release 25) — `ci:pages` green, 0 extra Actions minutes.

### Wave 20 — source registry (who is saying this?) — ✅ implemented (2026-10-20)

- **Source registry**: a panel lists the **senders** behind the slice — per outlet the
  number of documents, supported entities and relationships plus its documented window.
  That answers what per-node evidence lists never can: *who actually carries this case*,
  and how broad the source base is. Documents **without a source name** get their own
  row instead of being hidden; a referenced-but-unresolved document counts there too
  (honest blur instead of a silent gap).
- **Outlet filter ("only this sender")**: clicking an outlet shows only its reporting —
  edges documented by that outlet plus the entities appearing in them. Deliberately
  **different** from the Wave-19 claim filter: entities are narrowed here because the
  question is one outlet's coverage; the UI says so and one click undoes it. Case and
  whitespace are normalised, unknown outlets honestly yield an empty network, and the
  registry stays **complete** while the filter is active (switch without resetting).
- The filter lives in the permalink (`outlet=…`) and is length-bounded; oversized values
  are dropped on read.

**Delivered (Wave 20)**: `sourceRegistry()` (pure, tested, order-independent by sorting
document ids), `Filters.outlet`, `View.sourceRegistry`, registry panel + outlet filter
(DE/EN, status line, undo), token-only CSS, 2 new data-layer tests (dashboard 47) +
1 new release pin (release 26) — `ci:pages` green, 0 extra Actions minutes.

### Wave 21 — contradictions & currency — ✅ implemented (2026-10-21)

- **Conflicting claims**: when one entity pair carries several relationship types, that
  is not sold as a blanket contradiction. Every type gets its documented window and the
  comparison decides: **"conflict"** (windows overlap — both claims hold in the same
  period, a decision is needed), **"development"** (windows separate — ownership in 2020,
  sale in 2021 is a story, not a dispute) and **"cannot be assessed"** (at least one side
  undated). The same type from several sources is corroboration and does not appear.
- **Currency of the evidence**: how old is what the claims rest on? Per entity the newest
  document is compared against a window (12 months by default); the reference date is
  passed in so the result is reproducible. Entities **without any date** are counted
  separately — "unknown" is not "old". Month arithmetic clamps the day of month
  (31 Jan minus one month = 31 Dec, 31 Mar = 28 Feb, real leap years), LoD groups are not
  entities.
- Both sections travel into the **case report** (DE/EN): a case file that hides
  contradictions and stale evidence would be incomplete.

**Delivered (Wave 21)**: `pairConflicts()` + `evidenceFreshness()` + `monthsBefore()`
(pure, tested functions), `View.conflicts` + `View.freshness` from the worker, conflict
and currency panels (DE/EN, kind-based colouring, focusable entities), report sections,
token-only CSS, 2 new data-layer tests (dashboard 49) + 1 new release pin (release 27) —
`ci:pages` green, 0 extra Actions minutes. The i18n parity test caught a real
placeholder mismatch (`{entity}`/`{label}`) before the commit.

### Wave 22 — dispute view & time window — ✅ implemented (2026-10-21)

- **Dispute view** (`Filters.dispute`): one click reduces the network to exactly the
  pairs whose claims **really** clash — both claims of the pair stay visible, everything
  else goes. Developments (separate windows) and corroboration (same type) are **not**
  narrowed, because they are not in dispute. The conflict set is derived from the slice
  **before** narrowing, so toggling is stable and the panel stays complete; when nothing
  is contested the view says so honestly instead of just going blank.
- **Window of the currency check** (`Filters.fresh`, closed list 6 / 12 / 24 / 60 months,
  default 12): switchable in the panel with `aria-pressed`, spelled out in the permalink
  only when it differs from the default (`fresh=24`), off-vocabulary values dropped
  rather than guessed. "Stale" therefore always means one defined thing.
- Together they answer the question that precedes every phone call: *what is contested,
  and how thin is the file underneath it?*

**Delivered (Wave 22)**: `FRESH_WINDOWS`/`isFreshWindow`/`DEFAULT_FRESH_MONTHS`,
`Filters.dispute`, narrowing as the last step in `filterDataset()`, `fresh`+`dispute` in
the permalink (both writers), worker judges conflicts and currency on the slice before
narrowing, toggles in the conflict panel and window buttons in the currency panel,
10 new DE/EN keys, token-only CSS, 2 new data-layer tests (dashboard 51) + 1 new release
pin (release 28) — `ci:pages` green, 0 extra Actions minutes. Two wave-21 pins moved to
stable patterns because wave 22 extends the same lines.

### Wave 23 — how much the file can carry — ✅ implemented (2026-10-21)

- **Two honest measures instead of an invented score**: per entity the documents, the
  number of **independent** sources and the newest document are put side by side. Six
  named states follow: **no document**, **thin and old** (the combination that needs a
  phone call), **single source** (thin but current), **outdated** (corroborated but old),
  **undated** (corroborated but undateable) and **solid**.
- **Unnamed documents back a claim but never corroborate independently** (Wave 18's
  rule): three nameless documents stay one source. Documents that arrive through a
  visible relationship count (Wave 20's rule) — otherwise an entity would read "no
  document" only because the export attached the document to the edge. Both rules live
  in **one** exported function (`nodeBacking`) shared by panel and filter.
- **Thin-file filter** (`Filters.weak`): narrows to entities with at most one
  independent source — deliberately **clock-free** so it stays reproducible; the
  two-dimensional judgment (thin *and* old) stays with the panel.
- **CSV export** of the follow-up list (RFC-4180, same quoting helper as every other
  export) plus its own bilingual report section. Like conflicts and the registry, the
  panel is computed on the slice **before** narrowing, so narrowing never distorts its
  own judgment.

**Delivered (Wave 23)**: `nodeBacking()`, `evidenceRisk()`, `riskToCSV()`,
`Filters.weak` as the last step in `filterDataset()`, `weak=1` in the permalink (both
writers), risk panel with state list, CSV button, toggle and an honest all-solid state,
report section, 18 new DE/EN keys, token-only CSS, 3 new data-layer tests (dashboard 54)
+ 1 new release pin (release 29) — `ci:pages` green, 0 extra Actions minutes. The wave-22
pin moved to a behaviour pattern instead of pinning a line.

### Wave 24 — change comparison — ✅ implemented (2026-10-21)

- **What is new?** An older export can be loaded as the **comparison state**; the app then
  shows against the current state: new and vanished entities, new and vanished
  relationships, new documents, **newly contradictory pairs**, **resolved
  contradictions** and entities **now covered by two independent sources**.
- **Identity deliberately strict**: entities by id, relationships by the **directed**
  triple of source/type/target, documents by id. A renamed entity is neither new nor
  gone, and a second document for a known relationship is **not** a new relationship —
  exactly the confusion that makes monitoring useless.
- **Same rules as everywhere**: contradictions and independence are judged with the same
  functions the panels use (`pairConflicts`, `nodeBacking`), so "newly contradictory" and
  "now corroborated" mean exactly what they mean elsewhere.
- **Worker, not the UI thread**: the comparison state is parsed and diffed in the worker
  (like every import), so even 64 MiB cannot block the browser. The diff is a pure
  function, clock-free, deterministic, and bounded per list (counts stay complete).
- **Change list as CSV** (RFC-4180, reused quoting helper) for hand-over; the comparison
  can be removed at any time with one click.

**Delivered (Wave 24)**: `diffDatasets()`, `diffToCSV()`, `DatasetDiff`,
`WorkerRequest`/`WorkerResponse` `diff`, comparison-file button in the source strip,
change panel (counts, new/vanished entities, corroboration, new contradictions, resolved
ones, CSV, remove), 17 new DE/EN keys, token-only CSS, 1 new data-layer test
(dashboard 55) + 1 new release pin (release 30) — `ci:pages` green, 0 extra Actions
minutes.

### Wave 25 — searching the case — ✅ implemented (2026-10-21)

- **One search field for the whole case**: entities, contradictions, sturdiness,
  sources and research notes. Every hit names its **kind** — so it is clear *why* it
  matched — and a click jumps to the right place: focus the entity, open the dispute
  view, apply the outlet filter.
- **One row per object**, under its most telling label: an entity that is also a
  sturdiness finding is listed as an entity and carries the finding as its detail —
  two rows for the same thing would be noise.
- **Weighting is declared, not accidental**: entity before contradiction before
  sturdiness before source before note at equal text score, then label and id. An empty
  query returns **nothing** instead of a data dump; results are bounded, deterministic,
  display groups excluded, and every part of the index is optional.
- **Note excerpts show the match**, not the beginning: the window is centred on the hit
  and contains the searched word in full whenever it fits — an excerpt that cuts exactly
  the word you searched for is worse than none.
- The search runs **in the browser** on the panels already loaded — 0 extra Actions
  minutes, no server, no second copy of the data.

**Delivered (Wave 25)**: `searchCase()`, `noteSnippet()`, `CaseHit`/`CaseKind`, search
field in the analysis section with kind, detail, jump targets and an honest
empty/no-hit text, 14 new DE/EN keys, token-only CSS, 2 new data-layer tests
(dashboard 57) + 1 new release pin (release 31) — `ci:pages` green, 0 extra Actions
minutes.

### Wave 26 — build gate location-independent (go-live blocker) — ✅ implemented (2026-10-21)

- **Finding**: the Cloudflare Pages check fails on **every** branch commit — since at
  least wave 16, long before this work. Reproduced in a clean copy: the documented
  command `cd ../.. && npm ci && npm run ci:pages` **works** (exit 0), but
  `npm run ci:pages` **from `apps/web`** dies with `Missing script: "ci:pages"`. That is
  exactly what happens when Cloudflare starts the build from the configured root
  directory (`apps/web`) — the most likely cause of the long-standing "Build failed".
- **Fixed in two places so no configuration can be wrong**:
  1. `apps/web` and `apps/worker` get a delegating same-name script (`ci:pages` /
     `ci:worker`) that switches to the repository root — the build is identical from
     **both** entry points.
  2. `scripts/pages-build.mjs` and `scripts/worker-build.mjs` anchor themselves to the
     repository (`import.meta.url` → `process.chdir`) instead of trusting the caller's
     working directory, so relative paths can never point at the wrong files.
- **Verified both ways**: `npm run ci:pages` from the root **and** from `apps/web` →
  exit 0 (87 files, 6.58 MiB); `npm run ci:worker` from the root **and** from
  `apps/worker` → exit 0.
- Documented in `cloudflare-builds.json` and `docs/operations.md` (including the failure
  signature `Missing script: "ci:pages"`) so the finding cannot get lost again.
- **0 extra Actions minutes**, and the go-live path is no longer blocked by a
  configuration trap.

**Delivered (Wave 26)**: workspace delegation in both workspaces, self-anchoring build
gates, documented entry points, 1 new release pin (release 32), `ci:pages` green from
the root and from `apps/web`.

### Wave 27 — watchlist — ✅ implemented (2026-10-21)

- **Mark the entities that should stay in view** — one click in the inspector, with
  `aria-pressed`. The list survives a reload (`localStorage` only), is capped at 200
  entries, is **sanitized on read** (no duplicates, no empty ids, numbers and objects are
  junk) and leaves the device only through a deliberate CSV export.
- **Sorted by urgency, not by insertion**: disputes first, then vanished entries, then
  findings (thin and old, outdated, single source …), then the rest — nothing is hidden.
  A watchlist that hides its problems is just a bookmark list.
- **Judged with the panels' rules**: disputes from `pairConflicts` (real contradictions
  only, never temporal developments), backing from `nodeBacking`, states from
  `evidenceRisk`. "No finding" honestly means *not in the follow-up list* — not "solid",
  which the risk report does not claim.
- **Vanished entries are named, not deleted**: when a watched entity drops out of the
  current export the list says so and keeps the id. Watching is case work and must not
  depend on the data at hand.
- **CSV export** of the short list (including the disputed relationship types) so it can
  be taken into a call — no server, no telemetry, 0 extra Actions minutes.

**Delivered (Wave 27)**: `WATCH_LIMIT`/`WATCH_STORAGE_KEY`/`readWatchlist()`/`toggleWatch()`,
`watchReport()`, `watchToCSV()`, inspector toggle, watch panel with urgency ordering,
per-entry removal, vanished hint and CSV, 16 new DE/EN keys, token-only CSS,
2 new data-layer tests (dashboard 59) + 1 new release pin (release 34) —
`ci:pages` green, 0 extra Actions minutes.

### Wave 28 — watch digest — ✅ implemented (2026-10-21)

- **The actual monitoring question**: wave 24 shows *everything* that changed — which in
  practice is mostly noise. Wave 28 narrows the comparison to **your watchlist**: *what
  changed among exactly the entities I am watching?*
- **A change index instead of a baseline**: `diffDatasets()` now also returns the
  **entity ids** per kind of change (new, vanished, corroborated, newly contradictory,
  relationship changed, newly documented). The digest is therefore a pure function on the
  loaded comparison — the 64 MiB baseline never has to be read again, and ticking an entry
  in the watchlist refreshes the digest **immediately**.
- **One row per entity, every kind kept**, ordered by urgency (new contradiction →
  vanished → new → second independent source → relationship → new document). Unchanged
  watches do not appear at all — a digest that lists everything is not a digest.
- **Same rule as everywhere**: a new document attached to a relationship documents **both
  ends** (the wave-23 rule) — otherwise the digest would call an entity unchanged although
  it has new evidence.
- **Honest limits**: the index is capped at 2 000 ids per kind (deduplicated, sorted) and
  the panel says so when it was truncated. CSV export for "what moved among my watches".

**Delivered (Wave 28)**: `DatasetDiff.watch`, `watchChanges()`, `watchChangesToCSV()`,
`WatchChange`/`WatchChangeKind`, digest section in the watch panel (urgency order, kind
chips, CSV, truncation hint, honest "nothing changed" state), 10 new DE/EN keys,
token-only CSS, 2 new data-layer tests (dashboard 61) + 1 new release pin (release 35) —
`ci:pages` green, 0 extra Actions minutes.

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
| 13 comparison/multi-failure | browser/web worker | **0** | ✅ implemented |
| 14 sessions/provenance export | browser/web worker | **0** | ✅ implemented |
| 15 notes roundtrip | browser | **0** | ✅ implemented |
| 16 note index/case bundle | browser | **0** | ✅ implemented |
| 17 provenance gaps/bundle import | browser/web worker | **0** | ✅ implemented |
| 18 evidence quality/shared documents | browser/web worker | **0** | ✅ implemented |
| 19 evidence-quality filter | browser/worker | **0** | ✅ implemented |
| 20 source registry | browser/worker | **0** | ✅ implemented |
| 21 contradictions/currency | browser/worker | **0** | ✅ implemented |
| 22 dispute view/time window | browser/worker | **0** | ✅ implemented |
| 23 how much the file can carry | browser/worker | **0** | ✅ implemented |
| 24 change comparison | browser/worker | **0** | ✅ implemented |
| 25 case search | browser | **0** | ✅ implemented |
| 26 location-independent build gate | build/CI | **0** | ✅ implemented |
| 27 watchlist | browser (local) | **0** | ✅ implemented |
| 28 watch digest | browser | **0** | ✅ implemented |

No step creates new workflows, new cron jobs, longer jobs or paid services. The only
consumption-relevant limits remain the existing ones: 3 reservations/UTC day, 5-minute
job limit, 6 Gemini calls/day (shared), 500 Pages builds/month, 100,000 worker
requests/day.
