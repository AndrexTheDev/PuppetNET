import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
// Cloudflare may start this gate from the repository root or from a workspace
// root directory (apps/web, apps/worker). Both are supported: the gate anchors
// itself to the repository it lives in instead of trusting the caller's cwd.
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
process.chdir(repoRoot);
// Build gate for Workers Builds; production deployment is a separate command.
// worker.js and its Neo4j/KV tests are retained only as historical compatibility.
const typecheck = spawnSync('npm', ['run', 'check', '-w', '@puppetnet/worker'], { stdio: 'inherit' });
if (typecheck.status !== 0) process.exit(typecheck.status ?? 1);
for (const args of [
  ['--import', 'tsx', '--test', 'tests/search_worker.test.ts'],
  ['--test', 'tests/provision.test.mjs', 'tests/provision_e2e.test.mjs'],
]) {
  const result = spawnSync(process.execPath, args, { stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
