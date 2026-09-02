FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GAINTT_DB_PATH=/data/gaintt.sqlite \
    PORT=8000
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
COPY gaintt/ ./gaintt/
COPY scripts/ ./scripts/
COPY CONTEXT.md TASK.md README.md ./
COPY docs/ ./docs/
COPY examples/ ./examples/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["sh", "-c", "exec .venv/bin/uvicorn gaintt.main:app --host 0.0.0.0 --port ${PORT}"]
