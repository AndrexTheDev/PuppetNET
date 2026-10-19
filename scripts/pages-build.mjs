import { spawnSync } from 'node:child_process';
// Executed from repository root by Pages Git Integration, NOT GitHub Actions.
for (const args of [['--check', 'web/app.js'], ['--check', 'web/modals.js'], ['tests/web_smoke.mjs']]) {
  const result = spawnSync(process.execPath, args, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const result = spawnSync('npm', ['run', 'build', '-w', '@puppetnet/web'], {
  stdio: 'inherit', env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1' }
});
process.exit(result.status ?? 1);
