# Deployment

How to host the app privately for invited users, with HTTPS, on one server. The same accounts
work in the web app and in MCP clients such as Claude Code.

```
Internet ──443──▶ Caddy (HTTPS, certificates) ──▶ API (FastAPI, React interface, /mcp)
                                                    ├─▶ PostgreSQL (accounts, sessions, conversations)
                                                    └─▶ Qdrant (knowledge base)
```

Only Caddy is reachable from outside. The API, PostgreSQL and Qdrant listen on the private
Docker network.

## What you need

- A Linux server with Docker, **4 GB of memory** and 30 GB of disk. In use the stack takes about
  3 GB: the API about 2 GB (it holds the question-screening models), Qdrant about 0.5 GB, and
  PostgreSQL and Caddy little. Add a 4 GB swap file; the image build needs the headroom.
- A domain name, for example `research.example.com`, with a DNS `A` record pointing at the server.
  Ports 80 and 443 must be open; Caddy uses them to obtain the certificate.
- API keys for the models you use (see `.env.example`) and a Voyage key for query embeddings.

## 1. Configure

Copy the repository to the server and create `.env` from `.env.example`. In addition to the model
keys, set:

```sh
ENVIRONMENT=production
DOMAIN=research.example.com
PUBLIC_URL=https://research.example.com

# Long random values: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET=...
POSTGRES_PASSWORD=...

# The MCP endpoint only answers requests addressed to these hosts (DNS-rebinding protection).
MCP_ALLOWED_HOSTS=["research.example.com"]
MCP_ALLOWED_ORIGINS=["https://research.example.com"]

QDRANT_COLLECTION=financial_docs_v3

# Optional: trace every question and MCP search in LangSmith (project rag-agent-prod).
# Questions and answers are then stored by LangSmith; leave it off to keep them on this server.
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=...
```

The API refuses to start in production with the default `JWT_SECRET` or database password, a
`JWT_SECRET` shorter than 32 characters, a wildcard `CORS_ORIGINS`, or a `PUBLIC_URL` that is not
`https://`. In production the refresh cookie is sent over HTTPS only, the API sends HSTS, and the
interactive API docs (`/docs`) are off.

Limits worth reviewing for your budget (defaults shown):

| Setting | Default | Meaning |
| --- | --- | --- |
| `QUESTIONS_PER_HOUR` / `QUESTIONS_PER_DAY` | 60 / 300 | Questions per user to the in-app assistant |
| `MCP_RATE_LIMIT_PER_MINUTE` | 30 | Searches and page reads per user from MCP clients |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Account lockout after wrong passwords |
| `ACCESS_TOKEN_MINUTES` | 60 | Lifetime of an access token before it is renewed (a revoked session stops at once) |
| `MAX_CONCURRENT_QUESTIONS` | 0 (no limit) | Questions answered at once; others wait their turn. Set 1–2 on free model tiers, which refuse bursts |
| `RERANKER_LOCAL_FALLBACK` | off in production | If Voyage reranking fails, keep the search order rather than load a 2 GB local model |
| `SESSION_IDLE_DAYS` / `SESSION_MAX_DAYS` | 7 / 30 | When a browser or MCP client must sign in again |
| `GROQ_DAILY_TOKEN_LIMIT` | 200,000 | Groq's daily tokens per model, for the Health page's allowance estimate |
| `METRICS_RETENTION_DAYS` | 90 | Days of per-question and MCP-call summaries kept for the Health page |

## 2. Bring the knowledge base

Copy the collection the app searches rather than embedding the corpus again. A Qdrant snapshot of
one collection is about 450 MB. On the machine that has it:

```sh
curl -X POST "http://localhost:6333/collections/financial_docs_v3/snapshots?wait=true"
# note the "name" in the reply, then download it:
curl -o financial_docs_v3.snapshot \
  "http://localhost:6333/collections/financial_docs_v3/snapshots/<name>"
```

Copy `financial_docs_v3.snapshot` to the server. Qdrant is not reachable from outside there, so
start it alone and upload the snapshot from a container on the same Docker network:

