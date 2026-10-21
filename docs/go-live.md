# PuppetNET – Go-live in der richtigen Reihenfolge

> 🌐 **Sprachen · Languages:** Deutsch ↓ · [English](#english)

Stand 21.10.2026. Code, Tests und Beta sind abgeschlossen
([Beta-Bericht](testing/beta-2026-10-21.md)). Die Cloudflare-Pages-Builds sind seit 87b997c
wieder grün. Zuvor war seit Welle 6 kein Build durchgelaufen
([Vorfall](operations.md#incident-2026-10-2021-every-pages-build-failed-for-26-hours)). Offen sind nur Betreiberschritte. Jeder
Schritt hat ein prüfbares Ergebnis. Erst zum nächsten Schritt gehen, wenn er grün ist.
Details stehen in den verlinkten Dokumenten.

| # | Schritt | Wo | Fertig, wenn … |
|---|---|---|---|
| 1 | **Code nach `main`**: PR aus dem Session-Branch öffnen, CI abwarten, mergen. | GitHub | Checks „tests“ **und** „Cloudflare Pages“ grün; danach Produktions-Deploy unter puppetnet-web.pages.dev |
| 2 | **Neo4j-Zugang reparieren** (aktuell `Unauthorized`): In der Aura-Konsole das Passwort zurücksetzen und `NEO4J_URI` (`neo4j+s://…`), `NEO4J_USER` und `NEO4J_PASSWORD` als Repository-Secrets neu setzen. | Aura, GitHub → Settings → Secrets | Schritt 3 meldet Neo4j OK |
| 3 | **Verbindungsprüfung**: Actions → „Bounded daily ingestion and static publication“ → *Run workflow* auf `main` → `task: connection-check`. Nur lesend, keine Commits, kein Kontingent. | GitHub Actions | Job-Summary: Secrets, Neo4j, spaCy, Gemini und Export **OK** (Report auch als Artefakt) |
| 4 | **Quellen festlegen**: Variable `DAILY_RSS_FEEDS` mit deutschsprachigen Feeds, optional `VERIFIED_RSS_FEEDS` (exakte HTTPS-Feed-URLs, die automatisch veröffentlichen dürfen). | GitHub → Variables | Policy bewusst geprüft ([Quellen-Policy](source-policy.md)) |
| 5 | **Publikation einschalten**: `ENABLE_DAILY_STATIC=true`, dann einmal *Run workflow* mit `task: ingest`. | GitHub | Commit „data: publish auto-released static graph“ auf `main`, Pages-Build grün, `/dashboard/` zeigt Daten statt Standby, `/entity/<slug>/` erreichbar |
| 6 | **Erste 48 h beobachten** | [Monitoring-Checkliste](operations/monitoring-checklist.md) | Zwei Cron-Läufe (03:00/15:00 UTC) ohne Fehler, Ledger-Commits vorhanden |
| 7 | *Optional:* **Public-Profil + Gemini**: `PIPELINE_PROFILE=public`, `ENABLE_GEMINI_BROKER=true`, `GEMINI_MODE=document` und `GEMINI_MODEL_LIMITS` mit den Werten aus aistudio.google.com/rate-limit. Secret `GEMINI_API_KEY` aus einem Projekt **ohne** Abrechnung. `GEMINI_AUTO_RELEASE` **nicht** setzen. | GitHub | Job-Summary „Gemini free-tier broker“ ohne 429-Dauerfehler ([Gemini-Strategie §5](gemini-strategy.md)) |
| 8 | **Review-Routine** (nur bei Schritt 7): KI-Kanten (`method=llm`) und Entitäten, die nur das Modell gesehen hat (`cloud_only`), erscheinen erst nach `public_released = true`. Abfragen: [Gemini-Strategie §3.3](gemini-strategy.md). | Aura-Konsole | Offene Liste regelmäßig abgearbeitet; Ablehnung = `false` |

**Rechtliches (keine Rechtsberatung):**
- Die Gemini-Nutzungsbedingungen verlangen für API-Clients, die Nutzern im EWR, in der Schweiz oder im UK bereitgestellt werden, den kostenpflichtigen Dienst. PuppetNET ruft Gemini nur im eigenen Workflow auf, nicht aus dem Browser. Die Einordnung bleibt Betreibersache.
- Free-Tier-Inhalte darf Google verwenden. Es gehen nur öffentliche Artikeltexte an Gemini.
- Lösch- und Widerspruchsanfragen: [Quellen-Policy §5](source-policy.md).

**Dauerbetrieb:**
- In öffentlichen Repositories deaktiviert GitHub geplante Workflows nach 60 Tagen ohne Repository-Aktivität. Die Daten-Commits zählen in der Regel als Aktivität. Wenn die Läufe ausbleiben, den Workflow unter Actions wieder aktivieren.
- Rückweg jederzeit: `ENABLE_DAILY_STATIC` löschen (Publikation aus), `PIPELINE_PROFILE` löschen (zurück zu 2 + 1 Läufen) oder `ENABLE_GEMINI_BROKER=false`.
- Rollback der Website: [Release-Checkliste](release-checklist.md#rollback).

<a id="english"></a>

# PuppetNET – go-live in the right order (English)

As of 21 Oct 2026, code, tests and beta are complete ([beta report](testing/beta-2026-10-21.md)).
Only operator steps remain. Move on only when a step is green.

1. **Merge to `main`** via PR from the session branch. Done when the checks "tests" **and** "Cloudflare Pages" are green and the production deploy appears on puppetnet-web.pages.dev.
2. **Fix Neo4j access** (currently `Unauthorized`): reset the password in Aura and re-set the `NEO4J_URI` (`neo4j+s://…`), `NEO4J_USER` and `NEO4J_PASSWORD` secrets.
3. **Connection check**: Actions → "Bounded daily ingestion and static publication" → *Run workflow* on `main` → `task: connection-check`. Read-only, no commits, no quota. Done when all five stages are OK in the job summary (also stored as an artifact).
4. **Sources**: set `DAILY_RSS_FEEDS`, and optionally `VERIFIED_RSS_FEEDS` ([source policy](source-policy.md)).
5. **Enable publication**: set `ENABLE_DAILY_STATIC=true`, then *Run workflow* with `task: ingest`. Done when there is a data commit on `main`, the Pages build is green, the dashboard shows data instead of standby, and entity pages are reachable.
6. **Watch the first 48 h** ([monitoring checklist](operations/monitoring-checklist.md)).
7. *Optional:* **public profile + Gemini**: `PIPELINE_PROFILE=public`, `ENABLE_GEMINI_BROKER=true`, `GEMINI_MODE=document`, and `GEMINI_MODEL_LIMITS` from AI Studio. Use a `GEMINI_API_KEY` from a project **without** billing. Never set `GEMINI_AUTO_RELEASE`.
8. **Review routine**: `llm` edges and `cloud_only` entities are published only after `public_released = true` (queries in [Gemini strategy §3.3](gemini-strategy.md)).

Not legal advice: see the Gemini terms (paid services for API clients made available in the EEA/CH/UK) and the [source policy](source-policy.md). In public repositories, scheduled workflows are disabled after 60 days without repository activity; re-enable the workflow in Actions if runs stop.
