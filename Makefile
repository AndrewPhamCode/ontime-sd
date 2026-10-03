# Requires Docker Desktop running for anything touching the database.
.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Refuse to start a server on a port something else already owns.
#
# Without this, `make api` fails with a bare "address already in use" traceback
# and `make ui` silently moves to the next free port, so the UI then talks to an
# API that is not the one being edited. Both wasted real time during Phase 6.
# The check names the process holding the port, because the usual cause is an
# earlier run of this same target still in the background.
define check_port
	@if lsof -nP -iTCP:$(1) -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "Port $(1) is already in use by:"; \
		lsof -nP -iTCP:$(1) -sTCP:LISTEN | tail -n +2 | awk '{printf "  %s (pid %s)\n", $$1, $$2}'; \
		echo "Stop it first, or run: kill $$(lsof -ti tcp:$(1) -sTCP:LISTEN | tr '\n' ' ')"; \
		exit 1; \
	fi
endef

# Refuse to start something that needs the database when it is not reachable.
#
# Postgres being down is usually Docker Desktop not running, and the failure
# without this check is a forty line asyncpg traceback ending in ECONNREFUSED,
# which buries the one sentence that matters.
define check_db
	@if ! nc -z localhost $${PGPORT:-5433} >/dev/null 2>&1; then \
		echo "Postgres is not reachable on localhost:$${PGPORT:-5433}."; \
		echo "Start it with: make up    (needs Docker Desktop running)"; \
		exit 1; \
	fi
endef

install: ## Sync the virtualenv from pyproject.toml
	uv sync

up: ## Start Postgres in the background and wait for it to be healthy
	docker compose up -d --wait postgres

down: ## Stop Postgres, keeping the data volume
	docker compose down

nuke: ## Stop Postgres and delete all collected data
	docker compose down -v

migrate: ## Apply pending SQL migrations
	uv run ontime-migrate

mock: ## Run the mock GTFS-Realtime feed server
	uv run ontime-mock

run: ## Run the collector against whatever MTS_FEED_BASE_URL points at
	uv run ontime-collector

test: ## Run the test suite against the compose Postgres
	uv run pytest -q

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply formatting and autofixable lint rules
	uv run ruff check --fix .
	uv run ruff format .

psql: ## Open a psql shell on the compose database
	docker compose exec postgres psql -U ontime -d ontime_sd

health: ## Hit the collector health endpoint
	@curl -sS -i localhost:$${HEALTH_PORT:-8080}/healthz

load-gtfs: ## Download and load the static GTFS schedule (skips if unchanged)
	uv run ontime-load-gtfs

load-gtfs-force: ## Reload the schedule even if unchanged or already loaded
	uv run ontime-load-gtfs --force

infer-arrivals: ## Reconstruct arrivals from GPS for yesterday and today
	uv run ontime-infer-arrivals --days 2

evaluate: ## Score MTS predictions against inferred arrivals
	uv run ontime-evaluate --days 2

api: ## Run the read-only API on :8000
	$(call check_port,8000)
	$(call check_db)
	uv run ontime-api

ui: ## Run the web UI on :5174 (needs `make api` in another terminal)
	$(call check_port,5174)
	cd web && npm run dev

web-install: ## Install frontend dependencies
	cd web && npm install

web-check: ## Type check, lint, format check, test and build the frontend
	cd web && npm run typecheck && npm run lint && npm run format:check && npm test && npx vite build

web-types: ## Regenerate frontend API types from the running API
	cd web && npm run gen:types

model: ## Fit and score our predictors (train Sep 28-30, test Oct 1-2)
	uv run ontime-model --train-from 2026-09-28 --train-to 2026-09-30 --test-from 2026-10-01 --test-to 2026-10-02

compare: ## HEAD TO HEAD: our predictors vs MTS
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/compare.sql

baseline: ## THE NUMBER: MTS prediction error by horizon, route, time of day
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/baseline.sql

arrivals: ## Show inferred arrival counts and quality
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/arrivals.sql

schedule: ## Show loaded GTFS feed versions and recent load attempts
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/schedule.sql

service-install: ## Install and start both launchd agents (collector and weekly loader)
	bash scripts/install-service.sh

service-uninstall: ## Stop and remove both launchd agents
	bash scripts/uninstall-service.sh

service-status: ## Show whether the launchd agents are running
	@launchctl list | grep sd.ontime || echo "not loaded"

service-logs: ## Follow the collector log (gtfs.log for the loader)
	tail -f "$$HOME/Library/Logs/ontime-sd/collector.log"

infra-init: ## Initialise Terraform in infra/
	terraform -chdir=infra init

infra-plan: ## Show what would be created in AWS (reads infra/terraform.tfvars)
	terraform -chdir=infra plan

infra-apply: ## Create or update the AWS collector host. Costs money.
	terraform -chdir=infra apply

infra-destroy: ## Tear the AWS stack down. The data volume is protected and survives.
	terraform -chdir=infra destroy

infra-output: ## Show the host address, SSH command and cost estimate
	@terraform -chdir=infra output

migrate-to-aws: ## Copy collected history from this laptop to the cloud host
	@test -n "$(HOST)" || { echo "usage: make migrate-to-aws HOST=ec2-user@<ip>"; exit 2; }
	bash scripts/migrate-to-aws.sh "$(HOST)"

coverage: ## Show collection coverage and feed health from poll_log
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/coverage.sql

.PHONY: help install up down nuke migrate mock run test lint fmt psql health \
	service-install service-uninstall service-status service-logs coverage \
	load-gtfs load-gtfs-force schedule infer-arrivals arrivals evaluate baseline model compare \
	api ui web-install web-check web-types \
	infra-init infra-plan infra-apply infra-destroy infra-output migrate-to-aws
