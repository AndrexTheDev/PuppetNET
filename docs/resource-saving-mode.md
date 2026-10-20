# Ressourcenarmer Betrieb – aktueller Vertrag

## Phase 1: lokale Verarbeitung, begrenzte Cloud

`token_saver_nlp.py` entfernt Boilerplate, ganze Marketing-Füllzeilen und Duplikate.
Grammatische Stopwörter/Negation werden bewusst erhalten, damit spaCy keine falsche
Beziehungsrichtung erzeugt. NER läuft lokal mit dem deutschen spaCy-Modell; die
Quote „95 %“ ist ein Ziel, kein gemessener Qualitätswert. Nur syntaktisch begründete,
unklare Relationen dürfen optional in die Cloud. Standard: Cloud AUS.

Gemini: exaktes countTokens vor Generierung, höchstens 100 Eingabe- plus 50
Ausgabetokens, drei Reservierungen/Lauf, sechs/UTC-Tag. Meldet der Anbieter keine
Nutzung oder scheitert ein Generierungsaufruf, wird die Nutzung als unbekannt
markiert, nicht als kostenlos behauptet. Logs enthalten Summen und keine Quelltexte
oder Zugangsschlüssel. Gemini 1.5 Flash ist ein historischer Default, keine
Verfügbarkeits-/Free-Tier-Garantie. Nicht unterstützte Modelle werden abgelehnt.

Workflow: 03:00 UTC + Dispatch + manueller Start, opt-in `ENABLE_DAILY_STATIC=true`,
höchstens zwei reservierte Läufe/UTC-Tag. Hartes Joblimit 5 Minuten; intern 90s,
Ingestion-Prozess 105s, Export 45s. Installation und Git-Operationen brauchen
zusätzlich Zeit; kalte Installationen können das Joblimit überschreiten und
abgebrochen werden. Pip-Cache ist eine Optimierung, keine Verfügbarkeitsgarantie.
Redundante tägliche Legacy-Tests/Keep-alive-Jobs werden bei Aktivierung übersprungen;
PR-/Push-Tests bleiben erhalten. Das ist kein dauerhafter Datenbank-Heartbeat.

Exporter: begrenzte Neo4j-Leseabfragen, automatische Freigabe nach Confidence-/RSS-Policy,
`graph_latest.json` + stabile SHA256-ID-Dateien unter `data/entities/`. Grenzen:
5000 Knoten, 20000 Beziehungen; fehlerhafte Exporte behalten den bisherigen Stand.

## Phase 2: CDN und Search-Relay

Dashboard/Legacy verwenden den CDN-Snapshot, Profile denselben Snapshot zur Buildzeit.
Profil-HTML ist schon statisch, es braucht keinen erneuten Laufzeitfetch. Kein
Datenbankzugang vom Browser. Der Worker verwendet Cache API, keine KV-Bindings oder
KV-Schreibzugriffe. CORS wird auf explizit konfigurierte Origins beschränkt, im
Default ausschließlich `https://puppetnet-web.pages.dev`.

Secrets Actions: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, optional `GEMINI_API_KEY`.
Worker separat: `GH_PAT` und unabhängiges `SEARCH_AUTH_TOKEN` (>=32 Zeichen).
GitHub-Secrets werden nicht automatisch zu Cloudflare übertragen! Keine alten Secret-Aliasse. PAT niemals im Rechercheformular eingeben.

HTTP 202 bestätigt nur die Dispatch-Annahme. Die Suche filtert konfigurierte RSS-
Quellen, nicht das gesamte Internet. Die lokale Worker-Quote ist pro Isolate;
die Git-persistierte Workflow-Quote ist die maßgebliche Tagesreservierung.

## Phase 3: Darstellung und Export

Farben zentral in `apps/web/src/styles/palette.json`; `globals.css` ist der beim
SEO-Build synchronisierte CSS-Spiegel. WebGL und Leaflet importieren dieselbe
Palette. Mobile Banner sind fest reserviert, Schließen ist persistent und wird
bei blockiertem Storage abgefangen.

Dashboard startet in der Tabelle. Das GPU-Modul wird nur bei Netzwerkauswahl
geladen. ForceAtlas2 startet ausschließlich über den Layout-Knopf und wird beim
Verlassen des Netzwerks gestoppt. Legacy startet mit neuen Einstellungen im nicht
animierten Grid; alte bewusst gespeicherte Einstellungen können davon abweichen.
Einmaliges JSON-Parsing, Filter, DOM/WebGL-Rendering bleiben Browser-Rechenarbeit.
Es findet keine NLP-/Modell-Inferenz im Browser statt; wörtliche 0 % CPU ist unmöglich.

SEO läuft über npm prebuild und auch bei direktem next build über Next-Konfiguration:
Sitemap, Robots, OG-Bilder, Metadatenmanifest; Profil-HTML rendert OG/Schema.org.
SITE_ORIGIN hat Vorrang, Default ist die Pages-Produktionsdomain.

## Deployment und Kosten

Produktive Datencommits gehen direkt nach `main` mit `GH_PAT`. Die initiale
Codefreigabe erfolgt per PR aus dem Entwicklungsbranch; anschließend sind keine
Daten-PRs nötig. Pages muss main beobachten. Details und Datenschutz-Vetos:
[Daily ingestion](daily-ingestion.md).

~100 Actions-Minuten/Monat sind ein Planungsziel. 31 tägliche Läufe à 5 Minuten
wären bereits 155 Minuten, manuelle Läufe/CI kommen hinzu. Kein hartes Monatslimit
und keine garantierten 0 €. Provider-Kontingente/Billing beobachten; keine bezahlten
Tarife automatisch aktivieren. CDN kann den letzten Snapshot unabhängig von einer
laufenden Pipeline ausliefern, aber es gibt keine 24/7-SLA-Zusage.
