// Offline checks for scripts/provision-worker.mjs. No Cloudflare account, no network:
// the dry run must plan, print nothing secret, and leave the repository untouched.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPT = 'scripts/provision-worker.mjs';
const TOML = resolve(ROOT, 'apps/worker/wrangler.toml');
const GENERATED = resolve(ROOT, 'apps/worker/.secrets.generated.env');
const DEV_VARS = resolve(ROOT, 'apps/worker/.dev.vars');

/** A developer shell may have real credentials exported; the expectations below must not. */
const cleanEnv = () => {
  const env = { ...process.env, NO_COLOR: '1' };
  for (const key of Object.keys(env)) {
    if (/^(NEO4J_|CF_|TELEGRAM_|WORKER_URL|CLOUDFLARE_)/.test(key)) delete env[key];
  }
  return env;
};

const run = (...args) =>
  spawnSync(process.execPath, [SCRIPT, ...args], {
    cwd: ROOT,
    encoding: 'utf8',
    timeout: 60_000,
    env: cleanEnv(),
  });

const digest = (path) => createHash('sha256').update(readFileSync(path)).digest('hex');

test('dry run plans both KV namespaces and exits 0 without credentials', () => {
  const hadGenerated = existsSync(GENERATED);
  const hadDevVars = existsSync(DEV_VARS);
  const before = digest(TOML);
  const result = run('--dry-run');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /PUPPETNET_RATE_LIMIT/);
  assert.match(result.stdout, /PUPPETNET_RESULTS/);
  assert.match(result.stdout, /RATE_LIMIT_KV/);
  assert.match(result.stdout, /RESULT_KV/);
  assert.match(result.stdout, /no account calls/i);
  for (const name of ['PROXY_AUTH_TOKEN', 'GRAPH_API_TOKEN', 'STATUS_TOKEN']) {
    assert.match(result.stdout, new RegExp(name));
  }
  assert.match(result.stdout, /NEO4J_URI: missing/);
  assert.equal(digest(TOML), before, 'dry run must not edit wrangler.toml');
  assert.equal(existsSync(GENERATED), hadGenerated, 'dry run must not write generated secrets');
  assert.equal(existsSync(DEV_VARS), hadDevVars, 'dry run must not write .dev.vars');
});

test('dry run never prints a generated token', () => {
  const result = run('--dry-run');
  assert.equal(result.status, 0, result.stderr);
  const combined = `${result.stdout}\n${result.stderr}`;
  assert.equal(/(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])/i.test(combined), false, '64-hex secret leaked');
  assert.equal(/Bearer\s+[0-9a-f]{8,}/i.test(combined), false, 'bearer token leaked');
});

test('wrangler.toml still carries the all-zero placeholders (deploy requires real ids)', () => {
  const toml = readFileSync(TOML, 'utf8');
  assert.match(toml, /binding\s*=\s*"RATE_LIMIT_KV"/);
  assert.match(toml, /binding\s*=\s*"RESULT_KV"/);
  assert.equal((toml.match(/"0{32}"/g) || []).length, 4, 'expected four placeholder ids (id + preview_id)');
  assert.match(toml, /crons\s*=\s*\["0 \*\/12 \* \* \*", "23 \*\/3 \* \* \*"\]/);
});

test('--help lists the flags and exits 0', () => {
  const result = run('--help');
  assert.equal(result.status, 0, result.stderr);
  for (const flag of ['--dry-run', '--rotate', '--force-kv', '--test-crons', '--require-heartbeat']) {
    assert.match(result.stdout, new RegExp(flag.replace(/-/g, '\\-')));
  }
});

test('unknown options fail fast with exit code 2', () => {
  const result = run('--not-a-flag');
  assert.equal(result.status, 2);
  assert.match(result.stderr, /Unknown option: --not-a-flag/);
});
