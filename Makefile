.PHONY: install dev test build e2e

UV_CACHE_DIR ?= /tmp/gaintt-uv-cache

install:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv sync --dev
	cd frontend && pnpm install

dev:
	cd frontend && pnpm build
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn gaintt.main:app --reload

test:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest -q
	cd frontend && pnpm test
	cd frontend && pnpm build
	cd frontend && pnpm test:e2e

build:
	cd frontend && pnpm build

e2e:
	cd frontend && pnpm test:e2e
