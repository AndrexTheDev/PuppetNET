# Daily ingestion – automatische Freigabe und Produktion auf main

## Einmalige Aktivierung

1. Code-PR auf `main` integrieren. Die Entwicklungssitzung arbeitet weiterhin
   ausschließlich auf `arena/5fb0fe0b-puppetnet`; der produktive Workflow selbst
   checkt `main` aus und hat keine Arena-Branch-Abhängigkeit mehr.
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

Trigger: täglich 03:00 UTC, `repository_dispatch` mit `puppetnet-refresh`, manuell.
Nur Ausführungen auf `main`, opt-in. Gemeinsame Serialisierung mit Legacy-Ingestion.
Höchstens zwei Git-persistierte Reservierungen/UTC-Tag, davon höchstens ein Cron.
Reservierung wird VOR Modellinstallation und Datenbankarbeit auf main gesichert.

Hartes Joblimit 5 Minuten; Ingestion intern 90 Sekunden, Prozesslimit 105 Sekunden,
Export 45 Sekunden. Kalte Installation/Aura-Wakeup kann den Job trotzdem abbrechen.
Gemini standardmäßig aus, höchstens 100 Eingabe- + 50 Ausgabetokens pro Generierung,
3 Reservierungen/Lauf, 6/Tag. Historischer Modellname ist keine API-Verfügbarkeitszusage.

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
