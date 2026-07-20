# POSIX twin of Make.cmd. Keep the two in sync: contributors run whichever their
# platform gives them, and a target that exists in only one is a trap.
#
# The command strings here are identical to the ones CI runs (.github/workflows).
# If they ever diverge, CI is right and this file is the bug.
.PHONY: help install install-dev lint format type-check test test-cov integration \
        minio-up minio-down docs build clean pre-commit all-checks

PY ?= python
COMPOSE := docker compose -f tests/integration/docker-compose.yml

help:
	@echo "install      - install the package"
	@echo "install-dev  - install with dev + docs extras, then pre-commit hooks"
	@echo "lint         - ruff check"
	@echo "format       - ruff check --fix"
	@echo "type-check   - mypy strict"
	@echo "test         - pytest, integration deselected"
	@echo "test-cov     - pytest with coverage (gate: 80%)"
	@echo "integration  - pytest against the MinIO rig (needs minio-up)"
	@echo "minio-up     - start the local MinIO rig"
	@echo "minio-down   - stop the rig and delete its volumes"
	@echo "docs         - build the MkDocs site"
	@echo "build        - build sdist + wheel"
	@echo "clean        - remove build/test artefacts"
	@echo "pre-commit   - run every pre-commit hook over the whole tree"
	@echo "all-checks   - lint + type-check + test-cov"

install:
	$(PY) -m pip install -e .

install-dev:
	$(PY) -m pip install -e ".[dev,docs]" && pre-commit install

lint:
	ruff check src tests

format:
	ruff check src tests --fix

type-check:
	mypy src/bg_ai_model_management

test:
	pytest -q -m "not integration"

test-cov:
	pytest -q -m "not integration" --cov=src/bg_ai_model_management --cov-report=term-missing

# Skips cleanly when the rig is down: the suite gates on AIMM_IT_ENDPOINT, so a
# laptop without Docker sees skips rather than errors.
integration:
	pytest -q -m integration

minio-up:
	$(COMPOSE) up -d --wait

minio-down:
	$(COMPOSE) down -v

docs:
	mkdocs build --strict

build:
	$(PY) -m build

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov site

pre-commit:
	pre-commit run --all-files

all-checks: lint type-check test-cov
