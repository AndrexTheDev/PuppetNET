// End-to-end rehearsal of scripts/provision-worker.mjs against a stub `npx`.
//
// No Cloudflare account is involved: a fake wrangler answers whoami / secret
// list / kv namespace create / secret put / deploy, and the test asserts the
// script actually patched wrangler.toml, sent 64-hex tokens on stdin, wrote the
// 0600 secret file and deployed. This is the part of MP8 that can be proven
// offline; the real namespaces still need the operator's account.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPT = resolve(ROOT, 'scripts/provision-worker.mjs');
const LEGACY_TOML = `name = "historical-provisioner-test"
main = "worker.js"
[[kv_namespaces]]
binding = "RATE_LIMIT_KV"
id = "${'0'.repeat(32)}"
preview_id = "${'0'.repeat(32)}"

[[kv_namespaces]]
binding = "RESULT_KV"
id = "${'0'.repeat(32)}"
preview_id = "${'0'.repeat(32)}"

[triggers]
crons = ["0 */12 * * *", "23 */3 * * *"]
`;

const STUB = `#!/usr/bin/env node
const { appendFileSync, readFileSync } = require('node:fs');
const { createHash } = require('node:crypto');
const args = process.argv.slice(2);
if (args[0] === 'wrangler') args.shift();
const log = (line) => appendFileSync(process.env.STUB_LOG, line + '\\n');
log('CALL ' + args.join(' '));
const id = (seed) => createHash('sha256').update(seed).digest('hex').slice(0, 32);

if (args[0] === 'whoami') {
  console.log('You are logged in with an OAuth Token.');
  process.exit(0);
}
if (args[0] === 'secret' && args[1] === 'list') {
  console.log('[]');
  process.exit(0);
}
if (args[0] === 'kv' && args[1] === 'namespace' && args[2] === 'create') {
  const name = args[3];
  const preview = args.includes('--preview');
  const value = id(name + (preview ? ':preview' : ':production'));
  console.log('Creating namespace');
  console.log('{ binding = "' + name + '", ' + (preview ? 'preview_id' : 'id') + ' = "' + value + '" }');
  process.exit(0);
}
if (args[0] === 'secret' && args[1] === 'put') {
  const value = readFileSync(0, 'utf8').trim();
  log('SECRET ' + args[2] + ' len=' + value.length + ' hex=' + /^[0-9a-f]+$/i.test(value) + ' h=' + createHash('sha256').update(value).digest('hex').slice(0, 8));
  console.log('Success! Uploaded secret ' + args[2]);
  process.exit(0);
}
if (args[0] === 'deploy') {
  console.log('Uploaded puppetnet-edge-relay');
  console.log('Deployed to https://puppetnet-edge-relay.stub.workers.dev');
  process.exit(0);
}
console.error('stub: unexpected command ' + args.join(' '));
process.exit(9);
`;

test('provisioning patches the toml, uploads secrets and deploys against a stub', (t) => {
  if (process.platform === 'win32') return t.skip('POSIX shell stub');
  const dir = mkdtempSync(join(tmpdir(), 'puppetnet-provision-'));
  mkdirSync(join(dir, 'apps/worker'), { recursive: true });
  mkdirSync(join(dir, 'scripts'), { recursive: true });
  mkdirSync(join(dir, 'bin'), { recursive: true });
  copyFileSync(SCRIPT, join(dir, 'scripts/provision-worker.mjs'));
  writeFileSync(join(dir, 'apps/worker/wrangler.toml'), LEGACY_TOML);

  const bin = join(dir, 'bin/npx');
  writeFileSync(bin, STUB);
  chmodSync(bin, 0o755);
  const logPath = join(dir, 'stub.log');
  writeFileSync(logPath, '');

  const result = spawnSync(process.execPath, ['scripts/provision-worker.mjs', '--skip-verify'], {
    cwd: dir,
    encoding: 'utf8',
    timeout: 90_000,
    env: {
      ...process.env,
      PATH: `${join(dir, 'bin')}:${process.env.PATH}`,
      STUB_LOG: logPath,
      NEO4J_URI: 'neo4j+s://example.databases.neo4j.io:7687',
      NEO4J_USER: 'neo4j',
      NEO4J_PASSWORD: 'test-password',
      CF_ACCOUNT_ID: 'a'.repeat(32),
      CF_API_TOKEN: 'analytics-token',
      TELEGRAM_BOT_TOKEN: '123:abc',
      TELEGRAM_CHAT_ID: '-1001234567',
    },
  });

  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const calls = readFileSync(logPath, 'utf8').split('\n').filter(Boolean);

  // KV: four creates, in binding order, production then preview.
  const creates = calls.filter((line) => line.includes('kv namespace create'));
  assert.deepEqual(creates.map((line) => line.split('create ')[1]), [
    'PUPPETNET_RATE_LIMIT',
    'PUPPETNET_RATE_LIMIT --preview',
    'PUPPETNET_RESULTS',
    'PUPPETNET_RESULTS --preview',
  ]);

  // wrangler.toml now carries the stub ids, and nothing else changed.
  const toml = readFileSync(join(dir, 'apps/worker/wrangler.toml'), 'utf8');
  const expect = (seed) => createHash('sha256').update(seed).digest('hex').slice(0, 32);
  assert.match(toml, new RegExp(`binding = "RATE_LIMIT_KV"\\nid = "${expect('PUPPETNET_RATE_LIMIT:production')}"`));
  assert.match(toml, new RegExp(`preview_id = "${expect('PUPPETNET_RATE_LIMIT:preview')}"`));
  assert.match(toml, new RegExp(`binding = "RESULT_KV"\\nid = "${expect('PUPPETNET_RESULTS:production')}"`));
  assert.equal((toml.match(/"0{32}"/g) || []).length, 0);
  assert.match(toml, /crons = \["0 \*\/12 \* \* \*", "23 \*\/3 \* \* \*"\]/);

  // Secrets: every one was uploaded on stdin, the generated three are 64 hex.
  const secrets = calls.filter((line) => line.startsWith('SECRET '));
  const names = secrets.map((line) => line.split(' ')[1]).sort();
  assert.deepEqual(names, [
    'CF_ACCOUNT_ID', 'CF_API_TOKEN', 'GRAPH_API_TOKEN', 'NEO4J_PASSWORD', 'NEO4J_URI',
    'NEO4J_USER', 'PROXY_AUTH_TOKEN', 'STATUS_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
  ]);
  for (const name of ['PROXY_AUTH_TOKEN', 'GRAPH_API_TOKEN', 'STATUS_TOKEN']) {
    const line = secrets.find((entry) => entry.startsWith(`SECRET ${name} `));
    assert.match(line, /len=64 hex=true/, `${name} must be 32 random bytes as hex`);
  }
  const hashes = ['PROXY_AUTH_TOKEN', 'GRAPH_API_TOKEN', 'STATUS_TOKEN']
    .map((name) => secrets.find((entry) => entry.startsWith(`SECRET ${name} `)).split(' h=')[1]);
  assert.equal(new Set(hashes).size, 3, 'each generated token is distinct');

  // Deploy happened, the generated file is private, and no .dev.vars was written.
  assert.ok(calls.some((line) => line === 'CALL deploy'));
  const generated = join(dir, 'apps/worker/.secrets.generated.env');
  assert.ok(existsSync(generated));
  assert.equal(statSync(generated).mode & 0o777, 0o600);
  const generatedKeys = readFileSync(generated, 'utf8').split('\n').filter((line) => line.includes('=')).map((line) => line.split('=')[0]).sort();
  assert.deepEqual(generatedKeys, ['GRAPH_API_TOKEN', 'PROXY_AUTH_TOKEN', 'STATUS_TOKEN']);
  assert.equal(existsSync(join(dir, 'apps/worker/.dev.vars')), false);
});

