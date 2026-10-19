#!/usr/bin/env node
/**
 * provision-worker.mjs — Cloudflare provisioning for apps/worker, in one pass.
 *
 *   node scripts/provision-worker.mjs --dry-run          # plan only, no account needed
 *   node scripts/provision-worker.mjs                    # KV + secrets + deploy + verify
 *   node scripts/provision-worker.mjs --rotate           # also rotate generated tokens
 *   node scripts/provision-worker.mjs --test-crons       # local scheduled() smoke, then heartbeat
 *
 * What it does, in order:
 *
 *   1. Reads apps/worker/wrangler.toml and replaces the all-zero KV placeholder
 *      ids with real ones, creating the namespaces (and their preview siblings)
 *      through `wrangler kv namespace create`. An existing non-placeholder id is
 *      never overwritten without `--force-kv`.
 *   2. Sets the Worker secrets. PROXY_AUTH_TOKEN / GRAPH_API_TOKEN /
 *      STATUS_TOKEN are generated with 32 random bytes (64 hex chars) when they
 *      do not exist yet; the rest come from the environment:
 *        NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
 *        CF_API_TOKEN (Account Analytics: Read), CF_ACCOUNT_ID,
 *        TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional).
 *      Values are passed to `wrangler secret put` on stdin, never on the command
 *      line. Generated tokens are saved to apps/worker/.secrets.generated.env
 *      (mode 0600, git-ignored) so the operator can load them into the console.
 *   3. Deploys with `wrangler deploy`, then verifies /health, /status (401
 *      without a token), /status.json, /status/refresh, /graph/health, the cron
 *      schedules (when the token can read them) and the :_PuppetNetHeartbeat node.
 *
 * Nothing is written to the network in --dry-run: it only reads the config and
 * reports what would happen. No secret value is ever printed.
 *
 * This script requires `wrangler login` (or CLOUDFLARE_API_TOKEN) and network
 * access to api.cloudflare.com and the AuraDB instance.
 */

