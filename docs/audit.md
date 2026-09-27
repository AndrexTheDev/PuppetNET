# Repository audit

```bash
npm run audit            # report on stdout + docs/audit/findings.json
npm run audit -- --strict  # also fail on low-severity findings
```

The smoke suites answer *"does this feature work?"*. This audit answers a different
question: **"is the repository internally consistent, safe and free to run?"** Those are
the defects that survive a green test suite — a client-side cap that drifted from the
Worker's, a duplicate element id, an `aria-labelledby` pointing at nothing, a paid API
creeping into the source list, a toast that renders markup on the caller's word.

[`scripts/audit.mjs`](../scripts/audit.mjs) is static, offline and dependency-free: it
reads files and runs nothing. It exits non-zero when an unresolved `high` or `medium`
finding remains (`--strict` adds `low`).

## Campaign status

The audit runs continuously in CI; this table records the module-by-module review
that goes with it — what has been read line by line, and what that reading found.
"Green" means the suite is green *after* the fixes, with each one mutation-tested
(reintroduce the defect, watch the specific check go red).

| # | Module | Status | Found and fixed |
| - | ------ | ------ | --------------- |
| 1 | Audit foundation (`scripts/audit.mjs`, CI wiring) | done | See the dimensions below; every check is mutation-tested before it is trusted |
| 2 | Web console (`web/app.js`, `web/modals.js`, `web/styles.css`) | done | Dialog stacks that left the first card undismissable; `zoomBy` centring on nothing; four disagreeing definitions of "visible" (the HUD reported the pre-filter count) |
| 3 | Edge relay (`worker.js`, `wrangler.toml`) | done | `graphPath` emitted Cypher that needs Neo4j 5.24+ (every weighted handshake failed on an older 5.x); CORS joined a list into `Access-Control-Allow-Origin` (browser rejects it, so a two-origin deployment served neither); `robotPathMatches` made every pattern match every path (the longest line of a robots.txt decided every URL — including permitting what the file forbade); three SSRF bypasses (`localhost.`, `fd00::/8`, wildcard-DNS names such as `127.0.0.1.nip.io`) plus an `fc`-prefix false positive; fractional `retry_after`; a KV bucket refilled above its own burst; a cache HIT reporting the query time of whoever filled it |
| 4 | OSINT engine (`ingest.py`, `puppetnet/**`) | done | A re-read counted as fresh corroboration: the confidence `noisy_or` merged on every sighting, and the dedupe window is a *time* window, so re-reading one article (or a feed republishing an archive) walked a single document's edge towards 1.0 — entities accumulated `mention_count` and mentions accumulated on every re-read on top of it. Fixed with a per-edge `doc_ids` ledger and `row.is_new`: only a document the node has not counted merges confidence/observations. The dedupe read `WHERE d.fetched_at >= $since OR d.last_ingested_at >= $since` could use no index at all (an `OR` across two properties defeats both) and scanned every `:Document` on every run — 24 times a day; `.state/content_hashes.json` was a bare list trimmed by `sorted()`, so the comment said "most recent 100k" while the code kept an arbitrary slice, and it ignored `DEDUPE_WINDOW_DAYS` entirely, silently overriding every tier's window. Also: an entity with an empty or punctuation-only name has no comparison form, so every such mention merged into one junk node per type; and every served request wrote its rate-limit bucket to Workers KV, which the hourly tier would turn into 9 000+ writes a day against a 1 000-write free quota |
| 5 | Scheduled operation (workflows, `telegram_bot.py`, tiered cadence) | done | One cron for every source: the wire feeds were read once a day (so the day's news arrived a day late) and the registers were read as often as the feeds allowed. Now three tiers with their own crons and budgets (`hourly` / `daily` / `weekly`), `--tier` as an outer bound that `--sources` cannot widen, one shared concurrency lock across the three ingest workflows, and cron minutes moved off `:00`/`:30` because GitHub load-shifts round hours. `tests/test_schedule.py` pins the contract — cron slots, tier per workflow, shared lock, maintenance chain, budgets, and that no dispatch input reaches a shell body. A 24-hour loop had no way to notice a *hard* failure: the alert passes need a maintenance report, and the run that dies before writing one was invisible until somebody looked at the graph. Every ingest workflow now warns on exit 1/2 (suppressed per `workflow|source|exit code`, so 24 runs a day send one alert, not 24). And `telegram_bot.bullet()` trusted any value that *looked* like markup (`<…>`) and passed it through unescaped — an entity name or error message is not a caller, so `<b>pwned</b>` from a source field would have arrived in the channel as markup; the guard now escapes everything and `[security] python/looksLikeMarkup` fails the build if the pattern returns |
| 6 | Final report and hardening pass | done | The hardening pass closed the last two loose ends: `docs/operations.md` §9 "What this costs" now states the worst-case daily consumption of every free tier (and names the two quotas that can actually bind — 1 000 KV writes/day and 200 000 AuraDB nodes), and `tests/test_schedule.py` pins the failure-alert wiring per workflow so the 24-hour loop cannot silently lose its alarm. Verified in the same pass: 971 Python tests, `ruff`, 36 worker checks, 32 console checks, the audit, and CI 4/4 green on the branch (including the two commits that closed this module) |

## Dimensions

| Dimension | What it checks |
| --- | --- |
| `security` | HTML sinks (`innerHTML`, `insertAdjacentHTML`, `document.write`), dynamic code execution, inline event handlers, the toast sink and its markup allowlist, the `el({html: …})` sink, Worker CORS/token/method posture, secrets in logs or in the tree, Python hazards (`yaml.load`, `shell=True`, `pickle`, `eval`, `verify=False`, bare `except`), and the **cost perimeter**: paid hosts, CDNs and analytics are forbidden outright, every other external host must have a recorded free-tier budget |
| `bugs` | Cross-module invariants — the console's clamps versus `GRAPH_LIMITS` in `worker.js`, the Aura caps in `puppetnet/config.py` versus the console's HUD, the canonical-key grammar, the advertised version versus `WORKER_VERSION` — plus duplicate ids, dangling references (including every `data-open-modal` target) and leftover debugging |
| `bugs` (UI state) | Three pieces of state no single feature test can see drift: `ui/dialogMutualExclusion` (opening a dialog closes the one on screen — otherwise two cards stack and the first becomes undismissable, because `closeModal()` only hides what `state.modalOpen` names), `ui/visibilitySelector` (visibility comes from the `.flt-hidden` class, never from `:visible`, which reads a computed style that can lag a batch, nor from `:not(.flt-hidden)`, which Cytoscape 3.30.4 rejects as an invalid selector and then matches *everything*), and `ui/animationMotionPreference` (every `cy.animate` honours `reduce-motion`) |
| `a11y` | `ui/dialogScrollLock` (the page neither scrolls behind an open dialog nor stays locked afterwards) alongside the heading-outline and `alt` checks |
| `seo` | Title/description length, keyword and metadata completeness, Open Graph and Twitter cards, the card image's real pixel size versus its declared one, JSON-LD validity, required properties and internal `@id` references, heading outline, the `robots.txt` directives and its `Sitemap:` line, and the files a public deployment needs (`robots.txt`, `404.html`, and `sitemap.xml` — whose absence is legitimate only while `scripts/emit-seo-files.mjs` exists *and* is wired into `pages_deploy.yml`, because a committed sitemap cannot know the origin it is served from) |
| `info` | Operational posture: cron schedules, job timeouts, concurrency groups, and the harvest cadence the free tiers have to carry |

## Severity

* **high** — exploitable, data-losing, or two modules disagreeing in a way that breaks a request.
* **medium** — a real defect with a bounded blast radius, or a missing artefact a crawler or operator expects.
* **low** — hygiene: a stale debug call, a missing `alt`, a cosmetic inconsistency.
* **info** — recorded, not judged: the external-host inventory, the current cron cadence.

## Accepted risks

A finding can be accepted instead of fixed, but never silently: add its check id to
`ACCEPTED` in `scripts/audit.mjs` **with a written justification**. Accepted findings stay
in the report and in the JSON ledger, marked as such, so the decision remains visible to
the next reader. An acceptance that no longer matches the code is worse than no acceptance
— the justification names the code it relies on, and the checks it leans on
(`xss/elHtml`, `toast/sink`) run anyway.

The standing acceptances:

| Check id | Why it is accepted |
| --- | --- |
| `xss/innerHTML` | The surviving writes are container clears, the `el()` helper's documented `html` key, the QR SVG (a constant address through the vendored generator) and three settings/boot strings whose every interpolation passes `escapeHtml`. Audited by `xss/elHtml` and attacked at runtime by the hostile-markup check in `tests/web_smoke.mjs`. |
| `cost/external-host` | The inventory lists paid-tier hosts (`api.opencorporates.com`, `adsbexchange-com1.p.rapidapi.com`) that are key-gated and **off by default** — `cost/paidSourceDefault` fails if that ever changes. |
| `python/assert` | Load-time config self-validation and tests only; no production control flow depends on an `assert` surviving `python -O`. |

## The ledger

`docs/audit/findings.json` is **generated**, and therefore git-ignored: it is a snapshot of
one run, and committing it would only create merge noise. This document and the script are
the durable artefacts; CI regenerates the ledger on every run.

## Wiring into CI

The audit runs in the `web` job of [`ci.yml`](../.github/workflows/ci.yml), after both smoke
suites, so a green build means "green *and* consistent". It exits non-zero on any unresolved
`high` or `medium` finding, which currently means the pipeline fails on: a client cap that
drifted from `GRAPH_LIMITS`, a paid host in the source list, a dangling `data-open-modal`,
a dialog system that stopped closing its predecessor, a visibility count derived from a
selector again, or a deployment that lost its sitemap emitter.

The checks are mutation-tested, not merely written: each one has been verified to fail when
the code it protects is broken (remove the `closeModal()` call, drop a `reduce-motion`
guard, reintroduce `:visible`) and to pass when it is restored. A static gate that cannot
fail is decoration.