test('a second run keeps the existing namespaces and tokens', (t) => {
  if (process.platform === 'win32') return t.skip('POSIX shell stub');
  const dir = mkdtempSync(join(tmpdir(), 'puppetnet-provision-'));
  mkdirSync(join(dir, 'apps/worker'), { recursive: true });
  mkdirSync(join(dir, 'scripts'), { recursive: true });
  mkdirSync(join(dir, 'bin'), { recursive: true });
  copyFileSync(SCRIPT, join(dir, 'scripts/provision-worker.mjs'));

  // Start from an already-provisioned config and a Worker that has the secrets.
  const zeros = '0'.repeat(32);
  const prod = 'c'.repeat(32);
  const preview = 'd'.repeat(32);
  const seeded = LEGACY_TOML
    .replace(`id = "${zeros}"\npreview_id = "${zeros}"\n\n[[kv_namespaces]]\nbinding = "RESULT_KV"`,
             `id = "${prod}"\npreview_id = "${preview}"\n\n[[kv_namespaces]]\nbinding = "RESULT_KV"`);
  assert.equal((seeded.match(/"0{32}"/g) || []).length, 2);
  writeFileSync(join(dir, 'apps/worker/wrangler.toml'), seeded);

  const bin = join(dir, 'bin/npx');
  const stub = STUB.replace("console.log('[]');", "console.log(JSON.stringify(['PROXY_AUTH_TOKEN','GRAPH_API_TOKEN','STATUS_TOKEN','NEO4J_URI','NEO4J_PASSWORD','CF_ACCOUNT_ID','CF_API_TOKEN'].map((name) => ({ name }))));");
  writeFileSync(bin, stub);
  chmodSync(bin, 0o755);
  const logPath = join(dir, 'stub.log');
  writeFileSync(logPath, '');

  const result = spawnSync(process.execPath, ['scripts/provision-worker.mjs', '--skip-verify'], {
    cwd: dir,
    encoding: 'utf8',
    timeout: 90_000,
    env: {
      ...process.env,
      PATH: `${join(dir, 'bin')}:${process.env.PATH}`,
      STUB_LOG: logPath,
    },
  });

  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const calls = readFileSync(logPath, 'utf8').split('\n').filter(Boolean);
  assert.equal(calls.filter((line) => line.includes('kv namespace create')).length, 2,
    'only the untouched RESULT_KV pair is created');
  assert.ok(calls.some((line) => line.includes('kv namespace create PUPPETNET_RESULTS')));
  // Only NEO4J_USER is new: the other secrets already exist and are kept.
  assert.deepEqual(
    calls.filter((line) => line.startsWith('SECRET ')).map((line) => line.split(' ')[1]),
    ['NEO4J_USER'],
    'existing secrets are kept; nothing is re-uploaded without --rotate'
  );
  const toml = readFileSync(join(dir, 'apps/worker/wrangler.toml'), 'utf8');
  assert.match(toml, new RegExp(`binding = "RATE_LIMIT_KV"\\nid = "${prod}"\\npreview_id = "${preview}"`));
  assert.equal((toml.match(/"0{32}"/g) || []).length, 0);
});