import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { chmodSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const WORKER_DIR = resolve(ROOT, 'apps/worker');
const CONFIG_PATH = resolve(WORKER_DIR, 'wrangler.toml');
const GENERATED_PATH = resolve(WORKER_DIR, '.secrets.generated.env');
const DEV_VARS_PATH = resolve(WORKER_DIR, '.dev.vars');
const NPX = process.platform === 'win32' ? 'npx.cmd' : 'npx';

const argv = process.argv.slice(2);
const flags = new Set(argv);
const has = (name) => flags.has(name);
const DRY = has('--dry-run');
const ROTATE = has('--rotate');
const FORCE_KV = has('--force-kv');
const ALLOW_MISSING_ANALYTICS = has('--allow-missing-analytics');
const REQUIRE_HEARTBEAT = has('--require-heartbeat');
const TEST_CRONS = has('--test-crons');
const SKIP_KV = has('--skip-kv');
const SKIP_SECRETS = has('--skip-secrets');
const SKIP_DEPLOY = has('--skip-deploy');
const SKIP_VERIFY = has('--skip-verify');

const KNOWN_FLAGS = new Set([
  '--dry-run', '--rotate', '--force-kv', '--allow-missing-analytics', '--skip-kv',
  '--skip-secrets', '--skip-deploy', '--skip-verify', '--test-crons',
  '--require-heartbeat', '--help', '-h',
]);
const isMain = Boolean(process.argv[1]) && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

function parseArgs() {
  const unknownFlag = argv.find((arg) => arg.startsWith('-') && !KNOWN_FLAGS.has(arg));
  if (unknownFlag && unknownFlag !== '--help' && unknownFlag !== '-h') {
    console.error(`Unknown option: ${unknownFlag}\nRun with --help for the available flags.`);
    process.exit(2);
  }
  if (!argv.includes('--help') && !argv.includes('-h')) return;
  console.log(`Usage: node scripts/provision-worker.mjs [options]

  --dry-run                 print the plan; no account calls or writes
  --rotate                  rotate PROXY_AUTH_TOKEN / GRAPH_API_TOKEN / STATUS_TOKEN
  --force-kv                create and bind new KV namespaces even if ids exist
  --allow-missing-analytics continue without CF_API_TOKEN / CF_ACCOUNT_ID
  --skip-kv                 do not touch KV
  --skip-secrets            do not set secrets
  --skip-deploy             do not deploy
  --skip-verify             do not call the deployed Worker
  --test-crons              run both schedules through wrangler dev --test-scheduled
  --require-heartbeat       exit non-zero when :_PuppetNetHeartbeat is absent
  --help                    this text

Required environment for a real run: NEO4J_URI, NEO4J_PASSWORD,
CF_API_TOKEN, CF_ACCOUNT_ID. Optional: NEO4J_USERNAME, TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID. Generated and existing values are never printed.`);
  process.exit(0);
}

if (isMain) parseArgs();

const CRON_KEEPALIVE = '0 */12 * * *';
const CRON_HEALTH = '23 */3 * * *';
const PLACEHOLDER = /^0{32}$/;
const KV = [
  { binding: 'RATE_LIMIT_KV', name: 'PUPPETNET_RATE_LIMIT' },
  { binding: 'RESULT_KV', name: 'PUPPETNET_RESULTS' },
];

const REQUIRED_ENV = [
  { name: 'NEO4J_URI', hint: 'neo4j+s://<instance>.databases.neo4j.io:7687' },
  { name: 'NEO4J_PASSWORD', hint: 'the AuraDB password' },
  { name: 'CF_ACCOUNT_ID', hint: '32-hex account tag (dash.cloudflare.com → Workers → Overview)', required: !ALLOW_MISSING_ANALYTICS },
  { name: 'CF_API_TOKEN', hint: 'API token with Account Analytics: Read', required: !ALLOW_MISSING_ANALYTICS },
];
const OPTIONAL_ENV = [
  { name: 'NEO4J_USERNAME', fallback: 'neo4j' },
  { name: 'TELEGRAM_BOT_TOKEN' },
  { name: 'TELEGRAM_CHAT_ID' },
];

let failures = 0;
let warnings = 0;

function log(message = '') { process.stdout.write(`${message}\n`); }
function ok(message) { log(`  ok    ${message}`); }
function warn(message) { warnings += 1; log(`  warn  ${message}`); }
function fail(message) { failures += 1; log(`  FAIL  ${message}`); }
function step(message) { log(`\n== ${message}`); }
function mask(value, keep = 6) {
  const text = String(value || '');
  if (!text) return '(unset)';
  if (text.length <= keep + 4) return `${text.slice(0, 2)}…`;
  return `${text.slice(0, keep)}…${text.slice(-4)}`;
}

function fatal(message) {
  log(`\nERROR: ${message}`);
  process.exit(1);
}

function run(args, { input, capture = true } = {}) {
  const result = spawnSync(NPX, ['wrangler', ...args], {
    cwd: WORKER_DIR,
    encoding: 'utf8',
    env: process.env,
    input: input === undefined ? undefined : `${input}\n`,
    stdio: capture ? ['pipe', 'pipe', 'pipe'] : 'inherit',
  });
  return {
    status: result.status === null ? 1 : result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    output: `${result.stdout || ''}${result.stderr || ''}`,
  };
}

/* -------------------------------------------------------------------------- */
/*  wrangler.toml                                                             */
/* -------------------------------------------------------------------------- */

function readConfig() {
  return readFileSync(CONFIG_PATH, 'utf8');
}

function kvIds(config, binding) {
  const lines = config.split('\n');
  const at = lines.findIndex((line) => line.trim() === `binding = "${binding}"`);
  if (at < 0) return null;
  const found = { id: '', previewId: '' };
  for (let index = at + 1; index < Math.min(lines.length, at + 10); index += 1) {
    if (/^\[/.test(lines[index]) && index > at + 1) break;
    if (/^\s*id\s*=/.test(lines[index])) found.id = /"([^"]*)"/.exec(lines[index])?.[1] || '';
    if (/^\s*preview_id\s*=/.test(lines[index])) found.previewId = /"([^"]*)"/.exec(lines[index])?.[1] || '';
  }
  return found;
}

