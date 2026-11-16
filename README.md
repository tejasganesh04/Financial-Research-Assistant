# Financial Research Assistant

A private financial-document research app with a React interface, FastAPI backend and
LangGraph workflow. Invited users ask questions in natural language and receive
answers grounded in documents available to their role. Explicit requests for
extra access hold the drafted answer for Admin review before release. The same
accounts can connect their own AI assistant, such as Claude Code, over MCP.

## Features

- Invitation-only accounts (no sign-up) stored in PostgreSQL with scrypt password hashes,
  one-hour access tokens, rotating refresh tokens in an httpOnly cookie, account lockout and
  per-user question limits. Admins reset passwords, sign people out everywhere and deactivate
  accounts from the Users page; every sign-in and change is in the Activity log.
- Role-based search permissions, applied in the app and over MCP.
- Input privacy filtering and two model-based prompt-injection checks.
- Simple lookups with hybrid retrieval, reranking, relevance grading and bounded search retries.
- A research path that decomposes questions, collects evidence and checks for missing information.
- An exact calculator the answer model calls for arithmetic; answers show each formula with its
  numbers, and the grounding check verifies a calculation's inputs instead of redoing it.
- Answer grounding checks with feedback for a bounded revision.
- Admin approval or rejection of explicitly escalated answers.
- Saved conversations, live processing stages and document/page references.
- Local request traces with workflow decisions, timings and provider-reported token usage.
- An [MCP server](docs/mcp-server.md) so employees' own AI assistants, such as Claude Code, can search
  the same documents under the same role permissions.

## Run locally

See [local setup](LOCAL_SETUP.md) for required keys, storage and first-time setup.
With Docker Desktop running and a configured `.env`:

```sh
docker compose -f compose.minimal.yml -f compose.local.yml up -d --build
```

Create the accounts (one per role; the generated passwords are printed once):

```sh
make users
```

Open **http://localhost:8000/** and sign in. API documentation (development only):
**http://localhost:8000/docs**. To host it for others, see [deployment](docs/deployment.md).

The default demo profile uses **Groq GPT-OSS 20B** for smaller model tasks and
**GPT-OSS 120B** for synthesis, answers and grounding checks. **Voyage Finance 2**
embeds new search queries. OpenAI and Anthropic configurations remain supported.
Model requests are subject to each provider's usage limits and pricing.

## Knowledge base

The published snapshot contains **68,059 chunks from 84 public filings across
32 companies**, including document metadata, precomputed dense vectors and sparse
BM25 vectors. The seed script loads these into Qdrant without embedding the corpus
again. A second script builds the collection the app searches, where every chunk
starts with a header naming its company, filing, page, section, financial statement
and units ([local setup](LOCAL_SETUP.md#build-the-contextual-knowledge-base)).
[Data sources and terms](DATA_SOURCES.md) identify the snapshot and source corpus.

The permission model supports additional document types and confidentiality
levels, but the active snapshot contains public filings only. Source references
include filenames and pages; a PDF can be opened only when the original file is
available locally and the user's role permits access.

## Workflow

![LangGraph workflow](docs/diagrams/langgraph-workflow.png)

[Zoomable SVG](docs/diagrams/langgraph-workflow.svg) ·
[Mermaid source](docs/diagrams/langgraph-workflow.mmd)

The research branch is one graph node. Its internal search loop reuses the
retrieval, reranking and grading functions, then returns evidence and a synthesis
to the shared answer-generation path.

![Research agent workflow](docs/diagrams/research-agent-workflow.png)

[Zoomable SVG](docs/diagrams/research-agent-workflow.svg) ·
[Mermaid source](docs/diagrams/research-agent-workflow.mmd)

## Evaluation

Measured on the 150 public [FinanceBench](https://huggingface.co/datasets/PatronusAI/financebench)
questions. The search check asks whether the page holding the answer reaches the
answer model; the answer benchmark judges 25 full answers against reference answers.

| Version | Search check (of 150) | Answer benchmark |
| --- | --- | --- |
| Baseline | 101 | 15 / 25 (60%), median 73 s |
| Current | **145** | **20 / 24 (83%)**, median 32 s |

The [evaluation story](docs/evaluation-story.md) explains each version: why it was
changed, what changed, examples, and what was tried and dropped. Each version has a
[workflow diagram](docs/diagrams/versions/).

## Use from Claude Code and other AI assistants (MCP)

The app also serves its search as an [MCP](https://modelcontextprotocol.io) server at
`http://localhost:8000/mcp`. An employee's assistant can list, search and read the documents
their role allows, then write the answer itself; answer checks and Admin approval stay in the app.

![Two ways to use the knowledge base](docs/diagrams/mcp-access.png)

```sh
claude mcp add --transport http financial-research http://localhost:8000/mcp
```

Then run `/mcp` in Claude Code and choose **Authenticate**: a browser tab opens on the app, the
user signs in with their usual account and allows access (OAuth). Claude Code renews its access
hourly; a password change, deactivation or "sign out everywhere" stops it within the hour.

On FinanceBench the search tool returns the answer page within its default 8 results for 145 of
150 questions. The [MCP guide](docs/mcp-server.md) covers the tools, sign-in, limits and other clients.

## Code layout

| Folder | Contents |
| --- | --- |
| `frontend/` | React screens, Axios requests, styles and browser tests |
| `src/api/` | Sign-in, admin, MCP consent, conversations, approvals, PDF access and health routes |
| `src/graph/` | Graph construction, routing decisions and workflow nodes |
| `src/services/` | Retrieval, reranking, models, privacy, accounts and sessions, rate limits, persistence and request traces |
| `src/config/` | Settings, prompts and role permissions |
| `src/models/` | Request/response schemas and graph state |
| `src/mcp_server/` | MCP server: document search tools, OAuth sign-in and the `/mcp` endpoint |
| `scripts/seed_from_hf.py` | Restore the published knowledge base into an empty Qdrant collection |
| `scripts/build_contextual_collection.py` | Build the knowledge base with contextual chunk headers |
| `scripts/build_diagrams.py` | Generate the workflow diagrams |
| `scripts/manage_users.py` | Create accounts, set passwords, sign out, deactivate (`make users`) |
| `tests/` | Unit and integration regression checks |
| `tests/evaluation/financebench/` | Search check, MCP search check and answer benchmark |
| `docs/evaluation-story.md` | How each version was measured and improved |
| `docs/mcp-server.md` | MCP tools, connection, sign-in and limits |
| `docs/deployment.md` | Hosting with HTTPS (Caddy), production settings and backups |
| `docs/diagrams/` | Workflow diagrams, one per version, and editable sources |

## Checks

Backend tests use mocked model responses; account, session and OAuth tests run against an
isolated PostgreSQL database named in `TEST_POSTGRES_URL` (its name must end in `_test`).
Frontend tests intercept API requests.

```sh
python -m pytest tests/unit tests/integration
cd frontend
npm test
npm run build
```

Sign-in attempt limits and MCP call limits are counted per API process; question quotas are
counted in PostgreSQL. A single API process is the supported setup.

