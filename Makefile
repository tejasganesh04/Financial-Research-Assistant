.PHONY: up down restart logs ps seed-db build-kb run test test-unit test-integration lint format check clean

COMPOSE = docker compose -f compose.minimal.yml -f compose.local.yml

# --- Docker (see LOCAL_SETUP.md) ---
up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) up -d --no-deps --force-recreate api

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

# --- Knowledge base ---
seed-db:
	$(COMPOSE) exec -T api python scripts/seed_from_hf.py --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings --revision c31f0efb6eea1f33310eb82fed4d3b940367c21b

build-kb:
	$(COMPOSE) exec -T api python scripts/build_contextual_collection.py --target financial_docs_v3

# --- Local development (API on the host; Qdrant and PostgreSQL in Docker) ---
run:
	$(COMPOSE) up -d qdrant postgres
	uvicorn src.api.main:app --reload --port 8000

# --- Checks ---
test:
	pytest tests/unit/ tests/integration/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v --timeout=120

lint:
	ruff check src/ scripts/ tests/
	ruff format --check src/ scripts/ tests/

format:
	ruff check --fix src/ scripts/ tests/
	ruff format src/ scripts/ tests/

check: lint test-unit
	@echo "All checks passed"

clean:
	find src scripts tests -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find src scripts tests -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ htmlcov/ .coverage
