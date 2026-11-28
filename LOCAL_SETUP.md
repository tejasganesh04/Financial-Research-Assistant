# Local setup

FastAPI serves the React build and API together at **http://localhost:8000/**.
Docker also runs Qdrant and PostgreSQL. Use Docker Desktop and Docker
Compose; the image build compiles the React interface itself, so Node is only
needed on your machine for frontend development.

## 1. Configure the app

Copy `.env.example` to `.env` if you do not already have one. Keep `.env` private.
The default template selects Groq-only language-model calls:

| Setting | Purpose |
| --- | --- |
| `GROQ_API_KEY` | GPT-OSS 20B and 120B language-model calls |
| `VOYAGE_API_KEY` | New query embeddings in the snapshot's vector space |
| `POSTGRES_PASSWORD` | Private password for the local database |
| `JWT_SECRET` | Private signing key for access tokens (at least 32 characters) |
| `EMBEDDING_MODEL=voyage-finance-2` | Must match the published snapshot |
| `EMBEDDING_DIMENSIONS=1024` | Must match the published snapshot |

Use a private random database password and JWT secret. For example, generate
values locally with `python -c "import secrets; print(secrets.token_hex(32))"`.

OpenAI and Anthropic models are also supported. `compose.benchmark.yml` switches
every step to OpenAI models for the answer benchmark (see the
[evaluation story](docs/evaluation-story.md#reproducing-the-numbers)); it needs
`OPENAI_API_KEY`. Selecting a provider profile does not change the required Voyage
embedding model.

`VOYAGE_API_KEY` also powers the default reranker. Voyage's free embedding
allowance covers this project, but note two things before using its reranker:

- **Cost:** rerank-2.5, the measured setup, is billed at $0.05 per million tokens
  (about $0.001 per question). `VOYAGE_RERANK_MODEL=rerank-3` lists 200M free
  tokens; `RERANKER_PROVIDER=local` uses a free local model instead.
- **Rate limits:** without a payment method, Voyage allows about 3 requests and
  10,000 tokens per minute. A reranking call is larger than that, so the app falls
  back to the local model, and building the contextual knowledge base takes
  more than a day. Adding a payment method raises the limits.

## 2. Start the services

From the project root:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml up -d --build
```

The Dockerfile builds React and installs the Python packages pinned in
`requirements.lock`; the first build takes about 5–10 minutes, and later builds
reuse Docker's cache. Local bind mounts supply the application source; downloaded
model files are kept in a separate Docker volume. Initial model loading can take time.

Open **http://localhost:8000/**. API schemas are at **http://localhost:8000/docs**.

The `Makefile` wraps the commands in this guide: `make up`, `make seed-db`,
`make build-kb`, `make restart`, `make logs` and `make down`.

## 3. Restore an empty knowledge base

Skip this step when the existing Qdrant volume is already populated.

```sh
docker compose -f compose.minimal.yml -f compose.local.yml exec -T api python scripts/seed_from_hf.py --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings --revision c31f0efb6eea1f33310eb82fed4d3b940367c21b
```

The script checks the snapshot's embedding model, dimensions and sparse-vector
format. It refuses to replace an existing collection unless `--replace` is
explicitly supplied. Restoration reuses precomputed vectors; questions can still
require new query embeddings. See [data provenance](DATA_SOURCES.md).

### Build the contextual knowledge base

The app searches `financial_docs_v3`, a copy of the restored collection whose
chunks carry a header naming the company, filing, section, financial statement
and units. Build it once after restoring (about 16M Voyage tokens on the first
build, inside the free allowance; reruns re-embed only changed chunks):

```sh
docker compose -f compose.minimal.yml -f compose.local.yml exec -T api python scripts/build_contextual_collection.py --target financial_docs_v3
```

Then set `QDRANT_COLLECTION=financial_docs_v3` in `.env` and restart the API.

## Accounts and permissions

There is no sign-up. Create one account per role once the API is running; each password is
generated, printed once and stored only as a scrypt hash:

```sh
make users          # creates admin, analyst, finance, hr and executive (existing ones are kept)
make users-list     # status, open sessions and last sign-in
make user-password USER=analyst   # set a new password; ends that user's sessions
```

`scripts/manage_users.py` also creates further accounts and signs out, deactivates or unlocks
them. Administrators can do the same from the **Users** page in the app.

| Username | Role | Search permissions | Review answers? |
| --- | --- | --- | --- |
| `analyst` | Analyst | Public 10-K, 10-Q and 8-K filings | No |
| `finance` | Finance | Filings, invoices and expense policies; public/internal | No |
| `hr` | HR | Expense policies; public/internal | No |
| `executive` | Executive (`c_level`) | All supported types and confidentiality levels | No |
| `admin` | Administrator | All supported types and confidentiality levels | Yes |

Permissions are defined in `src/config/rbac_config.py`. The active dataset contains public
filings, so supported permission categories do not imply that matching documents are loaded.

### Sign-in sessions

- Signing in returns a 60-minute access token, kept only in the page's memory, and sets a refresh
  token in an httpOnly, `SameSite=Strict` cookie that page scripts cannot read. The page renews
  the access token before it expires and after a reload.
- A browser holds one account at a time, because every tab shares the refresh cookie. Signing in
  to another account in any tab ends the previous session and signs the other tabs out with a
  message; use a private window to work as two accounts at once.
- Refresh tokens rotate on every use; reusing an old one ends the session. A session ends after
  7 days without use or 30 days after sign-in (`SESSION_IDLE_DAYS`, `SESSION_MAX_DAYS`).
- Five wrong passwords in a row lock the account for 15 minutes; sign-in attempts are also
  limited per IP address. Each user may ask 60 questions an hour and 300 a day (`.env`).
- Changing a password, "sign out everywhere" and deactivation take effect at once: every request
  checks that the account is active and the session open, so the next one from that browser or AI
  assistant is refused.

Normal questions search within the user's permissions. To request extra access for one
question, a user selects **Ask with extra access** (on the welcome screen or below the message
box) and ticks the documents they need; the welcome screen lists what their role can search.
The app sends the request as a first line such as `/escalate doc_type=filings`, which can also be
typed directly.

Requested values are added to the user's ordinary scope for that question. A
supported drafted answer from an expanded search waits for Admin approval.
Approval releases that answer; it does not permanently change the role.

## Connect an AI assistant (MCP)

The API serves document search for MCP clients at `http://localhost:8000/mcp`, signed in with
the same accounts through OAuth:

```sh
claude mcp add --transport http financial-research http://localhost:8000/mcp
```

In Claude Code, run `/mcp`, choose `financial-research` and **Authenticate**. The browser opens
the app's consent page; sign in and select **Allow access**. The **Connect Claude Code** page in
the app shows the same steps with the configured address. See the [MCP guide](docs/mcp-server.md).

## Storage and request traces

- **Qdrant:** indexed chunks, metadata, dense vectors and sparse vectors.
- **PostgreSQL:** accounts, sessions, OAuth clients, the activity log, question quotas, graph
  checkpoints, conversations and approval state.
- **`logs/traces/`:** local request reports, excluded from Git.

Each question's report is saved under `logs/traces/<request ID>/`. Users never see request IDs;
administrators find them on the **Health** page (recent problems) and in LangSmith, where a
question's run ID is its request ID. Provider-reported usage feeds token counts; dollar costs are
estimates and can be incomplete when a model has no configured price.

Optional source PDFs belong in `data/sample/`, matching the indexed filename.
PDFs and downloaded datasets are not included in Git.

## Restart, check and stop

After changing `.env`, recreate the API:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml up -d --no-deps --force-recreate api
```

Health checks do not generate answers or call model APIs:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml ps
curl http://localhost:8000/v1/health
```

Questions can consume provider credits. When a provider's limit is reached, the browser shows a
plain message and when to try again; the provider, model and step are recorded for administrators
on the Health page and in the trace. Do not repeatedly submit a failed request.

To stop services while preserving stored data:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml down
```

Adding `--volumes` deletes persistent service data.

## Development

For frontend development, keep the backend running and use:

```sh
cd frontend
npm ci
npm run dev
```

Vite serves port 5173 and forwards `/v1` requests to FastAPI. For backend work,
use Python 3.11–3.13 and install this project's test dependencies:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest tests/unit tests/integration
```

Account, session and OAuth tests need an isolated database: create one whose name ends in
`_test` and pass it as `TEST_POSTGRES_URL` (the tests drop and recreate the account tables):

```sh
docker compose -f compose.minimal.yml -f compose.local.yml exec postgres createdb -U rag_user rag_agent_test
TEST_POSTGRES_URL=postgresql://rag_user:<password>@localhost:5432/rag_agent_test .venv/bin/python -m pytest tests/integration
```

Keep test databases separate from the working app. Browser tests use local Chrome and mocked API
responses; `npm test` and `npm run build` run from `frontend/`.