```sh
docker compose -f compose.minimal.yml -f compose.production.yml up -d qdrant
docker run --rm --network <project>_rag-net -v "$PWD":/backup:ro curlimages/curl:8.10.1 \
  -sS -X POST "http://qdrant:6333/collections/financial_docs_v3/snapshots/upload?priority=snapshot" \
  -F "snapshot=@/backup/financial_docs_v3.snapshot"
```

`<project>` is the Compose project name: `COMPOSE_PROJECT_NAME` in `.env`, or the folder name.
Alternatively restore the published snapshot and build the contextual collection as in
[local setup](../LOCAL_SETUP.md#3-restore-an-empty-knowledge-base) (this embeds again).

## 3. Start

```sh
docker compose -f compose.minimal.yml -f compose.production.yml up -d --build
docker compose -f compose.minimal.yml -f compose.production.yml logs -f caddy api
```

Caddy obtains the certificate on first start. On the first start the API also downloads its
question-screening models (a few hundred MB, kept in the `hf_cache` volume) in the background; the
log says `Question-screening models ready` when done. Questions asked before then wait for it.

Then create the accounts; each password is printed once:

```sh
docker compose -f compose.minimal.yml -f compose.production.yml exec api python scripts/manage_users.py seed
```

Keep the `admin` account for yourself and hand out the others. Open
`https://research.example.com/`, sign in as admin and check **Users** and **Activity**.

## 4. Invite people

- Give each person their username, password and the address. They can change the password under
  **Account**.
- For Claude Code they run `claude mcp add --transport http financial-research
  https://research.example.com/mcp`, then `/mcp` → **Authenticate**, and approve in the browser.
  The **Connect Claude Code** page in the app shows the exact command.
- To remove someone: **Users** → *Manage* → **Deactivate**. Their browser and MCP sessions end at
  once: every request checks that the account is active and the session open, so their next action
  is refused.

## Operations

**Backups.** Back up PostgreSQL (accounts, conversations, approvals) daily. `deploy/backup.sh`,
installed by `make deploy` as `/opt/fra/backup.sh`, writes a dump to `/opt/fra/backups` and keeps 14
days; run it nightly:

```sh
echo '20 3 * * * root /opt/fra/backup.sh >> /var/log/fra-backup.log 2>&1' > /etc/cron.d/fra-backup
```

Copy the dumps off the server now and then. To restore one:
`docker compose ... exec -T postgres pg_restore -U rag_user -d rag_agent --clean < file.dump`.

The Qdrant volume changes only when the knowledge base is rebuilt; keep the snapshot from step 2.

After each dump the script writes the time and size to `/opt/fra/status/backup.json`, which the API
reads (read-only, never the dumps themselves) for the Health page.

**Health page.** Administrators have a **Health** page: whether the website, answers, search,
reranker and backups are working; questions, answer times and MCP searches over 24 hours, 7 days or
30 days; how much of Groq's daily allowance is left (estimated from this server's own questions and
the limits Groq reports with each reply); AI figures such as tokens per question, cost, confidence,
retried searches, revised answers and answers slowed by a provider's per-minute limit; where the time goes by pipeline step; and recent failures,
each linked to its LangSmith trace when tracing is on. It reads one summary row per question and
per MCP call, which the API keeps for `METRICS_RETENTION_DAYS` (90). Nothing on the page calls a
model. The figures appear as soon as people use the app; a restart only forgets Groq's latest
reported limits until the next question.

