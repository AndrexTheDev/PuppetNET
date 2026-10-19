import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import config from '../apps/web/next.config.mjs';
const read = p => fs.readFileSync(p, 'utf8');
test('Next 15 exports a static app without hosted functions', () => {
  assert.match(JSON.parse(read('apps/web/package.json')).dependencies.next, /^15\./);
  assert.equal(config.output, 'export');
  assert.equal(config.images.unoptimized, true);
  assert.ok(fs.existsSync('apps/web/public/legacy/index.html'));
});
test('compatibility paths point to the single monorepo source', () => {
  for (const [old, current] of [['worker.js','apps/worker/worker.js'], ['puppetnet','packages/pipeline/puppetnet'], ['web','apps/web/public/legacy']])
    assert.equal(fs.realpathSync(old), fs.realpathSync(current));
});
test('only Python Actions remain, with no artificial consumption guard', () => {
  assert.deepEqual(fs.readdirSync('.github/workflows'), ['ingestion-pipeline.yml']);
  const text = read('.github/workflows/ingestion-pipeline.yml');
  assert.match(text, /group: puppetnet-python-ingestion/);
  assert.match(text, /runs-on: ubuntu-latest/);
  assert.doesNotMatch(text, /ci-budget|setup-node|npm |wrangler|actions: read/);
  assert.ok(!fs.existsSync('scripts/ci-budget.mjs'));
});
test('Free-only deployment defaults and no client-side secrets', () => {
  const worker = read('apps/worker/wrangler.toml');
  assert.match(worker, /cpu_ms = 10/);
  // KV is Free-tier and is where the distributed locks and health snapshots
  // live; Queues remain disabled because they are not needed for MP6.
  assert.match(worker, /\[\[kv_namespaces\]\]/);
  assert.match(worker, /binding = "RATE_LIMIT_KV"/);
  assert.match(worker, /binding = "RESULT_KV"/);
  assert.match(worker, /\[triggers\]/);
  assert.match(worker, /crons = \["0 \*\/12 \* \* \*", "23 \*\/3 \* \* \*"\]/);
  assert.doesNotMatch(worker, /\[\[queues/);
  const env = read('.env.example');
  for (const key of ['NEO4J_URI','CF_KV_NAMESPACE','TELEGRAM_BOT_TOKEN']) assert.match(env, new RegExp(`^${key}=`, 'm'));
  assert.doesNotMatch(env, /^NEXT_PUBLIC_.*(?:TOKEN|PASSWORD|SECRET)=/m);
});
test('Next headers allow its scripts and same-origin legacy iframe', () => {
  const headers = read('apps/web/public/_headers');
  assert.match(headers, /\/legacy\/\*\n/);
  assert.match(headers, /frame-ancestors 'self'/);
  assert.doesNotMatch(headers.split('/legacy/*')[0], /script-src|frame-src 'none'/);
});

test('Cloudflare build owners and watch paths are explicitly decoupled', () => {
  const settings = JSON.parse(read('cloudflare-builds.json'));
  assert.equal(settings.pages.rootDirectory, 'apps/web');
  assert.equal(settings.worker.rootDirectory, 'apps/worker');
  assert.ok(settings.pages.watchPaths.includes('apps/web/*'));
  assert.ok(settings.worker.watchPaths.includes('apps/worker/*'));
  assert.match(settings.pages.buildCommand, /ci:pages/);
  assert.match(settings.worker.buildCommand, /ci:worker/);
  assert.match(settings.worker.previewCommand, /versions upload/);
});
