import { spawnSync } from 'node:child_process';
// Prepare assets before tests; direct next build repeats this idempotently.
for (const args of [
  ['scripts/generate-seo.js'],
  ['--test', 'tests/release.test.mjs'],
  ['--check', 'web/app.js'],
  ['--check', 'web/modals.js'],
  ['tests/web_smoke.mjs'],
  ['--import', 'tsx', '--test', 'tests/dashboard.test.ts', 'tests/data_fetcher.test.ts'],
  ['--import', 'tsx', '--test', 'tests/seo.test.ts'],
  ['--import', 'tsx', '--test', 'tests/a11y.test.tsx'],
]) {
  const result = spawnSync(process.execPath, args, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const result = spawnSync('npm', ['run', 'build', '-w', '@puppetnet/web'], {
  stdio: 'inherit', env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1' }
});
if (result.status !== 0) process.exit(result.status ?? 1);
// The deployment directory is the product: verify the exported files, worker
// format and Cloudflare headers only after `next build` has written out/.
const exported = spawnSync(process.execPath, ['tests/export_smoke.mjs'], { stdio: 'inherit' });
process.exit(exported.status ?? 1);
