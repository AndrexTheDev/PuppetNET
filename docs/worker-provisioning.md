# Provisioning the Worker: KV, secrets, deploy and verification

This is the operator runbook for taking `apps/worker` from a repository checkout
to a live Worker with real KV bindings, secrets and cron schedules. It requires
a Cloudflare account on the Free plan, `wrangler login` (or
`CLOUDFLARE_API_TOKEN`), one AuraDB Free instance, and network access to
`api.cloudflare.com` and the database host.

The whole sequence is automated by **`scripts/provision-worker.mjs`**; every step
it performs is also listed manually below so it can be audited or reproduced.

---

## 1. One command

```bash
cd /path/to/PuppetNET
npx wrangler login
export NEO4J_URI='neo4j+s://<instance>.databases.neo4j.io:7687'
export NEO4J_USERNAME='neo4j'
export NEO4J_PASSWORD='...'
export CF_ACCOUNT_ID='<32-hex account tag>'
export CF_API_TOKEN='...'          # Account → Account Analytics → Read
# optional:
export TELEGRAM_BOT_TOKEN='123456:AA...'
export TELEGRAM_CHAT_ID='-1001234567890'

npm run provision:worker -- --dry-run   # plan only, no account calls
npm run provision:worker                # KV + secrets + deploy + verify
```

`--test-crons` additionally starts `wrangler dev --test-scheduled`, invokes both
schedules locally, and re-checks the AuraDB heartbeat. `--require-heartbeat`
makes a missing `:_PuppetNetHeartbeat` node a non-zero exit.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--dry-run` | print the plan; no account calls, writes or deployment |
| `--rotate` | rotate `PROXY_AUTH_TOKEN`, `GRAPH_API_TOKEN`, `STATUS_TOKEN` |
| `--force-kv` | create and bind new namespaces even if ids are present (orphans the old ones) |
| `--allow-missing-analytics` | deploy without `CF_API_TOKEN`/`CF_ACCOUNT_ID`; the card stays `n/a` |
| `--skip-kv` / `--skip-secrets` / `--skip-deploy` / `--skip-verify` | run one phase only |
| `--test-crons` | local scheduled() smoke + heartbeat |
| `--require-heartbeat` | fail if the heartbeat node is absent |

### What the script does

1. **KV namespaces.** Runs
   `wrangler kv namespace create PUPPETNET_RATE_LIMIT` and
   `... PUPPETNET_RESULTS`, plus a `--preview` sibling for each, parses the ids
   and replaces the 32-zero placeholders in `apps/worker/wrangler.toml`
   (`id` and `preview_id`). A binding that already has a non-zero id is kept
   unless `--force-kv` is passed. It then refuses to continue if any placeholder
   remains.
2. **Secrets.** Generates `PROXY_AUTH_TOKEN`, `GRAPH_API_TOKEN` and
   `STATUS_TOKEN` with `crypto.randomBytes(32).toString("hex")` when they do not
   exist (64 hex characters). Reads `NEO4J_URI`, `NEO4J_USERNAME`,
   `NEO4J_PASSWORD`, `CF_API_TOKEN`, `CF_ACCOUNT_ID` and the optional Telegram
   pair from the environment. Values go to `wrangler secret put` on **stdin**,
   never on the command line. Existing tokens are not rotated without
   `--rotate`. Generated values are written to
   `apps/worker/.secrets.generated.env` (mode 0600, git-ignored).
3. **Deploy.** `wrangler deploy`, then parses the `workers.dev` URL (or uses
   `WORKER_URL`).
4. **Verify.** `/health` (bindings and cron constants), `/status` without a token
   (must be 401), `/status.json` with the status token, `POST /status/refresh`
   (live AuraDB probe), `/graph/health`, the deployed cron schedules when the
   token has `Workers Scripts:Read`, and a Cypher query for
   `:_PuppetNetHeartbeat`.

**No secret is printed.** The script masks KV ids and only says whether a secret
was generated, supplied by the environment, or preserved.

### Offline rehearsal

`ci:worker` runs `tests/provision_e2e.test.mjs`, which copies the script and
`wrangler.toml` into a temp directory, puts a stub `npx` on `PATH` and performs a
full run: four namespace creates, id patching, ten secret uploads on stdin,
`deploy`. It asserts that the generated tokens are 64 hex characters and
distinct, that the file is 0600, and that a second run creates only the missing
namespace pair and uploads nothing. The real account calls remain the operator's
step.

---

## 2. Manual equivalent

### 2.1 KV

```bash
cd apps/worker
npx wrangler kv namespace create PUPPETNET_RATE_LIMIT
npx wrangler kv namespace create PUPPETNET_RATE_LIMIT --preview
npx wrangler kv namespace create PUPPETNET_RESULTS
npx wrangler kv namespace create PUPPETNET_RESULTS --preview
```

Copy the four ids into the two `[[kv_namespaces]]` blocks in
`apps/worker/wrangler.toml`:

```toml
[[kv_namespaces]]
binding = "RATE_LIMIT_KV"
id = "<production id>"
preview_id = "<preview id>"

