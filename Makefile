PYTHON ?= python3

.PHONY: install test lint migrate run-demo compose-up compose-down

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

migrate:
	$(PYTHON) -m alembic -c db/alembic.ini upgrade head

run-demo:
	$(PYTHON) -m uvicorn demo_app.app.main:app --reload --port 8000

compose-up:
	docker compose up --build

compose-down:
	docker compose down
