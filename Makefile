.PHONY: sync run test lint format typecheck check smoke-m0

sync:
	uv sync --locked --all-groups

run:
	uv run uvicorn repopilot.api:app --reload

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy src
	uv run pytest

smoke-m0:
	@uv run --frozen python scripts/m0_http_persistence_smoke.py
