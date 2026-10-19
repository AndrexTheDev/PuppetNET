#!/usr/bin/env node
/** Explicit KV-free setup. No credential values are printed or stored in files. */
import { spawnSync } from 'node:child_process';
const args = process.argv.slice(2);
if (args.some(arg => !['--dry-run', '--help'].includes(arg))) {
  console.error('Use --dry-run or no flags.'); process.exit(2);
}
if (args.includes('--help') || args.includes('--dry-run')) {
  console.log('KV-free search relay: no account calls, writes or deployment in dry-run.');
  console.log('Required secrets: GITHUB_DISPATCH_TOKEN, SEARCH_AUTH_TOKEN (>=32 characters).');
  console.log('Configure PAGES_ORIGIN, ALLOWED_ORIGINS, GITHUB_OWNER, GITHUB_REPO in apps/worker/wrangler.toml.');
  console.log('Real run uploads these two secrets through stdin and deploys this workspace. No KV namespaces or crons are created.');
  process.exit(0);
}
const secrets = { GITHUB_DISPATCH_TOKEN: process.env.GITHUB_DISPATCH_TOKEN, SEARCH_AUTH_TOKEN: process.env.SEARCH_AUTH_TOKEN };
if (!secrets.GITHUB_DISPATCH_TOKEN || !secrets.SEARCH_AUTH_TOKEN || secrets.SEARCH_AUTH_TOKEN.length < 32) {
  console.error('Required search-relay secrets missing or invalid. No account call performed.'); process.exit(1);
}
for (const [command, input] of [
  [['secret', 'bulk'], JSON.stringify(secrets)], [['deploy'], undefined],
]) {
  const result = spawnSync('npm', ['exec', '--workspace=@puppetnet/worker', '--', 'wrangler', ...command], {
    input, encoding: 'utf8', stdio: ['pipe', 'inherit', 'inherit'],
  });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
