.PHONY: install lint format typecheck security test unit integration coverage deps-up deps-down up down migrate check

PY ?= python

install:
	$(PY) -m pip install -r requirements-dev.txt

lint:
	ruff check src tests migrations
	ruff format --check src tests

format:
	ruff format src tests
	ruff check --fix src tests migrations

typecheck:
	mypy

security:
	bandit -q -r src -c pyproject.toml
	pip-audit -r requirements.txt --progress-spinner off

unit:
	pytest tests/unit tests/contract -q

# Needs PostgreSQL and Redis: `make deps-up` first. REQUIRE_INTEGRATION=1 turns "service unreachable" from a skip into a failure.
integration:
	REQUIRE_INTEGRATION=1 pytest tests/integration -q

test:
	REQUIRE_INTEGRATION=1 pytest -q

coverage:
	REQUIRE_INTEGRATION=1 pytest -q --cov --cov-report=term-missing --cov-fail-under=90

deps-up:
	docker compose up -d --wait postgres redis

deps-down:
	docker compose stop postgres redis

up:
	docker compose up -d --build --wait

down:
	docker compose down

migrate:
	alembic upgrade head

check: lint typecheck security coverage
