# 48-Stunden-Betriebsprotokoll

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

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

### 3.0 Schnellprüfung (ein Aufruf)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" -o /tmp/status.json
python3 - <<'PY'
import datetime, json
snap = json.load(open("/tmp/status.json"))
then = datetime.datetime.fromisoformat(snap["last_run"]["at"].replace("Z", "+00:00"))
now = datetime.datetime.now(datetime.timezone.utc)
print(f"Snapshot-Alter : {int((now - then).total_seconds() // 60)} min   (Limit 180)")
print(f"AuraDB-Knoten  : {snap['aura'].get('nodes')}  ({snap['aura'].get('node_percent')} %)   (Limit 180000)")
print(f"Workers/24 h   : {snap['workers'].get('requests_today')}   (Limit 100000)")
print(f"degraded       : {snap['degraded']}")
print(f"warnings       : {snap['warnings']}")
PY
```

KV-Writes stehen nicht in `/status.json` (siehe 3.1) und werden separat im Dashboard
abgelesen. Der Rest des Abschnitts erklärt jede Zahl und ihre Eskalation.

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
python3 - <<'PY'
import datetime, json
then = datetime.datetime.fromisoformat(json.load(open("/tmp/status.json"))["last_run"]["at"].replace("Z", "+00:00"))
age = int((datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() // 60)
print(f"Snapshot-Alter: {age} min")
PY
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

---

<a id="english"></a>

# 48-hour operations protocol (English)

Applies to the first **48 operating hours** after merging PR #3
(`arena/dbc5fb0c-puppetnet` → `main`). Goal: prove free-tier operation (€0.00),
not just claim it — every line is a check with a threshold, command and
escalation path. The table in § 6 is filled in as you go.

**Responsible:** Release Manager (lead), a second person as deputy.
**Time base:** all timestamps in UTC, as in Cloudflare and AuraDB.

---

## 1. Prerequisites (before T+0)

Without these points all following checks are moot:

- [ ] MP8 executed: `PUPPETNET_RATE_LIMIT` and `PUPPETNET_RESULTS` exist, `id`
      **and** `preview_id` are in `apps/worker/wrangler.toml` (no more 32 zeros).
- [ ] `npx wrangler deploy` successful; `GET /health` returns `ok: true`,
      `bindings.rate_limit_kv` and `bindings.result_kv` are `true`.
- [ ] `STATUS_TOKEN` lives in the password manager, not in the shell history.
- [ ] `GET /status` answers **401** without a token.
- [ ] Cron triggers visible in the dashboard: `0 */12 * * *` and `23 */3 * * *`
      (Wrangler 4 has no `triggers list`; Dashboard → Worker → Settings → Trigger Events).
- [ ] Access to the Cloudflare dashboard (KV metrics), the AuraDB console and `curl`/`jq` available.

On the command line:

```bash
export BASE='https://puppetnet-edge-relay.<subdomain>.workers.dev'
read -rs STATUS_TOKEN          # input without echo, does not land in the history
export STATUS_TOKEN
```

---

## 2. Hard thresholds

| Metric | Free limit | Green | Yellow (watch) | Red (escalate) |
| --- | --- | --- | --- | --- |
| KV writes per day | 1,000 | < 700 | 700–900 | > 900 |
| AuraDB nodes | 200,000 (app stop: 180,000) | < 162,000 (90 %) | 162,000–175,000 | > 175,000 |
| Snapshot age (`last_run.at`) | cron every 3 h | < 2 h 30 | 2 h 30–3 h | > 3 h |
| Workers requests per day | 100,000 | < 80,000 | 80,000–95,000 | > 95,000 |
| Last keep-alive | cron every 12 h | < 12 h | 12–13 h | > 13 h |

The 90 % mark is the worker's own warning threshold: `snapshot.warnings` reports
it, and the pipeline stops at `AURA_NODE_STOP=180000` — **before** the Aura limit
of 200,000. The 180k limit may only be reached with prior announcement to project
leadership.

---

## 3. The three mandatory checks

### 3.0 Quick check (one call)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" -o /tmp/status.json
python3 - <<'PY'
import datetime, json
snap = json.load(open("/tmp/status.json"))
then = datetime.datetime.fromisoformat(snap["last_run"]["at"].replace("Z", "+00:00"))
now = datetime.datetime.now(datetime.timezone.utc)
print(f"Snapshot age   : {int((now - then).total_seconds() // 60)} min   (limit 180)")
print(f"AuraDB nodes   : {snap['aura'].get('nodes')}  ({snap['aura'].get('node_percent')} %)   (limit 180000)")
print(f"Workers/24 h   : {snap['workers'].get('requests_today')}   (limit 100000)")
print(f"degraded       : {snap['degraded']}")
print(f"warnings       : {snap['warnings']}")
PY
```

