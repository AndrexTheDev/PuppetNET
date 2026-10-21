# Gemini-Strategie: Free Tier voll nutzen, nie überschreiten

Stand: 21.10.2026. Alle Zahlen sind mit Quelle und Prüfdatum angegeben.
Google garantiert keine Free-Tier-Werte; maßgeblich sind immer die Werte, die
AI Studio für **dein** Projekt anzeigt (<https://aistudio.google.com/rate-limit>).

## 1. Kurzfassung

* **Der knappe Rohstoff im Free Tier sind Requests pro Tag (RPD), nicht Tokens.**
  PuppetNET hat bisher pro mehrdeutigem Satz einen Request mit ca. 110 Tokens
  geschickt, höchstens 6 pro Tag. Das sind rund **1 % der täglichen Requests**
  und weit unter 0,1 % der Token-Kapazität eines Flash-Lite-Modells.
* Deshalb gilt jetzt: **ein Request pro Dokument statt pro Satzpaar**. Gemini
  liest den ganzen Artikel (bis 12 000 Zeichen) und liefert alle Entitäten und
  typisierten Relationen als schema-validiertes JSON. Jede Relation wird danach
  lokal geprüft: Das Zitat muss wörtlich im Text stehen, beide Namen nennen und
  darf nicht verneint oder spekulativ sein.
* Ein zentraler **Budget-Broker** (`packages/pipeline/src/gemini_broker.py`)
  ist die einzige Tür zu Gemini. Er arbeitet mit Limits pro Modell, rechnet den
  Tag nach Pacific-Zeit und plant nur 80 % jedes Limits. Er lernt aus 429-Antworten
  und verteilt das Tagesbudget fair auf die Läufe. Jeder Request wird gebucht,
  *bevor* er gesendet wird.
* **Das öffentliche Repository ist ein echter Vorteil, aber nur für GitHub
  Actions, nicht für Gemini.** Standard-Runner-Minuten sind für öffentliche Repos
  kostenlos und unbegrenzt. Das neue **Public-Profil** nutzt das für bis zu 8
  geplante Läufe pro Tag mit je 30 Dokumenten. Mit Gemini hat das nichts zu tun:
  Die Gemini-Limits gelten pro Google-Cloud-Projekt, egal ob der Code öffentlich ist.
* Alles ist **opt-in**. Ohne neue Repository-Variablen verhält sich die Pipeline
  exakt wie vorher (2 Cron-Läufe, 5 Minuten, Gemini aus).

## 2. Geprüfte Fakten

### 2.1 Gemini API Free Tier (Prüfdatum 21.10.2026)

| Fakt | Quelle |
| --- | --- |
| Limits gelten **pro Projekt und pro Modell**, nicht pro API-Key. Dimensionen: RPM, Input-TPM, RPD (bei manchen Modellen TPD). | ai.google.dev/gemini-api/docs/rate-limits (Stand 14.10.2026) |
| RPD setzt um **Mitternacht Pacific Time** zurück. | ebd. |
| Google veröffentlicht keine festen Free-Tier-Zahlen mehr („in AI Studio ansehen“, „nicht garantiert“). | ebd. |
| Free Tier verfügbar u. a. für Gemini 3.x Flash, 3.5/3.1 Flash-Lite, 2.5-Modelle, `gemini-embedding-2` und Gemma 4 (nur kostenlos, 256K Kontext). | ai.google.dev/gemini-api/docs/pricing (Stand 19.10.2026) |
| Batch API, Context Caching für Flash-Lite und Search-Grounding für 3.x sind **nicht** im Free Tier. | ebd. |
| Gemessen per 429-`QuotaFailure` an einem neuen Projekt: Flash-Lite (3.5 und 3.1) **15 RPM / 500 RPD**, 3.x Flash **5 RPM / 20 RPD** (und langsam), Embedding **100 RPM / 1000 RPD**, 2.5-Modelle für Neukunden HTTP 404. | dev.to-Messung vom 02.09.2026 |
| QA in diesem Repo (20.10.2026): `gemini-flash-lite-latest` funktioniert. `gemini-1.5-flash` ist eingestellt (404). `gemini-2.5-flash` ist für neue Konten gesperrt. Thinking-Modelle liefen in das 60-s-Timeout. | `qa-report-api-connections.json` |
| Inhalte, die an **unbezahlte** Dienste gehen, darf Google zur Produktverbesserung nutzen. | ai.google.dev/gemini-api/terms |
| Die Nutzungsbedingungen verlangen bezahlte Dienste, wenn man „API Clients“ Nutzern im **EWR, in der Schweiz oder im UK** zur Verfügung stellt. | ebd. (gültig ab 23.03.2026) |

Ältere Tabellen im Netz („1500 RPD“, „1M TPM“ für Flash) sind veraltet und
werden hier bewusst **nicht** verwendet.

### 2.2 GitHub Actions im öffentlichen Repository

Geprüft: `gh api repos/AndrexTheDev/PuppetNET` liefert `"private": false`.

| Behauptung | Ergebnis |
| --- | --- |
| „Öffentliche Repos haben im Free Tier viel weniger Begrenzungen“ | **Stimmt für Actions-Minuten.** Standard-GitHub-Runner sind für öffentliche Repos kostenlos und unbegrenzt. Sie sind auch größer: 4 vCPU, 16 GB RAM, 14 GB SSD (privat: 2000 Min./Monat im Free-Plan). Larger Runner kosten immer Geld. |
| „Also beliebig viele Läufe“ | **Nein.** Es bleiben harte Grenzen: 6 h pro Job, 20 parallele Jobs, `GITHUB_TOKEN` 1000 API-Requests/h, Cron frühestens alle 5 Minuten. |
| Cron-Zuverlässigkeit | Geplante Läufe können verzögert oder **verworfen** werden, besonders zur vollen Stunde. Eine Messung (Okt. 2026) ergab: nur 64 % der 15-Minuten-Slots liefen, mit 7,4 Minuten medianer Verzögerung. Deshalb laufen die Zusatzslots um **:30**. |
| Inaktivität | In öffentlichen Repos deaktiviert GitHub geplante Workflows nach **60 Tagen ohne Repo-Aktivität**. Die Daten-Commits zählen als Aktivität; trotzdem regelmäßig prüfen. |
| Fair Use | Die Nutzungsbedingungen (gültig ab 27.08.2026) erlauben auf GitHub-Runnern nur Arbeit für „Produktion, Test, Deployment oder Veröffentlichung“ des Projekts. Sie verbieten unverhältnismäßige Last, z. B. als CDN. Eine Datenpipeline für die eigene statische Seite in moderater Frequenz ist plausibel abgedeckt. Das ist eine Graubereich-Einschätzung, keine Rechtsberatung. |

**Konsequenz:** Die alte Minuten-Rechnung („310 Minuten/Monat“) und das
5-Minuten-Joblimit als Sparmaßnahme sind für dieses Repo überholt. Die echten
Engpässe sind jetzt:

1. Gemini-RPD.
2. Cloudflare Pages: 500 Builds/Monat im Free-Plan, 1 Build gleichzeitig, 20 000 Dateien pro Deployment.
3. Kapazität von Neo4j Aura Free. Der Writer stoppt neue Entitäten ab 180 000 Knoten.
4. Höflichkeit gegenüber den Quellen.
5. Git-Repo-Größe durch häufige Daten-Commits.

## 3. Architektur

```
RSS-Dokument ──► spaCy (lokal, immer) ──► lokale Relationen (method=dependency)
      │                                          │
      └──► Broker ─► Gemini (1 Request/Dokument) ─► Validator ─► method=llm
                                                         │
                         Merge: lokales Duplikat → nur "corroborated_by"
                                                         ▼
                        Neo4j (Upsert: lokal überschreibt llm, nie umgekehrt)
                                                         ▼
                 Export: llm-Kanten nur mit public_released=true (Veto)
```

### 3.1 Broker-Regeln (`gemini_broker.py`)

| Regel | Umsetzung |
| --- | --- |
| Budget pro Modell und Projekt | Eigener Zähler pro Modell. Eine Kaskade (`gemini-flash-lite-latest` → `gemini-3.1-flash-lite`) verdoppelt die nutzbaren RPD, weil jedes Modell einen eigenen Topf hat. |
| Pacific-Tag | `pacific_day()` (America/Los_Angeles). Ohne tzdata wird UTC−8 verwendet; der Tag wechselt dann höchstens eine Stunde *zu spät*, also auf der sicheren Seite. |
| Sicherheitsabstand | Geplant werden nur `GEMINI_SAFETY_FACTOR` × Limit (Standard 0,8, maximal 0,95). |
| Limits | Werte aus `GEMINI_MODEL_LIMITS` (aus AI Studio übernommen), sonst die gemessenen Referenzwerte der Modellfamilie. **Pro- und unbekannte Modelle werden nie implizit aufgerufen.** Eine ungültige Konfiguration schaltet den Broker ab, statt zu raten. |
| Buchung vor dem Senden | Ledger `.pipeline-state/gemini-broker.json`, atomar geschrieben und per Git persistiert (auch bei Fehlschlag oder Timeout). Fehlgeschlagene und abgelehnte Requests zählen mit. |
| Korruptes Ledger | Der Broker wird deaktiviert. Das Budget wird **nicht** zurückgesetzt (fail closed). |
| 429 pro Tag | Das Modell ist bis Mitternacht Pacific gesperrt. Das gemeldete `quotaValue` senkt das gelernte RPD-Limit dauerhaft (nur nach unten). |
| 429 pro Minute | Einmal `RetryInfo` abwarten (höchstens 30 s), sonst Pause des Modells für diesen Lauf. RPM/TPM werden gelernt. |
| RPM/TPM | Ein gleitendes 60-s-Fenster pro Modell wartet aktiv, statt in einen 429 zu laufen. |
| Fairness | Pro Lauf höchstens `ceil(Rest ÷ verbleibende geplante Läufe)`. Der erste Lauf kann den Tag nicht leerräumen. |
| Zeitgrenze | `GEMINI_RUN_SECONDS` beendet die Cloud-Arbeit rechtzeitig vor dem Job-Timeout. Die lokale Extraktion läuft immer weiter. |
| Fehler | 401/403 schalten den Broker ab. 400/404 parken das Modell. Zwei Fehler parken das Modell für den Lauf. |
| Thinking | Bei `gemini-2.5-*` und `gemini-3.*` wird `thinkingBudget: 0` gesetzt (das 60-s-Timeout-Problem aus der QA). |
| Gemma | Ohne `systemInstruction` und JSON-Schema: Die Regeln werden in den Prompt eingebettet, und JSON wird erbeten (für spätere Aufgaben). |
| Transparenz | Die Step Summary zeigt Requests heute, geplantes RPD und gesperrte Modelle. Key und Prompt werden nie geloggt. |

### 3.2 Dokument-Extraktion (`gemini_tasks.py`)

* Ein Request pro Dokument mit mindestens 200 Zeichen. Der Text wird auf 12 000
  Zeichen gekürzt (≈ 4 000 Tokens), dazu kommen bis zu 40 bekannte lokale Entitäten.
* `responseSchema` mit geschlossener Prädikatliste: alle `RelationType`s außer
  `PUPPET_MASTER_OF` (berechnet), `ASSOCIATED_WITH` (vage) und `MENTIONED_WITH`
  (Kookkurrenz).
* Die System-Instruktion behandelt DATA als **nicht vertrauenswürdig**: Sie
  befolgt keine Anweisungen aus dem Text und extrahiert nur explizite Aussagen.
* **Lokale Validierung (verwirft ohne Rückfrage):**
  * Das Zitat steht nicht wörtlich im Text (Leerzeichen und Groß-/Kleinschreibung normalisiert).
  * Das Zitat nennt nicht beide Entitäten. Bei Personen reicht der Nachname.
  * Die Aussage ist verneint oder spekulativ: laut Modell-Flag **oder** laut lokalem
    Lexikon (z. B. „nicht“, „soll“, „angeblich“, „dementiert“, „allegedly“ …).
  * Die Entität ist unbekannt und steht nicht im Text, das Prädikat ist nicht erlaubt,
    oder Subjekt und Objekt sind identisch.
* Konfidenz = Quellgewicht × 0,6 × (0,5 + 0,5 × Modellkonfidenz). Sie liegt also
  höchstens bei 0,6 und damit immer unter der Auto-Release-Schwelle von 0,75.
* `method = llm`, `extra = {extractor: gemini_doc, model, requires_review: true, public: false}`.

### 3.3 Veröffentlichung: KI-Kanten nur nach Freigabe

`Relation.extra` wird nicht in Neo4j gespeichert. Bisher war deshalb jede Kante
aus einem verifizierten Feed automatisch öffentlich, egal welcher Extraktor sie
erzeugt hatte. Neu gilt:

* Der Export liest `r.method` mit. Eine Kante mit `method = 'llm'` wird nur mit
  `public_released = true` veröffentlicht, außer `GEMINI_AUTO_RELEASE=true`
  (bewusst nicht empfohlen).
* Der Neo4j-Upsert stellt sicher: Eine spätere **lokale** Extraktion derselben
  Kante überschreibt `llm` mit `dependency`. Damit fällt das Veto weg, weil die
  Kante jetzt unabhängig bestätigt ist. Eine Cloud-Extraktion stuft eine lokale
  Kante dagegen nie herab.
* Auch die bisherigen Satzpaar-Klassifikationen aus der Cloud tragen jetzt
  `method = llm` (vorher getarnt als `pattern`).

Review-Abfrage in Neo4j (Aura-Konsole):

```cypher
// Offene KI-Kanten mit Belegzitat
MATCH (a)-[r {method: 'llm'}]->(b)
WHERE r.public_released IS NULL
RETURN a.name, type(r), b.name, r.confidence, r.evidence[-1] AS zitat
ORDER BY r.confidence DESC LIMIT 50;

// Freigeben bzw. ablehnen (einzeln, nach Prüfung des Zitats)
MATCH (a {name: $von})-[r {method: 'llm'}]->(b {name: $nach}) SET r.public_released = true;
MATCH (a {name: $von})-[r {method: 'llm'}]->(b {name: $nach}) SET r.public_released = false;
```

## 4. Budgetrechnung

| Größe | Konservativ (Standard) | Public-Profil |
| --- | --- | --- |
| Geplante + manuelle Läufe/UTC-Tag | 2 + 1 | 8 + 3 |
| Joblimit / Ingestion / Prozess / Export | 5 min / 90 s / 105 s / 45 s | 20 min / 600 s / 660 s / 120 s |
| Dokumente pro Lauf | 10 | 30 |
| Gemini-Zeitfenster pro Lauf | 60 s | 420 s |
| Extraktions-Requests/Tag (max.) | ≈ 30 | 8 × 30 + 3 × 30 = **330** |
| Geplantes RPD (Flash-Lite 500 × 0,8) | 400 pro Modell, mit Kaskade 800 | 400 pro Modell, mit Kaskade 800 |
| Fair Share pro Lauf (1. Modell) | ceil(400 ÷ 2) = 200 | ceil(400 ÷ 8) = 50 ≥ 30 |
| Input-Tokens/Minute (≤ 12 RPM × ≈ 4,5k) | ≈ 54k von 200k geplant | ≈ 54k von 200k geplant |
| Cloudflare-Pages-Builds/Monat (nur bei geänderten Daten) | ≤ 93 | ≤ 341 von 500 |

* Selbst im Maximalfall verbraucht das Public-Profil **≈ 41 % des geplanten**
  und ≈ 33 % des gemeldeten Flash-Lite-Tagesbudgets **eines** Modells. Der Rest
  plus das komplette zweite Modell bleiben Reserve für die nächsten Phasen.
* Ledger-Commits tragen `[skip ci]` und lösen keinen Pages-Build aus.
* Neo4j: rund 240 Dokumente/Tag erzeugen grob 1 000–1 500 neue Knoten. Aura Free
  wäre damit in etwa 4 Monaten voll. Der Writer stoppt neue Entitäten ab 180 000
  Knoten; der Füllstand muss beobachtet werden (nächste Phase: Pruning/TTL).

## 5. Aktivierung (Schritt für Schritt)

1. **Google-Projekt ohne Abrechnungskonto verwenden.** Das ist die wichtigste
   Garantie für „immer kostenlos“: Ohne Billing lehnt Google Requests über dem
   Limit mit HTTP 429 ab, statt sie zu berechnen. Mit aktivem Billing würde
   dasselbe Projekt im Paid Tier abgerechnet.
2. In AI Studio unter *Rate limits* die Werte für `gemini-flash-lite-latest` und
   `gemini-3.1-flash-lite` ablesen und als Repository-Variable eintragen, z. B.:
   `GEMINI_MODEL_LIMITS = {"gemini-flash-lite-latest":{"rpm":15,"rpd":500,"tpm":250000},"gemini-3.1-flash-lite":{"rpm":15,"rpd":500,"tpm":250000}}`
3. Repository-Variablen (*Settings → Secrets and variables → Actions → Variables*):

   | Variable | Wert | Wirkung |
   | --- | --- | --- |
   | `PIPELINE_PROFILE` | `public` | 8 + 3 Läufe, 30 Dokumente, 20-min-Job. Wird nur aktiv, wenn die API das Repo als öffentlich bestätigt. |
   | `ENABLE_GEMINI_BROKER` | `true` | Broker an (zusätzlich zum vorhandenen Secret `GEMINI_API_KEY`). |
   | `GEMINI_MODE` | `document` | Ein Request pro Dokument. Der alte Satzpaar-Fallback ist dann automatisch aus. |
   | `GEMINI_TASK_CAPS` (optional) | `{"extract":330}` | Harte Tagesobergrenze pro Aufgabe. |
   | `GEMINI_EXTRACT_MODELS` (optional) | `gemini-flash-lite-latest,gemini-3.1-flash-lite` | Modell-Kaskade. |
   | `GEMINI_AUTO_RELEASE` | *nicht setzen* | KI-Kanten bleiben bis zur Freigabe privat. |

4. Den Workflow einmal manuell starten (*Actions → Run workflow*) und die Step
   Summary „Gemini free-tier broker“ prüfen.
5. Rückweg: `PIPELINE_PROFILE` löschen (sofort zurück zu 2 + 1). Bereits
   verbrauchte Slots schließen dann den Rest des UTC-Tages.

## 6. Phasen

| Phase | Inhalt | Budget/Tag | Status |
| --- | --- | --- | --- |
| 1 | Broker, Dokument-Extraktion, Export-Veto, Public-Profil | ≤ 330 Flash-Lite | ✅ umgesetzt (opt-in) |
| 2 | Entitätsauflösung mit `gemini-embedding-2` (eigener Topf 1000 RPD; ein Request bettet viele Namen ein) für Alias-Kandidaten, Ergebnis nur als Review-Vorschlag | ≤ 50 Embedding | geplant |
| 3 | Tägliches Briefing (Telegram, `telegram_bot.py`) aus dem veröffentlichten Graph: 1 Request Flash (eigener Topf 20 RPD) oder Gemma 4 | ≤ 2 | geplant |
| 4 | Review-Hilfe: Begründung pro offener KI-Kante als Batch (viele Kanten pro Request) | ≤ 20 | geplant |
| 5 | Mehrsprachige Quellen: Übersetzen und Extrahieren in einem Request (spart den Umweg über englische Modelle) | im Phase-1-Budget | geplant |

Jede Phase bekommt einen eigenen Task-Namen im Broker (`GEMINI_TASK_CAPS`). So
kann keine Aufgabe die Extraktion verdrängen.

## 7. Risiken und offene Punkte

* **EWR-Klausel:** Die Gemini-Bedingungen verlangen Paid Services, wenn API Clients
  Nutzern im EWR/CH/UK bereitgestellt werden. PuppetNET nutzt Gemini nur
  **offline im Batch-Lauf des Betreibers**. Endnutzer sehen nur statische
  Ergebnisse und rufen Gemini nie auf. Ob das als „API Client bereitstellen“ gilt,
  ist nicht abschließend geklärt; bei Zweifel rechtlich prüfen lassen (keine
  Rechtsberatung).
* **Datennutzung:** Free-Tier-Inhalte darf Google verwenden. Deshalb gehen nur
  bereits öffentliche Artikeltexte an Gemini, niemals private Notizen oder Fallakten.
* **Halluzination/Prompt-Injection:** Die Zitatprüfung verhindert erfundene Belege.
  Ein Angreifer kann aber einen echten Satz in einen Artikel schreiben. Daher
  gilt `requires_review` plus Export-Veto.
* **Limits ändern sich ohne Vorwarnung:** Der Broker lernt nur nach unten. Erhöht
  Google die Limits, `GEMINI_MODEL_LIMITS` anpassen und den `learned`-Eintrag im
  Ledger löschen.
* **GitHub-Cron unzuverlässig:** Ausgefallene Slots gehen verloren. Das Ledger
  verhindert Doppelläufe; als Ersatz ist `repository_dispatch` von außen möglich.
* **Repo-Wachstum:** Bis zu 11 Daten-Commits pro Tag. Größe von
  `graph_latest.json` beobachten.

## English summary

The free-tier bottleneck is requests per day, not tokens, so PuppetNET now sends
one schema-constrained request per document through a budget broker. The broker
budgets per project and model, uses the Pacific quota day, a 0.8 safety factor
and learning from 429 responses, gives each run a fair share, and reserves each
request before sending it. Every cloud relation is verified against a verbatim
quote and stored as `method=llm`. It is never auto-published without an explicit
`public_released=true`. The public repository makes standard GitHub-hosted runner
minutes free (verified). An opt-in, API-verified `PIPELINE_PROFILE=public` uses
that for up to 8 scheduled runs per day. Gemini limits are unaffected by
repository visibility. Use a Google project without billing so overruns fail with
429 instead of being charged.