[[kv_namespaces]]
binding = "RESULT_KV"
id = "<production id>"
preview_id = "<preview id>"
```

`wrangler deploy` fails against all-zero ids **on purpose**: a Worker whose locks
and snapshots silently fell back to isolate memory would look healthy and lose
every cross-isolate guarantee.

### 2.2 Secrets

```bash
cd apps/worker
openssl rand -hex 32 | npx wrangler secret put PROXY_AUTH_TOKEN
openssl rand -hex 32 | npx wrangler secret put GRAPH_API_TOKEN
openssl rand -hex 32 | npx wrangler secret put STATUS_TOKEN

npx wrangler secret put NEO4J_URI
npx wrangler secret put NEO4J_USERNAME
npx wrangler secret put NEO4J_PASSWORD
npx wrangler secret put CF_API_TOKEN
npx wrangler secret put CF_ACCOUNT_ID

# optional, for degradation alerts:
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
```

`npx wrangler secret list` shows the names only. Rotating a token invalidates
every client that still presents the old one — the Worker accepts comma-separated
`PROXY_AUTH_TOKENS` / `GRAPH_API_TOKENS` for zero-downtime rotation.

### 2.3 Deploy

```bash
cd apps/worker
npx wrangler deploy
```

`wrangler deploy` also applies `[triggers]`. Wrangler 4 no longer has a
`wrangler triggers list` command; confirm the schedules in
**Dashboard → Workers & Pages → puppetnet-edge-relay → Settings → Trigger
Events → Cron Triggers**, or through the API:

```bash
curl -s -H "Authorization: Bearer $CF_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/workers/scripts/puppetnet-edge-relay/schedules" | jq
```

The token needs **Workers Scripts:Read** for this call; the analytics token used
by the dashboard does not have to include it. If the Worker script name ever
changes, export `CF_WORKER_SCRIPT_NAME` so the check follows it.

---

## 3. Verify

```bash
BASE=https://puppetnet-edge-relay.<subdomain>.workers.dev

