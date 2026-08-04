FROM node:24.11.1-alpine AS operator-ui

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web ./
RUN npm run build


FROM python:3.11-slim AS source

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.9.27
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=operator-ui /web/dist ./src/tenderguard/web_dist
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 tenderguard \
    && mkdir -p /app/var \
    && chown -R tenderguard:tenderguard /app/var

FROM source AS document-worker

RUN uv sync --frozen --no-editable --extra document-worker

ENV PATH="/app/.venv/bin:${PATH}"

USER tenderguard

CMD ["tenderguard", "dispatch-document-intake", "--max-events", "1"]

FROM source AS api

RUN uv sync --frozen --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

USER tenderguard

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn tenderguard.api.main:app --host 0.0.0.0 --port 8000"]
