import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import config from '../apps/web/next.config.mjs';
const read = p => fs.readFileSync(p, 'utf8');
test('Next 15 exports a static app without hosted functions', () => {
  assert.match(JSON.parse(read('apps/web/package.json')).dependencies.next, /^15\./);
  assert.equal(config.output, 'export');
  assert.equal(config.images.unoptimized, true);
  // The v1 console is archived: only the redirect route may exist under /legacy.
  assert.equal(fs.existsSync('apps/web/public/legacy'), false);
  assert.ok(fs.existsSync('apps/web/app/legacy/page.tsx'));
});
test('compatibility paths point to the single monorepo source', () => {
  for (const [old, current] of [['worker.js','apps/worker/worker.js'], ['puppetnet','packages/pipeline/puppetnet']])
    assert.equal(fs.realpathSync(old), fs.realpathSync(current));
  assert.equal(fs.existsSync('web'), false, 'the legacy console symlink must not come back');
});
test('only Python Actions remain, with no artificial consumption guard', () => {
  assert.deepEqual(fs.readdirSync('.github/workflows').sort(), ['daily_ingestion.yml', 'ingestion-pipeline.yml']);
  const text = read('.github/workflows/ingestion-pipeline.yml');
  assert.match(text, /group: puppetnet-python-ingestion/);
  assert.match(text, /runs-on: ubuntu-latest/);
  assert.doesNotMatch(text, /ci-budget|setup-node|npm |wrangler|actions: read/);
  assert.ok(!fs.existsSync('scripts/ci-budget.mjs'));
});
test('Free-only deployment defaults and no client-side secrets', () => {
  const worker = read('apps/worker/wrangler.toml');
  assert.match(worker, /cpu_ms = 10/);
  assert.match(worker, /main = "src\/index\.ts"/);
  assert.doesNotMatch(worker, /\[\[kv_namespaces\]\]|"0{32}"|\[\[queues/);
  const env = read('.env.example');
  for (const key of ['NEO4J_URI','CF_KV_NAMESPACE','TELEGRAM_BOT_TOKEN']) assert.match(env, new RegExp(`^${key}=`, 'm'));
  assert.doesNotMatch(env, /^NEXT_PUBLIC_.*(?:TOKEN|PASSWORD|SECRET)=/m);
});
test('Next headers keep the security baseline and /legacy only redirects', () => {
  const headers = read('apps/web/public/_headers');
  assert.match(headers, /frame-ancestors 'self'/);
  assert.match(headers, /X-Content-Type-Options: nosniff/);
  assert.match(headers, /\/_next\/static\/\*\n[ \t]+Cache-Control: public, max-age=31536000, immutable/);
  // The legacy console CSP block is gone; /legacy answers with a redirect only.
  assert.doesNotMatch(headers, /\/legacy\/vendor/);
  assert.match(read('apps/web/public/_redirects'), /^\/legacy\s+\/dashboard\/\s+301$/m);
});

test('Cloudflare build owners and watch paths are explicitly decoupled', () => {
  const settings = JSON.parse(read('cloudflare-builds.json'));
  assert.equal(settings.pages.rootDirectory, 'apps/web');
  assert.equal(settings.worker.rootDirectory, 'apps/worker');
  assert.ok(settings.pages.watchPaths.includes('apps/web/*'));
  assert.ok(settings.worker.watchPaths.includes('apps/worker/*'));
  assert.match(settings.pages.buildCommand, /ci:pages/);
  assert.equal(settings.pages.deployCommand, null, 'native Pages must not run the Workers deploy command');
  assert.match(settings.pages.manualDeployCommand, /deploy:pages/);
  assert.match(settings.worker.buildCommand, /ci:worker/);
  assert.match(settings.worker.previewCommand, /versions upload/);

  const rootPackage = JSON.parse(read('package.json'));
  assert.equal(
    rootPackage.scripts['deploy:pages'],
    'npm exec --workspace=@puppetnet/worker -- wrangler pages deploy --cwd ../web',
  );
  assert.equal(rootPackage.devDependencies.wrangler, undefined, 'the wrong root-level `npx wrangler deploy` must stay fail-closed');
});
