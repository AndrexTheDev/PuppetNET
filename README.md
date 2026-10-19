# PuppetNET 2.0 — decoupled hybrid CI/CD

Open-source OSINT ingestion, entity recognition and graph analysis. Baseline history
through `arena/01a0df64-puppetnet` commit `127ab47` is integrated into the session branch
`arena/322dea42-puppetnet`. Integration into `main` is delivered through PR #1; changes are authored on the
session branch, never pushed directly to `main`. Check the PR for merge status.

| Component | Source | Build / execution owner |
| --- | --- | --- |
| Next.js 15 static frontend | `apps/web` | Cloudflare Pages Git Integration |
| Relay and read-only graph API | `apps/worker` | Cloudflare Workers Builds |
| Python tests, GLiNER NLP, Neo4j ingestion | `packages/pipeline` | GitHub Actions standard Linux runner |

The new `/dashboard/` frontend uses Sigma.js 4, Graphology, an off-thread ForceAtlas2
layout and opt-in Leaflet/OpenStreetMap. The previous analyst console is retained at
`/legacy/index.html`. See [dashboard guide](docs/webgl-dashboard.md). `output: 'export'` generates
`apps/web/out` during `next build` (Next 15 does not use the old `next export` command).
There are no Pages Functions, SSR routes or hosted model APIs.

## Local development / verification

```sh
npm ci
npm run dev
npm run ci:pages                  # console regression gate + static Next build
npm run ci:worker                 # Worker regression gate, no deployment
npm run test:scaffold             # monorepo/build configuration contracts
npm run test:dashboard            # filters, LoD, evidence, 120k-element fixture
npm run deploy -w @puppetnet/worker -- --dry-run --outdir /tmp/puppetnet-worker
python -m venv .venv
. .venv/bin/activate
pip install -e './packages/pipeline[dev]'
python -m pytest -q -m 'not integration'
ruff check packages/pipeline tests/test_gliner.py tests/test_hybrid_ci.py
```

GLiNER CPU inference is an optional install (`packages/pipeline[gliner]`), selected
with `NLP_BACKEND=gliner`. Tests use offline model doubles; production ingestion loads
real model weights. spaCy remains the default for backwards-compatible local runs.
Root compatibility symlinks retain the previous Python/console/tooling paths without
source duplication; use a symlink-capable checkout.

## Deployment and operation

[Hybrid setup guide](docs/2.0-scaffold.md) contains the exact Pages and Workers Builds
settings, watch paths, secrets, production gates and GLiNER limitations.
[`cloudflare-builds.json`](cloudflare-builds.json) records dashboard settings; it is
**not** an automatically applied Cloudflare configuration schema.

GitHub Actions has **no build/deploy JavaScript jobs and no artificial minute gate**.
Standard hosted runner execution in this public repository is free under GitHub's
public-repository policy; normal service, concurrency and artifact limits still apply.
Daily ingestion is disabled until `ENABLE_INGESTION=true` and database secrets exist.

Older docs and workflows under `docs/legacy-*` describe the v1 architecture only.
The historical static audit is not a v2 certification. Current runtime regressions,
Python CI and the Cloudflare build gates are the active checks.
