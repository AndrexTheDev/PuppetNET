# Daily ingestion – automatische Freigabe und Produktion auf main

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

## Einmalige Aktivierung

1. Code-PR auf `main` integrieren. Die Entwicklungssitzung arbeitet weiterhin
   ausschließlich auf der jeweiligen Session-Branch; der produktive Workflow selbst
   checkt `main` aus und hat keine Arena-Branch-Abhängigkeit.
2. Repository-Secrets: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `GH_PAT`.
   `GEMINI_API_KEY` nur für bewusst aktivierten Cloud-Fallback. Alte Secret-Namen
   werden nicht mehr als Aliasse akzeptiert.
3. `GH_PAT`: auf dieses Repository beschränkter Token mit Contents read/write.
   Branch-Regeln müssen den erwarteten Bot-Push zulassen. Keine Schutzregeln
   pauschal abschalten; bei Ablehnung scheitert der Job ohne Force-Push.
4. Repository-Variablen: `DAILY_RSS_FEEDS` mit echten deutschsprachigen RSS-URLs;
   optional `VERIFIED_RSS_FEEDS` als kommagetrennte Allowlist exakter HTTPS-Feed-URLs.
   Ein Feed ist nicht automatisch verifiziert, nur weil er eingelesen wird.
5. `ENABLE_GEMINI_FALLBACK=false` zunächst beibehalten. Für Auto-Publikation
   nach bewusster Prüfung der Policy `ENABLE_DAILY_STATIC=true` setzen.
6. Cloudflare Pages: Produktionsbranch `main`, Git-Integration aktiv und
   Datenpfade `apps/web/*` nicht aus dem Build-Watch ausschließen. Ledgerpfade
   `.pipeline-state/*` nicht als alleinigen Build-Trigger konfigurieren.

## Freigabepolicy

- Confidence >=0.75 ODER exakte verifizierte RSS-Provenienz → im statischen Export
  `public_released: true` und kompatibel `public: true`.
- Die Vertrauensentscheidung kommt ausschließlich aus `VERIFIED_RSS_FEEDS` und
  dem vom RSS-Adapter gespeicherten `Document.feed_url`. `source_id=news_world`,
  ein `verified_rss=true`-Wert oder der Domainname eines Artikels genügen nicht.
- `private`, `noindex`, `release_blocked`, `negated`, `hedged`, `calculated` und
  berechnete `PUPPET_MASTER_OF`-Kanten werden nicht automatisch veröffentlicht.
  `public_released: false` ist eine explizite Rücknahme und überstimmt Auto-Approval.
- `public: false` ist dagegen häufig nur der NLP-Draftdefault. Ein hinreichender
  Score bzw. verifizierte Herkunft darf diesen Default überwinden. Auch der
  automatische `requires_review`-Marker wird nur unter dieser Policy überwunden.
- Manuell bereits öffentliche Daten bleiben kompatibel. Keine Veröffentlichung
  von Freitext-Evidenz, Privatnotizen, Risk-Scores oder vollständigen DB-Properties.
- Release-Flags werden im Export erzeugt; der Exporter verändert Neo4j nicht.
  Ältere Dokumente ohne gespeicherte Feed-URL brauchen erneute Ingestion oder
  erfüllen die Confidence-Regel. Herkunft wird nicht aus der Artikel-URL geraten.

**Warnung:** Confidence misst Extraktionssicherheit, nicht objektive Wahrheit.
Die Aktivierung dieser Policy akzeptiert automatische öffentliche Publikation
und mögliche Fehler ohne vorgelagerte menschliche Prüfung.

## Lauf und Limits

Trigger: täglich 03:00 und 15:00 UTC, `repository_dispatch` mit `puppetnet-refresh`, manuell.
Nur Ausführungen auf `main`, opt-in. Gemeinsame Serialisierung mit Legacy-Ingestion.
Höchstens drei Git-persistierte Reservierungen/UTC-Tag: zwei Cron-Slots (sie
garantieren die zwei täglichen Läufe) plus genau ein manueller Slot, damit ein
Dispatch niemals einen Cron-Lauf verdrängt. GitHub kann Cron-Starts bei hoher
Last verzögern; maßgeblich bleibt das Ledger, nicht die Uhrzeit.
Reservierung wird VOR Modellinstallation und Datenbankarbeit auf main gesichert.

Hartes Joblimit 5 Minuten; Ingestion intern 90 Sekunden, Prozesslimit 105 Sekunden,
Export 45 Sekunden. Kalte Installation/Aura-Wakeup kann den Job trotzdem abbrechen.
Gemini standardmäßig aus, höchstens 120 Eingabe- + 50 Ausgabetokens pro Generierung,
3 Reservierungen/Lauf, 6/Tag. Default-Modell seit 2026-10: `gemini-flash-lite-latest`
(QA-live-verifiziert; `gemini-1.5-flash` ist eingestellt). Jeder Modellname ist
keine API-Verfügbarkeitszusage.

