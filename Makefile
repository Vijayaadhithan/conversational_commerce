PYTHON ?= .venv/bin/python

.PHONY: check lint format format-check test

check: lint format-check test

lint:
	$(PYTHON) -m ruff check app scripts tests

format:
	$(PYTHON) -m ruff format app scripts tests

format-check:
	$(PYTHON) -m ruff format --check app scripts tests

test:
	$(PYTHON) -m pytest -q