KV writes are not in `/status.json` (see 3.1) and are read separately in the
dashboard. The rest of this section explains every number and its escalation.

### 3.1 KV write quota (< 1,000/day)

`/status.json` deliberately contains **no** write counter — KV counts
server-side, and the worker would have to write itself for every count (circular
argument). The source is the dashboard:

1. Cloudflare Dashboard → **Workers & Pages → KV**.
2. Namespace `PUPPETNET_RATE_LIMIT` → **Metrics** → period *Last 24 hours* → `Writes`.
3. The same for `PUPPETNET_RESULTS`.
4. Note the sum of both namespaces.

Expected value (worker-hardening § 7): ~30/day from the crons, up to 288/day from
the graph bucket sync (`GRAPH_KV_SYNC_INTERVAL_MS = 300000`) and up to 288/day if
someone calls `/status?live=1` every 5 minutes — typically **< 600**, ceiling
**1,000**.

If the GraphQL analytics access (`CF_API_TOKEN`, scope *Account Analytics: Read*)
is enabled, the dataset `kvOperationsAdaptiveGroups` delivers the same number
machine-readably; the dashboard value is authoritative.

### 3.2 AuraDB nodes (< 180,000, warning from 90 %)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" \
  | jq '{nodes: .aura.nodes, percent: .aura.node_percent,
         relationships: .aura.relationships, rel_percent: .aura.relationship_percent,
         available: .aura.available, error: .aura.error, warnings: .warnings}'