curl -s "$BASE/health" | jq
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/status"           # 401
curl -s -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status.json" | jq
curl -s -X POST -H "Authorization: Bearer $STATUS_TOKEN" "$BASE/status/refresh" | jq
curl -s "$BASE/graph/health" | jq '.graph.counts'
```

What to look for:

* `/health` → `ok: true`, `bindings.rate_limit_kv` and `result_kv` true,
  `cron.keepalive: "0 */12 * * *"`, `cron.health: "23 */3 * * *"`.
* `/status` without a token → `401` and `WWW-Authenticate`.
* `/status.json` → `kv.bound: true`, `rate.graph_rpm: 25`; after a refresh,
  `aura.available: true` with real node counts, and `workers.source:
  "cloudflare_graphql"` when the analytics token is correct.
* `/status` HTML → German dashboard, no script tags.

### 3.1 Local cron smoke

`wrangler dev --test-scheduled` does **not** touch the deployed cron schedule; it
runs the same `scheduled()` handler locally against the real database. It needs
the Neo4j secrets in `apps/worker/.dev.vars` (git-ignored; the provisioning
script writes it when `--test-crons` is used).

```bash
cd apps/worker
npx wrangler dev --test-scheduled
# second shell:
curl "http://127.0.0.1:8787/__scheduled?cron=0+%2A%2F12+%2A+%2A+%2A"
curl "http://127.0.0.1:8787/__scheduled?cron=23+%2A%2F3+%2A+%2A+%2A"
```

Alternatively, the dashboard can trigger a cron immediately (Worker → Settings →
Trigger Events → Cron Triggers → Trigger) where that control is available.
Otherwise wait for the next UTC slot (`00:00/12:00` for the heartbeat, `:23`
every three hours for the snapshot).

### 3.2 Heartbeat in AuraDB

The keep-alive schedule writes exactly one node. Confirm it in the Aura query
console:

```cypher
MATCH (h:_PuppetNetHeartbeat)
RETURN h.id AS id, h.last_seen AS last_seen, h.worker AS worker, h.version AS version
ORDER BY h.last_seen DESC
LIMIT 1;
```

Expected: `puppetnet-edge-cron` (or `KEEPALIVE_NODE_ID`), a fresh timestamp and
`puppetnet-edge-relay`. In the Aura browser or `cypher-shell`:

```bash
cypher-shell -a "$NEO4J_URI" -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (h:_PuppetNetHeartbeat) RETURN h.id, h.last_seen ORDER BY h.last_seen DESC LIMIT 1;"
```

The provisioning script performs the same check over the HTTP transaction API
and does not print the password.

---

## 4. Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `wrangler` says *not authenticated* | no login in this shell | `npx wrangler login`, or export `CLOUDFLARE_API_TOKEN` |
| `wrangler deploy` rejects a KV id | placeholder zeros still in `wrangler.toml`, or a namespace from another account | re-run the provisioning script; check the id belongs to the account |
| `KV namespace ... not found` | id copied from a different account/environment | create the namespace in the deploying account and paste that id |
| `/status` returns 401 with the token | wrong `STATUS_TOKEN`, or the Worker was deployed before the secret | `npx wrangler secret put STATUS_TOKEN`, redeploy if needed |
| `/status.json` shows `n/a` for Workers | analytics token missing or lacks **Account Analytics: Read**, or `CF_ACCOUNT_ID` is wrong | create a read-only token; `CF_WORKER_SCRIPT_NAME` must match the Worker name |
| `/status/refresh` says `neo4j_error` | wrong password/URI, or the Aura instance is paused | check `NEO4J_*`; wake the instance in the Aura console |
| `alert.skipped: throttled` | an alert was sent in the last six hours | expected; `retry_in_seconds` in the JSON says when |
| `alert.skipped: telegram_error` | wrong chat id, bot not in the chat, or a 3xx | send `/start` to the bot, verify the chat id, check the redacted detail |
| cron did not fire | Cloudflare schedules are UTC; a fresh deploy may take a minute | check Trigger Events; a local `--test-crons` proves the handler |
| `1027` / `511` | Workers Free daily request quota exhausted | wait until 00:00 UTC; the cron is blocked too, and the Python heartbeat is the fallback |
| a second namespace with the same title | an earlier run created it but died before `wrangler.toml` was written, or `--force-kv` was used | list them with `npx wrangler kv namespace list` and delete the unused ones in the dashboard |
| schedule query returns 403 | the analytics token has no **Workers Scripts:Read** | confirm the crons in the dashboard; the API check is optional |

---

## 5. Security notes

* `apps/worker/.secrets.generated.env` contains live credentials. It is
  git-ignored and 0600. Move the values into a password manager and delete it.
* Never put a secret on a command line (`wrangler secret put NAME value` is
  logged by shell history); the script uses stdin.
* `.dev.vars` is git-ignored and has the same handling rules.
* The KV namespace ids are not secrets, but they are account-scoped; treat them
  as infrastructure identifiers, not public configuration.
* Rotating `PROXY_AUTH_TOKEN` invalidates the Python harvester until
  `PROXY_AUTH_TOKEN` is updated on that side too. Use the plural token variables
  for a zero-downtime overlap.
* `STATUS_PUBLIC=true` makes the read-only dashboard public. It never exposes the
  live-probe path, and it contains node counts and the Worker version — decide
  consciously whether that is acceptable.

---

## 6. What the repository cannot do for you

Creating namespaces, setting secrets and deploying require **your** Cloudflare
account. The repository ships the placeholders, the script and the exact checks;
the credentials never enter Git. The same applies to the AuraDB password and the
Cloudflare analytics token.
