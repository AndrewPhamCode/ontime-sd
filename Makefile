# Requires Docker Desktop running for anything touching the database.
.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

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

service-install: ## Install and start the launchd agent for 24/7 collection
	bash scripts/install-service.sh

service-uninstall: ## Stop and remove the launchd agent
	bash scripts/uninstall-service.sh

service-status: ## Show whether the launchd agent is running
	launchctl list | grep sd.ontime.collector || echo "not loaded"

service-logs: ## Follow the collector log
	tail -f "$$HOME/Library/Logs/ontime-sd/collector.log"

coverage: ## Show collection coverage and feed health from poll_log
	docker compose exec -T postgres psql -U ontime -d ontime_sd -f /dev/stdin < scripts/coverage.sql

.PHONY: help install up down nuke migrate mock run test lint fmt psql health \
	service-install service-uninstall service-status service-logs coverage
