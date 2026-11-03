# Local setup

FastAPI serves the React build and API together at **http://localhost:8000/**.
Docker also runs Qdrant, PostgreSQL and Redis. Use Docker Desktop and Docker
Compose; Node is needed for the image build, not the running API.

## 1. Configure the app

Copy `.env.example` to `.env` if you do not already have one. Keep `.env` private.
The default template selects Groq-only language-model calls:

| Setting | Purpose |
| --- | --- |
| `GROQ_API_KEY` | GPT-OSS 20B and 120B language-model calls |
| `VOYAGE_API_KEY` | New query embeddings in the snapshot's vector space |
| `POSTGRES_PASSWORD` | Private password for the local database |
| `JWT_SECRET` | Private signing key for demo login tokens |
| `EMBEDDING_MODEL=voyage-finance-2` | Must match the published snapshot |
| `EMBEDDING_DIMENSIONS=1024` | Must match the published snapshot |

Use a private random database password and JWT secret. For example, generate
values locally with `python -c "import secrets; print(secrets.token_hex(32))"`.

Optional OpenAI/Anthropic settings remain in the template. To use that profile,
configure the corresponding provider keys and set `FORCE_GROQ_ONLY=false`,
`FORCE_OPENAI_ONLY=false` and `USE_GROQ_FAST_PATH=false`. Selecting a provider
profile does not change the required Voyage embedding model.

## 2. Start the services

From the project root:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml up -d --build
```

The Dockerfile builds React and uses a published Python dependency image.
Local bind mounts supply the application source; downloaded model files are
kept in a separate Docker volume. Initial model loading can take time.

Open **http://localhost:8000/**. API schemas are at **http://localhost:8000/docs**.
The local Redis port is **16380** to avoid conflicts with another local Redis.

## 3. Restore an empty knowledge base

Skip this step when the existing Qdrant volume is already populated.

```sh
docker compose -f compose.minimal.yml -f compose.local.yml exec -T api python scripts/seed_from_hf.py --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings --revision c31f0efb6eea1f33310eb82fed4d3b940367c21b
```

The script checks the snapshot's embedding model, dimensions and sparse-vector
format. It refuses to replace an existing collection unless `--replace` is
explicitly supplied. Restoration reuses precomputed vectors; questions can still
require new query embeddings. See [data provenance](DATA_SOURCES.md).

## Demo accounts and permissions

| Username | Password | Search permissions | Review answers? |
| --- | --- | --- | --- |
| `analyst` | `analyst123` | Public 10-K, 10-Q and 8-K filings | No |
| `finance` | `finance123` | Filings, invoices and expense policies; public/internal | No |
| `hr` | `hr123` | Expense policies; public/internal | No |
| `clevel` | `clevel123` | All supported types and confidentiality levels | No |
| `admin` | `admin123` | All supported types and confidentiality levels | Yes |

Accounts are fixed in `src/api/routes/auth.py`; permissions are defined in
`src/config/rbac_config.py`. The active dataset contains public filings, so
supported permission categories do not imply that matching documents are loaded.

Normal questions search within the user's permissions. To request extra access,
place an explicit command on the first line, for example as HR:

```text
/escalate doc_type=filings
What was Microsoft's total revenue in FY2016?
```

Requested values are added to the user's ordinary scope for that question. A
supported drafted answer from an expanded search waits for Admin approval.
Approval releases that answer; it does not permanently change the role.

## Storage and request traces

- **Qdrant:** indexed chunks, metadata, dense vectors and sparse vectors.
- **PostgreSQL:** graph checkpoints, conversations and approval state.
- **Redis:** cached query embeddings, reranker scores and relevance judgments.
- **`logs/traces/`:** local request reports, excluded from Git.

Expand **Request details** beneath a question to find its request ID and report
path. Provider-reported usage feeds token counts; dollar costs are estimates and
can be incomplete when a model has no configured price.

Redis caches use database 1. `RAG_RESULT_CACHE_TTL_SECONDS` sets the lifetime of
new entries, seven days by default. Existing entries keep their current expiry.
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

Questions can consume provider credits. Provider limits are shown in the browser
with the model and workflow stage; do not repeatedly submit a failed request.

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

Service integration tests need explicit test configuration. Keep test databases
separate from the working demo. Browser tests use local Chrome and mocked API
responses; `npm test` and `npm run build` run from `frontend/`.
