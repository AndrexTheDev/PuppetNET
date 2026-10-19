import { spawnSync } from 'node:child_process';
// Build gate for Workers Builds; production deployment is a separate command.
for (const args of [
  ['--check', 'apps/worker/worker.js'],
  ['tests/worker_smoke.mjs'],
  ['tests/worker_hardening.mjs'],
  ['--test', 'tests/provision.test.mjs'],
  ['--test', 'tests/provision_e2e.test.mjs'],
]) {
  const result = spawnSync(process.execPath, args, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
