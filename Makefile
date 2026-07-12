# gooseloop developer Makefile.
#
# `make check` is the umbrella - run it before pushing. Individual
# targets exist for granular use during work.
#
# Assumes the venv lives at ./.venv (created by `uv sync --extra dev`).
# Override PYTHON for CI or non-venv invocation, e.g. `PYTHON=python3 make check`.

PYTHON ?= .venv/bin/python

.PHONY: check test typecheck pre-release-check clean

# Umbrella: every check in one command.
check: test typecheck

test:
	$(PYTHON) -m pytest -q

# Scope (strict, gooseloop package only) lives in pyproject [tool.mypy].
typecheck:
	$(PYTHON) -m mypy

# Release-time check. Asserts pyproject [project].version matches the top
# CHANGELOG.md entry. Run before `uv publish`.
pre-release-check:
	$(PYTHON) scripts/pre_release_check.py

clean:
	rm -rf .pytest_cache dist/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