function setKvIds(config, binding, id, previewId) {
  const lines = config.split('\n');
  const at = lines.findIndex((line) => line.trim() === `binding = "${binding}"`);
  if (at < 0) throw new Error(`wrangler.toml has no [[kv_namespaces]] binding ${binding}`);
  let sawId = false;
  let sawPreview = false;
  let idLine = -1;
  for (let index = at + 1; index < Math.min(lines.length, at + 10); index += 1) {
    if (/^\[/.test(lines[index]) && index > at + 1) break;
    if (/^\s*id\s*=/.test(lines[index])) {
      lines[index] = `id = "${id}"`;
      idLine = index;
      sawId = true;
    }
    if (/^\s*preview_id\s*=/.test(lines[index])) {
      lines[index] = `preview_id = "${previewId || id}"`;
      sawPreview = true;
    }
  }
  if (!sawId) throw new Error(`binding ${binding} has no id line to replace`);
  if (!sawPreview) lines.splice(idLine + 1, 0, `preview_id = "${previewId || id}"`);
  return lines.join('\n');
}

/**
 * Wrangler prints the new namespace as a one-line binding snippet
 * (`{ binding = "...", id = "..." }`) or as TOML/JSON. Accept all three and
 * prefer `preview_id` when a preview namespace was requested.
 */
function parseNamespaceId(text, preview = false) {
  const keys = preview ? ['preview_id', 'id'] : ['id', 'preview_id'];
  for (const key of keys) {
    const match = new RegExp(`\\b${key}\\s*[=:]\\s*\"([0-9a-f]{32})\"`, 'i').exec(text);
    if (match) return match[1];
  }
  const json = /\"id\"\s*:\s*\"([0-9a-f]{32})\"/i.exec(text);
  return json ? json[1] : '';
}

function createNamespace(name, preview = false) {
  const args = ['kv', 'namespace', 'create', name];
  if (preview) args.push('--preview');
  const result = run(args);
  const text = result.output;
  const id = parseNamespaceId(text, preview);
  if (result.status !== 0 || !id) {
    throw new Error(`\`wrangler ${args.join(' ')}\` failed.\n${text.trim().slice(0, 1200)}`);
  }
  return id;
}

function assertKvConfigured(config) {
  for (const entry of KV) {
    const ids = kvIds(config, entry.binding);
    if (!ids || !ids.id || PLACEHOLDER.test(ids.id)) {
      throw new Error(`${entry.binding} still has a placeholder id in wrangler.toml`);
    }
    if (ids.previewId && PLACEHOLDER.test(ids.previewId)) {
      throw new Error(`${entry.binding} still has a placeholder preview_id in wrangler.toml`);
    }
  }
}

async function provisionKv() {
  step('KV namespaces');
  let config = readConfig();
  let changed = false;

  for (const entry of KV) {
    const current = kvIds(config, entry.binding);
    if (!current) fatal(`wrangler.toml is missing the ${entry.binding} [[kv_namespaces]] block`);
    const hasRealId = current.id && !PLACEHOLDER.test(current.id);
    if (hasRealId && !FORCE_KV) {
      ok(`${entry.binding}: already bound (${mask(current.id)}) — keeping it`);
      continue;
    }
    if (DRY) {
      log(`  plan  create ${entry.name} (+ preview) and bind it to ${entry.binding}`);
      continue;
    }
    log(`  ...   creating ${entry.name}`);
    const id = createNamespace(entry.name, false);
    let previewId = '';
    try {
      log(`  ...   creating ${entry.name} preview namespace`);
      previewId = createNamespace(entry.name, true);
    } catch (error) {
      previewId = id;
      warn(`${entry.name}: preview namespace could not be created (${String(error.message).split('\n')[0]}); preview_id = id`);
    }
    config = setKvIds(config, entry.binding, id, previewId || id);
    changed = true;
    ok(`${entry.binding}: id ${mask(id)}${previewId && previewId !== id ? `, preview ${mask(previewId)}` : ' (preview = id)'}`);
  }

  if (DRY) return;
  if (changed) {
    writeFileSync(CONFIG_PATH, config);
    ok('wrangler.toml updated');
  }
  const actual = readConfig();
  assertKvConfigured(actual);
  ok('no placeholder KV ids remain');
}

/* -------------------------------------------------------------------------- */
/*  secrets                                                                   */
/* -------------------------------------------------------------------------- */

/** Fail before KV side effects when a required value is neither exported nor set. */
function preflightSecrets() {
  if (SKIP_SECRETS) return new Set();
  const existing = existingSecrets();
  for (const spec of REQUIRED_ENV) {
    if (spec.required === false) continue;
    if ((process.env[spec.name] || '').trim()) continue;
    if (existing.has(spec.name)) continue;
    fatal(`${spec.name} is required: export it before running this script (${spec.hint}).`);
  }
  if (!((process.env.NEO4J_PASSWORD || '').trim()) && !existing.has('NEO4J_PASSWORD')) {
    fatal('NEO4J_PASSWORD is required: export it before running this script.');
  }
  const telegramToken = (process.env.TELEGRAM_BOT_TOKEN || '').trim() || (existing.has('TELEGRAM_BOT_TOKEN'));
  const telegramChat = (process.env.TELEGRAM_CHAT_ID || '').trim() || (existing.has('TELEGRAM_CHAT_ID'));
  if (Boolean(telegramToken) !== Boolean(telegramChat)) {
    warn('Telegram is configured only partially (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); alerts stay disabled');
  }
  return existing;
}

function readGenerated() {
  const values = new Map();
  if (!existsSync(GENERATED_PATH)) return values;
  for (const line of readFileSync(GENERATED_PATH, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const at = trimmed.indexOf('=');
    if (at > 0) values.set(trimmed.slice(0, at), trimmed.slice(at + 1));
  }
  return values;
}

function writeGenerated(values) {
  const header = [
    '# Generated by scripts/provision-worker.mjs.',
    '# These are live Worker credentials. Copy them into your password manager and',
    '# delete this file. It is git-ignored; never commit or paste it into chat.',
    '',
  ];
  const body = [...values.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key}=${value}`);
  writeFileSync(GENERATED_PATH, `${header.join('\n')}${body.join('\n')}\n`, { mode: 0o600 });
  try { chmodSync(GENERATED_PATH, 0o600); } catch (_) { /* Windows */ }
}

function existingSecrets() {
  if (DRY) return new Set();
  const result = run(['secret', 'list', '--format', 'json']);
  if (result.status !== 0) {
    if (/not found|does not exist|10007|workers script/i.test(result.output)) {
      log('  ...   Worker does not exist yet; no secrets to preserve');
      return new Set();
    }
    warn(`could not list existing secrets (${result.output.trim().split('\n').slice(-1)[0] || 'unknown error'}); treating none as set`);
    return new Set();
  }
  try {
    const parsed = JSON.parse(result.stdout);
    const list = Array.isArray(parsed) ? parsed : (parsed.secrets || parsed.result || []);
    return new Set(list.map((item) => (typeof item === 'string' ? item : item.name)).filter(Boolean));
  } catch (_) {
    return new Set((result.stdout.match(/"name"\s*:\s*"([^"]+)"/g) || []).map((line) => /"name"\s*:\s*"([^"]+)"/.exec(line)[1]));
  }
}

function putSecret(name, value) {
  const result = run(['secret', 'put', name], { input: value });
  if (result.status !== 0) {
    throw new Error(`\`wrangler secret put ${name}\` failed: ${result.output.trim().split('\n').slice(-2).join(' ')}`);
  }
  ok(`secret ${name} set`);
}

