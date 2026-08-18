PYTHON ?= python3

.PHONY: install test coverage lint compile migrate run-demo run-agent compose-config \
	compose-build compose-up compose-down compose-logs load-smoke stack-smoke dashboard-install \
	dashboard-test dashboard-build terraform-fmt terraform-init terraform-validate \
	infrastructure-test secret-scan delivery-check

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest

coverage:
	$(PYTHON) -m pytest -q --cov=common --cov=db --cov=demo_app --cov=agent --cov-fail-under=85

lint:
	$(PYTHON) -m ruff check .

compile:
	$(PYTHON) -m compileall -q common db demo_app agent load_tests scripts tests

migrate:
	$(PYTHON) -m alembic -c db/alembic.ini upgrade head

run-demo:
	$(PYTHON) -m uvicorn demo_app.app.main:app --reload --port 8000

run-agent:
	$(PYTHON) -m uvicorn agent.app.main:create_app --factory --reload --port 8100

compose-config:
	docker compose config --quiet

compose-build:
	docker compose build demo-app agent dashboard

compose-up:
	docker compose up --build --wait

compose-down:
	docker compose down

compose-logs:
	docker compose logs --tail=200 postgres migrate demo-app agent dashboard

load-smoke:
	docker compose --profile load run --rm locust

stack-smoke:
	bash scripts/smoke_stack.sh

dashboard-install:
	pnpm --dir dashboard install --frozen-lockfile

dashboard-test:
	pnpm --dir dashboard test

dashboard-build:
	pnpm --dir dashboard build

terraform-fmt:
	terraform -chdir=infra fmt -check -recursive

terraform-init:
	terraform -chdir=infra init -backend=false

terraform-validate: terraform-init
	terraform -chdir=infra validate

infrastructure-test:
	$(PYTHON) -m pytest -q tests/unit/infra

secret-scan:
	$(PYTHON) scripts/secret_scan.py

delivery-check: compile lint coverage dashboard-install dashboard-test dashboard-build secret-scan
