# Ressourcenarmer Betrieb – aktueller Vertrag

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

## Phase 1: lokale Verarbeitung, begrenzte Cloud

`token_saver_nlp.py` entfernt Boilerplate, ganze Marketing-Füllzeilen und Duplikate.
Grammatische Stopwörter/Negation werden bewusst erhalten, damit spaCy keine falsche
Beziehungsrichtung erzeugt. NER läuft lokal mit dem deutschen spaCy-Modell; die
Quote „95 %“ ist ein Ziel, kein gemessener Qualitätswert. Nur syntaktisch begründete,
unklare Relationen dürfen optional in die Cloud. Standard: Cloud AUS.

Gemini: exaktes countTokens vor Generierung, höchstens 120 Eingabe- plus 50
Ausgabetokens (Eingabelimit 2026-10 wegen Tokenizer-Drift von 100 angehoben —
derselbe Prompt zählt auf aktuellen Modellen ~109; Gesamtbudget 150 unverändert),
drei Reservierungen/Lauf, sechs/UTC-Tag. Meldet der Anbieter keine
Nutzung oder scheitert ein Generierungsaufruf, wird die Nutzung als unbekannt
markiert, nicht als kostenlos behauptet. Logs enthalten Summen und keine Quelltexte
oder Zugangsschlüssel. Default-Modell seit 2026-10 ist `gemini-flash-lite-latest`
(QA-live-verifiziert); `gemini-1.5-flash` wurde von Google eingestellt (HTTP 404).
Modellnamen sind keine Verfügbarkeits-/Free-Tier-Garantie. Nicht unterstützte
Modelle werden abgelehnt.

Workflow: 03:00 + 15:00 UTC + Dispatch + manueller Start, opt-in `ENABLE_DAILY_STATIC=true`,
höchstens drei reservierte Läufe/UTC-Tag (zwei Cron-Slots + ein manueller Slot).
Hartes Joblimit 5 Minuten; intern 90s,
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

---

<a id="english"></a>

# Resource-saving operation – current contract

## Phase 1: local processing, bounded cloud

`token_saver_nlp.py` removes boilerplate, whole marketing filler lines and
duplicates. Grammatical stopwords/negation are deliberately preserved so spaCy
does not produce a wrong relation direction. NER runs locally with the German
spaCy model; the "95 %" quote is a target, not a measured quality value. Only
syntactically grounded, ambiguous relations may optionally go to the cloud.
Default: cloud OFF.

Gemini: exact countTokens before generation, at most 120 input plus 50 output
tokens (input limit raised from 100 in 2026-10 because of tokenizer drift — the
same bounded prompt counts ~109 on current models; total budget 150 unchanged),
three reservations/run, six/UTC day. If the provider reports no usage or a
generation call fails, usage is marked unknown, not claimed free. Logs contain
sums only, no source texts or credentials. Default model since 2026-10 is
`gemini-flash-lite-latest` (verified by a live QA probe); `gemini-1.5-flash`
was retired by Google (HTTP 404). Model names are no availability/free-tier
guarantee. Unsupported models are rejected.

Workflow: 03:00 + 15:00 UTC + dispatch + manual start, opt-in
`ENABLE_DAILY_STATIC=true`, at most three reserved runs/UTC day (two cron slots
+ one manual slot). Hard job limit 5 minutes; internally 90s, ingestion process
105s, export 45s. Installation and git operations need additional time; cold
installs can exceed the job limit and be aborted. The pip cache is an
optimization, not an availability guarantee. Redundant daily legacy
tests/keep-alive jobs are skipped when the new pipeline is enabled; PR/push
tests remain. This is not a permanent database heartbeat.

Exporter: bounded Neo4j read queries, automatic release per the
confidence/RSS policy, `graph_latest.json` + stable SHA256 ID files under
`data/entities/`. Limits: 5000 nodes, 20000 relationships; failed exports keep
the previous state.

## Phase 2: CDN and search relay

Dashboard/legacy use the CDN snapshot, profiles the same snapshot at build
time. Profile HTML is already static; it needs no additional runtime fetch. No
database access from the browser. The worker uses the Cache API, no KV bindings
and no KV writes. CORS is limited to explicitly configured origins; by default
exclusively `https://puppetnet-web.pages.dev`.

Secrets Actions: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, optional
`GEMINI_API_KEY`. Worker separately: `GH_PAT` and an independent
`SEARCH_AUTH_TOKEN` (>=32 characters). GitHub secrets are not automatically
transferred to Cloudflare! No legacy secret aliases. Never enter the PAT in the
research form.

HTTP 202 only confirms dispatch acceptance. Search filters the configured RSS
sources, not the whole internet. The local worker quota is per isolate; the
Git-persisted workflow quota is the authoritative daily reservation.