async function provisionSecrets(existing = null) {
  step('Worker secrets');
  const known = existing || existingSecrets();
  const generated = readGenerated();
  const values = new Map();

  // Generated tokens: reuse the value from the environment or the generated
  // file. A token that already exists in Cloudflare is never rotated silently.
  for (const name of ['PROXY_AUTH_TOKEN', 'GRAPH_API_TOKEN', 'STATUS_TOKEN']) {
    const fromEnv = process.env[name] && process.env[name].trim();
    const fromFile = generated.get(name);
    if (fromEnv) {
      values.set(name, fromEnv);
      ok(`${name}: from environment`);
      continue;
    }
    if (known.has(name) && !ROTATE) {
      if (fromFile) values.set(name, fromFile);
      ok(`${name}: already set (kept${fromFile ? ', loaded from ' + GENERATED_PATH : ''})`);
      continue;
    }
    const value = randomBytes(32).toString('hex');
    values.set(name, value);
    generated.set(name, value);
    if (DRY) log(`  plan  ${name}: would generate a new 32-byte token`);
    else ok(`${name}: generated a new 32-byte token`);
  }

  if (!DRY && values.size) writeGenerated(generated);

  // Operator-supplied secrets.
  for (const spec of REQUIRED_ENV) {
    const value = process.env[spec.name] && process.env[spec.name].trim();
    if (value) {
      values.set(spec.name, value);
      ok(`${spec.name}: from environment`);
      continue;
    }
    if (known.has(spec.name)) {
      ok(`${spec.name}: already set (kept)`);
      continue;
    }
    if (spec.required === false) {
      warn(`${spec.name}: not set — the corresponding dashboard card stays n/a`);
      continue;
    }
    if (DRY) {
      log(`  plan  ${spec.name}: missing (required for a real run)`);
      continue;
    }
    fatal(`${spec.name} is required: export it before running this script (${spec.hint}).`);
  }

  for (const spec of OPTIONAL_ENV) {
    const value = (process.env[spec.name] && process.env[spec.name].trim()) || generated.get(spec.name) || (spec.fallback && !known.has(spec.name) ? spec.fallback : '');
    if (value) values.set(spec.name, value);
  }
  if (values.has('TELEGRAM_BOT_TOKEN') !== values.has('TELEGRAM_CHAT_ID')) {
    warn('Telegram alerts need both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; skipping the incomplete pair');
    values.delete('TELEGRAM_BOT_TOKEN');
    values.delete('TELEGRAM_CHAT_ID');
  }

  if (DRY) {
    for (const name of values.keys()) log(`  plan  wrangler secret put ${name}`);
    return values;
  }

  for (const [name, value] of values) {
    // Re-setting the same generated token is harmless, and it repairs a Worker
    // whose secret list was wiped. Non-generated values are only re-put when
    // they came from the environment this run.
    if (known.has(name) && !process.env[name] && !ROTATE && !['PROXY_AUTH_TOKEN', 'GRAPH_API_TOKEN', 'STATUS_TOKEN'].includes(name)) {
      continue;
    }
    try {
      putSecret(name, value);
    } catch (error) {
      fatal(error.message);
    }
  }
  return values;
}

