.PHONY: check lint format typecheck test test-unit test-int test-cov up down migrate backup restore-drill db-test-up db-test-down

UV ?= uv
BACKEND_DIR := backend

lint:
	$(UV) run --directory $(BACKEND_DIR) ruff check src tests alembic
	$(UV) run --directory $(BACKEND_DIR) ruff format --check src tests alembic

format:
	$(UV) run --directory $(BACKEND_DIR) ruff format src tests alembic
	$(UV) run --directory $(BACKEND_DIR) ruff check --fix src tests alembic

typecheck:
	$(UV) run --directory $(BACKEND_DIR) mypy src

db-test-up:
	POSTGRES_PASSWORD=$${POSTGRES_PASSWORD:-dev-test-only} docker compose --profile test up -d --wait db-test

db-test-down:
	POSTGRES_PASSWORD=$${POSTGRES_PASSWORD:-dev-test-only} docker compose --profile test down db-test

test-unit:
	$(UV) run --directory $(BACKEND_DIR) pytest tests/unit tests/boundary

test-int: db-test-up
	$(UV) run --directory $(BACKEND_DIR) pytest tests/integration

test: db-test-up
	$(UV) run --directory $(BACKEND_DIR) pytest

test-cov: db-test-up
	$(UV) run --directory $(BACKEND_DIR) pytest --cov=smartrss --cov-branch --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=80
	python3 scripts/check-critical-coverage.py $(BACKEND_DIR)/coverage.json
	rm -f $(BACKEND_DIR)/coverage.json

check: lint typecheck test-cov

up:
	docker compose up -d --build

down:
	docker compose down

migrate:
	docker compose exec api smartrss db migrate

backup:
	./scripts/backup.sh

restore-drill:
	./scripts/restore-drill.sh
