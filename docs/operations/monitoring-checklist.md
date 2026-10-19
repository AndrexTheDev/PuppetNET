# 48-Stunden-Betriebsprotokoll

Gilt für die ersten **48 Betriebsstunden** nach dem Merge von PR #3 (`arena/dbc5fb0c-puppetnet` → `main`).
Ziel: den Free-Tier-Betrieb (0,00 €) belegen, nicht nur behaupten — jede Zeile ist eine
Prüfung mit Schwelle, Befehl und Eskalationsweg. Ausgefüllt wird die Tabelle in § 6.

**Verantwortlich:** Release Manager (Federführung), eine zweite Person als Stellvertretung.
**Zeitbasis:** alle Zeitstempel in UTC, wie in Cloudflare und AuraDB.

---

## 1. Voraussetzungen (vor T+0)

Ohne diese Punkte sind alle folgenden Prüfungen gegenstandslos:

- [ ] MP8 ausgeführt: `PUPPETNET_RATE_LIMIT` und `PUPPETNET_RESULTS` existieren, `id` **und**
      `preview_id` stehen in `apps/worker/wrangler.toml` (keine 32 Nullen mehr).
- [ ] `npx wrangler deploy` erfolgreich; `GET /health` liefert `ok: true`,
      `bindings.rate_limit_kv` und `bindings.result_kv` sind `true`.
- [ ] `STATUS_TOKEN` liegt im Passwortmanager, nicht in der Shell-Historie.
- [ ] `GET /status` antwortet ohne Token mit **401**.
- [ ] Cron-Trigger im Dashboard sichtbar: `0 */12 * * *` und `23 */3 * * *`
      (Wrangler 4 kennt kein `triggers list`; Dashboard → Worker → Settings → Trigger Events).
- [ ] Zugriff auf Cloudflare-Dashboard (KV-Metriken), AuraDB-Konsole und `curl`/`jq` vorhanden.

Auf der Kommandozeile:

```bash
export BASE='https://puppetnet-edge-relay.<subdomain>.workers.dev'
read -rs STATUS_TOKEN          # Eingabe ohne Echo, landet nicht in der History
export STATUS_TOKEN
```

---

## 2. Harte Schwellen

| Kennzahl | Free-Limit | Grün | Gelb (beobachten) | Rot (eskalieren) |
| --- | --- | --- | --- | --- |
| KV-Writes pro Tag | 1 000 | < 700 | 700–900 | > 900 |
| AuraDB-Knoten | 200 000 (App-Stopp: 180 000) | < 162 000 (90 %) | 162 000–175 000 | > 175 000 |
| Alter des Snapshots (`last_run.at`) | Cron alle 3 h | < 2 h 30 | 2 h 30–3 h | > 3 h |
| Workers-Requests pro Tag | 100 000 | < 80 000 | 80 000–95 000 | > 95 000 |
| Letzter Keep-Alive | Cron alle 12 h | < 12 h | 12–13 h | > 13 h |

Die 90-%-Marke ist die Warnschwelle des Workers selbst: `snapshot.warnings` meldet sie,
und die Pipeline stoppt bei `AURA_NODE_STOP=180000` — **vor** dem Aura-Limit von 200 000.
Das 180k-Limit darf nur mit vorheriger Ankündigung an die Projektleitung erreicht werden.

---

## 3. Die drei Pflichtprüfungen

### 3.1 KV-Write-Kontingent (< 1 000/Tag)

`/status.json` enthält bewusst **keinen** Write-Zähler — KV zählt serverseitig, und der
Worker müsste für jede Zählung selbst schreiben (Zirkelschluss). Quelle ist das Dashboard:

1. Cloudflare Dashboard → **Workers & Pages → KV**.
2. Namespace `PUPPETNET_RATE_LIMIT` → **Metrics** → Zeitraum *Last 24 hours* → `Writes`.
3. Dasselbe für `PUPPETNET_RESULTS`.
4. Summe beider Namespaces notieren.

Erwartungswert (worker-hardening § 7): ~30/Tag aus den Crons, bis zu 288/Tag aus dem
Graph-Bucket-Sync (`GRAPH_KV_SYNC_INTERVAL_MS = 300000`) und bis zu 288/Tag, wenn jemand
`/status?live=1` im 5-Minuten-Takt aufruft — typisch **< 600**, Obergrenze **1 000**.

Wenn der GraphQL-Analytics-Zugang (`CF_API_TOKEN`, Scope *Account Analytics: Read*)
freigeschaltet ist, liefert das Dataset `kvOperationsAdaptiveGroups` dieselbe Zahl
maschinell; der Dashboard-Wert ist maßgeblich.