/* -------------------------------------------------------------------------- */
/*  deploy + verify                                                           */
/* -------------------------------------------------------------------------- */

function deploy() {
  step('Deploy');
  if (DRY) {
    log('  plan  npx wrangler deploy');
    return '';
  }
  const result = run(['deploy']);
  process.stdout.write(result.output);
  if (result.status !== 0) fatal('wrangler deploy failed (see output above)');
  const url = process.env.WORKER_URL
    || /https:\/\/[a-z0-9][a-z0-9.-]*\.workers\.dev/i.exec(result.output)?.[0]
    || '';
  if (url) ok(`deployed at ${url}`);
  else warn('deployed, but the URL was not in the output — set WORKER_URL for verification');
  return url;
}

async function request(url, { method = 'GET', token, body, timeout = 15000 } = {}) {
  const headers = { accept: 'application/json' };
  if (token) headers.authorization = `Bearer ${token}`;
  if (body !== undefined) headers['content-type'] = 'application/json';
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch (_) { /* HTML or empty */ }
  return { status: response.status, json, text, headers: response.headers };
}

async function verify(baseUrl, values) {
  step('Verification');
  if (!baseUrl) {
    warn('no WORKER_URL — skipped HTTP verification');
    return;
  }
  const base = baseUrl.replace(/\/+$/, '');
  const statusToken = values.get('STATUS_TOKEN') || process.env.STATUS_TOKEN || '';

  try {
    const health = await request(`${base}/health`);
    if (health.status !== 200) fail(`/health returned ${health.status}`);
    else if (!health.json?.ok) fail('/health did not report ok');
    else {
      ok(`/health ok, version ${health.json.version || '?'}`);
      const bindings = health.json.bindings || {};
      if (!bindings.rate_limit_kv || !bindings.result_kv) fail('KV bindings are not visible in /health');
      else ok('both KV bindings are attached');
      if (health.json.cron?.keepalive !== CRON_KEEPALIVE) warn(`/health reports keepalive cron ${JSON.stringify(health.json.cron?.keepalive)}`);
      else ok('keepalive cron is the expected 0 */12 * * *');
    }
  } catch (error) {
    fail(`/health request failed: ${error.message}`);
  }

  try {
    const unauth = await request(`${base}/status`);
    if (unauth.status === 401) ok('/status is token-protected (401 without a token)');
    else warn(`/status answered ${unauth.status} without a token — check STATUS_PUBLIC`);
  } catch (error) {
    fail(`/status request failed: ${error.message}`);
  }

  if (!statusToken) {
    warn('STATUS_TOKEN is unknown (pre-existing secret) — skipped /status.json and /status/refresh');
  } else {
    try {
      const status = await request(`${base}/status.json`, { token: statusToken });
      if (status.status !== 200) fail(`/status.json returned ${status.status}: ${status.text.slice(0, 200)}`);
      else {
        ok('/status.json served with the status token');
        if (!status.json.kv?.bound) fail('/status.json says KV is not bound');
        else ok('KV is bound');
        if (status.json.kv?.write_failed) fail('KV write failed during the last snapshot');
        if (status.json.rate?.graph_rpm !== 25) warn(`graph_rpm is ${status.json.rate?.graph_rpm}, expected 25`);
        if (status.json.workers?.source !== 'cloudflare_graphql') {
          warn(`Workers-requests source is ${status.json.workers?.source} — CF_API_TOKEN/CF_ACCOUNT_ID may be missing or lack Account Analytics:Read`);
        } else {
          ok('Cloudflare Analytics is reachable');
        }
      }
    } catch (error) {
      fail(`/status.json request failed: ${error.message}`);
    }

    try {
      const refresh = await request(`${base}/status/refresh`, { method: 'POST', token: statusToken, timeout: 30000 });
      if (refresh.status !== 200) fail(`/status/refresh returned ${refresh.status}: ${refresh.text.slice(0, 200)}`);
      else if (!refresh.json?.aura?.available) {
        fail(`live AuraDB probe failed: ${refresh.json?.aura?.error || 'unknown error'}`);
      } else {
        ok(`live AuraDB probe: ${refresh.json.aura.nodes} nodes, ${refresh.json.aura.relationships} relationships`);
      }
    } catch (error) {
      fail(`/status/refresh failed: ${error.message}`);
    }
  }

  try {
    const graph = await request(`${base}/graph/health`);
    if (graph.status !== 200) fail(`/graph/health returned ${graph.status}`);
    else ok(`/graph/health ok (${graph.json?.graph?.counts?.entities ?? 'n/a'} entities)`);
  } catch (error) {
    fail(`/graph/health request failed: ${error.message}`);
  }
}