**Tracing.** With `LANGCHAIN_TRACING_V2=true` and a `LANGCHAIN_API_KEY`, each question is a
LangSmith run whose ID is the request ID, and each MCP tool call
is a run named `mcp.<tool>` (the caller's token is never sent). Users never see request IDs;
administrators reach a trace from the Health page's recent problems (listed with a short "Ref") and
from an approval request (**Open the trace in LangSmith**). Production writes no per-question
report files (`REQUEST_TRACE_FILES=false`); locally they are kept under `logs/traces` for debugging
and the FinanceBench scripts.

**Updates.** From your own machine, `make deploy` sends the committed code to the server, builds
the new image there, switches to it and waits until the API reports healthy. It needs the Google
Cloud CLI signed in and `deploy/target.env` (see `deploy/target.env.example`). A failed build leaves
the running release untouched. On a server you manage by hand:

```sh
git pull
docker compose -f compose.minimal.yml -f compose.production.yml up -d --build api
```

Account tables are created or extended automatically at start-up.

**Automatic deploys.** `.github/workflows/ci.yml` runs the tests on every push and pull request and,
for pushes to `main` that pass, sends the release to the server. On the server a `deploy` user holds
one SSH key whose only permitted command is `/opt/fra/ci-receive.sh`, which reads the release from
standard input and runs `/opt/fra/server-update.sh` through `sudo`; the key cannot open a shell or
read the settings. `make deploy` installs both scripts. Create the user and the key once:

```sh
ssh-keygen -t ed25519 -N "" -C github-actions-deploy -f ci_deploy_key
# on the server, as root:
useradd --create-home --shell /bin/sh deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
echo 'command="/opt/fra/ci-receive.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty <contents of ci_deploy_key.pub>' \
  > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
echo 'deploy ALL=(root) NOPASSWD: /opt/fra/server-update.sh' > /etc/sudoers.d/fra-deploy
chmod 440 /etc/sudoers.d/fra-deploy
```

Then add four repository secrets (Settings → Secrets and variables → Actions, or `gh secret set`):
`DEPLOY_HOST` (the server's address), `DEPLOY_SSH_KEY` (the private key), `DEPLOY_KNOWN_HOSTS`
(the output of `ssh-keyscan -t ed25519 <address>`) and `DEPLOY_URL` (such as
`https://research.example.com`). Without `DEPLOY_HOST` the deploy job does nothing.

**Checking a deployment.** `frontend/tests/live-check.mjs` drives a real browser against a running
server: sign-in, a real question, the account page, an MCP client connecting through OAuth, every
admin action, the lockout, the phone layout and the theme. It creates a temporary account and
deactivates it at the end.

```sh
cd frontend
BASE=https://research.example.com ADMIN_PASSWORD=... node tests/live-check.mjs   # ASK=0 skips the question
```

Two more checks for a running server, both cleaning up after themselves:

- `tests/security-check.mjs` (no answer-model calls): signed-out and forged tokens, other users'
  conversations and approvals, admin endpoints as a non-admin, document access by role, path
  tricks, refresh-cookie flags and reuse detection, OAuth redirect and PKCE rules, input limits.
- `tests/review-check.mjs` (asks two questions): an HR user's explicit access request is held, an
  administrator rejects one with a reason and approves the other, and the user sees both outcomes.

On a free model tier, run the checks that ask questions sparingly: they use the same daily
allowance as your users.

**Rotating the signing key.** Changing `JWT_SECRET` and restarting ends every access token at
once; browsers and MCP clients renew with their refresh tokens.

**Logs.** `docker compose ... logs api` includes one audit line per MCP tool call
(`src.mcp_server.audit`). Sign-ins, failed attempts, lockouts, MCP connections and account changes
are in the app's **Activity** page (kept 180 days); service status, usage and failures are on the
**Health** page.

**One API process.** Sign-in attempt limits and MCP call limits are counted in memory per process;
question quotas are counted in PostgreSQL. Run a single API container (the default).

## Security summary

| Area | Measure |
| --- | --- |
| Accounts | No sign-up; created by an administrator. Passwords hashed with scrypt; minimum 12 characters. |
| Sign-in | Lockout after repeated failures; attempts limited per IP; every attempt logged. |
| Web sessions | 60-minute access token in page memory only; rotating refresh token in an httpOnly, `Secure`, `SameSite=Strict` cookie limited to `/v1/auth`; reuse of an old refresh token ends the session. |
| MCP | OAuth 2.1 with PKCE and dynamic registration; consent by the signed-in user; tokens bound to the MCP address; every call checks that the account is active and the connection open; rotating refresh tokens. |
| Revocation | Password change, sign-out-everywhere and deactivation take effect immediately: every web and MCP request checks the account and session, so the next one is refused. |
| Transport | HTTPS only via Caddy; HSTS; DNS-rebinding check on `/mcp`. |
| Browser | Content Security Policy (own scripts only, no framing), `nosniff`, no referrer. |
| Cost control | Per-user question quotas; per-user MCP call limits; registration limits per IP. |
