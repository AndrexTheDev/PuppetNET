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
test('only bounded, serialized CI and opt-in daily ingest remain active', () => {
  assert.deepEqual(fs.readdirSync('.github/workflows').sort(), ['ci.yml','ingest.yml']);
  for (const file of ['ci.yml', 'ingest.yml']) {
    const text = read(`.github/workflows/${file}`);
    assert.match(text, /group: puppetnet-free-tier/);
    assert.match(text, /cancel-in-progress: false/);
    assert.match(text, /node scripts\/ci-budget.mjs/);
    assert.match(text, /timeout-minutes: (10|20)\n/);
    assert.doesNotMatch(text, /strategy:|macos-|windows-/);
  }
  assert.match(read('.github/workflows/ingest.yml'), /ENABLE_FREE_INGEST == 'true'/);
});
test('Free-only deployment defaults and no client-side secrets', () => {
  const worker = read('apps/worker/wrangler.toml');
  assert.match(worker, /cpu_ms = 10/);
  assert.doesNotMatch(worker, /\[\[queues|\[\[kv_namespaces/);
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

test('monthly admission is conservative, paginated and fails closed', async () => {
  const { spawnSync } = await import('node:child_process');
  const guard = new URL('../scripts/ci-budget.mjs', import.meta.url).href;
  for (const [count, ok, expected] of [[90, true, 0], [91, true, 1], [0, false, 1]]) {
    const script = `globalThis.fetch = async () => ({ ok: ${ok}, status: 403, json: async () => ({ workflow_runs: Array(${count}).fill({}) }) }); await import(${JSON.stringify(guard)});`;
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
      env: { ...process.env, GH_TOKEN: 'fixture-not-a-real-token', GITHUB_REPOSITORY: 'fixture/repo' }, encoding: 'utf8'
    });
    assert.equal(result.status, expected, result.stderr);
  }
});