Exporter liest einen begrenzten Snapshot: maximal 5000 Entity-Kandidaten, 20000
Kanten, 5000 Dokumentmetadaten. `truncated` signalisiert erreichte Grenzen; keine
unbegrenzte Vollständigkeitsgarantie. Kanten brauchen beide veröffentlichten Enden.
Nur erlaubte öffentliche Metadaten und sichere HTTP(S)-Quellenlinks werden exportiert.
Stabile Slugs und SHA256-Dateinamen verhindern ID-Pfadmanipulation. Manifestgesteuertes
Pruning entfernt nur zuvor vom Exporter geschriebene, inzwischen veraltete Slices.

Ergebnis: `graph_latest.json`, `static-manifest.json`, `entities/<sha256-id>.json`.
Nur geänderte Inhalte werden committet; unveränderte Daten erzeugen keinen Datencommit.
Fehlgeschlagene Datenbankabfragen löschen nie den letzten Stand. Der produktive
Workflow setzt bewusst `--allow-empty`: ein erfolgreich gelesener Snapshot ohne
freigabefähige Entitäten veröffentlicht einen gültigen leeren Graphen und entfernt
alte Slices (wichtig bei vollständiger Rücknahme). Lokal bleibt die Leerexport-Sperre
ohne ausdrückliches `--allow-empty` aktiv.

## Automatischer Push

Checkout authentifiziert mit `GH_PAT`, Ziel ist fest `main`. Quota-/Cloud-Ledger
werden eng begrenzt separat gestaged, Daten nur unter `apps/web/public/data/`.
Keine Shell-Interpolation der Rechercheanfrage, kein `git add .`, kein Force-Push.
Wenn main parallel geändert wird, darf der Push mit Non-Fast-Forward scheitern;
keine fremden Commits werden überschrieben. Cloud-Ledger-Persistenz muss erfolgreich
sein, bevor neue Daten publiziert werden. Harte Jobabbrüche können Schluss-Schritte
verhindern; die frühe Laufreservierung bleibt trotzdem erhalten.

PAT-Datenpushes können den Cloudflare-Produktionsbuild auslösen. Ein erfolgreicher
Push ist kein Nachweis einer erfolgreichen Bereitstellung; Pages-Status prüfen.
Token-Ablauf, Branch-Regeln und deaktivierte Git-Integration können den Ablauf stoppen.

## Lokale Nutzung

```sh
NEO4J_URI='neo4j+s://…' NEO4J_USER='neo4j' NEO4J_PASSWORD='…' \
  python packages/pipeline/src/export_static_slices.py --neo4j

VERIFIED_RSS_FEEDS='https://deine-gepruefte-quelle.example/rss' \
  python packages/pipeline/src/export_static_slices.py --input graph.json

SITE_ORIGIN=https://puppetnet-web.pages.dev npm run ci:pages
```

Beispielwerte ersetzen. Keine echten Zugangsdaten in Shell-History oder Chat speichern;
für reale Nutzung Umgebungsvariablen über einen Secret-Manager setzen.

## Next.js-Leerexport

Next 15 verlangt für dynamische Routen mindestens einen Exportparameter. Bei null
Profilen wird ein reservierter Parameter verwendet, der ausschließlich notFound()
rendert. Der npm-postbuild-Schritt entfernt diesen verifizierten noindex-404-Stub
aus out/. Keine erfundene Entität und kein Sitemap-Eintrag werden veröffentlicht.
Für vollständige Release-Builds immer `npm run ci:pages` oder `npm run build` nutzen.
Wer Next direkt aufruft, führt danach `node scripts/finalize-static-export.mjs` aus.

---

<a id="english"></a>

# Daily ingestion – automatic release and production on main

## One-time activation

1. Merge the code PR into `main`. The development session keeps working
   exclusively on its session branch; the production workflow itself checks out
   `main` and has no arena-branch dependency.
2. Repository secrets: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `GH_PAT`.
   `GEMINI_API_KEY` only for the deliberately enabled cloud fallback. Legacy
   secret names are no longer accepted as aliases.
3. `GH_PAT`: token restricted to this repository with Contents read/write.
   Branch rules must allow the expected bot push. Do not disable protection
   rules wholesale; on rejection the job fails without force-push.
4. Repository variables: `DAILY_RSS_FEEDS` with real German-language RSS URLs;
   optionally `VERIFIED_RSS_FEEDS` as a comma-separated allowlist of exact
   HTTPS feed URLs. A feed is not automatically verified just because it is
   ingested.
5. Keep `ENABLE_GEMINI_FALLBACK=false` initially. For auto-publication, set
   `ENABLE_DAILY_STATIC=true` after a deliberate policy review.
