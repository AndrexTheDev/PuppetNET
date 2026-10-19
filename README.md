# PuppetNET 2.0 — zero-cost monorepo scaffold

Integrated baseline: `arena/01a0df64-puppetnet`, commit `127ab47`.
This session prepares integration on `arena/322dea42-puppetnet`; `main` is not modified.

| Path | Component |
| --- | --- |
| `apps/web` | Next.js 15 static export, including the existing analyst console at `/legacy/index.html` |
| `apps/worker` | Existing Cloudflare Worker relay and read-only graph API, Free configuration |
| `packages/pipeline` | Existing Python ingestion, NLP, graph analytics and Telegram tools |
| `tests` | Existing regression tests plus monorepo/budget contract tests |

This is a scaffold, **not a React rewrite** of the existing console. The home page embeds
it same-origin. Root compatibility symlinks preserve the old imports, scripts and tools;
there is only one source of truth per component. Use a checkout with symlink support.

## Local verification

```sh
npm ci
npm run dev                    # Next development server on 0.0.0.0:3000
npm run build                  # apps/web/out: deployable static files
npm run test:scaffold
npm test                       # Worker and existing console regression tests
python -m venv .venv
. .venv/bin/activate
pip install -e './packages/pipeline[dev]'
python -m pytest -q -m 'not integration'
ruff check packages/pipeline
python packages/pipeline/ingest.py --dry-run --list-sources
```

See [deployment, budget and migration notes](docs/2.0-scaffold.md).
The old documentation remains as historical reference, not as the current deployment
contract: [legacy overview](docs/legacy-README.md). Old expensive schedules live under
`docs/legacy-workflows/` and **are not executed** by GitHub Actions.
