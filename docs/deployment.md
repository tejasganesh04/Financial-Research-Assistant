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
LANGCHAIN_TRACING_V2=false
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
| `ACCESS_TOKEN_MINUTES` | 60 | Longest time a revoked session keeps working |
| `RERANKER_LOCAL_FALLBACK` | off in production | If Voyage reranking fails, keep the search order rather than load a 2 GB local model |
| `SESSION_IDLE_DAYS` / `SESSION_MAX_DAYS` | 7 / 30 | When a browser or MCP client must sign in again |

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
  once; an access token already issued stops working within `ACCESS_TOKEN_MINUTES`.

## Operations

**Backups.** Back up PostgreSQL (accounts, conversations, approvals) daily, for example with cron:

```sh
docker compose -f compose.minimal.yml -f compose.production.yml exec -T postgres \
  pg_dump -U rag_user -Fc rag_agent > backups/rag_agent-$(date +%F).dump
```

The Qdrant volume changes only when the knowledge base is rebuilt; keep the archive from step 2.

**Updates.** From your own machine, `make deploy` sends the committed code to the server, builds
the new image there, switches to it and waits until the API reports healthy. It needs the Google
Cloud CLI signed in and `deploy/target.env` (see `deploy/target.env.example`). A failed build leaves
the running release untouched. On a server you manage by hand:

```sh
git pull
docker compose -f compose.minimal.yml -f compose.production.yml up -d --build api
```

Account tables are created or extended automatically at start-up.

**Rotating the signing key.** Changing `JWT_SECRET` and restarting ends every access token at
once; browsers and MCP clients renew with their refresh tokens.

**Logs.** `docker compose ... logs api` includes one audit line per MCP tool call
(`src.mcp_server.audit`). Sign-ins, failed attempts, lockouts, MCP connections and account changes
are in the app's **Activity** page (kept 180 days).

**One API process.** Sign-in attempt limits and MCP call limits are counted in memory per process;
question quotas are counted in PostgreSQL. Run a single API container (the default).

## Security summary

| Area | Measure |
| --- | --- |
| Accounts | No sign-up; created by an administrator. Passwords hashed with scrypt; minimum 12 characters. |
| Sign-in | Lockout after repeated failures; attempts limited per IP; every attempt logged. |
| Web sessions | 60-minute access token in page memory only; rotating refresh token in an httpOnly, `Secure`, `SameSite=Strict` cookie limited to `/v1/auth`; reuse of an old refresh token ends the session. |
| MCP | OAuth 2.1 with PKCE and dynamic registration; consent by the signed-in user; tokens bound to the MCP address; rotating refresh tokens checked against the account at renewal. |
| Revocation | Password change, sign-out-everywhere and deactivation end sessions immediately; access tokens expire within the hour. |
| Transport | HTTPS only via Caddy; HSTS; DNS-rebinding check on `/mcp`. |
| Browser | Content Security Policy (own scripts only, no framing), `nosniff`, no referrer. |
| Cost control | Per-user question quotas; per-user MCP call limits; registration limits per IP. |
