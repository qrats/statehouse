.DEFAULT_GOAL := help
PYTHON ?= python
COMPOSE ?= docker compose -f docker/docker-compose.yml

.PHONY: help install lint fmt typecheck test cov dags smoke up down logs migrate clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install the package with dev extras
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

lint: ## Ruff
	ruff check src tests airflow scripts

fmt: ## Format in place
	ruff format src tests airflow scripts
	ruff check --fix src tests airflow scripts

typecheck: ## Mypy
	mypy

test: ## Unit tests
	pytest -q

cov: ## Unit tests with coverage
	pytest -q --cov=statehouse --cov-report=term-missing --cov-report=html

dags: ## Validate the DAG folder
	$(PYTHON) scripts/check_dags.py --dags airflow/dags

smoke: ## Probe live portals (network required)
	$(PYTHON) scripts/portal_smoke.py --jurisdictions all --report reports/smoke.json

up: ## Start the local stack
	$(COMPOSE) up -d --build

down: ## Stop the local stack
	$(COMPOSE) down -v

logs: ## Tail the scheduler
	$(COMPOSE) logs -f scheduler

migrate: ## Validate migrations without a database
	$(PYTHON) scripts/migrate.py --check-only --directory migrations

clean: ## Remove build and cache artefacts
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml reports
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
