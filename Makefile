.PHONY: run dev test test-unit test-integration lint format seed-db docker-up docker-down docker-all docker-build docker-prod docker-logs docker-ps docker-restart check clean

COMPOSE = docker compose -f compose.minimal.yml -f compose.local.yml

# --- Development ---
run:
	uvicorn src.api.main:app --reload --port 8000

dev: docker-up
	uvicorn src.api.main:app --reload --port 8000

# --- Testing ---
test:
	pytest tests/unit/ tests/integration/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v --timeout=120

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff check --fix src/ tests/
	ruff format src/ tests/

# --- Data ---
seed-db:
	$(COMPOSE) exec -T api python scripts/seed_from_hf.py --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings --revision c31f0efb6eea1f33310eb82fed4d3b940367c21b

# --- Docker ---
docker-up:
	$(COMPOSE) up -d qdrant postgres redis

docker-down:
	$(COMPOSE) down

docker-all:
	$(COMPOSE) up --build

# --- Production ---
docker-build:
	$(COMPOSE) build

docker-prod:
	$(COMPOSE) up -d --build

docker-logs:
	$(COMPOSE) logs -f

docker-ps:
	$(COMPOSE) ps

docker-restart:
	$(COMPOSE) restart api

# --- Checks ---
check: lint test-unit
	@echo "All checks passed"

# --- Cleanup ---
clean:
	find src scripts tests -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find src scripts tests -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ htmlcov/ .coverage