### 3.2 AuraDB-Knoten (< 180 000, Warnung ab 90 %)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" \
  | jq '{nodes: .aura.nodes, percent: .aura.node_percent,
         relationships: .aura.relationships, rel_percent: .aura.relationship_percent,
         available: .aura.available, error: .aura.error, warnings: .warnings}'
```

Wenn `aura.available` false ist, zuerst `POST /status/refresh` (Live-Probe), dann erneut
lesen. Gegenprobe direkt in Aura:

```cypher
MATCH (n) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
```

Beide Zahlen gehören in die Protokolltabelle. Steigen die Knoten um mehr als 5 000 pro
12-Stunden-Fenster, ist der nächste Ingestion-Lauf zu prüfen, bevor die 90-%-Marke fällt.

### 3.3 Alter des Status-Snapshots (< 3 Stunden)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" -o /tmp/status.json
AGE=$(( $(date -u +%s) - $(date -u -d "$(jq -r '.last_run.at' /tmp/status.json)" +%s) ))
echo "Snapshot-Alter: $((AGE/60)) min"
```

Der Health-Cron schreibt alle drei Stunden (`23 */3 * * *`), der Keep-Alive alle zwölf
(`0 */12 * * *`). Ein Alter über 3 h heißt: **mindestens ein Cron-Lauf ist ausgefallen
oder wurde vom KV-Lock blockiert.** Reihenfolge der Prüfung:

1. `POST /status/refresh` — erzeugt sofort einen Snapshot (nur wenn der gespeicherte
   älter als 5 min ist) und zeigt, ob AuraDB und Neo4j erreichbar sind.
2. Dashboard → Worker → Logs / `npx wrangler tail` — Fehler des `scheduled()`-Handlers.
3. Dashboard → Trigger Events — sind beide Cron-Ausdrücke noch eingetragen?
4. `keepalive_locked` im Snapshot: ein hängender Lock (600 s Keep-Alive, 900 s Health)
   löst sich von selbst; bleibt er länger, ist die KV-Uhr (`expirationTtl`) zu prüfen.

Ein Snapshot-Alter von 2 h 55 min ist **normal** (Cron alle 3 h); erst ab 3 h ist es ein
Vorfall. Der Refresh aus Schritt 1 ist selbst ein KV-Write und zählt in § 3.1.

---

## 4. Weitere Pflichtsignale

| Prüfung | Befehl / Ort | Erwartung |
| --- | --- | --- |
| Liveness & Version | `curl -fsS "$BASE/health" \| jq` | `ok: true`, `version: "1.7.0"`, `crons` korrekt |
| Auth-Dichtigkeit | `curl -s -o /dev/null -w '%{http_code}' "$BASE/status"` | `401` |
| Graph-API | `curl -fsS "$BASE/graph/health" \| jq '.graph.counts'` | Knoten/Zahlen steigen plausibel |
| Keep-Alive | `.keepalive` in `/status.json` | `ok: true`, `at` < 13 h |
| Workers-Requests | `.workers.requests_today`, `.workers.source` | < 80 000, Quelle `cloudflare_graphql` |
| Degradierung | `.degraded`, `.warnings` | leer, außer dokumentierten Hinweisen |
| Alarme | `.alert` in `/status.json` | kein Dauerfeuer; `skipped: throttled` ist normal (6-h-Drossel) |
| Cron-Ausführung | Dashboard → Trigger Events / Logs | beide Schedules, kein `auth_locked` |

`degraded` ist die wichtigste Zeile: sie fasst „KV nicht gebunden", fehlgeschlagenen
Keep-Alive, fehlende Workers-Analytics und nicht erreichbare AuraDB zusammen. Jeder
Eintrag wird im Protokoll mit Ursache und Maßnahme quittiert.

---

## 5. Zeitplan

| Checkpoint | Zeit (UTC) | Fokus |
| --- | --- | --- |
| T+0 | direkt nach dem Merge | Deploy-Live-Check, erster Snapshot, `/health`, `/status` 401 |
| T+4 h | +4 h | erster vollständiger Cron-Zyklus, KV-Writes, Snapshot-Alter |
| T+12 h | nach dem 12-h-Keep-Alive (`00:00`/`12:00`) | Heartbeat-Knoten, Keep-Alive, KV-Writes |
| T+24 h | +24 h | Tagesbilanz: Workers-Requests, KV-Writes/24 h, Aura-Wachstum |
| T+36 h | +36 h | dritter Health-Cron, Stabilität der Sperren, Alarmverhalten |
| T+48 h | +48 h | Abschlussbilanz, Abnahme (§ 7) |