async function verifySchedules() {
  step('Cron schedules');
  const account = process.env.CF_ACCOUNT_ID || '';
  const token = process.env.CF_API_TOKEN || '';
  if (!account || !token) {
    warn('CF_ACCOUNT_ID/CF_API_TOKEN unavailable — cron schedules were not queried');
    return;
  }
  try {
    const response = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${account}/workers/scripts/${encodeURIComponent(process.env.CF_WORKER_SCRIPT_NAME || 'puppetnet-edge-relay')}/schedules`,
      { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(15000) }
    );
    const body = await response.json().catch(() => null);
    if (response.status === 403 || response.status === 401) {
      warn('the analytics token cannot read Worker schedules (needs Workers Scripts:Read) — confirm both crons in the dashboard');
      return;
    }
    if (!response.ok || !body?.success) {
      warn(`schedule query failed (HTTP ${response.status}) — confirm both crons in the dashboard`);
      return;
    }
    const crons = (body.result?.schedules || []).map((entry) => entry.cron);
    const missing = [CRON_KEEPALIVE, CRON_HEALTH].filter((cron) => !crons.includes(cron));
    if (missing.length) fail(`deployed schedules are missing: ${missing.join(', ')} (got ${crons.join(', ') || 'none'})`);
    else ok(`both cron schedules are deployed: ${crons.join(' · ')}`);
  } catch (error) {
    warn(`schedule query failed: ${error.message} — confirm both crons in the dashboard`);
  }
}

/* -------------------------------------------------------------------------- */
/*  Neo4j heartbeat                                                           */
/* -------------------------------------------------------------------------- */

function neo4jBase(uri) {
  const match = /^(neo4j(\+s|\+ssc)?|bolt(\+s|\+ssc)?|https?):\/\/([^/?#]+)/i.exec(String(uri || ''));
  if (!match) return '';
  let authority = match[4];
  if (/^(neo4j|bolt)/i.test(match[1])) authority = authority.replace(/:\d+$/, '');
  const scheme = /^http:\/\//i.test(uri) ? 'http' : 'https';
  return `${scheme}://${authority}`;
}

async function neo4jQuery(statement, values = null) {
  const pick = (name) => (values && values.get(name)) || process.env[name] || '';
  const uri = pick('NEO4J_URI');
  const user = pick('NEO4J_USERNAME') || 'neo4j';
  const password = pick('NEO4J_PASSWORD');
  const database = process.env.NEO4J_DATABASE || 'neo4j';
  const base = neo4jBase(uri);
  if (!base || !password) return { ok: false, error: 'NEO4J_URI/NEO4J_PASSWORD are not exported' };
  try {
    const response = await fetch(`${base}/db/${encodeURIComponent(database)}/tx/commit`, {
      method: 'POST',
      headers: {
        authorization: `Basic ${Buffer.from(`${user}:${password}`).toString('base64')}`,
        'content-type': 'application/json',
        accept: 'application/json',
      },
      body: JSON.stringify({ statements: [{ statement }] }),
      signal: AbortSignal.timeout(20000),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok || !body || (body.errors && body.errors.length)) {
      return { ok: false, error: body?.errors?.[0]?.message || `HTTP ${response.status}` };
    }
    return { ok: true, results: body.results || [] };
  } catch (error) {
    return { ok: false, error: error.message };
  }
}

async function verifyHeartbeat(values = null) {
  step('AuraDB heartbeat');
  const result = await neo4jQuery(
    'MATCH (h:_PuppetNetHeartbeat) RETURN h.id AS id, toString(h.last_seen) AS last_seen, h.worker AS worker, h.version AS version ORDER BY h.last_seen DESC LIMIT 1',
    values
  );
  if (!result.ok) {
    warn(`heartbeat query failed: ${result.error}`);
    return false;
  }
  const columns = result.results?.[0]?.columns || [];
  const row = result.results?.[0]?.data?.[0]?.row;
  if (!row) {
    warn('no :_PuppetNetHeartbeat node yet — the keep-alive cron has not run against this database');
    warn(`run \`npm run provision:worker -- --test-crons\` or wait for ${CRON_KEEPALIVE} (UTC)`);
    return false;
  }
  const record = Object.fromEntries(columns.map((name, index) => [name, row[index]]));
  ok(`heartbeat present: ${record.id} · ${record.last_seen} · ${record.worker}@${record.version}`);
  return true;
}

/* -------------------------------------------------------------------------- */
/*  Local scheduled() smoke                                                   */
/* -------------------------------------------------------------------------- */

function writeDevVars(values) {
  const merged = new Map();
  if (existsSync(DEV_VARS_PATH)) {
    for (const line of readFileSync(DEV_VARS_PATH, 'utf8').split('\n')) {
      const at = line.indexOf('=');
      if (at > 0 && !line.trim().startsWith('#')) merged.set(line.slice(0, at), line.slice(at + 1));
    }
  }
  for (const [name, value] of values) if (value) merged.set(name, value);
  writeFileSync(DEV_VARS_PATH, `${[...merged].map(([key, value]) => `${key}=${value}`).join('\n')}\n`, { mode: 0o600 });
  try { chmodSync(DEV_VARS_PATH, 0o600); } catch (_) { /* Windows */ }
  ok(`local secrets written to ${DEV_VARS_PATH} (git-ignored, mode 0600)`);
}

async function testCrons(values) {
  step('Local scheduled() smoke');
  if (!values.has('NEO4J_URI') || !values.has('NEO4J_PASSWORD') || !values.has('NEO4J_USERNAME')) {
    warn('the local cron test needs NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD exported in this shell — skipped');
    warn('manual: export them, then `npx wrangler dev --test-scheduled` and curl the two /__scheduled URLs');
    return;
  }
  if (!DRY) writeDevVars(values);
  const port = Number(process.env.DEV_PORT || 8793);
  log(`  ...   starting wrangler dev --test-scheduled on port ${port}`);
  const child = spawn(NPX, ['wrangler', 'dev', '--test-scheduled', '--port', String(port)], {
    cwd: WORKER_DIR,
    env: process.env,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: process.platform !== 'win32',
  });
  let output = '';
  let exited = false;
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { output += chunk; });
  child.on('exit', () => { exited = true; });

  try {
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline && !exited) {
      if (/ready on|listening on|started server/i.test(output)) break;
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
    }
    if (exited) throw new Error(`wrangler dev exited early:\n${output.trim().slice(-1200)}`);
    for (const cron of [CRON_KEEPALIVE, CRON_HEALTH]) {
      const url = `http://127.0.0.1:${port}/__scheduled?cron=${encodeURIComponent(cron)}`;
      const response = await fetch(url, { signal: AbortSignal.timeout(60000) });
      const text = await response.text();
      if (response.status !== 200) throw new Error(`${cron} → HTTP ${response.status}: ${text.slice(0, 300)}`);
      ok(`scheduled ${cron} → ${text.slice(0, 300)}`);
    }
  } catch (error) {
    fail(`local cron test failed: ${error.message}`);
    log('  manual:  npx wrangler dev --test-scheduled');
    log(`           curl "http://127.0.0.1:8787/__scheduled?cron=0+%2A%2F12+%2A+%2A+%2A"`);
    log(`           curl "http://127.0.0.1:8787/__scheduled?cron=23+%2A%2F3+%2A+%2A+%2A"`);
  } finally {
    if (!exited) {
      try { process.kill(-child.pid, 'SIGTERM'); } catch (_) { child.kill('SIGTERM'); }
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 750));
  }
  await verifyHeartbeat(values);
}

