.PHONY: help install dev test cov lint fmt types check run check-config doctor curve dashboard replay tune docker clean

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
CONFIG ?= config.yaml
LOG ?= logs/trades.jsonl

help:            ## show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s "$$(printf '\t')"

install:         ## venv and runtime dependencies
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -r requirements.txt

dev: install     ## same plus linter, types, tests
	$(BIN)/pip install -r requirements-dev.txt

test:            ## run tests
	$(BIN)/python -m pytest

cov:             ## tests with coverage
	$(BIN)/python -m pytest --cov=src --cov-report=term-missing

lint:            ## ruff
	$(BIN)/ruff check .

fmt:             ## ruff with autofix
	$(BIN)/ruff check . --fix

types:           ## mypy
	$(BIN)/mypy src

check: lint types test  ## everything CI runs

check-config:    ## validate config, start nothing
	$(BIN)/python -m src.cli check --config $(CONFIG)

doctor:          ## pre-flight environment check
	$(BIN)/python -m src.cli doctor --config $(CONFIG)

curve:           ## what a trade on the curve costs
	$(BIN)/python -m src.cli curve

run:             ## start the pipeline (mode comes from the config)
	$(BIN)/python -m src.cli run --config $(CONFIG)

dashboard:       ## live dashboard from the log
	$(BIN)/python scripts/dashboard.py $(LOG) --watch 5

replay:          ## log summary
	$(BIN)/python scripts/replay.py $(LOG) --rotated

tune:            ## tune weights and threshold from the log
	$(BIN)/python scripts/tune.py $(LOG) --rotated

docker:          ## build the image
	docker build -t grokbot-pumpfun:latest .

clean:           ## remove build junk and caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ .coverage
