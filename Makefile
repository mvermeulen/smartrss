.PHONY: check check-prereqs lint format typecheck test test-unit test-int test-cov runner-test \
        frontend-install frontend-lint frontend-typecheck frontend-build frontend-test frontend-test-cov \
        test-e2e install-client up down migrate backup restore-drill db-test-up db-test-down

UV ?= uv
BACKEND_DIR := backend
FRONTEND_DIR := frontend
FRONTEND_RUNTIME ?= host

check-prereqs:
	./scripts/check-prereqs.sh --mode dev

lint:
	$(UV) run --directory $(BACKEND_DIR) ruff check src tests alembic ../client-runner
	$(UV) run --directory $(BACKEND_DIR) ruff format --check src tests alembic ../client-runner

format:
	$(UV) run --directory $(BACKEND_DIR) ruff format src tests alembic ../client-runner
	$(UV) run --directory $(BACKEND_DIR) ruff check --fix src tests alembic ../client-runner

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

runner-test:
	PYTHONPATH=client-runner $(UV) run --directory $(BACKEND_DIR) pytest ../client-runner/tests --cov=smartrss_client_runner --cov-branch --cov-report=term-missing --cov-fail-under=90

frontend-install:
	npm --prefix $(FRONTEND_DIR) ci

frontend-lint:
ifeq ($(FRONTEND_RUNTIME),docker)
	docker run --rm -v "$$(pwd):/work" -w /work/frontend node:22-alpine npm run lint
else
	npm --prefix $(FRONTEND_DIR) run lint
endif

frontend-typecheck:
ifeq ($(FRONTEND_RUNTIME),docker)
	docker run --rm -v "$$(pwd):/work" -w /work/frontend node:22-alpine npm run typecheck
else
	npm --prefix $(FRONTEND_DIR) run typecheck
endif

frontend-build:
ifeq ($(FRONTEND_RUNTIME),docker)
	docker run --rm -v "$$(pwd):/work" -w /work/frontend node:22-alpine npm run build
else
	npm --prefix $(FRONTEND_DIR) run build
endif

frontend-test:
ifeq ($(FRONTEND_RUNTIME),docker)
	docker run --rm -v "$$(pwd):/work" -w /work/frontend node:22-alpine npm run test
else
	npm --prefix $(FRONTEND_DIR) run test
endif

frontend-test-cov:
ifeq ($(FRONTEND_RUNTIME),docker)
	docker run --rm -v "$$(pwd):/work" -w /work/frontend node:22-alpine sh -c "npm run test:cov && node ../scripts/check-frontend-coverage.mjs coverage/coverage-summary.json"
else
	npm --prefix $(FRONTEND_DIR) run test:cov
	node scripts/check-frontend-coverage.mjs $(FRONTEND_DIR)/coverage/coverage-summary.json
endif

test-e2e: db-test-up frontend-build
	./scripts/check-prereqs.sh --mode e2e
	npm --prefix $(FRONTEND_DIR) run test:e2e

check: check-prereqs lint typecheck test-cov runner-test frontend-lint frontend-typecheck frontend-test-cov

install-client:
	./scripts/install-client.sh $(INSTALL_CLIENT_ARGS)

up:
	./scripts/check-prereqs.sh --mode server
	docker compose up -d --build

down:
	docker compose down

migrate:
	./scripts/check-prereqs.sh --mode server
	docker compose exec api smartrss db migrate

backup:
	./scripts/backup.sh

restore-drill:
	./scripts/restore-drill.sh