6. Cloudflare Pages: production branch `main`, Git integration active, and data
   paths `apps/web/*` not excluded from the build watch. Do not configure the
   ledger paths `.pipeline-state/*` as the sole build trigger.

## Release policy

- Confidence >=0.75 OR exact verified RSS provenance → `public_released: true`
  in the static export and compatibly `public: true`.
- The trust decision comes exclusively from `VERIFIED_RSS_FEEDS` and the
  `Document.feed_url` stored by the RSS adapter. `source_id=news_world`, a
  `verified_rss=true` value or an article's domain name are not sufficient.
- `private`, `noindex`, `release_blocked`, `negated`, `hedged`, `calculated`
  and calculated `PUPPET_MASTER_OF` edges are never published automatically.
  `public_released: false` is an explicit withdrawal and overrides
  auto-approval.
- `public: false`, by contrast, is often just the NLP draft default. A
  sufficient score or verified provenance may override that default. The
  automatic `requires_review` marker is also overridden only under this policy.
- Data already published manually stays compatible. No publication of
  free-text evidence, private notes, risk scores or full DB properties.
- Release flags are produced in the export; the exporter does not modify Neo4j.
  Older documents without a stored feed URL need re-ingestion or must satisfy
  the confidence rule. Provenance is not guessed from the article URL.

**Warning:** confidence measures extraction certainty, not objective truth.
Activating this policy accepts automatic public publication and possible errors
without prior human review.

## Runs and limits

Triggers: daily at 03:00 and 15:00 UTC, `repository_dispatch` with
`puppetnet-refresh`, manual. Runs on `main` only, opt-in. Shared serialization
with legacy ingestion. At most three Git-persisted reservations per UTC day:
two cron slots (they guarantee the two daily runs) plus exactly one manual slot,
so a dispatch never displaces a cron run. GitHub may delay cron starts under
high load; the ledger stays authoritative, not the clock.
The reservation is persisted to main BEFORE model installation and database work.

Hard job limit 5 minutes; ingestion internally 90 seconds, process limit
105 seconds, export 45 seconds. Cold installation/Aura wake-up can still abort
the job. Gemini off by default, at most 120 input + 50 output tokens per
generation, 3 reservations/run, 6/day. Default model since 2026-10:
`gemini-flash-lite-latest`; any model name is not an API availability promise.

The exporter reads a bounded snapshot: at most 5000 entity candidates,
20000 edges, 5000 document metadata rows. `truncated` signals reached limits;
no unbounded completeness guarantee. Edges need both published endpoints. Only
allowed public metadata and safe HTTP(S) source links are exported. Stable
slugs and SHA256 filenames prevent ID path manipulation. Manifest-driven
pruning removes only previously exporter-written, now outdated slices.

Result: `graph_latest.json`, `static-manifest.json`, `entities/<sha256-id>.json`.
Only changed content is committed; unchanged data produces no data commit.
Failed database queries never delete the last state. The production workflow
deliberately passes `--allow-empty`: a successfully read snapshot without
releasable entities publishes a valid empty graph and removes old slices
(important for full withdrawal). Locally, the empty-export lock stays active
without explicit `--allow-empty`.

## Automatic push

Checkout authenticates with `GH_PAT`; the target is fixed to `main`.
Quota/cloud ledgers are staged separately and tightly bounded; data only under
`apps/web/public/data/`. No shell interpolation of the research query, no
`git add .`, no force-push. If main changed in parallel, the push may fail with
non-fast-forward; foreign commits are never overwritten. Cloud ledger
persistence must succeed before new data is published. Hard job aborts can
prevent final steps; the early run reservation still persists.

PAT data pushes can trigger the Cloudflare production build. A successful push
is not proof of a successful deployment; check the Pages status. Token expiry,
branch rules and disabled Git integration can stop the flow.

## Local usage

```sh
NEO4J_URI='neo4j+s://…' NEO4J_USER='neo4j' NEO4J_PASSWORD='…' \
  python packages/pipeline/src/export_static_slices.py --neo4j

VERIFIED_RSS_FEEDS='https://your-vetted-source.example/rss' \
  python packages/pipeline/src/export_static_slices.py --input graph.json

SITE_ORIGIN=https://puppetnet-web.pages.dev npm run ci:pages
```

Replace example values. Never store real credentials in shell history or chat;
for real use set environment variables via a secret manager.

## Next.js empty export

Next 15 requires at least one export parameter for dynamic routes. With zero
profiles, a reserved parameter is used that renders notFound() exclusively. The
npm postbuild step removes this verified noindex 404 stub from out/. No invented
entity and no sitemap entry is published. For full release builds always use
`npm run ci:pages` or `npm run build`. When calling Next directly, run
`node scripts/finalize-static-export.mjs` afterwards.
