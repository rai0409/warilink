VENV := $(CURDIR)/.venv
PY := $(VENV)/bin
COMPOSE := docker compose -f infra/compose.yaml
DEV_DB := postgresql+psycopg://warilink:warilink-dev-only@localhost:55432/warilink

.PHONY: setup up up-ocr down db migrate api web worker test test-ocr test-stress lint e2e contracts

setup:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PY)/pip install -q uv
	cd apps/api && UV_PROJECT_ENVIRONMENT=$(VENV) $(PY)/uv sync --frozen --group ocr
	pnpm install --frozen-lockfile

up:
	$(COMPOSE) up -d --build db api web

up-ocr:
	$(COMPOSE) --profile ocr up -d --build db api web ocr-worker

down:
	$(COMPOSE) --profile ocr down

db:
	$(COMPOSE) up -d db

migrate:
	cd apps/api && WARILINK_DATABASE_URL=$(DEV_DB) $(PY)/alembic upgrade head

api:
	cd apps/api && WARILINK_DATABASE_URL=$(DEV_DB) $(PY)/uvicorn app.main:app --reload --port 8000

web:
	pnpm web:dev

worker:
	cd apps/api && WARILINK_DATABASE_URL=$(DEV_DB) $(PY)/python -m app.ocr.worker

lint:
	cd apps/api && $(PY)/ruff check app tests && $(PY)/mypy
	pnpm web:typecheck

test: lint
	cd apps/api && $(PY)/pytest

test-ocr:
	cd apps/api && $(PY)/pytest -m ocr

# 計算の大量テスト(数分)。結果は apps/api/var/stress-report.json。CI では実行しない
test-stress:
	cd apps/api && $(PY)/pytest -m stress -p no:cacheprovider

e2e:
	pnpm e2e

contracts:
	pnpm gen:contracts
