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

## Dimensions

| Dimension | What it checks |
| --- | --- |
| `security` | HTML sinks (`innerHTML`, `insertAdjacentHTML`, `document.write`), dynamic code execution, inline event handlers, the toast sink and its markup allowlist, the `el({html: …})` sink, Worker CORS/token/method posture, secrets in logs or in the tree, Python hazards (`yaml.load`, `shell=True`, `pickle`, `eval`, `verify=False`, bare `except`), and the **cost perimeter**: paid hosts, CDNs and analytics are forbidden outright, every other external host must have a recorded free-tier budget |
| `bugs` | Cross-module invariants — the console's clamps versus `GRAPH_LIMITS` in `worker.js`, the Aura caps in `puppetnet/config.py` versus the console's HUD, the canonical-key grammar, the advertised version versus `WORKER_VERSION` — plus duplicate ids, dangling references and leftover debugging |
| `seo` | Title/description length, keyword and metadata completeness, Open Graph and Twitter cards, the card image's real pixel size versus its declared one, JSON-LD validity, required properties and internal `@id` references, heading outline, and the files a public deployment needs (`robots.txt`, `sitemap.xml`, `404.html`) |
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

The audit joins `ci.yml` once the open findings below are resolved, so that a green build
means "green *and* consistent". Until then it is run on demand and reported in the commit
message.