```

If `aura.available` is false, first `POST /status/refresh` (live probe), then read
again. Cross-check directly in Aura:

```cypher
MATCH (n) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
```

Both numbers belong in the protocol table. If nodes rise by more than 5,000 per
12-hour window, check the next ingestion run before the 90 % mark falls.

### 3.3 Status snapshot age (< 3 hours)

```bash
curl -fsS -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" -o /tmp/status.json
python3 - <<'PY'
import datetime, json
then = datetime.datetime.fromisoformat(json.load(open("/tmp/status.json"))["last_run"]["at"].replace("Z", "+00:00"))
age = int((datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() // 60)
print(f"Snapshot age: {age} min")
PY
```

The health cron writes every three hours (`23 */3 * * *`), the keep-alive every
twelve (`0 */12 * * *`). An age over 3 h means: **at least one cron run failed or
was blocked by the KV lock.** Check in this order:

1. `POST /status/refresh` — produces a snapshot immediately (only if the stored one
   is older than 5 min) and shows whether AuraDB and Neo4j are reachable.
2. Dashboard → Worker → Logs / `npx wrangler tail` — errors of the `scheduled()` handler.
3. Dashboard → Trigger Events — are both cron expressions still registered?
4. `keepalive_locked` in the snapshot: a hanging lock (600 s keep-alive, 900 s health)
   releases itself; if it stays longer, check the KV clock (`expirationTtl`).

A snapshot age of 2 h 55 min is **normal** (cron every 3 h); only from 3 h is it an
incident. The refresh from step 1 is itself a KV write and counts in § 3.1.

---

## 4. Further mandatory signals

| Check | Command / location | Expectation |
| --- | --- | --- |
| Liveness & version | `curl -fsS "$BASE/health" \| jq` | `ok: true`, `version: "1.7.0"`, `crons` correct |
| Auth tightness | `curl -s -o /dev/null -w '%{http_code}' "$BASE/status"` | `401` |
| Graph API | `curl -fsS "$BASE/graph/health" \| jq '.graph.counts'` | node/count numbers rise plausibly |
| Keep-alive | `.keepalive` in `/status.json` | `ok: true`, `at` < 13 h |
| Workers requests | `.workers.requests_today`, `.workers.source` | < 80,000, source `cloudflare_graphql` |
| Degradation | `.degraded`, `.warnings` | empty, except documented notes |
| Alerts | `.alert` in `/status.json` | no continuous fire; `skipped: throttled` is normal (6-h throttle) |
| Cron execution | Dashboard → Trigger Events / Logs | both schedules, no `auth_locked` |

`degraded` is the most important row: it summarizes "KV not bound", failed
keep-alive, missing Workers analytics and unreachable AuraDB. Every entry is
acknowledged in the protocol with cause and measure.

---

## 5. Schedule

| Checkpoint | Time (UTC) | Focus |
| --- | --- | --- |
| T+0 | directly after the merge | deploy live check, first snapshot, `/health`, `/status` 401 |
| T+4 h | +4 h | first full cron cycle, KV writes, snapshot age |
| T+12 h | after the 12-h keep-alive (`00:00`/`12:00`) | heartbeat node, keep-alive, KV writes |
| T+24 h | +24 h | daily balance: Workers requests, KV writes/24 h, Aura growth |
| T+36 h | +36 h | third health cron, lock stability, alert behaviour |
| T+48 h | +48 h | final balance, acceptance (§ 7) |

Each checkpoint takes ~5 minutes: execute § 3.1–3.3, review § 4, fill the table
row. Escalate deviations immediately; do not collect them "until the next
checkpoint".

---

## 6. Protocol

| Checkpoint | Time (UTC) | Snapshot age | KV writes/24 h | Aura nodes (%) | Requests/24 h | `degraded`/`warnings` | OK? | Initials |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T+0 | | | | | | | | |
| T+4 h | | | | | | | | |
| T+12 h | | | | | | | | |
| T+24 h | | | | | | | | |
| T+36 h | | | | | | | | |
| T+48 h | | | | | | | | |

Notes on deviations (time, metric, cause, measure, duration):

```
…
```

---

## 7. Escalation

| Symptom | Likely cause | Immediate measure |
| --- | --- | --- |
| Snapshot age > 3 h | cron failed, lock hangs, deploy without `[triggers]` | `POST /status/refresh`; check Trigger Events; `wrangler tail` |
| `kv.bound: false` | KV binding missing in the deploy | check `wrangler.toml`, deploy again; locks fall back to isolate memory |
| KV writes > 900/day | graph bucket sync too frequent, refresh loop | **increase** `GRAPH_KV_SYNC_INTERVAL_MS` (do not lower), stop manual refreshes |
| Aura nodes > 162,000 (90 %) | ingestion runs faster than planned | throttle the ingestion tier; inform project leadership **before** 180,000 is reached |
| Aura nodes > 175,000 | hard stop approaching | pause ingestion immediately (`DRY_RUN`/disable workflow), plan a cleanup run |
| `aura.available: false`, `neo4j_error` | password/URI wrong or instance paused | open the Aura console, wake the instance; check the `NEO4J_*` secrets |
| Keep-alive > 13 h | cron or DB access disturbed | Aura pauses after **72 h without a write** — set the heartbeat manually (see below) |
| `/status` returns 200 without token | `STATUS_PUBLIC=true` | deliberate decision? otherwise fix the secret/var and redeploy |
| `alert.skipped: telegram_error` | bot not in the chat, wrong chat ID, 3xx | check the chat ID, add the bot; details are redacted in the JSON |
| 1027 / 511 | daily Workers quota exhausted | wait until 00:00 UTC; the Python heartbeat takes over keeping AuraDB awake |

**Manual heartbeat** (only if the cron provably does not run):

```bash
# writes the real heartbeat node to AuraDB; locally, without deployment
npm run provision:worker -- --test-crons --require-heartbeat
# more targeted, without the local worker:
cypher-shell -a "$NEO4J_URI" -u neo4j -p "$NEO4J_PASSWORD" \
  "MERGE (h:_PuppetNetHeartbeat {id:'puppetnet-edge-cron'}) SET h.last_seen = datetime(), h.manual = true;"
```

The second command is a real write and resets the 72-hour pause.

---

## 8. Conclusion after 48 hours

- [ ] All six checkpoints are filled in and green or acknowledged with a documented measure.
- [ ] KV writes stayed under 1,000 on **every** day (sum of both namespaces).
- [ ] AuraDB nodes stayed under 162,000 (90 %); no unannounced rise above 175,000.
- [ ] Snapshot age was < 3 h at every measurement.
- [ ] `/status` was 401 without a token throughout, `/health` 200 throughout.
- [ ] No `degraded` entry stayed open longer than one checkpoint.
- [ ] Result noted: free-tier operation confirmed / throttling needed / escalation open.

| Conclusion | Date | Name | Decision |
| --- | --- | --- | --- |
| 48-h review | | | |

After the review the `/status` page remains the ongoing operations monitor; this
protocol is only filled in for the first 48 hours and archived afterwards.
