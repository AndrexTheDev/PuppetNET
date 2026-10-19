import { spawnSync } from 'node:child_process';
// Executed from repository root by Pages Git Integration, NOT GitHub Actions.
//
// The SEO emitter runs first: it is origin-driven, so it no-ops (exit 0) unless
// the operator sets SITE_ORIGIN in the Pages build environment, and with
// --build-gate an unusable value warns instead of failing the deployment. When
// it does emit, `next build` copies apps/web/public/{robots.txt,sitemap.xml}
// into the exported root.
for (const args of [['scripts/emit-seo-files.mjs', '--build-gate'], ['--check', 'web/app.js'], ['--check', 'web/modals.js'], ['tests/web_smoke.mjs'], ['--import', 'tsx', '--test', 'tests/dashboard.test.ts']]) {
  const result = spawnSync(process.execPath, args, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const result = spawnSync('npm', ['run', 'build', '-w', '@puppetnet/web'], {
  stdio: 'inherit', env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1' }
});
process.exit(result.status ?? 1);