Jeder Checkpoint dauert ~5 Minuten: § 3.1–3.3 ausführen, § 4 sichten, Tabellenzeile
ausfüllen. Abweichungen sofort eskalieren, nicht „bis zum nächsten Checkpoint" sammeln.

---

## 6. Protokoll

| Checkpoint | Zeit (UTC) | Snapshot-Alter | KV-Writes/24 h | Aura Nodes (%) | Requests/24 h | `degraded`/`warnings` | OK? | Initialen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T+0 | | | | | | | | |
| T+4 h | | | | | | | | |
| T+12 h | | | | | | | | |
| T+24 h | | | | | | | | |
| T+36 h | | | | | | | | |
| T+48 h | | | | | | | | |

Notizen zu Abweichungen (Zeit, Kennzahl, Ursache, Maßnahme, Dauer):

```
…
```

---

## 7. Eskalation

| Symptom | Wahrscheinliche Ursache | Sofortmaßnahme |
| --- | --- | --- |
| Snapshot-Alter > 3 h | Cron ausgefallen, Lock hängt, Deploy ohne `[triggers]` | `POST /status/refresh`; Trigger Events prüfen; `wrangler tail` |
| `kv.bound: false` | KV-Binding fehlt im Deploy | `wrangler.toml` prüfen, erneut deployen; Locks fallen auf Isolate-Memory zurück |
| KV-Writes > 900/Tag | Graph-Bucket-Sync zu häufig, Refresh-Schleife | `GRAPH_KV_SYNC_INTERVAL_MS` **erhöhen** (nicht senken), manuelle Refreshes einstellen |
| Aura Nodes > 162 000 (90 %) | Ingestion läuft schneller als geplant | Ingestion-Tier drosseln; Projektleitung informieren, **bevor** 180 000 erreicht wird |
| Aura Nodes > 175 000 | Hard Stop naht | Ingestion sofort pausieren (`DRY_RUN`/Workflow disable), Cleanup-Lauf planen |
| `aura.available: false`, `neo4j_error` | Passwort/URI falsch oder Instanz pausiert | Aura-Konsole öffnen, Instanz wecken; `NEO4J_*`-Secrets prüfen |
| Keep-Alive > 13 h | Cron oder DB-Zugriff gestört | Aura pausiert nach **72 h ohne Schreibzugriff** — Heartbeat manuell setzen (siehe unten) |
| `/status` liefert 200 ohne Token | `STATUS_PUBLIC=true` | bewusst entschieden? sonst Secret/Var korrigieren und neu deployen |
| `alert.skipped: telegram_error` | Bot nicht im Chat, falsche Chat-ID, 3xx | Chat-ID prüfen, Bot hinzufügen; Details sind redigiert im JSON |
| 1027 / 511 | Tageskontingent Workers erschöpft | bis 00:00 UTC warten; Python-Heartbeat übernimmt die AuraDB-Wachhaltung |

**Manueller Heartbeat** (nur wenn der Cron nachweislich nicht läuft):

```bash
# schreibt den echten Heartbeat-Knoten in AuraDB; lokal, ohne Deployment
npm run provision:worker -- --test-crons --require-heartbeat
# gezielter, ohne lokalen Worker:
cypher-shell -a "$NEO4J_URI" -u neo4j -p "$NEO4J_PASSWORD" \
  "MERGE (h:_PuppetNetHeartbeat {id:'puppetnet-edge-cron'}) SET h.last_seen = datetime(), h.manual = true;"
```

Der zweite Befehl ist ein echter Schreibzugriff und setzt die 72-Stunden-Pause zurück.

---

## 8. Abschluss nach 48 Stunden

- [ ] Alle sechs Checkpoints sind ausgefüllt und grün oder mit dokumentierter Maßnahme quittiert.
- [ ] KV-Writes lagen an **jedem** Tag unter 1 000 (Summe beider Namespaces).
- [ ] AuraDB-Knoten blieben unter 162 000 (90 %); kein unangekündigter Anstieg über 175 000.
- [ ] Snapshot-Alter war bei jeder Messung < 3 h.
- [ ] `/status` war durchgehend 401 ohne Token, `/health` durchgehend 200.
- [ ] Kein `degraded`-Eintrag blieb länger als einen Checkpoint offen.
- [ ] Ergebnis notiert: Free-Tier-Betrieb bestätigt / Drosselung nötig / Eskalation offen.

| Abschluss | Datum | Name | Entscheidung |
| --- | --- | --- | --- |
| 48-h-Review | | | |

Nach dem Review bleibt die `/status`-Seite der laufende Betriebsmonitor; dieses Protokoll
wird nur für die ersten 48 Stunden ausgefüllt und danach archiviert.