/* -------------------------------------------------------------------------- */
/*  main                                                                      */
/* -------------------------------------------------------------------------- */

function assertAuthenticated() {
  const result = run(['whoami']);
  if (/not authenticated|please run `wrangler login`/i.test(result.output)) {
    fatal('wrangler is not authenticated. Run `npx wrangler login` (or export CLOUDFLARE_API_TOKEN).');
  }
  if (result.status !== 0) {
    fatal(`\`wrangler whoami\` failed:\n${result.output.trim().slice(0, 800)}`);
  }
  ok('wrangler is authenticated');
}

async function main() {
  if (/main\s*=\s*"src\/index\.ts"/.test(readFileSync(CONFIG_PATH, 'utf8'))) {
    log('KV-free configuration detected. Legacy KV provisioning is disabled.');
    if (DRY) {
      log('No account calls, no writes or deployments. Use scripts/provision-search-worker.mjs.');
      log('Required secrets: GITHUB_DISPATCH_TOKEN and SEARCH_AUTH_TOKEN. No KV namespaces or database secrets.');
      return;
    }
    fatal('Use scripts/provision-search-worker.mjs for the active zero-KV Worker.');
  }
  log('PuppetNET Worker provisioning');
  log(`config: ${CONFIG_PATH}${DRY ? '  [dry run]' : ''}`);
  if (DRY) log('No account calls, no writes, no deployment are performed in this mode.');
  if (!DRY) assertAuthenticated();
  const knownSecrets = DRY ? new Set() : preflightSecrets();

  if (!SKIP_KV) await provisionKv();
  else step('KV namespaces — skipped');

  if (DRY) {
    step('Secrets (plan)');
    const values = await provisionSecrets(knownSecrets);
    step('Summary');
    log(`  KV: ${SKIP_KV ? 'skipped' : 'create/patch the two namespaces'}`);
    log(`  secrets: ${SKIP_SECRETS ? 'skipped' : `${values.size} value(s) available or generated`}`);
    log('  deploy: npx wrangler deploy (after KV ids and secrets)');
    log('  verify: /health, /status (401), /status.json, /status/refresh, /graph/health, heartbeat');
    return;
  }

  const values = SKIP_SECRETS ? new Map() : await provisionSecrets(knownSecrets);
  if (!SKIP_SECRETS && values.size === 0 && process.env.NEO4J_PASSWORD) values.set('NEO4J_PASSWORD', process.env.NEO4J_PASSWORD);
  if (!SKIP_SECRETS && process.env.STATUS_TOKEN) values.set('STATUS_TOKEN', process.env.STATUS_TOKEN);

  const url = SKIP_DEPLOY ? (process.env.WORKER_URL || '') : deploy();
  if (SKIP_DEPLOY) step('Deploy — skipped');

  if (!SKIP_VERIFY) {
    await verify(url, values);
    await verifySchedules();
    await verifyHeartbeat(values);
  } else {
    step('Verification — skipped');
  }

  if (TEST_CRONS) await testCrons(values);

  step('Summary');
  log(`  failures: ${failures}`);
  log(`  warnings: ${warnings}`);
  if (existsSync(GENERATED_PATH)) {
    log(`  generated tokens: ${GENERATED_PATH} (mode 0600, git-ignored)`);
    log('  move them into your password manager and delete the file when done.');
  }
  if (failures > 0) process.exit(1);
  if (REQUIRE_HEARTBEAT) {
    // verifyHeartbeat already printed the reason; a missing node is fatal here.
    const heartbeat = await neo4jQuery('MATCH (h:_PuppetNetHeartbeat) RETURN count(h) AS n', values);
    if (!heartbeat.ok) {
      log(`  heartbeat count unavailable: ${heartbeat.error}`);
      process.exit(1);
    }
    const count = Number(heartbeat.results?.[0]?.data?.[0]?.row?.[0] || 0);
    if (!count) process.exit(1);
  }
}

export { PLACEHOLDER, kvIds, setKvIds, parseNamespaceId, neo4jBase, parseArgs };

if (isMain) main().catch((error) => fatal(error.stack || error.message));
